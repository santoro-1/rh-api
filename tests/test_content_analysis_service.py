from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from typing import Any

from app.database import SessionLocal
from app.config import get_settings
from app.models import ArkConfig, ContentAnalysisCache, User
from app.services.content_analysis.analysis import (
    ArkConcurrencyLimiter,
    analyze_content,
    build_ark_messages,
)
from app.services.content_analysis.ark import ArkAPIError
from app.services.security import encrypt_secret
from tests.conftest import create_user


FIXTURE = Path(__file__).parent / "fixtures" / "content_analysis_v1_valid.json"
SCRIPT = "那么通过八十四天"


def _valid_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _ark_response(payload: dict[str, Any], request_id: str = "resp-test") -> dict[str, Any]:
    return {
        "id": request_id,
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
    }


def _configured_user(username: str = "analysis-user") -> int:
    user = create_user(username, with_config=False)
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        db.add(
            ArkConfig(
                user=attached,
                enabled=True,
                api_key_encrypted=encrypt_secret("test-ark-key"),
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-seed-2-0-lite-260428",
                timeout_seconds=30,
                max_retries=2,
            )
        )
        db.commit()
    return user.id


class FakeArkClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _analyze(user_id: int, fake: FakeArkClient, **kwargs: Any) -> dict[str, Any]:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return analyze_content(
            db,
            user,
            original_script=SCRIPT,
            client_factory=lambda _: fake,
            **kwargs,
        )


def test_analysis_saves_valid_branches_and_reuses_cache() -> None:
    user_id = _configured_user()
    fake = FakeArkClient([_ark_response(_valid_payload())])

    first = _analyze(user_id, fake)
    second = _analyze(user_id, fake)

    assert first["overall_status"] == "SUCCESS"
    assert first["music_analysis_status"] == "SUCCESS"
    assert first["subtitle_analysis_status"] == "SUCCESS"
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["music_intent"] == first["music_intent"]
    assert second["subtitle_units"] == first["subtitle_units"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["temperature"] == 0.0
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    with SessionLocal() as db:
        record = db.query(ContentAnalysisCache).one()
        assert record.script_length == len(SCRIPT)
        assert record.cacheable is True
        assert record.provider_request_id == "resp-test"


def test_subtitle_failure_does_not_discard_valid_music() -> None:
    user_id = _configured_user("partial-music")
    payload = _valid_payload()
    payload["subtitle_units"][1]["text"] = "通道"
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "PARTIAL"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["music_intent"] is not None
    assert result["subtitle_analysis_status"] == "FAILED"
    assert result["subtitle_units"] is None
    assert result["errors"]["subtitle"]["code"] == "SUBTITLE_TEXT_MISMATCH"
    assert result["cacheable"] is True


def test_music_failure_does_not_discard_valid_subtitles() -> None:
    user_id = _configured_user("partial-subtitles")
    payload = _valid_payload()
    payload["music_intent"]["primary_scene"] = "not-a-scene"
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "PARTIAL"
    assert result["music_analysis_status"] == "FAILED"
    assert result["music_intent"] is None
    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert result["subtitle_units"] is not None


def test_subtitle_indexes_are_rebuilt_only_when_text_is_exact() -> None:
    user_id = _configured_user("repair-indexes")
    payload = _valid_payload()
    for index, unit in enumerate(payload["subtitle_units"]):
        unit["start"] = 100 + index
        unit["end"] = 200 + index
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(user_id, fake)

    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert [(unit["start"], unit["end"]) for unit in result["subtitle_units"]] == [
        (0, 2),
        (2, 4),
        (4, 7),
        (7, 8),
    ]


def test_force_refresh_failure_never_overwrites_previous_success() -> None:
    user_id = _configured_user("preserve-success")
    fake = FakeArkClient(
        [
            _ark_response(_valid_payload(), "resp-good"),
            {"id": "resp-bad", "choices": [{"message": {"content": "not-json"}}]},
        ]
    )

    first = _analyze(user_id, fake)
    refreshed = _analyze(user_id, fake, force_refresh=True)

    assert first["overall_status"] == "SUCCESS"
    assert refreshed["overall_status"] == "SUCCESS"
    assert refreshed["music_intent"] == first["music_intent"]
    assert refreshed["subtitle_units"] == first["subtitle_units"]
    assert refreshed["provider_request_id"] == "resp-good"
    assert len(fake.calls) == 2


def test_complete_transport_failure_is_not_sticky_cache() -> None:
    user_id = _configured_user("retry-failure")
    fake = FakeArkClient(
        [
            ArkAPIError("ARK_TIMEOUT", "safe", retryable=True, attempts=3),
            _ark_response(_valid_payload()),
        ]
    )

    failed = _analyze(user_id, fake)
    retried = _analyze(user_id, fake)

    assert failed["overall_status"] == "FAILED"
    assert failed["cacheable"] is False
    assert failed["provider_attempts"] == 3
    assert retried["overall_status"] == "SUCCESS"
    assert len(fake.calls) == 2


def test_default_prompt_treats_exact_script_as_data_and_forbids_timestamps() -> None:
    script = "  那么\n通过八十四天  "
    messages = build_ark_messages(script)

    assert len(messages) == 2
    assert "不得返回或推测任何字幕时间戳" in messages[0]["content"]
    assert json.loads(messages[1]["content"])["original_script"] == script


def test_ark_limiter_allows_at_most_ten_active_calls() -> None:
    limiter = ArkConcurrencyLimiter(10)
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def work() -> None:
        nonlocal active, peak
        with limiter.slot(2):
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: work(), range(20)))

    assert peak == 10
    assert limiter.active == 0
    assert get_settings().ark_max_concurrency == 10


