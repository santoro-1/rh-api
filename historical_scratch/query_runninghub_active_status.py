import json
import os

os.chdir("/opt/runninghub-video")

from app.database import SessionLocal
from app.models import GenerationTask
from app.services.runninghub_attempts import (
    enhancement_execution_account_for_remote,
    task_execution_account_for_remote,
)
from app.services.runninghub_dispatch import (
    task_uses_execution_pool,
)
from app.workers.task_worker import _load_task, _make_client


results = []
with SessionLocal() as database:
    task_ids = [
        row[0]
        for row in database.query(GenerationTask.id)
        .filter(GenerationTask.status == "RUNNING")
        .all()
    ]
    for task_id in task_ids:
        task = _load_task(database, task_id)
        if task is None:
            continue
        enhancement = task.enhancement
        if (
            enhancement is not None
            and enhancement.status not in {"SUCCESS", "FAILED", "CANCELLED"}
            and enhancement.remote_task_id
        ):
            stage = "seedvr2"
            remote_id = enhancement.remote_task_id
            config = (
                enhancement_execution_account_for_remote(task, enhancement)
                if task_uses_execution_pool(task)
                else task.user.runninghub_config
            )
        elif task.runninghub_task_id:
            stage = "digital_human"
            remote_id = task.runninghub_task_id
            config = (
                task_execution_account_for_remote(task)
                if task_uses_execution_pool(task)
                else task.user.runninghub_config
            )
        else:
            continue
        record = {
            "task_id": task.id,
            "row_key": task.batch_item.row_key if task.batch_item else None,
            "segment_index": task.segment.segment_index if task.segment else None,
            "audio_duration_seconds": task.audio_duration_seconds,
            "stage": stage,
            "remote_task_id": remote_id,
        }
        try:
            response = _make_client(config).query_task(remote_id)
            usage = response.get("usage") if isinstance(response, dict) else None
            record.update(
                {
                    "remote_status": response.get("status"),
                    "error_code": response.get("errorCode"),
                    "error_message": response.get("errorMessage"),
                    "task_cost_time": (
                        usage.get("taskCostTime")
                        if isinstance(usage, dict)
                        else None
                    ),
                }
            )
        except Exception as error:
            record["query_error"] = f"{type(error).__name__}: {error}"
        results.append(record)

print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
