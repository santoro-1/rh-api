from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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


DELETABLE_AUDIO_TASK_STATUSES = {
    AudioTaskStatus.AWAITING_REVIEW.value,
    AudioTaskStatus.SUCCESS.value,
    AudioTaskStatus.FAILED.value,
}


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
    task_ids = {task.id for task in tasks}
    for item in batch.items:
        if item.audio_task is not None:
            task_ids.add(item.audio_task.planned_generation_task_id)
        for segment in item.segments:
            for relative_path in (segment.audio_path, segment.video_path):
                if not relative_path:
                    continue
                normalized = str(relative_path).replace("\\", "/")
                parts = PurePosixPath(normalized).parts
                if (
                    len(parts) >= 3
                    and parts[0] == "uploads"
                    and parts[1] == str(batch.user_id)
                ):
                    task_ids.add(parts[2])
    return [
        (
            task_upload_dir(settings, batch.user_id, task_id),
            task_output_dir(settings, batch.user_id, task_id),
        )
        for task_id in sorted(task_ids)
    ]


def batch_is_deletable(batch: GenerationBatch) -> bool:
    """Allow terminal batches and locally stuck rows with no active worker."""

    if any(
        task.status not in TERMINAL_TASK_STATUSES
        for task in _video_tasks(batch)
    ):
        return False
    return all(
        item.audio_task is None
        or item.audio_task.status in DELETABLE_AUDIO_TASK_STATUSES
        for item in batch.items
    )


def _ensure_deletable(batch: GenerationBatch) -> None:
    if not batch_is_deletable(batch):
        raise BatchLifecycleError(
            "批次仍有排队或运行任务，不能删除"
        )


def delete_terminal_batch(
    db: Session,
    batch: GenerationBatch,
    settings: Settings,
) -> None:
    """Delete one safe batch atomically, then clean its local directories."""

    _ensure_deletable(batch)
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
