from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, RunningHubConfig, TaskStatus, User
from app.services.runninghub import RunningHubClient, RunningHubError
from app.services.logging_config import (
    configure_logging,
    log_event,
    start_heartbeat,
    write_heartbeat,
)
from app.services.security import decrypt_secret
from app.services.storage import create_download_target, to_relative_data_path
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow
from app.workflows.base import WorkflowAdapter, resolve_asset_path


logger = logging.getLogger(__name__)
REMOTE_CAPACITY_RECHECK_SECONDS = 180.0
_capacity_check_after: dict[int, float] = {}

# Only these remote-work states consume a user's configured concurrency slot.
# PENDING remains in the shared FIFO but does not occupy RunningHub capacity.
SLOT_OCCUPYING_TASK_STATUSES = (
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    return {
        "user_id": task.user_id,
        "username": task.user.username if task.user else None,
        "task_id": task.id,
    }


def _make_client(config: RunningHubConfig) -> RunningHubClient:
    """Build the account-level client; the selected adapter supplies the App ID."""

    return RunningHubClient(
        api_key=decrypt_secret(config.api_key_encrypted),
        base_url=config.base_url,
        ai_app_id=config.ai_app_id,
    )


def _mark_failed(task: GenerationTask, code: str, message: str) -> None:
    """Persist a terminal local failure and emit one operator event."""

    task.status = TaskStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.completed_at = _now()
    log_event(
        logger,
        "video.failed",
        "视频任务失败",
        level=logging.WARNING,
        workflow=task.workflow_type,
        error_code=code,
        error=message,
        **_video_log_context(task),
    )


def _mark_remote_cancelled(task: GenerationTask, message: str) -> None:
    """Close a task that RunningHub no longer exposes after manual cancel."""

    task.status = TaskStatus.CANCELLED.value
    task.error_code = "REMOTE_TASK_NOT_FOUND"
    task.error_message = message
    task.completed_at = _now()
    log_event(
        logger,
        "video.cancelled",
        "RunningHub 任务已不存在，本地任务已结束",
        level=logging.WARNING,
        runninghub_task_id=task.runninghub_task_id,
        error=message,
        **_video_log_context(task),
    )


def _has_timed_out(task: GenerationTask) -> bool:
    started_at = task.runninghub_submitted_at or task.created_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (
        _now() - started_at
    ).total_seconds() > get_settings().runninghub_task_timeout_seconds


def recover_interrupted_tasks(db: Session) -> int:
    """Reset only tasks that could not yet have been billed remotely."""

    result = db.execute(
        update(GenerationTask)
        .where(
            GenerationTask.status == TaskStatus.UPLOADING.value,
            GenerationTask.runninghub_task_id.is_(None),
        )
        .values(
            status=TaskStatus.PENDING.value,
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def claim_next_pending_task(db: Session) -> str | None:
    # user_id -> currently occupied slots, calculated once for this claim pass.
    active_counts = dict(
        db.execute(
            select(GenerationTask.user_id, func.count())
            .where(GenerationTask.status.in_(SLOT_OCCUPYING_TASK_STATUSES))
            .group_by(GenerationTask.user_id)
        ).all()
    )
    # user_id -> administrator-configured maximum parallel RunningHub tasks.
    concurrency_limits = dict(
        db.execute(
            select(
                RunningHubConfig.user_id,
                RunningHubConfig.max_concurrent_tasks,
            )
        ).all()
    )
    # Global creation time is the FIFO key; rows belonging to a full user are
    # skipped without blocking eligible rows created later by other users.
    pending_tasks = db.execute(
        select(GenerationTask.id, GenerationTask.user_id)
        .where(GenerationTask.status == TaskStatus.PENDING.value)
        .order_by(GenerationTask.created_at)
    ).all()
    for task_id, user_id in pending_tasks:
        if _capacity_check_is_deferred(user_id):
            continue
        limit = max(int(concurrency_limits.get(user_id, 1)), 1)
        if int(active_counts.get(user_id, 0)) >= limit:
            continue
        result = db.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.status == TaskStatus.PENDING.value,
            )
            .values(
                status=TaskStatus.UPLOADING.value,
                error_code=None,
                error_message=None,
            )
        )
        db.commit()
        if result.rowcount == 1:
            log_event(
                logger,
                "video.claimed",
                "视频 Worker 已领取任务",
                task_id=task_id,
                user_id=user_id,
                concurrency_limit=limit,
            )
            return task_id
    return None


def _load_task(db: Session, task_id: str) -> GenerationTask | None:
    return db.scalar(
        select(GenerationTask)
        .options(
            selectinload(GenerationTask.user).selectinload(User.runninghub_config),
            selectinload(GenerationTask.user).selectinload(User.workflow_configs),
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
            _mark_remote_cancelled(
                task,
                "RunningHub 返回任务不存在或已过期，可能已在平台手动取消",
            )
            db.commit()
            return
        # A query failure must never lead to a second paid submission.
        changed = (
            task.error_code != "QUERY_ERROR"
            or task.error_message != str(exc)
        )
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
                **_video_log_context(task),
            )
        return

    status = str(result.get("status") or "").upper()
    previous_status = task.status
    task.error_code = str(result.get("errorCode") or "") or None
    task.error_message = str(result.get("errorMessage") or "") or None
    usage = result.get("usage")
    task.runninghub_usage = json.dumps(usage, ensure_ascii=False) if usage is not None else None

    if status in {"QUEUED", "RUNNING"}:
        task.status = (
            TaskStatus.RUNNING.value if status == "RUNNING" else TaskStatus.SUBMITTED.value
        )
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
    if status == "FAILED":
        _mark_failed(
            task,
            task.error_code or "RUNNINGHUB_FAILED",
            task.error_message or "RunningHub 工作流运行失败",
        )
        db.commit()
        return
    if status != "SUCCESS":
        _mark_failed(task, "UNKNOWN_STATUS", f"RunningHub 返回未知任务状态：{status or '空'}")
        db.commit()
        return

    output = workflow.select_output(task, result)
    if output is None:
        _mark_failed(task, "EMPTY_RESULT", "工作流成功但没有可下载的结果")
        db.commit()
        return
    destination = create_download_target(
        get_settings(), task.user_id, task.id, output.extension
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
        db.commit()
        log_event(
            logger,
            "video.download_failed",
            "视频结果下载失败",
            level=logging.WARNING,
            runninghub_task_id=task.runninghub_task_id,
            error=str(exc),
            **_video_log_context(task),
        )
        return
    task.result_path = to_relative_data_path(destination, get_settings())
    task.output_metadata = json.dumps(output.metadata, ensure_ascii=False)
    task.status = TaskStatus.SUCCESS.value
    task.error_code = None
    task.error_message = None
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "video.completed",
        "视频任务完成",
        runninghub_task_id=task.runninghub_task_id,
        result_path=task.result_path,
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
) -> None:
    task.status = TaskStatus.PENDING.value
    task.error_code = None
    task.error_message = None
    task.completed_at = None
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
        **_video_log_context(task),
    )


def _remote_capacity_is_available(
    db: Session,
    task: GenerationTask,
    client: RunningHubClient,
    config: RunningHubConfig,
) -> bool:
    limit = max(int(config.max_concurrent_tasks), 1)
    try:
        current_tasks = client.get_account_current_task_count()
    except RunningHubError as exc:
        _return_to_capacity_queue(
            db,
            task,
            reason="读取 RunningHub 账号并发状态失败，任务将保留排队",
            event_code="video.capacity_check_error",
            level=logging.WARNING,
        )
        logger.warning("RunningHub 容量检查失败：%s", exc)
        return False
    if current_tasks >= limit:
        _return_to_capacity_queue(
            db,
            task,
            reason="RunningHub 账号并发已满，任务将保留排队",
            event_code="video.capacity_waiting",
            remote_current_tasks=current_tasks,
            concurrency_limit=limit,
        )
        return False
    return True


def process_task(db: Session, task_id: str) -> None:
    task = _load_task(db, task_id)
    if not task or task.status in {
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return
    if not task.user or not task.user.is_active or not task.user.runninghub_config:
        _mark_failed(task, "CONFIGURATION_ERROR", "账号已禁用或 RunningHub 配置缺失")
        db.commit()
        return
    try:
        workflow = get_workflow(task.workflow_type)
    except ValueError as exc:
        _mark_failed(task, "WORKFLOW_UNSUPPORTED", str(exc))
        db.commit()
        return
    workflow_config = get_user_workflow_config(task.user, workflow.key)
    if not workflow_config.is_enabled:
        _mark_failed(task, "WORKFLOW_DISABLED", f"工作流未为该账号启用：{workflow.key}")
        db.commit()
        return
    if task.runninghub_task_id and _has_timed_out(task):
        _mark_failed(task, "TASK_TIMEOUT", "RunningHub 任务超过允许的最长等待时间")
        db.commit()
        _allow_immediate_capacity_check(task.user_id)
        return
    try:
        client = _make_client(task.user.runninghub_config)
        # RunningHubClient is generic.  The workflow config determines only
        # which AI App the generic submit endpoint targets.
        client.ai_app_id = workflow_config.ai_app_id
        client.submission_type = workflow.submission_type
    except (ValueError, RunningHubError) as exc:
        _mark_failed(task, "CONFIGURATION_ERROR", str(exc))
        db.commit()
        return

    if task.runninghub_task_id:
        _handle_remote_status(db, task, client, workflow)
        if task.status not in SLOT_OCCUPYING_TASK_STATUSES:
            _allow_immediate_capacity_check(task.user_id)
        return

    if not _remote_capacity_is_available(
        db,
        task,
        client,
        task.user.runninghub_config,
    ):
        return

    try:
        settings = get_settings()
        log_event(
            logger,
            "video.upload_started",
            "开始上传视频任务素材",
            workflow=task.workflow_type,
            **_video_log_context(task),
        )
        uploaded_files = {
            asset.name: client.upload_file(resolve_asset_path(asset, settings))
            for asset in workflow.assets_for_task(task)
        }
        payload = workflow.build_payload(
            task,
            uploaded_files,
            ai_app_id=workflow_config.ai_app_id,
            instance_type=workflow_config.instance_type,
            settings=workflow_config.settings,
        )
        remote_task_id = client.submit_task(payload)
        # Persist as soon as submission returns. Every later run only queries this ID.
        task.runninghub_task_id = remote_task_id
        task.runninghub_submitted_at = _now()
        task.status = TaskStatus.SUBMITTED.value
        task.error_code = None
        task.error_message = None
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
        _defer_capacity_check(task.user_id, get_settings().poll_interval_seconds)
    except RunningHubError as exc:
        if exc.is_capacity_limited:
            _return_to_capacity_queue(
                db,
                task,
                reason="RunningHub 在提交时报告并发已满，任务将保留排队",
                event_code="video.capacity_waiting",
            )
            return
        _mark_failed(task, "SUBMIT_FAILED", str(exc))
        db.commit()
    except (OSError, ValueError) as exc:
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
    return processed


def main() -> None:
    configure_logging("video_worker")
    start_heartbeat("video_worker")
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        recovered = recover_interrupted_tasks(db)
        if recovered:
            logger.info("恢复了 %s 个未提交的中断任务", recovered)
    log_event(
        logger,
        "video.worker_started",
        "视频 Worker 已启动",
        poll_interval_seconds=settings.poll_interval_seconds,
        capacity_recheck_seconds=int(REMOTE_CAPACITY_RECHECK_SECONDS),
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
