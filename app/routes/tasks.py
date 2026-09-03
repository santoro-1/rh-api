from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import GenerationTask, TaskStatus, User
from app.routes.dependencies import (
    check_rate_limit,
    ensure_task_access,
    get_current_user,
    get_page_user,
)
from app.services.audio import (
    AudioInspectionError,
    format_duration_timecode,
    inspect_audio_duration,
    validate_time_range,
)
from app.services.media_segmentation import (
    DIGITAL_HUMAN_GENERATION_TAIL_SECONDS,
    DIGITAL_HUMAN_MAX_SEGMENT_SECONDS,
    MAX_SEGMENT_SECONDS,
    inspect_media_duration,
)
from app.services.csrf import require_csrf
from app.services.storage import (
    UploadValidationError,
    remove_directory,
    safe_relative_path,
    save_upload,
    task_upload_dir,
    task_output_dir,
    to_relative_data_path,
)
from app.services.task_creation import (
    TaskCreationError,
    create_generation_task,
    ensure_user_can_create_workflow,
    validate_task_input,
)
from app.services.task_management import (
    RETRYABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskManagementError,
    prepare_task_retry,
)
from app.services.runninghub import RunningHubError
from app.services.video_enhancement import (
    task_processing_stage,
    task_quality_variant,
    task_status_text,
)
from app.services.task_cancellation import (
    TaskCancellationError,
    cancel_generation_task,
)
from app.web import templates
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow, list_workflows
from app.workflows.base import WorkflowAsset


router = APIRouter(tags=["tasks"])

DIGITAL_HUMAN_WORKFLOW = "digital_human"
LTX_LIP_SYNC_WORKFLOW = "ltx_lip_sync"

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

ACTIVE_TASK_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
}

BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def _task_query():
    return select(GenerationTask).options(
        selectinload(GenerationTask.user),
        selectinload(GenerationTask.enhancement),
    )


def _beijing_date_boundary(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=BEIJING_TIMEZONE).astimezone(
        timezone.utc
    )


def _tasks_redirect(
    start_date: str,
    end_date: str,
    *,
    page: int = 1,
    page_size: int = 20,
) -> str:
    query = {
        key: value
        for key, value in (
            ("start_date", start_date),
            ("end_date", end_date),
            ("page", page if page > 1 else ""),
            ("page_size", page_size if page_size != 20 else ""),
        )
        if value
    }
    return f"/tasks?{urlencode(query)}" if query else "/tasks"


def _task_list_status_text(task: GenerationTask) -> str:
    if task.enhancement and task.status in {
        TaskStatus.FAILED.value,
        TaskStatus.DOWNLOAD_FAILED.value,
    }:
        return "视频清晰化失败"
    if (
        task.enhancement
        and task.enhancement.status != TaskStatus.SUCCESS.value
        and task.status != TaskStatus.CANCELLED.value
    ):
        return "视频清晰化中（48G）"
    if task.enhancement and task.enhancement.status == TaskStatus.SUCCESS.value:
        return "清晰视频已完成"
    return STATUS_LABELS.get(task.status, task.status)


def _task_thumbnail_available(task: GenerationTask) -> bool:
    if task.workflow_type != DIGITAL_HUMAN_WORKFLOW or not task.image_path:
        return False
    try:
        path = safe_relative_path(task.image_path, get_settings().data_dir)
        return path.is_file()
    except (OSError, ValueError):
        return False


def _task_download_available(task: GenerationTask) -> bool:
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        return False
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
        return path.is_file()
    except (OSError, ValueError):
        return False


