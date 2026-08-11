from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import (
    EnhancementStatus,
    GenerationTask,
    RunningHubExecutionAccount,
    TaskStatus,
)
from app.services.runninghub import RunningHubClient
from app.services.runninghub_attempts import (
    enhancement_attempt_for_remote_id,
    enhancement_execution_account_for_remote,
    finish_task_attempt,
    task_execution_account_for_remote,
)
from app.services.security import decrypt_secret
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow
from app.workflows.seedvr2_upscale import SEEDVR2_AI_APP_ID


class TaskCancellationError(ValueError):
    """A generation task cannot be safely cancelled."""


def _mark_cancelled(task: GenerationTask) -> None:
    task.status = TaskStatus.CANCELLED.value
    task.error_code = "CANCELLED_BY_USER"
    task.error_message = "任务已由用户取消"
    task.runninghub_auto_retry_after = None
    task.completed_at = datetime.now(timezone.utc)
    if task.enhancement is not None:
        task.enhancement.status = EnhancementStatus.CANCELLED.value
        task.enhancement.auto_retry_after = None
        task.enhancement.finished_at = task.completed_at
        attempt = enhancement_attempt_for_remote_id(task.enhancement)
        if attempt is not None:
            attempt.status = "CANCELLED"
            attempt.error_code = "CANCELLED_BY_USER"
            attempt.error_message = task.error_message
            attempt.finished_at = task.completed_at
    else:
        finish_task_attempt(
            task,
            status="CANCELLED",
            error_code="CANCELLED_BY_USER",
            error_message=task.error_message,
        )


def cancel_generation_task(db: Session, task: GenerationTask) -> None:
    """Cancel local queued work or a confirmed RunningHub remote task."""

    if task.status == TaskStatus.CANCELLED.value:
        return
    if task.status in {
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.DOWNLOAD_FAILED.value,
    }:
        raise TaskCancellationError("已结束的任务不能取消")

    enhancement = task.enhancement
    if enhancement is not None and enhancement.status not in {
        EnhancementStatus.SUCCESS.value,
        EnhancementStatus.FAILED.value,
        EnhancementStatus.DOWNLOAD_FAILED.value,
        EnhancementStatus.CANCELLED.value,
    }:
        if not enhancement.remote_task_id:
            if enhancement.status == EnhancementStatus.UPLOADING.value:
                raise TaskCancellationError("清晰化源视频正在上传，请稍后再取消")
            _mark_cancelled(task)
            return
        account = (
            enhancement_execution_account_for_remote(task, enhancement)
            or task.user.runninghub_config
        )
        if account is None or not account.api_key_encrypted:
            raise TaskCancellationError("账号缺少 RunningHub API Key，无法远程取消")
        client = RunningHubClient(
            api_key=decrypt_secret(account.api_key_encrypted),
            base_url=account.base_url,
            ai_app_id=SEEDVR2_AI_APP_ID,
            submission_type="ai-app",
        )
        client.cancel_task(enhancement.remote_task_id)
        _mark_cancelled(task)
        return

    if task.status == TaskStatus.PENDING.value and not task.runninghub_task_id:
        result = db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task.id,
                GenerationTask.status == TaskStatus.PENDING.value,
                GenerationTask.runninghub_task_id.is_(None),
            )
            .values(
                status=TaskStatus.CANCELLED.value,
                error_code="CANCELLED_BY_USER",
                error_message="任务已由用户取消",
                runninghub_auto_retry_after=None,
                completed_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount == 1:
            finish_task_attempt(
                task,
                status="CANCELLED",
                error_code="CANCELLED_BY_USER",
                error_message="任务已由用户取消",
            )
            db.expire(task)
            return
        db.refresh(task)

    if task.status == TaskStatus.UPLOADING.value and not task.runninghub_task_id:
        raise TaskCancellationError("任务正在上传素材，请稍后再取消")
    if not task.runninghub_task_id:
        raise TaskCancellationError("任务状态正在变化，请刷新后重试")

    account = task_execution_account_for_remote(task) or task.user.runninghub_config
    if account is None or not account.api_key_encrypted:
        raise TaskCancellationError("账号缺少 RunningHub API Key，无法远程取消")
    try:
        workflow_config = get_user_workflow_config(task.user, task.workflow_type)
        adapter = get_workflow(task.workflow_type)
        ai_app_id = (
            account.digital_human_ai_app_id
            if isinstance(account, RunningHubExecutionAccount)
            else workflow_config.ai_app_id
        )
        client = RunningHubClient(
            api_key=decrypt_secret(account.api_key_encrypted),
            base_url=account.base_url,
            ai_app_id=ai_app_id,
            submission_type=adapter.submission_type,
        )
    except ValueError as exc:
        raise TaskCancellationError(
            "当前工作流配置不完整，无法远程取消"
        ) from exc
    client.cancel_task(task.runninghub_task_id)
    _mark_cancelled(task)
