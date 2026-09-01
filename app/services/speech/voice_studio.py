from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    BATCH_SOURCE_LEGACY_WEB,
    MiniMaxVoiceAsset,
    User,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.services.audio import inspect_audio_duration
from app.services.speech.minimax import validate_clone_audio
from app.services.storage import (
    remove_directory,
    save_upload,
    to_relative_data_path,
    voice_creation_dir,
)


def _validate_reference(path: Path, label: str) -> None:
    validate_clone_audio(path)
    duration = inspect_audio_duration(path)
    if duration < 10:
        raise ValueError(f"{label}不能少于 10 秒")
    if duration > 300:
        raise ValueError(f"{label}不能超过 5 分钟")


def create_voice_task(
    db: Session,
    user: User,
    settings: Settings,
    *,
    method: str,
    name: str,
    preview_text: str,
    model: str,
    source_a: UploadFile,
    source_b: UploadFile | None,
    weight_a: int | None,
    noise_reduction: bool,
    volume_normalization: bool,
    cost_confirmed: bool,
    source_channel: str = BATCH_SOURCE_LEGACY_WEB,
    task_id: str | None = None,
) -> VoiceCreationTask:
    """Validate uploads and persist one clone or blend audition request."""

    config = user.minimax_config
    if (
        config is None
        or not config.api_key_encrypted
        or not config.account_binding_id
        or not config.credential_fingerprint
    ):
        raise ValueError("当前账号尚未配置 MiniMax API Key")
    method = method.strip().lower()
    if method not in {"clone", "mix"}:
        raise ValueError("声音制作方式只能为克隆或融合")
    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 100:
        raise ValueError("音色名称需为 1–100 个字符")
    if db.scalar(
        select(MiniMaxVoiceAsset.id).where(
            MiniMaxVoiceAsset.user_id == user.id,
            MiniMaxVoiceAsset.account_binding_id
            == config.account_binding_id,
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.name == clean_name,
        )
    ):
        raise ValueError("音色名称已存在，请换一个名称")
    clean_text = preview_text.strip()
    if not clean_text:
        raise ValueError("试听文案不能为空")
    if len(clean_text) > 1000:
        raise ValueError("试听文案不能超过 1000 个字符")
    if not model.strip() or len(model.strip()) > 100:
        raise ValueError("语音模型名称不合法")
    if not cost_confirmed:
        raise ValueError("请先确认声音制作和试听可能产生费用")
    if method == "clone" and source_b is not None:
        raise ValueError("声音克隆只需要一份声音母带")
    if method == "mix":
        if source_b is None:
            raise ValueError("音色融合需要 A、B 两份声音")
        if weight_a is None or not 1 <= weight_a <= 99:
            raise ValueError("音色 A 权重必须是 1–99 的整数")

    task_id = task_id or str(uuid.uuid4())
    directory = voice_creation_dir(settings, user.id, task_id)
    try:
        path_a, original_a = save_upload(
            source_a, directory, "audio", settings
        )
        _validate_reference(path_a, "声音 A")
        path_b: Path | None = None
        original_b: str | None = None
        if source_b is not None:
            path_b, original_b = save_upload(
                source_b, directory, "audio", settings
            )
            _validate_reference(path_b, "声音 B")

        now = datetime.now(timezone.utc)
        task = VoiceCreationTask(
            id=task_id,
            user_id=user.id,
            source_channel=source_channel,
            config_id=config.id,
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            method=method,
            name=clean_name,
            preview_text=clean_text,
            model=model.strip(),
            weight_a=weight_a if method == "mix" else None,
            weight_b=(100 - weight_a) if method == "mix" and weight_a else None,
            noise_reduction=noise_reduction,
            volume_normalization=volume_normalization,
            source_a_relative_path=to_relative_data_path(path_a, settings),
            source_a_original_name=original_a,
            source_b_relative_path=(
                to_relative_data_path(path_b, settings) if path_b else None
            ),
            source_b_original_name=original_b,
            status=VoiceCreationStatus.PENDING.value,
            cost_confirmed_at=now,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task
    except Exception:
        db.rollback()
        remove_directory(directory)
        raise


def request_voice_save(
    db: Session,
    task: VoiceCreationTask,
) -> None:
    if task.status == VoiceCreationStatus.SAVED.value:
        return
    if task.status != VoiceCreationStatus.PREVIEW_READY.value:
        raise ValueError("只有试听生成成功后才能保存为可复用音色")
    task.status = VoiceCreationStatus.SAVE_PENDING.value
    task.save_requested_at = datetime.now(timezone.utc)
    task.error_code = None
    task.error_message = None
    db.commit()
