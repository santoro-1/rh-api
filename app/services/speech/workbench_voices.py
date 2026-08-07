from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
    VoiceCreationTask,
)
from app.services.security import decrypt_secret
from app.services.speech.accounts import (
    replicate_shared_custom_voice,
    synchronize_shared_custom_voices,
)
from app.services.speech.minimax import MiniMaxAPIError, MiniMaxClient
from app.services.speech.system_voices import sync_system_voices
from app.services.storage import to_relative_data_path, voice_creation_dir


WORKBENCH_SYSTEM_VOICES: tuple[dict[str, str], ...] = (
    {
        "voice_id": "Chinese (Mandarin)_Reliable_Executive",
        "name": "沉稳男声",
        "description": "沉稳可靠，适合知识讲解和专业内容",
    },
    {
        "voice_id": "Chinese (Mandarin)_Warm_Girl",
        "name": "温暖女声",
        "description": "自然温暖，适合故事、情感和生活内容",
    },
    {
        "voice_id": "Chinese (Mandarin)_Unrestrained_Young_Man",
        "name": "活力男声",
        "description": "年轻有活力，适合广告和快节奏短视频",
    },
)

AVAILABLE_VOICE_STATUSES = {
    VoiceAssetStatus.READY.value,
    VoiceAssetStatus.ACTIVE.value,
}


def _client(user: User, settings: Settings) -> MiniMaxClient:
    config = user.minimax_config
    if config is None or not config.api_key_encrypted:
        raise ValueError("当前账号尚未配置 MiniMax API Key")
    return MiniMaxClient(
        decrypt_secret(config.api_key_encrypted, label="MiniMax API Key"),
        base_url=config.base_url,
        timeout=settings.minimax_request_timeout_seconds,
    )


def ensure_workbench_system_voices(
    db: Session,
    user: User,
    settings: Settings,
    *,
    available_voices: Iterable[dict[str, Any]] | None = None,
) -> list[MiniMaxVoiceAsset]:
    """Ensure the three product-approved provider voices exist for this account."""

    config = user.minimax_config
    if (
        config is None
        or not config.api_key_encrypted
        or not config.account_binding_id
        or not config.credential_fingerprint
    ):
        raise ValueError("当前账号尚未配置 MiniMax API Key")
    preset_ids = [item["voice_id"] for item in WORKBENCH_SYSTEM_VOICES]
    existing = db.scalars(
        select(MiniMaxVoiceAsset).where(
            MiniMaxVoiceAsset.user_id == user.id,
            MiniMaxVoiceAsset.account_binding_id == config.account_binding_id,
            MiniMaxVoiceAsset.voice_id.in_(preset_ids),
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.status.in_(AVAILABLE_VOICE_STATUSES),
        )
    ).all()
    by_provider_id = {voice.voice_id: voice for voice in existing}
    if len(by_provider_id) != len(preset_ids):
        client = _client(user, settings)
        available = (
            list(available_voices)
            if available_voices is not None
            else client.list_voices("system")
        )
        selections = {
            item["voice_id"]: item["name"] for item in WORKBENCH_SYSTEM_VOICES
        }
        sync_system_voices(
            db,
            user,
            client,
            selections,
            available_voices=available,
        )
        db.flush()
        existing = db.scalars(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.user_id == user.id,
                MiniMaxVoiceAsset.account_binding_id == config.account_binding_id,
                MiniMaxVoiceAsset.voice_id.in_(preset_ids),
                MiniMaxVoiceAsset.is_saved.is_(True),
                MiniMaxVoiceAsset.status.in_(AVAILABLE_VOICE_STATUSES),
            )
        ).all()
        by_provider_id = {voice.voice_id: voice for voice in existing}
    missing = [voice_id for voice_id in preset_ids if voice_id not in by_provider_id]
    if missing:
        raise ValueError(
            "当前 MiniMax 账号缺少工作台所需官方音色：" + "、".join(missing)
        )
    return [by_provider_id[voice_id] for voice_id in preset_ids]


def available_workbench_voices(db: Session, user: User) -> list[MiniMaxVoiceAsset]:
    config = user.minimax_config
    if config is None or not config.account_binding_id:
        return []
    synchronize_shared_custom_voices(db, user)
    voices = db.scalars(
        select(MiniMaxVoiceAsset)
        .where(
            MiniMaxVoiceAsset.user_id == user.id,
            MiniMaxVoiceAsset.account_binding_id == config.account_binding_id,
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.status.in_(AVAILABLE_VOICE_STATUSES),
        )
        .order_by(MiniMaxVoiceAsset.created_at.desc())
    ).all()
    preset_ids = {item["voice_id"] for item in WORKBENCH_SYSTEM_VOICES}
    return [
        voice
        for voice in voices
        if voice.method != "system" or voice.voice_id in preset_ids
    ]


