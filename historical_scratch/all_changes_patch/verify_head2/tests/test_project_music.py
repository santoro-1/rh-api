from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.music_matching import MusicProfileMatcher  # noqa: E402
from jyd_probe.project_music import (  # noqa: E402
    ProjectMusicSelector,
    item_video_duration_us,
    manual_music_selection,
)
from jyd_probe.project_store import ProjectStore  # noqa: E402


AUDIO_ROOT = PROJECT_ROOT / "data" / "libraries" / "audio_library"


def _item(*, music_status: str = "FAILED", music_intent=None) -> dict:
    return {
        "script_text": "鸡蛋是常见的营养食物。",
        "outputs": {
            "audio": {"asset_id": "audio-v2", "metadata": {}},
            "base_video": None,
            "original_video_segments": [],
        },
        "subtitles": {
            "bound_audio_asset_id": "audio-v2",
            "raw_cues": [
                {"start_us": 0, "end_us": 7_500_000, "text": "鸡蛋是常见的营养食物。"}
            ],
        },
        "content_analysis": {
            "music_analysis_status": music_status,
            "music_intent": music_intent,
            "subtitle_analysis_status": "SUCCESS",
        },
    }


class ProjectMusicSelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matcher = MusicProfileMatcher(AUDIO_ROOT)
        snapshot = cls.matcher.snapshot()
        cls.available = snapshot["assets_by_identity"]

    def test_manual_track_and_manual_none_are_both_authoritative(self) -> None:
        item = _item()
        selected = manual_music_selection(item, "music_id:manual")
        self.assertEqual(selected["selection_source"], "manual")
        self.assertEqual(selected["bgm_identity"], "music_id:manual")
        self.assertEqual(selected["reason_code"], "USER_SELECTED")

        none = manual_music_selection(item, "")
        self.assertEqual(none["selection_source"], "manual")
        self.assertIsNone(none["bgm_identity"])
        self.assertEqual(none["reason_code"], "USER_SELECTED_NONE")

    def test_failed_music_analysis_uses_project_default_then_none(self) -> None:
        identity = "music_id:6874387537750657031"
        selector = ProjectMusicSelector(self.matcher, self.available)
        selected, snapshot = selector.resolve_auto(
            {"settings": {"default_bgm_identity": identity}}, _item()
        )
        self.assertEqual(selected, identity)
        self.assertEqual(snapshot["status"], "FALLBACK")
        self.assertEqual(snapshot["selection_source"], "project_default")

        selected, snapshot = selector.resolve_auto({"settings": {}}, _item())
        self.assertEqual(selected, "")
        self.assertEqual(snapshot["selection_source"], "none")
        self.assertEqual(snapshot["reason_code"], "MUSIC_ANALYSIS_UNAVAILABLE")

    def test_duration_uses_only_cues_bound_to_current_audio(self) -> None:
        item = _item()
        self.assertEqual(item_video_duration_us(item), 7_500_000)
        item["subtitles"]["bound_audio_asset_id"] = "audio-v1"
        item["outputs"]["base_video"] = {"metadata": {"duration_us": 9_000_000}}
        self.assertEqual(item_video_duration_us(item), 9_000_000)

    def test_real_video_duration_wins_when_audio_is_longer(self) -> None:
        item = _item()
        item["outputs"]["audio"]["metadata"] = {"duration_us": 10_000_000}
        item["outputs"]["base_video"] = {"metadata": {"duration_us": 9_000_000}}
        self.assertEqual(item_video_duration_us(item), 9_000_000)

    def test_analysis_can_choose_a_preliminary_top1_before_audio_exists(self) -> None:
        selector = ProjectMusicSelector(self.matcher, self.available)
        item = _item(
            music_status="SUCCESS",
            music_intent={
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
                "vocal_preference": "prefer_instrumental",
                "opening_preference": "soft",
                "avoid_traits": ["strong_vocals", "dense_arrangement"],
                "confidence": 0.9,
            },
        )
        item["outputs"]["audio"] = None
        item["subtitles"]["raw_cues"] = []
        selected, snapshot = selector.resolve_for_analysis({"settings": {}}, item)
        self.assertIn(selected, self.available)
        self.assertEqual(snapshot["status"], "SUCCESS")
        self.assertEqual(snapshot["video_duration_us"], 0)

    def test_script_and_audio_changes_invalidate_only_auto_selection(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp" / f"project_music_{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root / "control.db")
            project = store.create_project(
                owner_user_id="user",
                owner_username="tester",
                name="音乐失效测试",
                items=[{"row_key": "1", "script_text": "原脚本"}],
            )
            item_id = project["items"][0]["item_id"]
            store.configure_item_postprocess(
                "user",
                project["project_id"],
                item_id,
                font_identity="font",
                bgm_identity="music_id:auto",
                bgm_selection_mode="auto",
                text_color="#FFFFFF",
                music_selection={"status": "SUCCESS"},
            )
            changed = store.update_item(
                "user", project["project_id"], item_id, script_text="新脚本"
            )
            auto_settings = changed["items"][0]["settings"]["postprocess"]
            self.assertEqual(auto_settings["bgm_identity"], "")
            self.assertEqual(
                auto_settings["music_selection"]["reason_code"], "SCRIPT_CHANGED"
            )

            store.configure_item_postprocess(
                "user",
                project["project_id"],
                item_id,
                font_identity="font",
                bgm_identity="music_id:auto-v2",
                bgm_selection_mode="auto",
                text_color="#FFFFFF",
                music_selection={"status": "SUCCESS"},
            )
            regenerated = store.prepare_item_audio_generation(
                "user", project["project_id"], item_id
            )
            auto_settings = regenerated["items"][0]["settings"]["postprocess"]
            self.assertEqual(auto_settings["bgm_identity"], "music_id:auto-v2")
            self.assertEqual(auto_settings["music_selection"]["status"], "STALE")
            self.assertEqual(
                auto_settings["music_selection"]["reason_code"],
                "AUDIO_VERSION_CHANGED",
            )

            store.configure_item_postprocess(
                "user",
                project["project_id"],
                item_id,
                font_identity="font",
                bgm_identity="music_id:manual",
                bgm_selection_mode="manual",
                text_color="#FFFFFF",
            )
            manual_changed = store.update_item(
                "user", project["project_id"], item_id, script_text="第三版脚本"
            )
            manual_settings = manual_changed["items"][0]["settings"]["postprocess"]
            self.assertEqual(manual_settings["bgm_identity"], "music_id:manual")
            self.assertEqual(manual_settings["bgm_selection_mode"], "manual")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
