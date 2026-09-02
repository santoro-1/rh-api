from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ArkAnalysisOperation,
    ContentAnalysisCache,
    VisualAnalysisCache,
)


ACTIVE_STATUSES = {"QUEUED", "RUNNING"}
FINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "EXPIRED"}


class ArkOperationConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def claim_ark_operation(
    db: Session,
    *,
    operation_id: str,
    user_id: int,
    kind: str,
    business_key: str,
    request_sha256: str,
) -> ArkAnalysisOperation:
    business_digest = _digest(business_key)
    existing = db.scalar(
        select(ArkAnalysisOperation).where(
            ArkAnalysisOperation.operation_id == operation_id
        )
    )
    if existing is not None:
        _validate_replay(
            existing,
            user_id=user_id,
            kind=kind,
            business_digest=business_digest,
            request_sha256=request_sha256,
        )
        return existing
    record = ArkAnalysisOperation(
        operation_id=operation_id,
        user_id=user_id,
        kind=kind,
        business_key_sha256=business_digest,
        request_sha256=request_sha256,
        status="QUEUED",
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ArkAnalysisOperation).where(
                ArkAnalysisOperation.operation_id == operation_id
            )
        )
        if existing is None:
            raise
        _validate_replay(
            existing,
            user_id=user_id,
            kind=kind,
            business_digest=business_digest,
            request_sha256=request_sha256,
        )
        return existing
    db.refresh(record)
    return record


def replay_ark_operation_result(
    db: Session, record: ArkAnalysisOperation
) -> dict[str, object] | None:
    if record.status not in {"SUCCEEDED", "PARTIAL"} or record.cache_id is None:
        return None
    if record.cache_kind == "content":
        from app.services.content_analysis.analysis import _serialize

        cache = db.get(ContentAnalysisCache, record.cache_id)
        return _serialize(cache, cache_hit=True) if cache is not None else None
    if record.cache_kind == "visual":
        from app.services.visual_analysis.analysis import _serialize

        cache = db.get(VisualAnalysisCache, record.cache_id)
        return _serialize(cache, cache_hit=True) if cache is not None else None
    return None


def _validate_replay(
    record: ArkAnalysisOperation,
    *,
    user_id: int,
    kind: str,
    business_digest: str,
    request_sha256: str,
) -> None:
    if (
        record.user_id != user_id
        or record.kind != kind
        or record.business_key_sha256 != business_digest
        or record.request_sha256 != request_sha256
    ):
        raise ArkOperationConflict(
            "ARK_OPERATION_ID_CONFLICT",
            "analysis_operation_id 已被另一份请求使用",
        )
    if record.status in {"FAILED", "EXPIRED"}:
        raise ArkOperationConflict(
            "ARK_OPERATION_REPLAY_BLOCKED",
            "该操作已终止；为避免重复计费，请创建新的操作 ID 后人工重试",
        )


def mark_ark_operation_running(db: Session, operation_id: str) -> None:
    record = db.scalar(
        select(ArkAnalysisOperation).where(
            ArkAnalysisOperation.operation_id == operation_id
        )
    )
    if record is None or record.status == "SUCCEEDED":
        return
    record.status = "RUNNING"
    record.started_at = record.started_at or datetime.now(timezone.utc)
    record.error_code = None
    record.error_summary = None
    db.commit()


def mark_ark_operation_succeeded(
    db: Session,
    operation_id: str,
    *,
    cache_kind: str | None = None,
    cache_id: int | None = None,
    status: str = "SUCCEEDED",
) -> None:
    record = db.scalar(
        select(ArkAnalysisOperation).where(
            ArkAnalysisOperation.operation_id == operation_id
        )
    )
    if record is None:
        return
    record.status = "PARTIAL" if status == "PARTIAL" else "SUCCEEDED"
    record.cache_kind = cache_kind
    record.cache_id = cache_id
    record.completed_at = datetime.now(timezone.utc)
    record.error_code = None
    record.error_summary = None
    db.commit()


def mark_ark_operation_failed(
    db: Session,
    operation_id: str,
    *,
    code: str,
    summary: str,
) -> None:
    record = db.scalar(
        select(ArkAnalysisOperation).where(
            ArkAnalysisOperation.operation_id == operation_id
        )
    )
    if record is None or record.status == "SUCCEEDED":
        return
    record.status = "FAILED"
    record.error_code = str(code)[:100]
    record.error_summary = str(summary)[:500]
    record.completed_at = datetime.now(timezone.utc)
    db.commit()


def release_unadmitted_ark_operation(db: Session, operation_id: str) -> None:
    """Allow the same ID to retry only when no paid worker ever started."""

    record = db.scalar(
        select(ArkAnalysisOperation).where(
            ArkAnalysisOperation.operation_id == operation_id
        )
    )
    if record is None or record.status != "QUEUED" or record.started_at is not None:
        return
    db.delete(record)
    db.commit()


def expire_stale_ark_operations(db: Session, *, stale_seconds: int = 600) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, stale_seconds))
    result = db.execute(
        update(ArkAnalysisOperation)
        .where(
            ArkAnalysisOperation.status.in_(ACTIVE_STATUSES),
            ArkAnalysisOperation.updated_at < cutoff,
        )
        .values(
            status="EXPIRED",
            error_code="ARK_OPERATION_RECOVERY_REQUIRED",
            error_summary="服务重启后无法确认上一次付费请求结果，已阻止自动重放",
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return int(result.rowcount or 0)
