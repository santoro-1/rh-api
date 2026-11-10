from __future__ import annotations

import json
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\src")

from jyd_probe.semantic_visuals import load_semantic_visual_catalog


ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")
STAGE = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819")

manifest = json.loads((STAGE / "manifest.json").read_text(encoding="utf-8"))
quarantine = json.loads(
    (ROOT / "quarantine" / "platform_ui_pending.json").read_text(encoding="utf-8")
)
catalog = load_semantic_visual_catalog(ROOT)

assert len(catalog.assets) == 1454
assert len(quarantine["assets"]) == 45
for row in manifest:
    new_asset = catalog.asset(row["new_asset_id"])
    old_asset = catalog.asset(row["source_asset_id"])
    assert new_asset is not None and new_asset["auto_eligible"] is True
    assert new_asset["platform_ui_status"] == "none"
    assert Path(new_asset["resource_path"]).is_file()
    assert old_asset is not None and old_asset["auto_eligible"] is False

for asset_id in {
    "broll.20260814.001.video.0acd36aeb5",
    "broll.20260814.012.video.40af9611e5",
    "broll.20260814.017.video.ab8e7a8cd1",
    "broll.20260814.018.video.bd046fca01",
    "broll.20260814.021.video.fd64795cf2",
    "broll.20260814.026.video.e74d4bb115",
    "broll.20260814.029.video.68731f13cd",
    "broll.20260814.031.video.80eb4148e8",
}:
    asset = catalog.asset(asset_id)
    assert asset is not None and asset["auto_eligible"] is True
    assert asset["platform_ui_status"] == "none"

print("installed catalog verification passed")
print("new_clean_assets=45 old_quarantined_assets=45 restored_original_assets=8")
