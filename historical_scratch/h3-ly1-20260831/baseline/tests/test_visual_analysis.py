from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from app.database import SessionLocal
from app.models import ArkConfig, User, VisualAnalysisCache
from app.services.security import encrypt_secret
from app.services.visual_analysis.analysis import (
    VISUAL_ANALYSIS_PROMPT_VERSION,
    _system_prompt,
    analyze_visual_context,
)
from app.services.visual_analysis.contracts import (
    VISUAL_ANALYSIS_REQUEST_SCHEMA_VERSION,
    VISUAL_ANALYSIS_SCHEMA_VERSION,
    VisualAnalysisContractError,
    parse_visual_analysis_request,
    parse_visual_analysis_result,
)
from tests.conftest import create_user


SCRIPT = "每天吃一个鸡蛋，不要鸡蛋里挑骨头。"


def _request() -> dict[str, Any]:
    first = SCRIPT.index("鸡蛋")
    second = SCRIPT.index("鸡蛋", first + 1)
    return {
        "schema_version": VISUAL_ANALYSIS_REQUEST_SCHEMA_VERSION,
        "original_script": SCRIPT,
        "script_sha256": hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest(),
        "catalog_version": "sha256:test-catalog",
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "char_start": first,
                "char_end": first + 2,
                "text": "鸡蛋",
                "allowed_concepts": [
                    {
                        "concept_id": "food.egg",
                        "description": "鸡蛋食物或食材",
                    }
                ],
            },
            {
                "candidate_id": "candidate-2",
                "char_start": second,
                "char_end": second + 2,
                "text": "鸡蛋",
                "allowed_concepts": [
                    {
                        "concept_id": "food.egg",
                        "description": "鸡蛋食物或食材",
                    }
                ],
            },
        ],
    }


def _result() -> dict[str, Any]:
    return {
        "schema_version": VISUAL_ANALYSIS_SCHEMA_VERSION,
        "script_sha256": _request()["script_sha256"],
        "catalog_version": _request()["catalog_version"],
        "decisions": [
            {
                "candidate_id": "candidate-1",
                "decision": "SHOW",
                "concept_id": "food.egg",
                "usage": "literal",
                "importance": 0.9,
                "confidence": 0.96,
                "reason_code": "LITERAL_CONCRETE_OBJECT",
            },
            {
                "candidate_id": "candidate-2",
                "decision": "SKIP",
                "concept_id": "food.egg",
                "usage": "idiom",
                "importance": 0.1,
                "confidence": 0.99,
                "reason_code": "SKIP_IDIOM",
            },
        ],
    }


def test_request_rejects_changed_text_and_extra_timing_fields() -> None:
    changed = _request()
    changed["candidates"][0]["text"] = "玉米"
    with pytest.raises(VisualAnalysisContractError):
        parse_visual_analysis_request(changed)

    timed = _request()
    timed["candidates"][0]["start_ms"] = 100
    with pytest.raises(VisualAnalysisContractError):
        parse_visual_analysis_request(timed)


def test_result_requires_every_candidate_once_and_allowed_concepts() -> None:
    request = parse_visual_analysis_request(_request())
    duplicate = _result()
    duplicate["decisions"][1]["candidate_id"] = "candidate-1"
    with pytest.raises(VisualAnalysisContractError):
        parse_visual_analysis_result(duplicate, request=request)

    unknown = _result()
    unknown["decisions"][0]["concept_id"] = "food.unknown"
    with pytest.raises(VisualAnalysisContractError):
        parse_visual_analysis_result(unknown, request=request)

    timed = _result()
    timed["decisions"][0]["start_us"] = 100
    with pytest.raises(VisualAnalysisContractError):
        parse_visual_analysis_result(timed, request=request)

    review = _result()
    review["decisions"][0].update(
        {
            "decision": "REVIEW",
            "usage": "uncertain",
            "reason_code": "SKIP_UNCERTAIN",
        }
    )
    parsed_review = parse_visual_analysis_result(review, request=request)
    assert parsed_review.decisions[0].decision.value == "REVIEW"


