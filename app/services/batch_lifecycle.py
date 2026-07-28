from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AudioTaskStatus, GenerationBatch
from app.services.storage import (
    remove_directory,
    task_output_dir,
    task_upload_dir,
)
from app.services.task_management import (
    RETRYABLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    TaskManagementError,
    prepare_task_retry,
)


class BatchLifecycleError(ValueError):
    """A batch cannot perform the requested lifecycle transition."""


class BatchFileCleanupError(OSError):
    """Database deletion succeeded but at least one local directory remained."""


@dataclass(frozen=True)
class RetrySummary:
    """Counts returned to the redirect banner after one batch retry request."""

    retried: int
    skipped: int


def _video_tasks(batch: GenerationBatch):
    return [
        task
        for item in batch.items
        for task in (
            [item.generation_task]
            if item.generation_task
            else [
                segment.generation_task
                for segment in item.segments
                if segment.generation_task
            ]
        )
    ]


def retry_failed_batch(
    batch: GenerationBatch,
    settings: Settings,
) -> RetrySummary:
    """Reset eligible child tasks while preserving paid remote results."""

    retried = 0
    skipped = 0
    for item in batch.items:
        item_tasks = (
            [item.generation_task]
            if item.generation_task
            else [
                segment.generation_task
                for segment in item.segments
                if segment.generation_task
            ]
        )
        for task in item_tasks:
            if task.status not in RETRYABLE_TASK_STATUSES:
                continue
            try:
                prepare_task_retry(task, settings)
                retried += 1
            except TaskManagementError:
                skipped += 1
        audio_task = item.audio_task
        if (
            not item_tasks
            and audio_task
            and audio_task.status == AudioTaskStatus.FAILED.value
        ):
            audio_task.status = AudioTaskStatus.PENDING.value
            audio_task.error_code = None
            audio_task.error_message = None
            audio_task.completed_at = None
            item.audio_status = "PENDING"
            item.status = "AUDIO_PENDING"
            retried += 1
    return RetrySummary(retried=retried, skipped=skipped)


def _deletion_directories(
    batch: GenerationBatch,
    settings: Settings,
) -> list[tuple[Path, Path]]:
    tasks = _video_tasks(batch)
    directories = [
        (
            task_upload_dir(settings, task.user_id, task.id),
            task_output_dir(settings, task.user_id, task.id),
        )
        for task in tasks
    ]
    directories.extend(
        (
            task_upload_dir(
                settings,
                audio_task.user_id,
                audio_task.planned_generation_task_id,
            ),
            task_output_dir(
                settings,
                audio_task.user_id,
                audio_task.planned_generation_task_id,
            ),
        )
        for item in batch.items
        if (audio_task := item.audio_task) is not None
        and item.generation_task is None
    )
    return directories


def _ensure_terminal(batch: GenerationBatch) -> None:
    unfinished = [
        item
        for item in batch.items
        if (
            item.generation_task is not None
            and item.generation_task.status not in TERMINAL_TASK_STATUSES
        )
        or any(
            segment.generation_task is None
            or segment.generation_task.status not in TERMINAL_TASK_STATUSES
            for segment in item.segments
        )
        or (
            item.generation_task is None
            and not item.segments
            and (
                item.audio_task is None
                or item.audio_task.status != AudioTaskStatus.FAILED.value
            )
        )
    ]
    if unfinished:
        raise BatchLifecycleError(
            "批次仍有排队或运行任务，不能删除"
        )


def delete_terminal_batch(
    db: Session,
    batch: GenerationBatch,
    settings: Settings,
) -> None:
    """Delete one terminal batch atomically, then clean its local directories."""

    _ensure_terminal(batch)
    tasks = _video_tasks(batch)
    directories = _deletion_directories(batch, settings)
    for task in tasks:
        db.delete(task)
    db.flush()
    db.delete(batch)
    db.commit()

    # File cleanup happens after the durable database deletion. Re-running the
    # delete endpoint is impossible, so surface a distinct operational error
    # and let scheduled cleanup/administration remove any orphan directory.
    try:
        for upload_dir, output_dir in directories:
            remove_directory(upload_dir)
            remove_directory(output_dir)
    except OSError as exc:
        raise BatchFileCleanupError(
            "批次记录已删除，但部分本地文件清理失败"
        ) from exc
