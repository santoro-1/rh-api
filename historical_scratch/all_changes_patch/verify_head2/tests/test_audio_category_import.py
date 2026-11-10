from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.library.export_audio_library import category_from_draft_name  # noqa: E402
from jyd_probe.audio_catalog import AudioCatalog, CombinedAudioCatalog  # noqa: E402


class AudioCategoryImportTest(unittest.TestCase):
    def setUp(self) -> None:
        test_temp_root = PROJECT_ROOT / "runtime" / "test_tmp"
        test_temp_root.mkdir(exist_ok=True)
        self.temp = test_temp_root / f"audio_category_{uuid.uuid4().hex}"
        (self.temp / "manifest").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.temp, True)
        manifest = {
            "schema": "jyd_probe.audio_library_manifest.v1",
            "assets": [
                {"identity": "music_id:1", "file": "files/one.mp3"},
                {"identity": "music_id:2", "file": "files/two.mp3"},
            ],
        }
        (self.temp / "manifest" / "audio_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_reads_category_from_draft_name(self) -> None:
        self.assertEqual(category_from_draft_name("音乐采集_轻松"), "轻松")
        self.assertEqual(category_from_draft_name("音效采集-转场"), "转场")
        with self.assertRaises(ValueError):
            category_from_draft_name("音乐采集01")

    def test_creates_reuses_and_adds_category_without_overwriting(self) -> None:
        catalog = AudioCatalog(self.temp)
        first = catalog.assign_many_to_category(["music_id:1", "music_id:2"], "轻松")
        second = catalog.assign_many_to_category(["music_id:1"], "轻松")
        third = catalog.assign_many_to_category(["music_id:1"], "口播")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["changed_count"], 0)
        self.assertTrue(third["created"])

        snapshot = catalog.snapshot()
        self.assertEqual(len([item for item in snapshot["categories"] if not item.get("system")]), 2)
        music_one = next(item for item in snapshot["assets"] if item["identity"] == "music_id:1")
        self.assertEqual(len(music_one["category_ids"]), 2)

    def test_combines_public_and_initially_empty_personal_audio_libraries(self) -> None:
        public_files = self.temp / "files"
        public_files.mkdir()
        (public_files / "one.mp3").write_bytes(b"one")
        (public_files / "two.mp3").write_bytes(b"two")
        personal = self.temp.parent / f"personal_audio_{uuid.uuid4().hex}"
        (personal / "manifest").mkdir(parents=True)
        (personal / "files").mkdir()
        self.addCleanup(shutil.rmtree, personal, True)
        (personal / "files" / "mine.mp3").write_bytes(b"mine")
        (personal / "manifest" / "audio_manifest.json").write_text(
            json.dumps(
                {
                    "schema": "jyd_probe.audio_library_manifest.v1",
                    "assets": [{"identity": "music_id:mine", "file": "files/mine.mp3"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        snapshot = CombinedAudioCatalog([self.temp, personal]).snapshot()

        self.assertEqual(snapshot["asset_count"], 3)
        mine = next(item for item in snapshot["assets"] if item["identity"] == "music_id:mine")
        self.assertEqual(mine["library_scope"], "personal")


if __name__ == "__main__":
    unittest.main()