def voice_payload(voice: MiniMaxVoiceAsset) -> dict[str, Any]:
    preset = next(
        (
            item
            for item in WORKBENCH_SYSTEM_VOICES
            if item["voice_id"] == voice.voice_id
        ),
        None,
    )
    activated = voice.method == "system" or voice.status == VoiceAssetStatus.ACTIVE.value
    return {
        "voice_asset_id": voice.id,
        "provider_voice_id": voice.voice_id,
        "name": voice.name,
        "description": preset["description"] if preset else "用户保存的自定义声音",
        "source": "official" if voice.method == "system" else "custom",
        "method": voice.method,
        "category": voice.category,
        "status": voice.status,
        "activated": activated,
        "activation_required": voice.method != "system" and not activated,
        "selectable": activated,
        "preview_available": bool(voice.preview_relative_path),
        "created_at": voice.created_at.isoformat(),
    }


def activate_workbench_voice(
    db: Session,
    user: User,
    voice: MiniMaxVoiceAsset,
    settings: Settings,
    *,
    cost_confirmed: bool,
) -> Path | None:
    """Explicitly perform the first paid TTS use for one saved custom voice."""

    if voice.user_id != user.id or not voice.is_saved:
        raise ValueError("音色不属于当前账号")
    if voice.method == "system":
        raise ValueError("MiniMax 官方音色不需要激活")
    if voice.status == VoiceAssetStatus.ACTIVE.value:
        if not voice.preview_relative_path:
            return None
        from app.services.storage import safe_relative_path

        cached = safe_relative_path(voice.preview_relative_path, settings.data_dir)
        return cached if cached.is_file() else None
    if voice.status != VoiceAssetStatus.READY.value:
        raise ValueError("当前音色状态不能激活")
    if not cost_confirmed:
        raise ValueError("请确认激活会触发 MiniMax 音色复刻费和本次语音合成费")

    audio, _payload = _client(user, settings).synthesize_voice(
        text="你好，这是音色激活试听。",
        voice_id=voice.voice_id,
        model="speech-2.8-turbo",
        speed=1.0,
        volume=1.0,
        pitch=0,
        language_boost="Chinese",
        output_format="mp3",
    )
    directory = voice_creation_dir(settings, user.id, "activation-previews")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{voice.id}.mp3"
    temporary = target.with_suffix(".mp3.tmp")
    temporary.write_bytes(audio)
    temporary.replace(target)
    activated_at = datetime.now(timezone.utc)
    voice.preview_relative_path = to_relative_data_path(target, settings)
    voice.status = VoiceAssetStatus.ACTIVE.value
    voice.activated_at = activated_at
    voice.expires_at = activated_at + timedelta(
        hours=settings.temporary_voice_retention_hours
    )
    replicate_shared_custom_voice(db, voice)
    db.commit()
    return target


def delete_workbench_voice(
    db: Session,
    user: User,
    voice: MiniMaxVoiceAsset,
) -> None:
    """Remove one custom voice card while retaining task and audio history."""

    if voice.user_id != user.id or not voice.is_saved:
        raise ValueError("音色不属于当前账号")
    if voice.method == "system":
        raise ValueError("MiniMax 官方音色不能在声音中心删除")
    voice.is_saved = False
    db.commit()


def creation_task_payload(task: VoiceCreationTask) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "voice_asset_id": task.voice_asset_id,
        "method": task.method,
        "name": task.name,
        "status": task.status,
        "preview_available": bool(task.preview_relative_path),
        "error_code": task.error_code,
        "error_message": task.error_message,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def generate_official_voice_preview(
    db: Session,
    user: User,
    voice: MiniMaxVoiceAsset,
    settings: Settings,
    *,
    preview_text: str,
    cost_confirmed: bool,
) -> Path:
    if voice.user_id != user.id or voice.method != "system":
        raise ValueError("只能为当前账号的官方音色生成试听")
    if voice.preview_relative_path:
        from app.services.storage import safe_relative_path

        cached = safe_relative_path(voice.preview_relative_path, settings.data_dir)
        if cached.is_file():
            return cached
    if not cost_confirmed:
        raise ValueError("请先确认官方音色试听会产生 MiniMax 语音合成费用")
    text = str(preview_text or "").strip()
    if not text or len(text) > 200:
        raise ValueError("试听文案长度必须在 1–200 个字符之间")
    audio, _payload = _client(user, settings).synthesize_voice(
        text=text,
        voice_id=voice.voice_id,
        model="speech-2.8-turbo",
        speed=1.0,
        volume=1.0,
        pitch=0,
        language_boost="Chinese",
        output_format="mp3",
    )
    directory = voice_creation_dir(settings, user.id, "official-previews")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{voice.id}.mp3"
    temporary = target.with_suffix(".mp3.tmp")
    temporary.write_bytes(audio)
    temporary.replace(target)
    voice.preview_relative_path = to_relative_data_path(target, settings)
    db.commit()
    return target


__all__ = [
    "MiniMaxAPIError",
    "WORKBENCH_SYSTEM_VOICES",
    "available_workbench_voices",
    "creation_task_payload",
    "ensure_workbench_system_voices",
    "generate_official_voice_preview",
    "voice_payload",
]
