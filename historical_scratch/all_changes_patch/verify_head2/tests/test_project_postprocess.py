from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_store import ProjectStore  # noqa: E402
from jyd_probe.caption_alignment import (  # noqa: E402
    CaptionAlignmentError,
    build_alignment,
)
from jyd_probe.project_postprocess import (  # noqa: E402
    ProjectPostprocessCoordinator,
    _uses_validated_subtitle_boundaries,
    bind_semantic_overlays_to_render_cues,
    draft_recipe_sha256,
)
from jyd_probe.h3_handoff import import_h3_handoff  # noqa: E402
from jyd_probe.web_api import WebApiSettings, create_app  # noqa: E402


def test_postprocess_agent_offline_has_terminal_failure_reason() -> None:
    class OfflineAgentQueue:
        execution_mode = "agent"

        @staticmethod
        def list_agents():
            return [{"agent_id": "processor-1", "status": "offline"}]

    coordinator = object.__new__(ProjectPostprocessCoordinator)
    coordinator.render_queue = OfflineAgentQueue()
    coordinator.stall_timeout_seconds = 30 * 60
    coordinator.no_agent_grace_seconds = 60
    queued_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()

    stalled = coordinator._active_render_stall(
        {"created_at": queued_at},
        {"status": "pending", "queued_at": queued_at},
    )

    assert stalled is not None
    assert stalled[0] == "JY_RENDER_AGENT_OFFLINE"
    assert "处理机未连接" in stalled[1]


def test_postprocess_active_job_has_bounded_waiting_time() -> None:
    coordinator = object.__new__(ProjectPostprocessCoordinator)
    coordinator.render_queue = object()
    coordinator.stall_timeout_seconds = 30 * 60
    coordinator.no_agent_grace_seconds = 60
    started_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()

    stalled = coordinator._active_render_stall(
        {"started_at": started_at},
        {"status": "running", "started_at": started_at},
    )

    assert stalled is not None
    assert stalled[0] == "JY_RENDER_TIMEOUT"
    assert "停止无限等待" in stalled[1]


def _health_music_intent() -> dict[str, object]:
    return {
        "primary_scene": "health_education",
        "secondary_scenes": ["habit_lifestyle"],
        "content_format": "knowledge_explanation",
        "topics": ["general_health"],
        "primary_mood": "calm",
        "secondary_moods": ["warm"],
        "valence": "positive",
        "energy": 2,
        "pace": "medium_slow",
        "seriousness": 3,
        "warmth": 4,
        "tension": 1,
        "speech_density": "high",
        "vocal_preference": "instrumental_only",
        "opening_preference": "soft",
        "avoid_traits": ["strong_vocals", "dense_arrangement"],
        "confidence": 0.9,
    }


def test_validated_subtitle_boundary_version_accepts_v20_and_newer() -> None:
    assert _uses_validated_subtitle_boundaries(
        {"subtitle_prompt_version": "jyd.subtitle-analysis.prompt.v20"}
    )
    assert _uses_validated_subtitle_boundaries(
        {"subtitle_prompt_version": "jyd.subtitle-analysis.prompt.v22"}
    )
    assert not _uses_validated_subtitle_boundaries(
        {"subtitle_prompt_version": "jyd.subtitle-analysis.prompt.v19"}
    )


def test_explicit_visual_starts_on_final_caption_containing_keyword() -> None:
    script = "你如果能够做到，在七天之内坚持吃苹果"
    apple_start = script.index("苹果")
    overlays = [
        {
            "overlay_id": "apple-image",
            "candidate_id": "vc_apple",
            "usage": "explicit",
            "timing_mode": "sentence",
            "selection_mode": "auto",
            "manual": False,
            "media_type": "image",
            "start_us": 0,
            "duration_us": 2_500_000,
            "timing_source": "funasr_phrase_timestamps",
        }
    ]
    render_cues = [
        {"start_us": 0, "duration_us": 1_000_000, "text": "你如果能够做到"},
        {
            "start_us": 1_000_000,
            "duration_us": 1_500_000,
            "text": "在七天之内坚持吃苹果",
        },
    ]
    candidate_request = {
        "candidates": [
            {
                "candidate_id": "vc_apple",
                "text": "苹果",
                "char_start": apple_start,
                "char_end": apple_start + 2,
            }
        ]
    }

    resolved = bind_semantic_overlays_to_render_cues(
        script, overlays, render_cues, candidate_request
    )

    assert resolved[0]["start_us"] == 1_000_000
    assert resolved[0]["duration_us"] == 1_500_000
    assert resolved[0]["timing_source"] == "final_caption_cue"
    assert resolved[0]["analysis_timing_source"] == "funasr_phrase_timestamps"
    assert resolved[0]["caption_anchor_text"] == "在七天之内坚持吃苹果"


def test_caption_binding_does_not_move_manual_or_seam_visuals() -> None:
    script = "坚持吃苹果"
    candidate_request = {
        "candidates": [
            {
                "candidate_id": "vc_apple",
                "text": "苹果",
                "char_start": 3,
                "char_end": 5,
            }
        ]
    }
    overlays = [
        {
            "candidate_id": "vc_apple",
            "manual": True,
            "start_us": 200_000,
            "duration_us": 500_000,
        },
        {
            "candidate_id": "vs_mood",
            "usage": "seam_broll",
            "timing_mode": "seam_broll",
            "start_us": 700_000,
            "duration_us": 1_000_000,
        },
    ]

    resolved = bind_semantic_overlays_to_render_cues(
        script,
        overlays,
        [{"start_us": 0, "duration_us": 1_000_000, "text": script}],
        candidate_request,
    )

    assert resolved == overlays


def test_unmappable_final_captions_keep_original_visual_timing() -> None:
    overlays = [
        {
            "candidate_id": "vc_apple",
            "manual": False,
            "timing_mode": "sentence",
            "start_us": 0,
            "duration_us": 1_000_000,
        },
        {
            "candidate_id": "vs_mood",
            "usage": "seam_broll",
            "timing_mode": "seam_broll",
            "start_us": 1_000_000,
            "duration_us": 1_000_000,
        },
    ]
    candidate_request = {
        "candidates": [
            {
                "candidate_id": "vc_apple",
                "text": "苹果",
                "char_start": 3,
                "char_end": 5,
            }
        ]
    }

    resolved = bind_semantic_overlays_to_render_cues(
        "坚持吃苹果",
        overlays,
        [{"start_us": 0, "duration_us": 1_000_000, "text": "错误字幕"}],
        candidate_request,
    )

    assert resolved == overlays


