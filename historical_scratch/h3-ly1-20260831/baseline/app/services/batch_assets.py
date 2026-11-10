from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import StagedAsset, User
from app.services.storage import (
    remove_directory,
    save_upload,
    staged_asset_dir,
    to_relative_data_path,
)


class StagedAssetError(ValueError):
    """A staged upload cannot be accepted or reused."""


def stage_asset(
    db: Session,
    user: User,
    upload: UploadFile,
    kind: str,
    settings: Settings,
) -> StagedAsset:
    """Validate one file independently so large batches avoid one huge request."""

    if kind not in {"image", "audio", "video"}:
        raise StagedAssetError("素材类型不合法")

    asset_id = str(uuid.uuid4())
    directory = staged_asset_dir(settings, user.id, asset_id)
    try:
        path, original_name = save_upload(upload, directory, kind, settings)
        size_bytes = path.stat().st_size
        current_bytes = db.scalar(
            select(func.coalesce(func.sum(StagedAsset.size_bytes), 0)).where(
                StagedAsset.user_id == user.id,
                StagedAsset.consumed_at.is_(None),
                StagedAsset.expires_at > datetime.now(timezone.utc),
            )
        )
        maximum = settings.max_batch_total_upload_mb * 1024 * 1024
        if int(current_bytes or 0) + size_bytes > maximum:
            raise StagedAssetError(
                f"当前批量暂存素材总量不能超过 "
                f"{settings.max_batch_total_upload_mb} MB"
            )
        now = datetime.now(timezone.utc)
        asset = StagedAsset(
            id=asset_id,
            user_id=user.id,
            kind=kind,
            relative_path=to_relative_data_path(path, settings),
            original_name=original_name,
            size_bytes=size_bytes,
            created_at=now,
            expires_at=now
            + timedelta(hours=settings.staged_asset_retention_hours),
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset
    except Exception:
        db.rollback()
        remove_directory(directory)
        raise


def load_available_assets(
    db: Session,
    user: User,
    asset_ids: list[str],
) -> list[StagedAsset]:
    """Resolve only unexpired assets owned by the current user."""

    unique_ids = list(dict.fromkeys(asset_ids))
    if not unique_ids:
        raise StagedAssetError("请先上传批量素材")
    assets = db.scalars(
        select(StagedAsset).where(
            StagedAsset.id.in_(unique_ids),
            StagedAsset.user_id == user.id,
            StagedAsset.consumed_at.is_(None),
            StagedAsset.expires_at > datetime.now(timezone.utc),
        )
    ).all()
    if len(assets) != len(unique_ids):
        raise StagedAssetError("部分暂存素材不存在、已过期或已被使用")
    assets_by_id = {asset.id: asset for asset in assets}
    # SQL does not preserve IN-clause order. Full-flow script rows deliberately
    # pair with the visible upload order, so restore the request order here.
    return [assets_by_id[asset_id] for asset_id in unique_ids]
