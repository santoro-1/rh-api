import json
import os
import re
import sqlite3
import sys
from pathlib import Path


def parse_json(value, default):
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default


def redact(value):
    if value is None:
        return None
    text = str(value)
    return re.sub(
        r"(?i)(authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )


def table_exists(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def select_rows(connection, table, columns, order_by=""):
    if not table_exists(connection, table):
        return []
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    selected = [column for column in columns if column in existing]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return [dict(row) for row in connection.execute(sql)]


def h3_batch_summary(batch):
    if not isinstance(batch, dict):
        return None
    return {
        "batch_id": batch.get("batch_id"),
        "status": batch.get("status"),
        "row_ids": batch.get("row_ids") if isinstance(batch.get("row_ids"), list) else [],
        "last_synced_at": batch.get("last_synced_at"),
    }


def h3_segment_summary(segment):
    if not isinstance(segment, dict):
        return None
    normalized_video = any(
        bool(segment.get(key))
        for key in (
            "normalized_video_download",
            "normalized_video_url",
            "video_download_url",
            "video_url",
            "output_url",
        )
    )
    return {
        "segment_id": segment.get("segment_id"),
        "index": segment.get("index"),
        "status": segment.get("status"),
        "can_retry": segment.get("can_retry"),
        "retry_count": segment.get("retry_count"),
        "error_code": segment.get("error_code"),
        "error_message": redact(segment.get("error_message")),
        "has_normalized_video_download": normalized_video,
    }


database_path = Path(sys.argv[1]).resolve()
output_path = Path(sys.argv[2]).resolve()
uri = "file:" + database_path.as_posix() + "?mode=ro"
connection = sqlite3.connect(uri, uri=True, timeout=10)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA query_only=ON")

summary = {
    "schema": "jyd.188-database-summary.v1",
    "database_file": str(database_path),
    "database_size_bytes": database_path.stat().st_size,
    "projects": [],
    "items": [],
    "assets": [],
    "operations": [],
    "result_batches": [],
    "links": [],
}

project_rows = select_rows(
    connection,
    "projects",
    [
        "project_id", "project_no", "name", "status", "revision",
        "settings_json", "created_at", "updated_at",
    ],
    "updated_at DESC",
)
for row in project_rows:
    settings = parse_json(row.pop("settings_json", "{}"), {})
    h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
    batches = []
    for batch in h3.get("batches", []):
        record = h3_batch_summary(batch)
        if record:
            batches.append(record)
    row["generation_mode"] = settings.get("generation_mode")
    row["h3"] = {
        "remote_batch_id": h3.get("remote_batch_id"),
        "remote_status": h3.get("remote_status"),
        "last_synced_at": h3.get("last_synced_at"),
        "batches": batches,
    }
    summary["projects"].append(row)

item_rows = select_rows(
    connection,
    "project_items",
    [
        "item_id", "project_id", "row_key", "position", "status",
        "current_image_asset_id", "current_audio_asset_id",
        "current_base_video_asset_id", "current_video_asset_id",
        "settings_json", "created_at", "updated_at",
    ],
    "project_id, position",
)
for row in item_rows:
    settings = parse_json(row.pop("settings_json", "{}"), {})
    h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
    segments = []
    for segment in h3.get("segments", []):
        record = h3_segment_summary(segment)
        if record:
            segments.append(record)
    for asset_column in (
        "current_image_asset_id",
        "current_audio_asset_id",
        "current_base_video_asset_id",
        "current_video_asset_id",
    ):
        asset_id = row.pop(asset_column, None)
        row[asset_column.replace("_asset_id", "_asset_recorded")] = bool(asset_id)
    row["h3"] = {
        "remote_batch_id": h3.get("remote_batch_id"),
        "remote_item_id": h3.get("remote_item_id"),
        "remote_status": h3.get("remote_status"),
        "last_synced_at": h3.get("last_synced_at"),
        "invalidated_reason": h3.get("invalidated_reason"),
        "segments": segments,
    }
    summary["items"].append(row)

asset_rows = select_rows(
    connection,
    "project_assets",
    [
        "asset_id", "project_id", "item_id", "asset_type", "version",
        "status", "source_type", "managed_path", "created_at", "updated_at",
    ],
    "project_id, item_id, asset_type, version",
)
for row in asset_rows:
    managed_path = row.pop("managed_path", None)
    row["managed_path_recorded"] = bool(managed_path)
    row["managed_file_exists"] = bool(managed_path and os.path.isfile(managed_path))
    summary["assets"].append(row)

operation_rows = select_rows(
    connection,
    "project_operations",
    [
        "operation_id", "correlation_id", "project_id", "item_id",
        "operation_type", "status", "attempt_count", "error_code",
        "error_message", "created_at", "updated_at", "started_at", "finished_at",
    ],
    "created_at DESC",
)
for row in operation_rows:
    row["error_message"] = redact(row.get("error_message"))
    summary["operations"].append(row)

result_rows = select_rows(
    connection,
    "project_result_batches",
    [
        "result_batch_id", "project_id", "date_key", "date_label", "batch_no",
        "export_path", "operation_type", "status", "jianying_batch_id",
        "error_message", "created_at", "updated_at",
    ],
    "created_at DESC",
)
for row in result_rows:
    export_path = row.pop("export_path", None)
    row["export_path_recorded"] = bool(export_path)
    row["export_path_exists"] = bool(export_path and os.path.exists(export_path))
    row["error_message"] = redact(row.get("error_message"))
    summary["result_batches"].append(row)

link_rows = select_rows(
    connection,
    "project_links",
    [
        "link_id", "project_id", "item_id", "system", "relation",
        "external_id", "created_at",
    ],
    "created_at DESC",
)
summary["links"] = link_rows

summary["counts"] = {
    key: len(summary[key])
    for key in ("projects", "items", "assets", "operations", "result_batches", "links")
}

connection.close()
output_path.write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
)