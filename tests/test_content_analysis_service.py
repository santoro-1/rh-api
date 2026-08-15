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
    _content_analysis_max_tokens,
    analyze_content,
    build_ark_messages,
)
from app.services.content_analysis.ark import ArkAPIError
from app.services.content_analysis.contracts import (
    CONTENT_ANALYSIS_PROVIDER_SCHEMA_VERSION,
    boundary_indexed_script,
    parse_content_analysis_provider_payload,
)
from app.services.security import encrypt_secret
from tests.conftest import create_user


FIXTURE = Path(__file__).parent / "fixtures" / "content_analysis_v1_valid.json"
SCRIPT = "那么通过八十四天"


def _valid_payload() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["title"] = {"line_1": "减脂真相", "line_2": "坚持更关键"}
    return payload


def _provider_payload(
    *,
    prefer_after: list[int] | None = None,
    allow_after: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "music_intent": _valid_payload()["music_intent"],
        "subtitle_breaks": {
            "prefer_after": prefer_after or [],
            "allow_after": allow_after or [],
        },
        "visual_plan": [],
        "title": {"line_1": "减脂真相", "line_2": "坚持更关键"},
    }


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
    assert fake.calls[0]["max_tokens"] == 1536
    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    with SessionLocal() as db:
        record = db.query(ContentAnalysisCache).one()
        assert record.script_length == len(SCRIPT)
        assert record.cacheable is True
        assert record.provider_request_id == "resp-test"


def test_analysis_accepts_json_wrapped_in_markdown_and_explanation() -> None:
    user_id = _configured_user("markdown-json")
    content = (
        "以下是分析结果：\n```json\n"
        + json.dumps(_valid_payload(), ensure_ascii=False)
        + "\n```\n请按此结果处理。"
    )
    fake = FakeArkClient(
        [{"id": "resp-markdown", "choices": [{"message": {"content": content}}]}]
    )

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "SUCCESS"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert result["provider_request_id"] == "resp-markdown"


def test_analysis_accepts_json_with_unfenced_intro_text() -> None:
    user_id = _configured_user("intro-json")
    content = "分析结果如下：\n" + json.dumps(_valid_payload(), ensure_ascii=False)
    fake = FakeArkClient(
        [{"id": "resp-intro", "choices": [{"message": {"content": content}}]}]
    )

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "SUCCESS"
    assert result["provider_request_id"] == "resp-intro"


def test_ark_request_uses_self_contained_schema_and_explicit_root_contract() -> None:
    user_id = _configured_user("provider-schema")
    fake = FakeArkClient([_ark_response(_valid_payload())])

    _analyze(user_id, fake)

    response_schema = fake.calls[0]["response_format"]["json_schema"]["schema"]
    serialized_schema = json.dumps(response_schema, ensure_ascii=False)
    system_prompt = fake.calls[0]["messages"][0]["content"]
    assert '"$ref"' not in serialized_schema
    assert response_schema["required"] == [
        "music_intent",
        "subtitle_breaks",
        "visual_plan",
        "title",
    ]
    assert "一次完成 music_intent、subtitle_breaks、visual_plan、title 四项任务" in system_prompt
    assert "每项仅含 anchor_id、concept_id、priority" in system_prompt
    assert "anchor.usage=enrichment" in system_prompt
    assert "anchor.usage=seam_broll" in system_prompt
    assert "priority=1" in system_prompt
    assert "concept_id 以 editorial. 开头的是编辑型空镜池" in system_prompt
    assert "连接处不是必填项" in system_prompt
    assert "只有宽泛大类相同、一个多义词、健康主题相同" in system_prompt
    assert "素材画面自带网络文字不是" in system_prompt
    assert "editorial.meal_daily" in system_prompt
    assert "绝不能为了填满频率或连接处强行填充" in system_prompt
    assert "不返回时间戳" in system_prompt


def test_long_script_gets_full_safe_output_budget_in_one_request() -> None:
    assert _content_analysis_max_tokens("健" * 730) == 2920
    assert _content_analysis_max_tokens("健" * 2000, visual_anchor_count=200) == 4096


def test_music_only_provider_object_is_partial_without_a_second_request() -> None:
    user_id = _configured_user("provider-music-only")
    payload = _valid_payload()
    fake = FakeArkClient(
        [_ark_response(payload["music_intent"], request_id="resp-music-only")]
    )

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "PARTIAL"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["music_intent"] == payload["music_intent"]
    assert result["subtitle_analysis_status"] == "FAILED"
    assert result["subtitle_units"] is None
    assert result["errors"]["subtitle"]["code"] == "SUBTITLE_MISSING"
    assert result["provider_request_id"] == "resp-music-only"
    assert result["provider_attempts"] == 1
    assert len(fake.calls) == 1


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


def test_title_failure_is_isolated_from_music_subtitle_and_visual_results() -> None:
    user_id = _configured_user("partial-title")
    payload = _provider_payload()
    payload["title"] = {"line_1": "一二三四五六", "line_2": "坚持更关键"}
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "PARTIAL"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert result["visual_analysis_status"] == "SUCCESS"
    assert result["title_analysis_status"] == "FAILED"
    assert result["title"] is None
    assert result["errors"]["title"]["code"] == "TITLE_SCHEMA_INVALID"
    assert result["cacheable"] is True
    assert len(fake.calls) == 1


