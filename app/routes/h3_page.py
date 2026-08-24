from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import GenerationBatchItem, User
from app.routes.dependencies import check_rate_limit, get_current_user, get_page_user
from app.services.batch_assets import StagedAssetError
from app.services.csrf import require_csrf
from app.services.h3_workbench import (
    H3WorkbenchError,
    confirm_h3_workbench_batch,
    get_h3_batch,
    h3_account_payload,
    h3_batch_payload,
    prepare_h3_workbench_batch,
)
from app.services.storage import safe_relative_path


router = APIRouter(prefix="/api/h3-page", tags=["h3-page"])


def _error(exc: Exception, *, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/accounts")
def page_h3_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        return h3_account_payload(db, current_user)
    except H3WorkbenchError as exc:
        raise _error(exc, status_code=403) from exc


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
