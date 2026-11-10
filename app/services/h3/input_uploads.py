from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import StagedAsset, User
from app.services.logging_config import log_event
from app.services.storage import (
    remove_directory,
    safe_relative_path,
    save_upload,
    staged_asset_dir,
    to_relative_data_path,
)


LOGGER = logging.getLogger(__name__)
H3_TRANSPORT_PROFILE = "original-v1"
H3_WEB_SOURCE = "h3_web"
H3_WORKBENCH_SOURCE = "h3_workbench"
ROLE_KINDS = {
    "reference_image": "image",
    "reference_video": "video",
}
_UPLOAD_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_SIZE = 1024 * 1024


class H3InputUploadError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_request(
    *,
    kind: str,
    asset_role: str,
    upload_key: str,
    source_sha256: str | None,
    transport_profile: str,
) -> str | None:
    if asset_role not in ROLE_KINDS or ROLE_KINDS[asset_role] != kind:
        raise H3InputUploadError(
            "H3 素材类型与用途不匹配",
            code="H3_UPLOAD_ROLE_INVALID",
        )
    if not _UPLOAD_KEY.fullmatch(upload_key):
        raise H3InputUploadError(
            "H3 上传键格式不合法",
            code="H3_UPLOAD_KEY_INVALID",
        )
    if transport_profile != H3_TRANSPORT_PROFILE:
        raise H3InputUploadError(
            "H3 上传传输方案不受支持",
            code="H3_TRANSPORT_PROFILE_INVALID",
        )
    normalized_sha = (source_sha256 or "").strip().lower() or None
    if normalized_sha is not None and not _SHA256.fullmatch(normalized_sha):
        raise H3InputUploadError(
            "H3 素材摘要格式不合法",
            code="H3_SOURCE_SHA256_INVALID",
        )
    return normalized_sha


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_upload(upload: UploadFile) -> str:
    digest = hashlib.sha256()
    try:
        while chunk := upload.file.read(_CHUNK_SIZE):
            digest.update(chunk)
    finally:
        upload.file.seek(0)
    return digest.hexdigest()


def _asset_path(asset: StagedAsset, settings: Settings) -> Path:
    return safe_relative_path(asset.relative_path, settings.data_dir)


def _is_reusable(asset: StagedAsset, settings: Settings) -> bool:
    expires_at = asset.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (
        asset.consumed_at is None
        and expires_at > _now()
        and _asset_path(asset, settings).is_file()
    )


def _payload(asset: StagedAsset, *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "asset_id": asset.id,
        "kind": asset.kind,
        "asset_role": asset.asset_role,
        "size_bytes": asset.size_bytes,
        "source_sha256": asset.source_sha256,
        "transport_sha256": asset.transport_sha256,
        "transport_profile": asset.transport_profile,
    }


def _find_by_key(
    db: Session,
    user: User,
    *,
    source_channel: str,
    upload_key: str,
) -> StagedAsset | None:
    return db.scalar(
        select(StagedAsset).where(
            StagedAsset.user_id == user.id,
            StagedAsset.source_channel == source_channel,
            StagedAsset.upload_key == upload_key,
        )
    )


def lookup_h3_asset(
    db: Session,
    user: User,
    settings: Settings,
    *,
    source_channel: str,
    upload_key: str,
) -> dict[str, object]:
    if not _UPLOAD_KEY.fullmatch(upload_key):
        raise H3InputUploadError(
            "H3 上传键格式不合法",
            code="H3_UPLOAD_KEY_INVALID",
        )
    asset = _find_by_key(
        db, user, source_channel=source_channel, upload_key=upload_key
    )
    if asset is None:
        return {"found": False}
    if _is_reusable(asset, settings):
        return {"found": True, **_payload(asset, status="reused")}
    if asset.consumed_at is None:
        directory = staged_asset_dir(settings, user.id, asset.id)
        db.delete(asset)
        db.commit()
        remove_directory(directory)
    return {"found": False}


def _ensure_existing_matches(
    asset: StagedAsset,
    *,
    kind: str,
    asset_role: str,
    source_sha256: str | None,
    transport_profile: str,
) -> None:
    expected_sha = source_sha256 or asset.source_sha256
    if (
        asset.kind != kind
        or asset.asset_role != asset_role
        or asset.transport_profile != transport_profile
        or (expected_sha is not None and asset.source_sha256 != expected_sha)
    ):
        raise H3InputUploadError(
            "同一 H3 上传键已绑定其他素材",
            code="H3_UPLOAD_KEY_CONFLICT",
            status_code=409,
        )


