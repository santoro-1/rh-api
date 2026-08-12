from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatchItem,
    GenerationSegment,
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
)
from app.services.audio import format_duration_timecode, inspect_audio_duration
from app.services.logging_config import (
    configure_logging,
    log_event,
    start_heartbeat,
    write_heartbeat,
)
from app.services.media_segmentation import (
    DIGITAL_HUMAN_MAX_SEGMENT_SECONDS,
    MediaSegmentationError,
    build_segment_plan,
    cut_audio_segment,
    cut_video_segment,
    inspect_media_duration,
    plan_timestamped_segments,
)
from app.services.security import decrypt_secret
from app.services.speech.accounts import replicate_shared_custom_voice
from app.services.speech.async_outputs import (
    decode_async_speech_output,
    dump_subtitle_cues,
    load_subtitle_cues,
)
from app.services.speech.minimax import (
    MiniMaxAPIError,
    MiniMaxClient,
    parse_pronunciation_tones,
)
from app.services.speech.voice_jobs import (
    claim_next_voice_task,
    process_voice_task,
    recover_interrupted_voice_tasks,
)
from app.services.storage import safe_relative_path, to_relative_data_path
from app.services.task_creation import create_generation_task, validate_task_input
from app.services.video_merge import merge_status_after_handoff
from app.workflows.base import WorkflowAsset


