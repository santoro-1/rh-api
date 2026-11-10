from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import threading
from typing import Any

from jyd_probe.project_content_analysis import ProjectContentAnalysisCoordinator
from jyd_probe.project_store import ProjectStore
from jyd_probe.semantic_visuals import load_semantic_visual_catalog
from jyd_probe.unified_visual_plan import (
    _compatibility_decisions,
    build_local_visual_result,
    prepare_unified_visual_input,
    refresh_saved_visual_item,
)


CATALOG_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "libraries"
    / "semantic_visual_library"
)


class UnifiedClient:
    def __init__(
        self,
        *,
        invalid_visual: bool = False,
        priority: int = 2,
        usage: str | None = None,
    ) -> None:
        self.invalid_visual = invalid_visual
        self.priority = priority
        self.usage = usage
        self.calls: list[dict[str, Any]] = []

    def analyze_workbench_content(
        self,
        _token: str,
        original_script: str,
        *,
        force_refresh: bool = False,
        visual_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "script": original_script,
                "force_refresh": force_refresh,
                "visual_context": visual_context,
            }
        )
        anchors = visual_context["anchors"] if visual_context is not None else []
        visual_plan: list[dict[str, Any]] = []
        if anchors:
            anchor = next(
                (
                    item
                    for item in anchors
                    if self.usage is None or item["usage"] == self.usage
                ),
                anchors[0],
            )
            concept_id = anchor["allowed_concepts"][0]
            if self.invalid_visual:
                concept_id = "food.not-offered"
            visual_plan = [
                {
                    "anchor_id": anchor["anchor_id"],
                    "concept_id": concept_id,
                    "priority": self.priority,
                }
            ]
        return {
            "schema_version": "jyd.content-analysis.v1",
            "prompt_version": "jyd.content-analysis.prompt.v7",
            "script_sha256": hashlib.sha256(
                original_script.encode("utf-8")
            ).hexdigest(),
            "script_length": len(original_script),
            "model": "doubao-test",
            "overall_status": "SUCCESS",
            "music_analysis_status": "SUCCESS",
            "subtitle_analysis_status": "SUCCESS",
            "title_analysis_status": "SUCCESS",
            "visual_analysis_status": "SUCCESS",
            "music_intent": {"primary_scene": "health_education"},
            "title": {"line_1": "长期坚持", "line_2": "习惯很重要"},
            "subtitle_units": [
                {
                    "start": 0,
                    "end": len(original_script),
                    "text": original_script,
                    "kind": "phrase",
                    "bind": "none",
                    "break_after": "allow",
                }
            ],
            "visual_catalog_version": (
                visual_context["catalog_version"] if visual_context is not None else None
            ),
            "visual_plan": visual_plan,
            "errors": {
                "music": None,
                "subtitle": None,
                "title": None,
                "visual": None,
            },
            "provider_request_id": "unified-test",
            "provider_attempts": 1,
            "cache_hit": False,
            "cacheable": True,
        }


def test_visual_input_uses_current_cue_duration_when_base_metadata_is_legacy() -> None:
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    prepared = prepare_unified_visual_input(
        {
            "script_text": "鸡蛋是常见食物。",
            "outputs": {
                "audio": {"asset_id": "audio-1", "version": 1, "metadata": {}},
                "base_video": {"asset_id": "video-1", "metadata": {"segment_count": 1}},
            },
            "subtitles": {
                "bound_audio_asset_id": "audio-1",
                "raw_cues": [
                    {
                        "start_us": 0,
                        "end_us": 9_900_000,
                        "text": "鸡蛋是常见食物。",
                    }
                ],
            },
        },
        catalog,
    )
    assert prepared.video_duration_us == 9_900_000


