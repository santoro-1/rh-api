from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.render_job import _build_visual_variant  # noqa: E402
from jyd_probe.content_replace import _apply_original_video_volume  # noqa: E402
from jyd_probe.visual_variant import (  # noqa: E402
    VisualVariant,
    _face_center_with_headroom,
    _snap_crop_to_top,
    apply_visual_variant_to_data,
)


class FakeFaceLocator:
    def locate(
        self,
        media_path: str,
        source_start_us: int,
        source_duration_us: int,
        sample_count: int,
    ) -> tuple[float, float] | None:
        return (0.5, 0.25)


class NoFaceLocator:
    def locate(
        self,
        media_path: str,
        source_start_us: int,
        source_duration_us: int,
        sample_count: int,
    ) -> tuple[float, float] | None:
        return None


def _draft() -> dict:
    return {
        "duration": 25_000_000,
        "tracks": [
            {
                "id": "video-track",
                "type": "video",
                "segments": [
                    {
                        "id": "segment-a",
                        "material_id": "video-a",
                        "target_timerange": {"start": 0, "duration": 25_000_000},
                        "source_timerange": {"start": 2_000_000, "duration": 25_000_000},
                        "clip": {"flip": {"horizontal": False, "vertical": False}},
                        "extra_material_refs": ["old-canvas"],
                    }
                ],
            }
        ],
        "materials": {
            "videos": [
                {
                    "id": "video-a",
                    "type": "video",
                    "path": "C:/media/input.mp4",
                    "width": 1080,
                    "height": 1920,
                }
            ],
            "canvases": [
                {"id": "old-canvas", "type": "canvas_color", "color": "#FFFFFFFF"}
            ],
            "transitions": [],
            "speeds": [],
        },
    }


class VisualVariantTest(unittest.TestCase):
    def test_manual_crop_adjustment_is_parsed(self) -> None:
        variant = _build_visual_variant({
            "visual_variant": {
                "enabled": True,
                "crop_offset_y": -0.25,
                "crop_zoom": 1.2,
            }
        })

        self.assertIsNotNone(variant)
        assert variant is not None
        self.assertEqual(variant.crop_offset_y, -0.25)
        self.assertEqual(variant.crop_zoom, 1.2)

    def test_original_video_volume_preserves_existing_mix(self) -> None:
        data = _draft()
        data["tracks"][0]["segments"][0]["volume"] = 0.8

        changed = _apply_original_video_volume(data, 0.5)

        self.assertEqual(changed, 1)
        self.assertAlmostEqual(data["tracks"][0]["segments"][0]["volume"], 0.4)

    def test_face_anchor_moves_up_to_preserve_hair_and_headroom(self) -> None:
        center_x, center_y = _face_center_with_headroom(400, 300, 200, 240, 1000, 1600)

        self.assertAlmostEqual(center_x, 0.5)
        self.assertLess(center_y, (300 + 120) / 1600)
        self.assertAlmostEqual(center_y, (420 - 240 * 0.75) / 1600)

    def test_small_top_crop_is_snapped_to_source_top(self) -> None:
        crop = {
            "upper_left_x": 0.0,
            "upper_left_y": 0.07,
            "upper_right_x": 1.0,
            "upper_right_y": 0.07,
            "lower_left_x": 0.0,
            "lower_left_y": 0.63,
            "lower_right_x": 1.0,
            "lower_right_y": 0.63,
        }

        adjusted = _snap_crop_to_top(crop)

        self.assertEqual(adjusted["upper_left_y"], 0.0)
        self.assertAlmostEqual(adjusted["lower_left_y"], 0.56)

    def test_crops_once_before_splitting_for_alternating_mirror(self) -> None:
        data = _draft()
        changed = apply_visual_variant_to_data(
            data,
            VisualVariant(
                mirror_interval_us=10_000_000,
                crop_ratio="1:1",
                background_color="#123456",
            ),
            face_locator=FakeFaceLocator(),
        )

        self.assertGreater(changed, 0)
        segments = data["tracks"][0]["segments"]
        self.assertEqual(
            [item["target_timerange"] for item in segments],
            [
                {"start": 0, "duration": 10_000_000},
                {"start": 10_000_000, "duration": 10_000_000},
                {"start": 20_000_000, "duration": 5_000_000},
            ],
        )
        self.assertEqual(
            [item["source_timerange"] for item in segments],
            [
                {"start": 2_000_000, "duration": 10_000_000},
                {"start": 12_000_000, "duration": 10_000_000},
                {"start": 22_000_000, "duration": 5_000_000},
            ],
        )
        self.assertEqual(
            [item["clip"]["flip"]["horizontal"] for item in segments],
            [False, True, False],
        )
        self.assertEqual(len(data["materials"]["videos"]), 2)
        self.assertEqual(len(data["materials"]["canvases"]), 2)
        cropped_material_ids = {segment["material_id"] for segment in segments}
        self.assertEqual(len(cropped_material_ids), 1)
        for segment in segments:
            self.assertNotIn("old-canvas", segment["extra_material_refs"])
            material = next(
                item for item in data["materials"]["videos"]
                if item["id"] == segment["material_id"]
            )
            crop = material["crop"]
            self.assertAlmostEqual(crop["upper_left_y"], 0.0)
            self.assertAlmostEqual(crop["lower_left_y"], 0.5625)
        self.assertEqual(data["materials"]["canvases"][1].get("color"), "#123456FF")

    def test_portrait_crop_uses_top_edge_when_face_detection_fails(self) -> None:
        data = _draft()
        warnings: list[str] = []

        apply_visual_variant_to_data(
            data,
            VisualVariant(mirror_interval_us=0, crop_ratio="1:1"),
            face_locator=NoFaceLocator(),
            warning=warnings.append,
        )

        crop = data["materials"]["videos"][-1]["crop"]
        self.assertEqual(crop["upper_left_y"], 0.0)
        self.assertAlmostEqual(crop["lower_left_y"], 0.5625)
        self.assertEqual(len(warnings), 1)

    def test_parses_visual_variant_job_settings(self) -> None:
        variant = _build_visual_variant(
            {
                "visual_variant": {
                    "mirror_interval_seconds": 7.5,
                    "crop_ratio": "3:4",
                    "background_color": "#ABCDEF",
                }
            }
        )

        self.assertIsNotNone(variant)
        assert variant is not None
        self.assertEqual(variant.mirror_interval_us, 7_500_000)
        self.assertEqual(variant.crop_ratio, "3:4")
        self.assertEqual(variant.background_color, "#ABCDEF")


if __name__ == "__main__":
    unittest.main()
