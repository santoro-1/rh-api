from __future__ import annotations

import json
import hashlib
import logging
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    EnhancementStatus,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    GenerationTaskAttempt,
    GenerationTaskEnhancement,
    GenerationTaskEnhancementAttempt,
    RunningHubConfig,
    RunningHubExecutionAccount,
    TaskStatus,
    User,
)
from app.services.runninghub import (
    RunningHubClient,
    RunningHubError,
    runninghub_upload_diagnostics,
)
from app.services.runninghub_dispatch import (
    DispatchReservation,
    cool_execution_account,
    mark_execution_account_healthy,
    prepare_legacy_credential_fingerprints,
    release_unsubmitted_pool_reservation,
    reserve_legacy_task,
    reserve_pool_task,
    task_uses_execution_pool,
)
from app.services.runninghub_attempts import (
    SUBMIT_OUTCOME_UNKNOWN,
    enhancement_execution_account_for_remote,
    ensure_reserved_task_attempt,
    finish_task_attempt,
    latest_enhancement_attempt,
    latest_task_attempt,
    task_attempt_for_remote_id,
    task_execution_account_for_remote,
)
from app.services.logging_config import (
    configure_logging,
    log_event,
    start_heartbeat,
    write_heartbeat,
)
from app.services.audio import add_silence_tail
from app.services.security import decrypt_secret
from app.services.storage import (
    create_download_target,
    create_enhanced_download_target,
    create_source_download_target,
    safe_relative_path,
    to_relative_data_path,
)
from app.services.workflow_configs import get_user_workflow_config
from app.services.video_merge import (
    process_pending_video_merges,
    recover_interrupted_video_merges,
)
from app.workflows import get_workflow
from app.workflows.base import WorkflowAdapter, resolve_asset_path
from app.workflows.digital_human import generation_tail_padding_seconds
from app.workflows.seedvr2_upscale import (
    SEEDVR2_AI_APP_ID,
    seedvr2_upscale_workflow,
)


logger = logging.getLogger(__name__)
REMOTE_CAPACITY_RECHECK_SECONDS = 180.0
REMOTE_WATCHDOG_CANCEL_ERROR_CODE = "REMOTE_WATCHDOG_CANCEL_FAILED"
_capacity_check_after: dict[int, float] = {}
_remote_account_task_counts: dict[int, int] = {}

