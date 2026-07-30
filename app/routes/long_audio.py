from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import LongAudioProject, LongAudioProjectStatus, User
from app.routes.dependencies import (
    check_rate_limit,
    get_current_user,
    get_page_user,
)
from app.services.csrf import require_csrf
from app.services.long_audio import (
    LongAudioError,
    confirm_long_audio_project,
    create_long_audio_project,
    save_reviewed_plan,
)
from app.services.storage import (
    UploadValidationError,
    long_audio_project_dir,
    remove_directory,
    safe_relative_path,
)
from app.web import templates


router = APIRouter(tags=["long-audio"])

STATUS_LABELS = {
    LongAudioProjectStatus.PENDING_ANALYSIS.value: "等待分析",
    LongAudioProjectStatus.ANALYZING.value: "正在分析停顿和脚本",
    LongAudioProjectStatus.REVIEW.value: "等待试听确认",
    LongAudioProjectStatus.PENDING_CUT.value: "等待切割",
    LongAudioProjectStatus.CUTTING.value: "正在切割音频和视频",
    LongAudioProjectStatus.COMPLETED.value: "已创建视频任务",
    LongAudioProjectStatus.FAILED.value: "处理失败",
}


def _ensure_access(project: LongAudioProject, user: User) -> None:
    if project.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=404, detail="长音频项目不存在")


def _load_project(db: Session, project_id: str, user: User) -> LongAudioProject:
    project = db.get(LongAudioProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="长音频项目不存在")
    _ensure_access(project, user)
    return project


def _plan_payload(project: LongAudioProject) -> list[dict[str, Any]]:
    try:
        value = json.loads(project.plan_json or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _project_payload(project: LongAudioProject) -> dict[str, Any]:
    return {
        "projectId": project.id,
        "name": project.name,
        "status": project.status,
        "statusLabel": STATUS_LABELS.get(project.status, project.status),
        "durationSeconds": project.duration_seconds,
        "alignmentProvider": project.alignment_provider,
        "segments": _plan_payload(project),
        "errorCode": project.error_code,
        "errorMessage": project.error_message,
        "batchId": project.batch_id,
        "expiresAt": project.expires_at.isoformat(),
        "createdAt": project.created_at.isoformat(),
    }


@router.get("/long-audio")
def long_audio_page(
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    projects = db.scalars(
        select(LongAudioProject)
        .where(LongAudioProject.user_id == current_user.id)
        .order_by(LongAudioProject.created_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request,
        "long_audio.html",
        {
            "current_user": current_user,
            "projects": projects,
            "status_labels": STATUS_LABELS,
            "alignment_provider": get_settings().long_audio_alignment_provider,
        },
    )


@router.get("/long-audio/{project_id}")
def long_audio_detail_page(
    request: Request,
    project_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    project = _load_project(db, project_id, current_user)
    return templates.TemplateResponse(
        request,
        "long_audio_detail.html",
        {
            "current_user": current_user,
            "project": project,
            "project_payload": _project_payload(project),
            "status_labels": STATUS_LABELS,
        },
    )


@router.post("/api/long-audio-projects")
def create_project(
    request: Request,
    name: str = Form(...),
    scriptText: str = Form(...),
    customAudio: UploadFile = File(...),
    sourceVideo: UploadFile = File(...),
    promptPrefix: str = Form("一名人物用中文说"),
    instanceType: str = Form("default"),
    alignmentProvider: str = Form(""),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    settings = get_settings()
    check_rate_limit(
        request,
        f"long-audio-create:{current_user.id}",
        settings.task_create_rate_limit_per_minute,
    )
    try:
        project = create_long_audio_project(
            db,
            current_user,
            settings,
            name=name,
            script_text=scriptText,
            audio=customAudio,
            source_video=sourceVideo,
            prompt_prefix=promptPrefix,
            instance_type=instanceType,
            alignment_provider=(
                alignmentProvider.strip()
                or settings.long_audio_alignment_provider
            ),
        )
        db.commit()
    except (LongAudioError, UploadValidationError, OSError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "projectId": project.id,
            "status": project.status,
            "detailUrl": f"/long-audio/{project.id}",
        },
        status_code=201,
    )


@router.get("/api/long-audio-projects/{project_id}")
def project_status(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _project_payload(_load_project(db, project_id, current_user))


@router.get("/api/long-audio-projects/{project_id}/audio")
def project_audio(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _load_project(db, project_id, current_user)
    path = safe_relative_path(project.audio_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="原始音频已清理")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


@router.put("/api/long-audio-projects/{project_id}/plan")
async def update_project_plan(
    project_id: str,
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    project = _load_project(db, project_id, current_user)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="分段请求格式错误")
    try:
        plans = save_reviewed_plan(
            project,
            body.get("segments"),
            get_settings(),
        )
        db.commit()
    except LongAudioError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": True, "segmentCount": len(plans)}


@router.post("/api/long-audio-projects/{project_id}/confirm")
def confirm_project(
    project_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    project = _load_project(db, project_id, current_user)
    if project.batch_id:
        return {
            "confirmed": False,
            "batchId": project.batch_id,
            "status": project.status,
        }
    try:
        confirm_long_audio_project(project)
        db.commit()
    except LongAudioError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"confirmed": True, "status": project.status}


@router.post("/long-audio/{project_id}/retry")
def retry_project(
    project_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    project = _load_project(db, project_id, current_user)
    if project.status != LongAudioProjectStatus.FAILED.value:
        raise HTTPException(status_code=409, detail="只有失败项目可以重试")
    project.status = (
        LongAudioProjectStatus.PENDING_CUT.value
        if project.confirmed_at and project.plan_json
        else LongAudioProjectStatus.PENDING_ANALYSIS.value
    )
    project.error_code = None
    project.error_message = None
    db.commit()
    return RedirectResponse(f"/long-audio/{project.id}", status_code=303)


@router.post("/long-audio/{project_id}/reanalyze")
def reanalyze_project(
    project_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    project = _load_project(db, project_id, current_user)
    if project.batch_id or project.status not in {
        LongAudioProjectStatus.REVIEW.value,
        LongAudioProjectStatus.FAILED.value,
    }:
        raise HTTPException(status_code=409, detail="当前状态不能重新分析")
    project.alignment_provider = (
        get_settings().long_audio_alignment_provider
    )
    project.plan_json = None
    project.status = LongAudioProjectStatus.PENDING_ANALYSIS.value
    project.confirmed_at = None
    project.error_code = None
    project.error_message = None
    db.commit()
    return RedirectResponse(f"/long-audio/{project.id}", status_code=303)


@router.post("/long-audio/{project_id}/delete")
def delete_project(
    project_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    project = _load_project(db, project_id, current_user)
    if project.status in {
        LongAudioProjectStatus.ANALYZING.value,
        LongAudioProjectStatus.CUTTING.value,
        LongAudioProjectStatus.PENDING_CUT.value,
    }:
        raise HTTPException(status_code=409, detail="项目正在处理，暂时不能删除")
    directory = long_audio_project_dir(
        get_settings(), project.user_id, project.id
    )
    db.delete(project)
    db.commit()
    remove_directory(directory)
    return RedirectResponse("/long-audio", status_code=303)
