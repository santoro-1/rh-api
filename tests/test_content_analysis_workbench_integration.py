from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from app.database import SessionLocal
from app.models import ArkConfig, User
from app.services.content_analysis.analysis import analyze_content
from app.services.content_analysis.contracts import parse_content_visual_context
from app.services.security import encrypt_secret
from tests.conftest import create_user


RUNNINGHUB_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_ROOT = (
    RUNNINGHUB_ROOT.parent.parent / "公寓" / "jyd_plain_json_probe"
)
WORKBENCH_SRC = WORKBENCH_ROOT / "src"
WORKBENCH_AUDIO_ROOT = WORKBENCH_ROOT / "data" / "libraries" / "audio_library"
FIXTURE = Path(__file__).parent / "fixtures" / "content_analysis_v1_valid.json"
SCRIPT = "那么通过八十四天"

if not WORKBENCH_SRC.is_dir():
    raise RuntimeError(f"跨项目验收缺少剪映工作台源码: {WORKBENCH_SRC}")
sys.path.append(str(WORKBENCH_SRC))

from jyd_probe.music_matching import MusicProfileMatcher  # noqa: E402
from jyd_probe.project_content_analysis import _validated_remote_result  # noqa: E402
from jyd_probe.semantic_subtitles import (  # noqa: E402
    map_subtitle_units_to_raw_cues,
    semantic_break_groups,
)
from jyd_probe.semantic_visuals import (  # noqa: E402
    load_semantic_visual_catalog,
    recall_semantic_visual_candidates,
)
from jyd_probe.unified_visual_plan import (  # noqa: E402
    build_content_visual_context,
    validate_remote_visual_plan,
)


class _FakeArkClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def create_chat_completion(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": f"acceptance-{self.calls}",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.payload, ensure_ascii=False)
                    }
                }
            ],
        }


def _fixture_payload() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["title"] = {"line_1": "减脂真相", "line_2": "坚持更关键"}
    return payload


def _provider_payload(*, prefer_after: list[int]) -> dict[str, Any]:
    return {
        "music_intent": _fixture_payload()["music_intent"],
        "subtitle_breaks": {
            "prefer_after": prefer_after,
            "allow_after": [],
        },
        "visual_plan": [],
        "title": {"line_1": "减脂真相", "line_2": "坚持更关键"},
    }


