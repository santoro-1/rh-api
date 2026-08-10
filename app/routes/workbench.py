from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    AudioTaskStatus,
    BATCH_SOURCE_NEW_WORKBENCH,
    EnhancementStatus,
    GenerationBatch,
    GenerationBatchItem,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceCreationTask,
)
from app.routes.dependencies import check_rate_limit
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
from app.services.visual_analysis import (
    VisualAnalysisInputError,
    VisualAnalysisUnavailable,
    analyze_visual_context,
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
    validate_workbench_execution_account_selection,
    workbench_execution_account_summary,
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


def _composition_payload(item: GenerationBatchItem) -> dict[str, Any]:
    tasks = [
        segment.generation_task
        for segment in sorted(item.segments, key=lambda value: value.segment_index)
        if segment.generation_task is not None
    ]
    audio_task = item.audio_task
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
        task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}
        for task in tasks
    ):
        status = "COMPOSITION_FAILED"
        failed = next(
            task
            for task in tasks
            if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}
        )
        error_message = failed.error_message or failed.runninghub_failed_reason
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
    elif audio_task is not None and (
        audio_task.reviewed_at is not None
        or audio_task.status
        not in {AudioTaskStatus.AWAITING_REVIEW.value, AudioTaskStatus.FAILED.value}
    ):
        status = "COMPOSITION_QUEUED"
    elif audio_task is not None and audio_task.status == AudioTaskStatus.FAILED.value:
        status = "COMPOSITION_FAILED"
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
        "runninghub_execution_account_ids": batch_execution_account_snapshot(
            item.batch
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
    return workbench_execution_account_summary(db, user)


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


@router.post("/api/workbench/content-analysis")
def workbench_content_analysis(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Analyze one exact script without exposing Ark credentials to workbench."""

    user = _token_user(str(payload.get("access_token", "")), db)
    original_script = payload.get("original_script")
    force_refresh = payload.get("force_refresh", False)
    visual_context = payload.get("visual_context")
    if not isinstance(original_script, str):
        raise HTTPException(status_code=400, detail="original_script 必须是字符串")
    if type(force_refresh) is not bool:
        raise HTTPException(status_code=400, detail="force_refresh 必须是布尔值")
    if visual_context is not None and not isinstance(visual_context, dict):
        raise HTTPException(status_code=400, detail="visual_context 必须是对象")
    try:
        kwargs: dict[str, Any] = {
            "original_script": original_script,
            "force_refresh": force_refresh,
        }
        if visual_context is not None:
            kwargs["visual_context_payload"] = visual_context
        return analyze_content(db, user, **kwargs)
    except ContentAnalysisInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ContentAnalysisUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/workbench/visual-analysis")
def workbench_visual_analysis(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Analyze only local semantic candidates without exposing Ark credentials."""

    user = _token_user(str(payload.get("access_token", "")), db)
    force_refresh = payload.get("force_refresh", False)
    if type(force_refresh) is not bool:
        raise HTTPException(status_code=400, detail="force_refresh 必须是布尔值")
    request_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"access_token", "force_refresh"}
    }
    try:
        return analyze_visual_context(
            db,
            user,
            payload=request_payload,
            force_refresh=force_refresh,
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
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    voice = _voice_for_user(voice_asset_id, user, db)
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
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    voice = _voice_for_user(voice_asset_id, user, db)
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
    access_token: str = Form(...),
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
    user = _token_user(access_token, db)
    check_rate_limit(
        request,
        f"workbench-voice-create:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    if source_b is not None and not source_b.filename:
        source_b = None
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
        )
    except (UploadValidationError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(creation_task_payload(task), status_code=201)


@router.post("/api/workbench/voice-creations/{task_id}/save")
def save_workbench_voice_creation(
    task_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    task = _voice_task_for_user(task_id, user, db)
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
    user = _token_user(str(payload.get("access_token", "")), db)
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
    return FileResponse(path, media_type="audio/mpeg", filename=f"{item.row_key}.mp3")


@router.post("/api/workbench/audio-batches/{batch_id}/items/{item_id}/retry")
def retry_workbench_audio(
    batch_id: str,
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token", "")), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(status_code=409, detail="请确认重新生成声音可能再次产生费用")
    batch = _batch_for_user(batch_id, user, db)
    item = next((candidate for candidate in batch.items if candidate.id == item_id), None)
    if item is None or item.audio_task is None:
        raise HTTPException(status_code=404, detail="声音任务不存在")
    try:
        if item.audio_task.status == AudioTaskStatus.AWAITING_REVIEW.value:
            regenerate_item_audio(batch, item_id)
        elif item.audio_task.status == AudioTaskStatus.FAILED.value:
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
    db.commit()
    return _audio_batch_payload(_batch_for_user(batch_id, user, db))


@router.post(
    "/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition"
)
def start_workbench_composition(
    batch_id: str,
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Approve one generated audio version and hand it to the existing workers."""

    user = _token_user(str(payload.get("access_token", "")), db)
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
    try:
        requested_image_sha256 = _payload_image_sha256(payload)
    except AudioReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        selected_account_ids = validate_workbench_execution_account_selection(
            db,
            user,
            selection_provided="runninghub_execution_account_ids" in payload,
            raw_selection=payload.get("runninghub_execution_account_ids"),
        )
        bind_batch_execution_account_snapshot(db, batch, selected_account_ids)
    except RunningHubPoolSelectionFormatError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RunningHubPoolSelectionPermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (
        RunningHubPoolSelectionUnavailableError,
        RunningHubPoolSnapshotConflictError,
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
        if handoff_started and requested_resolution != current_resolution:
            raise AudioReviewError("当前画面生成任务的分辨率已经锁定，不能修改")
        video_parameters["resolution"] = requested_resolution
        # The new workbench hands MiniMax timestamped audio to 4A.  Preserve
        # the existing whole-second ceiling (24.4 -> 25), but do not apply the
        # legacy 0.5-second silent tail used by upload-audio batch workflows.
        video_parameters["timing_mode"] = "exact_timestamps"
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
        task.video_parameters_json = json.dumps(
            video_parameters, ensure_ascii=False
        )
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
            if task.primary_path:
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
            task.reviewed_at is not None
            and current_attempt(task).status == "APPROVED"
        ) or item.segments or item.generation_task:
            # Retried HTTP requests must not create another paid handoff.
            pass
        else:
            raise AudioReviewError("当前声音尚未准备好进入画面生成")
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
    )
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.post("/api/workbench/tasks/{item_id}/composition/retry")
def retry_workbench_composition(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Retry only the failed RunningHub/download/merge stage for one row."""

    user = _token_user(str(payload.get("access_token", "")), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认失败的 RunningHub 任务重试可能再次产生费用",
        )
    item = _item_for_user(item_id, user, db)
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
        if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}
    ]
    try:
        if retryable:
            for task in retryable:
                prepare_task_retry(task, get_settings())
            invalidate_merged_video(item, get_settings())
        elif item.merged_video_status == MERGE_FAILED:
            retry_video_merge(item, get_settings())
        else:
            raise TaskManagementError("当前画面任务没有可重试的失败阶段")
    except (TaskManagementError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return _workbench_manifest(_item_for_user(item_id, user, db))


@router.post("/api/workbench/tasks/{item_id}/enhancement/backfill")
def backfill_workbench_video_enhancement(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    """Send saved digital-human source segments to SeedVR2 without rerunning 4A."""

    user = _token_user(str(payload.get("access_token", "")), db)
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
        for task in tasks:
            enhancement = task.enhancement
            if enhancement is None:
                queue_historical_seedvr2_enhancement(task, settings)
                queued_count += 1
            elif task.status in {
                TaskStatus.FAILED.value,
                TaskStatus.DOWNLOAD_FAILED.value,
            }:
                prepare_task_retry(task, settings)
                retried_count += 1
        if queued_count or retried_count:
            invalidate_merged_video(item, settings)
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
