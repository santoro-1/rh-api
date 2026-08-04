from __future__ import annotations

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
    GenerationBatch,
    GenerationBatchItem,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceCreationTask,
)
from app.routes.dependencies import check_rate_limit
from app.services.audio_review import AudioReviewError, regenerate_item_audio
from app.services.batch_assets import StagedAssetError, stage_asset
from app.services.batch_generation import BatchValidationError, create_batch, validate_batch
from app.services.batch_manifests import DIGITAL_HUMAN_WORKFLOW
from app.services.batch_status import batch_query
from app.services.postproduction import postproduction_manifest
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
    voice_payload,
)
from app.services.storage import (
    UploadValidationError,
    remove_directory,
    safe_relative_path,
)
from app.services.workbench_auth import (
    HANDOFF_LIFETIME_SECONDS,
    decode_workbench_token,
    issue_workbench_token,
    public_workbench_user,
    token_matches_user,
    workbench_handoffs,
)


router = APIRouter(tags=["workbench"])


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
        "name": batch.name,
        "review_required": batch.review_required,
        "items": items,
    }


def _workbench_manifest(item: GenerationBatchItem) -> dict[str, Any]:
    manifest = postproduction_manifest(item, get_settings())
    for video in manifest["source"]["videos"]:
        video["download_url"] = (
            f"/api/workbench/tasks/{item.id}/videos/{video['index']}"
        )
        video.pop("preview_url", None)
    manifest["batch_name"] = item.batch.name
    manifest["created_at"] = item.batch.created_at.isoformat()
    manifest["updated_at"] = item.updated_at.isoformat()
    return manifest


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
    return {
        "schema": "runninghub.workbench-voices.v1",
        "voices": [
            voice_payload(voice) for voice in available_workbench_voices(db, user)
        ],
        "creation_tasks": [creation_task_payload(task) for task in tasks],
    }


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
    asset_ids = payload.get("asset_ids")
    speech_options = payload.get("speech_options")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=422, detail="工作台脚本行格式不正确")
    if not isinstance(asset_ids, list):
        raise HTTPException(status_code=422, detail="工作台图片素材格式不正确")
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
        plan = validate_batch(
            db,
            user,
            get_settings(),
            workflow_type=DIGITAL_HUMAN_WORKFLOW,
            rows=[{str(key): str(value or "") for key, value in row.items()} for row in rows],
            asset_ids=[str(value) for value in asset_ids],
            batch_parameters={
                "person_mode": "单人",
                "resolution": str(payload.get("resolution") or "1024"),
            },
            audio_mode="minimax",
            speech_options=normalized_speech_options,
            review_required=False,
            video_review_required=False,
        )
        batch, created_directories = create_batch(
            db,
            user,
            get_settings(),
            name=str(payload.get("name") or "工作台声音批次"),
            request_key=request_key,
            plan=plan,
        )
        db.commit()
        batch = _batch_for_user(batch.id, user, db)
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
