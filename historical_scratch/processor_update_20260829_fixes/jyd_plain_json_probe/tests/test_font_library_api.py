from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import _list_font_library  # noqa: E402


class FontLibraryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"font_library_{uuid.uuid4().hex}"
        (self.temp / "manifest").mkdir(parents=True)
        (self.temp / "files").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_lists_only_complete_font_entries_as_available(self) -> None:
        (self.temp / "files" / "usable.ttf").write_bytes(b"font")
        manifest = {
            "schema": "jyd_probe.font_library_manifest.v1",
            "fonts": [
                {
                    "identity": "resource_id:1",
                    "name": "可用字体",
                    "resource_id": "1",
                    "file": "files/usable.ttf",
                    "size_bytes": 4,
                },
                {
                    "identity": "sha256:no-id",
                    "name": "系统字体",
                    "resource_id": "",
                    "file": "files/usable.ttf",
                },
                {
                    "identity": "resource_id:missing",
                    "name": "缺文件字体",
                    "resource_id": "missing",
                    "file": "files/missing.ttf",
                },
            ],
        }
        (self.temp / "manifest" / "font_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

        fonts = _list_font_library(self.temp)

        self.assertEqual(len(fonts), 3)
        self.assertTrue(fonts[0]["available"])
        self.assertFalse(fonts[1]["available"])
        self.assertFalse(fonts[2]["available"])
        self.assertEqual(fonts[0]["resource_id"], "1")
        self.assertEqual(Path(fonts[0]["path"]), (self.temp / "files" / "usable.ttf").resolve())


if __name__ == "__main__":
    unittest.main()
