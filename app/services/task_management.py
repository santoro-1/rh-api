from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.models import EnhancementStatus, GenerationTask, TaskStatus
from app.services.storage import (
    remove_directory,
    safe_relative_path,
    task_output_dir,
)
from app.services.runninghub_attempts import (
    enhancement_has_uncertain_submission,
    task_has_uncertain_submission,
)
from app.services.runninghub_dispatch import task_uses_execution_pool
from app.workflows import get_workflow
from app.workflows.base import resolve_asset_path


class TaskManagementError(ValueError):
    """A task cannot perform the requested lifecycle operation."""


RETRYABLE_TASK_STATUSES = {
    TaskStatus.FAILED.value,
    TaskStatus.DOWNLOAD_FAILED.value,
    TaskStatus.CANCELLED.value,
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
    """Reset a failed or cancelled stage without repeating paid success."""

    if task.status not in RETRYABLE_TASK_STATUSES:
        raise TaskManagementError("只有失败或已取消任务可以重新生成")
    if task_has_uncertain_submission(task):
        raise TaskManagementError(
            "RunningHub 提交结果无法确认，禁止盲目重提；请管理员先在 RunningHub 核对远程任务"
        )
    retry_time = now or datetime.now(timezone.utc)
    enhancement = task.enhancement
    if task.workflow_type == "digital_human" and enhancement is not None:
        if enhancement_has_uncertain_submission(enhancement):
            raise TaskManagementError(
                "SeedVR2 提交结果无法确认，禁止重新提交；请管理员先核对原执行账号"
            )
        try:
            source_path = safe_relative_path(
                enhancement.source_result_path, settings.data_dir
            )
        except ValueError as exc:
            raise TaskManagementError(
                "数字人源片段路径不合法，无法重试清晰化"
            ) from exc
        if not source_path.is_file():
            raise TaskManagementError(
                "数字人源片段已清理，无法只重试清晰化；请重新生成数字人"
            )
        if (
            enhancement.status == EnhancementStatus.DOWNLOAD_FAILED.value
            and enhancement.remote_task_id
        ):
            enhancement.status = EnhancementStatus.SUBMITTED.value
        else:
            if enhancement.result_path:
                try:
                    safe_relative_path(
                        enhancement.result_path, settings.data_dir
                    ).unlink(missing_ok=True)
                except ValueError:
                    pass
            enhancement.status = EnhancementStatus.PENDING.value
            enhancement.remote_task_id = None
            enhancement.submitted_at = None
            enhancement.started_at = None
            enhancement.finished_at = None
            enhancement.result_path = None
            enhancement.result_filename = None
            enhancement.result_size = None
            enhancement.result_sha256 = None
            enhancement.output_metadata_json = None
        enhancement.error_message = None
        enhancement.failed_reason_json = None
        enhancement.usage_json = None
        enhancement.auto_retry_count = 0
        enhancement.auto_retry_after = None
        task.status = TaskStatus.RUNNING.value
        task.error_code = None
        task.error_message = None
        task.result_path = None
        task.output_metadata = None
        task.completed_at = None
        return
    if (
        task.status == TaskStatus.DOWNLOAD_FAILED.value
        and task.runninghub_task_id
    ):
        task.status = TaskStatus.RUNNING.value
        task.runninghub_submitted_at = retry_time
    else:
        _ensure_task_assets(task, settings)
        remove_directory(task_output_dir(settings, task.user_id, task.id))
        task.runninghub_task_id = None
        task.runninghub_submitted_at = None
        task.runninghub_failed_reason = None
        task.runninghub_usage = None
        task.result_path = None
        task.output_metadata = None
        task.status = TaskStatus.PENDING.value
        if task_uses_execution_pool(task):
            task.execution_account_id = None
            task.execution_account = None
        # Retried work returns at the end of the shared per-user FIFO.
        task.created_at = retry_time

    task.error_code = None
    task.error_message = None
    task.runninghub_failed_reason = None
    task.runninghub_auto_retry_count = 0
    task.runninghub_auto_retry_after = None
    task.completed_at = None


def _ensure_task_assets(task: GenerationTask, settings: Settings) -> None:
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
            "原上传素材不可用，无法重新生成；请重新创建任务"
        ) from exc
    if missing:
        raise TaskManagementError(
            "原上传素材已清理，无法重新生成；请重新创建任务"
        )


def prepare_successful_segment_regeneration(
    task: GenerationTask,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    """Replace one successful segmented result after an explicit paid action."""

    if task.status != TaskStatus.SUCCESS.value or not task.segment_id:
        raise TaskManagementError("只有成功的分段视频可以重新生成")
    _ensure_task_assets(task, settings)
    retry_time = now or datetime.now(timezone.utc)
    remove_directory(task_output_dir(settings, task.user_id, task.id))
    task.enhancement = None
    if task_uses_execution_pool(task):
        task.execution_account_id = None
        task.execution_account = None
    task.runninghub_task_id = None
    task.runninghub_submitted_at = None
    task.runninghub_failed_reason = None
    task.runninghub_usage = None
    task.result_path = None
    task.output_metadata = None
    task.status = TaskStatus.PENDING.value
    task.created_at = retry_time
    task.error_code = None
    task.error_message = None
    task.runninghub_auto_retry_count = 0
    task.runninghub_auto_retry_after = None
    task.completed_at = None