class BlockingUnifiedClient(UnifiedClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze_workbench_content(
        self,
        token: str,
        original_script: str,
        *,
        force_refresh: bool = False,
        visual_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release unified analysis")
        return super().analyze_workbench_content(
            token,
            original_script,
            force_refresh=force_refresh,
            visual_context=visual_context,
        )


def _project(
    tmp_path: Path,
    *,
    with_raw_cues: bool,
) -> tuple[ProjectStore, dict[str, Any], str]:
    script = "每天吃一个鸡蛋"
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="统一视觉测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    if with_raw_cues:
        item = project["items"][0]
        project = store.set_item_subtitles(
            "user-1",
            project["project_id"],
            item["item_id"],
            {
                **item["subtitles"],
                "raw_cues": [
                    {"start_us": 0, "end_us": 3_000_000, "text": script}
                ],
            },
        )
    return store, project, script


def test_one_cloud_call_builds_existing_local_visual_recipe(tmp_path: Path) -> None:
    store, project, _script = _project(tmp_path, with_raw_cues=True)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = UnifiedClient()

    result = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=catalog,
    ).analyze("user-1", project["project_id"], "token")

    item = result["items"][0]
    assert len(client.calls) == 1
    assert item["content_analysis"]["overall_status"] == "SUCCESS", item[
        "content_analysis"
    ]
    assert item["visual_analysis"]["analysis_status"] == "SUCCESS"
    assert item["visual_analysis"]["mapping_status"] == "SUCCESS"
    assert len(item["visual_analysis"]["visual_plan"]) == 1
    assert len(item["visual_analysis"]["recipe"]["overlays"]) == 1
    anchor = client.calls[0]["visual_context"]["anchors"][0]
    assert anchor["anchor_id"] == "START" or anchor["anchor_id"].startswith("VA")
    assert anchor["usage"] == "explicit"
    assert anchor["text"] in anchor["context"]
    context_text = str(client.calls[0]["visual_context"])
    for forbidden in ("start_us", "duration_us", "asset_id", "image_path", "video_path"):
        assert forbidden not in context_text


def test_saved_plan_rebinds_after_raw_cues_without_another_cloud_call(
    tmp_path: Path,
) -> None:
    store, project, script = _project(tmp_path, with_raw_cues=False)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = UnifiedClient()
    analyzed = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=catalog,
    ).analyze("user-1", project["project_id"], "token")
    item = analyzed["items"][0]
    assert item["visual_analysis"]["analysis_status"] == "SUCCESS"
    assert item["visual_analysis"]["mapping_status"] == "FAILED"
    assert len(item["visual_analysis"]["visual_plan"]) == 1

    changed = store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 3_000_000, "text": script}],
        },
    )
    changed_item = changed["items"][0]
    assert changed_item["visual_analysis"]["mapping_status"] == "NOT_REQUESTED"
    rebound_project = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=catalog,
    ).analyze("user-1", project["project_id"], "token")
    rebound = rebound_project["items"][0]
    assert rebound["visual_analysis"]["mapping_status"] == "SUCCESS"
    assert len(rebound["visual_analysis"]["recipe"]["overlays"]) == 1
    assert len(client.calls) == 1


def test_saved_plan_refreshes_to_compatible_new_catalog_without_cloud(
    tmp_path: Path,
) -> None:
    store, project, _script = _project(tmp_path, with_raw_cues=True)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = UnifiedClient()
    analyzed = ProjectContentAnalysisCoordinator(
        store, client, visual_catalog=catalog
    ).analyze("user-1", project["project_id"], "token")
    item = analyzed["items"][0]
    refreshed_catalog = replace(catalog, catalog_version="sha256:test-new-catalog")

    outcome = refresh_saved_visual_item(
        store,
        owner_user_id="user-1",
        project_id=project["project_id"],
        item=item,
        catalog=refreshed_catalog,
    )

    refreshed = store.get_project("user-1", project["project_id"])["items"][0]
    assert outcome == "remapped"
    assert refreshed["visual_analysis"]["catalog_version"] == refreshed_catalog.catalog_version
    assert refreshed["visual_analysis"]["mapping_status"] == "SUCCESS"
    assert len(client.calls) == 1


