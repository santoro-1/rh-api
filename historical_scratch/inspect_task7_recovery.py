from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def compact_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {"raw": raw[:200]}
    if not isinstance(data, dict):
        return {"value": data}
    keep = {
        "remote_item_id",
        "remote_project_id",
        "remote_status",
        "video_status",
        "stage",
        "resolution",
        "width",
        "height",
        "error_code",
        "error_message",
        "source_asset_id",
        "output_asset_id",
    }
    return {key: value for key, value in data.items() if key in keep}


def inspect(db_path: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    project = con.execute(
        "SELECT * FROM projects WHERE project_no = ?",
        ("DH-20260810-0001",),
    ).fetchone()
    if project is None:
        return {"db": str(db_path), "project": None}
    item = con.execute(
        "SELECT * FROM project_items WHERE project_id = ? AND position = 7",
        (project["project_id"],),
    ).fetchone()
    result = {
        "db": str(db_path),
        "project": {key: project[key] for key in ("project_id", "project_no", "name", "status", "revision", "updated_at")},
        "item": None,
        "assets": [],
        "links": [],
        "operations": [],
    }
    if item is None:
        return result
    result["item"] = {
        key: item[key]
        for key in (
            "item_id",
            "row_key",
            "position",
            "status",
            "current_image_asset_id",
            "current_audio_asset_id",
            "current_base_video_asset_id",
            "current_video_asset_id",
            "created_at",
            "updated_at",
        )
    }
    for row in con.execute(
        "SELECT * FROM project_assets WHERE project_id = ? AND item_id = ? ORDER BY asset_type, version",
        (project["project_id"], item["item_id"]),
    ):
        result["assets"].append({
            key: row[key]
            for key in (
                "asset_id",
                "asset_type",
                "version",
                "status",
                "source_type",
                "filename",
                "managed_path",
                "created_at",
                "updated_at",
            )
        } | {"metadata": compact_json(row["metadata_json"]), "external": compact_json(row["external_ref_json"])})
    for row in con.execute(
        "SELECT * FROM project_links WHERE project_id = ? AND item_id = ? ORDER BY created_at",
        (project["project_id"], item["item_id"]),
    ):
        result["links"].append({
            key: row[key]
            for key in ("link_id", "system", "relation", "external_id", "created_at")
        } | {"metadata": compact_json(row["metadata_json"])})
    for row in con.execute(
        "SELECT * FROM project_operations WHERE project_id = ? AND item_id = ? ORDER BY created_at",
        (project["project_id"], item["item_id"]),
    ):
        result["operations"].append({
            key: row[key]
            for key in (
                "operation_id",
                "operation_type",
                "status",
                "attempt_count",
                "error_code",
                "error_message",
                "created_at",
                "updated_at",
                "finished_at",
            )
        } | {"payload": compact_json(row["payload_json"]), "result": compact_json(row["result_json"])})
    return result


for arg in sys.argv[1:]:
    print(json.dumps(inspect(Path(arg)), ensure_ascii=False, indent=2))
