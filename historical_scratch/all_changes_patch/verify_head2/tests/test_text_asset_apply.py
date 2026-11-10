from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.text_asset_apply import (  # noqa: E402
    add_text_template_to_data,
    apply_text_effect_to_track,
)


class TextAssetApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"text_asset_apply_{uuid.uuid4().hex}"
        self.temp.mkdir()
        self.addCleanup(shutil.rmtree, self.temp, True)

    @staticmethod
    def base_data() -> dict:
        return {
            "duration": 10_000_000,
            "materials": {
                "texts": [
                    {
                        "id": "target-text-id",
                        "type": "text",
                        "content": json.dumps(
                            {"text": "新增文字", "styles": [{"range": [0, 4], "size": 8}]},
                            ensure_ascii=False,
                        ),
                    }
                ],
                "effects": [],
                "text_templates": [],
                "material_animations": [],
            },
            "tracks": [
                {
                    "id": "target-track-id",
                    "type": "text",
                    "name": "程序新增文字_0",
                    "segments": [
                        {
                            "id": "target-segment-id",
                            "material_id": "target-text-id",
                            "render_index": 1,
                            "track_render_index": 1,
                            "target_timerange": {"start": 0, "duration": 10_000_000},
                        }
                    ],
                }
            ],
        }

    def test_applies_flower_text_with_library_resource_path(self) -> None:
        bundle = self.temp / "flower"
        resource = bundle / "resources" / "effect"
        resource.mkdir(parents=True)
        (resource / "config.json").write_text("{}", encoding="utf-8")
        metadata = bundle / "text_effect.json"
        metadata.write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.text_effect.v1",
                    "material": {
                        "id": "old-effect-material",
                        "type": "text_effect",
                        "resource_id": "flower-resource",
                        "effect_id": "flower-resource",
                        "path": "C:/old/flower",
                    },
                    "resource": {
                        "original_path": "C:/old/flower",
                        "library_path": "resources/effect",
                    },
                    "sample_text_material": {
                        "content": json.dumps(
                            {
                                "text": "默认文本",
                                "styles": [
                                    {
                                        "range": [0, 4],
                                        "effectStyle": {"id": "flower-resource", "path": "C:/old/flower"},
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        data = self.base_data()
        changed = apply_text_effect_to_track(data, metadata, "程序新增文字_0")
        self.assertEqual(changed, 1)
        effect = data["materials"]["effects"][0]
        self.assertNotEqual(effect["id"], "old-effect-material")
        self.assertEqual(Path(effect["path"]), resource.resolve())
        self.assertIn(effect["id"], data["tracks"][0]["segments"][0]["extra_material_refs"])
        content = json.loads(data["materials"]["texts"][0]["content"])
        self.assertEqual(Path(content["styles"][0]["effectStyle"]["path"]), resource.resolve())

    def test_clones_template_ids_slots_references_and_auto_duration(self) -> None:
        bundle = self.temp / "template"
        bundle.mkdir()
        metadata = bundle / "text_template.json"
        metadata.write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.text_template.v1",
                    "template": {
                        "id": "old-template",
                        "type": "text_template",
                        "name": "测试模板",
                        "text_info_resources": [
                            {
                                "text_material_id": "old-slot-one",
                                "extra_material_refs": ["old-animation", "old-effect"],
                                "attach_info": {"duration": 5_000_000},
                            },
                            {
                                "text_material_id": "old-slot-two",
                                "extra_material_refs": [],
                                "attach_info": {"duration": 5_000_000},
                            },
                        ],
                        "non_text_info_resources": [
                            {
                                "extra_material_refs": ["old-animation"],
                                "attach_info": {"duration": 5_000_000},
                            }
                        ],
                    },
                    "segment_template": {
                        "id": "old-segment",
                        "material_id": "old-template",
                        "extra_material_refs": ["old-animation", "old-effect"],
                        "render_index": 10,
                        "track_render_index": 2,
                        "target_timerange": {"start": 0, "duration": 5_000_000},
                    },
                    "text_slots": [
                        {
                            "slot_index": 0,
                            "text_material_id": "old-slot-one",
                            "text_material": {
                                "id": "old-slot-one",
                                "type": "text",
                                "content": json.dumps({"text": "一", "styles": [{"range": [0, 1]}]}),
                            },
                        },
                        {
                            "slot_index": 1,
                            "text_material_id": "old-slot-two",
                            "text_material": {
                                "id": "old-slot-two",
                                "type": "text",
                                "content": json.dumps({"text": "二", "styles": [{"range": [0, 1]}]}),
                            },
                        },
                    ],
                    "referenced_materials": {
                        "effects": [{"id": "old-effect", "type": "text_effect"}],
                        "material_animations": [{"id": "old-animation", "type": "sticker_animation"}],
                    },
                    "resources": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        data = self.base_data()
        changed = add_text_template_to_data(
            data,
            metadata,
            ["第一段新内容", ""],
            start_us=1_000_000,
            duration_us=0,
            track_name="复合模板测试",
        )
        self.assertEqual(changed, 1)
        template = data["materials"]["text_templates"][0]
        self.assertNotEqual(template["id"], "old-template")
        self.assertEqual(template["text_info_resources"][0]["attach_info"]["duration"], 9_000_000)
        self.assertEqual(template["non_text_info_resources"][0]["attach_info"]["duration"], 9_000_000)

        new_track = data["tracks"][-1]
        self.assertEqual(new_track["name"], "复合模板测试")
        segment = new_track["segments"][0]
        self.assertEqual(segment["target_timerange"], {"start": 1_000_000, "duration": 9_000_000})
        self.assertEqual(segment["material_id"], template["id"])

        text_by_id = {item["id"]: item for item in data["materials"]["texts"]}
        slot_ids = [item["text_material_id"] for item in template["text_info_resources"]]
        self.assertEqual(json.loads(text_by_id[slot_ids[0]]["content"])["text"], "第一段新内容")
        self.assertEqual(json.loads(text_by_id[slot_ids[1]]["content"])["text"], "")
        reference_ids = {
            data["materials"]["effects"][0]["id"],
            data["materials"]["material_animations"][0]["id"],
        }
        self.assertEqual(set(segment["extra_material_refs"]), reference_ids)
        self.assertTrue(reference_ids.isdisjoint({"old-effect", "old-animation"}))


if __name__ == "__main__":
    unittest.main()
