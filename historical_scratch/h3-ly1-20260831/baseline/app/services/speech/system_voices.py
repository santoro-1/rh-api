from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MiniMaxVoiceAsset, User, VoiceAssetStatus
from app.services.speech.minimax import MiniMaxClient


SYSTEM_VOICE_CATEGORY_ORDER = (
    "中文普通话",
    "中文方言",
    "英语",
    "日语",
    "韩语",
    "其他官方音色",
)


def system_voice_category(item: Mapping[str, Any]) -> str:
    """Infer a stable display group from MiniMax's public voice metadata."""

    description = item.get("description") or []
    if isinstance(description, str):
        description_text = description
    elif isinstance(description, Sequence):
        description_text = " ".join(str(part) for part in description)
    else:
        description_text = str(description)
    text = " ".join(
        (
            str(item.get("voice_id") or ""),
            str(item.get("voice_name") or item.get("name") or ""),
            description_text,
        )
    ).casefold()

    dialect_markers = (
        "cantonese",
        "sichuan",
        "sichuanese",
        "dongbei",
        "shaanxi",
        "minnan",
        "wenzhou",
        "粤语",
        "广东话",
        "四川",
        "东北",
        "陕西",
        "闽南",
        "温州",
        "方言",
    )
    if any(marker in text for marker in dialect_markers):
        return "中文方言"
    if any(marker in text for marker in ("japanese", "日本語", "日语")):
        return "日语"
    if any(marker in text for marker in ("korean", "한국어", "韩语")):
        return "韩语"
    if any(
        marker in text
        for marker in (
            "english",
            "英语",
            "british",
            "american",
            "aussie",
            "santa_claus",
        )
    ):
        return "英语"
    if any(
        marker in text
        for marker in (
            "chinese",
            "mandarin",
            "普通话",
            "中文",
        )
    ) or text.startswith(
        (
            "male-",
            "female-",
            "presenter_",
            "audiobook_",
        )
    ):
        return "中文普通话"
    return "其他官方音色"


def group_system_voice_assets(
    voices: Iterable[MiniMaxVoiceAsset],
) -> list[dict[str, object]]:
    """Group system assets in a predictable order for templates/selects."""

    grouped: dict[str, list[MiniMaxVoiceAsset]] = {
        category: [] for category in SYSTEM_VOICE_CATEGORY_ORDER
    }
    for voice in voices:
        category = voice.category or system_voice_category(
            {"voice_id": voice.voice_id, "voice_name": voice.name}
        )
        grouped.setdefault(category, []).append(voice)
    return [
        {
            "label": category,
            "voices": sorted(
                grouped[category],
                key=lambda voice: (voice.name.casefold(), voice.voice_id.casefold()),
            ),
        }
        for category in SYSTEM_VOICE_CATEGORY_ORDER
        if grouped[category]
    ]


def group_available_voice_assets(
    voices: Iterable[MiniMaxVoiceAsset],
) -> list[dict[str, object]]:
    """Return custom voices first, followed by grouped provider voices."""

    voice_list = list(voices)
    custom = sorted(
        (voice for voice in voice_list if voice.method != "system"),
        key=lambda voice: (voice.name.casefold(), voice.voice_id.casefold()),
    )
    groups: list[dict[str, object]] = []
    if custom:
        groups.append({"label": "我的自定义音色", "voices": custom})
    groups.extend(
        group_system_voice_assets(
            voice for voice in voice_list if voice.method == "system"
        )
    )
    return groups


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
    available: dict[str, dict[str, Any]] = {}
    for item in available_items:
        voice_id = str(item.get("voice_id") or "").strip()
        if voice_id:
            # MiniMax's system catalogue currently contains at least one
            # provider-owned ID with trailing whitespace.  The admin route
            # already strips IDs before saving them, so validation must use
            # the same canonical representation.
            available[voice_id] = item
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
        provider_item = available[voice_id]
        if voice is None:
            voice = MiniMaxVoiceAsset(
                id=str(uuid.uuid4()),
                user_id=user.id,
                config_id=config.id,
                voice_id=voice_id,
            )
            db.add(voice)
        voice.category = system_voice_category(provider_item)
        if voice.status == VoiceAssetStatus.HIDDEN.value:
            # An administrator explicitly removed this provider voice from the
            # local account. Keep the tombstone so a later full sync does not
            # silently make it selectable again.
            continue
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
