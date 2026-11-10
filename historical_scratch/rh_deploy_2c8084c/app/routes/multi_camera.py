from __future__ import annotations

import json
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import get_settings
from app.database import get_db
from app.models import (
    BATCH_SOURCE_MULTI_CAMERA_WEB,
    GenerationBatch,
    GenerationSegment,
    GenerationTask,
    TaskStatus,
    User,
)
from app.routes.dependencies import check_rate_limit, get_current_user, get_page_user
from app.services.batch_assets import StagedAssetError, stage_asset
from app.services.batch_status import (
    STATUS_LABELS,
    batch_query,
    generation_task_display_status,
    summarize_batch,
)
from app.services.csrf import require_csrf
from app.services.multi_camera import (
    MultiCameraError,
    build_plan,
    create_multi_camera_batch,
    plan_payload,
)
from app.services.multi_camera_access import (
    MultiCameraAccessError,
    ensure_multi_camera_access,
)
from app.services.storage import UploadValidationError, safe_relative_path
from app.services.task_management import (
    RETRYABLE_TASK_STATUSES,
    TaskManagementError,
    prepare_task_retry,
)
from app.services.workflow_configs import get_user_workflow_config
from app.web import templates


router = APIRouter(tags=["multi-camera"])


def _require_access(db: Session, user: User) -> None:
    try:
        ensure_multi_camera_access(db, user)
    except MultiCameraAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _load_batch(db: Session, user: User, batch_id: str) -> GenerationBatch:
    batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
    if (
        batch is None
        or batch.user_id != user.id
        or batch.source_channel != BATCH_SOURCE_MULTI_CAMERA_WEB
    ):
        raise HTTPException(status_code=404, detail="多机位批次不存在")
    return batch


def _segment_task(
    db: Session, user: User, segment_id: str
) -> tuple[GenerationSegment, GenerationTask]:
    segment = db.get(GenerationSegment, segment_id)
    if (
        segment is None
        or segment.batch_item.batch.user_id != user.id
        or segment.batch_item.batch.source_channel != BATCH_SOURCE_MULTI_CAMERA_WEB
        or segment.generation_task is None
    ):
        raise HTTPException(status_code=404, detail="多机位分段不存在")
    return segment, segment.generation_task


def _safe_name(value: str, fallback: str) -> str:
    result = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return result[:80] or fallback


def _batch_payload(batch: GenerationBatch) -> dict:
    summary = summarize_batch(batch)
    rows = []
    for item in batch.items:
        row_binding = item.multi_camera_binding
        segments = []
        for segment in item.segments:
            task = segment.generation_task
            camera_binding = segment.multi_camera_binding
            status = generation_task_display_status(task) if task else segment.status
            segments.append(
                {
                    "id": segment.id,
                    "index": segment.segment_index,
                    "startSeconds": segment.start_seconds,
                    "endSeconds": segment.end_seconds,
                    "durationSeconds": round(
                        segment.end_seconds - segment.start_seconds, 3
                    ),
                    "camera": (
                        camera_binding.camera_position if camera_binding else None
                    ),
                    "imageName": (
                        camera_binding.image_asset.original_name
                        if camera_binding
                        else None
                    ),
                    "imageSha256": (
                        camera_binding.image_sha256 if camera_binding else None
                    ),
                    "cutMethod": segment.alignment_method,
                    "status": status,
                    "statusLabel": STATUS_LABELS.get(status, status),
                    "taskId": task.id if task else None,
                    "runninghubTaskId": task.runninghub_task_id if task else None,
                    "errorMessage": (
                        task.error_message if task else segment.error_message
                    ),
                    "canRetry": bool(task and task.status in RETRYABLE_TASK_STATUSES),
                    "videoReady": bool(
                        task
                        and task.status == TaskStatus.SUCCESS.value
                        and task.result_path
                    ),
                }
            )
        rows.append(
            {
                "id": item.id,
                "rowNumber": item.row_number,
                "rowKey": item.row_key,
                "audioName": (
                    row_binding.audio_original_name if row_binding else item.row_key
                ),
                "audioSha256": (row_binding.audio_sha256 if row_binding else None),
                "imageGroupId": (row_binding.image_group_id if row_binding else None),
                "groupName": (row_binding.image_group.name if row_binding else ""),
                "durationSeconds": (
                    round(row_binding.duration_seconds, 3) if row_binding else None
                ),
                "segments": segments,
            }
        )
    return {
        "schema": "runninghub.multi-camera-batch.v1",
        "batch": summary,
        "configuration": (
            {
                "segmentationPolicy": batch.multi_camera_config.segmentation_policy,
                "orderingPolicy": batch.multi_camera_config.ordering_policy,
                "resolution": batch.multi_camera_config.resolution,
                "instanceType": batch.multi_camera_config.instance_type,
                "seedvr2Enabled": batch.multi_camera_config.seedvr2_enabled,
            }
            if batch.multi_camera_config
            else None
        ),
        "rows": rows,
        "downloadReady": any(
            segment["videoReady"] for row in rows for segment in row["segments"]
        ),
    }


