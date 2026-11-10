from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.draft_upload_plan import build_draft_upload_plan  # noqa: E402


class DraftUploadPlanTest(unittest.TestCase):
    def test_only_uploads_dependencies_that_will_be_retained(self) -> None:
        report = {
            "report_id": "report-1",
            "draft": {"name": "母版"},
            "dependencies": [
                self._dependency("video", "upload_required", False, 100),
                self._dependency("audio", "upload_required", True, 20),
                self._dependency("video_effect", "upload_required", True, 30),
                self._dependency("video_adjustment", "upload_required", False, 40),
                self._dependency("font", "upload_required", True, 10),
                self._dependency("text_effect", "central_library", True, 5),
                self._dependency("text_template_resource", "missing", True, 0),
            ],
        }
        plan = build_draft_upload_plan(
            report,
            {
                "audio": "replace",
                "video_effects": "remove",
                "text_style": "replace",
                "text_effects": "keep",
                "text_templates": "keep",
            },
        )
        by_kind = {item["kind"]: item for item in plan["dependencies"]}
        self.assertEqual(by_kind["video"]["decision"], "upload")
        self.assertEqual(by_kind["audio"]["decision"], "skip_replaced")
        self.assertEqual(by_kind["video_effect"]["decision"], "skip_removed")
        self.assertEqual(by_kind["video_adjustment"]["decision"], "upload")
        self.assertEqual(by_kind["font"]["decision"], "skip_replaced")
        self.assertEqual(by_kind["text_effect"]["decision"], "reuse_library")
        self.assertEqual(by_kind["text_template_resource"]["decision"], "blocked_missing")
        self.assertEqual(plan["summary"]["upload_count"], 2)
        self.assertEqual(plan["summary"]["upload_size_bytes"], 140)
        self.assertEqual(plan["summary"]["skipped_count"], 3)
        self.assertFalse(plan["summary"]["ready_for_upload"])

    def test_rejects_unknown_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "不合法"):
            build_draft_upload_plan({"dependencies": []}, {"audio": "sometimes"})

    def test_composite_text_templates_are_always_preserved(self) -> None:
        report = {
            "report_id": "report-2",
            "dependencies": [
                self._dependency("text_template_resource", "upload_required", True, 40),
            ],
        }
        plan = build_draft_upload_plan(report, {"text_templates": "remove"})
        self.assertEqual(plan["policies"]["text_templates"], "keep")
        self.assertEqual(plan["dependencies"][0]["decision"], "upload")
        self.assertTrue(plan["summary"]["ready_for_upload"])

    def test_template_center_skips_primary_video_and_audio_but_keeps_visuals(self) -> None:
        report = {
            "report_id": "template-center-report",
            "draft": {"main_video": {"material_id": "main-video"}},
            "dependencies": [
                {
                    **self._dependency("video", "upload_required", False, 1_000),
                    "references": [{"material_id": "main-video"}],
                },
                {
                    **self._dependency("video", "upload_required", False, 200),
                    "references": [{"material_id": "overlay-video"}],
                },
                self._dependency("audio", "missing", False, 0),
                self._dependency("sound_effect", "missing", False, 0),
                self._dependency("text_effect", "central_library", True, 30),
                self._dependency("sticker", "upload_required", False, 40),
            ],
        }
        plan = build_draft_upload_plan(
            report,
            {
                "audio": "keep",
                "video_effects": "keep",
                "text_style": "keep",
                "text_effects": "keep",
                "text_templates": "keep",
            },
            mode="template_center",
        )
        videos = [item for item in plan["dependencies"] if item["kind"] == "video"]
        self.assertEqual(plan["mode"], "template_center")
        self.assertEqual(videos[0]["decision"], "skip_replaced")
        self.assertEqual(videos[1]["decision"], "upload")
        self.assertEqual(
            {item["kind"]: item["decision"] for item in plan["dependencies"]}["audio"],
            "skip_replaced",
        )
        self.assertEqual(plan["summary"]["upload_size_bytes"], 270)
        self.assertTrue(plan["summary"]["ready_for_upload"])

    def test_rejects_unknown_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "上传清单模式"):
            build_draft_upload_plan({"dependencies": []}, mode="archive")

    @staticmethod
    def _dependency(kind: str, status: str, can_skip: bool, size: int) -> dict[str, object]:
        return {
            "kind": kind,
            "status": status,
            "can_skip_if_replaced": can_skip,
            "size_bytes": size,
            "path": f"D:/{kind}",
        }


if __name__ == "__main__":
    unittest.main()