def test_incompatible_new_catalog_becomes_explicitly_retryable_without_cloud(
    tmp_path: Path,
) -> None:
    store, project, _script = _project(tmp_path, with_raw_cues=True)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = UnifiedClient()
    analyzed = ProjectContentAnalysisCoordinator(
        store, client, visual_catalog=catalog
    ).analyze("user-1", project["project_id"], "token")
    item = analyzed["items"][0]
    empty_catalog = replace(
        catalog,
        catalog_version="sha256:test-empty-catalog",
        concepts=(),
        assets=(),
    )

    outcome = refresh_saved_visual_item(
        store,
        owner_user_id="user-1",
        project_id=project["project_id"],
        item=item,
        catalog=empty_catalog,
    )

    refreshed = store.get_project("user-1", project["project_id"])["items"][0]
    assert outcome == "retryable"
    assert refreshed["visual_analysis"]["analysis_status"] == "NOT_REQUESTED"
    assert refreshed["visual_analysis"]["error"]["code"] == "VISUAL_CATALOG_CHANGED"
    assert len(client.calls) == 1


def test_raw_cues_arriving_during_cloud_analysis_are_mapped_without_retry(
    tmp_path: Path,
) -> None:
    store, project, script = _project(tmp_path, with_raw_cues=False)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = BlockingUnifiedClient()
    result: dict[str, Any] = {}

    def analyze() -> None:
        result.update(
            ProjectContentAnalysisCoordinator(
                store,
                client,
                visual_catalog=catalog,
            ).analyze("user-1", project["project_id"], "token")
        )

    worker = threading.Thread(target=analyze)
    worker.start()
    assert client.started.wait(timeout=5)
    current_item = store.get_project("user-1", project["project_id"])["items"][0]
    store.set_item_subtitles(
        "user-1",
        project["project_id"],
        current_item["item_id"],
        {
            **current_item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 3_000_000, "text": script}],
        },
    )
    client.release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    item = result["items"][0]
    assert item["visual_analysis"]["mapping_status"] == "SUCCESS"
    assert len(item["visual_analysis"]["recipe"]["overlays"]) == 1
    assert len(client.calls) == 1


def test_invalid_visual_plan_does_not_discard_content_branches(tmp_path: Path) -> None:
    store, project, _script = _project(tmp_path, with_raw_cues=True)
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    result = ProjectContentAnalysisCoordinator(
        store,
        UnifiedClient(invalid_visual=True),
        visual_catalog=catalog,
    ).analyze("user-1", project["project_id"], "token")

    item = result["items"][0]
    assert item["content_analysis"]["overall_status"] == "SUCCESS"
    assert item["visual_analysis"]["analysis_status"] == "FAILED"
    assert item["visual_analysis"]["recipe"]["overlays"] == []


def test_one_cloud_call_skips_long_gap_when_only_taxonomy_fallback_exists(
    tmp_path: Path,
) -> None:
    script = "控制体重需要长期坚持，日常轻活动和生活安排都要逐步调整。" * 8
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="长口播 B-roll 测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    item = project["items"][0]
    project = store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [
                {"start_us": 0, "end_us": 70_000_000, "text": script}
            ],
        },
    )
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    client = UnifiedClient(usage="enrichment")

    result = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=catalog,
    ).analyze("user-1", project["project_id"], "token")

    analysis = result["items"][0]["visual_analysis"]
    assert len(client.calls) == 1
    assert len(analysis["visual_plan"]) == 1
    assert any(
        anchor["usage"] == "enrichment"
        for anchor in client.calls[0]["visual_context"]["anchors"]
    )
    assert set(analysis["visual_plan"][0]) == {"anchor_id", "concept_id", "priority"}
    assert analysis["decisions"][0]["decision"] == "SHOW"
    assert analysis["recipe"]["overlays"] == []


