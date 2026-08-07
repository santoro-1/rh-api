from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import (
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.services.logging_config import log_event
from app.services.security import decrypt_secret
from app.services.speech.accounts import replicate_shared_custom_voice
from app.services.speech.minimax import MiniMaxAPIError, MiniMaxClient
from app.services.storage import safe_relative_path, to_relative_data_path


logger = logging.getLogger(__name__)

# These states may sit across a paid provider call. On restart we do not repeat
# them automatically because a missing local response does not prove the
# provider call failed or was free.
ACTIVE_VOICE_CREATION_STATUSES = {
    VoiceCreationStatus.CLONING.value,
    VoiceCreationStatus.SYNTHESIZING.value,
    VoiceCreationStatus.SAVING.value,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def recover_interrupted_voice_tasks(db: Session) -> int:
    """Require explicit recreation after an ambiguous paid voice operation."""

    tasks = db.scalars(
        select(VoiceCreationTask).where(
            VoiceCreationTask.status.in_(ACTIVE_VOICE_CREATION_STATUSES)
        )
    ).all()
    for task in tasks:
        task.status = VoiceCreationStatus.FAILED.value
        task.error_code = "INTERRUPTED_REVIEW"
        task.error_message = (
            "声音制作在外部调用阶段中断；为避免重复付费，"
            "系统没有自动重试，请重新创建声音制作任务"
        )
        task.completed_at = _now()
    db.commit()
    return len(tasks)


def claim_next_voice_task(db: Session) -> str | None:
    """Atomically claim the oldest preview/save job from the voice FIFO."""

    row = db.execute(
        select(VoiceCreationTask.id, VoiceCreationTask.status)
        .where(
            VoiceCreationTask.status.in_(
                {
                    VoiceCreationStatus.PENDING.value,
                    VoiceCreationStatus.SAVE_PENDING.value,
                }
            )
        )
        .order_by(VoiceCreationTask.created_at)
        .limit(1)
    ).first()
    if row is None:
        return None
    task_id, current_status = row
    claimed_status = (
        VoiceCreationStatus.SAVING.value
        if current_status == VoiceCreationStatus.SAVE_PENDING.value
        else VoiceCreationStatus.CLONING.value
    )
    result = db.execute(
        update(VoiceCreationTask)
        .where(
            VoiceCreationTask.id == task_id,
            VoiceCreationTask.status == current_status,
        )
        .values(
            status=claimed_status,
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    if result.rowcount == 1:
        log_event(
            logger,
            "voice.claimed",
            "语音 Worker 已领取声音制作任务",
            task_id=task_id,
            status=claimed_status,
        )
        return task_id
    return None


def _make_client(task: VoiceCreationTask) -> MiniMaxClient:
    return MiniMaxClient(
        decrypt_secret(task.config.api_key_encrypted),
        base_url=task.config.base_url,
        timeout=get_settings().minimax_request_timeout_seconds,
    )


def _load_voice_task(
    db: Session,
    task_id: str,
) -> VoiceCreationTask | None:
    return db.scalar(
        select(VoiceCreationTask)
        .options(
            selectinload(VoiceCreationTask.user).selectinload(
                User.minimax_config
            ),
            selectinload(VoiceCreationTask.config),
            selectinload(VoiceCreationTask.voice_asset),
        )
        .where(VoiceCreationTask.id == task_id)
    )


def _voice_task_is_configured(task: VoiceCreationTask) -> bool:
    """Confirm the queued job still belongs to the user's active MiniMax account."""

    return bool(
        task.user.is_active
        and task.user.minimax_config is not None
        and task.user.minimax_config.api_key_encrypted
        and task.config_id == task.user.minimax_config.id
        and task.account_binding_id
        == task.user.minimax_config.account_binding_id
    )


def _mark_voice_task_failed(
    db: Session,
    task: VoiceCreationTask,
    code: str,
    message: str,
) -> None:
    task.status = VoiceCreationStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "voice.failed",
        "声音制作任务失败",
        level=logging.WARNING,
        task_id=task.id,
        method=task.method,
        error_code=code,
        error=message,
    )


def _write_voice_preview(
    task: VoiceCreationTask,
    audio_bytes: bytes,
) -> Path:
    directory = safe_relative_path(
        task.source_a_relative_path,
        get_settings().data_dir,
    ).parent
    target = directory / "preview.mp3"
    temporary = target.with_suffix(".mp3.part")
    temporary.write_bytes(audio_bytes)
    os.replace(temporary, target)
    return target


def _new_voice_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _upload_creation_source(
    db: Session,
    task: VoiceCreationTask,
    client: MiniMaxClient,
    label: str,
) -> tuple[int, Path]:
    path_value = (
        task.source_a_relative_path
        if label == "A"
        else task.source_b_relative_path
    )
    if not path_value:
        raise ValueError(f"声音 {label} 文件不存在")
    path = safe_relative_path(path_value, get_settings().data_dir)
    file_id_value = (
        task.source_a_file_id if label == "A" else task.source_b_file_id
    )
    if not file_id_value:
        file_id_value = str(client.upload_clone_audio(path))
        if label == "A":
            task.source_a_file_id = file_id_value
        else:
            task.source_b_file_id = file_id_value
        db.commit()
    return int(file_id_value), path


def _respect_t2a_rate_limit(task: VoiceCreationTask) -> None:
    """Apply the account-wide MiniMax requests-per-minute interval."""

    last_request = task.config.last_t2a_at
    if last_request is None:
        return
    interval = 60 / max(task.config.requests_per_minute, 1)
    elapsed = (_now() - _as_utc(last_request)).total_seconds()
    if elapsed < interval:
        time.sleep(interval - elapsed)


def _process_clone_preview(
    db: Session,
    task: VoiceCreationTask,
    client: MiniMaxClient,
) -> None:
    task.status = VoiceCreationStatus.CLONING.value
    db.commit()
    file_id, _path = _upload_creation_source(db, task, client, "A")
    if not task.temporary_voice_a_id:
        task.temporary_voice_a_id = _new_voice_id("VoiceStudioClone")
        task.final_voice_id = task.temporary_voice_a_id
        db.commit()
    _voice_id, audio_bytes, _content_type = client.clone_voice_with_preview(
        file_id,
        task.temporary_voice_a_id,
        preview_text=task.preview_text,
        model=task.model,
        noise_reduction=task.noise_reduction,
        volume_normalization=task.volume_normalization,
    )
    preview = _write_voice_preview(task, audio_bytes)
    task.preview_relative_path = to_relative_data_path(
        preview, get_settings()
    )
    task.status = VoiceCreationStatus.PREVIEW_READY.value
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "voice.preview_ready",
        "克隆声音试听已生成",
        task_id=task.id,
        method=task.method,
    )


def _process_mix_preview(
    db: Session,
    task: VoiceCreationTask,
    client: MiniMaxClient,
) -> None:
    task.status = VoiceCreationStatus.CLONING.value
    db.commit()
    file_a, _path_a = _upload_creation_source(db, task, client, "A")
    file_b, _path_b = _upload_creation_source(db, task, client, "B")
    if not task.temporary_voice_a_id:
        task.temporary_voice_a_id = _new_voice_id("VoiceStudioMixA")
    if not task.temporary_voice_b_id:
        task.temporary_voice_b_id = _new_voice_id("VoiceStudioMixB")
    db.commit()
    client.clone_voice(
        file_a,
        task.temporary_voice_a_id,
        noise_reduction=task.noise_reduction,
        volume_normalization=task.volume_normalization,
    )
    client.clone_voice(
        file_b,
        task.temporary_voice_b_id,
        noise_reduction=task.noise_reduction,
        volume_normalization=task.volume_normalization,
    )

    _respect_t2a_rate_limit(task)
    task.status = VoiceCreationStatus.SYNTHESIZING.value
    task.config.last_t2a_at = _now()
    db.commit()
    audio_bytes, _payload = client.synthesize_blended_voice(
        text=task.preview_text,
        voice_id_a=task.temporary_voice_a_id,
        voice_id_b=task.temporary_voice_b_id,
        weight_a=task.weight_a or 50,
        weight_b=task.weight_b or 50,
        model=task.model,
        speed=1,
        volume=1,
        pitch=0,
        language_boost="auto",
        output_format="mp3",
    )
    preview = _write_voice_preview(task, audio_bytes)
    task.preview_relative_path = to_relative_data_path(
        preview, get_settings()
    )
    task.status = VoiceCreationStatus.PREVIEW_READY.value
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "voice.preview_ready",
        "融合声音试听已生成",
        task_id=task.id,
        method=task.method,
    )


def _save_voice_asset(
    db: Session,
    task: VoiceCreationTask,
    client: MiniMaxClient,
) -> None:
    """Persist only the user-approved final voice in the reusable voice library."""

    if task.voice_asset is not None:
        task.status = VoiceCreationStatus.SAVED.value
        task.completed_at = task.completed_at or _now()
        db.commit()
        return
    if not task.preview_relative_path:
        raise ValueError("试听音频不存在，不能保存")
    task.status = VoiceCreationStatus.SAVING.value
    db.commit()

    if task.method == "mix":
        preview = safe_relative_path(
            task.preview_relative_path,
            get_settings().data_dir,
        )
        if not task.final_file_id:
            task.final_file_id = str(client.upload_clone_audio(preview))
            db.commit()
        if not task.final_voice_id:
            task.final_voice_id = _new_voice_id("VoiceStudioFinal")
            db.commit()
        client.clone_voice(
            int(task.final_file_id),
            task.final_voice_id,
            noise_reduction=task.noise_reduction,
            volume_normalization=task.volume_normalization,
        )
    if not task.final_voice_id:
        raise ValueError("最终 voice_id 不存在")

    expires_at = _now() + timedelta(
        hours=get_settings().temporary_voice_retention_hours
    )
    voice = MiniMaxVoiceAsset(
        id=str(uuid.uuid4()),
        user_id=task.user_id,
        config_id=task.config_id,
        name=task.name,
        voice_id=task.final_voice_id,
        account_binding_id=task.account_binding_id,
        credential_fingerprint=task.credential_fingerprint,
        status=VoiceAssetStatus.READY.value,
        method=task.method,
        is_saved=True,
        source_relative_path=task.source_a_relative_path,
        source_original_name=task.source_a_original_name,
        preview_relative_path=task.preview_relative_path,
        expires_at=expires_at,
    )
    db.add(voice)
    db.flush()
    replicate_shared_custom_voice(db, voice)
    task.voice_asset_id = voice.id
    task.status = VoiceCreationStatus.SAVED.value
    task.completed_at = _now()
    db.commit()
    log_event(
        logger,
        "voice.saved",
        "声音已保存到用户音色库",
        task_id=task.id,
        voice_asset_id=voice.id,
        method=task.method,
    )


def process_voice_task(db: Session, task_id: str) -> None:
    """Run one clone/blend preview or save operation to a terminal local state."""

    task = _load_voice_task(db, task_id)
    if task is None or task.status in {
        VoiceCreationStatus.PREVIEW_READY.value,
        VoiceCreationStatus.SAVED.value,
        VoiceCreationStatus.EXPIRED.value,
        VoiceCreationStatus.FAILED.value,
    }:
        return
    if not _voice_task_is_configured(task):
        _mark_voice_task_failed(
            db,
            task,
            "CONFIGURATION_ERROR",
            "MiniMax 账号配置缺失、已禁用或已更换",
        )
        return
    try:
        client = _make_client(task)
        if task.status in {
            VoiceCreationStatus.SAVE_PENDING.value,
            VoiceCreationStatus.SAVING.value,
        }:
            _save_voice_asset(db, task, client)
        elif task.method == "clone":
            _process_clone_preview(db, task, client)
        else:
            _process_mix_preview(db, task, client)
    except (MiniMaxAPIError, OSError, ValueError) as exc:
        _mark_voice_task_failed(
            db,
            task,
            "VOICE_CREATION_FAILED",
            str(exc),
        )
