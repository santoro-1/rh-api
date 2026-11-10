from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    BATCH_SOURCE_LTX_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    LongAudioProjectStatus,
    LtxPreparationStatus,
    User,
)
from app.routes.dependencies import check_rate_limit
from app.services.batch_assets import StagedAssetError
from app.services.ltx_workbench import (
    LtxWorkbenchError,
    create_ltx_workbench_batch,
    get_ltx_batch,
    get_ltx_item,
    ltx_batch_payload,
    ltx_item_payload,
    validate_ltx_workbench_rows,
)
from app.services.storage import safe_relative_path
from app.services.task_creation import TaskCreationError
from app.services.task_cancellation import (
    TaskCancellationError,
    cancel_generation_task,
)
from app.services.task_management import (
    RETRYABLE_TASK_STATUSES,
    TaskManagementError,
    prepare_task_retry,
)
from app.services.workbench_auth import decode_workbench_token, token_matches_user
from app.services.runninghub import RunningHubError


router = APIRouter(tags=["workbench-ltx"])


def _token_user(token: str, db: Session) -> User:
    payload = decode_workbench_token(token, get_settings())
    if payload is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    user = db.get(User, int(payload["user_id"]))
    if user is None or not token_matches_user(payload, user):
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return user


def _bearer_user(request: Request, db: Session) -> User:
    authorization = request.headers.get("authorization", "")
    token = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    return _token_user(token, db)


