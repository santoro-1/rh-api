from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, RunningHubConfig, TaskStatus, User
from app.services.runninghub import RunningHubClient, RunningHubError
from app.services.security import decrypt_secret
from app.services.storage import create_download_target, to_relative_data_path
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow
from app.workflows.base import WorkflowAdapter, resolve_asset_path


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_client(config: RunningHubConfig) -> RunningHubClient:
    """Build the account-level client; the selected adapter supplies the App ID."""

    return RunningHubClient(
        api_key=decrypt_secret(config.api_key_encrypted),
        base_url=config.base_url,
        ai_app_id=config.ai_app_id,
    )


def _mark_failed(task: GenerationTask, code: str, message: str) -> None:
    task.status = TaskStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.completed_at = _now()


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
    task_id = db.scalar(
        select(GenerationTask.id)
        .where(GenerationTask.status == TaskStatus.PENDING.value)
        .order_by(GenerationTask.created_at)
        .limit(1)
    )
    if not task_id:
        return None
    result = db.execute(
        update(GenerationTask)
        .where(
            GenerationTask.id == task_id,
            GenerationTask.status == TaskStatus.PENDING.value,
        )
        .values(status=TaskStatus.UPLOADING.value, error_code=None, error_message=None)
    )
    db.commit()
    return task_id if result.rowcount == 1 else None


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
        # A query failure must never lead to a second paid submission.
        task.error_code = "QUERY_ERROR"
        task.error_message = str(exc)
        db.commit()
        return

    status = str(result.get("status") or "").upper()
    task.error_code = str(result.get("errorCode") or "") or None
    task.error_message = str(result.get("errorMessage") or "") or None
    usage = result.get("usage")
    task.runninghub_usage = json.dumps(usage, ensure_ascii=False) if usage is not None else None

    if status in {"QUEUED", "RUNNING"}:
        task.status = (
            TaskStatus.RUNNING.value if status == "RUNNING" else TaskStatus.SUBMITTED.value
        )
        db.commit()
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
    try:
        client.download_result(output.url, destination)
    except RunningHubError as exc:
        task.status = TaskStatus.DOWNLOAD_FAILED.value
        task.error_code = "DOWNLOAD_FAILED"
        task.error_message = str(exc)
        db.commit()
        return
    task.result_path = to_relative_data_path(destination, get_settings())
    task.output_metadata = json.dumps(output.metadata, ensure_ascii=False)
    task.status = TaskStatus.SUCCESS.value
    task.error_code = None
    task.error_message = None
    task.completed_at = _now()
    db.commit()


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
        return
    try:
        client = _make_client(task.user.runninghub_config)
        # RunningHubClient is generic.  The workflow config determines only
        # which AI App the generic submit endpoint targets.
        client.ai_app_id = workflow_config.ai_app_id
    except (ValueError, RunningHubError) as exc:
        _mark_failed(task, "CONFIGURATION_ERROR", str(exc))
        db.commit()
        return

    if task.runninghub_task_id:
        _handle_remote_status(db, task, client, workflow)
        return

    try:
        settings = get_settings()
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
    except (OSError, ValueError, RunningHubError) as exc:
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

        pending_task_id = claim_next_pending_task(db)
        if pending_task_id:
            process_task(db, pending_task_id)
            processed += 1
    return processed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        recovered = recover_interrupted_tasks(db)
        if recovered:
            logger.info("恢复了 %s 个未提交的中断任务", recovered)
    logger.info("Worker 已启动，轮询间隔 %s 秒", settings.poll_interval_seconds)
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001 - worker must survive one bad task
            logger.exception("Worker 循环出现未预期错误")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
