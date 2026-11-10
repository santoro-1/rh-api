from __future__ import annotations

from pathlib import Path
import shutil
import sys
import unittest
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.asset_admin import AssetAdminCatalog  # noqa: E402


class AssetAdminCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        root = PROJECT_ROOT / "runtime" / "test_tmp"
        root.mkdir(exist_ok=True)
        self.temp = root / f"asset_admin_{uuid.uuid4().hex}"
        self.temp.mkdir()
        self.catalog = AssetAdminCatalog(self.temp / "asset_admin.json")
        self.items = [{"identity": "resource_id:1", "name": "原名称", "path": "asset.json"}]

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_rename_classify_disable_and_restore_without_changing_source(self) -> None:
        self.catalog.update(
            "effect",
            "resource_id:1",
            name="新名称",
            category="氛围",
            enabled=False,
        )
        decorated = self.catalog.decorate("effect", self.items)
        self.assertEqual(decorated[0]["name"], "新名称")
        self.assertEqual(decorated[0]["original_name"], "原名称")
        self.assertEqual(decorated[0]["category"], "氛围")
        self.assertFalse(decorated[0]["enabled"])
        self.assertEqual(self.items[0]["name"], "原名称")

        self.catalog.move_to_trash("effect", "resource_id:1")
        self.assertEqual(self.catalog.decorate("effect", self.items), [])
        trashed = self.catalog.decorate("effect", self.items, include_deleted=True)
        self.assertTrue(trashed[0]["deleted"])

        self.catalog.restore("effect", "resource_id:1")
        restored = self.catalog.decorate("effect", self.items)
        self.assertFalse(restored[0]["deleted"])
        self.assertTrue(restored[0]["enabled"])

        self.catalog.move_to_trash("effect", "resource_id:1")
        deleted_records = self.catalog.deleted_records()
        self.assertEqual(deleted_records[0]["identity"], "resource_id:1")
        self.catalog.mark_purged("effect", "resource_id:1")
        self.assertEqual(self.catalog.decorate("effect", self.items, include_deleted=True), [])

    def test_corner_sticker_is_a_supported_independent_asset_kind(self) -> None:
        decorated = self.catalog.decorate("corner_sticker", self.items)

        self.assertEqual(decorated[0]["kind"], "corner_sticker")
        self.assertEqual(decorated[0]["identity"], "resource_id:1")


if __name__ == "__main__":
    unittest.main()