def test_title_second_line_overflow_is_isolated_from_other_results() -> None:
    user_id = _configured_user("partial-title-line-2")
    payload = _provider_payload()
    payload["title"] = {"line_1": "健康真相", "line_2": "一二三四五六"}
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "PARTIAL"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert result["visual_analysis_status"] == "SUCCESS"
    assert result["title_analysis_status"] == "FAILED"
    assert result["title"] is None
    assert result["errors"]["title"]["code"] == "TITLE_SCHEMA_INVALID"
    assert len(fake.calls) == 1


def test_subtitle_mismatch_debug_snapshot_is_explicitly_opt_in(
    monkeypatch,
    tmp_path,
) -> None:
    user_id = _configured_user("subtitle-debug")
    payload = _valid_payload()
    payload["subtitle_units"][1]["text"] = "通道"
    monkeypatch.setenv("CONTENT_ANALYSIS_DEBUG_CAPTURE", "true")
    monkeypatch.setenv("CONTENT_ANALYSIS_DEBUG_DIR", str(tmp_path))

    result = _analyze(user_id, FakeArkClient([_ark_response(payload)]))

    assert result["subtitle_analysis_status"] == "FAILED"
    snapshots = list(tmp_path.glob("subtitle-mismatch-*.json"))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert snapshot["original_script"] == SCRIPT
    assert snapshot["returned_subtitle_text"] == "那么通道八十四天"
    assert snapshot["first_difference"] == {
        "index": 3,
        "source_character": "过",
        "returned_character": "道",
    }
    assert snapshot["subtitle_units"] == payload["subtitle_units"]


def test_schema_version_mismatch_debug_snapshot_is_explicitly_opt_in(
    monkeypatch,
    tmp_path,
) -> None:
    user_id = _configured_user("contract-debug")
    payload = _valid_payload()
    payload["schema_version"] = "jyd.content-analysis.v0"
    monkeypatch.setenv("CONTENT_ANALYSIS_DEBUG_CAPTURE", "true")
    monkeypatch.setenv("CONTENT_ANALYSIS_DEBUG_DIR", str(tmp_path))

    result = _analyze(user_id, FakeArkClient([_ark_response(payload, "resp-contract")]))

    assert result["overall_status"] == "FAILED"
    snapshots = list(tmp_path.glob("contract-failure-*.json"))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert snapshot["error_code"] == "SCHEMA_VERSION_MISMATCH"
    assert snapshot["expected_schema_version"] == CONTENT_ANALYSIS_PROVIDER_SCHEMA_VERSION
    assert snapshot["received_schema_version"] == "jyd.content-analysis.v0"
    assert snapshot["provider_request_id"] == "resp-contract"
    assert snapshot["original_script"] == SCRIPT
    assert snapshot["provider_payload"] == payload


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
    system_prompt = messages[0]["content"]

    assert len(messages) == 2
    assert "一次完成 music_intent、subtitle_breaks、visual_plan、title 四项任务" in system_prompt
    assert "长脚本也不能" in system_prompt
    assert "不得超过 13 个全角中文字符等效宽度" in system_prompt
    assert "13 是上限，不是固定长度" in system_prompt
    assert "只选最少量自然边界" in system_prompt
    assert "封面标题必须独立满足平台安全" in system_prompt
    assert "控重" in system_prompt
    assert "禁止用谐音字" in system_prompt
    assert "生活提醒" in system_prompt
    assert "不返回时间戳" in system_prompt
    user_payload = json.loads(messages[1]["content"])
    assert set(user_payload) == {
        "original_script",
        "boundary_indexed_script",
        "visual_context",
    }
    assert user_payload["original_script"] == script
    assert user_payload["boundary_indexed_script"] == boundary_indexed_script(script)

    assert user_payload["visual_context"] == {
        "catalog_version": "none",
        "concepts": [],
        "anchors": [],
    }
    example_json = system_prompt.split("输出示例：\n", maxsplit=1)[1]
    example_payload = json.loads(example_json)
    assert example_payload["subtitle_breaks"] == {
        "prefer_after": [6],
        "allow_after": [],
    }
    _, example_units, visual_plan, title = parse_content_analysis_provider_payload(
        example_payload,
        original_script="百分之八十四是由呼吸离开身体的",
    )
    assert [unit.text for unit in example_units] == [
        "百分之八十四",
        "是由呼吸离开身体的",
    ]
    assert visual_plan == []
    assert title.line_1 == "减脂真相"
    assert title.line_2 == "坚持更关键"


