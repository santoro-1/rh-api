"""Metadata-only callback API for the isolated cloud media verifier."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.services import media_verification


router = APIRouter(prefix="/api/cloud-media/jobs", tags=["cloud-media"])


def _authorize(authorization: str | None = Header(default=None)) -> Settings:
    settings = get_settings()
    if not settings.media_cloud_worker_enabled:
        raise HTTPException(status_code=503, detail="云端素材核验尚未启用")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not supplied or not hmac.compare_digest(supplied, settings.media_cloud_worker_token):
        raise HTTPException(status_code=401, detail="云端核验凭据无效")
    return settings


def _fail(exc: media_verification.VerificationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/claim")
def claim_job(settings: Settings = Depends(_authorize), db: Session = Depends(get_db)):
    return {"job": media_verification.claim(
        db, lease_seconds=settings.media_cloud_worker_lease_seconds,
        max_active=settings.media_cloud_worker_max_active,
    )}


@router.post("/{job_id}/heartbeat")
def heartbeat_job(
    job_id: str, payload: dict[str, Any] = Body(...),
    settings: Settings = Depends(_authorize), db: Session = Depends(get_db),
):
    try:
        return media_verification.heartbeat(
            db, job_id, str(payload.get("lease_id") or ""),
            lease_seconds=settings.media_cloud_worker_lease_seconds,
        )
    except media_verification.VerificationError as exc:
        raise _fail(exc) from exc


@router.post("/{job_id}/complete")
def complete_job(
    job_id: str, payload: dict[str, Any] = Body(...),
    _: Settings = Depends(_authorize), db: Session = Depends(get_db),
):
    observed_size = payload.get("observed_size")
    if observed_size is not None and (type(observed_size) is not int or observed_size < 0):
        raise HTTPException(status_code=400, detail="核验大小不合法")
    try:
        return media_verification.complete(
            db, job_id, str(payload.get("lease_id") or ""),
            outcome=str(payload.get("outcome") or ""),
            observed_size=observed_size,
            observed_sha256=payload.get("observed_sha256"),
            detected_kind=payload.get("detected_kind"),
            error_code=payload.get("error_code"),
        )
    except media_verification.VerificationError as exc:
        raise _fail(exc) from exc
