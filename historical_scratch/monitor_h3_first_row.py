from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path


RH_DB = Path(r"D:\工作内容\轻盈健\数字人\runninghub_mvp\data\app.db")
JYD_DB = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\runtime\test_environment\web_storage\control.db"
)
PROJECT_ID = "dae5c0c1c30e45ff8fbfa31087888572"
ROW_KEY = "1"
REMOTE_BATCH_ID = "978bd795-980b-4867-b10a-1bd660e1da77"
TERMINAL_FAILURES = {"FAILED", "CANCELLED", "H3_FAILED", "COMPOSITION_FAILED"}


def snapshot() -> dict:
    rh = sqlite3.connect(RH_DB, timeout=5)
    rh.row_factory = sqlite3.Row
    jyd = sqlite3.connect(JYD_DB, timeout=5)
    jyd.row_factory = sqlite3.Row
    try:
        remote_item = rh.execute(
            "SELECT * FROM generation_batch_items WHERE batch_id=? AND row_key=?",
            (REMOTE_BATCH_ID, ROW_KEY),
        ).fetchone()
        if remote_item is None:
            raise RuntimeError("找不到 H3 远端项目行")
        tasks = rh.execute(
            """
            SELECT s.segment_index,s.status AS segment_status,s.video_path,
                   s.error_code AS segment_error_code,s.error_message AS segment_error_message,
                   t.status AS task_status,t.runninghub_task_id,t.result_path,
                   t.error_code AS task_error_code,t.error_message AS task_error_message,
                   t.runninghub_failed_reason,t.runninghub_auto_retry_count,
                   h.normalized_video_path,h.normalized_video_sha256
            FROM generation_segments s
            LEFT JOIN generation_tasks t ON t.segment_id=s.id
            LEFT JOIN h3_segment_configs h ON h.segment_id=s.id
            WHERE s.batch_item_id=?
            ORDER BY s.segment_index
            """,
            (remote_item["id"],),
        ).fetchall()
        local_item = jyd.execute(
            "SELECT * FROM project_items WHERE project_id=? AND row_key=?",
            (PROJECT_ID, ROW_KEY),
        ).fetchone()
        if local_item is None:
            raise RuntimeError("找不到 8001 测试项目第一条")
        settings = json.loads(local_item["settings_json"] or "{}")
        h3 = settings.get("h3") if isinstance(settings.get("h3"), dict) else {}
        local_segments = h3.get("segments") if isinstance(h3.get("segments"), list) else []
        return {
            "remote_item_status": remote_item["status"],
            "remote_item_error_code": remote_item["error_code"],
            "remote_item_error_message": remote_item["error_message"],
            "remote_merged_status": remote_item["merged_video_status"],
            "remote_merged_path": remote_item["merged_video_path"],
            "tasks": [
                {
                    "index": int(row["segment_index"]) + 1,
                    "task": row["task_status"],
                    "segment": row["segment_status"],
                    "remote_task_id": row["runninghub_task_id"],
                    "result_ready": bool(row["result_path"]),
                    "normalized_ready": bool(row["normalized_video_path"]),
                    "retry_count": int(row["runninghub_auto_retry_count"] or 0),
                    "error_code": row["task_error_code"] or row["segment_error_code"],
                    "error_message": (
                        row["task_error_message"]
                        or row["segment_error_message"]
                        or row["runninghub_failed_reason"]
                    ),
                }
                for row in tasks
            ],
            "local_item_status": local_item["status"],
            "local_base_video_ready": bool(local_item["current_base_video_asset_id"]),
            "local_current_video_ready": bool(local_item["current_video_asset_id"]),
            "local_remote_status": h3.get("remote_status"),
            "local_previews_ready": sum(
                1 for value in local_segments if value.get("local_preview_ready") is True
            ),
            "local_preview_errors": [
                str(value.get("local_preview_error"))
                for value in local_segments
                if value.get("local_preview_error")
            ],
        }
    finally:
        rh.close()
        jyd.close()


def is_failed(value: dict) -> bool:
    statuses = {
        str(value.get("remote_item_status") or "").upper(),
        str(value.get("remote_merged_status") or "").upper(),
        str(value.get("local_item_status") or "").upper(),
        str(value.get("local_remote_status") or "").upper(),
    }
    for task in value["tasks"]:
        statuses.add(str(task.get("task") or "").upper())
        statuses.add(str(task.get("segment") or "").upper())
    return bool(statuses & TERMINAL_FAILURES)


def main() -> int:
    last = None
    deadline = time.monotonic() + 45 * 60
    while time.monotonic() < deadline:
        value = snapshot()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if encoded != last:
            print(
                datetime.now().astimezone().isoformat(timespec="seconds"),
                json.dumps(value, ensure_ascii=False),
                flush=True,
            )
            last = encoded
        if is_failed(value):
            print("MONITOR_FINAL=FAILED", flush=True)
            return 2
        if value["local_base_video_ready"]:
            print("MONITOR_FINAL=LOCAL_BASE_VIDEO_READY", flush=True)
            return 0
        time.sleep(15)
    print("MONITOR_FINAL=TIMEOUT", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