def test_caption_binding_ignores_source_list_line_breaks_and_spaces() -> None:
    script = (
        "姐姐一定要认真听。\n"
        "第一个水煮虾配清炒菠菜\n"
        "第二个菌菇炒鸡胸肉"
    )
    shrimp_start = script.index("水煮虾")
    mushroom_start = script.index("菌菇")
    overlays = [
        {
            "candidate_id": "vc_shrimp",
            "manual": False,
            "selection_mode": "auto",
            "usage": "explicit",
            "timing_mode": "sentence",
            "start_us": 900_000,
            "duration_us": 1_800_000,
        },
        {
            "candidate_id": "vc_mushroom",
            "manual": False,
            "selection_mode": "auto",
            "usage": "explicit",
            "timing_mode": "sentence",
            "start_us": 2_700_000,
            "duration_us": 1_800_000,
        },
    ]
    render_cues = [
        {"start_us": 0, "duration_us": 1_000_000, "text": "姐姐一定要认真听"},
        {"start_us": 1_000_000, "duration_us": 1_500_000, "text": "第一个水煮虾配清炒菠菜"},
        {"start_us": 2_500_000, "duration_us": 1_500_000, "text": "第二个菌菇炒鸡胸肉"},
    ]
    candidate_request = {
        "candidates": [
            {
                "candidate_id": "vc_shrimp",
                "char_start": shrimp_start,
                "char_end": shrimp_start + len("水煮虾"),
            },
            {
                "candidate_id": "vc_mushroom",
                "char_start": mushroom_start,
                "char_end": mushroom_start + len("菌菇"),
            },
        ]
    }

    resolved = bind_semantic_overlays_to_render_cues(
        script, overlays, render_cues, candidate_request
    )

    assert len(resolved) == 2
    assert resolved[0]["start_us"] == 1_000_000
    assert resolved[0]["caption_anchor_text"] == "第一个水煮虾配清炒菠菜"
    assert resolved[1]["start_us"] == 2_500_000
    assert resolved[1]["caption_anchor_text"] == "第二个菌菇炒鸡胸肉"


class ProjectPostprocessApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            PROJECT_ROOT
            / "runtime"
            / "test_tmp"
            / f"project_postprocess_{uuid.uuid4().hex}"
        )
        self.root.mkdir(parents=True)
        self.settings = WebApiSettings(
            storage_root=self.root / "storage",
            template_library_root=self.root / "templates",
            default_draft_root=self.root / "drafts",
            audio_library_root=PROJECT_ROOT / "data" / "libraries" / "audio_library",
            admin_password="admin-pass",
            admin_session_secret="admin-secret",
            auth_authority=False,
            auth_server_url="http://127.0.0.1:8000",
            execution_mode="agent",
        )
        for directory in (
            self.settings.storage_root,
            self.settings.template_library_root,
            self.settings.default_draft_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_h3_handoff_4b_aligns_generated_audio_with_segment_windows(self) -> None:
        user_id = "h3-postprocess-user"
        script = "第一句。第二句。"
        base_path = self.settings.storage_root / "h3-base-silent.mp4"
        master_path = self.settings.storage_root / "h3-master-av.mp4"
        audio_path = self.settings.storage_root / "h3-generated-full.wav"
        cues_path = self.settings.storage_root / "h3-segment-cue-windows.json"
        manifest_path = self.settings.storage_root / "jyd-handoff.json"
        base_path.write_bytes(b"video-only")
        master_path.write_bytes(b"h3-generated-audio-video")
        audio_path.write_bytes(b"h3-generated-audio")
        segment_ids = ["segment-1", "segment-2"]
        cues = [
            {"text": "第一句。", "start_seconds": 0.0, "end_seconds": 2.5},
            {"text": "第二句。", "start_seconds": 2.5, "end_seconds": 5.0},
        ]
        cues_path.write_text(
            json.dumps(cues, ensure_ascii=False), encoding="utf-8"
        )
        identity = {
            "project_id": "h3-local:postprocess",
            "segment_ids": segment_ids,
            "schema_version": "h3.jyd_handoff.v2",
        }
        handoff_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "h3.jyd_handoff.v2",
                    "project_id": identity["project_id"],
                    "handoff_id": handoff_id,
                    "source": {"row_key": "H3-001", "script_text": script},
                    "h3_master": {
                        "path": str(master_path),
                        "audio_video_pair": "h3_generated",
                    },
                    "base_video": {
                        "path": str(base_path),
                        "role": "base_video",
                        "audio_policy": "separate_h3_generated_audio",
                        "source_segment_ids": segment_ids,
                    },
                    "authoritative_audio": {
                        "path": str(audio_path),
                        "source": "h3_generated_audio",
                        "timeline_start_seconds": 0.0,
                        "reuse_once": True,
                    },
                    "subtitles": {
                        "raw_cues_asset": str(cues_path),
                        "timing_source": "h3_segment_windows_then_funasr",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = import_h3_handoff(
            store,
            owner_user_id=user_id,
            owner_username="tester",
            project_name="H3 4B 链路",
            manifest_path=manifest_path,
        )
        item = project["items"][0]

        class CapturingAligner:
            def __init__(self) -> None:
                self.path = None
                self.raw_cues = None

            def align(self, path, *, script, raw_cues, audio_asset_id, audio_version):
                self.path = Path(path)
                self.raw_cues = raw_cues
                return build_alignment(
                    script,
                    raw_cues,
                    {
                        "tokens": [
                            {"text": "第", "startSeconds": 0.4, "endSeconds": 0.6},
                            {"text": "一", "startSeconds": 0.7, "endSeconds": 0.9},
                            {"text": "句", "startSeconds": 1.0, "endSeconds": 1.2},
                            {"text": "第", "startSeconds": 2.9, "endSeconds": 3.1},
                            {"text": "二", "startSeconds": 3.2, "endSeconds": 3.4},
                            {"text": "句", "startSeconds": 3.5, "endSeconds": 3.7},
                        ]
                    },
                    audio_asset_id=audio_asset_id,
                    audio_version=audio_version,
                )

        class RenderQueue:
            def submit_batch(self, jobs, _variants):
                return {"batch_id": "h3-batch", "job_ids": ["h3-job"]}

            def get_status(self, _job_id):
                return {"job_id": "h3-job", "status": "running"}

        class EmptyMusicMatcher:
            def snapshot(self):
                return {"profiles": []}

        font_path = self.settings.storage_root / "font.ttf"
        font_path.write_bytes(b"font")
        aligner = CapturingAligner()
        coordinator = ProjectPostprocessCoordinator(
            store,
            RenderQueue(),
            storage_root=self.settings.storage_root,
            draft_root=self.settings.default_draft_root,
            fonts=[
                {
                    "identity": "font-test",
                    "name": "测试字体",
                    "path": str(font_path),
                    "available": True,
                }
            ],
            bgm_assets=[],
            music_matcher=EmptyMusicMatcher(),
            caption_aligner=aligner,
            require_precise_alignment=True,
            semantic_visual_library_root=self.root / "missing-visual-library",
        )
        with patch(
            "jyd_probe.project_postprocess.derive_project_render_cues",
            return_value=(
                [{"text": script, "start_us": 400_000, "duration_us": 4_200_000}],
                {"status": "SUCCESS", "timing_source": "funasr_word_timestamps"},
            ),
        ), patch.object(
            coordinator,
            "_build_draft_job",
            return_value={"output": {"skip_export": True}},
        ):
            result = coordinator.start(
                user_id,
                project["project_id"],
                idempotency_key="h3-generated-audio-alignment",
                item_settings=[
                    {
                        "item_id": item["item_id"],
                        "font_identity": "font-test",
                        "bgm_selection_mode": "manual",
                        "bgm_identity": "",
                    }
                ],
            )

        self.assertEqual(aligner.path, audio_path.resolve())
        self.assertEqual(aligner.raw_cues[1]["start_us"], 2_500_000)
        aligned_item = store.get_project(user_id, project["project_id"])["items"][0]
        alignment = aligned_item["subtitles"]["asr_alignment"]
        self.assertEqual(alignment.get("status"), "SUCCESS", alignment)
        self.assertEqual(
            alignment["audio_asset_id"],
            aligned_item["outputs"]["audio"]["asset_id"],
        )
        self.assertEqual(aligned_item["subtitles"]["status"], "PREVIEW_READY")
        self.assertEqual(aligned_item["status"], "POSTPROCESS_RUNNING")

    def test_export_prepares_old_preview_draft_before_starting_mp4_export(self) -> None:
        user_id = "old-preview-user"
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user_id,
            owner_username="tester",
            name="旧预览导出",
            items=[{"row_key": "1", "script_text": "旧预览"}],
        )
        item = project["items"][0]
        base_path = self.settings.storage_root / "old-preview-base.mp4"
        base_path.write_bytes(b"video")
        store.add_asset(
            owner_user_id=user_id,
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="base_video",
            source_type="runninghub_merge",
            status="READY",
            filename=base_path.name,
            managed_path=str(base_path),
            make_current=True,
        )
        store.set_item_subtitles(
            user_id,
            project["project_id"],
            item["item_id"],
            {
                "source": "minimax_timestamps",
                "raw_cues": [{"text": "旧预览", "start_us": 0, "duration_us": 1_000_000}],
                "render_cues": [{"text": "旧预览", "start_us": 0, "duration_us": 1_000_000}],
                "status": "PREVIEW_READY",
                "style": {},
                "overflow_risk": False,
            },
        )

        stale_dir = self.settings.default_draft_root / "stale-draft"
        stale_dir.mkdir(parents=True)
        (stale_dir / "draft_content.json").write_text("{}", encoding="utf-8")
        stale = store.create_operation(
            owner_user_id=user_id,
            project_id=project["project_id"],
            item_id=item["item_id"],
            operation_type="POSTPROCESS_GENERATE",
            idempotency_key="stale-success",
            payload={"draft_recipe_sha256": "0" * 64},
        )
        store.transition_operation(
            user_id,
            project["project_id"],
            item["item_id"],
            operation_id=stale["operation_id"],
            operation_type="POSTPROCESS_GENERATE",
            status="SUCCEEDED",
            item_status="COMPOSITION_READY",
            result={
                "output_draft_dir": str(stale_dir),
                "output_draft_name": stale_dir.name,
            },
        )
        failed = store.create_operation(
            owner_user_id=user_id,
            project_id=project["project_id"],
            item_id=item["item_id"],
            operation_type="POSTPROCESS_GENERATE",
            idempotency_key="newer-failed",
            payload={},
        )
        store.transition_operation(
            user_id,
            project["project_id"],
            item["item_id"],
            operation_id=failed["operation_id"],
            operation_type="POSTPROCESS_GENERATE",
            status="FAILED",
            item_status="COMPOSITION_FAILED",
            error_code="TEST",
            error_message="new draft failed",
        )

        class Queue:
            def __init__(self) -> None:
                self.jobs = []

            def submit_batch(self, jobs, _variants):
                self.jobs.extend(jobs)
                return {"batch_id": "prepare-batch", "job_ids": ["prepare-job"]}

            def get_status(self, _job_id):
                return {"job_id": "prepare-job", "status": "running"}

        class EmptyMusicMatcher:
            def snapshot(self):
                return {"profiles": []}

        queue = Queue()
        coordinator = ProjectPostprocessCoordinator(
            store,
            queue,
            storage_root=self.settings.storage_root,
            draft_root=self.settings.default_draft_root,
            fonts=[],
            bgm_assets=[],
            music_matcher=EmptyMusicMatcher(),
        )
        prepared_job = {
            "schema": "jyd.render_job.v1",
            "source": {"type": "video", "media_path": str(base_path)},
            "output": {"draft_root": str(self.settings.default_draft_root), "draft_name": "fresh", "skip_export": True},
        }
        with patch.object(coordinator, "_build_draft_job", return_value=prepared_job):
            result = coordinator.export_preview(
                user_id,
                project["project_id"],
                item["item_id"],
                idempotency_key="download-old-preview",
            )

        self.assertEqual(len(queue.jobs), 1)
        self.assertTrue(queue.jobs[0]["output"]["skip_export"])
        self.assertNotIn("mp4_path", queue.jobs[0]["output"])
        latest = result["operations"][-1]
        self.assertEqual(latest["operation_type"], "POSTPROCESS_GENERATE")
        self.assertEqual(latest["result"]["reason"], "export_prepare")
        self.assertFalse(
            any(operation["operation_type"] == "POSTPROCESS_EXPORT" for operation in result["operations"])
        )

    def test_export_freezes_one_safe_rebuild_job_for_draft_discovery_recovery(self) -> None:
        user_id = "draft-recovery-user"
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user_id,
            owner_username="tester",
            name="草稿识别恢复",
            items=[{"row_key": "1", "script_text": "恢复草稿"}],
        )
        item = project["items"][0]
        base_path = self.settings.storage_root / "recovery-base.mp4"
        base_path.write_bytes(b"video")
        store.add_asset(
            owner_user_id=user_id,
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="base_video",
            source_type="runninghub_merge",
            status="READY",
            filename=base_path.name,
            managed_path=str(base_path),
            make_current=True,
        )
        store.set_item_subtitles(
            user_id,
            project["project_id"],
            item["item_id"],
            {
                "source": "minimax_timestamps",
                "raw_cues": [{"text": "恢复草稿", "start_us": 0, "duration_us": 1_000_000}],
                "render_cues": [{"text": "恢复草稿", "start_us": 0, "duration_us": 1_000_000}],
                "status": "PREVIEW_READY",
                "style": {},
                "overflow_risk": False,
            },
        )
        frozen_dir = self.settings.default_draft_root / "frozen-draft"
        frozen_dir.mkdir(parents=True)
        (frozen_dir / "draft_content.json").write_text("{}", encoding="utf-8")
        recovery_job = {
            "schema": "jyd.render_job.v1",
            "source": {"type": "video", "media_path": str(base_path)},
            "output": {
                "draft_root": str(self.settings.default_draft_root),
                "draft_name": "recovered-draft",
                "skip_export": True,
            },
        }
        generated = store.create_operation(
            owner_user_id=user_id,
            project_id=project["project_id"],
            item_id=item["item_id"],
            operation_type="POSTPROCESS_GENERATE",
            idempotency_key="frozen-success",
            payload={"draft_recipe_sha256": draft_recipe_sha256(recovery_job)},
        )
        store.transition_operation(
            user_id,
            project["project_id"],
            item["item_id"],
            operation_id=generated["operation_id"],
            operation_type="POSTPROCESS_GENERATE",
            status="SUCCEEDED",
            item_status="COMPOSITION_READY",
            result={
                "output_draft_dir": str(frozen_dir),
                "output_draft_name": frozen_dir.name,
            },
        )

        class Queue:
            def __init__(self) -> None:
                self.jobs = []

            def submit_batch(self, jobs, _variants):
                self.jobs.extend(jobs)
                return {"batch_id": "export-batch", "job_ids": ["export-job"]}

            def get_status(self, _job_id):
                return {"job_id": "export-job", "status": "running"}

        class EmptyMusicMatcher:
            def snapshot(self):
                return {"profiles": []}

        queue = Queue()
        coordinator = ProjectPostprocessCoordinator(
            store,
            queue,
            storage_root=self.settings.storage_root,
            draft_root=self.settings.default_draft_root,
            fonts=[],
            bgm_assets=[],
            music_matcher=EmptyMusicMatcher(),
        )
        with patch.object(coordinator, "_build_draft_job", return_value=recovery_job):
            coordinator.export_preview(
                user_id,
                project["project_id"],
                item["item_id"],
                idempotency_key="download-with-recovery",
            )

        self.assertEqual(len(queue.jobs), 1)
        export_job = queue.jobs[0]
        self.assertEqual(export_job["source"]["type"], "existing_draft")
        self.assertEqual(export_job["source"]["draft_name"], frozen_dir.name)
        embedded = export_job["source"]["recovery"]["rebuild_job"]
        self.assertIs(embedded, recovery_job)
        self.assertTrue(embedded["output"]["skip_export"])
        self.assertNotEqual(
            embedded["output"]["draft_name"], export_job["source"]["draft_name"]
        )
        self.assertEqual(
            embedded["observability"]["recovery_reason"],
            "draft_discovery_exhausted",
        )

    def test_draft_recipe_hash_ignores_output_but_detects_timeline_changes(self) -> None:
        first = {
            "schema": "jyd.render_job.v1",
            "source": {"type": "video", "media_path": "base.mp4"},
            "visual_overlays": [{"asset_id": "asset-a", "start_us": 1_000_000}],
            "output": {"draft_name": "draft-a", "skip_export": True},
        }
        same_recipe = {
            **first,
            "output": {"draft_name": "draft-b", "mp4_path": "result.mp4"},
            "observability": {"operation_id": "runtime-only"},
        }
        changed_recipe = {
            **first,
            "visual_overlays": [{"asset_id": "asset-b", "start_us": 1_000_000}],
        }

        self.assertEqual(draft_recipe_sha256(first), draft_recipe_sha256(same_recipe))
        self.assertNotEqual(draft_recipe_sha256(first), draft_recipe_sha256(changed_recipe))
        self.assertNotEqual(
            draft_recipe_sha256(first, subtitle_analysis_identity="analysis-v19"),
            draft_recipe_sha256(first, subtitle_analysis_identity="analysis-v20"),
        )

    def test_one_asr_failure_does_not_block_later_rows(self) -> None:
        user_id = "postprocess-isolation-user"
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user_id,
            owner_username="tester",
            name="ASR 行级隔离",
            items=[
                {"row_key": "1", "script_text": "甲"},
                {"row_key": "2", "script_text": "乙"},
                {"row_key": "3", "script_text": "丙"},
            ],
        )
        for item in project["items"]:
            audio_path = self.settings.storage_root / f"{item['row_key']}.mp3"
            audio_path.write_bytes(b"audio")
            audio = store.add_asset(
                owner_user_id=user_id,
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="audio",
                source_type="minimax",
                status="READY",
                filename=audio_path.name,
                managed_path=str(audio_path),
                make_current=True,
            )
            store.set_item_subtitles(
                user_id,
                project["project_id"],
                item["item_id"],
                {
                    "source": "minimax_timestamps",
                    "raw_cues": [
                        {
                            "text": item["script_text"],
                            "start_us": 0,
                            "duration_us": 1_000_000,
                        }
                    ],
                    "render_cues": [],
                    "bound_audio_asset_id": audio["asset_id"],
                    "status": "READY",
                    "style": {},
                    "overflow_risk": False,
                },
            )
            base_path = self.settings.storage_root / f"{item['row_key']}.mp4"
            base_path.write_bytes(b"video")
            store.add_asset(
                owner_user_id=user_id,
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="base_video",
                source_type="runninghub_merge",
                status="READY",
                filename=base_path.name,
                managed_path=str(base_path),
                make_current=True,
            )

        class RowAligner:
            def __init__(self) -> None:
                self.calls = 0

            def align(self, _path, *, script, raw_cues, audio_asset_id, audio_version):
                self.calls += 1
                if self.calls == 1:
                    raise CaptionAlignmentError(
                        "ASR_TIMESTAMPS_MISSING", "FunASR 没有返回字词时间戳"
                    )
                if self.calls == 2:
                    raise CaptionAlignmentError(
                        "ASR_SCRIPT_MATCH_TOO_LOW",
                        "FunASR 与原脚本精确命中率仅 82.0%，拒绝生成精确字幕",
                    )
                return build_alignment(
                    script,
                    raw_cues,
                    {
                        "tokens": [
                            {
                                "text": script,
                                "startSeconds": 0.1,
                                "endSeconds": 0.9,
                            }
                        ]
                    },
                    audio_asset_id=audio_asset_id,
                    audio_version=audio_version,
                )

        class RenderQueue:
            def __init__(self) -> None:
                self.jobs = []

            def submit_batch(self, jobs, _variants):
                self.jobs.extend(jobs)
                return {
                    "batch_id": "batch-1",
                    "job_ids": [f"job-{index}" for index, _job in enumerate(jobs, 1)],
                }

            def get_status(self, _job_id):
                return {"job_id": "job-1", "status": "running"}

        class EmptyMusicMatcher:
            def snapshot(self):
                return {"profiles": []}

        font_path = self.settings.storage_root / "font.ttf"
        font_path.write_bytes(b"font")
        aligner = RowAligner()
        queue = RenderQueue()
        coordinator = ProjectPostprocessCoordinator(
            store,
            queue,
            storage_root=self.settings.storage_root,
            draft_root=self.settings.default_draft_root,
            fonts=[
                {
                    "identity": "font-test",
                    "name": "测试字体",
                    "path": str(font_path),
                    "available": True,
                }
            ],
            bgm_assets=[],
            music_matcher=EmptyMusicMatcher(),
            caption_aligner=aligner,
            require_precise_alignment=True,
            semantic_visual_library_root=self.root / "missing-visual-library",
        )
        with patch(
            "jyd_probe.project_postprocess.derive_project_render_cues",
            return_value=(
                [{"text": "乙", "start_us": 100_000, "duration_us": 800_000}],
                {"status": "SUCCESS", "timing_source": "funasr_word_timestamps"},
            ),
        ) as derive_cues, patch.object(
            coordinator,
            "_build_draft_job",
            return_value={"output": {"skip_export": True}},
        ):
            result = coordinator.start(
                user_id,
                project["project_id"],
                idempotency_key="asr-isolation",
                item_settings=[
                    {
                        "item_id": item["item_id"],
                        "font_identity": "font-test",
                        "bgm_selection_mode": "manual",
                        "bgm_identity": "",
                    }
                    for item in project["items"]
                ],
            )

        self.assertEqual(aligner.calls, 3)
        self.assertEqual(len(queue.jobs), 2)
        rows = {item["row_key"]: item for item in result["items"]}
        self.assertEqual(rows["1"]["subtitles"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(rows["2"]["subtitles"]["status"], "PREVIEW_READY")
        self.assertEqual(rows["2"]["status"], "POSTPROCESS_RUNNING")
        self.assertIn(
            "已改用 MiniMax 原始时间轴",
            rows["2"]["subtitles"]["review_reason"],
        )
        self.assertEqual(
            rows["2"]["subtitles"]["asr_alignment"]["reason_code"],
            "ASR_SCRIPT_MATCH_TOO_LOW",
        )
        self.assertEqual(rows["3"]["subtitles"]["status"], "PREVIEW_READY")
        fallback_call, precise_call = derive_cues.call_args_list
        self.assertIsNone(fallback_call.kwargs["asr_alignment"])
        self.assertFalse(fallback_call.kwargs["require_precise_alignment"])
        self.assertIsNotNone(precise_call.kwargs["asr_alignment"])
        self.assertTrue(precise_call.kwargs["require_precise_alignment"])

    def test_4b_uses_real_font_width_one_line_position_and_bgm(self) -> None:
        user = {"user_id": "postprocess-user", "username": "tester", "enabled": True}
        store = ProjectStore(self.settings.storage_root / "control.db")
        project = store.create_project(
            owner_user_id=user["user_id"],
            owner_username=user["username"],
            name="4B 测试",
            items=[
                {
                    "row_key": "1",
                    "script_text": "这是一段需要使用真实字体宽度拆成多条单行字幕的较长测试文案。",
                },
                {
                    "row_key": "2",
                    "script_text": "另一条尚未生成基础视频的脚本不应阻塞当前行。",
                },
            ],
        )
        item = project["items"][0]
        image_path = self.settings.storage_root / "seed-person.png"
        image_path.write_bytes(b"image")
        registered_image = store.register_input_image(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            filename=image_path.name,
            content_type="image/png",
            size_bytes=image_path.stat().st_size,
            sha256="seed-person",
            managed_path=str(image_path),
        )
        store.replace_item_image(
            user["user_id"], project["project_id"], item["item_id"], registered_image["image_id"]
        )
        audio_path = self.settings.storage_root / "seed-audio.mp3"
        audio_path.write_bytes(b"audio")
        audio = store.add_asset(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename="1.mp3",
            managed_path=str(audio_path),
            make_current=True,
        )
        store.set_item_subtitles(
            user["user_id"],
            project["project_id"],
            item["item_id"],
            {
                "source": "minimax_timestamps",
                "raw_cues": [
                    {
                        "start_us": 0,
                        "end_us": 4_000_000,
                        "text": "这是一段需要使用真实字体宽度拆成多条单行字幕的较长测试文案。",
                    }
                ],
                "render_cues": [],
                "bound_audio_asset_id": audio["asset_id"],
                "bound_video_asset_id": None,
                "style": {},
                "status": "READY",
                "overflow_risk": False,
            },
        )
        base_path = self.settings.storage_root / "seed-base.mp4"
        base_path.write_bytes(b"base-video")
        store.add_asset(
            owner_user_id=user["user_id"],
            project_id=project["project_id"],
            item_id=item["item_id"],
            asset_type="base_video",
            source_type="runninghub_merge",
            status="READY",
            filename="1-base.mp4",
            managed_path=str(base_path),
            metadata={"segment_count": 2},
            make_current=True,
        )
        for index, (start, end) in enumerate(((0.0, 1.75), (1.75, 4.0)), start=1):
            segment_path = self.settings.storage_root / f"seed-segment-{index}.mp4"
            segment_path.write_bytes(f"segment-{index}".encode("ascii"))
            store.add_asset(
                owner_user_id=user["user_id"],
                project_id=project["project_id"],
                item_id=item["item_id"],
                asset_type="original_video_segment",
                source_type="runninghub",
                status="READY",
                filename=segment_path.name,
                managed_path=str(segment_path),
                external_ref={"video_index": index},
                metadata={
                    "start_seconds": start,
                    "end_seconds": end,
                    "actual_duration_us": round((end - start) * 1_000_000),
                },
            )

        captured: dict[str, object] = {"submit_count": 0, "jobs": []}
        statuses: dict[str, dict[str, object]] = {}

        def fake_submit_batch(_queue, jobs, variants):
            captured["submit_count"] = int(captured["submit_count"]) + 1
            captured["jobs"].append(jobs[0])
            captured["job"] = jobs[0]
            captured["variant"] = variants[0]
            if jobs[0]["output"].get("skip_export") is True:
                job_id = f"draft-job-{captured['submit_count']}"
                draft_dir = (
                    Path(jobs[0]["output"]["draft_root"])
                    / jobs[0]["output"]["draft_name"]
                )
                draft_dir.mkdir(parents=True, exist_ok=True)
                (draft_dir / "draft_content.json").write_text("{}", encoding="utf-8")
                statuses[job_id] = {
                    "job_id": job_id,
                    "status": "completed",
                    "result": {
                        "output_draft_dir": str(draft_dir),
                        "output_draft_name": jobs[0]["output"]["draft_name"],
                    },
                }
            else:
                job_id = f"export-job-{captured['submit_count']}"
                output = Path(jobs[0]["output"]["mp4_path"])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"captioned-with-bgm")
                captured["output"] = output
                statuses[job_id] = {
                    "job_id": job_id,
                    "status": "completed",
                    "result": {"output_mp4": str(output)},
                }
            return {
                "batch_id": f"batch-{captured['submit_count']}",
                "job_ids": [job_id],
            }

        def fake_get_status(_queue, job_id):
            return statuses[job_id]

        with patch(
            "jyd_probe.auth_center.AuthCenterClient.login",
            return_value={"access_token": "token", "user": user},
        ), patch(
            "jyd_probe.auth_center.AuthCenterClient.verify", return_value=user
        ), patch(
            "jyd_probe.web_api.RenderJobQueue.submit_batch", new=fake_submit_batch
        ), patch(
            "jyd_probe.web_api.RenderJobQueue.get_status", new=fake_get_status
        ):
            with TestClient(create_app(self.settings)) as client:
                login = client.post(
                    "/api/auth/login",
                    json={"username": "tester", "password": "pass123"},
                )
                self.assertEqual(login.status_code, 200, login.text)
                options = client.get("/api/new/postprocess/options")
                self.assertEqual(options.status_code, 200, options.text)
                self.assertAlmostEqual(
                    options.json()["caption"]["bottom_offset_ratio"],
                    0.5 + (-0.382336816305469) / 2,
                )
                self.assertEqual(options.json()["caption"]["font_size"], 11.0)
                self.assertAlmostEqual(options.json()["caption"]["clip_scale"], 1.32)
                self.assertEqual(options.json()["caption"]["stroke_color"], "#000000")
                self.assertEqual(options.json()["caption"]["stroke_width"], 0.0)
                self.assertEqual(options.json()["default_layout_profile"], "standing")
                self.assertEqual(
                    [profile["id"] for profile in options.json()["layout_profiles"]],
                    ["standing", "seated"],
                )
                self.assertEqual(
                    options.json()["default_font_identity"],
                    "resource_id:7086699209738424840",
                )
                font = options.json()["fonts"][0]
                self.assertEqual(font["name"], "DouyinSansBold")
                bgm = options.json()["bgm"][0]
                font_preview = client.get(font["preview_url"])
                self.assertEqual(font_preview.status_code, 200, font_preview.text)
                self.assertGreater(len(font_preview.content), 1024)
                self.assertTrue(
                    font_preview.headers["content-type"].startswith("font/")
                    or font_preview.headers["content-type"] == "application/octet-stream"
                )
                bgm_preview = client.get(bgm["preview_url"])
                self.assertEqual(bgm_preview.status_code, 200, bgm_preview.text)
                self.assertGreater(len(bgm_preview.content), 1024)
                generated = client.post(
                    f"/api/new/projects/{project['project_id']}/postprocess/generate",
                    json={
                        "idempotency_key": "postprocess-1",
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "font_identity": font["identity"],
                                "bgm_identity": bgm["identity"],
                                "text_color": "#FFFFFF",
                                "top_title": {
                                    "label": "减肥大实话",
                                    "headline": "只有坚持才能达成目标",
                                },
                                "cover_title": {
                                    "line_1": "健康真相",
                                    "line_2": "别再踩坑",
                                },
                            }
                        ],
                    },
                )
                self.assertEqual(generated.status_code, 200, generated.text)
                row = generated.json()["items"][0]
                self.assertEqual(row["status"], "COMPOSITION_READY")
                self.assertIsNone(row["outputs"]["composition_video"])
                self.assertEqual(row["subtitles"]["status"], "PREVIEW_READY")
                self.assertEqual(
                    row["subtitles"]["bound_video_asset_id"],
                    row["outputs"]["base_video"]["asset_id"],
                )
                self.assertEqual(row["subtitles"]["style"]["max_lines"], 1)
                self.assertEqual(row["subtitles"]["style"]["max_width_ratio"], 0.8)
                self.assertAlmostEqual(
                    row["subtitles"]["style"]["bottom_offset_ratio"],
                    0.5 + (-0.382336816305469) / 2,
                )
                self.assertAlmostEqual(row["subtitles"]["style"]["transform_y"], -0.382336816305469)
                self.assertEqual(row["subtitles"]["style"]["font_size"], 11.0)
                self.assertAlmostEqual(row["subtitles"]["style"]["clip_scale"], 1.32)
                self.assertEqual(row["settings"]["postprocess"]["layout_profile"], "standing")
                self.assertEqual(
                    row["settings"]["postprocess"]["top_title"],
                    {"label": "减肥大实话", "headline": "只有坚持才能达成目标"},
                )
                self.assertEqual(
                    row["settings"]["postprocess"]["cover_title"],
                    {"line_1": "健康真相", "line_2": "别再踩坑"},
                )
                self.assertEqual(row["subtitles"]["style"]["stroke_color"], "")
                self.assertEqual(row["subtitles"]["style"]["stroke_width"], 0.0)
                self.assertAlmostEqual(row["subtitles"]["style"]["shadow_alpha"], 0.8999999761581421)
                self.assertEqual(row["subtitles"]["semantic_mapping"]["status"], "FALLBACK")
                self.assertGreater(len(row["subtitles"]["render_cues"]), 1)
                self.assertTrue(
                    all("\n" not in cue["text"] for cue in row["subtitles"]["render_cues"])
                )

                self.assertTrue(row["allowed_actions"]["generate_variants"])
                self.assertFalse(row["allowed_actions"]["download_current_video"])
                operation = generated.json()["operations"][-1]
                self.assertEqual(operation["status"], "SUCCEEDED")
                self.assertEqual(
                    operation["result"]["preview_mode"],
                    "browser_with_frozen_draft",
                )
                self.assertIn("job_id", operation["result"])
                self.assertTrue(Path(operation["result"]["output_draft_dir"]).is_dir())
                self.assertEqual(captured["submit_count"], 1)

                downloaded = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/current-video"
                )
                self.assertEqual(downloaded.status_code, 404, downloaded.text)
                base_download = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/base-video"
                )
                self.assertEqual(base_download.status_code, 200, base_download.text)
                self.assertEqual(base_download.content, b"base-video")

                exported = client.post(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/postprocess/export",
                    json={"idempotency_key": "explicit-download-1"},
                )
                self.assertEqual(exported.status_code, 200, exported.text)
                exported_row = exported.json()["items"][0]
                self.assertIsNotNone(exported_row["outputs"]["composition_video"])
                self.assertEqual(captured["submit_count"], 2)
                job = captured["jobs"][0]
                export_job = captured["jobs"][1]
                self.assertEqual(export_job["source"]["type"], "existing_draft")
                self.assertEqual(job["output"]["draft_name"], "1-composition")
                self.assertTrue(job["output"]["skip_export"])
                self.assertEqual(job["cover"]["frame_source"], "input_image")
                self.assertEqual(job["cover"]["image_path"], str(image_path.resolve()))
                self.assertEqual(job["cover"]["text_line_1"], "健康真相")
                self.assertEqual(job["cover"]["text_line_2"], "别再踩坑")
                self.assertTrue(job["captions"]["single_line"])
                self.assertEqual(job["captions"]["max_lines"], 1)
                self.assertAlmostEqual(job["captions"]["transform_y"], -0.382336816305469)
                self.assertEqual(job["captions"]["size"], 11.0)
                self.assertAlmostEqual(job["captions"]["clip_scale"], 1.32)
                self.assertEqual(job["captions"]["stroke_color"], "")
                self.assertEqual(job["captions"]["stroke_width"], 0.0)
                self.assertEqual(job["source"]["type"], "video_sequence")
                self.assertEqual(
                    [entry["target_duration_us"] for entry in job["source"]["items"]],
                    [1_750_000, 2_250_000],
                )
                self.assertEqual(
                    [entry["volume"] for entry in job["source"]["items"]],
                    [0.0, 0.0],
                )
                self.assertEqual(job["original_video_volume"], 0.0)
                self.assertEqual(job["audios"][0]["media_path"], str(audio_path.resolve()))
                self.assertEqual(job["audios"][0]["volume"], 1.0)
                bgm_media_path = Path(job["audios"][1]["media_path"])
                self.assertTrue(bgm_media_path.is_file())
                self.assertEqual(job["audios"][1]["library_identity"], bgm["identity"])
                recovery_bgm = export_job["source"]["recovery"]["rebuild_job"][
                    "audios"
                ][1]
                self.assertEqual(recovery_bgm["media_path"], str(bgm_media_path))
                self.assertEqual(recovery_bgm["library_identity"], bgm["identity"])
                self.assertEqual(job["audios"][1]["volume"], 0.18)
                self.assertTrue(job["audios"][1]["align_to_end"])
                self.assertEqual(job["audios"][1]["crossfade_us"], 200_000)
                self.assertEqual(job["audios"][1]["fade_in_us"], 5_000_000)
                self.assertEqual(job["source"]["fade_out_us"], 0)
                self.assertEqual(
                    [(text["text"], text["transform_y"], text["size"], text["color"]) for text in job["texts"]],
                    [
                        ("世界冠军带你自律", 0.8155959933996199, 19.0, "#FFF589"),
                        (
                            "非医疗保健科普：仅供参考，个人经验分享，不代表普遍性\n"
                            "如有不适请线下就医",
                            -0.916666666666667,
                            6.0,
                            "#FFFFFF",
                        ),
                        ("张雒", -0.10062777724609877, 11.0, "#FFFFFF"),
                        ("世界蹦床冠军", -0.06835219192755801, 11.0, "#FFFFFF"),
                        ("专注35+女性身材管理", -0.14366189100415258, 11.0, "#FFFFFF"),
                    ],
                )
                self.assertEqual(job["texts"][0]["stroke_color"], "")
                self.assertEqual(job["texts"][0]["stroke_width"], 0.0)
                self.assertEqual(job["texts"][0]["opacity"], 1.0)
                self.assertEqual(job["texts"][1]["opacity"], 0.5)
                self.assertEqual(job["fixed_overlays"][0]["rotation"], -90.0)
                self.assertEqual(job["fixed_overlays"][0]["renderer"], "sticker")
                self.assertAlmostEqual(job["fixed_overlays"][0]["scale"], 0.8941348042237189)
                self.assertAlmostEqual(job["cover"]["text_scale"], 1.1045453049181124)
                self.assertFalse(job["cover"]["auto_wrapping"])
                downloaded = client.get(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/current-video"
                )
                self.assertEqual(downloaded.status_code, 200, downloaded.text)
                self.assertEqual(downloaded.content, b"captioned-with-bgm")

                retried = client.patch(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/postprocess-settings",
                    json={
                        "font_identity": font["identity"],
                        "bgm_identity": bgm["identity"],
                        "text_color": "#FFFFFF",
                        "force_retry": True,
                    },
                )
                self.assertEqual(retried.status_code, 200, retried.text)
                retried_row = retried.json()["items"][0]
                self.assertEqual(retried_row["status"], "BASE_VIDEO_READY")
                self.assertIsNone(retried_row["outputs"]["composition_video"])
                self.assertEqual(
                    len(retried_row["asset_history"].get("composition_video", [])), 1
                )
                regenerated = client.post(
                    f"/api/new/projects/{project['project_id']}/postprocess/generate",
                    json={
                        "idempotency_key": "postprocess-retry-1",
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "font_identity": font["identity"],
                                "bgm_identity": bgm["identity"],
                                "text_color": "#FFFFFF",
                            }
                        ],
                    },
                )
                self.assertEqual(regenerated.status_code, 200, regenerated.text)
                self.assertEqual(
                    regenerated.json()["items"][0]["subtitles"]["style"]["font_size"],
                    11.0,
                )
                self.assertEqual(
                    regenerated.json()["items"][0]["settings"]["postprocess"]["top_title"],
                    {"label": "减肥大实话", "headline": "只有坚持才能达成目标"},
                )

                changed = client.patch(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/postprocess-settings",
                    json={
                        "font_identity": font["identity"],
                        "bgm_identity": "",
                        "text_color": "#00FF00",
                        "layout_profile": "seated",
                    },
                )
                self.assertEqual(changed.status_code, 200, changed.text)
                changed_row = changed.json()["items"][0]
                self.assertEqual(changed_row["status"], "BASE_VIDEO_READY")
                self.assertIsNotNone(changed_row["outputs"]["base_video"])
                self.assertIsNone(changed_row["outputs"]["composition_video"])
                self.assertEqual(
                    len(changed_row["asset_history"].get("composition_video", [])), 1
                )
                self.assertTrue(changed_row["allowed_actions"]["start_postprocess"])
                self.assertEqual(
                    changed_row["settings"]["postprocess"]["layout_profile"], "seated"
                )

                retried = client.post(
                    f"/api/new/projects/{project['project_id']}/postprocess/generate",
                    json={
                        "idempotency_key": "preview-retry-one-row",
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "font_identity": font["identity"],
                                "bgm_identity": "",
                                "text_color": "#00FF00",
                                "layout_profile": "seated",
                            }
                        ],
                    },
                )
                self.assertEqual(retried.status_code, 200, retried.text)
                retried_row = retried.json()["items"][0]
                self.assertEqual(retried_row["subtitles"]["status"], "PREVIEW_READY")
                self.assertEqual(retried_row["subtitles"]["style"]["text_color"], "#FFFFFF")
                self.assertEqual(retried_row["subtitles"]["style"]["font_size"], 15.0)
                self.assertEqual(retried_row["subtitles"]["style"]["clip_scale"], 1.0)
                self.assertAlmostEqual(
                    retried_row["subtitles"]["style"]["transform_y"],
                    -0.32080308951309267,
                )
                self.assertIsNone(retried_row["outputs"]["composition_video"])
                self.assertEqual(captured["submit_count"], 4)

                current = store.get_project(user["user_id"], project["project_id"])
                current_row = current["items"][0]
                expected_hash = current_row["content_analysis"]["script_sha256"]
                self.assertTrue(
                    store.complete_item_content_analysis(
                        user["user_id"],
                        project["project_id"],
                        item["item_id"],
                        expected_script_sha256=expected_hash,
                        result={
                            "music_analysis_status": "SUCCESS",
                            "subtitle_analysis_status": "FAILED",
                            "music_intent": _health_music_intent(),
                            "subtitle_units": None,
                            "errors": {
                                "music": None,
                                "subtitle": {"code": "INVALID_SUBTITLES"},
                            },
                            "schema_version": "jyd.content-analysis.v1",
                        },
                    )
                )
                auto = client.patch(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/postprocess-settings",
                    json={
                        "font_identity": font["identity"],
                        "bgm_identity": "",
                        "bgm_selection_mode": "auto",
                        "text_color": "#00FF00",
                    },
                )
                self.assertEqual(auto.status_code, 200, auto.text)
                self.assertEqual(
                    auto.json()["items"][0]["settings"]["postprocess"]
                    ["music_selection"]["status"],
                    "NOT_REQUESTED",
                )
                auto_preview = client.post(
                    f"/api/new/projects/{project['project_id']}/postprocess/generate",
                    json={
                        "idempotency_key": "preview-ai-top1",
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "font_identity": font["identity"],
                                "bgm_identity": "",
                                "bgm_selection_mode": "auto",
                                "text_color": "#00FF00",
                            }
                        ],
                    },
                )
                self.assertEqual(auto_preview.status_code, 200, auto_preview.text)
                auto_row = auto_preview.json()["items"][0]
                auto_settings = auto_row["settings"]["postprocess"]
                self.assertEqual(auto_settings["bgm_selection_mode"], "auto")
                self.assertEqual(
                    auto_settings["bgm_identity"], "music_id:6874387537750657031"
                )
                selection = auto_settings["music_selection"]
                self.assertEqual(selection["status"], "SUCCESS")
                self.assertEqual(selection["selection_source"], "ai")
                self.assertEqual(selection["video_duration_us"], 4_000_000)
                self.assertEqual(selection["audio_asset_id"], audio["asset_id"])
                self.assertIn("matcher_version", selection)
                self.assertIn("profile_hash", selection)
                self.assertNotIn("top3", selection)
                self.assertNotIn("candidates", selection)
                self.assertEqual(
                    auto_row["content_analysis"]["subtitle_analysis_status"], "FAILED"
                )
                self.assertEqual(auto_row["subtitles"]["status"], "PREVIEW_READY")

                layout_changed = client.patch(
                    f"/api/new/projects/{project['project_id']}/items/{item['item_id']}/postprocess-settings",
                    json={
                        "font_identity": font["identity"],
                        "bgm_identity": "",
                        "bgm_selection_mode": "auto",
                        "preserve_auto_bgm": True,
                        "text_color": "#00FF00",
                        "layout_profile": "seated",
                    },
                )
                self.assertEqual(layout_changed.status_code, 200, layout_changed.text)
                preserved = layout_changed.json()["items"][0]["settings"]["postprocess"]
                self.assertEqual(preserved["layout_profile"], "seated")
                self.assertEqual(
                    preserved["bgm_identity"], "music_id:6874387537750657031"
                )
                self.assertEqual(preserved["music_selection"], selection)
                self.assertEqual(preserved["bgm_volume"], auto_settings["bgm_volume"])


if __name__ == "__main__":
    unittest.main()
