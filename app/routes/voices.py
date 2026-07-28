from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.routes.dependencies import check_rate_limit, get_current_user, get_page_user
from app.services.csrf import require_csrf
from app.services.speech.voice_studio import create_voice_task, request_voice_save
from app.services.storage import UploadValidationError, safe_relative_path
from app.web import templates


router = APIRouter(tags=["voices"])
AVAILABLE_VOICE_STATUSES = {
    VoiceAssetStatus.READY.value,
    VoiceAssetStatus.ACTIVE.value,
}
ACTIVE_CREATION_STATUSES = {
    VoiceCreationStatus.PENDING.value,
    VoiceCreationStatus.CLONING.value,
    VoiceCreationStatus.SYNTHESIZING.value,
    VoiceCreationStatus.SAVE_PENDING.value,
    VoiceCreationStatus.SAVING.value,
}


def _task_for_user(
    db: Session,
    task_id: str,
    user: User,
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


@router.get("/voices")
def voice_studio_page(
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    account_binding_id = (
        current_user.minimax_config.account_binding_id
        if current_user.minimax_config
        else None
    )
    tasks = db.scalars(
        select(VoiceCreationTask)
        .options(selectinload(VoiceCreationTask.voice_asset))
        .where(VoiceCreationTask.user_id == current_user.id)
        .order_by(VoiceCreationTask.created_at.desc())
        .limit(30)
    ).all()
    voices = (
        db.scalars(
            select(MiniMaxVoiceAsset)
            .where(
                MiniMaxVoiceAsset.user_id == current_user.id,
                MiniMaxVoiceAsset.account_binding_id == account_binding_id,
                MiniMaxVoiceAsset.is_saved.is_(True),
                MiniMaxVoiceAsset.status.in_(AVAILABLE_VOICE_STATUSES),
            )
            .order_by(MiniMaxVoiceAsset.created_at.desc())
        ).all()
        if account_binding_id
        else []
    )
    return templates.TemplateResponse(
        request,
        "voices.html",
        {
            "current_user": current_user,
            "minimax_configured": bool(
                current_user.minimax_config
                and current_user.minimax_config.api_key_encrypted
                and account_binding_id
            ),
            "voice_tasks": tasks,
            "voices": voices,
            "has_active_tasks": any(
                task.status in ACTIVE_CREATION_STATUSES for task in tasks
            ),
        },
    )


@router.post("/api/voice-creations")
def create_voice_creation(
    request: Request,
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
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    check_rate_limit(
        request,
        f"voice-create:{current_user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    if source_b is not None and not source_b.filename:
        source_b = None
    try:
        task = create_voice_task(
            db,
            current_user,
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
    return JSONResponse({"taskId": task.id}, status_code=201)


@router.post("/api/voice-creations/{task_id}/save")
def save_voice_creation(
    task_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _task_for_user(db, task_id, current_user)
    try:
        request_voice_save(db, task)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": task.status}


@router.get("/api/voice-creations/{task_id}/preview")
def voice_creation_preview(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _task_for_user(db, task_id, current_user)
    if not task.preview_relative_path:
        raise HTTPException(status_code=404, detail="试听音频尚未生成")
    path = safe_relative_path(task.preview_relative_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="试听音频不存在")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"{task.name}-preview{path.suffix}",
    )
