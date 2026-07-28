from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.models import GenerationTask, TaskStatus
from app.services.storage import remove_directory, task_output_dir
from app.workflows import get_workflow
from app.workflows.base import resolve_asset_path


class TaskManagementError(ValueError):
    """A task cannot perform the requested lifecycle operation."""


RETRYABLE_TASK_STATUSES = {
    TaskStatus.FAILED.value,
    TaskStatus.DOWNLOAD_FAILED.value,
}

TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCESS.value,
    TaskStatus.FAILED.value,
    TaskStatus.DOWNLOAD_FAILED.value,
    TaskStatus.CANCELLED.value,
}


def prepare_task_retry(
    task: GenerationTask,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """Reset a failed row without repeating a paid successful generation."""

    if task.status not in RETRYABLE_TASK_STATUSES:
        raise TaskManagementError("只有失败任务可以重试")
    retry_time = now or datetime.now(timezone.utc)
    if (
        task.status == TaskStatus.DOWNLOAD_FAILED.value
        and task.runninghub_task_id
    ):
        task.status = TaskStatus.RUNNING.value
        task.runninghub_submitted_at = retry_time
    else:
        try:
            workflow = get_workflow(task.workflow_type)
            assets = workflow.assets_for_task(task)
            missing = [
                asset.original_name
                for asset in assets
                if not resolve_asset_path(asset, settings).is_file()
            ]
        except ValueError as exc:
            raise TaskManagementError(
                "原上传素材不可用，无法重试；请重新创建任务"
            ) from exc
        if missing:
            raise TaskManagementError(
                "原上传素材已清理，无法重试；请重新创建任务"
            )
        remove_directory(task_output_dir(settings, task.user_id, task.id))
        task.runninghub_task_id = None
        task.runninghub_submitted_at = None
        task.runninghub_usage = None
        task.result_path = None
        task.output_metadata = None
        task.status = TaskStatus.PENDING.value
        # Retried work returns at the end of the shared per-user FIFO.
        task.created_at = retry_time

    task.error_code = None
    task.error_message = None
    task.completed_at = None
