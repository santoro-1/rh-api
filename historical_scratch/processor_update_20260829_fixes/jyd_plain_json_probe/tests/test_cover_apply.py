from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys
import unittest
import uuid

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.cover_apply import (  # noqa: E402
    COVER_TRACK_PREFIX,
    CoverConfig,
    _transform_frame,
    apply_cover_timeline_offset,
    prepare_cover_assets,
    rebase_cover_material_paths,
)
from jyd_probe.render_job import _build_cover  # noqa: E402
from jyd_probe.content_replace import _replace_cover_text_fonts_in_data  # noqa: E402


class CoverApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"cover_{uuid.uuid4().hex}"
        self.temp.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_cover_offset_selects_a_different_three_four_region_without_zoom(self) -> None:
        frame = Image.new("RGBA", (18, 32))
        for y in range(32):
            for x in range(18):
                frame.putpixel((x, y), (y, 0, 0, 255))

        top = _transform_frame(frame, scale=1.0, offset_x=0.0, offset_y=1.0)
        bottom = _transform_frame(frame, scale=1.0, offset_x=0.0, offset_y=-1.0)

        self.assertEqual(top.getpixel((0, 4))[0], 0)
        self.assertEqual(bottom.getpixel((0, 4))[0], 8)

    def test_prepares_frame_with_half_transparent_black_rectangle(self) -> None:
        source = self.temp / "source.png"
        Image.new("RGB", (108, 192), (220, 80, 40)).save(source)
        data = {
            "duration": 2_000_000,
            "canvas_config": {"width": 108, "height": 192},
            "materials": {
                "videos": [{"id": "video-1", "path": str(source), "type": "photo"}],
            },
            "tracks": [
                {
                    "type": "video",
                    "segments": [
                        {
                            "material_id": "video-1",
                            "target_timerange": {"start": 0, "duration": 2_000_000},
                            "source_timerange": {"start": 0, "duration": 2_000_000},
                        }
                    ],
                }
            ],
        }
        config = CoverConfig(frame_time_us=500_000, frame_source="timeline", fps=60, frame_count=3)

        prepared = prepare_cover_assets(data, config, self.temp)

        self.assertEqual(prepared.duration_us, 50_000)
        self.assertTrue(prepared.frame_path.is_file())
        self.assertFalse((self.temp / "cover_assets" / "__jyd_cover_overlay.png").exists())
        with Image.open(prepared.frame_path) as frame:
            self.assertEqual(frame.size, (108, 192))
            upper = frame.getpixel((54, 20))
            rectangle = frame.getpixel((54, 120))
            self.assertGreater(upper[0], 200)
            self.assertAlmostEqual(rectangle[0], 110, delta=8)
            self.assertAlmostEqual(rectangle[1], 40, delta=8)

    def test_input_image_is_center_cropped_to_canvas_before_cover_overlay(self) -> None:
        source = self.temp / "portrait-source.png"
        Image.new("RGB", (200, 100), (200, 120, 40)).save(source)
        data = {"canvas_config": {"width": 108, "height": 192}}
        config = CoverConfig(
            frame_time_us=0,
            frame_source="input_image",
            image_path=str(source),
            overlay_y_ratio=0.609375,
            overlay_width_ratio=1.0,
            overlay_height_ratio=0.36,
        )

        prepared = prepare_cover_assets(data, config, self.temp)

        with Image.open(prepared.frame_path) as frame:
            self.assertEqual(frame.size, (108, 192))
            self.assertGreater(frame.getpixel((54, 30))[0], 180)
            self.assertAlmostEqual(frame.getpixel((54, 120))[0], 100, delta=10)

    def test_shifts_original_tracks_but_not_cover_tracks(self) -> None:
        data = {
            "duration": 2_000_000,
            "tracks": [
                {
                    "name": "main-video",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 2_000_000}}],
                },
                {
                    "name": "subtitles",
                    "type": "text",
                    "segments": [{"target_timerange": {"start": 250_000, "duration": 400_000}}],
                },
                {
                    "name": f"{COVER_TRACK_PREFIX}frame",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 50_000}}],
                },
            ],
        }
        config = CoverConfig(frame_time_us=0, fps=60, frame_count=3)

        changed = apply_cover_timeline_offset(data, config)

        self.assertEqual(changed, 5)
        self.assertEqual(data["duration"], 2_050_000)
        self.assertEqual(
            [
                segment["target_timerange"]["start"]
                for segment in data["tracks"][0]["segments"]
            ],
            [0, 50_000],
        )
        self.assertEqual(data["tracks"][1]["segments"][0]["target_timerange"]["start"], 300_000)
        self.assertEqual(len(data["tracks"]), 2)

    def test_cover_precedes_video_audio_captions_effects_and_stickers(self) -> None:
        track_types = ["video", "audio", "text", "effect", "sticker"]
        data = {
            "duration": 1_000_000,
            "tracks": [
                {
                    "name": f"content-{track_type}",
                    "type": track_type,
                    "segments": [{"target_timerange": {"start": 0, "duration": 1_000_000}}],
                }
                for track_type in track_types
            ]
            + [
                {
                    "name": f"{COVER_TRACK_PREFIX}frame",
                    "type": "video",
                    "segments": [{"target_timerange": {"start": 0, "duration": 100_000}}],
                }
            ],
        }

        changed = apply_cover_timeline_offset(
            data, CoverConfig(frame_time_us=0, fps=30, frame_count=3)
        )

        self.assertEqual(changed, len(track_types) + 3)
        self.assertEqual(data["duration"], 1_100_000)
        self.assertEqual(
            [
                segment["target_timerange"]["start"]
                for segment in data["tracks"][0]["segments"]
            ],
            [0, 100_000],
        )
        self.assertTrue(
            all(
                track["segments"][0]["target_timerange"]["start"] == 100_000
                for track in data["tracks"][1:]
            )
        )
        self.assertFalse(
            any(
                str(track.get("name", "")).startswith(f"{COVER_TRACK_PREFIX}frame")
                for track in data["tracks"]
            )
        )

    def test_fixed_nameplate_starts_after_cover_frames(self) -> None:
        data = {
            "duration": 1_000_000,
            "tracks": [
                {
                    "name": "main-video",
                    "type": "video",
                    "segments": [
                        {"target_timerange": {"start": 0, "duration": 1_000_000}}
                    ],
                },
                {
                    "name": "固定人名牌",
                    "type": "sticker",
                    "segments": [
                        {"target_timerange": {"start": 0, "duration": 1_000_000}}
                    ],
                },
                {
                    "name": f"{COVER_TRACK_PREFIX}frame",
                    "type": "video",
                    "segments": [
                        {"target_timerange": {"start": 0, "duration": 100_000}}
                    ],
                },
            ],
        }

        apply_cover_timeline_offset(
            data, CoverConfig(frame_time_us=0, fps=30, frame_count=3)
        )

        nameplate = next(
            track for track in data["tracks"] if track.get("name") == "固定人名牌"
        )["segments"][0]["target_timerange"]
        self.assertEqual(nameplate, {"start": 100_000, "duration": 1_000_000})

    def test_builds_three_frame_cover_from_job_config(self) -> None:
        cover = _build_cover(
            {
                "cover": {
                    "enabled": True,
                    "frame_time_seconds": 1.25,
                    "text_line_1": "第一行",
                    "text_line_2": "第二行",
                }
            },
            {"fps": 60},
        )

        self.assertIsNotNone(cover)
        assert cover is not None
        self.assertEqual(cover.frame_time_us, 1_250_000)
        self.assertEqual(cover.frame_source, "preview_material")
        self.assertEqual(cover.duration_us, 50_000)
        self.assertEqual(cover.text_line_1, "第一行")
        self.assertEqual(cover.text_line_2, "第二行")
        self.assertEqual(cover.line_1_size, 30.0)
        self.assertEqual(cover.line_2_size, 22.0)
        self.assertEqual(cover.line_1_y, -160 / 1920)
        self.assertEqual(cover.line_2_y, -655 / 1920)
        self.assertEqual(cover.line_1_color, "#FADF4A")
        self.assertEqual(cover.line_2_color, "#F5F6F0")
        self.assertFalse(cover.auto_wrapping)
        self.assertEqual(cover.max_line_width, 0.86)

    def test_builds_editable_cover_layout(self) -> None:
        cover = _build_cover(
            {
                "cover": {
                    "enabled": True,
                    "frame_scale": 1.25,
                    "frame_offset_y": 0.2,
                    "overlay_x_ratio": 0.45,
                    "overlay_y_ratio": 0.7,
                    "overlay_width_ratio": 0.8,
                    "overlay_height_ratio": 0.3,
                    "line_1_x": -0.1,
                    "line_1_y": -0.2,
                    "line_1_size": 16,
                    "line_1_color": "#12ABEF",
                }
            },
            {"fps": 30},
        )

        self.assertIsNotNone(cover)
        assert cover is not None
        self.assertEqual(cover.frame_scale, 1.25)
        self.assertEqual(cover.frame_offset_y, 0.2)
        self.assertEqual(cover.overlay_width_ratio, 0.8)
        self.assertEqual(cover.line_1_x, -0.1)
        self.assertEqual(cover.line_1_size, 16)
        self.assertEqual(cover.line_1_color, "#12ABEF")

    def test_cover_uses_selected_caption_font(self) -> None:
        cover = _build_cover(
            {
                "cover": {"enabled": True},
                "existing_text_font": {
                    "font_id": "font-resource-id",
                    "font_path": r"D:\fonts\selected.ttf",
                    "font_title": "选中字体",
                },
            },
            {"fps": 30},
        )

        self.assertIsNotNone(cover)
        assert cover is not None
        self.assertEqual(cover.font_id, "font-resource-id")
        self.assertEqual(cover.font_path, r"D:\fonts\selected.ttf")
        self.assertEqual(cover.font_title, "选中字体")

    def test_cover_font_override_precedes_caption_font(self) -> None:
        cover = _build_cover(
            {
                "cover": {
                    "enabled": True,
                    "font": {
                        "font_id": "cover-font-id",
                        "font_path": r"D:\fonts\cover.ttf",
                        "font_title": "封面字体",
                    },
                },
                "existing_text_font": {
                    "font_id": "caption-font-id",
                    "font_path": r"D:\fonts\caption.ttf",
                    "font_title": "字幕字体",
                },
            },
            {"fps": 30},
        )

        self.assertIsNotNone(cover)
        assert cover is not None
        self.assertEqual(cover.font_id, "cover-font-id")
        self.assertEqual(cover.font_path, r"D:\fonts\cover.ttf")
        self.assertEqual(cover.font_title, "封面字体")

    def test_applies_selected_font_only_to_cover_text_tracks(self) -> None:
        data = {
            "materials": {
                "texts": [
                    {
                        "id": "cover-text",
                        "content": json.dumps({"text": "封面", "styles": [{"range": [0, 2]}]}),
                    },
                    {
                        "id": "ordinary-text",
                        "content": json.dumps({"text": "字幕", "styles": [{"range": [0, 2]}]}),
                    },
                ]
            },
            "tracks": [
                {
                    "name": f"{COVER_TRACK_PREFIX}text_1",
                    "type": "text",
                    "segments": [{"material_id": "cover-text"}],
                },
                {
                    "name": "subtitles",
                    "type": "text",
                    "segments": [{"material_id": "ordinary-text"}],
                },
            ],
        }
        config = CoverConfig(
            frame_time_us=0,
            font_id="font-resource-id",
            font_path=r"D:\fonts\selected.ttf",
            font_title="选中字体",
        )

        changed = _replace_cover_text_fonts_in_data(object(), data, config)

        self.assertGreater(changed, 0)
        cover_material, ordinary_material = data["materials"]["texts"]
        cover_content = json.loads(cover_material["content"])
        ordinary_content = json.loads(ordinary_material["content"])
        self.assertEqual(cover_content["styles"][0]["font"]["id"], "font-resource-id")
        self.assertNotIn("font", ordinary_content["styles"][0])
        self.assertEqual(cover_material["alignment"], 1)
        self.assertEqual(cover_material["letter_spacing"], 0.0)
        self.assertEqual(cover_material["line_spacing"], 0.06)
        self.assertTrue(cover_material["has_shadow"])
        self.assertEqual(cover_material["shadow_alpha"], 0.9)
        self.assertEqual(cover_material["shadow_smoothing"], 0.15)
        self.assertEqual(cover_material["shadow_distance"], 5.0)
        self.assertEqual(cover_material["shadow_angle"], -45.0)
        self.assertAlmostEqual(cover_material["shadow_point"]["x"], 0.636396103, places=6)
        self.assertAlmostEqual(cover_material["shadow_point"]["y"], -0.636396103, places=6)
        cover_shadow = cover_content["styles"][0]["shadows"][0]
        self.assertEqual(cover_shadow["alpha"], 0.9)
        self.assertEqual(cover_shadow["distance"], 5.0)
        self.assertEqual(cover_shadow["angle"], -45.0)
        self.assertEqual(cover_shadow["content"]["solid"]["color"], [0.0, 0.0, 0.0])

    def test_keeps_generated_cover_material_on_real_absolute_path(self) -> None:
        data = {
            "materials": {
                "videos": [
                    {
                        "material_name": "existing.jpg",
                        "path": "##_draftpath_placeholder_EXISTING_##/existing.jpg",
                    },
                    {
                        "material_name": "__jyd_cover_frame.jpg",
                        "path": r"D:\中文目录\cover_assets\__jyd_cover_frame.jpg",
                    },
                ]
            }
        }

        changed = rebase_cover_material_paths(data, self.temp)

        self.assertEqual(changed, 1)
        videos = data["materials"]["videos"]
        self.assertEqual(
            videos[1]["path"],
            (self.temp.resolve() / "cover_assets" / "__jyd_cover_frame.jpg").as_posix(),
        )


if __name__ == "__main__":
    unittest.main()
