from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import MiniMaxConfig, MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.security import encrypt_secret, secret_fingerprint


def credential_fingerprint(api_key: str) -> str:
    """Identify an API account without storing or displaying its secret."""

    return secret_fingerprint(api_key)


def validate_minimax_config(
    base_url: str,
    requests_per_minute: int,
) -> None:
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("MiniMax Base URL 必须以 http:// 或 https:// 开头")
    if not 1 <= requests_per_minute <= 60:
        raise ValueError("MiniMax 每分钟请求数必须在 1 到 60 之间")


_SHARED_CUSTOM_VOICE_STATUSES = {
    VoiceAssetStatus.READY.value,
    VoiceAssetStatus.ACTIVE.value,
}


def _utc_timestamp(value: datetime) -> float:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return normalized.timestamp()


def _merge_shared_voice_state(
    target: MiniMaxVoiceAsset,
    source: MiniMaxVoiceAsset,
) -> bool:
    """Copy account-wide activation facts without undoing a local deletion."""

    if not target.is_saved:
        return False
    changed = False
    if (
        source.status == VoiceAssetStatus.ACTIVE.value
        and target.status != VoiceAssetStatus.ACTIVE.value
    ):
        target.status = VoiceAssetStatus.ACTIVE.value
        changed = True
    for field in ("activated_at", "preview_relative_path"):
        if getattr(target, field) is None and getattr(source, field) is not None:
            setattr(target, field, getattr(source, field))
            changed = True
    if source.expires_at is not None and (
        target.expires_at is None
        or _utc_timestamp(source.expires_at) > _utc_timestamp(target.expires_at)
    ):
        target.expires_at = source.expires_at
        changed = True
    return changed


def _copy_shared_custom_voice(
    db: Session,
    source: MiniMaxVoiceAsset,
    config: MiniMaxConfig,
    existing: MiniMaxVoiceAsset | None,
) -> tuple[MiniMaxVoiceAsset, bool]:
    if existing is not None:
        return existing, _merge_shared_voice_state(existing, source)
    voice = MiniMaxVoiceAsset(
        id=str(uuid.uuid4()),
        user_id=config.user_id,
        config_id=config.id,
        name=source.name,
        voice_id=source.voice_id,
        account_binding_id=config.account_binding_id,
        credential_fingerprint=(
            config.credential_fingerprint or source.credential_fingerprint
        ),
        status=source.status,
        method=source.method,
        category=source.category,
        is_saved=True,
        source_relative_path=source.source_relative_path,
        source_original_name=source.source_original_name,
        remote_file_id=source.remote_file_id,
        preview_relative_path=source.preview_relative_path,
        activated_at=source.activated_at,
        expires_at=source.expires_at,
    )
    try:
        with db.begin_nested():
            db.add(voice)
            db.flush()
        return voice, True
    except IntegrityError:
        concurrent = db.scalar(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.config_id == config.id,
                MiniMaxVoiceAsset.voice_id == source.voice_id,
            )
        )
        if concurrent is None:
            raise
        return concurrent, _merge_shared_voice_state(concurrent, source)


def synchronize_shared_custom_voices(
    db: Session,
    user: User,
) -> int:
    """Materialize provider-account voices into one user's local library.

    Local rows keep user actions isolated, while the credential fingerprint makes
    the underlying MiniMax custom voice reusable by website users holding the
    exact same API key.
    """

    config = user.minimax_config
    if config is None or not config.credential_fingerprint:
        return 0
    sources = db.scalars(
        select(MiniMaxVoiceAsset)
        .where(
            MiniMaxVoiceAsset.credential_fingerprint
            == config.credential_fingerprint,
            MiniMaxVoiceAsset.method != "system",
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.status.in_(_SHARED_CUSTOM_VOICE_STATUSES),
        )
        .order_by(MiniMaxVoiceAsset.created_at, MiniMaxVoiceAsset.id)
    ).all()
    local_by_provider_id = {
        voice.voice_id: voice
        for voice in db.scalars(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.config_id == config.id,
                MiniMaxVoiceAsset.method != "system",
            )
        ).all()
    }
    changed = 0
    for source in sources:
        local = local_by_provider_id.get(source.voice_id)
        local, did_change = _copy_shared_custom_voice(db, source, config, local)
        local_by_provider_id[source.voice_id] = local
        changed += int(did_change)
    if changed:
        db.flush()
    return changed


def replicate_shared_custom_voice(
    db: Session,
    source: MiniMaxVoiceAsset,
) -> int:
    """Propagate one saved custom voice and its activation state to peers."""

    if (
        source.method == "system"
        or not source.is_saved
        or source.status not in _SHARED_CUSTOM_VOICE_STATUSES
    ):
        return 0
    configs = db.scalars(
        select(MiniMaxConfig).where(
            MiniMaxConfig.credential_fingerprint
            == source.credential_fingerprint
        )
    ).all()
    existing_by_config = {
        voice.config_id: voice
        for voice in db.scalars(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.voice_id == source.voice_id,
                MiniMaxVoiceAsset.config_id.in_([config.id for config in configs]),
            )
        ).all()
    }
    changed = 0
    for config in configs:
        local, did_change = _copy_shared_custom_voice(
            db,
            source,
            config,
            existing_by_config.get(config.id),
        )
        existing_by_config[config.id] = local
        changed += int(did_change)
    if changed:
        db.flush()
    return changed


def save_minimax_config(
    db: Session,
    user: User,
    *,
    api_key: str,
    base_url: str,
    requests_per_minute: int,
    account_label: str = "MiniMax 账号",
    start_new_account_binding: bool = False,
) -> MiniMaxConfig:
    """Save a credential while preserving a stable provider-account binding."""

    clean_url = base_url.strip().rstrip("/")
    validate_minimax_config(
        clean_url,
        requests_per_minute,
    )
    config = user.minimax_config
    clean_api_key = api_key.strip()
    new_fingerprint = (
        credential_fingerprint(clean_api_key) if clean_api_key else None
    )
    if config is None:
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=None,
            account_binding_id=str(uuid.uuid4()),
            account_label=account_label.strip() or "MiniMax 账号",
            base_url=clean_url,
            requests_per_minute=requests_per_minute,
        )
    else:
        config.base_url = clean_url
        config.requests_per_minute = requests_per_minute
        config.account_label = account_label.strip() or "MiniMax 账号"
        if start_new_account_binding:
            if not clean_api_key:
                raise ValueError("切换到新的 MiniMax 账号时必须填写新的 API Key")
            config.account_binding_id = str(uuid.uuid4())
    if clean_api_key:
        config.api_key_encrypted = encrypt_secret(clean_api_key)
        config.credential_fingerprint = new_fingerprint
    db.add(config)
    db.flush()
    if clean_api_key and not start_new_account_binding:
        for voice in db.scalars(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.config_id == config.id,
                MiniMaxVoiceAsset.account_binding_id
                == config.account_binding_id,
            )
        ).all():
            voice.credential_fingerprint = new_fingerprint
    synchronize_shared_custom_voices(db, user)
    return config
