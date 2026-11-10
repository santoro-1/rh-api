from __future__ import annotations

import json
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")
stage = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819")
library = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")

pairs = [
    ("catalog", stage / "original_catalog.json", library / "catalog.json"),
    (
        "quarantine",
        stage / "original_platform_ui_pending.json",
        library / "quarantine" / "platform_ui_pending.json",
    ),
]
for name, before_path, current_path in pairs:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    current = json.loads(current_path.read_text(encoding="utf-8"))
    print(f"{name}: semantic_equal={before == current}")
    if before != current and name == "catalog":
        before_by_id = {row["asset_id"]: row for row in before["assets"]}
        current_by_id = {row["asset_id"]: row for row in current["assets"]}
        print(f"  counts={len(before_by_id)}->{len(current_by_id)}")
        print(f"  added={sorted(current_by_id.keys() - before_by_id.keys())[:20]}")
        print(f"  removed={sorted(before_by_id.keys() - current_by_id.keys())[:20]}")
        changed = [key for key in before_by_id.keys() & current_by_id.keys() if before_by_id[key] != current_by_id[key]]
        print(f"  changed_count={len(changed)} changed={sorted(changed)[:20]}")
    elif before != current:
        before_ids = {row["asset_id"] for row in before["assets"]}
        current_ids = {row["asset_id"] for row in current["assets"]}
        print(f"  counts={len(before_ids)}->{len(current_ids)}")
        print(f"  added={sorted(current_ids - before_ids)}")
        print(f"  removed={sorted(before_ids - current_ids)}")
