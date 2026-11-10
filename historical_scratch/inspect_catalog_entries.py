from __future__ import annotations

import json
import sys
from pathlib import Path


CATALOG = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\catalog.json")
TARGETS = {
    "review.u0061.video.40976d2d4c",
    "review.u0317.video.e79db70509",
    "broll.20260814.001.video.0acd36aeb5",
}

payload = json.loads(CATALOG.read_text(encoding="utf-8"))
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps({key: type(payload[key]).__name__ for key in payload}, ensure_ascii=False, indent=2))
assets = payload.get("assets") or payload.get("items") or payload.get("library")
for asset in assets:
    if asset.get("asset_id") in TARGETS:
        print(json.dumps(asset, ensure_ascii=False, indent=2))
