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
    DIGITAL_HUMAN_WORKFLOW,
    LTX_WORKFLOW,
    LongAudioError,
    confirm_long_audio_project,
    create_long_audio_project,
    save_reviewed_plan,
    sync_linked_batch_item,
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
    LongAudioProjectStatus.PENDING_ANALYSIS.value: "等待处理",
    LongAudioProjectStatus.ANALYZING.value: "处理中",
    LongAudioProjectStatus.REVIEW.value: "等待试听确认",
    LongAudioProjectStatus.PENDING_CUT.value: "等待创建任务",
    LongAudioProjectStatus.CUTTING.value: "正在创建任务",
    LongAudioProjectStatus.COMPLETED.value: "已创建视频任务",
    LongAudioProjectStatus.FAILED.value: "处理失败",
    LongAudioProjectStatus.CANCELLED.value: "已取消",
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
    try:
        remote_metrics = json.loads(project.remote_metrics_json or "null")
    except json.JSONDecodeError:
        remote_metrics = None
    return {
        "projectId": project.id,
        "name": project.name,
        "status": project.status,
        "statusLabel": STATUS_LABELS.get(project.status, project.status),
        "durationSeconds": project.duration_seconds,
        "alignmentProvider": project.alignment_provider,
        "workflowType": project.workflow_type,
        "reviewRequired": project.review_required,
        "segments": _plan_payload(project),
        "errorCode": project.error_code,
        "errorMessage": project.error_message,
        "batchId": (
            project.batch_item.batch_id
            if project.batch_item is not None
            else project.batch_id
        ),
        "remoteWorkerId": project.remote_worker_id,
        "remoteLeaseExpiresAt": (
            project.remote_lease_expires_at.isoformat()
            if project.remote_lease_expires_at
            else None
        ),
        "remoteMetrics": remote_metrics,
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
    workflowType: str = Form(LTX_WORKFLOW),
    scriptText: str = Form(""),
    customAudio: UploadFile = File(...),
    sourceVideo: UploadFile | None = File(None),
    sourceImage: UploadFile | None = File(None),
    promptPrefix: str = Form("一名人物用中文说"),
    digitalPrompt: str = Form(
        "人物自然地说话，他的身体和手部随着说话的节奏做着自然且随意的动作，"
        "镜头保持稳定。"
    ),
    instanceType: str = Form("plus"),
    reviewRequired: bool = Form(False),
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
        primary = (
            sourceVideo
            if workflowType == LTX_WORKFLOW
            else sourceImage
        )
        if primary is None or not primary.filename:
            raise LongAudioError(
                "对口型工作流必须上传源视频"
                if workflowType == LTX_WORKFLOW
                else "数字人工作流必须上传源图片"
            )
        project = create_long_audio_project(
            db,
            current_user,
            settings,
            name=name,
            script_text=scriptText,
            audio=customAudio,
            source_video=primary,
            prompt_prefix=promptPrefix,
            instance_type=instanceType,
            alignment_provider=(
                alignmentProvider.strip()
                or settings.long_audio_alignment_provider
            ),
            workflow_type=workflowType,
            review_required=reviewRequired,
            digital_prompt=digitalPrompt,
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
    sync_linked_batch_item(project)
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
        if project.workflow_type == LTX_WORKFLOW
        else "vad_silence"
    )
    project.plan_json = None
    project.status = LongAudioProjectStatus.PENDING_ANALYSIS.value
    project.confirmed_at = None
    project.error_code = None
    project.error_message = None
    sync_linked_batch_item(project)
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
