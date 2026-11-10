from __future__ import annotations

import json
import sqlite3
import sys


connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    """
    SELECT p.project_no, p.name, i.item_id, i.script_text,
           i.visual_analysis_json, i.subtitles_json
    FROM project_items AS i
    JOIN projects AS p ON p.project_id = i.project_id
    WHERE i.position = 15
    ORDER BY p.updated_at DESC
    """
).fetchall()
for row in rows:
    visual = json.loads(row["visual_analysis_json"] or "{}")
    subtitles = json.loads(row["subtitles_json"] or "{}")
    print(
        json.dumps(
            {
                "project_no": row["project_no"],
                "name": row["name"],
                "item_id": row["item_id"],
                "script_preview": row["script_text"][:100],
                "analysis_status": visual.get("analysis_status"),
                "mapping_status": visual.get("mapping_status"),
                "visual_plan": visual.get("visual_plan", []),
                "mapped_candidates": visual.get("mapped_candidates", []),
                "overlays": (visual.get("recipe") or {}).get("overlays", []),
                "has_asr_alignment": isinstance(subtitles.get("asr_alignment"), dict),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
