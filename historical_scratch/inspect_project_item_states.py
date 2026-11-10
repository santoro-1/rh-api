from __future__ import annotations

import json
import sqlite3
from pathlib import Path

db = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db")
con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
project_id = con.execute(
    "SELECT project_id FROM projects WHERE project_no = 'DH-20260810-0001'"
).fetchone()[0]
rows = []
for item in con.execute(
    """
    SELECT item_id, position, status, current_image_asset_id, current_audio_asset_id,
           current_base_video_asset_id, current_video_asset_id, updated_at
    FROM project_items WHERE project_id = ? AND position = 7 ORDER BY position
    """,
    (project_id,),
):
    assets = {
        row["asset_id"]: dict(row)
        for row in con.execute(
            "SELECT asset_id, asset_type, version, status, managed_path, created_at FROM project_assets WHERE item_id = ?",
            (item["item_id"],),
        )
    }
    full = con.execute("SELECT settings_json, subtitles_json FROM project_items WHERE item_id = ?", (item["item_id"],)).fetchone()
    settings = json.loads(full["settings_json"] or "{}")
    subtitles = json.loads(full["subtitles_json"] or "{}")
    rows.append(dict(item) | {
        "current_base": assets.get(item["current_base_video_asset_id"]),
        "current_video": assets.get(item["current_video_asset_id"]),
        "composition_invalidated_reason": settings.get("composition_invalidated_reason"),
        "subtitle_status": subtitles.get("status"),
        "bound_video_asset_id": subtitles.get("bound_video_asset_id"),
    })
print(json.dumps(rows, ensure_ascii=False, indent=2))