def test_one_call_visual_plan_uses_only_offered_local_candidates() -> None:
    user_id = _configured_user("visual-plan")
    visual_context = {
        "catalog_version": "food-motion-v1",
        "concepts": [
            {"concept_id": "food.cucumber", "description": "黄瓜菜品"},
            {"concept_id": "exercise.walk", "description": "步行动作"},
        ],
        "anchors": [
            {
                "anchor_id": "B2",
                "char_start": 2,
                "char_end": 4,
                "text": "通过",
                "context": "通过步行改善状态",
                "usage": "explicit",
                "allowed_concepts": ["exercise.walk"],
            }
        ],
    }
    payload = _provider_payload(prefer_after=[4])
    payload["visual_plan"] = [
        {"anchor_id": "VA2", "concept_id": "exercise.walk", "priority": 1}
    ]
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(
        user_id,
        fake,
        visual_context_payload=visual_context,
    )

    assert result["overall_status"] == "SUCCESS"
    assert result["visual_analysis_status"] == "SUCCESS"
    assert result["visual_catalog_version"] == "food-motion-v1"
    assert result["visual_plan"] == payload["visual_plan"]
    request_payload = json.loads(fake.calls[0]["messages"][1]["content"])
    assert request_payload["visual_context"] == {
        **visual_context,
        "anchors": [{**visual_context["anchors"][0], "anchor_id": "VA2"}],
    }


def test_one_invalid_visual_reference_is_dropped_without_failing_valid_choices() -> None:
    user_id = _configured_user("visual-plan-partial-repair")
    visual_context = {
        "catalog_version": "food-motion-v1",
        "concepts": [
            {"concept_id": "exercise.walk", "description": "步行动作"},
        ],
        "anchors": [
            {
                "anchor_id": "VA2",
                "char_start": 2,
                "char_end": 4,
                "text": "通过",
                "context": "通过八十四天",
                "usage": "explicit",
                "allowed_concepts": ["exercise.walk"],
            }
        ],
    }
    payload = _provider_payload(prefer_after=[4])
    payload["visual_plan"] = [
        {"anchor_id": "VA2", "concept_id": "food.not-offered", "priority": 1},
        {"anchor_id": "VA99", "concept_id": "exercise.walk", "priority": 1},
        {"anchor_id": "VA2", "concept_id": "exercise.walk", "priority": 1},
    ]
    fake = FakeArkClient([_ark_response(payload)])

    result = _analyze(
        user_id,
        fake,
        visual_context_payload=visual_context,
    )

    assert result["visual_analysis_status"] == "SUCCESS"
    assert result["visual_plan"] == [payload["visual_plan"][2]]


def test_compact_provider_breaks_expand_to_public_subtitle_units() -> None:
    user_id = _configured_user("provider-breaks")
    fake = FakeArkClient([_ark_response(_provider_payload(prefer_after=[4]))])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "SUCCESS"
    assert result["schema_version"] == "jyd.content-analysis.v1"
    assert [unit["text"] for unit in result["subtitle_units"]] == [
        "那么通过",
        "八十四天",
    ]
    assert [
        (unit["start"], unit["end"], unit["break_after"])
        for unit in result["subtitle_units"]
    ] == [(0, 4, "prefer"), (4, 8, "prefer")]
    assert len(fake.calls) == 1


def test_semantic_break_plan_uses_positions_without_returning_split_text() -> None:
    script = "百分之八十四是由呼吸的形式来离开我们身体的"
    user_id = _configured_user("semantic-breaks")
    provider_payload = _provider_payload(prefer_after=[6], allow_after=[13])
    fake = FakeArkClient([_ark_response(provider_payload)])

    with SessionLocal() as db:
        result = analyze_content(
            db,
            db.get(User, user_id),
            original_script=script,
            client_factory=lambda _: fake,
        )

    assert [unit["text"] for unit in result["subtitle_units"]] == [
        "百分之八十四",
        "是由呼吸的形式",
        "来离开我们身体的",
    ]
    assert [unit["break_after"] for unit in result["subtitle_units"]] == [
        "prefer",
        "allow",
        "prefer",
    ]
    assert provider_payload["subtitle_breaks"] == {
        "prefer_after": [6],
        "allow_after": [13],
    }


def test_punctuation_breaks_are_added_locally_without_model_output() -> None:
    script = "第一句，第二句"
    user_id = _configured_user("local-punctuation-break")
    fake = FakeArkClient([_ark_response(_provider_payload())])

    with SessionLocal() as db:
        result = analyze_content(
            db,
            db.get(User, user_id),
            original_script=script,
            client_factory=lambda _: fake,
        )

    assert [unit["text"] for unit in result["subtitle_units"]] == ["第一句，", "第二句"]
    assert [unit["break_after"] for unit in result["subtitle_units"]] == [
        "prefer",
        "prefer",
    ]


def test_unoffered_break_position_is_dropped_without_failing_subtitle_branch() -> None:
    user_id = _configured_user("invalid-provider-break")
    fake = FakeArkClient([_ark_response(_provider_payload(prefer_after=[4, 99]))])

    result = _analyze(user_id, fake)

    assert result["overall_status"] == "SUCCESS"
    assert result["music_analysis_status"] == "SUCCESS"
    assert result["subtitle_analysis_status"] == "SUCCESS"
    assert result["errors"]["subtitle"] is None
    assert [unit["text"] for unit in result["subtitle_units"]] == [
        "那么通过",
        "八十四天",
    ]
    assert len(fake.calls) == 1


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