def stage_h3_asset(
    db: Session,
    user: User,
    upload: UploadFile,
    settings: Settings,
    *,
    source_channel: str,
    kind: str,
    asset_role: str,
    upload_key: str,
    source_sha256: str | None,
    transport_profile: str,
) -> tuple[dict[str, object], bool]:
    started = time.monotonic()
    normalized_sha = _validate_request(
        kind=kind,
        asset_role=asset_role,
        upload_key=upload_key,
        source_sha256=source_sha256,
        transport_profile=transport_profile,
    )
    existing = _find_by_key(
        db, user, source_channel=source_channel, upload_key=upload_key
    )
    if existing is not None:
        incoming_sha = _sha256_upload(upload)
        if normalized_sha is not None and normalized_sha != incoming_sha:
            raise H3InputUploadError(
                "上传后的 H3 素材摘要与本地文件不一致",
                code="H3_SOURCE_SHA256_MISMATCH",
                status_code=422,
            )
        normalized_sha = incoming_sha
        _ensure_existing_matches(
            existing,
            kind=kind,
            asset_role=asset_role,
            source_sha256=normalized_sha,
            transport_profile=transport_profile,
        )
        if _is_reusable(existing, settings):
            payload = _payload(existing, status="reused")
            _log_upload(existing, source_channel, started, reused=True)
            return payload, True
        raise H3InputUploadError(
            "该 H3 上传键对应的素材已消费或失效",
            code="H3_UPLOAD_KEY_CONFLICT",
            status_code=409,
        )

    asset_id = str(uuid.uuid4())
    directory = staged_asset_dir(settings, user.id, asset_id)
    try:
        path, original_name = save_upload(upload, directory, kind, settings)
        size_bytes = path.stat().st_size
        transport_sha = sha256_file(path)
        if normalized_sha is not None and normalized_sha != transport_sha:
            raise H3InputUploadError(
                "上传后的 H3 素材摘要与本地文件不一致",
                code="H3_SOURCE_SHA256_MISMATCH",
                status_code=422,
            )
        source_sha = normalized_sha or transport_sha
        # Preserve the verified digest for the unique-key race path below.
        normalized_sha = source_sha
        current_bytes = db.scalar(
            select(func.coalesce(func.sum(StagedAsset.size_bytes), 0)).where(
                StagedAsset.user_id == user.id,
                StagedAsset.consumed_at.is_(None),
                StagedAsset.expires_at > _now(),
            )
        )
        maximum = settings.max_batch_total_upload_mb * 1024 * 1024
        if int(current_bytes or 0) + size_bytes > maximum:
            raise H3InputUploadError(
                f"当前批量暂存素材总量不能超过 {settings.max_batch_total_upload_mb} MB",
                code="H3_UPLOAD_QUOTA_EXCEEDED",
                status_code=413,
            )
        now = _now()
        asset = StagedAsset(
            id=asset_id,
            user_id=user.id,
            kind=kind,
            relative_path=to_relative_data_path(path, settings),
            original_name=original_name,
            size_bytes=size_bytes,
            source_channel=source_channel,
            asset_role=asset_role,
            upload_key=upload_key,
            source_sha256=source_sha,
            transport_sha256=transport_sha,
            transport_profile=transport_profile,
            created_at=now,
            expires_at=now + timedelta(hours=settings.staged_asset_retention_hours),
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        _log_upload(asset, source_channel, started, reused=False)
        return _payload(asset, status="uploaded"), False
    except IntegrityError:
        db.rollback()
        remove_directory(directory)
        existing = _find_by_key(
            db, user, source_channel=source_channel, upload_key=upload_key
        )
        if existing is None:
            raise
        _ensure_existing_matches(
            existing,
            kind=kind,
            asset_role=asset_role,
            source_sha256=normalized_sha,
            transport_profile=transport_profile,
        )
        if not _is_reusable(existing, settings):
            raise H3InputUploadError(
                "该 H3 上传键对应的素材已消费或失效",
                code="H3_UPLOAD_KEY_CONFLICT",
                status_code=409,
            )
        _log_upload(existing, source_channel, started, reused=True)
        return _payload(existing, status="reused"), True
    except Exception:
        db.rollback()
        remove_directory(directory)
        raise


def _log_upload(
    asset: StagedAsset,
    source_channel: str,
    started: float,
    *,
    reused: bool,
) -> None:
    log_event(
        LOGGER,
        "h3_input_upload",
        "H3 输入素材已复用" if reused else "H3 输入素材上传完成",
        source_channel=source_channel,
        asset_role=asset.asset_role,
        kind=asset.kind,
        size_bytes=asset.size_bytes,
        elapsed_ms=round((time.monotonic() - started) * 1000),
        source_sha256_prefix=(asset.source_sha256 or "")[:12],
        transport_sha256_prefix=(asset.transport_sha256 or "")[:12],
        reused=reused,
    )
