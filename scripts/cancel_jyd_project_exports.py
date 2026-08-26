from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Cancel queued JYD MP4 exports but keep drafts")
    parser.add_argument("--jyd-root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--owner-user-id", required=True)
    args = parser.parse_args()

    jyd_root = Path(args.jyd_root).expanduser().resolve()
    sys.path.insert(0, str(jyd_root / "src"))
    from jyd_probe.project_store import ProjectStore
    from jyd_probe.task_store import SQLiteTaskStore

    database = Path(args.database).expanduser().resolve()
    project_store = ProjectStore(database)
    task_store = SQLiteTaskStore(database)
    project = project_store.get_project(args.owner_user_id, args.project_id)
    item_by_id = {str(item["item_id"]): item for item in project["items"]}
    results: list[dict[str, object]] = []
    for operation in project.get("operations", []):
        if (
            operation.get("operation_type") != "POSTPROCESS_EXPORT"
            or operation.get("status") not in {"PENDING", "RUNNING"}
        ):
            continue
        result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
        batch_id = str(result.get("batch_id") or "")
        item_id = str(operation.get("item_id") or "")
        item = item_by_id.get(item_id)
        if not batch_id or item is None:
            continue
        cancelled_jobs = task_store.cancel_batch(batch_id)
        project_store.transition_operation(
            args.owner_user_id,
            args.project_id,
            item_id,
            operation_id=str(operation["operation_id"]),
            operation_type="POSTPROCESS_EXPORT",
            status="FAILED",
            item_status="COMPOSITION_READY",
            result={"batch_id": batch_id, "job_id": result.get("job_id")},
            error_code="USER_CANCELLED_EXPORT",
            error_message="用户只需要可编辑草稿，已取消 MP4 导出",
        )
        results.append(
            {
                "row_key": item["row_key"],
                "batch_id": batch_id,
                "cancelled_pending_job_ids": cancelled_jobs,
            }
        )
    current = project_store.get_project(args.owner_user_id, args.project_id)
    print(
        json.dumps(
            {
                "project_id": args.project_id,
                "project_status": current["status"],
                "cancelled_exports": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
