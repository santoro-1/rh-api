from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationTask,
    MiniMaxVoiceAsset,
    StagedAsset,
    TaskStatus,
    VoiceCreationStatus,
    VoiceCreationTask,
)
from app.services.storage import (
    remove_directory,
    staged_asset_dir,
    task_output_dir,
    task_upload_dir,
    voice_creation_dir,
    voice_source_dir,
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def main() -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    upload_cutoff = now - timedelta(days=settings.upload_retention_days)
    output_cutoff = now - timedelta(days=settings.output_retention_days)
    terminal_statuses = [
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.DOWNLOAD_FAILED.value,
        TaskStatus.CANCELLED.value,
    ]
    removed_uploads = removed_outputs = removed_staged_assets = removed_voice_sources = 0
    with SessionLocal() as db:
        staged_assets = db.scalars(
            select(StagedAsset).where(StagedAsset.expires_at < now)
        ).all()
        for asset in staged_assets:
            remove_directory(
                staged_asset_dir(settings, asset.user_id, asset.id)
            )
            db.delete(asset)
            removed_staged_assets += 1

        voice_assets = db.scalars(
            select(MiniMaxVoiceAsset).where(
                MiniMaxVoiceAsset.expires_at.is_not(None),
                MiniMaxVoiceAsset.expires_at < now,
                MiniMaxVoiceAsset.source_relative_path.is_not(None),
            )
        ).all()
        active_audio_statuses = {
            AudioTaskStatus.PENDING.value,
            AudioTaskStatus.CLONING.value,
            AudioTaskStatus.SYNTHESIZING.value,
            AudioTaskStatus.REMOTE_PENDING.value,
            AudioTaskStatus.ALIGNING.value,
            AudioTaskStatus.SEGMENTING.value,
            AudioTaskStatus.HANDOFF.value,
        }
        for voice in voice_assets:
            active_reference = db.scalar(
                select(AudioGenerationTask.id)
                .where(
                    (
                        (AudioGenerationTask.voice_a_id == voice.id)
                        | (AudioGenerationTask.voice_b_id == voice.id)
                        | (AudioGenerationTask.voice_asset_id == voice.id)
                    ),
                    AudioGenerationTask.status.in_(active_audio_statuses),
                )
                .limit(1)
            )
            if active_reference:
                continue
            creation_task = db.scalar(
                select(VoiceCreationTask).where(
                    VoiceCreationTask.voice_asset_id == voice.id
                )
            )
            if creation_task is not None:
                remove_directory(
                    voice_creation_dir(
                        settings,
                        creation_task.user_id,
                        creation_task.id,
                    )
                )
                creation_task.source_a_relative_path = ""
                creation_task.source_b_relative_path = None
                creation_task.preview_relative_path = None
            else:
                remove_directory(
                    voice_source_dir(settings, voice.user_id, voice.id)
                )
            voice.source_relative_path = None
            voice.preview_relative_path = None
            voice.expires_at = None
            removed_voice_sources += 1

        failed_voice_cutoff = now - timedelta(
            hours=settings.temporary_voice_retention_hours
        )
        expired_preview_tasks = db.scalars(
            select(VoiceCreationTask).where(
                VoiceCreationTask.status
                == VoiceCreationStatus.PREVIEW_READY.value,
                VoiceCreationTask.voice_asset_id.is_(None),
                func.coalesce(
                    VoiceCreationTask.completed_at,
                    VoiceCreationTask.updated_at,
                )
                < failed_voice_cutoff,
            )
        ).all()
        for voice_task in expired_preview_tasks:
            db.refresh(voice_task)
            if (
                voice_task.status
                != VoiceCreationStatus.PREVIEW_READY.value
                or voice_task.voice_asset_id is not None
            ):
                continue
            remove_directory(
                voice_creation_dir(
                    settings,
                    voice_task.user_id,
                    voice_task.id,
                )
            )
            voice_task.source_a_relative_path = ""
            voice_task.source_b_relative_path = None
            voice_task.preview_relative_path = None
            voice_task.status = VoiceCreationStatus.EXPIRED.value
            voice_task.error_code = "PREVIEW_EXPIRED"
            voice_task.error_message = "试听超过保留期限且未保存，文件已自动清理"
            voice_task.completed_at = now
            removed_voice_sources += 1

        failed_voice_tasks = db.scalars(
            select(VoiceCreationTask).where(
                VoiceCreationTask.status == VoiceCreationStatus.FAILED.value,
                VoiceCreationTask.completed_at.is_not(None),
                VoiceCreationTask.completed_at < failed_voice_cutoff,
            )
        ).all()
        for voice_task in failed_voice_tasks:
            remove_directory(
                voice_creation_dir(
                    settings,
                    voice_task.user_id,
                    voice_task.id,
                )
            )
            voice_task.source_a_relative_path = ""
            voice_task.source_b_relative_path = None
            voice_task.preview_relative_path = None
            removed_voice_sources += 1

        tasks = db.scalars(
            select(GenerationTask).where(GenerationTask.status.in_(terminal_statuses))
        ).all()
        for task in tasks:
            terminal_at = task.completed_at or task.updated_at
            if _as_utc(terminal_at) < upload_cutoff:
                remove_directory(task_upload_dir(settings, task.user_id, task.id))
                removed_uploads += 1
            if (
                task.status == TaskStatus.SUCCESS.value
                and task.completed_at
                and _as_utc(task.completed_at) < output_cutoff
            ):
                remove_directory(task_output_dir(settings, task.user_id, task.id))
                task.result_path = None
                removed_outputs += 1

        # Full-flow source media, complete audio and cut segments share the
        # audio task's planned upload directory. Keep them until every visible
        # child has reached a terminal state and the normal 48-hour window ends.
        audio_tasks = db.scalars(select(AudioGenerationTask)).all()
        for audio_task in audio_tasks:
            terminal_at = audio_task.completed_at or audio_task.updated_at
            if _as_utc(terminal_at) >= upload_cutoff:
                continue
            child_tasks = [
                segment.generation_task
                for segment in audio_task.batch_item.segments
                if segment.generation_task is not None
            ]
            if child_tasks and any(
                task.status not in terminal_statuses for task in child_tasks
            ):
                continue
            remove_directory(
                task_upload_dir(
                    settings,
                    audio_task.user_id,
                    audio_task.planned_generation_task_id,
                )
            )
            audio_task.output_path = None
            audio_task.subtitle_path = None
            removed_uploads += 1
        db.commit()
    print(
        f"已清理暂存素材 {removed_staged_assets} 个、上传目录 "
        f"{removed_uploads} 个、输出目录 {removed_outputs} 个、"
        f"声音参考目录 {removed_voice_sources} 个。"
    )


if __name__ == "__main__":
    main()
