from __future__ import annotations

from typing import Any

import pytest
import requests

from app.database import SessionLocal
from app.models import ArkConfig, User
from app.services.content_analysis.ark import (
    ArkAPIError,
    ArkClient,
    ark_client_from_config,
)
from app.services.content_analysis.ark_accounts import (
    ARK_DEFAULT_BASE_URL,
    save_ark_config,
)
from app.services.security import decrypt_secret
from tests.conftest import create_user, login


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.headers: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _admin_user_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "username": "ark-form-user",
        "password": "password123",
        "is_active": "true",
        "api_key": "runninghub-key",
        "base_url": "https://www.runninghub.cn",
        "ai_app_id": "2062251097452007426",
        "instance_type": "default",
        "default_prompt": "默认提示词",
        "max_concurrent_tasks": "1",
        "minimax_base_url": "https://api.minimaxi.com",
        "minimax_requests_per_minute": "20",
        "ark_enabled": "true",
        "ark_api_key": "ark-form-secret",
        "ark_base_url": ARK_DEFAULT_BASE_URL,
        "ark_model": "ep-test-model",
        "ark_timeout_seconds": "45",
        "ark_max_retries": "3",
    }
    payload.update(overrides)
    return payload


def test_ark_config_encrypts_key_and_blank_update_preserves_ciphertext() -> None:
    create_user("ark-config-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ark-config-user").one()
        config = save_ark_config(
            db,
            user,
            enabled=True,
            api_key="ark-secret-value",
            base_url=f"{ARK_DEFAULT_BASE_URL}/",
            model="ep-first",
            timeout_seconds=30,
            max_retries=2,
        )
        db.commit()
        db.refresh(config)
        original_ciphertext = config.api_key_encrypted
        assert original_ciphertext != "ark-secret-value"
        assert "ark-secret-value" not in (original_ciphertext or "")
        assert (
            decrypt_secret(original_ciphertext, label="豆包 Ark API Key")
            == "ark-secret-value"
        )

        save_ark_config(
            db,
            user,
            enabled=True,
            api_key="",
            base_url=ARK_DEFAULT_BASE_URL,
            model="ep-second",
            timeout_seconds=40,
            max_retries=1,
        )
        db.commit()
        db.refresh(config)
        assert config.api_key_encrypted == original_ciphertext
        assert config.model == "ep-second"
        assert config.timeout_seconds == 40


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"enabled": True, "api_key": "", "model": "ep"}, "API Key"),
        ({"enabled": True, "api_key": "secret", "model": ""}, "模型"),
        ({"base_url": "file:///tmp/ark"}, "Base URL"),
        ({"timeout_seconds": 0}, "1 到 120"),
        ({"max_retries": 6}, "0 到 5"),
    ],
)
def test_ark_config_rejects_invalid_settings(
    overrides: dict[str, Any], message: str
) -> None:
    create_user("ark-invalid-user")
    values: dict[str, Any] = {
        "enabled": False,
        "api_key": "",
        "base_url": ARK_DEFAULT_BASE_URL,
        "model": "",
        "timeout_seconds": 30,
        "max_retries": 2,
    }
    values.update(overrides)
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ark-invalid-user").one()
        with pytest.raises(ValueError, match=message):
            save_ark_config(db, user, **values)


def test_admin_form_saves_ark_config_without_ever_echoing_key(client) -> None:
    create_user("ark-admin", is_admin=True)
    login(client, "ark-admin")
    response = client.post(
        "/admin/users",
        data=_admin_user_payload(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ark-form-user").one()
        config = db.query(ArkConfig).filter_by(user_id=user.id).one()
        user_id = user.id
        assert config.enabled is True
        assert config.base_url == ARK_DEFAULT_BASE_URL
        assert config.model == "ep-test-model"
        assert config.timeout_seconds == 45
        assert config.max_retries == 3
        assert (
            decrypt_secret(config.api_key_encrypted, label="豆包 Ark API Key")
            == "ark-form-secret"
        )

    page = client.get(f"/admin/users/{user_id}")
    assert page.status_code == 200
    assert "豆包内容分析" in page.text
    assert "已加密保存，留空不修改" in page.text
    assert "ark-form-secret" not in page.text


def test_admin_cannot_enable_ark_without_key_or_model(client) -> None:
    create_user("ark-validation-admin", is_admin=True)
    login(client, "ark-validation-admin")
    response = client.post(
        "/admin/users",
        data=_admin_user_payload(ark_api_key="", ark_model=""),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Ark API Key" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.query(User).filter_by(username="ark-form-user").first() is None


def test_ark_client_sends_openai_compatible_request() -> None:
    session = FakeSession(
        [FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]})]
    )
    client = ArkClient(
        "ark-client-secret",
        base_url=ARK_DEFAULT_BASE_URL,
        model="ep-client-model",
        timeout_seconds=25,
        max_retries=0,
        session=session,
        sleep=lambda _: None,
    )
    result = client.create_chat_completion(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "private script"},
        ],
        response_format={"type": "json_object"},
        max_tokens=2048,
    )

    assert result["choices"][0]["message"]["content"] == "{}"
    assert session.headers["Authorization"] == "Bearer ark-client-secret"
    assert session.calls == [
        {
            "url": f"{ARK_DEFAULT_BASE_URL}/chat/completions",
            "json": {
                "model": "ep-client-model",
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "private script"},
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "max_tokens": 2048,
            },
            "timeout": 25,
        }
    ]


