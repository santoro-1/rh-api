from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import GenerationBatchItem, GenerationSegment, User
from app.routes.dependencies import check_rate_limit
from app.services.batch_assets import StagedAssetError
from app.services.h3_workbench import (
    H3WorkbenchError,
    approve_h3_audio_source,
    cancel_h3_segment,
    confirm_h3_workbench_batch,
    confirm_h3_segment_regeneration,
    confirm_h3_segment_retry,
    get_h3_batch,
    h3_account_payload,
    h3_audio_sources_payload,
    h3_batch_payload,
    prepare_h3_workbench_batch,
    prepare_h3_segment_regeneration,
    prepare_h3_segment_retry,
)
from app.services.workbench_auth import decode_workbench_token, token_matches_user
from app.services.storage import safe_relative_path


router = APIRouter(tags=["workbench-h3"])


def _token_user(token: str, db: Session) -> User:
    payload = decode_workbench_token(token, get_settings())
    if payload is None:
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    user = db.get(User, int(payload["user_id"]))
    if user is None or not token_matches_user(payload, user):
        raise HTTPException(status_code=401, detail="账号已停用、已删除或登录已失效")
    return user


def _service_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _bearer_user(request: Request, db: Session) -> User:
    authorization = str(request.headers.get("authorization") or "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="缺少下载授权")
    return _token_user(token.strip(), db)


@router.post("/api/workbench/h3-execution-accounts")
def get_h3_execution_accounts(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    try:
        return h3_account_payload(db, user)
    except H3WorkbenchError as exc:
        raise _service_error(exc) from exc


@router.post("/api/workbench/h3-audio-sources")
def get_h3_audio_sources(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    return h3_audio_sources_payload(db, user)


@router.post("/api/workbench/h3-audio-sources/approve")
def approve_h3_audio(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    try:
        task = approve_h3_audio_source(
            db,
            user,
            audio_batch_id=str(payload.get("audio_batch_id") or ""),
            audio_item_id=str(payload.get("audio_item_id") or ""),
            audio_generation_version=int(payload.get("audio_generation_version") or 0),
        )
    except (H3WorkbenchError, TypeError, ValueError) as exc:
        raise _service_error(exc) from exc
    return {
        "audio_batch_id": task.batch_item.batch_id,
        "audio_item_id": task.batch_item_id,
        "audio_generation_version": task.generation_version,
        "status": task.status,
        "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
    }


@router.post("/api/workbench/h3-batches/prepare")
def prepare_h3_batch(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    check_rate_limit(
        request,
        f"workbench-h3-prepare:{user.id}",
        max(get_settings().task_create_rate_limit_per_minute * 2, 10),
    )
    try:
        batch, created = prepare_h3_workbench_batch(
            db,
            user,
            get_settings(),
            name=str(payload.get("name") or ""),
            request_key=str(payload.get("request_key") or ""),
            correlation_id=str(payload.get("correlation_id") or "") or None,
            reference_image_asset_ids=payload.get("reference_image_asset_ids", []),
            defaults=payload.get("defaults"),
            rows=payload.get("rows"),
            selected_account_ids=payload.get("selected_account_ids"),
        )
    except (H3WorkbenchError, StagedAssetError, ValueError, OSError) as exc:
        raise _service_error(exc) from exc
    return JSONResponse(
        h3_batch_payload(batch),
        status_code=201 if created else 200,
    )


@router.post("/api/workbench/h3-batches/{batch_id}/confirm")
def confirm_h3_batch(
    batch_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认每个未复用 H3 分段都会产生 RunningHub 费用",
        )
    check_rate_limit(
        request,
        f"workbench-h3-confirm:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch = confirm_h3_workbench_batch(
            db,
            user,
            batch_id,
            cost_confirmed=payload.get("cost_confirmed"),
        )
    except (H3WorkbenchError, ValueError) as exc:
        raise _service_error(exc) from exc
    return h3_batch_payload(batch)


@router.post("/api/workbench/h3-batches/{batch_id}")
def get_h3_batch_status(
    batch_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    batch = get_h3_batch(db, user, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="H3 批次不存在")
    return h3_batch_payload(batch)


@router.post("/api/workbench/h3-segments/{segment_id}/regeneration/prepare")
def prepare_h3_regeneration(
    segment_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    try:
        return prepare_h3_segment_regeneration(db, user, segment_id)
    except (H3WorkbenchError, ValueError) as exc:
        raise _service_error(exc) from exc


@router.post("/api/workbench/h3-segments/{segment_id}/regeneration/confirm")
def confirm_h3_regeneration(
    segment_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(
            status_code=409,
            detail="请确认主动重生成及全部连续下游分段都会产生 RunningHub 费用",
        )
    check_rate_limit(
        request,
        f"workbench-h3-regenerate:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch, receipt = confirm_h3_segment_regeneration(
            db,
            user,
            segment_id,
            request_key=str(payload.get("request_key") or ""),
            quote_token=str(payload.get("quote_token") or ""),
            cost_confirmed=payload.get("cost_confirmed"),
            settings=get_settings(),
        )
    except (H3WorkbenchError, ValueError, OSError) as exc:
        raise _service_error(exc) from exc
    response = h3_batch_payload(batch)
    response["regeneration"] = receipt
    return response


@router.post("/api/workbench/h3-segments/{segment_id}/retry/prepare")
def prepare_h3_retry(
    segment_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    try:
        return prepare_h3_segment_retry(db, user, segment_id)
    except (H3WorkbenchError, ValueError) as exc:
        raise _service_error(exc) from exc


@router.post("/api/workbench/h3-segments/{segment_id}/retry/confirm")
def confirm_h3_retry(
    segment_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    check_rate_limit(
        request,
        f"workbench-h3-retry:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch, receipt = confirm_h3_segment_retry(
            db,
            user,
            segment_id,
            request_key=str(payload.get("request_key") or ""),
            quote_token=str(payload.get("quote_token") or ""),
            cost_confirmed=payload.get("cost_confirmed"),
            settings=get_settings(),
        )
    except (H3WorkbenchError, ValueError, OSError) as exc:
        raise _service_error(exc) from exc
    response = h3_batch_payload(batch)
    response["retry"] = receipt
    return response


@router.post("/api/workbench/h3-segments/{segment_id}/cancel")
def cancel_h3_task(
    segment_id: str,
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = _token_user(str(payload.get("access_token") or ""), db)
    check_rate_limit(
        request,
        f"workbench-h3-cancel:{user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch, receipt = cancel_h3_segment(
            db,
            user,
            segment_id,
            request_key=str(payload.get("request_key") or ""),
        )
    except (H3WorkbenchError, ValueError) as exc:
        raise _service_error(exc) from exc
    response = h3_batch_payload(batch)
    response["cancellation"] = receipt
    return response


@router.get("/api/workbench/h3-segments/{segment_id}/video")
def download_h3_segment_video(
    segment_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    segment = db.scalar(
        select(GenerationSegment).where(GenerationSegment.id == segment_id)
    )
    if (
        segment is None
        or segment.batch_item.batch.user_id != user.id
        or segment.h3_config is None
        or segment.h3_config.invalidated_at is not None
        or not segment.h3_config.normalized_video_path
    ):
        raise HTTPException(status_code=404, detail="H3 标准化分段不存在")
    path = safe_relative_path(
        segment.h3_config.normalized_video_path,
        get_settings().data_dir,
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="H3 标准化分段文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/api/workbench/h3-items/{item_id}/raw-cues")
def download_h3_raw_cues(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = db.get(GenerationBatchItem, item_id)
    if (
        item is None
        or item.batch.user_id != user.id
        or item.h3_config is None
        or not item.h3_config.raw_cues_path
    ):
        raise HTTPException(status_code=404, detail="H3 raw cues 不存在")
    path = safe_relative_path(item.h3_config.raw_cues_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="H3 raw cues 文件不存在")
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{item.row_key}-raw-cues.json",
    )


@router.get("/api/workbench/h3-items/{item_id}/audio")
def download_h3_input_audio(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _bearer_user(request, db)
    item = db.get(GenerationBatchItem, item_id)
    if (
        item is None
        or item.batch.user_id != user.id
        or item.h3_config is None
        or not item.h3_config.full_audio_path
    ):
        raise HTTPException(status_code=404, detail="H3 输入音频不存在")
    path = safe_relative_path(item.h3_config.full_audio_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="H3 输入音频文件不存在")
    return FileResponse(path, filename=item.h3_config.full_audio_original_name)
