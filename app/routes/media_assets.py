"""Authenticated, metadata-only OSS upload API (not wired into paid tasks yet)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.routes.dependencies import check_rate_limit, get_current_user
from app.services.csrf import require_csrf
from app.services.device_auth.admission import workbench_user
from app.services.media_oss import OssMediaStore
from app.services.media_uploads import (
    MediaUploadError, complete_upload, create_upload, sign_parts, upload_status,
)
from app.services.h3_pool import user_has_h3_pool_entitlement


router = APIRouter(tags=["media-assets"])


def _csrf_header(request: Request) -> None:
    # JSON API: the existing Form-based dependency would make FastAPI require
    # an embedded body and reject otherwise valid JSON before our handler.
    require_csrf(request, None)


def _store() -> OssMediaStore:
    settings = get_settings()
    if not settings.media_oss_enabled:
        raise HTTPException(status_code=503, detail="OSS 直传尚未启用")
    return OssMediaStore(settings)


def _fail(exc: MediaUploadError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _create(
    db: Session, user: User, request: Request, payload: dict[str, Any],
    *, allowed_channels: set[str],
) -> dict[str, object]:
    check_rate_limit(request, f"media-upload-create:{user.id}", 60)
    channel = str(payload.get("source_channel") or "")
    if channel not in allowed_channels:
        raise HTTPException(status_code=403, detail="素材来源不允许")
    if channel.startswith("h3_") and not user_has_h3_pool_entitlement(db, user):
        raise HTTPException(status_code=403, detail="当前账号未开通 H3 权限")
    try:
        return create_upload(
            db, user, get_settings(), _store(),
            kind=str(payload.get("kind") or ""),
            source_channel=channel,
            asset_role=str(payload.get("asset_role") or ""),
            upload_key=str(payload.get("upload_key") or ""),
            filename=str(payload.get("filename") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            source_sha256=str(payload.get("source_sha256") or "").lower(),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, MediaUploadError):
            raise _fail(exc) from exc
        raise HTTPException(status_code=400, detail="素材参数不合法") from exc


def _status(db: Session, user: User, asset_id: str) -> dict[str, object]:
    try:
        return upload_status(db, user, _store(), asset_id)
    except MediaUploadError as exc:
        raise _fail(exc) from exc


def _parts(db: Session, user: User, asset_id: str, start: int, count: int) -> dict[str, object]:
    try:
        return sign_parts(db, user, _store(), asset_id, start=start, count=count)
    except MediaUploadError as exc:
        raise _fail(exc) from exc


def _part_range(payload: dict[str, Any]) -> tuple[int, int]:
    try:
        return int(payload.get("start") or 0), int(payload.get("count") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="分片范围不合法") from exc


def _complete(db: Session, user: User, asset_id: str) -> dict[str, object]:
    try:
        return complete_upload(db, user, _store(), asset_id)
    except MediaUploadError as exc:
        raise _fail(exc) from exc


@router.post("/api/media-assets/uploads")
def web_create(
    request: Request, payload: dict[str, Any] = Body(...),
    _: None = Depends(_csrf_header), user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _create(db, user, request, payload, allowed_channels={"h3_web", "web_legacy"})


@router.get("/api/media-assets/uploads/{asset_id}")
def web_status(asset_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _status(db, user, asset_id)


@router.post("/api/media-assets/uploads/{asset_id}/parts")
def web_parts(
    asset_id: str, payload: dict[str, Any] = Body(...),
    _: None = Depends(_csrf_header), user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _parts(db, user, asset_id, *_part_range(payload))


@router.post("/api/media-assets/uploads/{asset_id}/complete")
def web_complete(
    asset_id: str, _: None = Depends(_csrf_header),
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    return _complete(db, user, asset_id)


@router.post("/api/workbench/media-assets/uploads")
def workbench_create(
    request: Request, payload: dict[str, Any] = Body(...), db: Session = Depends(get_db),
):
    user = workbench_user(request, db, body_token=str(payload.get("access_token") or ""), new_work=True)
    return _create(
        db, user, request, payload,
        allowed_channels={"h3_workbench", "workbench_legacy", "ltx_workbench"},
    )


@router.post("/api/workbench/media-assets/uploads/{asset_id}/status")
def workbench_status(
    asset_id: str, request: Request, payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(request, db, body_token=str(payload.get("access_token") or ""))
    return _status(db, user, asset_id)


@router.post("/api/workbench/media-assets/uploads/{asset_id}/parts")
def workbench_parts(
    asset_id: str, request: Request, payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(request, db, body_token=str(payload.get("access_token") or ""), new_work=True)
    return _parts(db, user, asset_id, *_part_range(payload))


@router.post("/api/workbench/media-assets/uploads/{asset_id}/complete")
def workbench_complete(
    asset_id: str, request: Request, payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
):
    user = workbench_user(request, db, body_token=str(payload.get("access_token") or ""), new_work=True)
    return _complete(db, user, asset_id)
