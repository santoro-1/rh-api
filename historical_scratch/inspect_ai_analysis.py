from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


SAFE_FIELDS = (
    "status",
    "overall_status",
    "subtitle_analysis_status",
    "title_analysis_status",
    "music_analysis_status",
    "error_code",
    "error_message",
    "script_hash",
    "source_hash",
    "script_sha256",
    "snapshot_schema",
    "schema_version",
    "prompt_version",
    "subtitle_prompt_version",
    "model",
    "request_count",
    "provider_attempts",
    "provider_request_id",
    "cache_hit",
    "cacheable",
    "requested_at",
    "invalidated_at",
    "invalidated_reason",
    "started_at",
    "created_at",
    "updated_at",
    "completed_at",
    "finished_at",
    "analyzed_at",
    "version",
)


def json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_invalid_json": True}
    return value if isinstance(value, dict) else {"_json_type": type(value).__name__}


def snapshot_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"keys": sorted(payload)}
    for field in SAFE_FIELDS:
        value = payload.get(field)
        if value not in (None, "", [], {}):
            summary[field] = value
    for nested_name in ("result", "analysis", "metadata"):
        nested = payload.get(nested_name)
        if isinstance(nested, dict):
            summary[f"{nested_name}_keys"] = sorted(nested)
            for field in SAFE_FIELDS:
                value = nested.get(field)
                if value not in (None, "", [], {}):
                    summary[f"{nested_name}.{field}"] = value
    errors = payload.get("errors")
    if isinstance(errors, (dict, list)):
        summary["errors"] = errors
    requested_at = payload.get("requested_at")
    analyzed_at = payload.get("analyzed_at")
    if isinstance(requested_at, str) and isinstance(analyzed_at, str):
        try:
            summary["elapsed_seconds"] = round(
                (datetime.fromisoformat(analyzed_at) - datetime.fromisoformat(requested_at)).total_seconds(),
                3,
            )
        except ValueError:
            pass
    return summary


def main() -> None:
    db_path = Path(sys.argv[1]).resolve()
    project_filter = sys.argv[2] if len(sys.argv) > 2 else ""
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        project_sql = (
            "SELECT project_id, status, revision, created_at, updated_at FROM projects"
        )
        item_sql = (
            "SELECT project_id, item_id, row_key, position, status, "
            "content_analysis_json, created_at, updated_at FROM project_items"
        )
        query_params: tuple[str, ...] = ()
        if project_filter:
            project_sql += " WHERE project_id = ?"
            item_sql += " WHERE project_id = ?"
            query_params = (project_filter,)
        project_rows = connection.execute(
            project_sql + " ORDER BY updated_at DESC", query_params
        ).fetchall()
        item_rows = connection.execute(
            item_sql + " ORDER BY project_id, position", query_params
        ).fetchall()
        operation_params: tuple[str, ...] = ()
        operation_project_clause = ""
        if project_filter:
            operation_project_clause = "AND project_id = ? "
            operation_params = (project_filter,)
        operations = connection.execute(
            "SELECT project_id, item_id, operation_type, status, error_code, "
            "error_message, created_at, updated_at, started_at, finished_at "
            "FROM project_operations "
            "WHERE UPPER(operation_type) LIKE '%ANALYSIS%' "
            + operation_project_clause
            + "ORDER BY created_at",
            operation_params,
        ).fetchall()

        items = []
        status_counts: Counter[str] = Counter()
        for row in item_rows:
            payload = json_object(row["content_analysis_json"])
            analysis_status = str(
                payload.get("overall_status") or payload.get("status") or "<missing>"
            )
            status_counts[analysis_status] += 1
            items.append(
                {
                    "project_id": row["project_id"],
                    "item_id": row["item_id"],
                    "row_key": row["row_key"],
                    "position": row["position"],
                    "item_status": row["status"],
                    "item_created_at": row["created_at"],
                    "item_updated_at": row["updated_at"],
                    "content_analysis": snapshot_summary(payload),
                }
            )

        print(
            json.dumps(
                {
                    "projects": [dict(row) for row in project_rows],
                    "content_analysis_status_counts": dict(status_counts),
                    "items": items,
                    "analysis_operations": [dict(row) for row in operations],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