# Only these remote-work states consume a user's configured concurrency slot.
# PENDING remains in the shared FIFO but does not occupy RunningHub capacity.
SLOT_OCCUPYING_TASK_STATUSES = (
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capacity_check_is_deferred(user_id: int) -> bool:
    deadline = _capacity_check_after.get(user_id)
    if deadline is None:
        return False
    if deadline <= time.monotonic():
        _capacity_check_after.pop(user_id, None)
        return False
    return True


def _defer_capacity_check(user_id: int, seconds: float) -> None:
    _capacity_check_after[user_id] = time.monotonic() + max(seconds, 0.0)


def _allow_immediate_capacity_check(user_id: int) -> None:
    _capacity_check_after.pop(user_id, None)


def _video_log_context(task: GenerationTask) -> dict[str, object]:
    item = task.batch_item or (task.segment.batch_item if task.segment else None)
    batch = item.batch if item else None
    enhancement_attempt = (
        latest_enhancement_attempt(task.enhancement)
        if task.enhancement is not None
        else None
    )
    digital_attempt = latest_task_attempt(task)
    remote_stage = (
        "seedvr2"
        if task.enhancement is not None
        else "digital_human"
    )
    remote_attempt_number = (
        enhancement_attempt.attempt_number
        if remote_stage == "seedvr2" and enhancement_attempt is not None
        else (
            digital_attempt.attempt_number
            if digital_attempt is not None
            else None
        )
    )
    return {
        "user_id": task.user_id,
        "username": task.user.username if task.user else None,
        "task_id": task.id,
        "batch_id": batch.id if batch else None,
        "batch_item_id": item.id if item else None,
        "source_channel": batch.source_channel if batch else None,
        "correlation_id": (batch.correlation_id or batch.id) if batch else None,
        "remote_stage": remote_stage,
        "remote_attempt_number": remote_attempt_number,
        "execution_account_id": task.execution_account_id,
        "execution_account_label": (
            task.execution_account.label if task.execution_account else None
        ),
        "execution_account_max_concurrency": (
            task.execution_account.max_concurrent_tasks
            if task.execution_account
            else None
        ),
    }


def _make_client(
    config: RunningHubConfig | RunningHubExecutionAccount,
) -> RunningHubClient:
    """Build the account-level client; the selected adapter supplies the App ID."""

    ai_app_id = (
        config.digital_human_ai_app_id
        if isinstance(config, RunningHubExecutionAccount)
        else config.ai_app_id
    )
    return RunningHubClient(
        api_key=decrypt_secret(config.api_key_encrypted),
        base_url=config.base_url,
        ai_app_id=ai_app_id,
    )


def _runninghub_failed_reason(result: dict) -> dict | None:
    failed_reason = result.get("failedReason")
    return failed_reason if isinstance(failed_reason, dict) and failed_reason else None


def _runninghub_failure_message(result: dict) -> str:
    """Turn RunningHub's structured failure into a useful operator message."""

    failed_reason = _runninghub_failed_reason(result) or {}
    lines = [
        str(result.get("errorMessage") or "RunningHub 工作流运行失败").strip()
    ]
    exception_message = str(
        failed_reason.get("exception_message") or ""
    ).strip()
    if exception_message and exception_message not in lines:
        lines.append(exception_message)

    location: list[str] = []
    exception_type = str(
        failed_reason.get("exception_type") or ""
    ).strip()
    node_name = str(failed_reason.get("node_name") or "").strip()
    node_id = str(failed_reason.get("node_id") or "").strip()
    if exception_type:
        location.append(f"异常类型：{exception_type}")
    if node_name:
        location.append(f"失败节点：{node_name}")
    if node_id:
        location.append(f"节点 ID：{node_id}")
    if location:
        lines.append("；".join(location))
    return "\n".join(line for line in lines if line)


def _runninghub_attempt_history(task: GenerationTask) -> list[dict]:
    if not task.runninghub_attempt_history:
        return []
    try:
        value = json.loads(task.runninghub_attempt_history)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _record_runninghub_failure(
    task: GenerationTask,
    result: dict,
    *,
    message: str,
) -> None:
    history = _runninghub_attempt_history(task)
    history.append(
        {
            "taskId": task.runninghub_task_id,
            "status": "FAILED",
            "errorCode": str(result.get("errorCode") or "") or None,
            "errorMessage": message,
            "failedReason": _runninghub_failed_reason(result),
            "usage": result.get("usage"),
            "submittedAt": (
                task.runninghub_submitted_at.isoformat()
                if task.runninghub_submitted_at
                else None
            ),
            "failedAt": _now().isoformat(),
        }
    )
    # Keep enough evidence for repeated manual retry cycles without allowing
    # one pathological task to grow its database row forever.
    history = history[-50:]
    task.runninghub_attempt_history = json.dumps(
        history,
        ensure_ascii=False,
    )


def _schedule_runninghub_auto_retry(
    task: GenerationTask,
    *,
    message: str,
    release_pool_account: bool = False,
) -> tuple[str, int] | None:
    settings = get_settings()
    limit = settings.runninghub_auto_retry_limit
    completed_retries = int(task.runninghub_auto_retry_count or 0)
    if completed_retries >= limit:
        return None

    retry_number = completed_retries + 1
    delay_seconds = (
        settings.runninghub_auto_retry_base_delay_seconds
        * (2 ** (retry_number - 1))
    )
    previous_remote_id = str(task.runninghub_task_id or "")
    task.runninghub_auto_retry_count = retry_number
    task.runninghub_auto_retry_after = _now() + timedelta(
        seconds=delay_seconds
    )
    task.status = TaskStatus.PENDING.value
    task.error_message = (
        f"{message}\n"
        f"系统已安排第 {retry_number}/{limit} 次自动重试，"
        f"约 {delay_seconds} 秒后重新进入 RunningHub 队列。"
    )
    task.runninghub_task_id = None
    task.runninghub_submitted_at = None
    task.result_path = None
    task.output_metadata = None
    task.completed_at = None
    if release_pool_account:
        task.execution_account_id = None
        task.execution_account = None
    return previous_remote_id, delay_seconds


def _handle_safe_pre_submission_failure(
    db: Session,
    task: GenerationTask,
    *,
    code: str,
    message: str,
    diagnostics: dict[str, Any] | None = None,
    release_pool_account: bool = False,
) -> None:
    """Retry failures that happened before a remote task could be created."""

    log_context = _video_log_context(task)
    task.error_code = code
    scheduled = _schedule_runninghub_auto_retry(task, message=message)
    if scheduled is not None:
        _, delay_seconds = scheduled
        if release_pool_account:
            release_unsubmitted_pool_reservation(task)
        db.commit()
        log_event(
            logger,
            "video.pre_submit_retry_scheduled",
            "RunningHub 素材上传失败，已安排安全自动重试",
            level=logging.WARNING,
            workflow=task.workflow_type,
            auto_retry_count=task.runninghub_auto_retry_count,
            auto_retry_limit=get_settings().runninghub_auto_retry_limit,
            retry_after_seconds=delay_seconds,
            error_code=code,
            error=message,
            **(diagnostics or {}),
            **log_context,
        )
        return
    _mark_failed(
        task,
        code,
        (
            f"{message}\n"
            f"已用完 {get_settings().runninghub_auto_retry_limit} "
            "次自动重试，请检查网络或 RunningHub 服务后再决定是否人工重试。"
        ),
        diagnostics=diagnostics,
    )
    db.commit()


def _mark_failed(
    task: GenerationTask,
    code: str,
    message: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    """Persist a terminal local failure and emit one operator event."""

    task.status = TaskStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.runninghub_auto_retry_after = None
    task.completed_at = _now()
    log_event(
        logger,
        "video.failed",
        "视频任务失败",
        level=logging.WARNING,
        workflow=task.workflow_type,
        runninghub_task_id=task.runninghub_task_id,
        error_code=code,
        error=message,
        **(diagnostics or {}),
        **_video_log_context(task),
    )


def _mark_remote_cancelled(task: GenerationTask, message: str) -> None:
    """Close a task that RunningHub no longer exposes after manual cancel."""

    task.status = TaskStatus.CANCELLED.value
    task.error_code = "REMOTE_TASK_NOT_FOUND"
    task.error_message = message
    task.completed_at = _now()
    finish_task_attempt(
        task,
        status="CANCELLED",
        error_code="REMOTE_TASK_NOT_FOUND",
        error_message=message,
    )
    log_event(
        logger,
        "video.cancelled",
        "RunningHub 任务已不存在，本地任务已结束",
        level=logging.WARNING,
        runninghub_task_id=task.runninghub_task_id,
        error=message,
        **_video_log_context(task),
    )


def _remote_watchdog_has_expired(task: GenerationTask) -> bool:
    """Detect a remotely submitted task that exceeded the safety window."""

    started_at = task.runninghub_submitted_at or task.created_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (
        _now() - started_at
    ).total_seconds() > get_settings().runninghub_remote_watchdog_seconds


def _cancel_expired_remote_task(
    db: Session,
    task: GenerationTask,
    client: RunningHubClient,
) -> None:
    """Cancel a stale remote task before releasing its local billing slot."""

    assert task.runninghub_task_id
    watchdog_seconds = get_settings().runninghub_remote_watchdog_seconds
    try:
        client.cancel_task(task.runninghub_task_id)
    except RunningHubError as exc:
        message = (
            f"RunningHub 远程任务已超过 {watchdog_seconds // 3600} 小时安全等待上限，"
            "自动取消尚未成功；系统会保留任务并继续查询、重试取消。"
        )
        changed = (
            task.error_code != REMOTE_WATCHDOG_CANCEL_ERROR_CODE
            or task.error_message != message
        )
        task.error_code = REMOTE_WATCHDOG_CANCEL_ERROR_CODE
        task.error_message = message
        db.commit()
        if changed:
            log_event(
                logger,
                "video.watchdog_cancel_failed",
                "RunningHub 超时任务自动取消失败，将继续重试",
                level=logging.WARNING,
                runninghub_task_id=task.runninghub_task_id,
                watchdog_seconds=watchdog_seconds,
                error=str(exc),
                **exc.log_details(),
                **_video_log_context(task),
            )
        return

    _mark_failed(
        task,
        "REMOTE_WATCHDOG_TIMEOUT",
        (
            f"RunningHub 远程任务提交后超过 {watchdog_seconds // 3600} 小时仍未结束，"
            "已自动取消并关闭本地任务。"
        ),
        diagnostics={"watchdog_seconds": watchdog_seconds},
    )
    finish_task_attempt(
        task,
        status="WATCHDOG_CANCELLED",
        error_code="REMOTE_WATCHDOG_TIMEOUT",
        error_message=task.error_message,
    )
    db.commit()


def recover_interrupted_tasks(db: Session) -> int:
    """Reset only tasks that could not yet have been billed remotely."""

    interrupted = list(
        db.scalars(
            select(GenerationTask)
            .options(selectinload(GenerationTask.runninghub_attempts))
            .where(
                GenerationTask.status == TaskStatus.UPLOADING.value,
                GenerationTask.runninghub_task_id.is_(None),
            )
        ).all()
    )
    for task in interrupted:
        finish_task_attempt(
            task,
            status="INTERRUPTED_BEFORE_SUBMIT",
            error_code="WORKER_RESTARTED",
            error_message="Worker 在远程提交前重启，已安全释放本地预留",
        )

    result = db.execute(
        update(GenerationTask)
        .where(
            GenerationTask.status == TaskStatus.UPLOADING.value,
            GenerationTask.runninghub_task_id.is_(None),
        )
        .values(
            status=TaskStatus.PENDING.value,
            execution_account_id=None,
            error_code=None,
            error_message=None,
        )
    )
    enhancement_result = db.execute(
        update(GenerationTaskEnhancement)
        .where(
            GenerationTaskEnhancement.status == EnhancementStatus.UPLOADING.value,
            GenerationTaskEnhancement.remote_task_id.is_(None),
        )
        .values(
            status=EnhancementStatus.PENDING.value,
            error_message=None,
        )
    )
    db.commit()
    return int(result.rowcount or 0) + int(enhancement_result.rowcount or 0)


def claim_next_pending_task(db: Session) -> str | None:
    prepare_legacy_credential_fingerprints(db)
    # Global creation time remains the FIFO key. A task whose entire selected
    # pool is full/cooling is skipped so a healthy account or another user's
    # later task can still make progress.
    pending_tasks = list(
        db.scalars(
            select(GenerationTask)
            .options(
                selectinload(GenerationTask.user).selectinload(
                    User.runninghub_config
                ),
                selectinload(GenerationTask.user).selectinload(
                    User.workflow_configs
                ),
                selectinload(GenerationTask.execution_account),
                selectinload(GenerationTask.batch_item).selectinload(
                    GenerationBatchItem.batch
                ),
                selectinload(GenerationTask.segment)
                .selectinload(GenerationSegment.batch_item)
                .selectinload(GenerationBatchItem.batch),
            )
        .where(
            GenerationTask.status == TaskStatus.PENDING.value,
            or_(
                GenerationTask.runninghub_auto_retry_after.is_(None),
                GenerationTask.runninghub_auto_retry_after <= _now(),
            ),
        )
        .order_by(GenerationTask.created_at)
        ).all()
    )
    for pending_task in pending_tasks:
        reservation: DispatchReservation | None
        if task_uses_execution_pool(pending_task):
            reservation = reserve_pool_task(
                db,
                pending_task,
                remote_active_counts=_remote_account_task_counts,
            )
        else:
            if _capacity_check_is_deferred(pending_task.user_id):
                continue
            reservation = reserve_legacy_task(db, pending_task)
        if reservation is None:
            continue
        task = _load_task(db, reservation.task_id)
        if task is not None:
            ensure_reserved_task_attempt(task)
            db.commit()
        log_event(
            logger,
            "video.pool_reserved" if reservation.uses_pool else "video.claimed",
            (
                "视频 Worker 已从 RunningHub 资源池原子预留账号并领取任务"
                if reservation.uses_pool
                else "视频 Worker 已领取任务"
            ),
            **(
                _video_log_context(task)
                if task is not None
                else {"task_id": reservation.task_id}
            ),
            concurrency_limit=reservation.concurrency_limit,
            occupied_before_reservation=reservation.occupied_before_reservation,
        )
        return reservation.task_id
    return None


def _load_task(db: Session, task_id: str) -> GenerationTask | None:
    return db.scalar(
        select(GenerationTask)
        .options(
            selectinload(GenerationTask.user).selectinload(User.runninghub_config),
            selectinload(GenerationTask.user).selectinload(User.workflow_configs),
            selectinload(GenerationTask.execution_account),
            selectinload(GenerationTask.runninghub_attempts).selectinload(
                GenerationTaskAttempt.execution_account
            ),
            selectinload(GenerationTask.enhancement).selectinload(
                GenerationTaskEnhancement.attempts
            ),
            selectinload(GenerationTask.batch_item).selectinload(
                GenerationBatchItem.batch
            ),
            selectinload(GenerationTask.segment)
            .selectinload(GenerationSegment.batch_item)
            .selectinload(GenerationBatchItem.batch),
        )
        .where(GenerationTask.id == task_id)
    )


def _handle_remote_status(
    db: Session,
    task: GenerationTask,
    client: RunningHubClient,
    workflow: WorkflowAdapter,
) -> None:
    assert task.runninghub_task_id
    try:
        result = client.query_task(task.runninghub_task_id)
    except RunningHubError as exc:
        if exc.is_task_not_found:
            if task.execution_account:
                mark_execution_account_healthy(
                    task.execution_account, clear_cooldown=False
                )
                _remote_account_task_counts.pop(task.execution_account.id, None)
            _mark_remote_cancelled(
                task,
                "RunningHub 返回任务不存在或已过期，可能已在平台手动取消",
            )
            db.commit()
            return
        if task.execution_account:
            cool_execution_account(
                task.execution_account,
                error_code=str(exc.error_code or "QUERY_ERROR"),
                cooldown_seconds=REMOTE_CAPACITY_RECHECK_SECONDS,
                unhealthy=True,
            )
        # A query failure must never lead to a second paid submission.
        watchdog_cancel_pending = (
            task.error_code == REMOTE_WATCHDOG_CANCEL_ERROR_CODE
        )
        changed = not watchdog_cancel_pending and (
            task.error_code != "QUERY_ERROR"
            or task.error_message != str(exc)
        )
        if not watchdog_cancel_pending:
            task.error_code = "QUERY_ERROR"
            task.error_message = str(exc)
        db.commit()
        if changed:
            log_event(
                logger,
                "video.query_error",
                "RunningHub 状态查询失败，将保留远程任务继续查询",
                level=logging.WARNING,
                runninghub_task_id=task.runninghub_task_id,
                error=str(exc),
                **exc.log_details(),
                **_video_log_context(task),
            )
        return

    if task.execution_account:
        mark_execution_account_healthy(
            task.execution_account, clear_cooldown=False
        )
    status = str(result.get("status") or "").upper()
    previous_status = task.status
    remote_error_code = str(result.get("errorCode") or "") or None
    remote_error_message = str(result.get("errorMessage") or "") or None
    failed_reason = _runninghub_failed_reason(result)
    task.runninghub_failed_reason = (
        json.dumps(failed_reason, ensure_ascii=False)
        if failed_reason is not None
        else None
    )
    usage = result.get("usage")
    task.runninghub_usage = json.dumps(usage, ensure_ascii=False) if usage is not None else None

    if status in {"QUEUED", "RUNNING"}:
        # Preserve a failed watchdog cancellation so operators can see why an
        # otherwise active task is being cancelled again on every poll cycle.
        if task.error_code != REMOTE_WATCHDOG_CANCEL_ERROR_CODE:
            task.error_code = remote_error_code
            task.error_message = remote_error_message
        task.status = (
            TaskStatus.RUNNING.value if status == "RUNNING" else TaskStatus.SUBMITTED.value
        )
        attempt = task_attempt_for_remote_id(task)
        if attempt is not None:
            attempt.status = status
        db.commit()
        if task.status != previous_status:
            log_event(
                logger,
                "video.remote_status",
                "RunningHub 任务状态已变化",
                runninghub_task_id=task.runninghub_task_id,
                previous_status=previous_status,
                status=task.status,
                **_video_log_context(task),
            )
        return
    if task.execution_account:
        _remote_account_task_counts.pop(task.execution_account.id, None)
    task.error_code = remote_error_code
    task.error_message = remote_error_message
    if status == "FAILED":
        failure_message = _runninghub_failure_message(result)
        _record_runninghub_failure(
            task,
            result,
            message=failure_message,
        )
        finish_task_attempt(
            task,
            status="FAILED",
            error_code=task.error_code,
            error_message=failure_message,
            failed_reason=failed_reason,
        )
        scheduled = _schedule_runninghub_auto_retry(
            task,
            message=failure_message,
            release_pool_account=task.execution_account_id is not None,
        )
        if scheduled is not None:
            previous_remote_id, delay_seconds = scheduled
            db.commit()
            log_event(
                logger,
                "video.auto_retry_scheduled",
                "RunningHub 任务失败，已安排自动重试",
                level=logging.WARNING,
                workflow=task.workflow_type,
                previous_runninghub_task_id=previous_remote_id,
                auto_retry_count=task.runninghub_auto_retry_count,
                auto_retry_limit=get_settings().runninghub_auto_retry_limit,
                retry_after_seconds=delay_seconds,
                error_code=task.error_code,
                error=failure_message,
                **_video_log_context(task),
            )
            return
        _mark_failed(
            task,
            task.error_code or "RUNNINGHUB_FAILED",
            (
                f"{failure_message}\n"
                f"已用完 {get_settings().runninghub_auto_retry_limit} "
                "次自动重试，请人工检查后再决定是否重试。"
            ),
        )
        db.commit()
        return
    if status != "SUCCESS":
        _mark_failed(task, "UNKNOWN_STATUS", f"RunningHub 返回未知任务状态：{status or '空'}")
        finish_task_attempt(
            task,
            status="UNKNOWN_STATUS",
            error_code="UNKNOWN_STATUS",
            error_message=task.error_message,
        )
        db.commit()
        return

    output = workflow.select_output(task, result)
    if output is None:
        _mark_failed(task, "EMPTY_RESULT", "工作流成功但没有可下载的结果")
        finish_task_attempt(
            task,
            status="INVALID_OUTPUT",
            error_code="EMPTY_RESULT",
            error_message=task.error_message,
        )
        db.commit()
        return
    destination = (
        create_source_download_target(
            get_settings(), task.user_id, task.id, output.extension
        )
        if task.workflow_type == "digital_human"
        else create_download_target(
            get_settings(), task.user_id, task.id, output.extension
        )
    )
    log_event(
        logger,
        "video.download_started",
        "RunningHub 已返回结果，开始下载视频",
        runninghub_task_id=task.runninghub_task_id,
        **_video_log_context(task),
    )
    try:
        client.download_result(output.url, destination)
    except RunningHubError as exc:
        task.status = TaskStatus.DOWNLOAD_FAILED.value
        task.error_code = "DOWNLOAD_FAILED"
        task.error_message = str(exc)
        task.completed_at = _now()
        finish_task_attempt(
            task,
            status="DOWNLOAD_FAILED",
            error_code="DOWNLOAD_FAILED",
            error_message=str(exc),
        )
        db.commit()
        log_event(
            logger,
            "video.download_failed",
            "视频结果下载失败",
            level=logging.WARNING,
            runninghub_task_id=task.runninghub_task_id,
            error=str(exc),
            **exc.log_details(),
            **_video_log_context(task),
        )
        return
    if task.workflow_type == "digital_human":
        enhancement = task.enhancement
        if enhancement is None:
            enhancement = GenerationTaskEnhancement(
                id=str(uuid.uuid4()),
                generation_task_id=task.id,
                status=EnhancementStatus.PENDING.value,
                source_result_path=to_relative_data_path(destination, get_settings()),
                source_filename=destination.name,
                source_size=destination.stat().st_size,
                source_sha256=_file_sha256(destination),
                source_output_metadata_json=json.dumps(
                    output.metadata, ensure_ascii=False
                ),
                execution_account_id=task.execution_account_id,
            )
            db.add(enhancement)
        task.status = TaskStatus.RUNNING.value
        task.result_path = None
        task.output_metadata = None
        task.error_code = None
        task.error_message = None
        task.runninghub_failed_reason = None
        task.runninghub_auto_retry_after = None
        task.completed_at = None
        finish_task_attempt(task, status="SUCCESS")
        db.commit()
        log_event(
            logger,
            "video.enhancement_queued",
            "数字人源片段已保存，等待 SeedVR2 48G 清晰化",
            runninghub_task_id=task.runninghub_task_id,
            source_result_path=enhancement.source_result_path,
            **_video_log_context(task),
        )
        return

    task.result_path = to_relative_data_path(destination, get_settings())
    task.output_metadata = json.dumps(output.metadata, ensure_ascii=False)
    task.status = TaskStatus.SUCCESS.value
    task.error_code = None
    task.error_message = None
    task.runninghub_failed_reason = None
    task.runninghub_auto_retry_after = None
    task.completed_at = _now()
    finish_task_attempt(task, status="SUCCESS")
    db.commit()
    log_event(
        logger,
        "video.completed",
        "视频任务完成",
        runninghub_task_id=task.runninghub_task_id,
        result_path=task.result_path,
        **_video_log_context(task),
    )


def _enhancement_attempt(
    enhancement: GenerationTaskEnhancement,
) -> GenerationTaskEnhancementAttempt | None:
    return enhancement.attempts[-1] if enhancement.attempts else None


def _new_enhancement_attempt(
    enhancement: GenerationTaskEnhancement,
) -> GenerationTaskEnhancementAttempt:
    attempt = GenerationTaskEnhancementAttempt(
        id=str(uuid.uuid4()),
        enhancement_id=enhancement.id,
        attempt_number=len(enhancement.attempts) + 1,
        execution_account_id=enhancement.execution_account_id,
        status="UPLOADING",
    )
    enhancement.attempts.append(attempt)
    return attempt


def _enhancement_failure(
    task: GenerationTask,
    enhancement: GenerationTaskEnhancement,
    *,
    code: str,
    message: str,
) -> None:
    enhancement.status = EnhancementStatus.FAILED.value
    enhancement.error_message = message
    enhancement.auto_retry_after = None
    enhancement.finished_at = _now()
    task.status = TaskStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.completed_at = _now()


def _schedule_enhancement_retry(
    task: GenerationTask,
    enhancement: GenerationTaskEnhancement,
    *,
    message: str,
) -> tuple[int, int] | None:
    settings = get_settings()
    completed_retries = int(enhancement.auto_retry_count or 0)
    if completed_retries >= settings.runninghub_auto_retry_limit:
        return None
    retry_number = completed_retries + 1
    delay_seconds = settings.runninghub_auto_retry_base_delay_seconds * (
        2 ** (retry_number - 1)
    )
    enhancement.auto_retry_count = retry_number
    enhancement.auto_retry_after = _now() + timedelta(seconds=delay_seconds)
    enhancement.remote_task_id = None
    enhancement.submitted_at = None
    enhancement.status = EnhancementStatus.PENDING.value
    enhancement.error_message = (
        f"{message}\n系统已安排 SeedVR2 第 {retry_number}/"
        f"{settings.runninghub_auto_retry_limit} 次自动重试，约 {delay_seconds} 秒后执行。"
    )
    task.status = TaskStatus.RUNNING.value
    task.error_code = None
    task.error_message = enhancement.error_message
    task.completed_at = None
    return retry_number, delay_seconds


def _enhancement_watchdog_expired(
    enhancement: GenerationTaskEnhancement,
) -> bool:
    started_at = enhancement.submitted_at or enhancement.created_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (
        _now() - started_at
    ).total_seconds() > get_settings().runninghub_remote_watchdog_seconds


def _finish_enhancement_attempt(
    enhancement: GenerationTaskEnhancement,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    failed_reason: dict[str, Any] | None = None,
) -> None:
    attempt = _enhancement_attempt(enhancement)
    if attempt is None:
        return
    attempt.status = status
    attempt.error_code = error_code
    attempt.error_message = error_message
    attempt.failed_reason_json = (
        json.dumps(failed_reason, ensure_ascii=False)
        if failed_reason is not None
        else None
    )
    attempt.finished_at = _now()


def _handle_enhancement_remote_status(
    db: Session,
    task: GenerationTask,
    enhancement: GenerationTaskEnhancement,
    client: RunningHubClient,
) -> None:
    assert enhancement.remote_task_id
    try:
        result = client.query_task(enhancement.remote_task_id)
    except RunningHubError as exc:
        if exc.is_task_not_found:
            _finish_enhancement_attempt(
                enhancement,
                status="MISSING",
                error_code=str(exc.error_code or "REMOTE_TASK_NOT_FOUND"),
                error_message=str(exc),
            )
            _enhancement_failure(
                task,
                enhancement,
                code="VIDEO_ENHANCEMENT_REMOTE_MISSING",
                message="SeedVR2 任务不存在或已过期，可使用已保存的数字人源片段重试清晰化",
            )
            db.commit()
            return
        enhancement.error_message = str(exc)
        task.error_code = "VIDEO_ENHANCEMENT_QUERY_ERROR"
        task.error_message = "SeedVR2 状态查询暂时失败，系统会保留远端任务继续查询"
        db.commit()
        return

    status = str(result.get("status") or "").upper()
    failed_reason = _runninghub_failed_reason(result)
    enhancement.failed_reason_json = (
        json.dumps(failed_reason, ensure_ascii=False)
        if failed_reason is not None
        else None
    )
    usage = result.get("usage")
    enhancement.usage_json = (
        json.dumps(usage, ensure_ascii=False) if usage is not None else None
    )
    if status in {"QUEUED", "RUNNING"}:
        enhancement.status = (
            EnhancementStatus.RUNNING.value
            if status == "RUNNING"
            else EnhancementStatus.SUBMITTED.value
        )
        if enhancement.started_at is None and status == "RUNNING":
            enhancement.started_at = _now()
        task.status = TaskStatus.RUNNING.value
        task.error_code = None
        task.error_message = None
        attempt = _enhancement_attempt(enhancement)
        if attempt is not None:
            attempt.status = status
        db.commit()
        if _enhancement_watchdog_expired(enhancement):
            try:
                client.cancel_task(enhancement.remote_task_id)
            except RunningHubError as exc:
                task.error_code = "VIDEO_ENHANCEMENT_WATCHDOG_CANCEL_FAILED"
                task.error_message = (
                    "SeedVR2 已超过安全等待上限，自动取消尚未成功；"
                    "系统会保留远端任务继续查询。"
                )
                enhancement.error_message = task.error_message
                db.commit()
                return
            _finish_enhancement_attempt(
                enhancement,
                status="WATCHDOG_CANCELLED",
                error_code="REMOTE_WATCHDOG_TIMEOUT",
            )
            _enhancement_failure(
                task,
                enhancement,
                code="VIDEO_ENHANCEMENT_WATCHDOG_TIMEOUT",
                message="SeedVR2 超过安全等待上限，已取消远端任务",
            )
            db.commit()
        return

    if task.execution_account:
        _remote_account_task_counts.pop(task.execution_account.id, None)
    if status == "FAILED":
        message = _runninghub_failure_message(result)
        _finish_enhancement_attempt(
            enhancement,
            status="FAILED",
            error_code=str(result.get("errorCode") or "") or None,
            error_message=message,
            failed_reason=failed_reason,
        )
        scheduled = _schedule_enhancement_retry(
            task, enhancement, message=message
        )
        if scheduled is None:
            _enhancement_failure(
                task,
                enhancement,
                code="VIDEO_ENHANCEMENT_FAILED",
                message=(
                    f"{message}\n已用完 {get_settings().runninghub_auto_retry_limit} "
                    "次 SeedVR2 自动重试，请人工检查后再重试清晰化。"
                ),
            )
        db.commit()
        return
    if status != "SUCCESS":
        _finish_enhancement_attempt(
            enhancement,
            status="UNKNOWN_STATUS",
            error_message=f"RunningHub 返回未知任务状态：{status or '空'}",
        )
        _enhancement_failure(
            task,
            enhancement,
            code="VIDEO_ENHANCEMENT_UNKNOWN_STATUS",
            message=f"SeedVR2 返回未知任务状态：{status or '空'}",
        )
        db.commit()
        return

    output = seedvr2_upscale_workflow.select_output(result)
    if output is None:
        _finish_enhancement_attempt(
            enhancement,
            status="INVALID_OUTPUT",
            error_code="EMPTY_OR_AMBIGUOUS_VIDEO_RESULT",
        )
        _enhancement_failure(
            task,
            enhancement,
            code="VIDEO_ENHANCEMENT_INVALID_OUTPUT",
            message="SeedVR2 已成功但没有唯一可确认的视频结果，未自动重复付费提交",
        )
        db.commit()
        return

    destination = create_enhanced_download_target(
        get_settings(), task.user_id, task.id, output.extension
    )
    try:
        client.download_result(output.url, destination)
    except RunningHubError as exc:
        enhancement.status = EnhancementStatus.DOWNLOAD_FAILED.value
        enhancement.error_message = str(exc)
        task.status = TaskStatus.DOWNLOAD_FAILED.value
        task.error_code = "VIDEO_ENHANCEMENT_DOWNLOAD_FAILED"
        task.error_message = str(exc)
        task.completed_at = _now()
        _finish_enhancement_attempt(
            enhancement,
            status="DOWNLOAD_FAILED",
            error_code="VIDEO_ENHANCEMENT_DOWNLOAD_FAILED",
            error_message=str(exc),
        )
        db.commit()
        return

    enhancement.status = EnhancementStatus.SUCCESS.value
    enhancement.result_path = to_relative_data_path(destination, get_settings())
    enhancement.result_filename = destination.name
    enhancement.result_size = destination.stat().st_size
    enhancement.result_sha256 = _file_sha256(destination)
    enhancement.output_metadata_json = json.dumps(
        output.metadata, ensure_ascii=False
    )
    enhancement.error_message = None
    enhancement.failed_reason_json = None
    enhancement.auto_retry_after = None
    enhancement.finished_at = _now()
    _finish_enhancement_attempt(enhancement, status="SUCCESS")
    task.result_path = enhancement.result_path
    task.output_metadata = json.dumps(
        {
            "quality_variant": "seedvr2_upscaled",
            "enhanced_by": "runninghub_seedvr2",
            "seedvr2": output.metadata,
        },
        ensure_ascii=False,
    )
    task.status = TaskStatus.SUCCESS.value
    task.error_code = None
    task.error_message = None
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "video.enhancement_completed",
        "SeedVR2 48G 清晰化完成",
        seedvr2_task_id=enhancement.remote_task_id,
        result_path=task.result_path,
        **_video_log_context(task),
    )


def _process_enhancement(
    db: Session,
    task: GenerationTask,
    enhancement: GenerationTaskEnhancement,
    client: RunningHubClient,
    execution_config: RunningHubConfig | RunningHubExecutionAccount,
) -> None:
    if enhancement.status == EnhancementStatus.SUCCESS.value:
        if enhancement.result_path and not task.result_path:
            task.result_path = enhancement.result_path
            task.status = TaskStatus.SUCCESS.value
            task.completed_at = enhancement.finished_at or _now()
            db.commit()
        return
    if enhancement.status in {
        EnhancementStatus.FAILED.value,
        EnhancementStatus.DOWNLOAD_FAILED.value,
        EnhancementStatus.CANCELLED.value,
    }:
        return
    if enhancement.remote_task_id:
        _handle_enhancement_remote_status(db, task, enhancement, client)
        return
    if enhancement.auto_retry_after is not None:
        retry_after = enhancement.auto_retry_after
        if retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=timezone.utc)
        if retry_after > _now():
            return

    source_path = safe_relative_path(
        enhancement.source_result_path, get_settings().data_dir
    )
    if not source_path.is_file():
        _enhancement_failure(
            task,
            enhancement,
            code="VIDEO_ENHANCEMENT_SOURCE_MISSING",
            message="数字人源片段不存在，无法执行 SeedVR2 清晰化",
        )
        db.commit()
        return

    try:
        current_tasks = client.get_account_current_task_count()
    except RunningHubError as exc:
        enhancement.error_message = str(exc)
        task.error_code = "VIDEO_ENHANCEMENT_CAPACITY_QUERY_FAILED"
        task.error_message = "暂时无法读取 SeedVR2 执行账号容量，将继续等待"
        db.commit()
        return
    limit = int(execution_config.max_concurrent_tasks)
    if current_tasks >= limit:
        return

    enhancement.status = EnhancementStatus.UPLOADING.value
    enhancement.error_message = None
    task.status = TaskStatus.RUNNING.value
    task.error_code = None
    task.error_message = None
    attempt = _new_enhancement_attempt(enhancement)
    db.commit()
    try:
        uploaded_video = client.upload_file(source_path)
        payload = seedvr2_upscale_workflow.build_payload(uploaded_video)
        attempt.payload_summary_json = json.dumps(
            {
                "ai_app_id": SEEDVR2_AI_APP_ID,
                "instance_type": payload["instanceType"],
                "node_ids": [node["nodeId"] for node in payload["nodeInfoList"]],
            },
            ensure_ascii=False,
        )
        remote_task_id = client.submit_task(payload)
    except RunningHubError as exc:
        _finish_enhancement_attempt(
            enhancement,
            status=(
                "PRE_SUBMISSION_FAILED"
                if exc.retry_safe
                else (
                    "SUBMIT_UNKNOWN"
                    if exc.submission_outcome_unknown
                    else "SUBMISSION_REJECTED"
                )
            ),
            error_code=str(exc.error_code or "") or None,
            error_message=str(exc),
        )
        if exc.is_capacity_limited:
            attempt.status = "CAPACITY_WAIT"
            enhancement.status = EnhancementStatus.PENDING.value
            enhancement.auto_retry_after = _now() + timedelta(
                seconds=REMOTE_CAPACITY_RECHECK_SECONDS
            )
            enhancement.error_message = "SeedVR2 执行账号并发已满，将继续等待"
            task.status = TaskStatus.RUNNING.value
            task.error_code = None
            task.error_message = enhancement.error_message
        elif exc.retry_safe:
            scheduled = _schedule_enhancement_retry(
                task, enhancement, message=str(exc)
            )
            if scheduled is None:
                _enhancement_failure(
                    task,
                    enhancement,
                    code="VIDEO_ENHANCEMENT_UPLOAD_FAILED",
                    message=str(exc),
                )
        elif exc.submission_outcome_unknown:
            _enhancement_failure(
                task,
                enhancement,
                code=SUBMIT_OUTCOME_UNKNOWN,
                message=(
                    f"{exc}\nSeedVR2 提交结果无法确认，未自动重复付费提交。"
                ),
            )
        else:
            scheduled = _schedule_enhancement_retry(
                task, enhancement, message=str(exc)
            )
            if scheduled is None:
                _enhancement_failure(
                    task,
                    enhancement,
                    code="VIDEO_ENHANCEMENT_SUBMISSION_REJECTED",
                    message=str(exc),
                )
        db.commit()
        return
    except (OSError, ValueError) as exc:
        _finish_enhancement_attempt(
            enhancement,
            status="PRE_SUBMISSION_FAILED",
            error_message=str(exc),
        )
        scheduled = _schedule_enhancement_retry(
            task, enhancement, message=str(exc)
        )
        if scheduled is None:
            _enhancement_failure(
                task,
                enhancement,
                code="VIDEO_ENHANCEMENT_UPLOAD_FAILED",
                message=str(exc),
            )
        db.commit()
        return

    enhancement.remote_task_id = remote_task_id
    enhancement.status = EnhancementStatus.SUBMITTED.value
    enhancement.submitted_at = _now()
    enhancement.started_at = None
    enhancement.error_message = None
    enhancement.auto_retry_after = None
    attempt.remote_task_id = remote_task_id
    attempt.status = "SUBMITTED"
    attempt.submitted_at = enhancement.submitted_at
    task.status = TaskStatus.RUNNING.value
    task.error_code = None
    task.error_message = None
    if task.execution_account:
        _remote_account_task_counts[task.execution_account.id] = (
            _remote_account_task_counts.get(task.execution_account.id, 0) + 1
        )
    db.commit()
    log_event(
        logger,
        "video.enhancement_submitted",
        "数字人源片段已提交 SeedVR2 48G 清晰化",
        seedvr2_task_id=remote_task_id,
        **_video_log_context(task),
    )


