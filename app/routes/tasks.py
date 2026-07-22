from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import ACTIVE_TASK_STATUSES, GenerationTask, TaskStatus, User
from app.routes.dependencies import (
    check_rate_limit,
    ensure_task_access,
    get_current_user,
    get_page_user,
)
from app.services.audio import (
    AudioInspectionError,
    format_timecode,
    inspect_audio_duration,
    validate_time_range,
)
from app.services.storage import (
    UploadValidationError,
    remove_directory,
    safe_relative_path,
    save_upload,
    task_upload_dir,
    to_relative_data_path,
)
from app.web import templates
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow, list_workflows
from app.workflows.base import WorkflowAsset


router = APIRouter(tags=["tasks"])

DIGITAL_HUMAN_WORKFLOW = "digital_human"

STATUS_LABELS = {
    TaskStatus.PENDING.value: "等待处理",
    TaskStatus.UPLOADING.value: "正在上传素材",
    TaskStatus.SUBMITTED.value: "已经提交到 RunningHub",
    TaskStatus.RUNNING.value: "正在生成",
    TaskStatus.SUCCESS.value: "生成成功",
    TaskStatus.FAILED.value: "生成失败",
    TaskStatus.DOWNLOAD_FAILED.value: "视频下载失败",
    TaskStatus.CANCELLED.value: "已取消",
}


def _task_query():
    return select(GenerationTask).options(selectinload(GenerationTask.user))


def _serialize_task(task: GenerationTask) -> dict:
    return {
        "taskId": task.id,
        "status": task.status,
        "statusText": STATUS_LABELS.get(task.status, task.status),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "completedAt": task.completed_at.isoformat() if task.completed_at else None,
        "errorCode": task.error_code,
        "errorMessage": task.error_message,
        "workflowType": task.workflow_type,
        "downloadUrl": (
            f"/api/tasks/{task.id}/download"
            if task.status == TaskStatus.SUCCESS.value and task.result_path
            else None
        ),
    }


@router.get("/generate")
def generate_page(
    request: Request,
    current_user: User = Depends(get_page_user),
):
    account_config = current_user.runninghub_config
    workflow_config = get_user_workflow_config(current_user, DIGITAL_HUMAN_WORKFLOW)
    return templates.TemplateResponse(
        request,
        "generate.html",
        {
            "current_user": current_user,
            "config": workflow_config,
            "has_api_key": bool(account_config and account_config.api_key_encrypted),
        },
    )


@router.get("/api/workflows")
def available_workflows(current_user: User = Depends(get_current_user)):
    """Discovery endpoint for future workflow-selection pages."""

    items = []
    for workflow in list_workflows():
        config = get_user_workflow_config(current_user, workflow.key)
        items.append(
            {
                "key": workflow.key,
                "displayName": workflow.display_name,
                "defaultPrompt": config.default_prompt,
                "enabled": config.is_enabled,
            }
        )
    return items


