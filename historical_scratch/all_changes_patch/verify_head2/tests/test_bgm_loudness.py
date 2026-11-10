from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.bgm_loudness import (  # noqa: E402
    BGM_FALLBACK_VOLUME,
    automatic_bgm_mix,
    fallback_bgm_volume,
    volume_from_loudness,
)
from jyd_probe.project_postprocess import resolve_project_cover_image  # noqa: E402


class AutomaticBgmLoudnessTest(unittest.TestCase):
    def test_volume_is_relative_to_voice_and_bounded(self) -> None:
        self.assertAlmostEqual(volume_from_loudness(-16.0, -14.0), 0.2239, places=4)
        self.assertEqual(volume_from_loudness(-16.0, -40.0), 0.25)
        self.assertEqual(volume_from_loudness(-16.0, 0.0), 0.08)

    @patch("jyd_probe.bgm_loudness.measure_integrated_lufs")
    def test_one_mix_snapshot_is_reused_by_preview_and_export(self, measure) -> None:
        measure.side_effect = [-16.0, -14.0]
        mix = automatic_bgm_mix("voice.mp3", "bgm.mp3")
        self.assertEqual(mix["algorithm"], "speech-relative-lufs.v1")
        self.assertEqual(mix["volume"], 0.2239)
        self.assertFalse(mix["fallback"])

    @patch("jyd_probe.bgm_loudness.measure_integrated_lufs", side_effect=ValueError("bad audio"))
    def test_analysis_failure_uses_conservative_program_default(self, _measure) -> None:
        mix = automatic_bgm_mix("voice.mp3", "bgm.mp3")
        self.assertEqual(mix["volume"], BGM_FALLBACK_VOLUME)
        self.assertTrue(mix["fallback"])

    @patch("jyd_probe.bgm_loudness.measure_integrated_lufs")
    def test_strong_vocals_get_four_more_db_of_narration_headroom(self, measure) -> None:
        measure.side_effect = [-16.0, -14.0]
        mix = automatic_bgm_mix("voice.mp3", "bgm.mp3", strong_vocals=True)
        self.assertEqual(mix["target_gap_db"], 15.0)
        self.assertEqual(mix["volume"], 0.1413)
        self.assertTrue(mix["strong_vocals"])
        self.assertLess(mix["volume"], 0.2239)

    def test_strong_vocal_fallback_is_lower_than_normal_fallback(self) -> None:
        self.assertEqual(fallback_bgm_volume(strong_vocals=True), 0.1136)
        self.assertLess(fallback_bgm_volume(strong_vocals=True), BGM_FALLBACK_VOLUME)


class FrozenCoverImageTest(unittest.TestCase):
    def test_existing_video_uses_matching_historical_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_path = root / "current.png"
            frozen_path = root / "frozen.png"
            current_path.write_bytes(b"current")
            frozen_path.write_bytes(b"frozen")
            item = {
                "row_key": "3",
                "inputs": {
                    "image": {
                        "asset_id": "current",
                        "managed_path": str(current_path),
                        "metadata": {"sha256": "current-sha"},
                    }
                },
                "outputs": {
                    "base_video": {
                        "metadata": {
                            "input_image_asset_id": "frozen",
                            "input_image_sha256": "frozen-sha",
                        }
                    }
                },
                "asset_history": {
                    "input_image": [
                        {
                            "asset_id": "frozen",
                            "managed_path": str(frozen_path),
                            "metadata": {"sha256": "frozen-sha"},
                        }
                    ]
                },
            }

            resolved = resolve_project_cover_image(item)

            self.assertEqual(resolved["asset_id"], "frozen")

    def test_missing_frozen_image_never_silently_uses_current_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            current_path = Path(temporary) / "current.png"
            current_path.write_bytes(b"current")
            item = {
                "row_key": "9",
                "inputs": {
                    "image": {
                        "asset_id": "current",
                        "managed_path": str(current_path),
                        "metadata": {"sha256": "current-sha"},
                    }
                },
                "outputs": {
                    "base_video": {
                        "metadata": {"input_image_sha256": "missing-sha"}
                    }
                },
                "asset_history": {"input_image": []},
            }

            with self.assertRaisesRegex(ValueError, "已停止生成可能配错的封面"):
                resolve_project_cover_image(item)


if __name__ == "__main__":
    unittest.main()