def test_enrichment_priority_one_requires_review(tmp_path: Path) -> None:
    script = "三伏天也可以安排日常轻活动，节奏放慢并逐步养成习惯。" * 8
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="无关 B-roll 安全门测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    item = project["items"][0]
    project = store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 40_000_000, "text": script}],
        },
    )

    result = ProjectContentAnalysisCoordinator(
        store,
        UnifiedClient(priority=1, usage="enrichment"),
        visual_catalog=load_semantic_visual_catalog(CATALOG_ROOT),
    ).analyze("user-1", project["project_id"], "token")

    analysis = result["items"][0]["visual_analysis"]
    assert analysis["visual_plan"][0]["priority"] == 1
    assert analysis["decisions"][0]["decision"] == "REVIEW"
    assert analysis["recipe"]["overlays"] == []


def test_enrichment_priority_two_still_requires_a_direct_script_concept() -> None:
    candidate_request = {
        "candidates": [
            {
                "candidate_id": "ve_0",
                "char_start": 0,
                "usage": "enrichment",
                "direct_concept_ids": ["food.apple"],
                "allowed_concepts": [
                    {"concept_id": "food.apple", "description": "苹果"},
                    {
                        "concept_id": "editorial.mood_atmosphere",
                        "description": "泛氛围",
                    },
                ],
            }
        ]
    }

    weak = _compatibility_decisions(
        [
            {
                "anchor_id": "START",
                "concept_id": "editorial.mood_atmosphere",
                "priority": 2,
            }
        ],
        candidate_request,
    )
    strong = _compatibility_decisions(
        [{"anchor_id": "START", "concept_id": "food.apple", "priority": 2}],
        candidate_request,
    )

    assert weak[0]["decision"] == "REVIEW"
    assert strong[0]["decision"] == "SHOW"


def test_real_activity_script_uses_explicit_action_video_in_one_cloud_call(
    tmp_path: Path,
) -> None:
    script = "今天跟着我做胯下击掌燃脂操，动作放慢，保持呼吸稳定。"
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="运动动作统一分析测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    item = project["items"][0]
    project = store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 8_000_000, "text": script}],
        },
    )
    client = UnifiedClient()

    result = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=load_semantic_visual_catalog(CATALOG_ROOT),
    ).analyze("user-1", project["project_id"], "token")

    analysis = result["items"][0]["visual_analysis"]
    assert len(client.calls) == 1
    assert len(analysis["visual_plan"]) == 1
    assert set(analysis["visual_plan"][0]) == {
        "anchor_id",
        "concept_id",
        "priority",
    }
    overlay = analysis["recipe"]["overlays"][0]
    assert overlay["usage"] == "explicit"
    assert overlay["asset_id"] == "activity.aerobic.crotch_clap.video.01"
    assert overlay["media_type"] == "video"
    assert overlay["corner"] == "bottom_center"
    assert overlay["scale"] == 0.615


def test_real_script_without_matching_material_stays_empty_after_one_cloud_call(
    tmp_path: Path,
) -> None:
    script = "今天聊聊怎样整理一天的心情，先放慢节奏，再认真听听自己的感受。"
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="无适合素材统一分析测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    item = project["items"][0]
    project = store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 9_000_000, "text": script}],
        },
    )
    client = UnifiedClient()

    result = ProjectContentAnalysisCoordinator(
        store,
        client,
        visual_catalog=load_semantic_visual_catalog(CATALOG_ROOT),
    ).analyze("user-1", project["project_id"], "token")

    analysis = result["items"][0]["visual_analysis"]
    assert len(client.calls) == 1
    assert client.calls[0]["visual_context"] is None
    assert analysis["analysis_status"] == "SUCCESS"
    assert analysis["visual_plan"] == []
    assert analysis["recipe"]["overlays"] == []


