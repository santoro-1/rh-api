"""Leased OSS verification jobs; no media bytes pass through this process."""

from __future__ import annotations

import re
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import MediaAsset, MediaVerificationJob


MAX_ATTEMPTS = 3
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODES = {"OBJECT_MISSING", "SIZE_MISMATCH", "SHA_MISMATCH", "FORMAT_INVALID", "WORKER_ERROR"}


class VerificationError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def enqueue(db: Session, asset: MediaAsset) -> MediaVerificationJob:
    """Call in the same transaction that changes the asset to UPLOADED."""
    job = next(
        (item for item in db.new if isinstance(item, MediaVerificationJob) and item.asset_id == asset.id),
        None,
    )
    if job is None:
        job = db.scalar(select(MediaVerificationJob).where(MediaVerificationJob.asset_id == asset.id))
    if job is None:
        job = MediaVerificationJob(
            id=str(uuid.uuid4()), asset_id=asset.id, status="PENDING", attempts=0,
            created_at=_now(), updated_at=_now(),
        )
        db.add(job)
    return job


def claim(db: Session, *, lease_seconds: int, max_active: int) -> dict[str, object] | None:
    now = _now()
    active = select(func.count(MediaVerificationJob.id)).where(
        MediaVerificationJob.status == "RUNNING",
        MediaVerificationJob.lease_expires_at > now,
    ).scalar_subquery()
    candidates = db.scalars(select(MediaVerificationJob).where(or_(
        MediaVerificationJob.status == "PENDING",
        (MediaVerificationJob.status == "RUNNING") & (MediaVerificationJob.lease_expires_at <= now),
    )).order_by(MediaVerificationJob.created_at).limit(16)).all()
    for candidate in candidates:
        asset = db.get(MediaAsset, candidate.asset_id)
        if asset is None:
            continue
        if candidate.attempts >= MAX_ATTEMPTS or _aware(asset.expires_at) <= now:
            changed = db.execute(update(MediaVerificationJob).where(
                MediaVerificationJob.id == candidate.id,
                MediaVerificationJob.status == candidate.status,
                MediaVerificationJob.attempts == candidate.attempts,
            ).values(status="REJECTED", error_code="WORKER_ERROR", updated_at=now)).rowcount
            if changed:
                asset.status = "REJECTED"
                db.commit()
            continue
        lease_id = str(uuid.uuid4())
        claimed = db.execute(update(MediaVerificationJob).where(
            MediaVerificationJob.id == candidate.id,
            MediaVerificationJob.status == candidate.status,
            MediaVerificationJob.attempts == candidate.attempts,
            or_(MediaVerificationJob.status == "PENDING", MediaVerificationJob.lease_expires_at <= now),
            active < max_active,
        ).values(
            status="RUNNING", lease_id=lease_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            attempts=candidate.attempts + 1, error_code=None, receipt_sha256=None, updated_at=now,
        )).rowcount
        if claimed:
            asset.status = "VERIFYING"
            db.commit()
            return {
                "job_id": candidate.id, "lease_id": lease_id,
                "object_key": asset.object_key, "asset_id": asset.id,
                "kind": asset.kind, "size_bytes": asset.size_bytes,
                "expected_sha256": asset.expected_sha256,
                "lease_expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
            }
        db.rollback()
        return None
    return None


def _leased(db: Session, job_id: str, lease_id: str) -> tuple[MediaVerificationJob, MediaAsset]:
    job = db.get(MediaVerificationJob, job_id)
    if job is None:
        raise VerificationError("核验任务不存在", 404)
    asset = db.get(MediaAsset, job.asset_id)
    if asset is None or job.lease_id != lease_id:
        raise VerificationError("核验租约无效")
    return job, asset


def heartbeat(db: Session, job_id: str, lease_id: str, *, lease_seconds: int) -> dict[str, object]:
    job, _ = _leased(db, job_id, lease_id)
    now = _now()
    if job.status != "RUNNING" or _aware(job.lease_expires_at) <= now:
        raise VerificationError("核验租约已过期")
    new_expiry = now + timedelta(seconds=lease_seconds)
    renewed = db.execute(update(MediaVerificationJob).where(
        MediaVerificationJob.id == job_id,
        MediaVerificationJob.status == "RUNNING",
        MediaVerificationJob.lease_id == lease_id,
        MediaVerificationJob.lease_expires_at > now,
    ).values(lease_expires_at=new_expiry, updated_at=now).execution_options(
        synchronize_session=False
    )).rowcount
    if not renewed:
        db.rollback()
        raise VerificationError("核验租约已过期")
    db.commit()
    return {"ok": True, "lease_expires_at": new_expiry.isoformat()}


def complete(
    db: Session, job_id: str, lease_id: str, *, outcome: str,
    observed_size: int | None = None, observed_sha256: str | None = None,
    detected_kind: str | None = None, error_code: str | None = None,
) -> dict[str, object]:
    if outcome not in {"verified", "invalid", "retryable_error"}:
        raise VerificationError("核验结果不合法", 400)
    job, asset = _leased(db, job_id, lease_id)
    receipt = hashlib.sha256(json.dumps(
        [outcome, observed_size, observed_sha256, detected_kind, error_code],
        separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    if job.receipt_sha256 == receipt:
        return {"asset_id": asset.id, "status": asset.status}
    if job.status != "RUNNING" or _aware(job.lease_expires_at) <= _now():
        raise VerificationError("核验租约已过期")
    if outcome == "verified":
        if not isinstance(observed_sha256, str) or not _SHA.fullmatch(observed_sha256):
            raise VerificationError("摘要格式不合法", 400)
        if observed_size != asset.size_bytes:
            outcome, error_code = "invalid", "SIZE_MISMATCH"
        elif observed_sha256 != asset.expected_sha256:
            outcome, error_code = "invalid", "SHA_MISMATCH"
        elif detected_kind != asset.kind:
            outcome, error_code = "invalid", "FORMAT_INVALID"
    elif not isinstance(error_code, str) or error_code not in _ERROR_CODES:
        raise VerificationError("错误码不合法", 400)
    if outcome == "verified":
        next_job, next_asset, error_code = "SUCCEEDED", "READY", None
    elif outcome == "invalid" or job.attempts >= MAX_ATTEMPTS:
        next_job, next_asset = "REJECTED", "REJECTED"
    else:
        next_job, next_asset = "PENDING", "UPLOADED"
    now = _now()
    updated = db.execute(update(MediaVerificationJob).where(
        MediaVerificationJob.id == job_id,
        MediaVerificationJob.status == "RUNNING",
        MediaVerificationJob.lease_id == lease_id,
        MediaVerificationJob.lease_expires_at > now,
    ).values(
        status=next_job, error_code=error_code,
        receipt_sha256=receipt, updated_at=now,
    ).execution_options(synchronize_session=False)).rowcount
    if not updated:
        db.rollback()
        raise VerificationError("核验租约已过期")
    asset.status = next_asset
    if next_asset == "READY":
        asset.verified_sha256 = observed_sha256
    db.commit()
    return {"asset_id": asset.id, "status": asset.status}