def _task_failed_reason(task: GenerationTask) -> dict | None:
    if not task.runninghub_failed_reason:
        return None
    try:
        value = json.loads(task.runninghub_failed_reason)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _task_attempt_history(task: GenerationTask) -> list[dict]:
    if not task.runninghub_attempt_history:
        return []
    try:
        value = json.loads(task.runninghub_attempt_history)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _serialize_task(task: GenerationTask) -> dict:
    enhancement = task.enhancement
    fallback_status = STATUS_LABELS.get(task.status, task.status)
    auto_retry_count = (
        enhancement.auto_retry_count
        if enhancement is not None
        and enhancement.status != "SUCCESS"
        else task.runninghub_auto_retry_count
    )
    auto_retry_after = (
        enhancement.auto_retry_after
        if enhancement is not None
        and enhancement.status != "SUCCESS"
        else task.runninghub_auto_retry_after
    )
    return {
        "taskId": task.id,
        "runninghubTaskId": (
            enhancement.remote_task_id
            if enhancement is not None and enhancement.remote_task_id
            else task.runninghub_task_id
        ),
        "digitalHumanTaskId": task.runninghub_task_id,
        "enhancementTaskId": (
            enhancement.remote_task_id if enhancement is not None else None
        ),
        "status": task.status,
        "statusText": task_status_text(task, fallback_status),
        "processingStage": task_processing_stage(task),
        "enhancementStatus": (
            enhancement.status if enhancement is not None else None
        ),
        "qualityVariant": task_quality_variant(task),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "completedAt": task.completed_at.isoformat() if task.completed_at else None,
        "errorCode": task.error_code,
        "errorMessage": task.error_message,
        "failedReason": _task_failed_reason(task),
        "attemptHistory": _task_attempt_history(task),
        "autoRetryCount": auto_retry_count,
        "autoRetryLimit": get_settings().runninghub_auto_retry_limit,
        "autoRetryAfter": (
            auto_retry_after.isoformat()
            if auto_retry_after
            else None
        ),
        "workflowType": task.workflow_type,
        "seedvr2Enabled": task.seedvr2_enabled,
        "sourceDownloadUrl": (
            f"/api/tasks/{task.id}/source-video"
            if task.workflow_type == DIGITAL_HUMAN_WORKFLOW and enhancement is not None
            else None
        ),
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
    digital_config = get_user_workflow_config(current_user, DIGITAL_HUMAN_WORKFLOW)
    ltx_config = get_user_workflow_config(current_user, LTX_LIP_SYNC_WORKFLOW)
    has_api_key = bool(account_config and account_config.api_key_encrypted)
    requested_workflow = request.query_params.get("workflow", DIGITAL_HUMAN_WORKFLOW)
    if requested_workflow not in {DIGITAL_HUMAN_WORKFLOW, LTX_LIP_SYNC_WORKFLOW}:
        requested_workflow = DIGITAL_HUMAN_WORKFLOW
    return templates.TemplateResponse(
        request,
        "generate.html",
        {
            "current_user": current_user,
            "config": digital_config,
            "ltx_config": ltx_config,
            "has_api_key": has_api_key,
            "can_generate_ltx": bool(
                has_api_key and ltx_config.is_enabled and ltx_config.ai_app_id
            ),
            "initial_workflow": requested_workflow,
        },
    )


@router.get("/generate/ltx-lip-sync")
def ltx_lip_sync_generate_page(
    request: Request,
    current_user: User = Depends(get_page_user),
):
    del request, current_user
    return RedirectResponse("/generate?workflow=ltx_lip_sync", status_code=303)


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
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
):
    settings = get_settings()
    temporary_dir = settings.data_dir / "temporary-audio-inspection" / uuid.uuid4().hex
    try:
        saved_path, _ = save_upload(audio, temporary_dir, "audio", settings)
        duration = inspect_audio_duration(saved_path)
        end_text = format_duration_timecode(duration)
        if duration < 1:
            raise AudioInspectionError("音频时长不足 1 秒，无法生成视频")
        if duration > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01:
            raise AudioInspectionError(
                "数字人口播不能超过 32.8 秒；系统还会追加 2 秒静音用于自然收尾，"
                "请先拆分音频，或使用脚本完整流程自动切分"
            )
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
    personMode: str = Form("1"),
    instanceType: str = Form("plus"),
    seedvr2Enabled: bool = Form(False),
    leftAudio: UploadFile | None = File(None),
    rightAudio: UploadFile | None = File(None),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(request, f"task-create:{current_user.id}", settings.task_create_rate_limit_per_minute)
    try:
        ensure_user_can_create_workflow(current_user, DIGITAL_HUMAN_WORKFLOW)
        digital_config = get_user_workflow_config(
            current_user, DIGITAL_HUMAN_WORKFLOW
        )
        if str(personMode).strip() != "1":
            raise TaskCreationError("双人数字人模式暂未开放")
    except TaskCreationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = str(uuid.uuid4())
    upload_dir = task_upload_dir(settings, current_user.id, task_id)
    try:
        image_path, image_original_name = save_upload(image, upload_dir, "image", settings)
        audio_path, audio_original_name = save_upload(audio, upload_dir, "audio", settings)
        duration = inspect_audio_duration(audio_path)
        if duration > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01:
            raise ValueError(
                "数字人口播不能超过 32.8 秒；系统还会追加 2 秒静音用于自然收尾，"
                "请先拆分音频，或使用脚本完整流程自动切分"
            )
        selected_start, selected_end = validate_time_range(
            startTime,
            endTime,
            duration,
        )
        # Public timecodes use whole seconds. The final value may equal the
        # ceiling of a fractional file duration (32.4 -> 33), so validate the
        # actual playable interval instead of treating encoder padding as speech.
        selected_duration = min(selected_end, duration) - selected_start
        if selected_duration > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01:
            raise ValueError("单次数字人口播区间不能超过 32.8 秒")
        parameters = {
            "prompt": prompt,
            "start_time": startTime,
            "end_time": endTime,
            "resolution": resolution,
            "person_mode": personMode,
            # Digital-human compute is an administrator-owned user setting.
            "instance_type": digital_config.instance_type,
            "seedvr2_enabled": seedvr2Enabled,
            "generation_tail_seconds": DIGITAL_HUMAN_GENERATION_TAIL_SECONDS,
        }
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
        if str(personMode).strip() == "0":
            if not leftAudio or not leftAudio.filename or not rightAudio or not rightAudio.filename:
                raise ValueError("双人模式必须上传左边人物音频和右边人物音频")
            for name, upload in (("left_audio", leftAudio), ("right_audio", rightAudio)):
                path, original_name = save_upload(upload, upload_dir, "audio", settings)
                auxiliary_duration = inspect_audio_duration(path)
                if (
                    auxiliary_duration
                    > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01
                ):
                    label = "左人物音频" if name == "left_audio" else "右人物音频"
                    raise ValueError(f"{label}不能超过 32.8 秒，请先拆分音频")
                assets.append(
                    WorkflowAsset(
                        name=name,
                        kind="audio",
                        relative_path=to_relative_data_path(path, settings),
                        original_name=original_name,
                    )
                )
        validated = validate_task_input(
            current_user,
            DIGITAL_HUMAN_WORKFLOW,
            assets,
            parameters,
            {"audio_duration_seconds": duration},
        )
        create_generation_task(
            db,
            current_user,
            validated,
            task_id=task_id,
        )
        db.commit()
    except (
        UploadValidationError,
        AudioInspectionError,
        TaskCreationError,
        ValueError,
    ) as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=500, detail="服务器保存上传文件失败") from exc

    return JSONResponse({"taskId": task_id, "status": TaskStatus.PENDING.value}, status_code=201)