@router.get("/generate/multi-camera")
def multi_camera_page(
    request: Request,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    _require_access(db, current_user)
    config = get_user_workflow_config(current_user, "digital_human")
    recent = (
        db.scalars(
            batch_query()
            .where(
                GenerationBatch.user_id == current_user.id,
                GenerationBatch.source_channel == BATCH_SOURCE_MULTI_CAMERA_WEB,
            )
            .order_by(GenerationBatch.created_at.desc())
            .limit(20)
        )
        .unique()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "multi_camera.html",
        {
            "current_user": current_user,
            "default_prompt": config.default_prompt,
            "recent_batches": [summarize_batch(batch) for batch in recent],
        },
    )


@router.post("/api/multi-camera/assets")
def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    _require_access(db, current_user)
    if kind not in {"image", "audio"}:
        raise HTTPException(status_code=400, detail="只接受图片或音频素材")
    settings = get_settings()
    check_rate_limit(
        request,
        f"multi-camera-asset:{current_user.id}",
        max(settings.task_create_rate_limit_per_minute * 30, 120),
    )
    try:
        asset = stage_asset(db, current_user, file, kind, settings)
    except (UploadValidationError, StagedAssetError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "assetId": asset.id,
            "kind": asset.kind,
            "originalName": asset.original_name,
            "sizeBytes": asset.size_bytes,
        },
        status_code=201,
    )


@router.post("/api/multi-camera/preflight")
async def preflight(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    _require_access(db, current_user)
    try:
        payload = await request.json()
        plan = build_plan(db, current_user, payload, get_settings())
        return plan_payload(plan)
    except (MultiCameraError, TaskManagementError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/multi-camera/batches")
async def create_batch(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    _require_access(db, current_user)
    settings = get_settings()
    check_rate_limit(
        request,
        f"multi-camera-create:{current_user.id}",
        settings.task_create_rate_limit_per_minute,
    )
    try:
        payload = await request.json()
        batch, created = create_multi_camera_batch(db, current_user, payload, settings)
        return JSONResponse(
            {
                "batchId": batch.id,
                "created": created,
                "statusUrl": f"/api/multi-camera/batches/{batch.id}",
            },
            status_code=201 if created else 200,
        )
    except (MultiCameraError, TaskManagementError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/multi-camera/batches/{batch_id}")
def batch_status(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_access(db, current_user)
    return _batch_payload(_load_batch(db, current_user, batch_id))


@router.post("/api/multi-camera/segments/{segment_id}/retry")
def retry_segment(
    segment_id: str,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    _require_access(db, current_user)
    _, task = _segment_task(db, current_user, segment_id)
    try:
        prepare_task_retry(task, get_settings())
        db.commit()
    except TaskManagementError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"segmentId": segment_id, "status": task.status}


@router.get("/api/multi-camera/segments/{segment_id}/video")
def download_segment(
    segment_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_access(db, current_user)
    segment, task = _segment_task(db, current_user, segment_id)
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise HTTPException(status_code=409, detail="当前分段视频尚未生成完成")
    try:
        path = safe_relative_path(task.result_path, get_settings().data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="视频文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    filename = f"segment-{segment.segment_index:03d}{path.suffix.lower()}"
    return FileResponse(path, filename=filename)


@router.get("/api/multi-camera/batches/{batch_id}/download")
def download_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_access(db, current_user)
    batch = _load_batch(db, current_user, batch_id)
    settings = get_settings()
    manifest = _batch_payload(batch)
    files: list[tuple[Path, str]] = []
    for item in batch.items:
        row_name = _safe_name(item.row_key, f"row-{item.row_number:03d}")
        for segment in item.segments:
            task = segment.generation_task
            if (
                not task
                or task.status != TaskStatus.SUCCESS.value
                or not task.result_path
            ):
                continue
            try:
                source = safe_relative_path(task.result_path, settings.data_dir)
            except ValueError:
                continue
            if not source.is_file():
                continue
            files.append(
                (
                    source,
                    (
                        f"{item.row_number:03d}-{row_name}/"
                        f"{segment.segment_index:03d}-camera-"
                        f"{segment.multi_camera_binding.camera_position}"
                        f"{source.suffix.lower()}"
                    ),
                )
            )
    if not files:
        raise HTTPException(status_code=409, detail="当前没有可下载的成功片段")
    directory = settings.runtime_dir / "multi-camera-downloads"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        for source, archive_name in files:
            output.write(source, archive_name)
    filename = f"{_safe_name(batch.name, 'multi-camera')}-{batch.id[:8]}.zip"
    return FileResponse(
        archive,
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )
