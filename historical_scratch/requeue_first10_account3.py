from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path


DB_PATH = Path(
    r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db"
)
PROJECT_ID = "0f0f6e40a6284ee587897daef54fac7d"
TARGET_ROW_KEYS = ("1", "2", "3", "4", "8", "9", "10", "11", "12", "13")
EXPECTED_ERROR = "该画面生成操作的 RunningHub 执行账号快照已锁定，不能修改"
EXPECTED_BATCH_ID = "fabd8c18-6445-4b73-a1a2-e3ee850d1d9a"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def main() -> None:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in TARGET_ROW_KEYS)
        rows = connection.execute(
            f"""
            SELECT i.position, i.row_key, i.item_id, i.status AS item_status,
                   i.current_base_video_asset_id, i.current_video_asset_id,
                   o.rowid AS operation_rowid, o.operation_id, o.status AS operation_status,
                   o.error_code, o.error_message, o.payload_json, o.correlation_id
            FROM project_items AS i
            JOIN project_operations AS o ON o.item_id=i.item_id
            WHERE i.project_id=?
              AND i.row_key IN ({placeholders})
              AND o.operation_type='COMPOSITION_GENERATE'
              AND o.rowid=(
                  SELECT MAX(o2.rowid)
                  FROM project_operations AS o2
                  WHERE o2.project_id=i.project_id
                    AND o2.item_id=i.item_id
                    AND o2.operation_type='COMPOSITION_GENERATE'
              )
            ORDER BY i.position
            """,
            (PROJECT_ID, *TARGET_ROW_KEYS),
        ).fetchall()
        actual_keys = tuple(str(row["row_key"]) for row in rows)
        if actual_keys != TARGET_ROW_KEYS:
            raise RuntimeError(f"目标行发生变化：{actual_keys!r}")

        prepared: list[tuple[sqlite3.Row, dict[str, object]]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if row["item_status"] != "COMPOSITION_FAILED":
                raise RuntimeError(f"任务 {row['row_key']} 已不再是失败状态")
            if row["operation_status"] != "FAILED" or row["error_code"] != "AuthCenterError":
                raise RuntimeError(f"任务 {row['row_key']} 的最新操作已变化")
            if row["error_message"] != EXPECTED_ERROR:
                raise RuntimeError(f"任务 {row['row_key']} 的失败原因已变化")
            if row["current_base_video_asset_id"] or row["current_video_asset_id"]:
                raise RuntimeError(f"任务 {row['row_key']} 已存在视频，拒绝重复提交")
            if payload.get("batch_id") != EXPECTED_BATCH_ID:
                raise RuntimeError(f"任务 {row['row_key']} 不属于预期的锁定批次")
            if payload.get("runninghub_execution_account_ids") != [3, 5]:
                raise RuntimeError(f"任务 {row['row_key']} 的原账号快照已变化")
            payload["runninghub_execution_account_ids"] = [3]
            prepared.append((row, payload))

        now = now_iso()
        created: list[dict[str, str]] = []
        for row, payload in prepared:
            operation_id = uuid.uuid4().hex
            idempotency_key = (
                f"manual-account3-first10-{now}-{row['item_id']}-{uuid.uuid4().hex}"
            )
            connection.execute(
                """
                INSERT INTO project_operations(
                    operation_id, correlation_id, project_id, item_id,
                    operation_type, status, idempotency_key,
                    payload_json, result_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'COMPOSITION_GENERATE', 'PENDING', ?, ?, '{}', ?, ?)
                """,
                (
                    operation_id,
                    str(row["correlation_id"] or uuid.uuid4().hex),
                    PROJECT_ID,
                    str(row["item_id"]),
                    idempotency_key,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE project_items SET status='COMPOSITION_QUEUED', updated_at=? WHERE item_id=?",
                (now, str(row["item_id"])),
            )
            created.append(
                {
                    "row_key": str(row["row_key"]),
                    "item_id": str(row["item_id"]),
                    "operation_id": operation_id,
                }
            )

        connection.execute(
            "UPDATE projects SET status='PROCESSING', updated_at=? WHERE project_id=?",
            (now, PROJECT_ID),
        )
        connection.commit()
        print(
            json.dumps(
                {"account_ids": [3], "created_count": len(created), "operations": created},
                ensure_ascii=True,
            )
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
