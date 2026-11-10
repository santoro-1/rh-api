"""Idempotent metadata-only upload sessions for private OSS media."""

from __future__ import annotations

import math
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MediaAsset, StagedAsset, User
from app.services.media_oss import OssMediaStore, UploadedPart
from app.services.media_verification import enqueue


PART_SIZE = 8 * 1024 * 1024
LOGGER = logging.getLogger(__name__)
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"image", "audio", "video"}
_CHANNELS = {"h3_web", "h3_workbench", "web_legacy", "workbench_legacy", "ltx_workbench"}


class MediaUploadError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400, code: str = "MEDIA_UPLOAD_INVALID") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry(asset: MediaAsset) -> datetime:
    value = asset.expires_at
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _parts_count(asset: MediaAsset) -> int:
    return math.ceil(asset.size_bytes / asset.part_size)


def _payload(asset: MediaAsset) -> dict[str, object]:
    return {
        "asset_id": asset.id,
        "status": asset.status,
        "kind": asset.kind,
        "size_bytes": asset.size_bytes,
        "part_size": asset.part_size,
        "part_count": _parts_count(asset),
        "source_channel": asset.source_channel,
        "asset_role": asset.asset_role,
        "source_sha256": asset.expected_sha256,
    }


def _validate(
    settings: Settings,
    *, kind: str, source_channel: str, asset_role: str,
    upload_key: str, filename: str, size_bytes: int, source_sha256: str,
) -> str:
    if not settings.media_oss_enabled:
        raise MediaUploadError("OSS 直传尚未启用", status_code=503, code="MEDIA_OSS_DISABLED")
    if kind not in _KINDS or source_channel not in _CHANNELS:
        raise MediaUploadError("素材类型或来源不受支持")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", asset_role):
        raise MediaUploadError("素材用途不合法")
    if source_channel.startswith("h3_") and (kind, asset_role) not in {
        ("image", "reference_image"), ("video", "reference_video"),
        ("audio", "input_audio"),
    }:
        raise MediaUploadError("H3 素材类型与用途不匹配")
    if not _KEY.fullmatch(upload_key):
        raise MediaUploadError("上传键格式不合法")
    if not _SHA.fullmatch(source_sha256):
        raise MediaUploadError("SHA-256 格式不合法")
    clean_name = filename.replace("\\", "/").split("/")[-1].strip()
    if not clean_name or len(clean_name) > 255 or any(ord(ch) < 32 for ch in clean_name):
        raise MediaUploadError("文件名不合法")
    limit_mb = getattr(settings, f"max_{kind}_size_mb")
    if not 1 <= size_bytes <= limit_mb * 1024 * 1024:
        raise MediaUploadError("素材大小超过限制", status_code=413, code="MEDIA_SIZE_INVALID")
    return clean_name


def _owned(db: Session, user: User, asset_id: str) -> MediaAsset:
    asset = db.scalar(select(MediaAsset).where(MediaAsset.id == asset_id, MediaAsset.user_id == user.id))
    if asset is None:
        raise MediaUploadError("素材不存在", status_code=404, code="MEDIA_NOT_FOUND")
    if _expiry(asset) <= _now():
        raise MediaUploadError("上传会话已过期", status_code=410, code="MEDIA_EXPIRED")
    return asset


def create_upload(
    db: Session, user: User, settings: Settings, store: OssMediaStore,
    *, kind: str, source_channel: str, asset_role: str,
    upload_key: str, filename: str, size_bytes: int, source_sha256: str,
) -> dict[str, object]:
    clean_name = _validate(
        settings, kind=kind, source_channel=source_channel, asset_role=asset_role,
        upload_key=upload_key, filename=filename, size_bytes=size_bytes,
        source_sha256=source_sha256,
    )
    existing = db.scalar(select(MediaAsset).where(
        MediaAsset.user_id == user.id,
        MediaAsset.source_channel == source_channel,
        MediaAsset.upload_key == upload_key,
    ))
    if existing is not None:
        if (
            existing.kind != kind or existing.asset_role != asset_role
            or existing.size_bytes != size_bytes or existing.expected_sha256 != source_sha256
        ):
            raise MediaUploadError("上传键已绑定其他素材", status_code=409, code="MEDIA_KEY_CONFLICT")
        return _payload(_owned(db, user, existing.id))
    if source_channel.startswith("h3_"):
        # Never silently rebind a key already reserved by the active local H3
        # protocol during a mixed-client rollout.
        legacy = db.scalar(select(StagedAsset.id).where(
            StagedAsset.user_id == user.id,
            StagedAsset.source_channel == source_channel,
            StagedAsset.upload_key == upload_key,
            StagedAsset.consumed_at.is_(None),
            StagedAsset.expires_at > _now(),
        ))
        if legacy is not None:
            raise MediaUploadError(
                "该 H3 上传键已由旧上传会话使用",
                status_code=409, code="MEDIA_LEGACY_KEY_EXISTS",
            )
    used = db.scalar(select(func.coalesce(func.sum(MediaAsset.size_bytes), 0)).where(
        MediaAsset.user_id == user.id,
        MediaAsset.expires_at > _now(),
        MediaAsset.status.in_(["UPLOADING", "UPLOADED", "VERIFYING", "READY"]),
    ))
    if int(used or 0) + size_bytes > settings.max_batch_total_upload_mb * 1024 * 1024:
        raise MediaUploadError("暂存素材总量超过限制", status_code=413, code="MEDIA_QUOTA_EXCEEDED")
    asset_id = str(uuid.uuid4())
    object_key = f"media/users/{user.id}/{asset_id}/source"
    upload_id = store.initiate(object_key)
    asset = MediaAsset(
        id=asset_id, user_id=user.id, kind=kind, source_channel=source_channel,
        asset_role=asset_role, upload_key=upload_key, original_name=clean_name,
        size_bytes=size_bytes, expected_sha256=source_sha256,
        object_key=object_key, multipart_upload_id=upload_id,
        part_size=PART_SIZE, status="UPLOADING",
        created_at=_now(), updated_at=_now(),
        expires_at=_now() + timedelta(hours=settings.staged_asset_retention_hours),
    )
    db.add(asset)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            store.abort(object_key, upload_id)
        except Exception:
            LOGGER.warning("OSS 竞争上传的孤立分片清理失败", extra={"asset_id": asset_id})
        winner = db.scalar(select(MediaAsset).where(
            MediaAsset.user_id == user.id,
            MediaAsset.source_channel == source_channel,
            MediaAsset.upload_key == upload_key,
        ))
        if winner is None:
            raise
        if (
            winner.kind != kind or winner.asset_role != asset_role
            or winner.size_bytes != size_bytes or winner.expected_sha256 != source_sha256
        ):
            raise MediaUploadError("上传键已绑定其他素材", status_code=409, code="MEDIA_KEY_CONFLICT")
        return _payload(_owned(db, user, winner.id))
    return _payload(asset)