def test_workbench_analysis_endpoint_calls_service_and_then_cache(
    client,
    monkeypatch,
) -> None:
    _configured_user("analysis-api-real")
    login = client.post(
        "/api/auth/center/login",
        json={"username": "analysis-api-real", "password": "password123"},
    )
    token = login.json()["access_token"]
    fake = FakeArkClient([_ark_response(_valid_payload())])
    monkeypatch.setattr(
        "app.services.content_analysis.analysis.ark_client_from_config",
        lambda _: fake,
    )

    first = client.post(
        "/api/workbench/content-analysis",
        json={"access_token": token, "original_script": SCRIPT},
    )
    second = client.post(
        "/api/workbench/content-analysis",
        json={"access_token": token, "original_script": SCRIPT},
    )

    assert first.status_code == 200
    assert first.json()["overall_status"] == "SUCCESS"
    assert first.json()["cache_hit"] is False
    assert second.status_code == 200
    assert second.json()["cache_hit"] is True
    assert len(fake.calls) == 1


def test_workbench_analysis_endpoint_keeps_script_exact(client, monkeypatch) -> None:
    create_user("analysis-api", with_config=False)
    login = client.post(
        "/api/auth/center/login",
        json={"username": "analysis-api", "password": "password123"},
    )
    token = login.json()["access_token"]
    captured: dict[str, Any] = {}

    def fake_analyze(db, user, *, original_script, force_refresh=False):
        captured.update(
            user_id=user.id,
            original_script=original_script,
            force_refresh=force_refresh,
        )
        return {"overall_status": "SUCCESS"}

    monkeypatch.setattr("app.routes.workbench.analyze_content", fake_analyze)
    response = client.post(
        "/api/workbench/content-analysis",
        json={
            "access_token": token,
            "original_script": "  原文\n不能 trim  ",
            "force_refresh": True,
        },
    )

    assert response.status_code == 200
    assert captured["original_script"] == "  原文\n不能 trim  "
    assert captured["force_refresh"] is True


def test_workbench_analysis_endpoint_requires_valid_token(client) -> None:
    response = client.post(
        "/api/workbench/content-analysis",
        json={"access_token": "invalid", "original_script": SCRIPT},
    )
    assert response.status_code == 401