def test_real_segment_boundary_flows_into_local_seam_broll_recipe(tmp_path: Path) -> None:
    script = "前面先热身。接着做胯下击掌燃脂操，保持呼吸。"
    segment_1 = tmp_path / "segment-1.mp4"
    segment_2 = tmp_path / "segment-2.mp4"
    segment_1.write_bytes(b"segment-1")
    segment_2.write_bytes(b"segment-2")
    item = {
        "script_text": script,
        "subtitles": {
            "raw_cues": [
                {"start_us": 0, "end_us": 2_000_000, "text": "前面先热身。"},
                {
                    "start_us": 2_000_000,
                    "end_us": 6_000_000,
                    "text": "接着做胯下击掌燃脂操，保持呼吸。",
                },
            ]
        },
        "outputs": {
            "base_video": {
                "managed_path": str(tmp_path / "base.mp4"),
                "metadata": {"duration_us": 6_000_000, "segment_count": 2},
                "external_ref": {"source_task_ids": ["task-1", "task-2"]},
            },
            "original_video_segments": [
                {
                    "asset_id": "segment-1",
                    "status": "READY",
                    "managed_path": str(segment_1),
                    "external_ref": {"video_index": 1, "remote_task_id": "task-1"},
                    "metadata": {
                        "start_seconds": 0.0,
                        "end_seconds": 2.0,
                        "actual_duration_us": 2_000_000,
                        "script_text": "前面先热身。",
                    },
                },
                {
                    "asset_id": "segment-2",
                    "status": "READY",
                    "managed_path": str(segment_2),
                    "external_ref": {"video_index": 2, "remote_task_id": "task-2"},
                    "metadata": {
                        "start_seconds": 2.0,
                        "end_seconds": 6.0,
                        "actual_duration_us": 4_000_000,
                        "script_text": "接着做胯下击掌燃脂操，保持呼吸。",
                    },
                },
            ],
        },
    }
    catalog = load_semantic_visual_catalog(CATALOG_ROOT)
    visual_input = prepare_unified_visual_input(item, catalog)
    anchor = next(
        value
        for value in visual_input.visual_context["anchors"]
        if value["usage"] == "seam_broll"
    )
    result, recipe = build_local_visual_result(
        script=script,
        visual_input=visual_input,
        plan=[
            {
                "anchor_id": anchor["anchor_id"],
                "concept_id": "activity.aerobic",
                "priority": 2,
            }
        ],
        catalog=catalog,
        provider_payload={"provider_attempts": 1},
    )

    assert result["analysis_status"] == "SUCCESS"
    assert visual_input.segment_boundaries == [
        {
            "boundary_us": 2_000_000,
            "segment_index": 2,
            "segment_start_us": 2_000_000,
            "segment_end_us": 6_000_000,
            "script_text": "接着做胯下击掌燃脂操，保持呼吸。",
        }
    ]
    seam = next(item for item in recipe["overlays"] if item["usage"] == "seam_broll")
    assert seam["start_us"] == 1_500_000
    assert seam["duration_us"] == 1_000_000
    assert seam["segment_boundary_us"] == 2_000_000
    assert seam["media_type"] == "video"


def test_unified_visual_input_does_not_offer_editorial_pools_for_enrichment() -> None:
    item = {
        "script_text": "长期改变来自每天做得到的小选择。" * 12,
        "settings": {"source_metadata": {"article_type": "鸡汤文"}},
        "outputs": {"base_video": {"metadata": {"duration_us": 60_000_000}}},
    }
    visual_input = prepare_unified_visual_input(
        item, load_semantic_visual_catalog(CATALOG_ROOT)
    )
    offered = {
        concept_id
        for anchor in visual_input.visual_context["anchors"]
        if anchor["usage"] == "enrichment"
        for concept_id in anchor["allowed_concepts"]
        if concept_id.startswith("editorial.")
    }

    assert offered == set()