@router.post("/api/audio/inspect")
def inspect_audio(
    request: Request,
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    settings = get_settings()
    temporary_dir = settings.data_dir / "temporary-audio-inspection" / uuid.uuid4().hex
    try:
        saved_path, _ = save_upload(audio, temporary_dir, "audio", settings)
        duration = inspect_audio_duration(saved_path)
        end_text = format_timecode(duration)
        if duration < 1:
            raise AudioInspectionError("音频时长不足 1 秒，无法生成视频")
        return {
            "durationSeconds": round(duration, 3),
            "durationText": end_text,
            "suggestedStartTime": "0:00",
            "suggestedEndTime": end_text,
        }
    except (UploadValidationError, AudioInspectionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        remove_directory(temporary_dir)


@router.post("/api/tasks")
def create_task(
    request: Request,
    image: UploadFile = File(...),
    audio: UploadFile = File(...),
    startTime: str = Form(...),
    endTime: str = Form(...),
    prompt: str = Form(...),
    resolution: str = Form("1024"),
    overallMode: str = Form("2"),
    personMode: str = Form("1"),
    leftAudio: UploadFile | None = File(None),
    rightAudio: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(request, f"task-create:{current_user.id}", settings.task_create_rate_limit_per_minute)
    account_config = current_user.runninghub_config
    workflow_config = get_user_workflow_config(current_user, DIGITAL_HUMAN_WORKFLOW)
    workflow = get_workflow(DIGITAL_HUMAN_WORKFLOW)
    if not account_config or not account_config.api_key_encrypted:
        raise HTTPException(status_code=400, detail="当前账号尚未配置 RunningHub API Key")

    if not workflow_config.is_enabled:
        raise HTTPException(status_code=400, detail="当前账号尚未启用数字人工作流")

    active_count = db.scalar(
        select(func.count())
        .select_from(GenerationTask)
        .where(
            GenerationTask.user_id == current_user.id,
            GenerationTask.status.in_(ACTIVE_TASK_STATUSES),
        )
    )
    if active_count and active_count >= account_config.max_concurrent_tasks:
        raise HTTPException(status_code=429, detail="当前正在生成的任务数已达到账号上限")

    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 5000:
        raise HTTPException(status_code=400, detail="提示词长度必须在 1 到 5000 个字符之间")

    task_id = str(uuid.uuid4())
    upload_dir = task_upload_dir(settings, current_user.id, task_id)
    try:
        image_path, image_original_name = save_upload(image, upload_dir, "image", settings)
        audio_path, audio_original_name = save_upload(audio, upload_dir, "audio", settings)
        duration = inspect_audio_duration(audio_path)
        parameters = workflow.validate_parameters(
            {
                "prompt": prompt,
                "start_time": startTime,
                "end_time": endTime,
                "resolution": resolution,
                "overall_mode": overallMode,
                "person_mode": personMode,
            },
            {"audio_duration_seconds": duration},
        )
        assets = [
            WorkflowAsset(
                name="image",
                kind="image",
                relative_path=to_relative_data_path(image_path, settings),
                original_name=image_original_name,
            ),
            WorkflowAsset(
                name="audio",
                kind="audio",
                relative_path=to_relative_data_path(audio_path, settings),
                original_name=audio_original_name,
            ),
        ]
        if parameters["person_mode"] == "0":
            if not leftAudio or not leftAudio.filename or not rightAudio or not rightAudio.filename:
                raise ValueError("双人模式必须上传左边人物音频和右边人物音频")
            for name, upload in (("left_audio", leftAudio), ("right_audio", rightAudio)):
                path, original_name = save_upload(upload, upload_dir, "audio", settings)
                assets.append(
                    WorkflowAsset(
                        name=name,
                        kind="audio",
                        relative_path=to_relative_data_path(path, settings),
                        original_name=original_name,
                    )
                )
        input_payload = workflow.serialize_input(
            assets,
            parameters,
            {"audio_duration_seconds": duration},
        )
        task = GenerationTask(
            id=task_id,
            user_id=current_user.id,
            workflow_type=workflow.key,
            input_payload=json.dumps(input_payload, ensure_ascii=False),
            image_path=assets[0].relative_path,
            audio_path=assets[1].relative_path,
            image_original_name=image_original_name,
            audio_original_name=audio_original_name,
            audio_duration_seconds=duration,
            start_seconds=float(parameters["start_seconds"]),
            end_seconds=float(parameters["end_seconds"]),
            prompt=str(parameters["prompt"]),
            status=TaskStatus.PENDING.value,
        )
        db.add(task)
        db.commit()
    except (UploadValidationError, AudioInspectionError, ValueError) as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=500, detail="服务器保存上传文件失败") from exc

    return JSONResponse({"taskId": task_id, "status": TaskStatus.PENDING.value}, status_code=201)


@router.get("/api/tasks/{task_id}")
def task_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.scalar(_task_query().where(GenerationTask.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    return _serialize_task(task)


@router.get("/api/tasks/{task_id}/image")
def task_image(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    try:
        path = safe_relative_path(task.image_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path)


@router.get("/api/tasks/{task_id}/download")
def download_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise HTTPException(status_code=404, detail="生成视频尚不可下载")
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"digital-human-{task.id}.mp4",
        content_disposition_type="attachment",
    )


@router.get("/api/tasks/{task_id}/preview")
def preview_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise HTTPException(status_code=404, detail="生成视频尚不可预览")
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(path, media_type="video/mp4")


@router.get("/tasks")
def tasks_page(
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    statement = _task_query().order_by(GenerationTask.created_at.desc())
    if not current_user.is_admin:
        statement = statement.where(GenerationTask.user_id == current_user.id)
    tasks = db.scalars(statement).all()
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "tasks": tasks,
            "current_user": current_user,
            "status_labels": STATUS_LABELS,
            "now": datetime.now(timezone.utc),
        },
    )


@router.get("/tasks/{task_id}")
def task_detail_page(
    task_id: str,
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    task = db.scalar(_task_query().where(GenerationTask.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "current_user": current_user,
            "status_labels": STATUS_LABELS,
        },
    )
