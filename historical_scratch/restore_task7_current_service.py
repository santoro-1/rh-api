from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


DB = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db")
PROJECT_NO = "DH-20260810-0001"
POSITION = 7
BASE_ASSET_ID = "e3be1c1775e341159d782de6941dc0d2"
VIDEO_ASSET_ID = "a3dbef3f59544274b279e7a6e1ddd891"
ACTIVE = {"AUDIO_RUNNING", "DIGITAL_HUMAN_QUEUED", "DIGITAL_HUMAN_RUNNING", "VIDEO_ENHANCING", "VIDEO_MERGING", "POSTPROCESS_RUNNING", "VARIANT_QUEUED", "VARIANT_RUNNING"}
FAILED = {"AUDIO_FAILED", "DIGITAL_HUMAN_FAILED", "COMPOSITION_FAILED", "VARIANT_FAILED"}


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
backup_dir = Path(r"D:\JYD\backups") / f"{stamp}-task7-recovery"
backup_dir.mkdir(parents=True, exist_ok=False)
backup_db = backup_dir / "control.db"

source = sqlite3.connect(DB, timeout=30)
destination = sqlite3.connect(backup_db)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()

connection = sqlite3.connect(DB, timeout=30)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA busy_timeout=30000")
try:
    connection.execute("BEGIN IMMEDIATE")
    project = connection.execute(
        "SELECT * FROM projects WHERE project_no = ?", (PROJECT_NO,)
    ).fetchone()
    if project is None:
        raise RuntimeError("目标项目不存在")
    item = connection.execute(
        "SELECT * FROM project_items WHERE project_id = ? AND position = ?",
        (project["project_id"], POSITION),
    ).fetchone()
    if item is None:
        raise RuntimeError("任务 7 不存在")
    if item["status"] != "AUDIO_READY" or item["current_base_video_asset_id"] or item["current_video_asset_id"]:
        raise RuntimeError(f"任务 7 当前状态已变化，停止恢复: {row_dict(item)}")

    assets = {}
    for asset_id, expected_type in ((BASE_ASSET_ID, "base_video"), (VIDEO_ASSET_ID, "composition_video")):
        asset = connection.execute(
            "SELECT * FROM project_assets WHERE asset_id = ? AND project_id = ? AND item_id = ?",
            (asset_id, project["project_id"], item["item_id"]),
        ).fetchone()
        if asset is None or asset["asset_type"] != expected_type or asset["status"] != "READY":
            raise RuntimeError(f"恢复资产不可用: {asset_id}")
        managed_path = Path(asset["managed_path"])
        if not managed_path.is_file() or managed_path.stat().st_size <= 0:
            raise RuntimeError(f"恢复文件不存在或为空: {managed_path}")
        assets[expected_type] = {"asset_id": asset_id, "path": str(managed_path), "size": managed_path.stat().st_size}

    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    subtitles = json.loads(item["subtitles_json"] or "{}")
    subtitles["bound_video_asset_id"] = VIDEO_ASSET_ID
    connection.execute(
        """
        UPDATE project_items
        SET current_base_video_asset_id=?, current_video_asset_id=?,
            subtitles_json=?, status='COMPOSITION_READY', updated_at=?
        WHERE item_id=? AND project_id=?
        """,
        (BASE_ASSET_ID, VIDEO_ASSET_ID, json.dumps(subtitles, ensure_ascii=False, separators=(",", ":")), now, item["item_id"], project["project_id"]),
    )

    state_rows = connection.execute(
        "SELECT status, current_audio_asset_id, current_base_video_asset_id, current_video_asset_id FROM project_items WHERE project_id=?",
        (project["project_id"],),
    ).fetchall()
    statuses = [str(row["status"]) for row in state_rows]
    if any(status in ACTIVE for status in statuses):
        project_status = "PROCESSING"
    elif statuses and all(status == "VARIANT_READY" for status in statuses):
        project_status = "VARIANT_READY"
    elif any(status in FAILED for status in statuses):
        successful = any(row["current_audio_asset_id"] or row["current_base_video_asset_id"] or row["current_video_asset_id"] for row in state_rows)
        project_status = "PARTIAL_FAILED" if successful else "FAILED"
    elif state_rows and all(row["current_video_asset_id"] or str(row["status"]) in {"COMPOSITION_READY", "VARIANT_READY"} for row in state_rows):
        project_status = "COMPOSITION_READY"
    elif state_rows and all(row["current_base_video_asset_id"] for row in state_rows):
        project_status = "BASE_VIDEO_READY"
    elif state_rows and all(row["current_audio_asset_id"] for row in state_rows):
        project_status = "AUDIO_READY"
    else:
        project_status = "DRAFT"
    connection.execute(
        "UPDATE projects SET status=?, updated_at=? WHERE project_id=?",
        (project_status, now, project["project_id"]),
    )
    connection.commit()

    restored = connection.execute(
        "SELECT position, status, current_base_video_asset_id, current_video_asset_id, updated_at FROM project_items WHERE item_id=?",
        (item["item_id"],),
    ).fetchone()
    print(json.dumps({
        "backup": str(backup_db),
        "project_id": project["project_id"],
        "item_id": item["item_id"],
        "before": {
            "status": item["status"],
            "current_base_video_asset_id": item["current_base_video_asset_id"],
            "current_video_asset_id": item["current_video_asset_id"],
        },
        "after": row_dict(restored),
        "assets": assets,
        "project_status": project_status,
    }, ensure_ascii=False, indent=2))
except Exception:
    connection.rollback()
    raise
finally:
    connection.close()
