from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import GenerationBatchItem, H3RemoteAsrJob, User
from app.routes.dependencies import check_rate_limit, get_current_user, get_page_user
from app.services.batch_assets import StagedAssetError
from app.services.csrf import require_csrf
from app.services.h3.input_uploads import (
    H3_TRANSPORT_PROFILE,
    H3_WEB_SOURCE,
    H3InputUploadError,
    lookup_h3_asset,
    stage_h3_asset,
)
from app.services.h3_pool import user_has_h3_pool_entitlement
from app.services.h3_workbench import (
    H3WorkbenchError,
    confirm_h3_workbench_batch,
    get_h3_batch,
    h3_account_payload,
    h3_audio_alignment_job_payload,
    h3_batch_payload,
    prepare_h3_audio_alignment_job,
    prepare_h3_workbench_batch,
)
from app.services.storage import safe_relative_path


router = APIRouter(prefix="/api/h3-page", tags=["h3-page"])


def _error(exc: Exception, *, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


def _h3_upload_error(exc: H3InputUploadError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _require_h3_access(db: Session, user: User) -> None:
    if not user_has_h3_pool_entitlement(db, user):
        raise HTTPException(status_code=403, detail="当前账号未开通 H3 权限")


@router.post("/assets/lookup")
async def page_h3_asset_lookup(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="H3 素材查询内容不是有效对象")
    _require_h3_access(db, current_user)
    check_rate_limit(request, f"h3-page-asset-lookup:{current_user.id}", 120)
    try:
        return lookup_h3_asset(
            db,
            current_user,
            get_settings(),
            source_channel=H3_WEB_SOURCE,
            upload_key=str(payload.get("upload_key") or ""),
        )
    except H3InputUploadError as exc:
        raise _h3_upload_error(exc) from exc


@router.post("/assets")
def page_h3_asset_upload(
    request: Request,
    file: UploadFile = File(...),
    kind: str = Form(...),
    asset_role: str = Form(...),
    upload_key: str = Form(...),
    transport_profile: str = Form(H3_TRANSPORT_PROFILE),
    source_sha256: str = Form(""),
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    _require_h3_access(db, current_user)
    check_rate_limit(request, f"h3-page-asset-upload:{current_user.id}", 60)
    try:
        response, reused = stage_h3_asset(
            db,
            current_user,
            file,
            get_settings(),
            source_channel=H3_WEB_SOURCE,
            kind=kind,
            asset_role=asset_role,
            upload_key=upload_key,
            source_sha256=source_sha256 or None,
            transport_profile=transport_profile,
        )
    except H3InputUploadError as exc:
        raise _h3_upload_error(exc) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(response, status_code=200 if reused else 201)


@router.get("/accounts")
def page_h3_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return h3_account_payload(db, current_user)
    except H3WorkbenchError as exc:
        raise _error(exc, status_code=403) from exc


@router.post("/audio-alignments")
async def prepare_page_h3_audio_alignment(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="H3 对齐请求内容不是有效对象")
    settings = get_settings()
    check_rate_limit(
        request,
        f"h3-page-alignment:{current_user.id}",
        max(settings.task_create_rate_limit_per_minute * 10, 50),
    )
    try:
        job, created = prepare_h3_audio_alignment_job(
            db,
            current_user,
            settings,
            audio_asset_id=str(payload.get("audio_asset_id") or ""),
            script_text=str(payload.get("script_text") or ""),
        )
        response = h3_audio_alignment_job_payload(job)
    except (H3WorkbenchError, StagedAssetError, ValueError, OSError) as exc:
        raise _error(exc) from exc
    return JSONResponse(response, status_code=201 if created else 200)


@router.get("/audio-alignments/{job_id}")
def page_h3_audio_alignment(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(H3RemoteAsrJob, job_id)
    if job is None or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="H3 音频对齐任务不存在")
    try:
        return h3_audio_alignment_job_payload(job)
    except H3WorkbenchError as exc:
        raise _error(exc, status_code=409) from exc


@router.post("/batches/prepare")
async def prepare_page_h3_batch(
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="H3 请求内容不是有效对象")
    check_rate_limit(
        request,
        f"h3-page-prepare:{current_user.id}",
        max(get_settings().task_create_rate_limit_per_minute * 2, 10),
    )
    try:
        batch, created = prepare_h3_workbench_batch(
            db,
            current_user,
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
        raise _error(exc) from exc
    return JSONResponse(h3_batch_payload(batch), status_code=201 if created else 200)


@router.post("/batches/{batch_id}/confirm")
async def confirm_page_h3_batch(
    batch_id: str,
    request: Request,
    csrf_ok: None = Depends(require_csrf),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    del csrf_ok
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="H3 请求内容不是有效对象")
    if payload.get("cost_confirmed") is not True:
        raise HTTPException(status_code=409, detail="请先确认 H3 分段费用")
    check_rate_limit(
        request,
        f"h3-page-confirm:{current_user.id}",
        get_settings().task_create_rate_limit_per_minute,
    )
    try:
        batch = confirm_h3_workbench_batch(
            db, current_user, batch_id, cost_confirmed=True
        )
    except (H3WorkbenchError, ValueError) as exc:
        raise _error(exc) from exc
    return h3_batch_payload(batch)


@router.get("/batches/{batch_id}")
def page_h3_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = get_h3_batch(db, current_user, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="H3 批次不存在")
    return h3_batch_payload(batch)


@router.get("/items/{item_id}/audio")
def page_h3_input_audio(
    item_id: str,
    current_user: User = Depends(get_page_user),
    db: Session = Depends(get_db),
):
    item = db.get(GenerationBatchItem, item_id)
    if (
        item is None
        or item.batch.user_id != current_user.id
        or item.h3_config is None
    ):
        raise HTTPException(status_code=404, detail="H3 输入音频不存在")
    path = safe_relative_path(item.h3_config.full_audio_path, get_settings().data_dir)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="H3 输入音频文件不存在")
    return FileResponse(path, filename=item.h3_config.full_audio_original_name)
