from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.speech.minimax import MiniMaxClient


def sync_system_voices(
    db: Session,
    user: User,
    client: MiniMaxClient,
    selections: dict[str, str],
    *,
    available_voices: Iterable[dict[str, Any]] | None = None,
) -> list[MiniMaxVoiceAsset]:
    """Verify and save selected provider-owned voices without cloning them."""

    config = user.minimax_config
    if (
        config is None
        or not config.api_key_encrypted
        or not config.account_binding_id
        or not config.credential_fingerprint
    ):
        raise ValueError("当前账号尚未配置 MiniMax API Key")

    available_items = (
        list(available_voices)
        if available_voices is not None
        else client.list_voices("system")
    )
    available = {
        str(item["voice_id"]): item
        for item in available_items
        if item.get("voice_id")
    }
    missing = [voice_id for voice_id in selections if voice_id not in available]
    if missing:
        raise ValueError(f"MiniMax 账号中找不到官方音色：{', '.join(missing)}")

    saved: list[MiniMaxVoiceAsset] = []
    for voice_id, local_name in selections.items():
        clean_name = local_name.strip()
        if not clean_name or len(clean_name) > 100:
            raise ValueError("官方音色名称长度必须在 1–100 个字符之间")
        voice = db.scalar(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.config_id == config.id,
                MiniMaxVoiceAsset.voice_id == voice_id,
            )
        )
        if voice is None:
            voice = MiniMaxVoiceAsset(
                id=str(uuid.uuid4()),
                user_id=user.id,
                config_id=config.id,
                voice_id=voice_id,
            )
            db.add(voice)
        voice.name = clean_name
        voice.account_binding_id = config.account_binding_id
        voice.credential_fingerprint = config.credential_fingerprint
        voice.status = VoiceAssetStatus.ACTIVE.value
        voice.method = "system"
        voice.is_saved = True
        voice.source_relative_path = None
        voice.source_original_name = None
        voice.remote_file_id = None
        voice.preview_relative_path = None
        voice.activated_at = voice.activated_at or datetime.now(timezone.utc)
        voice.expires_at = None
        saved.append(voice)
    return saved