def upload_status(db: Session, user: User, store: OssMediaStore, asset_id: str) -> dict[str, object]:
    asset = _owned(db, user, asset_id)
    result = _payload(asset)
    result["uploaded_parts"] = (
        [part.number for part in store.list_parts(asset.object_key, asset.multipart_upload_id)]
        if asset.status == "UPLOADING" else []
    )
    return result


def sign_parts(
    db: Session, user: User, store: OssMediaStore, asset_id: str,
    *, start: int, count: int,
) -> dict[str, object]:
    asset = _owned(db, user, asset_id)
    if asset.status != "UPLOADING":
        raise MediaUploadError("素材已结束上传", status_code=409, code="MEDIA_NOT_UPLOADING")
    if start < 1 or count < 1 or count > 32 or start + count - 1 > _parts_count(asset):
        raise MediaUploadError("分片范围不合法")
    present = {part.number for part in store.list_parts(asset.object_key, asset.multipart_upload_id)}
    return {
        "asset_id": asset.id,
        "part_size": asset.part_size,
        "uploaded_parts": sorted(present),
        "parts": [
            {"number": n, "url": store.sign_part(asset.object_key, asset.multipart_upload_id, n)}
            for n in range(start, start + count) if n not in present
        ],
    }


def _validate_parts(asset: MediaAsset, parts: list[UploadedPart]) -> None:
    expected_count = _parts_count(asset)
    if len(parts) != expected_count:
        raise MediaUploadError("分片尚未传齐", status_code=409, code="MEDIA_PARTS_INCOMPLETE")
    for index, part in enumerate(sorted(parts, key=lambda item: item.number), start=1):
        expected_size = (
            asset.part_size if index < expected_count
            else asset.size_bytes - asset.part_size * (expected_count - 1)
        )
        if part.number != index or part.size != expected_size or not part.etag:
            raise MediaUploadError("分片编号或大小不符", status_code=422, code="MEDIA_PARTS_INVALID")


def complete_upload(db: Session, user: User, store: OssMediaStore, asset_id: str) -> dict[str, object]:
    asset = _owned(db, user, asset_id)
    if asset.status == "UPLOADED":
        enqueue(db, asset)
        db.commit()
        return _payload(asset)
    if asset.status != "UPLOADING":
        raise MediaUploadError("素材状态不允许完成上传", status_code=409, code="MEDIA_STATE_INVALID")
    try:
        parts = store.list_parts(asset.object_key, asset.multipart_upload_id)
    except Exception:
        # Complete may have reached OSS before the web process committed.
        try:
            already_complete = store.object_size(asset.object_key) == asset.size_bytes
        except Exception:
            already_complete = False
        if not already_complete:
            raise
        asset.status = "UPLOADED"
        enqueue(db, asset)
        db.commit()
        return _payload(asset)
    _validate_parts(asset, parts)
    try:
        store.complete(asset.object_key, asset.multipart_upload_id, sorted(parts, key=lambda item: item.number))
    except Exception:
        # A timed-out Complete can have succeeded. HEAD settles the ambiguity without re-uploading.
        if store.object_size(asset.object_key) != asset.size_bytes:
            raise
    if store.object_size(asset.object_key) != asset.size_bytes:
        raise MediaUploadError("OSS 对象大小不符", status_code=422, code="MEDIA_OBJECT_SIZE_MISMATCH")
    asset.status = "UPLOADED"  # Still unusable until independent content/SHA verification.
    enqueue(db, asset)
    db.commit()
    return _payload(asset)