def test_ark_client_retries_timeout_429_and_5xx_then_succeeds() -> None:
    sleeps: list[float] = []
    session = FakeSession(
        [
            requests.Timeout("private script must not leak"),
            FakeResponse(429, {}, headers={"retry-after": "0.25"}),
            FakeResponse(503, {}, headers={"x-request-id": "retry-request"}),
            FakeResponse(200, {"ok": True}),
        ]
    )
    client = ArkClient(
        "retry-secret",
        base_url=ARK_DEFAULT_BASE_URL,
        model="ep-retry",
        max_retries=3,
        session=session,
        sleep=sleeps.append,
    )
    assert client.create_chat_completion(
        messages=[{"role": "user", "content": "private script"}]
    ) == {"ok": True}
    assert len(session.calls) == 4
    assert sleeps == [0.5, 0.25, 2.0]


def test_ark_client_failure_is_structured_and_does_not_leak_bodies() -> None:
    session = FakeSession(
        [
            FakeResponse(
                500,
                {"error": "raw-response private-script ark-secret"},
                headers={"x-request-id": "safe-request-id"},
            ),
            FakeResponse(
                500,
                {"error": "raw-response private-script ark-secret"},
                headers={"x-request-id": "safe-request-id"},
            ),
        ]
    )
    client = ArkClient(
        "ark-secret",
        base_url=ARK_DEFAULT_BASE_URL,
        model="ep-failure",
        max_retries=1,
        session=session,
        sleep=lambda _: None,
    )
    with pytest.raises(ArkAPIError) as captured:
        client.create_chat_completion(
            messages=[{"role": "user", "content": "private-script"}]
        )
    error = captured.value
    assert error.diagnostics == {
        "code": "ARK_HTTP_ERROR",
        "status_code": 500,
        "retryable": True,
        "request_id": "safe-request-id",
        "attempts": 2,
    }
    safe_text = str(error)
    assert "raw-response" not in safe_text
    assert "private-script" not in safe_text
    assert "ark-secret" not in safe_text


def test_ark_client_does_not_retry_non_retryable_http_error() -> None:
    session = FakeSession([FakeResponse(400, {"error": "bad request"})])
    client = ArkClient(
        "ark-secret",
        base_url=ARK_DEFAULT_BASE_URL,
        model="ep-no-retry",
        max_retries=5,
        session=session,
        sleep=lambda _: pytest.fail("400 must not sleep or retry"),
    )
    with pytest.raises(ArkAPIError) as captured:
        client.create_chat_completion(
            messages=[{"role": "user", "content": "script"}]
        )
    assert captured.value.status_code == 400
    assert captured.value.retryable is False
    assert captured.value.attempts == 1
    assert len(session.calls) == 1


def test_ark_client_can_be_built_from_encrypted_user_config() -> None:
    create_user("ark-client-config-user")
    session = FakeSession([FakeResponse(200, {"ok": True})])
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ark-client-config-user").one()
        config = save_ark_config(
            db,
            user,
            enabled=True,
            api_key="encrypted-client-key",
            base_url=ARK_DEFAULT_BASE_URL,
            model="ep-from-config",
            timeout_seconds=35,
            max_retries=0,
        )
        db.commit()
        client = ark_client_from_config(
            config,
            session=session,
            sleep=lambda _: None,
        )
        assert client.create_chat_completion(
            messages=[{"role": "user", "content": "script"}]
        ) == {"ok": True}
    assert session.headers["Authorization"] == "Bearer encrypted-client-key"
    assert session.calls[0]["timeout"] == 35


def test_deleting_user_cascades_to_ark_config() -> None:
    user = create_user("ark-cascade-user")
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        save_ark_config(
            db,
            attached,
            enabled=False,
            api_key="",
            base_url=ARK_DEFAULT_BASE_URL,
            model="",
            timeout_seconds=30,
            max_retries=2,
        )
        db.commit()
        assert db.query(ArkConfig).filter_by(user_id=user.id).count() == 1
        db.delete(attached)
        db.commit()
        assert db.query(ArkConfig).filter_by(user_id=user.id).count() == 0