def _analyze(
    *,
    username: str,
    script: str,
    payload: dict[str, Any],
    visual_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = create_user(username, with_config=False)
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        db.add(
            ArkConfig(
                user=attached,
                enabled=True,
                api_key_encrypted=encrypt_secret("acceptance-test-key"),
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-seed-2-0-lite-260428",
                timeout_seconds=30,
                max_retries=0,
            )
        )
        db.commit()

    fake = _FakeArkClient(payload)
    with SessionLocal() as db:
        attached = db.get(User, user.id)
        result = analyze_content(
            db,
            attached,
            original_script=script,
            visual_context_payload=visual_context,
            client_factory=lambda _config: fake,
        )
    assert fake.calls == 1
    return result


def _consume_in_workbench(result: dict[str, Any], script: str) -> dict[str, Any]:
    return _validated_remote_result(result, original_script=script)


def test_server_success_contract_drives_workbench_subtitles_and_top1() -> None:
    result = _analyze(
        username="cross-project-success",
        script=SCRIPT,
        payload=_provider_payload(prefer_after=[4]),
    )

    consumed = _consume_in_workbench(result, SCRIPT)
    timed_units = map_subtitle_units_to_raw_cues(
        SCRIPT,
        consumed["subtitle_units"],
        [{"start_us": 0, "end_us": 8_000_000, "text": SCRIPT}],
    )
    groups = semantic_break_groups(timed_units)
    selection = MusicProfileMatcher(WORKBENCH_AUDIO_ROOT).recommend(
        consumed["music_intent"],
        video_duration_us=8_000_000,
    )

    assert consumed["music_analysis_status"] == "SUCCESS"
    assert consumed["subtitle_analysis_status"] == "SUCCESS"
    assert [group["text"] for group in groups] == ["那么通过", "八十四天"]
    assert selection["bgm_identity"].startswith("music_id:")
    assert "candidates" not in selection
    assert "top3" not in selection


def test_server_and_workbench_share_one_selected_only_visual_plan() -> None:
    script = "每天吃一个鸡蛋"
    catalog = load_semantic_visual_catalog(
        WORKBENCH_ROOT / "data" / "libraries" / "semantic_visual_library"
    )
    candidate_request = recall_semantic_visual_candidates(script, catalog)
    visual_context = build_content_visual_context(candidate_request)
    anchor = visual_context["anchors"][0]
    payload = _provider_payload(prefer_after=[])
    payload["visual_plan"] = [
        {
            "anchor_id": anchor["anchor_id"],
            "concept_id": anchor["allowed_concepts"][0],
            "priority": 2,
        }
    ]

    result = _analyze(
        username="cross-project-visual-plan",
        script=script,
        payload=payload,
        visual_context=visual_context,
    )
    consumed = _consume_in_workbench(result, script)
    visual = validate_remote_visual_plan(
        result,
        candidate_request=candidate_request,
    )

    assert consumed["music_analysis_status"] == "SUCCESS"
    assert consumed["subtitle_analysis_status"] == "SUCCESS"
    assert visual["analysis_status"] == "SUCCESS"
    assert visual["visual_plan"] == payload["visual_plan"]


def test_periodic_and_seam_broll_only_expose_contextual_concepts() -> None:
    catalog = load_semantic_visual_catalog(
        WORKBENCH_ROOT / "data" / "libraries" / "semantic_visual_library"
    )
    opening = "日常轻活动要循序渐进，按照自己的节奏慢慢坚持。" * 8
    next_segment = "接下来安排日常轻活动，并留意身体感受。"
    script = opening + next_segment
    candidate_request = recall_semantic_visual_candidates(
        script,
        catalog,
        video_duration_us=60_000_000,
        segment_boundaries=[
            {
                "boundary_us": 50_000_000,
                "script_text": next_segment,
            }
        ],
    )
    visual_context = build_content_visual_context(candidate_request)

    parsed = parse_content_visual_context(visual_context, original_script=script)
    enrichment = [item for item in parsed.anchors if item.usage == "enrichment"]
    seams = [item for item in parsed.anchors if item.usage == "seam_broll"]

    assert enrichment
    assert seams
    assert all(len(item.allowed_concepts) <= 8 for item in enrichment)
    assert all("activity.light_daily" in item.allowed_concepts for item in enrichment)
    assert "activity.light_daily" in seams[0].allowed_concepts


def test_server_partial_results_keep_each_valid_workbench_branch() -> None:
    subtitle_invalid = _fixture_payload()
    subtitle_invalid["subtitle_units"][1]["text"] = "通道"
    music_result = _consume_in_workbench(
        _analyze(
            username="cross-project-music-only",
            script=SCRIPT,
            payload=subtitle_invalid,
        ),
        SCRIPT,
    )
    selection = MusicProfileMatcher(WORKBENCH_AUDIO_ROOT).recommend(
        music_result["music_intent"],
        video_duration_us=8_000_000,
    )

    music_invalid = _fixture_payload()
    music_invalid["music_intent"]["primary_scene"] = "unknown-scene"
    subtitle_result = _consume_in_workbench(
        _analyze(
            username="cross-project-subtitle-only",
            script=SCRIPT,
            payload=music_invalid,
        ),
        SCRIPT,
    )
    timed_units = map_subtitle_units_to_raw_cues(
        SCRIPT,
        subtitle_result["subtitle_units"],
        [{"start_us": 0, "end_us": 8_000_000, "text": SCRIPT}],
    )

    assert music_result["music_analysis_status"] == "SUCCESS"
    assert music_result["subtitle_analysis_status"] == "FAILED"
    assert music_result["subtitle_units"] is None
    assert selection["bgm_identity"].startswith("music_id:")
    assert subtitle_result["music_analysis_status"] == "FAILED"
    assert subtitle_result["subtitle_analysis_status"] == "SUCCESS"
    assert subtitle_result["music_intent"] is None
    assert "".join(unit["text"] for unit in timed_units) == SCRIPT


def test_repaired_indexes_preserve_spaces_newlines_and_tilde_across_projects() -> None:
    script = " 那么\n通过八十四天~"
    payload = _fixture_payload()
    unit_specs = [
        (" ", "whitespace", "none", "avoid"),
        ("那么", "connector", "right", "avoid"),
        ("\n", "whitespace", "both", "avoid"),
        ("通过", "word", "none", "allow"),
        ("八十四", "number", "right", "avoid"),
        ("天", "word", "left", "avoid"),
        ("~", "punctuation", "left", "prefer"),
    ]
    payload["subtitle_units"] = [
        {
            "start": 100 + index,
            "end": 200 + index,
            "text": text,
            "kind": kind,
            "bind": bind,
            "break_after": break_after,
        }
        for index, (text, kind, bind, break_after) in enumerate(unit_specs)
    ]

    consumed = _consume_in_workbench(
        _analyze(
            username="cross-project-exact-characters",
            script=script,
            payload=payload,
        ),
        script,
    )
    units = consumed["subtitle_units"]
    timed_units = map_subtitle_units_to_raw_cues(
        script,
        units,
        [
            {
                "start_us": 0,
                "end_us": 10_000_000,
                "text": "那么通过八十四天~",
            }
        ],
    )

    assert [(unit["start"], unit["end"]) for unit in units] == [
        (0, 1),
        (1, 3),
        (3, 4),
        (4, 6),
        (6, 9),
        (9, 10),
        (10, 11),
    ]
    assert "".join(unit["text"] for unit in timed_units) == script
    assert timed_units[-1]["text"] == "~"
