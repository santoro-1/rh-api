from __future__ import annotations

from pathlib import Path
from typing import Any

from jyd_probe.project_store import ProjectStore
from jyd_probe.project_visual_analysis import (
    ProjectVisualAnalysisCoordinator,
    _is_strong_automatic_broll_decision,
)
from jyd_probe.semantic_visuals import load_semantic_visual_catalog


CATALOG_ROOT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "libraries"
    / "semantic_visual_library"
)


class FakeVisualClient:
    def __init__(self, *, show_first: bool = True) -> None:
        self.show_first = show_first
        self.calls: list[dict[str, Any]] = []

    def analyze_workbench_visuals(
        self,
        token: str,
        payload: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(payload)
        decisions = []
        for index, candidate in enumerate(payload["candidates"]):
            show = self.show_first and index == 0
            decisions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "decision": "SHOW" if show else "SKIP",
                    "concept_id": candidate["allowed_concepts"][0]["concept_id"],
                    "usage": "literal" if show else "idiom",
                    "importance": 0.9 if show else 0.1,
                    "confidence": 0.96,
                    "reason_code": (
                        "LITERAL_CONCRETE_OBJECT" if show else "SKIP_IDIOM"
                    ),
                }
            )
        return {
            "schema_version": "jyd.visual-analysis.v1",
            "analysis_status": "SUCCESS",
            "script_sha256": payload["script_sha256"],
            "catalog_version": payload["catalog_version"],
            "decisions": decisions,
            "provider_request_id": "visual-test",
            "provider_attempts": 1,
            "cache_hit": False,
            "cacheable": True,
            "error": None,
        }


def _project(tmp_path: Path, script: str) -> tuple[ProjectStore, dict[str, Any]]:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="语义贴图测试",
        items=[{"row_key": "1", "script_text": script}],
    )
    item = project["items"][0]
    store.set_item_subtitles(
        "user-1",
        project["project_id"],
        item["item_id"],
        {
            **item["subtitles"],
            "raw_cues": [{"start_us": 0, "end_us": 4_000_000, "text": script}],
        },
    )
    return store, store.get_project("user-1", project["project_id"])


def test_automatic_broll_gate_rejects_editorial_and_non_direct_matches() -> None:
    candidate = {
        "candidate_id": "vs_1",
        "direct_concept_ids": ["food.apple"],
    }
    strong = {
        "candidate_id": "vs_1",
        "decision": "SHOW",
        "concept_id": "food.apple",
        "usage": "literal",
        "confidence": 0.96,
        "reason_code": "MATCH_EXACT_OBJECT",
    }

    assert _is_strong_automatic_broll_decision(strong, candidate) is True
    assert _is_strong_automatic_broll_decision(
        {
            **strong,
            "concept_id": "editorial.mood_atmosphere",
            "usage": "editorial_context",
            "reason_code": "MATCH_EDITORIAL_CONTEXT",
        },
        candidate,
    ) is False
    assert _is_strong_automatic_broll_decision(
        {**strong, "concept_id": "activity.walk"}, candidate
    ) is False
    assert _is_strong_automatic_broll_decision(
        {**strong, "confidence": 0.89}, candidate
    ) is False


def test_analysis_builds_recipe_from_cloud_context_and_local_time(tmp_path: Path) -> None:
    script = "每天吃一个鸡蛋，鸡蛋里挑骨头。"
    store, project = _project(tmp_path, script)
    client = FakeVisualClient()
    coordinator = ProjectVisualAnalysisCoordinator(
        store, client, load_semantic_visual_catalog(CATALOG_ROOT)
    )

    result = coordinator.analyze("user-1", project["project_id"], "token")
    visual = result["items"][0]["visual_analysis"]

    assert visual["analysis_status"] == "SUCCESS"
    assert visual["mapping_status"] == "SUCCESS"
    assert len(visual["decisions"]) == 2
    assert len(visual["recipe"]["overlays"]) == 1
    assert visual["recipe"]["overlays"][0]["concept_id"] == "food.egg"
    overlay = visual["recipe"]["overlays"][0]
    assert overlay["start_us"] == 0
    assert overlay["phrase_text"] == "每天吃一个鸡蛋"
    assert overlay["timing_source"] == "minimax_raw_cue_phrase_span"
    assert len(client.calls) == 1


