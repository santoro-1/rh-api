import json
import sqlite3
from collections import Counter
from pathlib import Path


DB_PATH = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db")
PROJECT_ID = "fb690817a6a04995a907d51b8f2ed8a7"


connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT row_key, status, updated_at FROM project_items "
    "WHERE project_id = ? AND CAST(row_key AS INTEGER) BETWEEN 11 AND 30 "
    "ORDER BY CAST(row_key AS INTEGER)",
    (PROJECT_ID,),
).fetchall()
print(json.dumps({
    "counts": dict(Counter(row["status"] for row in rows)),
    "active_rows": [
        row["row_key"]
        for row in rows
        if row["status"] in {"DIGITAL_HUMAN_RUNNING", "VIDEO_ENHANCING"}
    ],
    "latest_updated_at": max((row["updated_at"] for row in rows), default=None),
}, ensure_ascii=False))
