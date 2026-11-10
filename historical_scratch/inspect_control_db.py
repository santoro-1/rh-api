from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


database = Path(sys.argv[1]).resolve()
project_id = sys.argv[2]
connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    project = connection.execute(
        "SELECT project_id, project_no, owner_user_id, owner_username, status, revision "
        "FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    items = connection.execute(
        "SELECT item_id, row_key, status, script_text, subtitles_json, "
        "content_analysis_json, settings_json FROM project_items WHERE project_id=? "
        "ORDER BY position",
        (project_id,),
    ).fetchall()
    sources = connection.execute(
        "SELECT source_id, version, filename, size_bytes, sha256, managed_path, created_at "
        "FROM project_script_sources WHERE project_id=? ORDER BY version",
        (project_id,),
    ).fetchall()
    item_payloads = []
    for row in items:
        subtitles = json.loads(row["subtitles_json"] or "{}")
        content_analysis = json.loads(row["content_analysis_json"] or "{}")
        alignment = subtitles.get("asr_alignment") or {}
        item_payloads.append(
            {
                "item_id": row["item_id"],
                "row_key": row["row_key"],
                "status": row["status"],
                "script_text": row["script_text"],
                "subtitle_status": subtitles.get("status"),
                "review_reason": subtitles.get("review_reason"),
                "raw_cues": subtitles.get("raw_cues"),
                "render_cues": subtitles.get("render_cues"),
                "semantic_mapping": subtitles.get("semantic_mapping"),
                "alignment_summary": {
                    key: alignment.get(key)
                    for key in (
                        "status",
                        "script_sha256",
                        "audio_asset_id",
                        "audio_version",
                        "model",
                        "script_token_count",
                        "recognized_token_count",
                        "exact_match_count",
                        "exact_match_ratio",
                    )
                },
                "subtitle_units": content_analysis.get("subtitle_units"),
            }
        )
    payload = {
        "project": dict(project) if project else None,
        "items": item_payloads,
        "script_sources": [dict(row) for row in sources],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
finally:
    connection.close()
