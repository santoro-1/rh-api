from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlalchemy import update
from sqlalchemy.orm import object_session

from app.config import Settings
from app.models import EnhancementStatus, GenerationTask, TaskStatus
from app.services.device_auth.admission import WorkbenchIdentity, require_new_work
from app.services.device_auth.queued_work import task_requires_device_admission
from app.services.storage import (
    remove_directory,
    safe_relative_path,
    task_output_dir,
)
from app.services.runninghub_attempts import (
    enhancement_has_uncertain_submission,
    task_has_uncertain_submission,
)
from app.services.runninghub_dispatch import (
    task_is_legacy_web_digital_human,
    task_uses_execution_pool,
)
from app.services.runninghub_pool import (
    RunningHubPoolSelectionUnavailableError,
    assigned_execution_account_ids,
    task_execution_account_snapshot,
)
from app.services.seedvr2_dispatch import (
    release_seedvr2_account_for_new_attempt,
    task_uses_dual_pool,
)
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


def _ensure_legacy_pool_snapshot(task: GenerationTask) -> None:
    if (
        not task_is_legacy_web_digital_human(task)
        or task_execution_account_snapshot(task) is not None
    ):
        return
    db = object_session(task)
    if db is None:
        raise TaskManagementError("任务未绑定数据库会话，无法冻结执行账号")
    try:
        assigned_ids = assigned_execution_account_ids(db, task.user)
    except RunningHubPoolSelectionUnavailableError as exc:
        raise TaskManagementError(str(exc)) from exc
    task.runninghub_execution_account_ids_json = json.dumps(
        assigned_ids, ensure_ascii=False, separators=(",", ":")
    )
    # Snapshot-less historical tasks came from the personal-key path. Do not
    # let a stale incidental binding override the user's current assignment.
    task.execution_account_id = None


def prepare_task_retry(
    task: GenerationTask,
    settings: Settings,
    *,
    now: datetime | None = None,
    device_identity: WorkbenchIdentity | None = None,
) -> None:
    """Reset a failed or cancelled stage without repeating paid success."""

    if task.status not in RETRYABLE_TASK_STATUSES:
        raise TaskManagementError("只有失败或已取消任务可以重新生成")
    if task_has_uncertain_submission(task):
        raise TaskManagementError(
            "RunningHub 提交结果无法确认，禁止盲目重提；请管理员先在 RunningHub 核对远程任务"
        )
    retry_time = now or datetime.now(timezone.utc)
    if task_requires_device_admission(task):
        db = object_session(task)
        if db is not None:
            # A caller may have just recorded the provider failure on this ORM
            # object without committing it yet (the worker/pool recovery path
            # does this).  Flush that caller-owned state before taking the
            # optimistic-lock snapshot.  A genuinely stale, otherwise-clean
            # object still keeps its old timestamp and is rejected below.
            db.flush()
            reviewed_status = task.status
            reviewed_updated_at = task.updated_at
        if not (task.status == TaskStatus.DOWNLOAD_FAILED.value and task.runninghub_task_id):
            if db is None:
                raise TaskManagementError("工作台付费重试缺少数据库授权上下文")
            # Shared legacy/batch retry routes must not borrow the original
            # task's admitted device to start another paid attempt. Only the
            # newly verified request may authorize that attempt.
            require_new_work(db, user_id=task.user_id, identity=device_identity)
        if db is not None:
            # Compare the state actually reviewed by this caller, not merely
            # its stale ORM object. Keep the write lock through reset+commit so
            # a second click cannot overwrite a task already claimed by Worker.
            changed = db.execute(
                update(GenerationTask)
                .where(
                    GenerationTask.id == task.id,
                    GenerationTask.status == reviewed_status,
                    GenerationTask.updated_at == reviewed_updated_at,
                )
                .values(updated_at=retry_time)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise TaskManagementError("任务状态已变化，请刷新后查看；本次未重复创建重试")
            task.updated_at = retry_time
    enhancement = task.enhancement
    if enhancement is not None:
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
                "源视频片段路径不合法，无法重试清晰化"
            ) from exc
        if not source_path.is_file():
            raise TaskManagementError(
                "源视频片段已清理，无法只重试清晰化；请重新生成源视频阶段"
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
            if (
                task_uses_dual_pool(task)
                and enhancement.seedvr2_execution_account is not None
                and enhancement.seedvr2_execution_account.health_status
                in {"UNHEALTHY", "ERROR"}
            ):
                release_seedvr2_account_for_new_attempt(enhancement)
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
    if not (
        task.status == TaskStatus.DOWNLOAD_FAILED.value
        and task.runninghub_task_id
    ):
        _ensure_legacy_pool_snapshot(task)
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
        # A cloud-accepted attempt keeps its recorded pool account.  Safe
        # pre-submission failures release their reservation before reaching
        # this path, so only never-started work can be assigned afresh.
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


def task_retry_starts_new_provider_work(task: GenerationTask) -> bool:
    """Classify from server state; callers must not trust a client retry label."""
    if task.enhancement is not None:
        return not (
            task.enhancement.status == EnhancementStatus.DOWNLOAD_FAILED.value
            and task.enhancement.remote_task_id
        )
    return not (
        task.status == TaskStatus.DOWNLOAD_FAILED.value
        and task.runninghub_task_id
    )


def prepare_successful_segment_regeneration(
    task: GenerationTask,
    settings: Settings,
    *,
    now: datetime | None = None,
    device_identity: WorkbenchIdentity | None = None,
) -> None:
    """Replace one successful segmented result after an explicit paid action."""

    if task.status != TaskStatus.SUCCESS.value or not task.segment_id:
        raise TaskManagementError("只有成功的分段视频可以重新生成")
    if task_requires_device_admission(task):
        db = object_session(task)
        if db is None:
            raise TaskManagementError("工作台付费重生成缺少数据库授权上下文")
        require_new_work(db, user_id=task.user_id, identity=device_identity)
    _ensure_task_assets(task, settings)
    _ensure_legacy_pool_snapshot(task)
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