def test_raw_cue_mismatch_fails_only_visual_mapping(tmp_path: Path) -> None:
    script = "每天吃一个鸡蛋"
    store, project = _project(tmp_path, script)
    item = project["items"][0]
    subtitles = dict(item["subtitles"])
    subtitles["raw_cues"] = [
        {"start_us": 0, "end_us": 2_000_000, "text": "每天吃一个玉米"}
    ]
    store.set_item_subtitles(
        "user-1", project["project_id"], item["item_id"], subtitles
    )
    coordinator = ProjectVisualAnalysisCoordinator(
        store, FakeVisualClient(), load_semantic_visual_catalog(CATALOG_ROOT)
    )

    result = coordinator.analyze("user-1", project["project_id"], "token")
    current = result["items"][0]

    assert current["visual_analysis"]["analysis_status"] == "SUCCESS"
    assert current["visual_analysis"]["mapping_status"] == "FAILED"
    assert current["visual_analysis"]["recipe"]["overlays"] == []
    assert current["subtitles"]["raw_cues"] == subtitles["raw_cues"]


def test_locked_manual_overlay_survives_reanalysis_and_script_change(tmp_path: Path) -> None:
    script = "每天吃一个鸡蛋"
    store, project = _project(tmp_path, script)
    coordinator = ProjectVisualAnalysisCoordinator(
        store, FakeVisualClient(), load_semantic_visual_catalog(CATALOG_ROOT)
    )
    analyzed = coordinator.analyze("user-1", project["project_id"], "token")
    item = analyzed["items"][0]
    overlay = dict(item["visual_analysis"]["recipe"]["overlays"][0])
    overlay.update({"corner": "top_left", "locked": True})
    edited = store.update_item_visual_overlays(
        "user-1",
        analyzed["project_id"],
        item["item_id"],
        overlays=[overlay],
        expected_revision=analyzed["revision"],
        catalog_version=load_semantic_visual_catalog(CATALOG_ROOT).catalog_version,
    )

    refreshed = coordinator.analyze(
        "user-1", edited["project_id"], "token", force_refresh=True
    )
    locked = refreshed["items"][0]["visual_analysis"]["recipe"]["overlays"][0]
    assert locked["corner"] == "top_left"
    assert locked["manual"] is True
    assert locked["locked"] is True

    changed = store.update_item(
        "user-1",
        refreshed["project_id"],
        item["item_id"],
        script_text="每天吃一个水煮蛋",
    )
    retained = changed["items"][0]["visual_analysis"]
    assert retained["analysis_status"] == "NOT_REQUESTED"
    assert retained["recipe"]["overlays"][0]["requires_review"] is True


def test_raw_cues_change_invalidates_only_automatic_visual_recipe(tmp_path: Path) -> None:
    script = "每天吃一个鸡蛋"
    store, project = _project(tmp_path, script)
    coordinator = ProjectVisualAnalysisCoordinator(
        store, FakeVisualClient(), load_semantic_visual_catalog(CATALOG_ROOT)
    )
    analyzed = coordinator.analyze("user-1", project["project_id"], "token")
    item = analyzed["items"][0]
    assert len(item["visual_analysis"]["recipe"]["overlays"]) == 1

    subtitles = dict(item["subtitles"])
    subtitles["raw_cues"] = [
        {"start_us": 200_000, "end_us": 4_200_000, "text": script}
    ]
    changed = store.set_item_subtitles(
        "user-1", analyzed["project_id"], item["item_id"], subtitles
    )
    visual = changed["items"][0]["visual_analysis"]

    assert visual["analysis_status"] == "SUCCESS"
    assert visual["mapping_status"] == "NOT_REQUESTED"
    assert visual["invalidated_reason"] == "AUDIO_OR_RAW_CUES_CHANGED"
    assert visual["visual_plan"] == []
    assert visual["recipe"]["overlays"] == []
    assert changed["items"][0]["subtitles"]["raw_cues"] == subtitles["raw_cues"]