logger = logging.getLogger(__name__)
# Restart recovery treats these as ambiguous in-flight TTS states. PENDING and
# AWAITING_REVIEW are intentionally excluded: neither should be marked failed.
ACTIVE_AUDIO_STATUSES = {
    AudioTaskStatus.CLONING.value,
    AudioTaskStatus.SYNTHESIZING.value,
    AudioTaskStatus.REMOTE_PENDING.value,
    AudioTaskStatus.ALIGNING.value,
    AudioTaskStatus.SEGMENTING.value,
    AudioTaskStatus.HANDOFF.value,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _audio_log_context(task: AudioGenerationTask) -> dict[str, object]:
    batch = task.batch_item.batch if task.batch_item else None
    return {
        "user_id": task.user_id,
        "username": task.user.username if task.user else None,
        "batch_id": batch.id if batch else None,
        "source_channel": batch.source_channel if batch else None,
        "correlation_id": (batch.correlation_id or batch.id) if batch else None,
        "batch_item_id": task.batch_item_id,
        "task_id": task.id,
    }


def _mark_failed(
    db: Session,
    task: AudioGenerationTask,
    code: str,
    message: str,
) -> None:
    """Persist a terminal TTS failure and expose it to the admin event log."""

    task.status = AudioTaskStatus.FAILED.value
    task.error_code = code
    task.error_message = message
    task.completed_at = _now()
    task.batch_item.audio_status = "FAILED"
    task.batch_item.status = "AUDIO_FAILED"
    db.commit()
    log_event(
        logger,
        "audio.failed",
        "语音任务失败",
        level=logging.WARNING,
        error_code=code,
        error=message,
        **_audio_log_context(task),
    )


def recover_interrupted_tasks(db: Session) -> int:
    """Stop ambiguous paid work for explicit review instead of auto-repeating it."""

    tasks = db.scalars(
        select(AudioGenerationTask)
        .options(
            selectinload(AudioGenerationTask.batch_item).selectinload(
                GenerationBatchItem.segments
            )
        )
        .where(AudioGenerationTask.status.in_(ACTIVE_AUDIO_STATUSES))
    ).all()
    recovered = 0
    for task in tasks:
        if task.status == AudioTaskStatus.REMOTE_PENDING.value:
            # The provider task ID is durable, so normal polling can resume.
            continue
        if (
            task.batch_item.generation_task is not None
            or task.batch_item.segments
        ):
            task.status = AudioTaskStatus.SUCCESS.value
            task.error_code = None
            task.error_message = None
            task.completed_at = task.completed_at or _now()
        elif task.provider_task_id:
            task.status = AudioTaskStatus.REMOTE_PENDING.value
            task.error_code = None
            task.error_message = None
            task.completed_at = None
            task.batch_item.audio_status = "REMOTE_PENDING"
            task.batch_item.status = "GENERATING_AUDIO"
        else:
            task.status = AudioTaskStatus.FAILED.value
            task.error_code = "INTERRUPTED_REVIEW"
            task.error_message = (
                "语音任务在外部调用阶段中断；已保留原 voice_id，"
                "请在批次页面确认后重试"
            )
            task.completed_at = _now()
            task.batch_item.audio_status = "FAILED"
            task.batch_item.status = "AUDIO_FAILED"
        recovered += 1
    db.commit()
    return recovered


def claim_next_pending_task(db: Session) -> str | None:
    task_id = db.scalar(
        select(AudioGenerationTask.id)
        .where(AudioGenerationTask.status == AudioTaskStatus.PENDING.value)
        .order_by(AudioGenerationTask.created_at)
        .limit(1)
    )
    if task_id is None:
        return None
    result = db.execute(
        update(AudioGenerationTask)
        .where(
            AudioGenerationTask.id == task_id,
            AudioGenerationTask.status == AudioTaskStatus.PENDING.value,
        )
        .values(
            status=AudioTaskStatus.CLONING.value,
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    if result.rowcount == 1:
        task = _load_task(db, task_id)
        log_event(
            logger,
            "audio.claimed",
            "语音 Worker 已领取脚本任务",
            **(_audio_log_context(task) if task is not None else {"task_id": task_id}),
        )
        return task_id
    return None


def _load_task(db: Session, task_id: str) -> AudioGenerationTask | None:
    return db.scalar(
        select(AudioGenerationTask)
        .options(
            selectinload(AudioGenerationTask.user).selectinload(
                User.minimax_config
            ),
            selectinload(AudioGenerationTask.user).selectinload(
                User.runninghub_config
            ),
            selectinload(AudioGenerationTask.user).selectinload(
                User.workflow_configs
            ),
            selectinload(AudioGenerationTask.voice_a),
            selectinload(AudioGenerationTask.voice_b),
            selectinload(AudioGenerationTask.voice_asset),
            selectinload(AudioGenerationTask.batch_item).selectinload(
                GenerationBatchItem.batch
            ),
            selectinload(AudioGenerationTask.batch_item).selectinload(
                GenerationBatchItem.generation_task
            ),
            selectinload(AudioGenerationTask.batch_item)
            .selectinload(GenerationBatchItem.segments)
            .selectinload(GenerationSegment.generation_task),
        )
        .where(AudioGenerationTask.id == task_id)
    )


def _make_client(
    task: AudioGenerationTask,
) -> MiniMaxClient:
    return MiniMaxClient(
        decrypt_secret(task.config.api_key_encrypted),
        base_url=task.config.base_url,
        timeout=get_settings().minimax_request_timeout_seconds,
    )


def _ensure_cloned(
    db: Session,
    task: AudioGenerationTask,
    voice: MiniMaxVoiceAsset,
    client: MiniMaxClient,
) -> None:
    if voice.status in {
        VoiceAssetStatus.CLONED.value,
        VoiceAssetStatus.ACTIVE.value,
    }:
        return
    if not voice.source_relative_path:
        raise ValueError(f"{voice.name} 缺少短期声音参考文件")
    source = safe_relative_path(
        voice.source_relative_path,
        get_settings().data_dir,
    )
    if not voice.remote_file_id:
        remote_file_id = client.upload_clone_audio(source)
        # Persist every remote stage before the next paid/ambiguous call.
        voice.remote_file_id = str(remote_file_id)
        voice.status = VoiceAssetStatus.UPLOADED.value
        db.commit()
    client.clone_voice(int(voice.remote_file_id), voice.voice_id)
    voice.status = VoiceAssetStatus.CLONED.value
    db.commit()


def _respect_t2a_rate_limit(
    task: AudioGenerationTask,
) -> None:
    last_request = task.config.last_t2a_at
    if last_request is None:
        return
    interval = 60 / max(task.config.requests_per_minute, 1)
    elapsed = (_now() - _as_utc(last_request)).total_seconds()
    if elapsed < interval:
        time.sleep(interval - elapsed)


def _write_audio(task: AudioGenerationTask, audio_bytes: bytes) -> Path:
    target = (
        get_settings().uploads_dir
        / str(task.user_id)
        / task.planned_generation_task_id
        / (
            f"generated-audio-v{task.generation_version:03d}."
            f"{task.output_format}"
        )
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(audio_bytes)
    os.replace(temporary, target)
    return target


def _async_result_path(task: AudioGenerationTask) -> Path:
    directory = (
        get_settings().uploads_dir
        / str(task.user_id)
        / task.planned_generation_task_id
    )
    legacy = directory / "minimax-async-result.bundle"
    if task.generation_version == 1 and legacy.is_file():
        return legacy
    return directory / (
        f"minimax-async-result-v{task.generation_version:03d}.bundle"
    )


def _write_async_result(task: AudioGenerationTask, payload: bytes) -> Path:
    target = _async_result_path(task)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    temporary.write_bytes(payload)
    os.replace(temporary, target)
    return target


def _write_subtitles(task: AudioGenerationTask, cues) -> Path:
    target = (
        get_settings().uploads_dir
        / str(task.user_id)
        / task.planned_generation_task_id
        / f"generated-subtitles-v{task.generation_version:03d}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    temporary.write_text(dump_subtitle_cues(cues), encoding="utf-8")
    os.replace(temporary, target)
    return target


def _activate_used_voices(db: Session, task: AudioGenerationTask) -> None:
    activated_at = _now()
    voice = task.voice_asset
    activated_voices = (
        (voice,) if voice is not None else (task.voice_a, task.voice_b)
    )
    seen_voice_ids: set[str] = set()
    for activated_voice in activated_voices:
        if activated_voice.id in seen_voice_ids:
            continue
        seen_voice_ids.add(activated_voice.id)
        activated_voice.status = VoiceAssetStatus.ACTIVE.value
        activated_voice.activated_at = (
            activated_voice.activated_at or activated_at
        )
        # Keep the paid voice_id, but remove raw references after 48 hours.
        activated_voice.expires_at = activated_at + timedelta(
            hours=get_settings().temporary_voice_retention_hours
        )
        replicate_shared_custom_voice(db, activated_voice)


def _complete_async_speech(
    db: Session,
    task: AudioGenerationTask,
    client: MiniMaxClient,
) -> bool:
    """Finish one TTS version; return True only when video handoff may continue."""

    result_path = _async_result_path(task)
    if result_path.is_file():
        result_payload = result_path.read_bytes()
    else:
        status, file_id, _payload = client.query_async_speech_task(
            task.provider_task_id or ""
        )
        if file_id and file_id != task.provider_file_id:
            task.provider_file_id = file_id
            db.commit()
        if status == "processing":
            return False
        if status in {"failed", "expired"}:
            raise MiniMaxAPIError(
                "MiniMax 异步语音任务"
                + ("失败" if status == "failed" else "已过期")
            )
        if not task.provider_file_id:
            raise MiniMaxAPIError("MiniMax 异步语音任务完成但缺少 file_id")
        result_payload = client.download_file_content(task.provider_file_id)
        # Preserve the provider bundle so a parser fix can be retried without
        # resubmitting or paying for the same speech again.
        _write_async_result(task, result_payload)

    decoded = decode_async_speech_output(
        result_payload,
        expected_format=task.output_format,
    )
    output = _write_audio(task, decoded.audio_bytes)
    subtitles = _write_subtitles(task, decoded.cues)
    task.output_path = to_relative_data_path(output, get_settings())
    task.subtitle_path = to_relative_data_path(subtitles, get_settings())
    review_required = task.batch_item.batch.review_required
    attempt = db.scalar(
        select(AudioGenerationAttempt).where(
            AudioGenerationAttempt.audio_task_id == task.id,
            AudioGenerationAttempt.version == task.generation_version,
        )
    )
    if attempt is None:
        attempt = AudioGenerationAttempt(
            id=str(uuid.uuid4()),
            audio_task_id=task.id,
            version=task.generation_version,
            provider_task_id=task.provider_task_id,
            provider_file_id=task.provider_file_id,
            output_path=task.output_path,
            subtitle_path=task.subtitle_path,
            status="READY" if review_required else "APPROVED",
            created_at=task.provider_submitted_at or _now(),
            completed_at=_now(),
        )
        db.add(attempt)
    if review_required:
        task.status = AudioTaskStatus.AWAITING_REVIEW.value
        task.batch_item.audio_status = "AWAITING_REVIEW"
        task.batch_item.status = "AWAITING_AUDIO_REVIEW"
    else:
        task.status = AudioTaskStatus.ALIGNING.value
        task.batch_item.audio_status = "ALIGNING"
    _activate_used_voices(db, task)
    db.commit()
    log_event(
        logger,
        (
            "audio.awaiting_review"
            if review_required
            else "audio.generated"
        ),
        (
            "完整语音已生成，等待用户试听审核"
            if review_required
            else "完整语音已生成，准备切分"
        ),
        generation_version=task.generation_version,
        provider_task_id=task.provider_task_id,
        **_audio_log_context(task),
    )
    return not review_required


def _handoff_to_video(db: Session, task: AudioGenerationTask) -> None:
    if task.batch_item.segments:
        task.status = AudioTaskStatus.SUCCESS.value
        task.completed_at = task.completed_at or _now()
        db.commit()
        return
    if not task.output_path:
        raise ValueError("语音任务缺少可交接的音频文件")
    audio_path = safe_relative_path(task.output_path, get_settings().data_dir)
    if not audio_path.is_file():
        raise ValueError("已生成音频文件不存在")
    if not task.primary_kind or not task.primary_path or not task.primary_original_name:
        raise ValueError("画面素材尚未绑定，请从工作台启动 4A 画面合成")

    task.status = AudioTaskStatus.ALIGNING.value
    task.batch_item.audio_status = "ALIGNING"
    task.batch_item.status = "ALIGNING_AUDIO"
    db.commit()

    workflow_type = task.batch_item.batch.workflow_type
    parameters = json.loads(task.video_parameters_json)
    full_duration = inspect_audio_duration(audio_path)
    segment_max_seconds = (
        DIGITAL_HUMAN_MAX_SEGMENT_SECONDS
        if workflow_type == "digital_human"
        else None
    )
    if task.subtitle_path:
        subtitle_path = safe_relative_path(
            task.subtitle_path, get_settings().data_dir
        )
        if not subtitle_path.is_file():
            raise ValueError("句级时间轴文件不存在")
        cues = load_subtitle_cues(
            subtitle_path.read_text(encoding="utf-8")
        )
        plans = (
            plan_timestamped_segments(
                cues,
                full_duration,
                max_segment_seconds=segment_max_seconds,
            )
            if segment_max_seconds is not None
            else plan_timestamped_segments(cues, full_duration)
        )
    elif task.provider_task_id:
        raise ValueError("MiniMax 异步结果缺少句级时间轴，尚未创建视频任务")
    else:
        # Compatibility for audio rows created before async TTS was introduced.
        plans = (
            build_segment_plan(
                audio_path,
                task.speech_script,
                max_segment_seconds=segment_max_seconds,
            )
            if segment_max_seconds is not None
            else build_segment_plan(audio_path, task.speech_script)
        )
    task.alignment_method = ",".join(
        sorted({plan.alignment_method for plan in plans})
    )
    task.status = AudioTaskStatus.SEGMENTING.value
    task.batch_item.audio_status = "SEGMENTING"
    task.batch_item.status = "CREATING_SEGMENTS"
    db.commit()
    log_event(
        logger,
        "audio.segmentation_started",
        "开始按音频时间轴创建视频子任务",
        segment_count=len(plans),
        alignment_method=task.alignment_method,
        **_audio_log_context(task),
    )

    settings = get_settings()
    source_primary = safe_relative_path(task.primary_path, settings.data_dir)
    if workflow_type == "ltx_lip_sync":
        video_duration = inspect_media_duration(source_primary)
        if video_duration + 0.05 < full_duration:
            raise ValueError(
                f"源视频时长不足：视频 {video_duration:.1f} 秒，"
                f"生成音频 {full_duration:.1f} 秒。已保留完整音频，"
                "请更换更长的视频后重试视频阶段"
            )

    segment_dir = audio_path.parent / "segments"
    base_time = _now()
    for plan in plans:
        segment_id = str(uuid.uuid4())
        segment_audio = segment_dir / f"segment-{plan.index:03d}.mp3"
        cut_audio_segment(
            audio_path,
            segment_audio,
            start_seconds=plan.start_seconds,
            end_seconds=plan.end_seconds,
        )
        segment_duration = inspect_audio_duration(segment_audio)
        segment_audio_relative = to_relative_data_path(segment_audio, settings)
        primary_path = task.primary_path
        primary_original_name = task.primary_original_name
        segment_video_relative: str | None = None
        if workflow_type == "ltx_lip_sync":
            segment_video = segment_dir / f"segment-{plan.index:03d}.mp4"
            cut_video_segment(
                source_primary,
                segment_video,
                start_seconds=plan.start_seconds,
                duration_seconds=segment_duration,
            )
            segment_video_relative = to_relative_data_path(
                segment_video, settings
            )
            primary_path = segment_video_relative
            primary_original_name = (
                f"{Path(task.primary_original_name).stem}-"
                f"{plan.index:03d}.mp4"
            )
            prompt_prefix = str(
                parameters.get("prompt_prefix") or "一名人物用中文说"
            ).strip().rstrip("：:")
            segment_prompt = f"{prompt_prefix}：“{plan.script_text}”"
            segment_parameters = {
                **parameters,
                "prompt": segment_prompt,
            }
            metadata = {
                "has_custom_audio": True,
                "audio_duration_seconds": segment_duration,
            }
            primary_name = "video"
        else:
            segment_prompt = str(parameters.get("prompt") or "")
            segment_parameters = {
                **parameters,
                "start_time": "0:00",
                "end_time": format_duration_timecode(segment_duration),
                "person_mode": "1",
            }
            metadata = {"audio_duration_seconds": segment_duration}
            primary_name = "image"

        segment = GenerationSegment(
            id=segment_id,
            batch_item_id=task.batch_item_id,
            segment_index=plan.index,
            script_text=plan.script_text,
            start_seconds=plan.start_seconds,
            end_seconds=plan.end_seconds,
            audio_path=segment_audio_relative,
            video_path=segment_video_relative,
            prompt=segment_prompt,
            alignment_method=plan.alignment_method,
            status="CREATING_TASK",
        )
        db.add(segment)
        db.flush()
        validated = validate_task_input(
            task.user,
            workflow_type,
            [
                WorkflowAsset(
                    name=primary_name,
                    kind=task.primary_kind,
                    relative_path=primary_path,
                    original_name=primary_original_name,
                ),
                WorkflowAsset(
                    name="audio",
                    kind="audio",
                    relative_path=segment_audio_relative,
                    original_name=(
                        f"generated-{task.batch_item.row_key}-"
                        f"{plan.index:03d}.mp3"
                    ),
                ),
            ],
            segment_parameters,
            metadata,
        )
        create_generation_task(
            db,
            task.user,
            validated,
            segment_id=segment.id,
            created_at=base_time + timedelta(microseconds=plan.index),
        )
        segment.status = "TASK_CREATED"

    task.status = AudioTaskStatus.SUCCESS.value
    task.error_code = None
    task.error_message = None
    task.completed_at = _now()
    task.batch_item.status = "SEGMENTS_CREATED"
    # The new workbench needs one normalized base video even for a single
    # provider result.  Keep the legacy website's original behavior: only
    # multi-segment rows enter the quick-concatenation worker.
    task.batch_item.merged_video_status = merge_status_after_handoff(
        task.batch_item.batch.source_channel,
        len(plans),
    )
    task.batch_item.merged_video_path = None
    task.batch_item.merged_video_error = None
    db.commit()
    log_event(
        logger,
        "audio.handoff_completed",
        "语音切分完成，视频子任务已进入本地队列",
        segment_count=len(plans),
        **_audio_log_context(task),
    )


def process_task(db: Session, task_id: str) -> None:
    task = _load_task(db, task_id)
    if task is None or task.status == AudioTaskStatus.SUCCESS.value:
        return
    if not task.user.is_active:
        _mark_failed(db, task, "CONFIGURATION_ERROR", "账号已禁用")
        return
    if (
        task.user.minimax_config is None
        or not task.user.minimax_config.api_key_encrypted
        or task.config_id != task.user.minimax_config.id
        or task.account_binding_id
        != task.user.minimax_config.account_binding_id
    ):
        _mark_failed(
            db,
            task,
            "CONFIGURATION_ERROR",
            "MiniMax 账号配置缺失或已更换",
        )
        return
    try:
        output_exists = bool(
            task.output_path
            and safe_relative_path(
                task.output_path, get_settings().data_dir
            ).is_file()
        )
        if not output_exists:
            client = _make_client(task)
            voice = task.voice_asset
            if voice is not None:
                if (
                    not voice.is_saved
                    or voice.status
                    not in {
                        VoiceAssetStatus.READY.value,
                        VoiceAssetStatus.ACTIVE.value,
                    }
                    or voice.account_binding_id
                    != task.account_binding_id
                ):
                    raise ValueError("已选择的保存音色不可用")
            else:
                # Compatibility for local rows created before the voice studio.
                task.status = AudioTaskStatus.CLONING.value
                task.batch_item.audio_status = "CLONING"
                db.commit()
                _ensure_cloned(db, task, task.voice_a, client)
                _ensure_cloned(db, task, task.voice_b, client)

            if voice is not None and task.provider_task_id:
                if not _complete_async_speech(db, task, client):
                    return
            elif voice is not None:
                _respect_t2a_rate_limit(task)
                task.status = AudioTaskStatus.SYNTHESIZING.value
                task.batch_item.audio_status = "SYNTHESIZING"
                task.attempt_count += 1
                task.config.last_t2a_at = _now()
                db.commit()
                provider_task_id, provider_file_id, _payload = (
                    client.create_async_speech_task(
                        text=task.speech_script,
                        voice_id=voice.voice_id,
                        model=task.model,
                        speed=task.speed,
                        volume=task.volume,
                        pitch=task.pitch,
                        language_boost=task.language_boost,
                        output_format=task.output_format,
                        pronunciation_tones=parse_pronunciation_tones(
                            task.pronunciation_dict_json
                        ),
                    )
                )
                # Save the remote IDs before returning to the polling loop.
                task.provider_task_id = provider_task_id
                task.provider_file_id = provider_file_id
                task.provider_submitted_at = _now()
                task.status = AudioTaskStatus.REMOTE_PENDING.value
                task.batch_item.audio_status = "REMOTE_PENDING"
                task.batch_item.status = "GENERATING_AUDIO"
                db.commit()
                log_event(
                    logger,
                    "audio.submitted",
                    "脚本已提交 MiniMax 异步语音生成",
                    generation_version=task.generation_version,
                    provider_task_id=provider_task_id,
                    **_audio_log_context(task),
                )
                return
            else:
                # Legacy unsaved blend rows still use the original sync API.
                _respect_t2a_rate_limit(task)
                task.status = AudioTaskStatus.SYNTHESIZING.value
                task.batch_item.audio_status = "SYNTHESIZING"
                task.attempt_count += 1
                task.config.last_t2a_at = _now()
                db.commit()
                audio_bytes, _payload = client.synthesize_blended_voice(
                    text=task.speech_script,
                    voice_id_a=task.voice_a.voice_id,
                    voice_id_b=task.voice_b.voice_id,
                    weight_a=task.weight_a,
                    weight_b=task.weight_b,
                    model=task.model,
                    speed=task.speed,
                    volume=task.volume,
                    pitch=task.pitch,
                    language_boost=task.language_boost,
                    output_format=task.output_format,
                )
                output = _write_audio(task, audio_bytes)
                task.output_path = to_relative_data_path(
                    output, get_settings()
                )
                _activate_used_voices(db, task)
                db.commit()
        _handoff_to_video(db, task)
    except (MediaSegmentationError, MiniMaxAPIError, OSError, ValueError) as exc:
        _mark_failed(db, task, "AUDIO_GENERATION_FAILED", str(exc))


def run_once() -> int:
    processed = 0
    with SessionLocal() as db:
        while voice_task_id := claim_next_voice_task(db):
            process_voice_task(db, voice_task_id)
            processed += 1
        remote_task_ids = db.scalars(
            select(AudioGenerationTask.id)
            .where(
                AudioGenerationTask.status
                == AudioTaskStatus.REMOTE_PENDING.value
            )
            .order_by(AudioGenerationTask.created_at)
        ).all()
        for task_id in remote_task_ids:
            process_task(db, task_id)
            processed += 1
        while task_id := claim_next_pending_task(db):
            process_task(db, task_id)
            processed += 1
    return processed


def main() -> None:
    configure_logging("audio_worker")
    start_heartbeat("audio_worker")
    settings = get_settings()
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.voice_sources_dir.mkdir(parents=True, exist_ok=True)
    settings.voice_creations_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        voice_recovered = recover_interrupted_voice_tasks(db)
        if voice_recovered:
            logger.warning(
                "标记了 %s 个中断声音制作任务等待重新创建",
                voice_recovered,
            )
        recovered = recover_interrupted_tasks(db)
        if recovered:
            logger.warning("标记了 %s 个中断语音任务等待人工重试", recovered)
    log_event(
        logger,
        "audio.worker_started",
        "语音 Worker 已启动",
        poll_interval_seconds=settings.poll_interval_seconds,
    )
    while True:
        try:
            processed = run_once()
            write_heartbeat("audio_worker", processed=processed)
        except Exception:  # noqa: BLE001 - worker must survive one bad task
            logger.exception("语音 Worker 循环出现未预期错误")
            write_heartbeat("audio_worker", error="loop_error")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