def test_seam_candidate_contract_and_prompt_define_a_real_relevance_floor() -> None:
    request = _request()
    request["candidates"] = [
        {
            **request["candidates"][0],
            "usage": "seam_broll",
            "direct_concept_ids": ["food.egg"],
            "segment_boundary_us": 2_000_000,
        }
    ]

    parsed = parse_visual_analysis_request(request)
    assert parsed.candidates[0].usage == "seam_broll"
    prompt = _system_prompt()
    assert "频率强行选择" in prompt
    assert "宽泛" in prompt
    assert "素材画面本身可能带网络文字" in prompt
    assert "只允许选择 direct_concept_ids" in prompt
    assert "不得使用" in prompt and "MATCH_EDITORIAL_CONTEXT" in prompt
    assert VISUAL_ANALYSIS_PROMPT_VERSION == "jyd.visual-analysis.prompt.v3"


class FakeArkClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create_chat_completion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _configured_user() -> int:
    user = create_user("visual-analysis-user", with_config=False)
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        db.add(
            ArkConfig(
                user=attached,
                enabled=True,
                api_key_encrypted=encrypt_secret("test-ark-key"),
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="test-model",
                timeout_seconds=30,
                max_retries=2,
            )
        )
        db.commit()
    return user.id


def test_visual_analysis_calls_ark_once_and_reuses_exact_cache() -> None:
    user_id = _configured_user()
    fake = FakeArkClient(
        {
            "id": "visual-response-1",
            "choices": [
                {"message": {"content": json.dumps(_result(), ensure_ascii=False)}}
            ],
        }
    )

    with SessionLocal() as db:
        user = db.get(User, user_id)
        first = analyze_visual_context(
            db, user, payload=_request(), client_factory=lambda _: fake
        )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        second = analyze_visual_context(
            db, user, payload=_request(), client_factory=lambda _: fake
        )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        refreshed = analyze_visual_context(
            db,
            user,
            payload=_request(),
            force_refresh=True,
            client_factory=lambda _: fake,
        )

    assert first["analysis_status"] == "SUCCESS"
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert refreshed["cache_hit"] is False
    assert len(fake.calls) == 2
    assert "start_ms" not in fake.calls[0]["messages"][1]["content"]
    with SessionLocal() as db:
        assert db.query(VisualAnalysisCache).count() == 1


def test_invalid_provider_response_is_not_cached() -> None:
    user_id = _configured_user()
    invalid = _result()
    invalid["decisions"] = invalid["decisions"][:1]
    fake = FakeArkClient(
        {"choices": [{"message": {"content": json.dumps(invalid)}}]}
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        result = analyze_visual_context(
            db, user, payload=_request(), client_factory=lambda _: fake
        )
    assert result["analysis_status"] == "FAILED"
    assert result["error"]["code"] == "VISUAL_RESPONSE_INVALID"
    with SessionLocal() as db:
        assert db.query(VisualAnalysisCache).count() == 0


def test_workbench_visual_analysis_endpoint_keeps_strict_payload(
    client, monkeypatch
) -> None:
    create_user("visual-analysis-api", with_config=False)
    login = client.post(
        "/api/auth/center/login",
        json={"username": "visual-analysis-api", "password": "password123"},
    )
    token = login.json()["access_token"]
    captured: dict[str, Any] = {}

    def fake_analyze(db, user, *, payload, force_refresh=False):
        captured.update(payload=payload, force_refresh=force_refresh, user_id=user.id)
        return {"analysis_status": "SUCCESS", "decisions": []}

    monkeypatch.setattr("app.routes.workbench.analyze_visual_context", fake_analyze)
    response = client.post(
        "/api/workbench/visual-analysis",
        json={
            "access_token": token,
            **_request(),
            "force_refresh": True,
        },
    )

    assert response.status_code == 200
    assert captured["payload"] == _request()
    assert "access_token" not in captured["payload"]
    assert captured["force_refresh"] is True
