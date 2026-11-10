from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.content_replace import _apply_font_to_text_material, _remove_replaced_materials  # noqa: E402
from jyd_probe.render_job import (  # noqa: E402
    _build_existing_text_font_replacements,
    _build_existing_text_style_replacements,
)


class MotherDraftRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"mother_render_{uuid.uuid4().hex}"
        self.temp.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_removes_old_audio_and_effect_tracks_before_new_assets_are_added(self) -> None:
        data = {
            "tracks": [
                {"type": "video", "segments": []},
                {"type": "audio", "segments": [{"material_id": "old-audio"}]},
                {"type": "effect", "segments": [{"material_id": "old-effect"}]},
                {"type": "text", "segments": []},
            ],
            "materials": {
                "audios": [{"id": "old-audio"}],
                "audio_effects": [{"id": "old-audio-effect"}],
                "video_effects": [{"id": "old-effect"}],
                "effects": [{"id": "old-generic-effect"}],
                "videos": [{"id": "video"}],
            },
        }
        changed = _remove_replaced_materials(data, remove_audio=True, remove_effects=True)
        self.assertGreater(changed, 0)
        self.assertEqual([track["type"] for track in data["tracks"]], ["video", "text"])
        self.assertEqual(data["materials"]["audios"], [])
        self.assertEqual(data["materials"]["audio_effects"], [])
        self.assertEqual(data["materials"]["video_effects"], [])
        self.assertEqual(data["materials"]["effects"], [])
        self.assertEqual(len(data["materials"]["videos"]), 1)

    def test_builds_style_replacements_for_every_regular_text_segment(self) -> None:
        style = self.temp / "style.json"
        style.write_text("{}", encoding="utf-8")
        draft = {
            "tracks": [
                {
                    "type": "text",
                    "segments": [
                        {"material_id": "text-1"},
                        {"material_id": "text-template"},
                        {"material_id": "text-2"},
                    ],
                },
                {"type": "video", "segments": []},
                {"type": "text", "segments": [{"material_id": "text-3"}]},
            ],
            "materials": {
                "texts": [{"id": "text-1"}, {"id": "text-2"}, {"id": "text-3"}],
                "text_templates": [{"id": "text-template"}],
            },
        }
        replacements = _build_existing_text_style_replacements(
            {"existing_text_style": {"style_json_path": str(style), "apply_clip": True}},
            draft,
        )
        self.assertEqual(
            [(item.track_index, item.segment_index) for item in replacements],
            [(0, 0), (0, 2), (1, 0)],
        )
        self.assertTrue(all(str(item.style_json_path) == str(style) for item in replacements))

    def test_builds_font_replacements_for_regular_text_only(self) -> None:
        font = self.temp / "font.ttf"
        font.write_bytes(b"font")
        draft = {
            "tracks": [
                {
                    "type": "text",
                    "segments": [
                        {"material_id": "text-1"},
                        {"material_id": "complex-template"},
                        {"material_id": "text-2"},
                    ],
                },
                {"type": "text", "segments": [{"material_id": "text-3"}]},
            ],
            "materials": {
                "texts": [{"id": "text-1"}, {"id": "text-2"}, {"id": "text-3"}],
                "text_templates": [{"id": "complex-template"}],
            },
        }
        replacements = _build_existing_text_font_replacements(
            {
                "existing_text_font": {
                    "font_id": "font-resource-1",
                    "font_path": str(font),
                    "font_title": "测试字体",
                }
            },
            draft,
        )
        self.assertEqual(
            [(item.track_index, item.segment_index) for item in replacements],
            [(0, 0), (0, 2), (1, 0)],
        )
        self.assertTrue(all(item.font_id == "font-resource-1" for item in replacements))
        self.assertTrue(all(item.font_title == "测试字体" for item in replacements))

    def test_font_replacement_preserves_non_font_text_style(self) -> None:
        original_style = {
            "range": [0, 4],
            "size": 9.5,
            "fill": {"content": {"solid": {"color": [1, 0.5, 0]}}},
            "stroke": {"width": 0.08},
            "font": {"id": "old", "path": "old.ttf"},
        }
        material = {
            "id": "text-1",
            "content": json.dumps({"text": "测试文字", "styles": [original_style]}, ensure_ascii=False),
            "font_path": "old.ttf",
            "font_resource_id": "old",
            "fonts": [{"id": "font-entry", "category_id": "category", "title": "旧字体"}],
            "line_max_width": 0.75,
        }

        _apply_font_to_text_material(
            material,
            {"id": "new-resource", "path": "new.ttf"},
            "新字体",
        )

        content = json.loads(material["content"])
        style = content["styles"][0]
        self.assertEqual(style["font"], {"id": "new-resource", "path": "new.ttf"})
        self.assertEqual(style["size"], 9.5)
        self.assertEqual(style["fill"], original_style["fill"])
        self.assertEqual(style["stroke"], original_style["stroke"])
        self.assertEqual(material["line_max_width"], 0.75)
        self.assertEqual(material["font_resource_id"], "new-resource")
        self.assertEqual(material["font_path"], "new.ttf")
        self.assertEqual(material["fonts"][0]["title"], "新字体")
        self.assertEqual(material["fonts"][0]["category_id"], "category")


if __name__ == "__main__":
    unittest.main()
