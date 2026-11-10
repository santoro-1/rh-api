from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.text_effect_export import export_text_effect_library  # noqa: E402


class TextEffectExportTest(unittest.TestCase):
    def test_exports_standalone_flower_text_and_skips_template(self) -> None:
        test_temp_root = PROJECT_ROOT / "runtime" / "test_tmp"
        test_temp_root.mkdir(exist_ok=True)
        temp = test_temp_root / f"text_effect_{uuid.uuid4().hex}"
        temp.mkdir()
        self.addCleanup(shutil.rmtree, temp, True)
        with self.subTest(source="synthetic-draft"):
            resource = temp / "jianying-cache" / "flower"
            (resource / "texture").mkdir(parents=True)
            (resource / "config.json").write_text("{}", encoding="utf-8")
            (resource / "texture" / "sample.png").write_bytes(b"png")

            effect_material = {
                "id": "effect-material-id",
                "name": "测试花字",
                "type": "text_effect",
                "effect_id": "effect-100",
                "resource_id": "resource-100",
                "path": str(resource),
            }
            content = {
                "text": "默认文本",
                "styles": [
                    {
                        "range": [0, 4],
                        "effectStyle": {"id": "resource-100", "path": str(resource)},
                    }
                ],
            }
            data = {
                "materials": {
                    "texts": [
                        {"id": "text-id", "type": "text", "content": json.dumps(content, ensure_ascii=False)},
                        {"id": "template-child", "type": "text", "content": json.dumps(content)},
                    ],
                    "text_templates": [{"id": "template-id"}],
                    "effects": [effect_material, dict(effect_material)],
                },
                "tracks": [
                    {
                        "type": "text",
                        "segments": [
                            {
                                "id": "standalone-segment",
                                "material_id": "text-id",
                                "extra_material_refs": ["effect-material-id", "effect-material-id"],
                                "target_timerange": {"start": 0, "duration": 3_000_000},
                            },
                            {
                                "id": "template-segment",
                                "material_id": "template-id",
                                "extra_material_refs": ["effect-material-id"],
                                "target_timerange": {"start": 3_000_000, "duration": 3_000_000},
                            },
                        ],
                    }
                ],
            }

            output = temp / "library"
            result = export_text_effect_library(data, output, source_label="test-draft")

            self.assertEqual(result.scanned_text_segment_count, 2)
            self.assertEqual(result.flower_text_segment_count, 1)
            self.assertEqual(result.encountered_effect_count, 1)
            self.assertEqual(result.exported_count, 1)
            self.assertEqual(result.missing_resource_count, 0)
            self.assertGreater(result.duplicate_reference_count, 0)

            record = result.effects[0]
            bundle = output / record["bundle"]
            self.assertTrue((bundle / "text_effect.json").is_file())
            self.assertTrue((bundle / "resources" / "effect" / "config.json").is_file())
            self.assertTrue((bundle / "resources" / "effect" / "texture" / "sample.png").is_file())

            metadata = json.loads((bundle / "text_effect.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema"], "jyd_probe.text_effect.v1")
            self.assertEqual(metadata["name"], "测试花字")
            self.assertEqual(len(metadata["sources"]), 1)

            second = export_text_effect_library(data, output, source_label="test-draft")
            self.assertEqual(second.exported_count, 0)
            self.assertEqual(second.existing_count, 1)
            self.assertEqual(len(second.effects), 1)


if __name__ == "__main__":
    unittest.main()
