from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def load(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        projects = connection.execute(
            "SELECT project_id, name, created_at, updated_at FROM projects "
            "ORDER BY updated_at DESC"
        ).fetchall()
        for project in projects:
            rows = connection.execute(
                "SELECT item_id,row_key,status,content_analysis_json,visual_analysis_json,updated_at "
                "FROM project_items WHERE project_id=? ORDER BY CAST(row_key AS INTEGER),row_key",
                (project["project_id"],),
            ).fetchall()
            summaries = []
            for row in rows:
                content = load(row["content_analysis_json"])
                visual = load(row["visual_analysis_json"])
                if not content:
                    continue
                errors = content.get("errors") if isinstance(content.get("errors"), dict) else {}
                summaries.append(
                    {
                        "row_key": row["row_key"],
                        "item_id": row["item_id"],
                        "item_status": row["status"],
                        "overall_status": content.get("overall_status"),
                        "music_status": content.get("music_analysis_status"),
                        "subtitle_status": content.get("subtitle_analysis_status"),
                        "title_status": content.get("title_analysis_status"),
                        "request_error": errors.get("request"),
                        "provider_request_id": content.get("provider_request_id"),
                        "provider_attempts": content.get("provider_attempts"),
                        "request_count": content.get("request_count"),
                        "requested_at": content.get("requested_at"),
                        "analyzed_at": content.get("analyzed_at"),
                        "visual_status": visual.get("analysis_status"),
                        "visual_error": visual.get("error"),
                        "item_updated_at": row["updated_at"],
                    }
                )
            if summaries:
                print(
                    json.dumps(
                        {
                            "project_id": project["project_id"],
                            "name": project["name"],
                            "created_at": project["created_at"],
                            "updated_at": project["updated_at"],
                            "items": summaries,
                        },
                        ensure_ascii=True,
                    )
                )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