def _service_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/api/workbench/ltx-batches/validate")
def validate_ltx_batch(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    check_rate_limit(
        request,
        f"workbench-ltx-validate:{user.id}",
        max(get_settings().task_create_rate_limit_per_minute * 5, 20),
    )
    try:
        return validate_ltx_workbench_rows(
            db, user, get_settings(), payload.get("rows")
        )
    except (LtxWorkbenchError, StagedAssetError, TaskCreationError, ValueError, OSError) as exc:
        raise _service_error(exc) from exc


@router.post("/api/workbench/ltx-batches")
def create_ltx_batch(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认每个分段的 LTX 对口型和 SeedVR2 清晰化都会产生 RunningHub 费用",
        )
    check_rate_limit(
        request,
        f"workbench-ltx-create:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch, created = create_ltx_workbench_batch(
            db,
            user,
            get_settings(),
            name=str(payload.get("name") or ""),
            request_key=str(payload.get("request_key") or ""),
            correlation_id=str(payload.get("correlation_id") or "") or None,
            raw_rows=payload.get("rows"),
        )
    except (LtxWorkbenchError, StagedAssetError, TaskCreationError, ValueError, OSError) as exc:
        raise _service_error(exc) from exc
    return JSONResponse(
        ltx_batch_payload(batch), status_code=201 if created else 200
    )


@router.post("/api/workbench/ltx-batches/{batch_id}")
def get_ltx_batch_status(
    batch_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    batch = get_ltx_batch(db, user, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="LTX 批次不存在")
    return ltx_batch_payload(batch)


@router.post("/api/workbench/ltx-items/{item_id}")
def get_ltx_item_status(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    item = get_ltx_item(db, user, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="LTX 任务不存在")
    return ltx_item_payload(item)


@router.post("/api/workbench/ltx-items/{item_id}/retry")
def retry_ltx_preparation(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    item = get_ltx_item(db, user, item_id)
    if item is None or item.long_audio_project is None or item.ltx_preparation_job is None:
        raise HTTPException(status_code=404, detail="LTX 任务不存在")
    project = item.long_audio_project
    preparation = item.ltx_preparation_job
    if preparation.status != LtxPreparationStatus.FAILED.value:
        raise HTTPException(status_code=409, detail="只有准备失败的 LTX 任务可以重试")
    if item.segments:
        raise HTTPException(status_code=409, detail="视频分段已经创建，请按失败分段重试")
    if project.plan_json:
        project.status = LongAudioProjectStatus.PENDING_CUT.value
        preparation.status = LtxPreparationStatus.READY_TO_MATERIALIZE.value
        item.audio_status = "SEGMENTING"
    else:
        project.status = LongAudioProjectStatus.PENDING_ANALYSIS.value
        preparation.status = LtxPreparationStatus.ASR_PENDING.value
        item.audio_status = "ASR_PENDING"
    project.error_code = None
    project.error_message = None
    preparation.error_code = None
    preparation.error_message = None
    item.status = "PREPARING_LTX"
    item.error_code = None
    item.error_message = None
    db.commit()
    refreshed = get_ltx_item(db, user, item_id)
    assert refreshed is not None
    return ltx_item_payload(refreshed)


@router.post("/api/workbench/ltx-items/{item_id}/cancel")
def cancel_ltx_preparation(
    item_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    item = get_ltx_item(db, user, item_id)
    if item is None or item.long_audio_project is None or item.ltx_preparation_job is None:
        raise HTTPException(status_code=404, detail="LTX 任务不存在")
    project = item.long_audio_project
    preparation = item.ltx_preparation_job
    if item.segments:
        raise HTTPException(
            status_code=409,
            detail="LTX 视频分段已经创建，请在分段任务中取消或重试",
        )
    if project.status in {
        LongAudioProjectStatus.COMPLETED.value,
        LongAudioProjectStatus.CANCELLED.value,
    }:
        raise HTTPException(status_code=409, detail="当前 LTX 准备状态不能取消")
    project.status = LongAudioProjectStatus.CANCELLED.value
    project.error_code = None
    project.error_message = None
    project.remote_lease_id = None
    project.remote_lease_expires_at = None
    preparation.status = LtxPreparationStatus.CANCELLED.value
    preparation.error_code = None
    preparation.error_message = None
    item.status = "CANCELLED"
    item.audio_status = "CANCELLED"
    item.error_code = None
    item.error_message = None
    db.commit()
    refreshed = get_ltx_item(db, user, item_id)
    assert refreshed is not None
    return ltx_item_payload(refreshed)


def _item_segment(item, segment_index: int):
    return next(
        (
            segment
            for segment in item.segments
            if segment.segment_index == segment_index
        ),
        None,
    )


def _segment_for_user(
    db: Session, user: User, segment_id: str
) -> GenerationSegment | None:
    return db.scalar(
        select(GenerationSegment)
        .join(GenerationBatchItem, GenerationSegment.batch_item_id == GenerationBatchItem.id)
        .join(GenerationBatch, GenerationBatchItem.batch_id == GenerationBatch.id)
        .options(
            selectinload(GenerationSegment.generation_task),
            selectinload(GenerationSegment.batch_item),
        )
        .where(
            GenerationSegment.id == segment_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == "ltx_lip_sync",
            GenerationBatch.source_channel == BATCH_SOURCE_LTX_WORKBENCH,
        )
    )


def _retry_segment_task(
    db: Session,
    item,
    segment: GenerationSegment,
    *,
    request_key: str,
) -> dict[str, Any]:
    clean_request_key = request_key.strip()
    if not clean_request_key or len(clean_request_key) > 100:
        raise HTTPException(status_code=422, detail="request_key 长度必须为 1–100")
    task = segment.generation_task
    if task is None:
        raise HTTPException(status_code=404, detail="LTX 分段任务不存在")
    try:
        input_payload = json.loads(task.input_payload or "{}")
    except json.JSONDecodeError:
        input_payload = {}
    if not isinstance(input_payload, dict):
        input_payload = {}
    retry_metadata = input_payload.get("_ltx_manual_retry")
    if (
        isinstance(retry_metadata, dict)
        and retry_metadata.get("request_key") == clean_request_key
    ):
        return ltx_item_payload(item)
    if task.status not in RETRYABLE_TASK_STATUSES:
        raise HTTPException(status_code=409, detail="只有失败或已取消分段可以重试")
    try:
        prepare_task_retry(task, get_settings())
    except TaskManagementError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    input_payload["_ltx_manual_retry"] = {"request_key": clean_request_key}
    task.input_payload = json.dumps(input_payload, ensure_ascii=False)
    segment.status = "TASK_CREATED"
    segment.error_code = None
    segment.error_message = None
    item.status = "VIDEO_PENDING"
    item.error_code = None
    item.error_message = None
    item.merged_video_status = "MERGE_PENDING"
    item.merged_video_error = None
    db.commit()
    return ltx_item_payload(item)


@router.post(
    "/api/workbench/ltx-items/{item_id}/segments/{segment_index}/retry"
)
def retry_ltx_segment(
    item_id: str,
    segment_index: int,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认重试当前失败阶段会产生 RunningHub 费用",
        )
    item = get_ltx_item(db, user, item_id)
    segment = _item_segment(item, segment_index) if item is not None else None
    if item is None or segment is None:
        raise HTTPException(status_code=404, detail="LTX 分段任务不存在")
    return _retry_segment_task(
        db,
        item,
        segment,
        request_key=str(payload.get("request_key") or ""),
    )


@router.post("/api/workbench/ltx-segments/{segment_id}/retry")
def retry_ltx_segment_by_id(
    segment_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认重试当前失败阶段会产生 RunningHub 费用",
        )
    segment = _segment_for_user(db, user, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="LTX 分段任务不存在")
    return _retry_segment_task(
        db,
        segment.batch_item,
        segment,
        request_key=str(payload.get("request_key") or ""),
    )


@router.post(
    "/api/workbench/ltx-items/{item_id}/segments/{segment_index}/cancel"
)
def cancel_ltx_segment(
    item_id: str,
    segment_index: int,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    item = get_ltx_item(db, user, item_id)
    segment = _item_segment(item, segment_index) if item is not None else None
    task = segment.generation_task if segment is not None else None
    if task is None:
        raise HTTPException(status_code=404, detail="LTX 分段任务不存在")
    try:
        cancel_generation_task(db, task)
        db.commit()
    except (TaskCancellationError, RunningHubError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refreshed = get_ltx_item(db, user, item_id)
    assert refreshed is not None
    return ltx_item_payload(refreshed)


def _segment_file(
    item_id: str,
    segment_index: int,
    request: Request,
    db: Session,
) -> tuple[Path, str]:
    user = _bearer_user(request, db)
    item = get_ltx_item(db, user, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="LTX 任务不存在")
    segment = _item_segment(item, segment_index)
    task = segment.generation_task if segment is not None else None
    if task is None or not task.result_path:
        raise HTTPException(status_code=404, detail="LTX 分段视频尚未生成")
    path = safe_relative_path(task.result_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="LTX 分段视频文件不存在")
    return path, f"{item.row_key}-segment-{segment_index:03d}{path.suffix}"


@router.get("/api/workbench/ltx-items/{item_id}/segments/{segment_index}/video")
def download_ltx_segment(
    item_id: str,
    segment_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    path, filename = _segment_file(item_id, segment_index, request, db)
    return FileResponse(path, filename=filename)


@router.get(
    "/api/workbench/ltx-items/{item_id}/segments/{segment_index}/source-video"
)
def download_ltx_source_segment(
    item_id: str,
    segment_index: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = get_ltx_item(db, user, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="LTX 任务不存在")
    segment = _item_segment(item, segment_index)
    task = segment.generation_task if segment is not None else None
    enhancement = task.enhancement if task is not None else None
    if enhancement is None or not enhancement.source_result_path:
        raise HTTPException(status_code=404, detail="LTX 原始分段视频尚未生成")
    path = safe_relative_path(
        enhancement.source_result_path, get_settings().data_dir
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="LTX 原始分段视频文件不存在")
    return FileResponse(
        path,
        filename=f"{item.row_key}-segment-{segment_index:03d}-ltx-source{path.suffix}",
    )


@router.get("/api/workbench/ltx-items/{item_id}/base-video")
def download_ltx_base_video(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = get_ltx_item(db, user, item_id)
    if item is None or not item.merged_video_path:
        raise HTTPException(status_code=404, detail="LTX 基础视频尚未生成")
    path = safe_relative_path(item.merged_video_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="LTX 基础视频文件不存在")
    return FileResponse(path, filename=f"{item.row_key}-base{path.suffix}")