def _return_to_capacity_queue(
    db: Session,
    task: GenerationTask,
    *,
    reason: str,
    event_code: str,
    level: int = logging.INFO,
    remote_current_tasks: int | None = None,
    concurrency_limit: int | None = None,
    diagnostics: dict[str, Any] | None = None,
    release_pool_account: bool = False,
) -> None:
    log_context = _video_log_context(task)
    pool_account = task.execution_account
    task.status = TaskStatus.PENDING.value
    task.error_code = None
    task.error_message = None
    task.completed_at = None
    if release_pool_account:
        release_unsubmitted_pool_reservation(task)
    else:
        _defer_capacity_check(task.user_id, REMOTE_CAPACITY_RECHECK_SECONDS)
    db.commit()
    log_event(
        logger,
        event_code,
        reason,
        level=level,
        remote_current_tasks=remote_current_tasks,
        concurrency_limit=concurrency_limit,
        retry_after_seconds=int(REMOTE_CAPACITY_RECHECK_SECONDS),
        **(diagnostics or {}),
        **log_context,
    )
    if pool_account is not None and release_pool_account:
        log_event(
            logger,
            "video.pool_reservation_released",
            "未创建远程任务，已释放 RunningHub 执行账号预留",
            execution_account_id=pool_account.id,
            execution_account_label=pool_account.label,
            **{key: value for key, value in log_context.items() if not key.startswith("execution_account")},
        )


def _remote_capacity_is_available(
    db: Session,
    task: GenerationTask,
    client: RunningHubClient,
    config: RunningHubConfig | RunningHubExecutionAccount,
) -> bool:
    limit = max(int(config.max_concurrent_tasks), 1)
    pool_account = (
        config if isinstance(config, RunningHubExecutionAccount) else None
    )
    try:
        current_tasks = client.get_account_current_task_count()
    except RunningHubError as exc:
        if pool_account is not None:
            cool_execution_account(
                pool_account,
                error_code=str(exc.error_code or "CAPACITY_CHECK_ERROR"),
                cooldown_seconds=REMOTE_CAPACITY_RECHECK_SECONDS,
                unhealthy=True,
            )
        _return_to_capacity_queue(
            db,
            task,
            reason="读取 RunningHub 账号并发状态失败，任务将保留排队",
            event_code="video.capacity_check_error",
            level=logging.WARNING,
            diagnostics=exc.log_details(),
            release_pool_account=pool_account is not None,
        )
        return False
    if pool_account is not None:
        _remote_account_task_counts[pool_account.id] = current_tasks
        mark_execution_account_healthy(pool_account)
    if current_tasks >= limit:
        if pool_account is not None:
            cool_execution_account(
                pool_account,
                error_code="CAPACITY_FULL",
                cooldown_seconds=REMOTE_CAPACITY_RECHECK_SECONDS,
                unhealthy=False,
            )
        _return_to_capacity_queue(
            db,
            task,
            reason="RunningHub 账号并发已满，任务将保留排队",
            event_code="video.capacity_waiting",
            remote_current_tasks=current_tasks,
            concurrency_limit=limit,
            release_pool_account=pool_account is not None,
        )
        return False
    if pool_account is not None:
        db.commit()
    return True


def process_task(db: Session, task_id: str) -> None:
    task = _load_task(db, task_id)
    if not task or task.status in {
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return
    if not task.user or not task.user.is_active:
        _mark_failed(task, "CONFIGURATION_ERROR", "账号已禁用")
        db.commit()
        return
    uses_pool = task_uses_execution_pool(task)
    if uses_pool:
        bound_pool_account = (
            enhancement_execution_account_for_remote(
                task, task.enhancement
            )
            if task.enhancement is not None
            else (
                task_execution_account_for_remote(task)
                if task.runninghub_task_id
                else task.execution_account
            )
        )
        if bound_pool_account is None:
            _mark_failed(
                task,
                "POOL_ACCOUNT_BINDING_MISSING",
                "新版工作台资源池任务缺少已预留的 RunningHub 执行账号",
            )
            db.commit()
            return
        execution_config: RunningHubConfig | RunningHubExecutionAccount = (
            bound_pool_account
        )
    else:
        if task.user.runninghub_config is None:
            _mark_failed(task, "CONFIGURATION_ERROR", "RunningHub 配置缺失")
            db.commit()
            return
        execution_config = task.user.runninghub_config
    try:
        workflow = get_workflow(task.workflow_type)
    except ValueError as exc:
        _mark_failed(task, "WORKFLOW_UNSUPPORTED", str(exc))
        db.commit()
        return
    workflow_config = get_user_workflow_config(task.user, workflow.key)
    if not uses_pool and not workflow_config.is_enabled:
        _mark_failed(task, "WORKFLOW_DISABLED", f"工作流未为该账号启用：{workflow.key}")
        db.commit()
        return
    try:
        client = _make_client(execution_config)
        effective_ai_app_id = (
            execution_config.digital_human_ai_app_id
            if uses_pool
            and isinstance(execution_config, RunningHubExecutionAccount)
            else workflow_config.ai_app_id
        )
        # RunningHubClient is generic.  The workflow config determines only
        # which AI App the generic submit endpoint targets.
        client.ai_app_id = effective_ai_app_id
        client.submission_type = workflow.submission_type
    except (ValueError, RunningHubError) as exc:
        _mark_failed(task, "CONFIGURATION_ERROR", str(exc))
        db.commit()
        return

    if task.workflow_type == "digital_human" and task.enhancement is not None:
        if (
            task.enhancement.execution_account_id
            != task.execution_account_id
        ):
            _enhancement_failure(
                task,
                task.enhancement,
                code="VIDEO_ENHANCEMENT_ACCOUNT_MISMATCH",
                message=(
                    "数字人和 SeedVR2 执行账号绑定不一致，已停止处理以避免跨账号付费"
                ),
            )
            db.commit()
            return
        client.ai_app_id = SEEDVR2_AI_APP_ID
        client.submission_type = seedvr2_upscale_workflow.submission_type
        _process_enhancement(
            db,
            task,
            task.enhancement,
            client,
            execution_config,
        )
        return

    if task.runninghub_task_id:
        if task_attempt_for_remote_id(task) is None:
            attempt = ensure_reserved_task_attempt(task)
            attempt.remote_task_id = task.runninghub_task_id
            attempt.status = task.status
            attempt.submitted_at = task.runninghub_submitted_at
            db.commit()
        # RunningHub owns queueing, execution and its normal timeout. Always
        # consume the newest remote state before applying our much larger
        # stuck-task watchdog, otherwise a just-finished task can be misclosed.
        _handle_remote_status(db, task, client, workflow)
        if (
            task.status in SLOT_OCCUPYING_TASK_STATUSES
            and _remote_watchdog_has_expired(task)
        ):
            _cancel_expired_remote_task(db, task, client)
        if task.status not in SLOT_OCCUPYING_TASK_STATUSES:
            _allow_immediate_capacity_check(task.user_id)
        return

    if not _remote_capacity_is_available(
        db,
        task,
        client,
        execution_config,
    ):
        return

    try:
        settings = get_settings()
        attempt = ensure_reserved_task_attempt(task)
        attempt.status = "UPLOADING"
        db.commit()
        log_event(
            logger,
            "video.upload_started",
            "开始上传视频任务素材",
            workflow=task.workflow_type,
            **_video_log_context(task),
        )
        uploaded_files = {}
        with tempfile.TemporaryDirectory(prefix="runninghub-upload-") as work_dir:
            for asset in workflow.assets_for_task(task):
                asset_path = resolve_asset_path(asset, settings)
                upload_path = asset_path
                if task.workflow_type == "digital_human" and asset.name == "audio":
                    padding_seconds = generation_tail_padding_seconds(task)
                    if padding_seconds > 0:
                        upload_path = Path(work_dir) / "audio-with-tail.mp3"
                        add_silence_tail(
                            asset_path,
                            upload_path,
                            padding_seconds=padding_seconds,
                        )
                try:
                    uploaded_files[asset.name] = client.upload_file(upload_path)
                except RunningHubError as exc:
                    exc.diagnostics.setdefault("asset_slot", asset.name)
                    exc.diagnostics.setdefault(
                        "asset_original_name",
                        asset.original_name,
                    )
                    if upload_path.is_file():
                        for key, value in runninghub_upload_diagnostics(
                            upload_path.stat().st_size
                        ).items():
                            exc.diagnostics.setdefault(key, value)
                    raise
        payload = workflow.build_payload(
            task,
            uploaded_files,
            ai_app_id=effective_ai_app_id,
            instance_type=("default" if uses_pool else workflow_config.instance_type),
            settings=({} if uses_pool else workflow_config.settings),
        )
        remote_task_id = client.submit_task(payload)
        # Persist as soon as submission returns. Every later run only queries this ID.
        task.runninghub_task_id = remote_task_id
        task.runninghub_submitted_at = _now()
        task.status = TaskStatus.SUBMITTED.value
        task.error_code = None
        task.error_message = None
        task.runninghub_failed_reason = None
        task.runninghub_usage = None
        task.runninghub_auto_retry_after = None
        attempt.remote_task_id = remote_task_id
        attempt.status = "SUBMITTED"
        attempt.submitted_at = task.runninghub_submitted_at
        if task.execution_account:
            mark_execution_account_healthy(task.execution_account)
            _remote_account_task_counts[task.execution_account.id] = (
                _remote_account_task_counts.get(task.execution_account.id, 0) + 1
            )
        db.commit()
        log_event(
            logger,
            "video.submitted",
            "视频任务已提交 RunningHub",
            workflow=task.workflow_type,
            runninghub_task_id=remote_task_id,
            **_video_log_context(task),
        )
        # Let RunningHub accountStatus catch up before considering another
        # task for this user. Other users remain eligible in the same pass.
        if not uses_pool:
            _defer_capacity_check(task.user_id, get_settings().poll_interval_seconds)
    except RunningHubError as exc:
        if exc.is_capacity_limited:
            finish_task_attempt(
                task,
                status="CAPACITY_WAIT",
                error_code=str(exc.error_code or "CAPACITY_FULL"),
                error_message=str(exc),
            )
            if task.execution_account:
                cool_execution_account(
                    task.execution_account,
                    error_code="CAPACITY_FULL",
                    cooldown_seconds=REMOTE_CAPACITY_RECHECK_SECONDS,
                    unhealthy=False,
                )
            _return_to_capacity_queue(
                db,
                task,
                reason="RunningHub 在提交时报告并发已满，任务将保留排队",
                event_code="video.capacity_waiting",
                release_pool_account=task.execution_account is not None,
            )
            return
        if task.execution_account:
            cool_execution_account(
                task.execution_account,
                error_code=str(exc.error_code or "SUBMIT_FAILED"),
                cooldown_seconds=REMOTE_CAPACITY_RECHECK_SECONDS,
                unhealthy=True,
            )
        if exc.retry_safe and task.runninghub_task_id is None:
            finish_task_attempt(
                task,
                status="PRE_SUBMISSION_FAILED",
                error_code=str(exc.error_code or "SUBMIT_FAILED"),
                error_message=str(exc),
            )
            _handle_safe_pre_submission_failure(
                db,
                task,
                code="SUBMIT_FAILED",
                message=str(exc),
                diagnostics=exc.log_details(),
                release_pool_account=task.execution_account is not None,
            )
            return
        if exc.submission_outcome_unknown and task.runninghub_task_id is None:
            finish_task_attempt(
                task,
                status="SUBMIT_UNKNOWN",
                error_code=SUBMIT_OUTCOME_UNKNOWN,
                error_message=str(exc),
                finished=False,
            )
            _mark_failed(
                task,
                SUBMIT_OUTCOME_UNKNOWN,
                (
                    f"{exc}\nRunningHub 提交结果无法确认，系统已保留原执行账号和容量，"
                    "禁止自动或人工盲目重提；请管理员先在 RunningHub 核对。"
                ),
                diagnostics=exc.log_details(),
            )
            db.commit()
            return
        finish_task_attempt(
            task,
            status="SUBMISSION_REJECTED",
            error_code=str(exc.error_code or "SUBMIT_FAILED"),
            error_message=str(exc),
        )
        _mark_failed(
            task,
            "SUBMIT_FAILED",
            str(exc),
            diagnostics=exc.log_details(),
        )
        db.commit()
    except (OSError, ValueError) as exc:
        finish_task_attempt(
            task,
            status="PRE_SUBMISSION_FAILED",
            error_code="SUBMIT_FAILED",
            error_message=str(exc),
        )
        _mark_failed(task, "SUBMIT_FAILED", str(exc))
        db.commit()


def run_once() -> int:
    processed = 0
    with SessionLocal() as db:
        active_ids = db.scalars(
            select(GenerationTask.id)
            .where(
                GenerationTask.status.in_(
                    [TaskStatus.SUBMITTED.value, TaskStatus.RUNNING.value]
                )
            )
            .order_by(GenerationTask.updated_at)
        ).all()
        for task_id in active_ids:
            process_task(db, task_id)
            processed += 1

        while pending_task_id := claim_next_pending_task(db):
            process_task(db, pending_task_id)
            processed += 1
        processed += process_pending_video_merges(db, get_settings())
    return processed


def main() -> None:
    configure_logging("video_worker")
    start_heartbeat("video_worker")
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        recovered = recover_interrupted_tasks(db)
        recovered_merges = recover_interrupted_video_merges(db)
        if recovered:
            logger.info("恢复了 %s 个未提交的中断任务", recovered)
        if recovered_merges:
            logger.info("恢复了 %s 个中断的完整视频合并任务", recovered_merges)
    log_event(
        logger,
        "video.worker_started",
        "视频 Worker 已启动",
        poll_interval_seconds=settings.poll_interval_seconds,
        capacity_recheck_seconds=int(REMOTE_CAPACITY_RECHECK_SECONDS),
        remote_watchdog_seconds=settings.runninghub_remote_watchdog_seconds,
    )
    while True:
        try:
            processed = run_once()
            write_heartbeat("video_worker", processed=processed)
        except Exception:  # noqa: BLE001 - worker must survive one bad task
            logger.exception("Worker 循环出现未预期错误")
            write_heartbeat("video_worker", error="loop_error")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