def test_seam_supplement_runs_once_after_segments_exist_and_merges_recipe(
    tmp_path: Path,
) -> None:
    script = "前面先热身。接着做胯下击掌燃脂操，保持呼吸。"
    second_segment = "接着做胯下击掌燃脂操，保持呼吸。"
    store, project = _project(tmp_path, script)
    item = project["items"][0]
    base_path = tmp_path / "base.mp4"
    segment_1 = tmp_path / "segment-1.mp4"
    segment_2 = tmp_path / "segment-2.mp4"
    for path in (base_path, segment_1, segment_2):
        path.write_bytes(b"video")
    store.add_asset(
        owner_user_id="user-1",
        project_id=project["project_id"],
        item_id=item["item_id"],
        asset_type="original_video_segment",
        source_type="runninghub",
        status="READY",
        managed_path=str(segment_1),
        external_ref={"video_index": 1},
        metadata={
            "start_seconds": 0.0,
            "end_seconds": 2.0,
            "actual_duration_us": 2_000_000,
            "script_text": "前面先热身。",
        },
    )
    store.add_asset(
        owner_user_id="user-1",
        project_id=project["project_id"],
        item_id=item["item_id"],
        asset_type="original_video_segment",
        source_type="runninghub",
        status="READY",
        managed_path=str(segment_2),
        external_ref={"video_index": 2},
        metadata={
            "start_seconds": 2.0,
            "end_seconds": 6.0,
            "actual_duration_us": 4_000_000,
            "script_text": second_segment,
        },
    )
    store.add_asset(
        owner_user_id="user-1",
        project_id=project["project_id"],
        item_id=item["item_id"],
        asset_type="base_video",
        source_type="runninghub",
        status="READY",
        managed_path=str(base_path),
        metadata={"segment_count": 2, "duration_us": 6_000_000},
        make_current=True,
    )
    client = FakeVisualClient()
    coordinator = ProjectVisualAnalysisCoordinator(
        store, client, load_semantic_visual_catalog(CATALOG_ROOT)
    )

    first = coordinator.supplement_seams(
        "user-1", project["project_id"], "token", item_ids=[item["item_id"]]
    )
    visual = first["items"][0]["visual_analysis"]

    assert len(client.calls) == 1
    assert all(candidate["usage"] == "seam_broll" for candidate in client.calls[0]["candidates"])
    assert visual["seam_analysis"]["status"] == "SUCCESS"
    assert visual["seam_analysis"]["catalog_version"] == coordinator.catalog.catalog_version
    assert visual["seam_analysis"]["recipe_fingerprint"]
    seam = next(
        overlay
        for overlay in visual["recipe"]["overlays"]
        if overlay.get("usage") == "seam_broll"
    )
    assert seam["start_us"] == 1_500_000

    coordinator.supplement_seams(
        "user-1", project["project_id"], "token", item_ids=[item["item_id"]]
    )
    assert len(client.calls) == 1

    store.update_item_seam_visual_analysis(
        "user-1",
        project["project_id"],
        item["item_id"],
        expected_script_sha256=visual["script_sha256"],
        seam_analysis=dict(visual["seam_analysis"]),
        recipe={**visual["recipe"], "overlays": [], "used_asset_ids": []},
    )
    repaired = coordinator.supplement_seams(
        "user-1", project["project_id"], "token", item_ids=[item["item_id"]]
    )
    assert len(client.calls) == 2
    assert any(
        overlay.get("usage") == "seam_broll"
        for overlay in repaired["items"][0]["visual_analysis"]["recipe"]["overlays"]
    )
