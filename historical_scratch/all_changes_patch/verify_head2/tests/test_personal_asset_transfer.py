from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.web_api import _import_personal_asset_package  # noqa: E402


class PersonalAssetTransferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT_ROOT / "runtime" / "test_tmp" / f"personal_assets_{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_imports_supported_personal_library_directories(self) -> None:
        package = self.root / "assets.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("audio_library/manifest/audio_manifest.json", '{"assets": []}')
            archive.writestr("corner_sticker_library/bundles/demo/sticker.json", "{}")

        result = _import_personal_asset_package(package, self.root / "personal")

        self.assertEqual(
            result["imported_libraries"],
            ["audio_library", "corner_sticker_library"],
        )
        self.assertTrue(
            (self.root / "personal" / "corner_sticker_library" / "bundles" / "demo" / "sticker.json").is_file()
        )

    def test_rejects_package_path_traversal(self) -> None:
        package = self.root / "unsafe.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../outside.txt", "unsafe")

        with self.assertRaisesRegex(ValueError, "不安全路径"):
            _import_personal_asset_package(package, self.root / "personal")


if __name__ == "__main__":
    unittest.main()
