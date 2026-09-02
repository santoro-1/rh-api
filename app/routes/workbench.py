from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
import uuid

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import (
    ArkConfig,
    AudioTaskStatus,
    BATCH_SOURCE_NEW_WORKBENCH,
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    EnhancementStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationTask,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceCreationTask,
)
from app.routes.dependencies import check_rate_limit
from app.services.audio import ensure_generated_speech_mastered
from app.services.ark_request_manager import (
    ArkRequestManager,
    ArkRequestManagerError,
)
from app.services.ark_operation_ledger import (
    ArkOperationConflict,
    claim_ark_operation,
    mark_ark_operation_failed,
    mark_ark_operation_running,
    mark_ark_operation_succeeded,
    replay_ark_operation_result,
    release_unadmitted_ark_operation,
)
from app.services.audio_review import (
    AudioReviewError,
    approve_item_audio,
    current_attempt,
    regenerate_item_audio,
)
from app.services.batch_assets import (
    StagedAssetError,
    load_available_assets,
    stage_asset,
)
from app.services.batch_generation import (
    BatchValidationError,
    create_batch,
    validate_workbench_audio_batch,
)
from app.services.batch_manifests import DIGITAL_HUMAN_WORKFLOW
from app.services.batch_status import batch_query
from app.services.content_analysis.analysis import (
    ContentAnalysisInputError,
    ContentAnalysisUnavailable,
    analyze_content,
)
from app.services.device_auth.admission import (
    request_identity,
    require_new_work,
    workbench_user,
)
from app.services.device_auth.queued_work import (
    bind_new_operation,
    task_resource,
)
from app.services.visual_analysis import (
    VisualAnalysisInputError,
    VisualAnalysisUnavailable,
    analyze_visual_context,
)
from app.workflows.digital_human import (
    DIGITAL_HUMAN_TAIL_PADDING_SECONDS,
    GENERATION_TAIL_PARAMETER,
)
from app.services.postproduction import postproduction_manifest
from app.services.logging_config import log_event
from app.services.runninghub_pool import (
    RunningHubPoolSelectionFormatError,
    RunningHubPoolSelectionPermissionError,
    RunningHubPoolSelectionUnavailableError,
    RunningHubPoolSnapshotConflictError,
    batch_execution_account_snapshot,
    bind_batch_execution_account_snapshot,
    bind_item_execution_account_snapshot,
    item_execution_account_snapshot,
    validate_workbench_execution_account_selection,
    workbench_execution_account_summary,
)
from app.services.runninghub_dual_pool import (
    RunningHubDualPoolError,
    batch_execution_mode,
    bind_batch_execution_mode,
    resolve_execution_mode,
    user_has_dual_pool_entitlement,
)
from app.services.seedvr2_pool import (
    SeedVR2PoolSelectionFormatError,
    SeedVR2PoolSelectionPermissionError,
    SeedVR2PoolSelectionUnavailableError,
    SeedVR2PoolSnapshotConflictError,
    bind_seedvr2_batch_account_snapshot,
    bind_seedvr2_item_account_snapshot,
    seedvr2_batch_account_snapshot,
    seedvr2_item_account_snapshot,
    seedvr2_workbench_account_summary,
    validate_seedvr2_account_selection,
)
from app.services.security import verify_password
from app.services.speech.minimax import MiniMaxAPIError
from app.services.speech.voice_studio import create_voice_task, request_voice_save
from app.services.speech.workbench_voices import (
    activate_workbench_voice,
    available_workbench_voices,
    creation_task_payload,
    delete_workbench_voice,
    ensure_workbench_system_voices,
    generate_official_voice_preview,
    import_workbench_clone_voice,
    voice_payload,
)
from app.services.storage import (
    UploadValidationError,
    materialize_staged_asset,
    remove_directory,
    safe_relative_path,
    task_upload_dir,
    to_relative_data_path,
)
from app.services.task_management import (
    TaskManagementError,
    prepare_task_retry,
    task_retry_starts_new_provider_work,
)
from app.services.video_enhancement import (
    VideoEnhancementBackfillError,
    historical_source_path,
    queue_historical_seedvr2_enhancement,
)
from app.services.workflow_configs import get_user_workflow_config
from app.services.video_merge import (
    MERGE_FAILED,
    MERGED_PREVIEW_READY,
    MERGED_VIDEO_READY,
    invalidate_merged_video,
    retry_video_merge,
)
from app.services.workbench_auth import (
    HANDOFF_LIFETIME_SECONDS,
    decode_workbench_token,
    issue_workbench_token,
    public_workbench_user,
    token_matches_user,
    workbench_handoffs,
)


logger = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_image_sha256(payload: dict[str, Any]) -> str:
    value = str(payload.get("image_sha256") or "").strip().lower()
    if not value:
        return ""
    if len(value) != 64:
        raise AudioReviewError("当前项目图片指纹不合法")
    try:
        int(value, 16)
    except ValueError as exc:
        raise AudioReviewError("当前项目图片指纹不合法") from exc
    return value


def _reset_video_handoff_for_new_image(
    db: Session,
    item: GenerationBatchItem,
) -> None:
    """Discard only video-stage state while preserving approved MiniMax audio."""

    video_tasks = [
        segment.generation_task
        for segment in item.segments
        if segment.generation_task is not None
    ]
    if item.generation_task is not None:
        video_tasks.append(item.generation_task)
    active_statuses = {
        TaskStatus.PENDING.value,
        TaskStatus.UPLOADING.value,
        TaskStatus.SUBMITTED.value,
        TaskStatus.RUNNING.value,
    }
    if any(task.status in active_statuses for task in video_tasks):
        raise AudioReviewError("旧图片的画面任务仍在运行，请完成后再更换图片")
    if item.merged_video_status == "MERGING":
        raise AudioReviewError("旧图片的视频仍在合并，请完成后再更换图片")

    # Keep the former output file recoverable.  Only detach its database
    # pointer here; the next merge writes a new item-scoped result, and normal
    # retention cleanup may remove the orphan later.
    item.merged_video_status = "MERGE_PENDING"
    item.merged_video_path = None
    item.merged_video_error = None
    item.merged_at = None
    item.merged_reviewed_at = None
    for video_task in video_tasks:
        video_task.segment = None
        video_task.batch_item = None
        db.delete(video_task)
    db.flush()
    item.segments.clear()
    db.flush()

    audio_task = item.audio_task
    if audio_task is None:
        raise AudioReviewError("声音任务不存在")
    audio_task.status = AudioTaskStatus.PENDING.value
    audio_task.error_code = None
    audio_task.error_message = None
    audio_task.completed_at = None
    item.audio_status = "AUDIO_APPROVED"
    item.status = "AUDIO_APPROVED"
    item.error_code = None
    item.error_message = None


def _video_handoff_tasks(item: GenerationBatchItem) -> list[GenerationTask]:
    """Return every paid video-stage task currently attached to one row."""

    tasks = [
        segment.generation_task
        for segment in item.segments
        if segment.generation_task is not None
    ]
    if item.generation_task is not None:
        tasks.append(item.generation_task)
    return tasks


def _has_cancelled_digital_human_handoff(item: GenerationBatchItem) -> bool:
    """Whether a provider-cancelled 4A command may be rebuilt from saved audio.

    A SeedVR2 cancellation already owns a successful digital-human source and
    must stay on the enhancement-only retry path.  Mixed terminal segment rows
    are accepted when at least one digital-human command was cancelled: changing
    the image or resolution intentionally rebuilds the whole video stage so all
    segments use one input snapshot.
    """

    tasks = _video_handoff_tasks(item)
    return bool(tasks) and any(
        task.status == TaskStatus.CANCELLED.value for task in tasks
    ) and all(task.enhancement is None for task in tasks)


router = APIRouter(tags=["workbench"])
_LEGACY_WORKBENCH_DIGITAL_PROMPT = "人物自然地说话"


def _token_user(token: str, db: Session) -> User:
    settings = get_settings()
    payload = decode_workbench_token(token, settings)
    if payload is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    user = db.get(User, int(payload["user_id"]))
    if user is None or not token_matches_user(payload, user):
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return user


def _bearer_user(request: Request, db: Session) -> User:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    return _token_user(token, db)


