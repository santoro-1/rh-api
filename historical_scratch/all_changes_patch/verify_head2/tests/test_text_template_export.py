from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.text_template_export import export_text_template_library  # noqa: E402


class TextTemplateExportTest(unittest.TestCase):
    def test_exports_complete_template_graph_and_resources(self) -> None:
        test_root = PROJECT_ROOT / "runtime" / "test_tmp"
        test_root.mkdir(exist_ok=True)
        temp = test_root / f"text_template_{uuid.uuid4().hex}"
        temp.mkdir()
        self.addCleanup(shutil.rmtree, temp, True)

        template_resource = temp / "cache" / "template"
        flower_resource = temp / "cache" / "flower"
        font_resource = temp / "cache" / "font.ttf"
        template_resource.mkdir(parents=True)
        flower_resource.mkdir(parents=True)
        (template_resource / "template.json").write_text("{}", encoding="utf-8")
        (flower_resource / "effect.json").write_text("{}", encoding="utf-8")
        font_resource.write_bytes(b"font")

        slot_one_content = {
            "text": "第一段",
            "styles": [{"font": {"path": str(font_resource)}}],
        }
        slot_two_content = {"text": "第二段", "styles": []}
        template = {
            "id": "template-material-id",
            "type": "text_template",
            "name": "测试复合文字模板",
            "effect_id": "template-100",
            "resource_id": "template-100",
            "path": str(template_resource),
            "resources": [
                {"panel": "fonts", "resource_id": "font-100", "path": str(font_resource)},
                {"panel": "flower", "resource_id": "flower-100", "path": str(flower_resource)},
            ],
            "text_info_resources": [
                {
                    "text_material_id": "text-one",
                    "extra_material_refs": ["animation-id", "effect-id"],
                },
                {"text_material_id": "text-two", "extra_material_refs": []},
            ],
            "non_text_info_resources": [
                {"type": "sticker", "extra_material_refs": ["animation-id"]}
            ],
        }
        data = {
            "materials": {
                "text_templates": [template],
                "texts": [
                    {"id": "text-one", "type": "text", "content": json.dumps(slot_one_content)},
                    {"id": "text-two", "type": "text", "content": json.dumps(slot_two_content)},
                    {"id": "normal-text", "type": "text", "content": json.dumps({"text": "普通文字"})},
                ],
                "effects": [
                    {
                        "id": "effect-id",
                        "type": "text_effect",
                        "resource_id": "flower-100",
                        "path": str(flower_resource),
                    }
                ],
                "material_animations": [
                    {"id": "animation-id", "type": "sticker_animation"}
                ],
            },
            "tracks": [
                {
                    "type": "text",
                    "segments": [
                        {
                            "id": "template-segment",
                            "material_id": "template-material-id",
                            "extra_material_refs": ["animation-id", "effect-id"],
                            "target_timerange": {"start": 0, "duration": 5_000_000},
                        },
                        {
                            "id": "normal-segment",
                            "material_id": "normal-text",
                            "target_timerange": {"start": 5_000_000, "duration": 3_000_000},
                        },
                    ],
                }
            ],
        }

        output = temp / "library"
        result = export_text_template_library(data, output, source_label="test-draft")

        self.assertEqual(result.scanned_text_segment_count, 2)
        self.assertEqual(result.encountered_template_count, 1)
        self.assertEqual(result.text_slot_count, 2)
        self.assertEqual(result.missing_text_material_count, 0)
        self.assertEqual(result.unresolved_reference_count, 0)
        self.assertEqual(result.missing_resource_count, 0)

        record = result.templates[0]
        bundle = output / record["bundle"]
        metadata = json.loads((bundle / "text_template.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema"], "jyd_probe.text_template.v1")
        self.assertEqual([slot["text"] for slot in metadata["text_slots"]], ["第一段", "第二段"])
        self.assertEqual(len(metadata["referenced_materials"]["effects"]), 1)
        self.assertEqual(len(metadata["referenced_materials"]["material_animations"]), 1)
        self.assertEqual(len(metadata["resources"]), 3)
        for resource in metadata["resources"]:
            self.assertEqual(resource["status"], "copied")
            self.assertTrue((bundle / resource["library_path"]).exists())

        second = export_text_template_library(data, output, source_label="test-draft")
        self.assertEqual(second.exported_count, 0)
        self.assertEqual(second.existing_count, 1)
        self.assertEqual(len(second.templates), 1)


if __name__ == "__main__":
    unittest.main()
