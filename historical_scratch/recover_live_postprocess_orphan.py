from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path


PROJECT_ID = "0f0f6e40a6284ee587897daef54fac7d"
OPERATION_ID = "3932e181f1f6438cbe1514f63e8530d8"
DATABASE = Path(
    r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db"
)
REPOSITORY = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")


def service_is_listening() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", 8010)) == 0


def main() -> None:
    if service_is_listening():
        raise SystemExit("Refusing to edit the live database while port 8010 is listening")

    with sqlite3.connect(DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT o.*, p.owner_user_id
            FROM project_operations o
            JOIN projects p ON p.project_id=o.project_id
            WHERE o.project_id=? AND o.operation_id=?
            """,
            (PROJECT_ID, OPERATION_ID),
        ).fetchone()
    if row is None:
        raise SystemExit("Expected orphan operation no longer exists")
    result = json.loads(row["result_json"] or "{}")
    if (
        row["operation_type"] != "POSTPROCESS_GENERATE"
        or row["status"] != "PENDING"
        or result.get("job_id")
    ):
        raise SystemExit(
            "Refusing to recover operation because its live state changed: "
            f"type={row['operation_type']} status={row['status']} result={result}"
        )

    sys.path.insert(0, str(REPOSITORY / "src"))
    from jyd_probe.project_store import ProjectStore

    store = ProjectStore(DATABASE)
    project = store.transition_operation(
        str(row["owner_user_id"]),
        PROJECT_ID,
        str(row["item_id"]),
        operation_id=OPERATION_ID,
        operation_type="POSTPROCESS_GENERATE",
        status="FAILED",
        item_status="COMPOSITION_FAILED",
        error_code="LOCAL_HANDOFF_INTERRUPTED",
        error_message="剪映草稿任务提交在取得 job_id 前中断，已安全回收，可重新生成草稿",
    )
    item = next(entry for entry in project["items"] if entry["item_id"] == row["item_id"])
    print(
        json.dumps(
            {
                "operation_id": OPERATION_ID,
                "operation_status": "FAILED",
                "item_id": item["item_id"],
                "row_key": item["row_key"],
                "item_status": item["status"],
                "project_status": project["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