def _item_for_user(item_id: str, user: User, db: Session) -> GenerationBatchItem:
    batch = db.scalar(
        batch_query()
        .join(GenerationBatchItem, GenerationBatchItem.batch_id == GenerationBatch.id)
        .where(
            GenerationBatchItem.id == item_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == DIGITAL_HUMAN_WORKFLOW,
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="数字人任务不存在")
    return next(item for item in batch.items if item.id == item_id)


def _batch_for_user(batch_id: str, user: User, db: Session) -> GenerationBatch:
    batch = db.scalar(
        batch_query().where(
            GenerationBatch.id == batch_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == DIGITAL_HUMAN_WORKFLOW,
            GenerationBatch.source_channel == BATCH_SOURCE_NEW_WORKBENCH,
        )
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="数字人批次不存在")
    return batch


def _voice_for_user(
    voice_asset_id: str, user: User, db: Session
) -> MiniMaxVoiceAsset:
    voice = db.scalar(
        select(MiniMaxVoiceAsset).where(
            MiniMaxVoiceAsset.id == voice_asset_id,
            MiniMaxVoiceAsset.user_id == user.id,
            MiniMaxVoiceAsset.is_saved.is_(True),
        )
    )
    if voice is None:
        raise HTTPException(status_code=404, detail="声音原型不存在")
    return voice


def _voice_task_for_user(
    task_id: str, user: User, db: Session
) -> VoiceCreationTask:
    task = db.scalar(
        select(VoiceCreationTask)
        .options(selectinload(VoiceCreationTask.voice_asset))
        .where(
            VoiceCreationTask.id == task_id,
            VoiceCreationTask.user_id == user.id,
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="声音制作任务不存在")
    return task


def _audio_batch_payload(batch: GenerationBatch) -> dict[str, Any]:
    items = []
    for item in sorted(batch.items, key=lambda value: value.row_number):
        audio_task = item.audio_task
        captions = postproduction_manifest(item, get_settings()).get("captions")
        items.append(
            {
                "item_id": item.id,
                "row_key": item.row_key,
                "status": audio_task.status if audio_task else item.audio_status,
                "generation_version": (
                    audio_task.generation_version if audio_task else 0
                ),
                "error_code": audio_task.error_code if audio_task else None,
                "error_message": (
                    audio_task.error_message if audio_task else item.error_message
                ),
                "audio_ready": bool(audio_task and audio_task.output_path),
                "audio_download_url": (
                    f"/api/workbench/audio-batches/{batch.id}/items/{item.id}/audio"
                    if audio_task and audio_task.output_path
                    else None
                ),
                "captions": captions,
            }
        )
    return {
        "schema": "runninghub.workbench-audio-batch.v1",
        "batch_id": batch.id,
        "correlation_id": batch.correlation_id or batch.id,
        "source_channel": batch.source_channel,
        "name": batch.name,
        "review_required": batch.review_required,
        "runninghub_execution_account_ids": batch_execution_account_snapshot(batch),
        "seedvr2_execution_account_ids": seedvr2_batch_account_snapshot(batch),
        "execution_mode": batch.execution_mode,
        "items": items,
    }


def _workbench_manifest(item: GenerationBatchItem) -> dict[str, Any]:
    manifest = postproduction_manifest(item, get_settings())
    for video in manifest["source"]["videos"]:
        video["download_url"] = (
            f"/api/workbench/tasks/{item.id}/videos/{video['index']}"
        )
        if video.get("source_download_url"):
            video["source_download_url"] = (
                f"/api/workbench/tasks/{item.id}/videos/{video['index']}/source"
            )
        video.pop("preview_url", None)
    manifest["batch_name"] = item.batch.name
    manifest["correlation_id"] = item.batch.correlation_id or item.batch.id
    manifest["created_at"] = item.batch.created_at.isoformat()
    manifest["updated_at"] = item.updated_at.isoformat()
    manifest["composition"] = _composition_payload(item)
    return manifest


def _safe_execution_account(account: Any) -> dict[str, Any] | None:
    """Expose only the internal identifier and operator-facing label."""

    if account is None:
        return None
    return {"id": int(account.id), "label": str(account.label)}


def _execution_assignments(
    tasks: list[GenerationTask], execution_mode: str | None
) -> list[dict[str, Any]]:
    """Report the actual per-segment bindings without exposing credentials."""

    assignments: list[dict[str, Any]] = []
    dual_pool = execution_mode == BATCH_EXECUTION_MODE_DUAL_POOL_V1
    for segment_index, task in enumerate(tasks, start=1):
        enhancement = task.enhancement
        digital_account = _safe_execution_account(task.execution_account)
        if enhancement is None:
            seedvr2_account = None if dual_pool else digital_account
            seedvr2_status = None
        elif dual_pool:
            seedvr2_account = _safe_execution_account(
                enhancement.seedvr2_execution_account
            )
            seedvr2_status = enhancement.status
        else:
            # same_account_v1 permanently binds SeedVR2 to the digital-human
            # executor. Prefer the persisted enhancement binding once present,
            # while still showing the reserved account before enhancement starts.
            seedvr2_account = (
                _safe_execution_account(enhancement.execution_account)
                or digital_account
            )
            seedvr2_status = enhancement.status
        assignments.append(
            {
                "segment_index": segment_index,
                "digital_human": {
                    "status": task.status,
                    "account": digital_account,
                },
                "seedvr2": {
                    "status": seedvr2_status,
                    "account": seedvr2_account,
                },
            }
        )
    return assignments


def _h3_audio_is_approved(item: GenerationBatchItem) -> bool:
    return str(item.audio_status or "").strip().upper() == "AUDIO_APPROVED_H3" or str(
        item.status or ""
    ).strip().upper() == "AUDIO_APPROVED_H3"


def _composition_payload(item: GenerationBatchItem) -> dict[str, Any]:
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ]
    audio_task = item.audio_task
    audio_failed = bool(
        not tasks
        and audio_task is not None
        and audio_task.status == AudioTaskStatus.FAILED.value
    )
    h3_audio_approved = _h3_audio_is_approved(item)
    obsolete_h3_composition_handoff = bool(
        not tasks
        and item.generation_task is None
        and h3_audio_approved
        and audio_task is not None
        and audio_task.primary_path
    )
    error_message = item.merged_video_error or item.error_message
    if audio_task is not None and audio_task.error_message:
        error_message = audio_task.error_message

    if item.merged_video_path and item.merged_video_status in {
        MERGED_PREVIEW_READY,
        MERGED_VIDEO_READY,
    }:
        status = "BASE_VIDEO_READY"
    elif item.merged_video_status == MERGE_FAILED:
        status = "COMPOSITION_FAILED"
    elif any(
        task.status
        in {
            TaskStatus.FAILED.value,
            TaskStatus.DOWNLOAD_FAILED.value,
            TaskStatus.CANCELLED.value,
        }
        for task in tasks
    ):
        status = "COMPOSITION_FAILED"
        failed = next(
            task
            for task in tasks
            if task.status
            in {
                TaskStatus.FAILED.value,
                TaskStatus.DOWNLOAD_FAILED.value,
                TaskStatus.CANCELLED.value,
            }
        )
        error_message = failed.error_message or failed.runninghub_failed_reason
    elif audio_failed:
        status = "COMPOSITION_FAILED"
    elif obsolete_h3_composition_handoff:
        status = "COMPOSITION_FAILED"
        error_message = (
            "该请求来自已停用的普通数字人交接；新版工作台请直接使用多参考生成"
        )
    elif any(
        task.enhancement is not None
        and task.enhancement.status != EnhancementStatus.SUCCESS.value
        for task in tasks
    ):
        status = "VIDEO_ENHANCING"
    elif tasks and all(task.status == TaskStatus.SUCCESS.value for task in tasks):
        status = "VIDEO_MERGING"
    elif tasks:
        status = "DIGITAL_HUMAN_RUNNING"
    elif h3_audio_approved:
        status = "AUDIO_READY"
    elif audio_task is not None and (
        audio_task.reviewed_at is not None
        or audio_task.status
        not in {AudioTaskStatus.AWAITING_REVIEW.value, AudioTaskStatus.FAILED.value}
    ):
        status = "COMPOSITION_QUEUED"
    else:
        status = "AUDIO_READY"
    enhancement_statuses = [
        task.enhancement.status
        for task in tasks
        if task.enhancement is not None
    ]
    enhancement_status = next(
        (
            value
            for value in enhancement_statuses
            if value != EnhancementStatus.SUCCESS.value
        ),
        EnhancementStatus.SUCCESS.value if enhancement_statuses else None,
    )
    quality_variant = (
        "seedvr2_upscaled"
        if tasks
        and len(enhancement_statuses) == len(tasks)
        and all(
            value == EnhancementStatus.SUCCESS.value
            for value in enhancement_statuses
        )
        else None
    )
    return {
        "status": status,
        "processing_stage": status,
        "enhancement_status": enhancement_status,
        "quality_variant": quality_variant,
        "segment_count": len(tasks),
        "merge_status": item.merged_video_status,
        "image_sha256": audio_task.primary_sha256 if audio_task is not None else None,
        "base_video_ready": status == "BASE_VIDEO_READY",
        "base_video_download_url": (
            f"/api/workbench/tasks/{item.id}/base-video"
            if status == "BASE_VIDEO_READY"
            else None
        ),
        "error_message": error_message,
        "error_code": (
            "NEW_WORKBENCH_H3_ONLY"
            if obsolete_h3_composition_handoff
            else (audio_task.error_code if audio_failed else None)
        ),
        "failure_stage": (
            "handoff"
            if obsolete_h3_composition_handoff
            else ("audio" if audio_failed else None)
        ),
        "runninghub_execution_account_ids": item_execution_account_snapshot(item),
        "seedvr2_execution_account_ids": seedvr2_item_account_snapshot(item),
        "execution_mode": item.batch.execution_mode,
        "execution_assignments": _execution_assignments(
            tasks, item.batch.execution_mode
        ),
    }


@router.post("/api/auth/center/login")
def workbench_login(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    check_rate_limit(request, "workbench-login", get_settings().login_rate_limit_per_minute)
    username = str(payload.get("username", "")).strip()
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.is_active
        or not verify_password(str(payload.get("password", "")), user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return {
        "ok": True,
        "access_token": issue_workbench_token(user, get_settings()),
        "user": public_workbench_user(user),
    }


@router.post("/api/auth/center/verify")
def workbench_verify(payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    user = _token_user(str(payload.get("access_token", "")), db)
    return {"valid": True, "user": public_workbench_user(user)}


@router.post("/api/auth/center/handoff")
def workbench_create_handoff(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    token = str(payload.get("access_token", ""))
    _token_user(token, db)
    return {
        "handoff_code": workbench_handoffs.issue(token),
        "expires_in": HANDOFF_LIFETIME_SECONDS,
    }


@router.post("/api/auth/center/handoff/consume")
def workbench_consume_handoff(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    token = workbench_handoffs.consume(str(payload.get("handoff_code", "")))
    if not token:
        raise HTTPException(status_code=401, detail="登录接力码无效或已过期")
    user = _token_user(token, db)
    refreshed = issue_workbench_token(user, get_settings())
    return {"access_token": refreshed, "user": public_workbench_user(user)}


@router.post("/api/workbench/runninghub-execution-accounts")
def workbench_runninghub_execution_accounts(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Return only safe capacity metadata for the authenticated administrator."""

    user = _token_user(str(payload.get("access_token", "")), db)
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="只有管理员可以使用 RunningHub 执行账号资源池",
        )
    mode = resolve_execution_mode(
        db, user=user, source_channel=BATCH_SOURCE_NEW_WORKBENCH,
        workflow_type=DIGITAL_HUMAN_WORKFLOW,
    )
    if mode == BATCH_EXECUTION_MODE_DUAL_POOL_V1:
        return {
            "schema": "runninghub.workbench-dual-pool.v1",
            "execution_mode": mode,
            "pool_access": True,
            "digital_human": workbench_execution_account_summary(db, user),
            "seedvr2": seedvr2_workbench_account_summary(db, user),
        }
    return {
        **workbench_execution_account_summary(db, user),
        "execution_mode": mode,
        "pool_access": True,
    }


@router.post("/api/workbench/runninghub-dual-pool-accounts")
def workbench_runninghub_dual_pool_accounts(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Return safe stage-specific metadata; never return credentials."""

    user = _token_user(str(payload.get("access_token", "")), db)
    mode = resolve_execution_mode(
        db,
        user=user,
        source_channel=BATCH_SOURCE_NEW_WORKBENCH,
        workflow_type=DIGITAL_HUMAN_WORKFLOW,
    )
    has_entitlement = user_has_dual_pool_entitlement(db, user)
    if mode != BATCH_EXECUTION_MODE_DUAL_POOL_V1:
        if not has_entitlement:
            return {
                "schema": "runninghub.workbench-dual-pool.v1",
                "execution_mode": mode,
                "pool_access": False,
            }
        return {
            **workbench_execution_account_summary(db, user),
            "execution_mode": mode,
            "pool_access": True,
        }
    if not has_entitlement:
        raise HTTPException(status_code=403, detail="当前账号没有双资源池权限")
    return {
        "schema": "runninghub.workbench-dual-pool.v1",
        "execution_mode": mode,
        "pool_access": True,
        "digital_human": workbench_execution_account_summary(db, user),
        "seedvr2": seedvr2_workbench_account_summary(db, user),
    }


@router.post("/api/workbench/tasks")
def workbench_tasks(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    user = _token_user(str(payload.get("access_token", "")), db)
    limit = min(max(int(payload.get("limit", 50) or 50), 1), 100)
    batches = db.scalars(
        batch_query()
        .where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == DIGITAL_HUMAN_WORKFLOW,
        )
        .order_by(GenerationBatch.created_at.desc())
        .limit(limit)
    ).unique().all()
    tasks = [_workbench_manifest(item) for batch in batches for item in batch.items]
    return {"schema": "runninghub.workbench-inbox.v1", "tasks": tasks[:limit]}


def _analysis_operation_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("analysis_operation_id") or "").strip()
    if not value:
        return str(uuid.uuid4())
    if len(value) > 64 or not all(
        character.isalnum() or character in {"-", "_", ".", ":"}
        for character in value
    ):
        raise HTTPException(status_code=400, detail="analysis_operation_id 格式不合法")
    return value


def _analysis_request_budget_seconds(request: Request) -> float:
    maximum = float(get_settings().ark_analysis_total_timeout_seconds)
    raw = str(request.headers.get("X-JYD-Request-Budget-Ms") or "").strip()
    if not raw:
        return maximum
    try:
        milliseconds = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-JYD-Request-Budget-Ms 必须是整数"
        ) from exc
    if milliseconds <= 0:
        raise HTTPException(status_code=400, detail="豆包请求预算必须大于 0")
    return min(maximum, milliseconds / 1000.0)


def _ark_circuit_key(db: Session, user_id: int) -> str:
    config = db.scalar(select(ArkConfig).where(ArkConfig.user_id == user_id))
    if config is None:
        return f"user:{user_id}:unconfigured"
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                str(config.id),
                str(config.base_url or "").strip().lower(),
                str(config.model or "").strip(),
                str(config.api_key_encrypted or ""),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"ark:{fingerprint}"


def _ark_manager_http_error(exc: ArkRequestManagerError) -> HTTPException:
    headers = {"X-Ark-Error-Code": exc.code}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return HTTPException(
        status_code=exc.status_code,
        detail=str(exc),
        headers=headers,
    )


def _ark_operation_http_error(exc: ArkOperationConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=str(exc),
        headers={"X-Ark-Error-Code": exc.code},
    )


async def _run_ark_operation(
    request: Request,
    *,
    operation_id: str,
    business_key: str,
    kind: str,
    circuit_key: str,
    runner,
    total_timeout_seconds: float,
) -> dict[str, Any]:
    manager: ArkRequestManager | None = getattr(
        request.app.state, "ark_request_manager", None
    )
    if manager is None:
        # A negative queue wait is the internal marker for the compatibility
        # path: the analysis service still owns its legacy semaphore.
        result = await run_in_threadpool(runner, total_timeout_seconds, -1.0)
    else:
        try:
            future = manager.submit(
                operation_id=operation_id,
                business_key=business_key,
                kind=kind,
                circuit_key=circuit_key,
                runner=runner,
                total_timeout_seconds=total_timeout_seconds,
                queue_timeout_seconds=min(
                    float(get_settings().ark_queue_wait_timeout_seconds),
                    max(0.001, total_timeout_seconds - 0.001),
                ),
            )
            result = await asyncio.wrap_future(future)
        except ArkRequestManagerError as exc:
            with SessionLocal() as ledger_db:
                if exc.code in {
                    "ARK_QUEUE_FULL",
                    "ARK_CIRCUIT_OPEN",
                    "ARK_MANAGER_SHUTTING_DOWN",
                }:
                    release_unadmitted_ark_operation(ledger_db, operation_id)
                else:
                    mark_ark_operation_failed(
                        ledger_db,
                        operation_id,
                        code=exc.code,
                        summary=str(exc),
                    )
            raise _ark_manager_http_error(exc) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="豆包分析返回格式错误")
    return {
        **result,
        "analysis_operation_id": operation_id,
        "request_manager_enabled": manager is not None,
    }


@router.post("/api/workbench/content-analysis")
async def workbench_content_analysis(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Analyze one exact script without exposing Ark credentials to workbench."""

    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
        new_work=True,
    )
    original_script = payload.get("original_script")
    force_refresh = payload.get("force_refresh", False)
    visual_context = payload.get("visual_context")
    if not isinstance(original_script, str):
        raise HTTPException(status_code=400, detail="original_script 必须是字符串")
    if type(force_refresh) is not bool:
        raise HTTPException(status_code=400, detail="force_refresh 必须是布尔值")
    if visual_context is not None and not isinstance(visual_context, dict):
        raise HTTPException(status_code=400, detail="visual_context 必须是对象")
    operation_id = _analysis_operation_id(payload)
    force_refresh_generation = payload.get("force_refresh_generation", 0)
    if (
        isinstance(force_refresh_generation, bool)
        or not isinstance(force_refresh_generation, int)
        or force_refresh_generation < 0
    ):
        raise HTTPException(
            status_code=400, detail="force_refresh_generation 必须是非负整数"
        )
    user_id = int(user.id)
    visual_digest = hashlib.sha256(
        json.dumps(
            visual_context or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    script_digest = hashlib.sha256(original_script.encode("utf-8")).hexdigest()
    business_key = ":".join(
        (
            "content",
            str(user_id),
            script_digest,
            visual_digest,
            str(force_refresh_generation),
            operation_id if force_refresh else "shared",
        )
    )
    circuit_key = _ark_circuit_key(db, user_id)
    total_budget = _analysis_request_budget_seconds(request)
    try:
        operation = claim_ark_operation(
            db,
            operation_id=operation_id,
            user_id=user_id,
            kind="content",
            business_key=business_key,
            request_sha256=hashlib.sha256(
                f"{script_digest}:{visual_digest}".encode("utf-8")
            ).hexdigest(),
        )
    except ArkOperationConflict as exc:
        raise _ark_operation_http_error(exc) from exc
    replay = replay_ark_operation_result(db, operation)
    if operation.status in {"SUCCEEDED", "PARTIAL"}:
        if replay is None:
            raise _ark_operation_http_error(
                ArkOperationConflict(
                    "ARK_OPERATION_RESULT_MISSING",
                    "操作账本存在但缓存结果缺失，已阻止自动重放",
                )
            )
        return {
            **replay,
            "analysis_operation_id": operation_id,
            "request_manager_enabled": getattr(
                request.app.state, "ark_request_manager", None
            )
            is not None,
            "operation_replayed": True,
        }

    def run_analysis(remaining_seconds: float, queue_wait_seconds: float):
        with SessionLocal() as worker_db:
            worker_user = worker_db.get(User, user_id)
            if worker_user is None:
                raise ContentAnalysisUnavailable("当前账号不存在或已停用")
            mark_ark_operation_running(worker_db, operation_id)
            kwargs: dict[str, Any] = {
                "original_script": original_script,
                "force_refresh": force_refresh,
            }
            if visual_context is not None:
                kwargs["visual_context_payload"] = visual_context
            if queue_wait_seconds >= 0:
                kwargs.update(
                    total_budget_seconds=remaining_seconds,
                    skip_legacy_limiter=True,
                )
            try:
                result = analyze_content(worker_db, worker_user, **kwargs)
            except BaseException as exc:
                mark_ark_operation_failed(
                    worker_db,
                    operation_id,
                    code=getattr(exc, "code", type(exc).__name__),
                    summary=str(exc) or "豆包内容分析执行失败",
                )
                raise
            mark_ark_operation_succeeded(
                worker_db,
                operation_id,
                cache_kind="content",
                cache_id=result.get("cache_id"),
                status=(
                    "PARTIAL"
                    if str(result.get("overall_status") or "").upper() == "PARTIAL"
                    else "SUCCEEDED"
                ),
            )
            return result
    try:
        return await _run_ark_operation(
            request,
            operation_id=operation_id,
            business_key=business_key,
            kind="content",
            circuit_key=circuit_key,
            runner=run_analysis,
            total_timeout_seconds=total_budget,
        )
    except ContentAnalysisInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ContentAnalysisUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/workbench/visual-analysis")
async def workbench_visual_analysis(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Analyze only local semantic candidates without exposing Ark credentials."""

    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
        new_work=True,
    )
    force_refresh = payload.get("force_refresh", False)
    if type(force_refresh) is not bool:
        raise HTTPException(status_code=400, detail="force_refresh 必须是布尔值")
    request_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "access_token",
            "force_refresh",
            "analysis_operation_id",
            "force_refresh_generation",
        }
    }
    operation_id = _analysis_operation_id(payload)
    force_refresh_generation = payload.get("force_refresh_generation", 0)
    if (
        isinstance(force_refresh_generation, bool)
        or not isinstance(force_refresh_generation, int)
        or force_refresh_generation < 0
    ):
        raise HTTPException(
            status_code=400, detail="force_refresh_generation 必须是非负整数"
        )
    user_id = int(user.id)
    request_digest = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    business_key = ":".join(
        (
            "visual",
            str(user_id),
            request_digest,
            str(force_refresh_generation),
            operation_id if force_refresh else "shared",
        )
    )
    circuit_key = _ark_circuit_key(db, user_id)
    total_budget = _analysis_request_budget_seconds(request)
    try:
        operation = claim_ark_operation(
            db,
            operation_id=operation_id,
            user_id=user_id,
            kind="visual",
            business_key=business_key,
            request_sha256=request_digest,
        )
    except ArkOperationConflict as exc:
        raise _ark_operation_http_error(exc) from exc
    replay = replay_ark_operation_result(db, operation)
    if operation.status in {"SUCCEEDED", "PARTIAL"}:
        if replay is None:
            raise _ark_operation_http_error(
                ArkOperationConflict(
                    "ARK_OPERATION_RESULT_MISSING",
                    "操作账本存在但缓存结果缺失，已阻止自动重放",
                )
            )
        return {
            **replay,
            "analysis_operation_id": operation_id,
            "request_manager_enabled": getattr(
                request.app.state, "ark_request_manager", None
            )
            is not None,
            "operation_replayed": True,
        }

    def run_analysis(remaining_seconds: float, queue_wait_seconds: float):
        with SessionLocal() as worker_db:
            worker_user = worker_db.get(User, user_id)
            if worker_user is None:
                raise VisualAnalysisUnavailable("当前账号不存在或已停用")
            mark_ark_operation_running(worker_db, operation_id)
            kwargs: dict[str, Any] = {
                "payload": request_payload,
                "force_refresh": force_refresh,
            }
            if queue_wait_seconds >= 0:
                kwargs.update(
                    total_budget_seconds=remaining_seconds,
                    skip_legacy_limiter=True,
                )
            try:
                result = analyze_visual_context(worker_db, worker_user, **kwargs)
            except BaseException as exc:
                mark_ark_operation_failed(
                    worker_db,
                    operation_id,
                    code=getattr(exc, "code", type(exc).__name__),
                    summary=str(exc) or "豆包视觉分析执行失败",
                )
                raise
            if str(result.get("analysis_status") or "").upper() == "FAILED":
                error = result.get("error")
                mark_ark_operation_failed(
                    worker_db,
                    operation_id,
                    code=(
                        str(error.get("code") or "VISUAL_ANALYSIS_FAILED")
                        if isinstance(error, dict)
                        else "VISUAL_ANALYSIS_FAILED"
                    ),
                    summary=(
                        str(error.get("summary") or "豆包视觉分析失败")
                        if isinstance(error, dict)
                        else "豆包视觉分析失败"
                    ),
                )
            else:
                mark_ark_operation_succeeded(
                    worker_db,
                    operation_id,
                    cache_kind="visual",
                    cache_id=result.get("cache_id"),
                )
            return result
    try:
        return await _run_ark_operation(
            request,
            operation_id=operation_id,
            business_key=business_key,
            kind="visual",
            circuit_key=circuit_key,
            runner=run_analysis,
            total_timeout_seconds=total_budget,
        )
    except VisualAnalysisInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VisualAnalysisUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/workbench/tasks/{item_id}")
def workbench_task(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.get("/api/workbench/tasks/{item_id}/videos/{video_index}")
def workbench_video(
    item_id: str,
    video_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = _item_for_user(item_id, user, db)
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ] if item.segments else ([item.generation_task] if item.generation_task else [])
    if video_index < 1 or video_index > len(tasks):
        raise HTTPException(status_code=404, detail="视频片段不存在")
    task = tasks[video_index - 1]
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise HTTPException(status_code=409, detail="视频片段尚未生成完成")
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    filename = Path(task.audio_original_name or f"segment-{video_index}.mp4").stem + ".mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename)


@router.get("/api/workbench/tasks/{item_id}/videos/{video_index}/source")
def workbench_source_video(
    item_id: str,
    video_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = _item_for_user(item_id, user, db)
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ] if item.segments else ([item.generation_task] if item.generation_task else [])
    if video_index < 1 or video_index > len(tasks):
        raise HTTPException(status_code=404, detail="视频片段不存在")
    task = tasks[video_index - 1]
    if task.enhancement is None:
        raise HTTPException(status_code=404, detail="数字人源片段不存在")
    try:
        path = safe_relative_path(
            task.enhancement.source_result_path,
            get_settings().data_dir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="数字人源片段不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="数字人源片段不存在")
    filename = Path(task.audio_original_name or f"segment-{video_index}.mp4").stem
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{filename}-source{path.suffix}",
    )


@router.post("/api/workbench/voices")
def workbench_voices(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    """Return the reusable voice library for the shared website account."""

    user = _token_user(str(payload.get("access_token", "")), db)
    try:
        ensure_workbench_system_voices(db, user, get_settings())
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MiniMaxAPIError, OSError) as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    tasks = db.scalars(
        select(VoiceCreationTask)
        .where(VoiceCreationTask.user_id == user.id)
        .order_by(VoiceCreationTask.created_at.desc())
        .limit(30)
    ).all()
    voices = available_workbench_voices(db, user)
    db.commit()
    return {
        "schema": "runninghub.workbench-voices.v1",
        "voices": [voice_payload(voice) for voice in voices],
        "creation_tasks": [creation_task_payload(task) for task in tasks],
    }


@router.post("/api/workbench/voices/import")
def import_workbench_voice(
    payload: dict[str, Any] = Body(...), db: Session = Depends(get_db)
):
    """Register a clone voice already present under the configured MiniMax key."""

    user = _token_user(str(payload.get("access_token", "")), db)
    try:
        voice, created = import_workbench_clone_voice(
            db,
            user,
            get_settings(),
            voice_id=str(payload.get("voice_id") or ""),
            name=str(payload.get("name") or ""),
            already_activated=payload.get("already_activated") is True,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiniMaxAPIError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(
        {**voice_payload(voice), "imported": True, "created": created},
        status_code=201 if created else 200,
    )


@router.post("/api/workbench/voices/{voice_asset_id}/preview")
def create_workbench_official_voice_preview(
    voice_asset_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    voice = _voice_for_user(voice_asset_id, user, db)
    cached_preview = False
    if voice.preview_relative_path:
        try:
            cached_preview = safe_relative_path(
                voice.preview_relative_path, get_settings().data_dir
            ).is_file()
        except ValueError:
            cached_preview = False
    if not cached_preview:
        require_new_work(
            db,
            user_id=user.id,
            identity=request_identity(request),
        )
    try:
        generate_official_voice_preview(
            db,
            user,
            voice,
            get_settings(),
            preview_text=str(
                payload.get("preview_text")
                or "你好，这是一段官方声音的试听内容。"
            ),
            cost_confirmed=payload.get("cost_confirmed") is True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MiniMaxAPIError, OSError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "preview_url": f"/api/workbench/voices/{voice.id}/preview",
    }


@router.get("/api/workbench/voices/{voice_asset_id}/preview")
def download_workbench_voice_preview(
    voice_asset_id: str, request: Request, db: Session = Depends(get_db)
):
    user = _bearer_user(request, db)
    voice = _voice_for_user(voice_asset_id, user, db)
    if not voice.preview_relative_path:
        raise HTTPException(status_code=404, detail="声音试听尚未生成")
    path = safe_relative_path(voice.preview_relative_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="声音试听文件不存在")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{voice.name}.mp3")


@router.post("/api/workbench/voices/{voice_asset_id}/activate")
def activate_workbench_saved_voice(
    voice_asset_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    voice = _voice_for_user(voice_asset_id, user, db)
    if voice.status != "ACTIVE":
        require_new_work(
            db,
            user_id=user.id,
            identity=request_identity(request),
        )
    try:
        activate_workbench_voice(
            db,
            user,
            voice,
            get_settings(),
            cost_confirmed=payload.get("cost_confirmed") is True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MiniMaxAPIError, OSError) as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return voice_payload(voice)


@router.post("/api/workbench/voices/{voice_asset_id}/delete")
def delete_workbench_saved_voice(
    voice_asset_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    voice = _voice_for_user(voice_asset_id, user, db)
    try:
        delete_workbench_voice(db, user, voice)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True, "voice_asset_id": voice_asset_id}


@router.post("/api/workbench/voice-creations")
def create_workbench_voice_creation(
    request: Request,
    access_token: str = Form(""),
    method: str = Form(...),
    name: str = Form(...),
    preview_text: str = Form(...),
    model: str = Form("speech-2.8-turbo"),
    weight_a: int | None = Form(None),
    noise_reduction: bool = Form(False),
    volume_normalization: bool = Form(False),
    cost_confirmed: bool = Form(False),
    source_a: UploadFile = File(...),
    source_b: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = workbench_user(request, db, body_token=access_token)
    check_rate_limit(
        request,
        f"workbench-voice-create:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    if source_b is not None and not source_b.filename:
        source_b = None
    task_id = str(uuid.uuid4())
    bind_new_operation(
        db,
        user_id=user.id,
        identity=request_identity(request),
        operation_kind="workbench.voice.create",
        request_snapshot={
            "task_id": task_id,
            "method": method,
            "name": name,
            "model": model,
            "weight_a": weight_a,
            "noise_reduction": noise_reduction,
            "volume_normalization": volume_normalization,
            "source_a_name": source_a.filename,
            "source_b_name": source_b.filename if source_b is not None else None,
        },
        resources=[("voice_creation_task", task_id)],
    )
    try:
        task = create_voice_task(
            db,
            user,
            get_settings(),
            method=method,
            name=name,
            preview_text=preview_text,
            model=model,
            source_a=source_a,
            source_b=source_b,
            weight_a=weight_a,
            noise_reduction=noise_reduction,
            volume_normalization=volume_normalization,
            cost_confirmed=cost_confirmed,
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            task_id=task_id,
        )
    except (UploadValidationError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(creation_task_payload(task), status_code=201)


@router.post("/api/workbench/voice-creations/{task_id}/save")
def save_workbench_voice_creation(
    task_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    task = _voice_task_for_user(task_id, user, db)
    if task.status != "SAVED":
        bind_new_operation(
            db,
            user_id=user.id,
            identity=request_identity(request),
            operation_kind="workbench.voice.save",
            request_snapshot={"task_id": task.id, "status": task.status},
            resources=[("voice_creation_task", task.id)],
        )
    try:
        request_voice_save(db, task)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return creation_task_payload(task)


@router.get("/api/workbench/voice-creations/{task_id}/preview")
def download_workbench_voice_creation_preview(
    task_id: str, request: Request, db: Session = Depends(get_db)
):
    user = _bearer_user(request, db)
    task = _voice_task_for_user(task_id, user, db)
    if not task.preview_relative_path:
        raise HTTPException(status_code=404, detail="声音试听尚未生成")
    path = safe_relative_path(task.preview_relative_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="声音试听文件不存在")
    return FileResponse(path, media_type="audio/mpeg", filename=f"{task.name}.mp3")


@router.post("/api/workbench/batch-assets")
def upload_workbench_batch_asset(
    request: Request,
    access_token: str = Form(...),
    kind: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = _token_user(access_token, db)
    check_rate_limit(
        request,
        f"workbench-batch-asset:{user.id}",
        max(get_settings().task_create_rate_limit_per_minute * 20, 100),
    )
    try:
        asset = stage_asset(db, user, file, kind, get_settings())
    except (UploadValidationError, StagedAssetError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "asset_id": asset.id,
            "kind": asset.kind,
            "original_name": asset.original_name,
        },
        status_code=201,
    )


@router.post("/api/workbench/audio-batches")
def create_workbench_audio_batch(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    check_rate_limit(
        request,
        f"workbench-audio-batch:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    request_key = str(payload.get("request_key") or "").strip()
    if not request_key:
        raise HTTPException(status_code=422, detail="工作台请求缺少幂等键")
    existing = db.scalar(
        select(GenerationBatch).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    if existing is not None:
        return _audio_batch_payload(_batch_for_user(existing.id, user, db))
    rows = payload.get("rows")
    speech_options = payload.get("speech_options")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=422, detail="工作台脚本行格式不正确")
    if not isinstance(speech_options, dict):
        raise HTTPException(status_code=422, detail="工作台声音参数格式不正确")
    selected_voice = _voice_for_user(
        str(speech_options.get("voiceAssetId") or ""), user, db
    )
    if (
        selected_voice.method != "system"
        and selected_voice.status != "ACTIVE"
    ):
        raise HTTPException(status_code=409, detail="请先在声音克隆中心激活该音色")
    normalized_speech_options = dict(speech_options)
    normalized_speech_options["reviewRequired"] = True
    created_directories: list[Path] = []
    try:
        plan = validate_workbench_audio_batch(
            db,
            user,
            get_settings(),
            rows=[{str(key): str(value or "") for key, value in row.items()} for row in rows],
            speech_options=normalized_speech_options,
            resolution=str(payload.get("resolution") or "1024"),
        )
        batch, created_directories = create_batch(
            db,
            user,
            get_settings(),
            name=str(payload.get("name") or "工作台声音批次"),
            request_key=request_key,
            plan=plan,
            correlation_id=str(payload.get("correlation_id") or "").strip() or None,
        )
        audio_tasks = [
            item.audio_task for item in batch.items if item.audio_task is not None
        ]
        bind_new_operation(
            db,
            user_id=user.id,
            identity=request_identity(request),
            operation_kind="workbench.audio.generate",
            request_snapshot={
                "batch_id": batch.id,
                "request_key": request_key,
                "audio_task_ids": [task.id for task in audio_tasks],
            },
            resources=[("audio_generation_task", task.id) for task in audio_tasks],
        )
        db.commit()
        batch = _batch_for_user(batch.id, user, db)
        log_event(
            logger,
            "workbench.audio_batch_created",
            "新版工作台声音批次已创建",
            user_id=user.id,
            username=user.username,
            batch_id=batch.id,
            source_channel=batch.source_channel,
            correlation_id=batch.correlation_id or batch.id,
            item_count=batch.total_items,
        )
    except BatchValidationError as exc:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        return JSONResponse(
            {"detail": str(exc), "errors": exc.errors}, status_code=400
        )
    except (OSError, ValueError) as exc:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(_audio_batch_payload(batch), status_code=201)


@router.post("/api/workbench/audio-batches/lookup")
def lookup_workbench_audio_batch(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Read a lost submission receipt; this route never creates paid work."""
    user = _token_user(str(payload.get("access_token", "")), db)
    request_key = payload.get("request_key")
    if not isinstance(request_key, str) or not 1 <= len(request_key.strip()) <= 64:
        raise HTTPException(status_code=422, detail="工作台请求标识不合法")
    batch = db.scalar(select(GenerationBatch).where(
        GenerationBatch.user_id == user.id,
        GenerationBatch.request_key == request_key.strip(),
        GenerationBatch.source_channel == BATCH_SOURCE_NEW_WORKBENCH,
        GenerationBatch.audio_mode == "minimax",
    ))
    result = {"schema": "runninghub.workbench-audio-lookup.v1", "found": batch is not None}
    if batch is None:
        # Absence is only an observation, not proof that an in-flight request
        # cannot commit later. Clients must not use this to authorize a new key.
        return result
    batch = _batch_for_user(batch.id, user, db)
    result["batch"] = _audio_batch_payload(batch)
    result["request_key"] = batch.request_key
    result["input_bindings"] = {
        item.id: {
            "script_sha256": hashlib.sha256(item.audio_task.speech_script.encode("utf-8")).hexdigest(),
            "voice_asset_id": item.audio_task.voice_asset_id,
            "speech_settings": {
                "model": item.audio_task.model,
                "speed": item.audio_task.speed,
                "volume": item.audio_task.volume,
                "pitch": item.audio_task.pitch,
                "languageBoost": item.audio_task.language_boost,
                "outputFormat": item.audio_task.output_format,
            },
        }
        for item in batch.items if item.audio_task is not None
    }
    return result


@router.post("/api/workbench/audio-batches/{batch_id}")
def workbench_audio_batch(
    batch_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    return _audio_batch_payload(_batch_for_user(batch_id, user, db))


@router.get("/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio")
def download_workbench_audio(
    batch_id: str, item_id: str, request: Request, db: Session = Depends(get_db)
):
    user = _bearer_user(request, db)
    batch = _batch_for_user(batch_id, user, db)
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if item is None or item.audio_task is None or not item.audio_task.output_path:
        raise HTTPException(status_code=404, detail="生成音频尚未准备完成")
    path = safe_relative_path(item.audio_task.output_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="生成音频文件不存在")
    ensure_generated_speech_mastered(path)
    return FileResponse(path, media_type="audio/mpeg", filename=f"{item.row_key}.mp3")


@router.post("/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry")
def retry_workbench_audio(
    batch_id: str,
    item_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(status_code=409, detail="请确认重新生成声音可能再次产生费用")
    batch = _batch_for_user(batch_id, user, db)
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if item is None or item.audio_task is None:
        raise HTTPException(status_code=404, detail="声音任务不存在")
    try:
        requested_speed = float(payload.get("speed", item.audio_task.speed))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="语速必须是数字") from exc
    if not 0.5 <= requested_speed <= 2.0:
        raise HTTPException(status_code=422, detail="语速必须在 0.5–2.0 之间")
    try:
        if item.audio_task.status == AudioTaskStatus.AWAITING_REVIEW.value:
            item.audio_task.speed = requested_speed
            regenerate_item_audio(batch, item_id)
        elif item.audio_task.status == AudioTaskStatus.FAILED.value:
            item.audio_task.speed = requested_speed
            item.audio_task.status = AudioTaskStatus.PENDING.value
            item.audio_task.error_code = None
            item.audio_task.error_message = None
            item.audio_task.completed_at = None
            item.audio_status = "PENDING"
            item.status = "AUDIO_PENDING"
        else:
            raise AudioReviewError("当前声音状态不能重新生成")
    except AudioReviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    bind_new_operation(
        db,
        user_id=user.id,
        identity=request_identity(request),
        operation_kind="workbench.audio.retry",
        request_snapshot={
            "batch_id": batch.id,
            "item_id": item.id,
            "audio_task_id": item.audio_task.id,
            "generation_version": item.audio_task.generation_version,
            "speed": requested_speed,
        },
        resources=[("audio_generation_task", item.audio_task.id)],
    )
    db.commit()
    return _audio_batch_payload(_batch_for_user(batch_id, user, db))


@router.post(
    "/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition"
)
def start_workbench_composition(
    batch_id: str,
    item_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Approve one generated audio version and hand it to the existing workers."""

    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(status_code=409, detail="请确认画面生成会产生 RunningHub 费用")
    if not str(payload.get("idempotency_key") or "").strip():
        raise HTTPException(status_code=422, detail="画面生成请求缺少幂等键")
    batch = _batch_for_user(batch_id, user, db)
    request_correlation_id = str(payload.get("correlation_id") or "").strip()
    batch_correlation_id = batch.correlation_id or batch.id
    if request_correlation_id and request_correlation_id != batch_correlation_id:
        raise HTTPException(status_code=409, detail="日志关联标识与声音批次不一致")
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if item is None or item.audio_task is None:
        raise HTTPException(status_code=404, detail="声音任务不存在")
    task = item.audio_task
    if (
        _h3_audio_is_approved(item)
        and not item.segments
        and item.generation_task is None
    ):
        raise HTTPException(
            status_code=409,
            detail="新版工作台只支持多参考生成；普通数字人交接请求已停止",
        )
    try:
        requested_image_sha256 = _payload_image_sha256(payload)
    except AudioReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    selected_seedvr2_account_ids = None
    execution_mode = (
        batch_execution_mode(batch)
        if batch.execution_mode is not None
        else resolve_execution_mode(
            db,
            user=user,
            source_channel=batch.source_channel,
            workflow_type=batch.workflow_type,
        )
    )
    try:
        bind_batch_execution_mode(db, batch, execution_mode)
        allow_item_account_replace = (
            task.reviewed_at is None
            and not item.segments
            and item.generation_task is None
        )
        if execution_mode == BATCH_EXECUTION_MODE_DUAL_POOL_V1:
            selected_account_ids = validate_workbench_execution_account_selection(
                db,
                user,
                selection_provided="runninghub_execution_account_ids" in payload,
                raw_selection=payload.get("runninghub_execution_account_ids"),
                allow_non_admin=True,
            )
            selected_seedvr2_account_ids = validate_seedvr2_account_selection(
                db, user=user, raw_selection=payload.get("seedvr2_execution_account_ids")
            )
            if batch_execution_account_snapshot(batch) is None:
                bind_batch_execution_account_snapshot(db, batch, selected_account_ids)
            if seedvr2_batch_account_snapshot(batch) is None:
                bind_seedvr2_batch_account_snapshot(
                    db, batch, selected_seedvr2_account_ids
                )
            bind_item_execution_account_snapshot(
                db,
                item,
                selected_account_ids,
                allow_replace=allow_item_account_replace,
            )
            bind_seedvr2_item_account_snapshot(
                db,
                item,
                selected_seedvr2_account_ids,
                allow_replace=allow_item_account_replace,
            )
        else:
            if "seedvr2_execution_account_ids" in payload:
                raise RunningHubDualPoolError(
                    "当前画面生成操作未进入双资源池模式，请刷新账号列表后重新确认"
                )
            selected_account_ids = validate_workbench_execution_account_selection(
                db,
                user,
                selection_provided="runninghub_execution_account_ids" in payload,
                raw_selection=payload.get("runninghub_execution_account_ids"),
                allow_non_admin=user_has_dual_pool_entitlement(db, user),
            )
            if batch_execution_account_snapshot(batch) is None:
                bind_batch_execution_account_snapshot(db, batch, selected_account_ids)
            bind_item_execution_account_snapshot(
                db,
                item,
                selected_account_ids,
                allow_replace=allow_item_account_replace,
            )
    except RunningHubPoolSelectionFormatError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SeedVR2PoolSelectionFormatError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RunningHubPoolSelectionPermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SeedVR2PoolSelectionPermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (
        RunningHubPoolSelectionUnavailableError,
        RunningHubPoolSnapshotConflictError,
        RunningHubDualPoolError,
        SeedVR2PoolSelectionUnavailableError,
        SeedVR2PoolSnapshotConflictError,
    ) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    materialized_image: Path | None = None
    try:
        try:
            video_parameters = json.loads(task.video_parameters_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AudioReviewError("画面生成参数不合法") from exc
        if not isinstance(video_parameters, dict):
            raise AudioReviewError("画面生成参数不合法")
        requested_resolution = str(
            payload.get("resolution")
            or video_parameters.get("resolution")
            or "1024"
        ).strip()
        try:
            if int(requested_resolution) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise AudioReviewError("数字人最长边分辨率必须是正整数") from exc
        current_resolution = str(
            video_parameters.get("resolution") or "1024"
        ).strip()
        handoff_started = bool(
            task.reviewed_at is not None
            or item.segments
            or item.generation_task
        )
        resolution_changed = requested_resolution != current_resolution
        # The new workbench hands MiniMax timestamped audio to 4A. Keep the
        # authoritative speech duration and raw cues unchanged. The video
        # worker creates a temporary two-second provider input tail later.
        video_parameters["timing_mode"] = "exact_timestamps"
        video_parameters[GENERATION_TAIL_PARAMETER] = DIGITAL_HUMAN_TAIL_PADDING_SECONDS
        # Older local workbench builds injected this short placeholder into
        # every row, which accidentally overrode the user's complete server
        # configuration. Refresh only that exact legacy value at the paid 4A
        # handoff; custom row prompts remain untouched.
        if (
            batch.source_channel == BATCH_SOURCE_NEW_WORKBENCH
            and str(video_parameters.get("prompt") or "").strip()
            == _LEGACY_WORKBENCH_DIGITAL_PROMPT
        ):
            configured_prompt = str(
                get_user_workflow_config(
                    user, DIGITAL_HUMAN_WORKFLOW
                ).default_prompt
                or ""
            ).strip()
            if configured_prompt:
                video_parameters["prompt"] = configured_prompt
        # Module 4A in the new workbench always includes SeedVR2.  Older audio
        # rows were created before this snapshot field existed; allowing the
        # workflow validator's False default here would silently skip the
        # enhancement stage after the user had confirmed the complete 4A cost.
        if batch.source_channel == BATCH_SOURCE_NEW_WORKBENCH:
            video_parameters["seedvr2_enabled"] = True
        current_image_sha256 = str(task.primary_sha256 or "").strip().lower()
        if task.primary_path and not current_image_sha256:
            try:
                current_image_path = safe_relative_path(
                    task.primary_path, get_settings().data_dir
                )
            except ValueError as exc:
                raise AudioReviewError("已绑定的项目图片路径不合法") from exc
            if not current_image_path.is_file():
                raise AudioReviewError("已绑定的项目图片不存在")
            current_image_sha256 = _file_sha256(current_image_path)
            task.primary_sha256 = current_image_sha256

        image_changed = bool(
            task.primary_path
            and requested_image_sha256
            and requested_image_sha256 != current_image_sha256
        )
        video_handoff_reset = False
        if handoff_started and resolution_changed:
            if not _has_cancelled_digital_human_handoff(item):
                raise AudioReviewError("当前画面生成任务的分辨率已经锁定，不能修改")
            _reset_video_handoff_for_new_image(db, item)
            video_handoff_reset = True
        video_parameters["resolution"] = requested_resolution
        task.video_parameters_json = json.dumps(
            video_parameters, ensure_ascii=False
        )
        if not task.primary_path or image_changed:
            image_asset_id = str(payload.get("image_asset_id") or "").strip()
            if not image_asset_id:
                raise AudioReviewError("4A 画面生成缺少当前项目图片")
            try:
                image_asset = load_available_assets(db, user, [image_asset_id])[0]
            except StagedAssetError as exc:
                raise AudioReviewError(str(exc)) from exc
            if image_asset.kind != "image":
                raise AudioReviewError("4A 画面素材必须是图片")
            source = safe_relative_path(
                image_asset.relative_path, get_settings().data_dir
            )
            uploaded_image_sha256 = _file_sha256(source)
            if (
                requested_image_sha256
                and uploaded_image_sha256 != requested_image_sha256
            ):
                raise AudioReviewError("上传图片与当前项目图片版本不一致")
            if task.primary_path and not video_handoff_reset:
                _reset_video_handoff_for_new_image(db, item)
            materialized_image = materialize_staged_asset(
                source,
                task_upload_dir(
                    get_settings(), user.id, task.planned_generation_task_id
                ),
                kind="image",
            )
            task.primary_kind = "image"
            task.primary_path = to_relative_data_path(
                materialized_image, get_settings()
            )
            task.primary_original_name = image_asset.original_name
            task.primary_sha256 = uploaded_image_sha256
            image_asset.consumed_at = datetime.now(timezone.utc)
            db.flush()
        if task.status == AudioTaskStatus.AWAITING_REVIEW.value:
            approve_item_audio(batch, item_id)
        elif (
            task.status != AudioTaskStatus.FAILED.value
            and task.reviewed_at is not None
            and current_attempt(task).status == "APPROVED"
        ) or item.segments or item.generation_task:
            # Retried HTTP requests must not create another paid handoff.
            pass
        else:
            raise AudioReviewError("当前声音尚未准备好进入画面生成")
        if not handoff_started or video_handoff_reset:
            bind_new_operation(
                db,
                user_id=user.id,
                identity=request_identity(request),
                operation_kind="workbench.composition",
                request_snapshot={
                    "batch_id": batch.id,
                    "item_id": item.id,
                    "audio_task_id": task.id,
                    "idempotency_key": str(payload.get("idempotency_key") or ""),
                    "image_sha256": task.primary_sha256,
                    "resolution": requested_resolution,
                },
                resources=[("audio_generation_task", task.id)],
            )
    except AudioReviewError as exc:
        db.rollback()
        if materialized_image is not None:
            materialized_image.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    log_event(
        logger,
        "workbench.composition_started",
        "新版工作台画面生成已开始",
        user_id=user.id,
        username=user.username,
        batch_id=batch.id,
        batch_item_id=item.id,
        source_channel=batch.source_channel,
        correlation_id=batch_correlation_id,
        image_sha256=task.primary_sha256,
        runninghub_execution_account_ids=selected_account_ids,
        seedvr2_execution_account_ids=selected_seedvr2_account_ids,
        execution_mode=execution_mode,
    )
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.post("/api/workbench/tasks/{item_id}/composition/retry")
def retry_workbench_composition(
    item_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Create a new command for the failed/cancelled active stage of one row."""

    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    identity = request_identity(request)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认失败或已取消的 RunningHub 阶段重新生成可能再次产生费用",
        )
    item = _item_for_user(item_id, user, db)
    requested_resolution = str(payload.get("resolution") or "").strip()
    if requested_resolution:
        try:
            if int(requested_resolution) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="数字人最长边分辨率必须是正整数",
            ) from exc
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ]
    if not tasks and item.generation_task is not None:
        tasks = [item.generation_task]
    retryable = [
        task
        for task in tasks
        if task.status
        in {
            TaskStatus.FAILED.value,
            TaskStatus.DOWNLOAD_FAILED.value,
            TaskStatus.CANCELLED.value,
        }
    ]
    paid_retryable = [
        task for task in retryable if task_retry_starts_new_provider_work(task)
    ]
    try:
        if retryable:
            for task in retryable:
                # A provider-side cancel closes that paid command.  Reusing its
                # RunningHub task id is impossible, so a digital-human cancel
                # becomes a fresh submission from the saved image/audio and
                # uses the workbench's current resolution.  SeedVR2 cancels
                # already have an enhancement/source and only restart that
                # enhancement stage below.
                if (
                    requested_resolution
                    and task.status == TaskStatus.CANCELLED.value
                    and task.enhancement is None
                ):
                    try:
                        task_input = json.loads(task.input_payload)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise TaskManagementError(
                            "已取消数字人任务的输入快照不合法，无法重新生成"
                        ) from exc
                    parameters = task_input.get("parameters")
                    if not isinstance(parameters, dict):
                        raise TaskManagementError(
                            "已取消数字人任务的参数快照不合法，无法重新生成"
                        )
                    parameters["resolution"] = requested_resolution
                    task.input_payload = json.dumps(task_input, ensure_ascii=False)
                prepare_task_retry(
                    task,
                    get_settings(),
                    device_identity=identity,
                )
            invalidate_merged_video(item, get_settings())
        elif item.merged_video_status == MERGE_FAILED:
            retry_video_merge(item, get_settings())
        else:
            raise TaskManagementError("当前画面任务没有可重试的失败阶段")
    except (TaskManagementError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if paid_retryable:
        bind_new_operation(
            db,
            user_id=user.id,
            identity=identity,
            operation_kind="workbench.composition.retry",
            request_snapshot={
                "item_id": item.id,
                "task_ids": [task.id for task in paid_retryable],
                "resolution": requested_resolution or None,
            },
            resources=[task_resource(task) for task in paid_retryable],
        )
    db.commit()
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.post("/api/workbench/tasks/{item_id}/enhancement/backfill")
def backfill_workbench_video_enhancement(
    item_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Send saved digital-human source segments to SeedVR2 without rerunning 4A."""

    user = workbench_user(
        request,
        db,
        body_token=str(payload.get("access_token", "")),
    )
    identity = request_identity(request)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认补跑 SeedVR2 48G 视频清晰化会产生 RunningHub 费用",
        )
    item = _item_for_user(item_id, user, db)
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ]
    if not tasks and item.generation_task is not None:
        tasks = [item.generation_task]
    if not tasks:
        raise HTTPException(status_code=409, detail="当前任务没有数字人源片段")

    settings = get_settings()
    try:
        # Validate the complete row before changing any stage so six-segment
        # rows cannot be left half queued when one historical file is missing.
        for task in tasks:
            enhancement = task.enhancement
            if enhancement is None:
                historical_source_path(task, settings)
            elif enhancement.status != EnhancementStatus.SUCCESS.value:
                historical_source_path(task, settings)
                if enhancement.status == EnhancementStatus.CANCELLED.value:
                    raise VideoEnhancementBackfillError(
                        "SeedVR2 清晰化已取消，不能自动补跑"
                    )

        queued_count = 0
        retried_count = 0
        paid_tasks: list[GenerationTask] = []
        for task in tasks:
            enhancement = task.enhancement
            if enhancement is None:
                queue_historical_seedvr2_enhancement(task, settings)
                queued_count += 1
                paid_tasks.append(task)
            elif task.status in {
                TaskStatus.FAILED.value,
                TaskStatus.DOWNLOAD_FAILED.value,
            }:
                starts_new_work = task_retry_starts_new_provider_work(task)
                prepare_task_retry(
                    task,
                    settings,
                    device_identity=identity,
                )
                retried_count += 1
                if starts_new_work:
                    paid_tasks.append(task)
        if queued_count or retried_count:
            invalidate_merged_video(item, settings)
        if paid_tasks:
            bind_new_operation(
                db,
                user_id=user.id,
                identity=identity,
                operation_kind="workbench.enhancement.backfill",
                request_snapshot={
                    "item_id": item.id,
                    "task_ids": [task.id for task in paid_tasks],
                },
                resources=[task_resource(task) for task in paid_tasks],
            )
    except (
        VideoEnhancementBackfillError,
        TaskManagementError,
        OSError,
        ValueError,
    ) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.commit()
    refreshed = _item_for_user(item_id, user, db)
    manifest = _workbench_manifest(refreshed)
    manifest["seedvr2_backfill"] = {
        "queued_count": queued_count,
        "retried_count": retried_count,
        "already_attached_count": len(tasks) - queued_count - retried_count,
        "digital_human_rerun_count": 0,
        "instance_type": "plus",
        "gpu_memory": "48G",
    }
    log_event(
        logger,
        "workbench.seedvr2_backfill_queued",
        "历史数字人源片段已进入 SeedVR2 48G 清晰化",
        user_id=user.id,
        username=user.username,
        batch_id=item.batch_id,
        batch_item_id=item.id,
        queued_count=queued_count,
        retried_count=retried_count,
        digital_human_rerun_count=0,
    )
    return manifest


@router.get("/api/workbench/tasks/{item_id}/base-video")
def download_workbench_base_video(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = _item_for_user(item_id, user, db)
    if (
        not item.merged_video_path
        or item.merged_video_status
        not in {MERGED_PREVIEW_READY, MERGED_VIDEO_READY}
    ):
        raise HTTPException(status_code=409, detail="基础视频尚未生成完成")
    try:
        path = safe_relative_path(item.merged_video_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="基础视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="基础视频文件不存在")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{item.row_key}-base.mp4",
    )
