from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


db_path = Path(sys.argv[1])
connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row

tables = [
    row["name"]
    for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'project_%' ORDER BY name"
    )
]
print(json.dumps({"tables": tables}, ensure_ascii=False, indent=2))
for table in tables:
    columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]
    print(json.dumps({"table": table, "columns": columns}, ensure_ascii=False))

projects = [
    dict(row)
    for row in connection.execute(
        """
        SELECT p.project_id, p.project_no, p.name, p.updated_at, COUNT(i.item_id) AS item_count
        FROM projects AS p
        LEFT JOIN project_items AS i ON i.project_id = p.project_id
        GROUP BY p.project_id
        ORDER BY p.updated_at DESC
        LIMIT 20
        """
    )
]
print(json.dumps({"projects": projects}, ensure_ascii=False, indent=2))
for project in projects:
    if int(project["item_count"] or 0) < 15:
        continue
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT item_id, row_key, position, script_text, status,
                   content_analysis_json, visual_analysis_json, subtitles_json, settings_json
            FROM project_items
            WHERE project_id = ? AND position = 15
            """,
            (project["project_id"],),
        )
    ]
    print(json.dumps({"project": project, "position_15": rows}, ensure_ascii=False, indent=2))
