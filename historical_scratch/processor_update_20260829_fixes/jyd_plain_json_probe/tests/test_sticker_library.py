from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.sticker_apply import add_fullscreen_sticker_to_data  # noqa: E402
from jyd_probe.sticker_export import (  # noqa: E402
    corner_alpha_reveal,
    export_sticker_library,
    visible_content_bounds,
)


class StickerLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"sticker_{uuid.uuid4().hex}"
        self.resource = self.temp / "source_resource"
        self.resource.mkdir(parents=True)
        (self.resource / "config.json").write_text('{"version":"4.0.0"}', encoding="utf-8")
        (self.resource / "singleImage.png").write_bytes(b"fake-png-preview")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_exports_resource_bundle_and_adds_full_timeline_sticker(self) -> None:
        source = {
            "duration": 5_050_000,
            "tracks": [
                {
                    "id": "track-old",
                    "type": "sticker",
                    "segments": [
                        {
                            "id": "segment-old",
                            "material_id": "material-old",
                            "render_index": 14003,
                            "clip": {"transform": {"x": 0.0, "y": 0.0}},
                            "target_timerange": {"duration": 2_000_000},
                        }
                    ],
                }
            ],
            "materials": {
                "stickers": [
                    {
                        "id": "material-old",
                        "name": "测试全屏边框",
                        "path": str(self.resource),
                        "resource_id": "resource-1",
                        "sticker_id": "resource-1",
                        "type": "sticker",
                    }
                ]
            },
        }
        result = export_sticker_library(source, self.temp / "library", source_label="test")
        self.assertEqual(result.exported_count, 1)
        metadata_path = Path(result.stickers[0]["metadata_file"])
        metadata_path = result.output_dir / metadata_path
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        copied_resource = metadata_path.parent / payload["resource"]["library_path"]
        self.assertTrue((copied_resource / "config.json").is_file())
        self.assertTrue((copied_resource / "singleImage.png").is_file())
        self.assertEqual(payload["usage"], "fullscreen_overlay")

        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 200, "height": 400},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }
        changed = add_fullscreen_sticker_to_data(target, metadata_path)
        self.assertEqual(changed, 1)
        self.assertEqual(len(target["materials"]["stickers"]), 1)
        new_material = target["materials"]["stickers"][0]
        self.assertNotEqual(new_material["id"], "material-old")
        self.assertEqual(Path(new_material["path"]), copied_resource.resolve())
        sticker_track = target["tracks"][-1]
        self.assertEqual(sticker_track["type"], "sticker")
        self.assertEqual(
            sticker_track["segments"][0]["target_timerange"],
            {"start": 0, "duration": 8_000_000},
        )
        self.assertEqual(sticker_track["segments"][0]["material_id"], new_material["id"])

    def test_adds_four_corner_stickers_with_five_percent_visible(self) -> None:
        metadata_path = self._export_single_sticker()
        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 200, "height": 400},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }

        for corner in ("top_left", "top_right", "bottom_left", "bottom_right"):
            add_fullscreen_sticker_to_data(
                target,
                metadata_path,
                corner=corner,
                visible_ratio=0.05,
            )

        clips = [track["segments"][0]["clip"] for track in target["tracks"][1:]]
        transforms = [clip["transform"] for clip in clips]
        self.assertEqual(
            [(item["x"], item["y"]) for item in transforms],
            [(-1.9, 1.9), (1.9, 1.9), (-1.9, -1.9), (1.9, -1.9)],
        )
        self.assertTrue(all(clip["scale"] == {"x": 1.0, "y": 1.0} for clip in clips))
        self.assertTrue(all(clip["rotation"] == 0.0 for clip in clips))

    def test_corner_sticker_has_independent_scale_and_opacity(self) -> None:
        metadata_path = self._export_single_sticker()
        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 200, "height": 400},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }

        add_fullscreen_sticker_to_data(
            target,
            metadata_path,
            corner="top_left",
            visible_ratio=0.05,
            scale=0.1,
            opacity=0.05,
        )

        segment = target["tracks"][-1]["segments"][0]
        self.assertEqual(segment["global_alpha"], 0.05)
        self.assertEqual(segment["clip"]["scale"], {"x": 0.1, "y": 0.1})

    def test_corner_position_uses_non_transparent_content_bounds(self) -> None:
        preview = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(preview).rectangle((70, 10, 89, 29), fill=(0, 120, 255, 255))
        preview.save(self.resource / "singleImage.png")
        metadata_path = self._export_single_sticker()
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        bounds = payload["content_bounds"]
        self.assertAlmostEqual(bounds["left"], 0.7)
        self.assertAlmostEqual(bounds["top"], 0.1)
        self.assertAlmostEqual(bounds["right"], 0.9)
        self.assertAlmostEqual(bounds["bottom"], 0.3)

        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 200, "height": 400},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }
        add_fullscreen_sticker_to_data(
            target,
            metadata_path,
            corner="top_left",
            visible_ratio=0.1,
        )

        transform = target["tracks"][-1]["segments"][0]["clip"]["transform"]
        self.assertGreater(transform["x"], -1.0)
        self.assertLess(transform["y"], 1.0)

    def test_application_recomputes_preview_bounds_when_metadata_is_stale(self) -> None:
        preview = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(preview).rectangle((70, 10, 89, 29), fill=(0, 120, 255, 255))
        preview.save(self.resource / "singleImage.png")
        metadata_path = self._export_single_sticker()
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["content_bounds"] = {
            "left": 0.0,
            "top": 0.0,
            "right": 1.0,
            "bottom": 1.0,
            "source_width": 9999,
            "source_height": 9999,
        }
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 200, "height": 400},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }

        add_fullscreen_sticker_to_data(
            target,
            metadata_path,
            corner="top_left",
            visible_ratio=0.1,
        )

        transform = target["tracks"][-1]["segments"][0]["clip"]["transform"]
        self.assertGreater(transform["x"], -1.0)
        self.assertLess(transform["y"], 1.0)

    def test_corner_reveal_counts_alpha_when_bounding_box_corner_is_empty(self) -> None:
        preview = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        draw = ImageDraw.Draw(preview)
        draw.rectangle((70, 10, 89, 19), fill=(255, 255, 255, 255))
        draw.rectangle((10, 70, 19, 89), fill=(255, 255, 255, 255))
        preview_path = self.resource / "singleImage.png"
        preview.save(preview_path)

        reveal = corner_alpha_reveal(preview_path, "top_left", 0.1)

        self.assertIsNotNone(reveal)
        assert reveal is not None
        cut_x = float(reveal["cut_x"])
        cut_y = float(reveal["cut_y"])
        alpha = preview.getchannel("A")
        total_alpha = 0
        visible_alpha = 0
        for y in range(preview.height):
            for x in range(preview.width):
                value = int(alpha.getpixel((x, y)))
                total_alpha += value
                if (x + 0.5) / preview.width >= cut_x and (y + 0.5) / preview.height >= cut_y:
                    visible_alpha += value
        self.assertGreaterEqual(visible_alpha / total_alpha, 0.1)
        self.assertEqual(
            alpha.crop((82, 82, 100, 100)).getbbox(),
            None,
            "The old bounding-box corner contains no visible pixels",
        )

    def test_sprite_sheet_uses_frame_size_instead_of_atlas_size(self) -> None:
        atlas = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
        draw = ImageDraw.Draw(atlas)
        draw.rectangle((7, 2, 9, 7), fill=(255, 0, 0, 255))
        draw.rectangle((10, 1, 12, 6), fill=(0, 120, 255, 255))
        atlas_path = self.resource / "SequenceMap.png"
        atlas.save(atlas_path)
        animation = {
            "frames": [
                {
                    "frame": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "sourceSize": {"w": 10, "h": 10},
                    "spriteSourceSize": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "rotated": "false",
                },
                {
                    "frame": {"x": 10, "y": 0, "w": 10, "h": 10},
                    "sourceSize": {"w": 10, "h": 10},
                    "spriteSourceSize": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "rotated": "false",
                },
            ],
            "meta": {"image": "SequenceMap.png"},
        }
        (self.resource / "ani_info.json").write_text(
            json.dumps(animation), encoding="utf-8"
        )

        bounds = visible_content_bounds(atlas_path)

        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertEqual(bounds["source_width"], 10)
        self.assertEqual(bounds["source_height"], 10)
        self.assertAlmostEqual(bounds["left"], 0.0)
        self.assertAlmostEqual(bounds["top"], 0.1)
        self.assertAlmostEqual(bounds["right"], 1.0)
        self.assertAlmostEqual(bounds["bottom"], 0.8)
        self.assertAlmostEqual(bounds["always_visible_left"], 0.7)
        self.assertAlmostEqual(bounds["always_visible_right"], 0.3)

    def test_animated_sticker_keeps_each_frame_visible_in_a_corner(self) -> None:
        atlas = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
        draw = ImageDraw.Draw(atlas)
        draw.rectangle((7, 2, 9, 7), fill=(255, 0, 0, 255))
        draw.rectangle((10, 1, 12, 6), fill=(0, 120, 255, 255))
        atlas_path = self.resource / "SequenceMap.png"
        atlas.save(atlas_path)
        animation = {
            "frames": [
                {
                    "frame": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "sourceSize": {"w": 10, "h": 10},
                    "spriteSourceSize": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "rotated": "false",
                },
                {
                    "frame": {"x": 10, "y": 0, "w": 10, "h": 10},
                    "sourceSize": {"w": 10, "h": 10},
                    "spriteSourceSize": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "rotated": "false",
                },
            ],
            "meta": {"image": "SequenceMap.png"},
        }
        (self.resource / "ani_info.json").write_text(json.dumps(animation), encoding="utf-8")
        metadata_path = self._export_single_sticker()
        target = {
            "duration": 8_000_000,
            "canvas_config": {"width": 10, "height": 10},
            "tracks": [{"id": "video-track", "type": "video", "segments": []}],
            "materials": {"stickers": []},
        }

        add_fullscreen_sticker_to_data(
            target,
            metadata_path,
            corner="top_left",
            visible_ratio=0.05,
        )

        transform = target["tracks"][-1]["segments"][0]["clip"]["transform"]
        self.assertGreater(transform["x"], -1.0)
        self.assertLess(transform["y"], 1.5)

    def _export_single_sticker(self) -> Path:
        source = {
            "duration": 5_050_000,
            "tracks": [
                {
                    "id": "track-old",
                    "type": "sticker",
                    "segments": [
                        {
                            "id": "segment-old",
                            "material_id": "material-old",
                            "render_index": 14003,
                            "clip": {"transform": {"x": 0.0, "y": 0.0}},
                            "target_timerange": {"duration": 2_000_000},
                        }
                    ],
                }
            ],
            "materials": {
                "stickers": [
                    {
                        "id": "material-old",
                        "name": "测试全屏边框",
                        "path": str(self.resource),
                        "resource_id": "resource-1",
                        "sticker_id": "resource-1",
                        "type": "sticker",
                    }
                ]
            },
        }
        result = export_sticker_library(source, self.temp / "library")
        return result.output_dir / Path(result.stickers[0]["metadata_file"])


if __name__ == "__main__":
    unittest.main()
