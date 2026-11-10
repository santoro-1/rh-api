from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.font_export import (  # noqa: E402
    FONT_MANIFEST_SCHEMA,
    _font_display_name,
    export_font_library,
    refresh_font_library_metadata,
)


class FontExportTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"font_export_{uuid.uuid4().hex}"
        self.temp.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_copies_font_once_and_reuses_manifest_record(self) -> None:
        font_path = self.temp / "ExampleFont.otf"
        font_path.write_bytes(b"font-data")
        missing_path = self.temp / "MissingFont.ttf"
        content = json.dumps(
            {
                "text": "测试字体",
                "styles": [
                    {
                        "font": {
                            "path": str(font_path),
                            "id": "7001",
                            "resource_id": "7001",
                        }
                    },
                    {"font": {"path": str(missing_path), "id": "7002"}},
                ],
            },
            ensure_ascii=False,
        )
        draft = {
            "duration": 2_000_000,
            "tracks": [
                {
                    "id": "text-track",
                    "type": "text",
                    "segments": [
                        {
                            "id": "text-segment",
                            "material_id": "text-material",
                            "target_timerange": {"start": 0, "duration": 2_000_000},
                        }
                    ],
                }
            ],
            "materials": {"texts": [{"id": "text-material", "content": content}]},
        }
        output = self.temp / "font_library"
        draft_dir = self.temp / "draft"
        draft_dir.mkdir()

        first = export_font_library(
            draft,
            output,
            source_draft_dir=draft_dir,
            source_label="测试草稿",
        )
        self.assertEqual(first.encountered_count, 2)
        self.assertEqual(first.copied_count, 1)
        self.assertEqual(first.missing_count, 1)
        self.assertEqual(len(first.fonts), 1)
        copied_path = Path(first.fonts[0]["absolute_path"])
        self.assertTrue(copied_path.is_file())
        self.assertEqual(copied_path.read_bytes(), b"font-data")

        second = export_font_library(
            draft,
            output,
            source_draft_dir=draft_dir,
            source_label="测试草稿",
        )
        self.assertEqual(second.copied_count, 0)
        self.assertEqual(second.existing_count, 1)
        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], FONT_MANIFEST_SCHEMA)
        self.assertEqual(len(manifest["fonts"]), 1)

    def test_reads_full_name_from_font_name_table(self) -> None:
        class FakeRecord:
            nameID = 4

            @staticmethod
            def toUnicode() -> str:
                return "HelloFont BangKeTi"

        class FakeFont(dict):
            def __init__(self) -> None:
                super().__init__({"name": type("NameTable", (), {"names": [FakeRecord()]})()})

            def close(self) -> None:
                pass

        with patch("jyd_probe.font_export.TTFont", return_value=FakeFont()):
            self.assertEqual(_font_display_name(self.temp / "font.ttf"), "HelloFont BangKeTi")

    def test_refreshes_only_generic_names(self) -> None:
        library = self.temp / "font_library"
        (library / "manifest").mkdir(parents=True)
        (library / "metadata").mkdir()
        (library / "files").mkdir()
        (library / "files" / "generic.ttf").write_bytes(b"generic")
        (library / "files" / "friendly.ttf").write_bytes(b"friendly")
        fonts = [
            {
                "identity": "resource_id:1",
                "name": "font",
                "file": "files/generic.ttf",
                "metadata_file": "metadata/generic.json",
            },
            {
                "identity": "resource_id:2",
                "name": "优设标题黑",
                "file": "files/friendly.ttf",
                "metadata_file": "metadata/friendly.json",
            },
        ]
        (library / "manifest" / "font_manifest.json").write_text(
            json.dumps({"schema": FONT_MANIFEST_SCHEMA, "fonts": fonts}, ensure_ascii=False),
            encoding="utf-8",
        )

        with patch("jyd_probe.font_export._font_display_name", return_value="Internal Font Name"):
            result = refresh_font_library_metadata(library)

        manifest = json.loads((library / "manifest" / "font_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(manifest["fonts"][0]["name"], "Internal Font Name")
        self.assertEqual(manifest["fonts"][1]["name"], "优设标题黑")


if __name__ == "__main__":
    unittest.main()