@router.post("/api/tasks/ltx-lip-sync")
def create_ltx_lip_sync_task(
    request: Request,
    sourceVideo: UploadFile = File(...),
    customAudio: UploadFile | None = File(None),
    prompt: str = Form(...),
    instanceType: str = Form("plus"),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(
        request,
        f"task-create:{current_user.id}",
        settings.task_create_rate_limit_per_minute,
    )
    try:
        ensure_user_can_create_workflow(current_user, LTX_LIP_SYNC_WORKFLOW)
    except TaskCreationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = str(uuid.uuid4())
    upload_dir = task_upload_dir(settings, current_user.id, task_id)
    try:
        video_path, video_original_name = save_upload(
            sourceVideo, upload_dir, "video", settings
        )
        assets = [
            WorkflowAsset(
                name="video",
                kind="video",
                relative_path=to_relative_data_path(video_path, settings),
                original_name=video_original_name,
            )
        ]
        if not customAudio or not customAudio.filename:
            raise ValueError("必须上传自定义音频")
        audio_path, audio_original_name = save_upload(customAudio, upload_dir, "audio", settings)
        duration = inspect_audio_duration(audio_path)
        if duration > MAX_SEGMENT_SECONDS + 0.01:
            raise ValueError(
                f"音频不能超过 {MAX_SEGMENT_SECONDS:g} 秒；"
                "请先拆分音频，或使用脚本完整流程自动切分"
            )
        video_duration = inspect_media_duration(video_path)
        if video_duration + 0.05 < duration:
            raise ValueError(
                f"源视频时长不足：视频 {video_duration:.1f} 秒，"
                f"音频 {duration:.1f} 秒"
            )
        assets.append(
            WorkflowAsset(
                name="audio",
                kind="audio",
                relative_path=to_relative_data_path(audio_path, settings),
                original_name=audio_original_name,
            )
        )
        parameters = {"prompt": prompt, "instance_type": instanceType}
        validated = validate_task_input(
            current_user,
            LTX_LIP_SYNC_WORKFLOW,
            assets,
            parameters,
            {
                "has_custom_audio": True,
                "audio_duration_seconds": duration,
            },
        )
        create_generation_task(
            db,
            current_user,
            validated,
            task_id=task_id,
        )
        db.commit()
    except (UploadValidationError, TaskCreationError, ValueError) as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        db.rollback()
        remove_directory(upload_dir)
        raise HTTPException(status_code=500, detail="服务器保存上传文件失败") from exc

    return JSONResponse(
        {"taskId": task_id, "status": TaskStatus.PENDING.value}, status_code=201
    )


@router.get("/api/tasks/statuses")
def task_statuses(
    ids: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task_ids = list(
        dict.fromkeys(value.strip() for value in ids.split(",") if value.strip())
    )
    if len(task_ids) > 50:
        raise HTTPException(status_code=400, detail="一次最多查询 50 个任务")
    if not task_ids:
        return JSONResponse({"tasks": []}, headers={"Cache-Control": "no-store"})

    statement = _task_query().where(GenerationTask.id.in_(task_ids))
    if not current_user.is_admin:
        statement = statement.where(GenerationTask.user_id == current_user.id)
    visible_tasks = {task.id: task for task in db.scalars(statement).all()}
    payload = []
    for task_id in task_ids:
        task = visible_tasks.get(task_id)
        if task is None:
            continue
        download_available = _task_download_available(task)
        payload.append(
            {
                "taskId": task.id,
                "status": task.status,
                "statusLabel": _task_list_status_text(task),
                "errorMessage": task.error_message,
                "downloadAvailable": download_available,
                "downloadUrl": (
                    f"/api/tasks/{task.id}/download"
                    if download_available else None
                ),
                "updatedAt": task.updated_at.isoformat(),
            }
        )
    return JSONResponse(
        {"tasks": payload},
        headers={"Cache-Control": "no-store"},
    )


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


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    if task.status not in RETRYABLE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="只有失败任务可以重试")

    try:
        prepare_task_retry(task, get_settings())
    except TaskManagementError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    task = db.scalar(_task_query().where(GenerationTask.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    try:
        cancel_generation_task(db, task)
        db.commit()
    except (TaskCancellationError, RunningHubError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@router.post("/tasks/{task_id}/delete")
def delete_task(
    task_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    if task.status not in TERMINAL_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="任务处理期间不能删除记录")

    settings = get_settings()
    upload_dir = task_upload_dir(settings, task.user_id, task.id)
    output_dir = task_output_dir(settings, task.user_id, task.id)
    db.delete(task)
    db.commit()
    try:
        remove_directory(upload_dir)
        remove_directory(output_dir)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="任务记录已删除，但本地文件清理失败",
        ) from exc
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/bulk-delete")
def bulk_delete_tasks(
    task_ids: list[str] | None = Form(None),
    start_date: str = Form(""),
    end_date: str = Form(""),
    page: int = Form(1),
    page_size: int = Form(20),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_ids = list(dict.fromkeys(task_ids or []))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="请至少选择一个任务")

    tasks: list[GenerationTask] = []
    for task_id in selected_ids:
        task = db.get(GenerationTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="所选任务不存在")
        ensure_task_access(task, current_user)
        tasks.append(task)

    if any(task.status not in TERMINAL_TASK_STATUSES for task in tasks):
        raise HTTPException(
            status_code=409,
            detail="所选任务包含正在排队或处理的任务，请取消选择后再删除",
        )

    settings = get_settings()
    directories = [
        (
            task_upload_dir(settings, task.user_id, task.id),
            task_output_dir(settings, task.user_id, task.id),
        )
        for task in tasks
    ]
    for task in tasks:
        db.delete(task)
    db.commit()

    try:
        for upload_dir, output_dir in directories:
            remove_directory(upload_dir)
            remove_directory(output_dir)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="任务记录已删除，但部分本地文件清理失败",
        ) from exc

    return RedirectResponse(
        _tasks_redirect(
            start_date,
            end_date,
            page=max(1, page),
            page_size=page_size if page_size in {20, 50} else 20,
        ),
        status_code=303,
    )


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
    if task.workflow_type != DIGITAL_HUMAN_WORKFLOW:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        path = safe_relative_path(task.image_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/api/tasks/{task_id}/source-video")
def task_source_video(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(GenerationTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    ensure_task_access(task, current_user)
    if task.workflow_type == DIGITAL_HUMAN_WORKFLOW:
        if task.enhancement is None:
            raise HTTPException(status_code=404, detail="数字人源片段不存在")
        relative_path = task.enhancement.source_result_path
    elif task.workflow_type == LTX_LIP_SYNC_WORKFLOW:
        relative_path = task.image_path
    else:
        raise HTTPException(status_code=404, detail="源视频不存在")
    try:
        path = safe_relative_path(relative_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="源视频不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="源视频不存在")
    if task.workflow_type == DIGITAL_HUMAN_WORKFLOW:
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"source-{task.id}{path.suffix}",
            content_disposition_type="attachment",
        )
    return FileResponse(path, media_type="video/mp4")


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
        filename=f"{task.workflow_type}-{task.id}{path.suffix}",
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
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须大于等于 1")
    if page_size not in {20, 50}:
        raise HTTPException(status_code=400, detail="每页数量只能是 20 或 50")

    # Full-flow video calls are shown under their batch parent instead of
    # flooding the flat single-task history with every cut segment.
    conditions = [GenerationTask.segment_id.is_(None)]
    if not current_user.is_admin:
        conditions.append(GenerationTask.user_id == current_user.id)
    if start_date:
        conditions.append(
            GenerationTask.created_at >= _beijing_date_boundary(start_date)
        )
    if end_date:
        conditions.append(
            GenerationTask.created_at
            < _beijing_date_boundary(end_date + timedelta(days=1))
        )
    total_tasks = db.scalar(
        select(func.count(GenerationTask.id)).where(*conditions)
    ) or 0
    statement = (
        _task_query()
        .where(*conditions)
        .order_by(GenerationTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tasks = db.scalars(statement).all()
    thumbnail_available = {
        task.id: _task_thumbnail_available(task) for task in tasks
    }
    total_pages = (total_tasks + page_size - 1) // page_size
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "tasks": tasks,
            "current_user": current_user,
            "status_labels": STATUS_LABELS,
            "now": datetime.now(timezone.utc),
            "start_date": start_date.isoformat() if start_date else "",
            "end_date": end_date.isoformat() if end_date else "",
            "has_date_filter": bool(start_date or end_date),
            "thumbnail_available": thumbnail_available,
            "page": page,
            "page_size": page_size,
            "total_tasks": total_tasks,
            "total_pages": total_pages,
            "previous_page_url": (
                _tasks_redirect(
                    start_date.isoformat() if start_date else "",
                    end_date.isoformat() if end_date else "",
                    page=page - 1,
                    page_size=page_size,
                )
                if page > 1
                else None
            ),
            "next_page_url": (
                _tasks_redirect(
                    start_date.isoformat() if start_date else "",
                    end_date.isoformat() if end_date else "",
                    page=page + 1,
                    page_size=page_size,
                )
                if page * page_size < total_tasks
                else None
            ),
            "workflow_names": {
                workflow.key: workflow.display_name for workflow in list_workflows()
            },
            "has_active_tasks": any(
                task.status in ACTIVE_TASK_STATUSES for task in tasks
            ),
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
    batch_id = None
    if task.batch_item is not None:
        batch_id = task.batch_item.batch_id
    elif task.segment is not None:
        batch_id = task.segment.batch_item.batch_id
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "current_user": current_user,
            "return_url": f"/batches/{batch_id}" if batch_id else "/tasks",
            "return_label": "返回所属批次" if batch_id else "返回单次任务",
            "status_labels": STATUS_LABELS,
            "workflow_names": {
                workflow.key: workflow.display_name for workflow in list_workflows()
            },
            "failed_reason": _task_failed_reason(task),
            "attempt_history": _task_attempt_history(task),
            "auto_retry_limit": get_settings().runninghub_auto_retry_limit,
        },
    )
