from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationBatchItem,
)


class AudioReviewError(ValueError):
    """The requested approval action is invalid for the current task state."""


def reviewable_item(
    batch: GenerationBatch,
    item_id: str,
) -> GenerationBatchItem:
    """Resolve one row at the full-audio review gate."""

    item = next(
        (candidate for candidate in batch.items if candidate.id == item_id),
        None,
    )
    if (
        item is None
        or not batch.review_required
        or item.audio_task is None
        or item.audio_task.status != AudioTaskStatus.AWAITING_REVIEW.value
        or not item.audio_task.output_path
        or item.segments
        or item.generation_task
    ):
        raise AudioReviewError("当前任务不在语音审核阶段")
    return item


def current_attempt(
    task: AudioGenerationTask,
) -> AudioGenerationAttempt:
    """Return the version currently selected for approval/regeneration."""

    attempt = next(
        (
            candidate
            for candidate in task.attempts
            if candidate.version == task.generation_version
        ),
        None,
    )
    if attempt is None:
        raise AudioReviewError("当前语音版本记录不存在")
    return attempt


def _approve_item(
    item: GenerationBatchItem,
    reviewed_at: datetime,
) -> None:
    task = item.audio_task
    if task is None:
        raise AudioReviewError("当前任务没有可审核语音")
    current_attempt(task).status = "APPROVED"
    task.reviewed_at = reviewed_at
    # PENDING means the audio Worker may now perform segmentation/handoff. It
    # does not generate or charge for another MiniMax speech version.
    task.status = AudioTaskStatus.PENDING.value
    item.audio_status = "AUDIO_APPROVED"
    item.status = "AUDIO_APPROVED"


def approve_item_audio(
    batch: GenerationBatch,
    item_id: str,
    *,
    reviewed_at: datetime | None = None,
) -> None:
    item = reviewable_item(batch, item_id)
    _approve_item(item, reviewed_at or datetime.now(timezone.utc))


def approve_all_audio(
    batch: GenerationBatch,
    *,
    reviewed_at: datetime | None = None,
) -> int:
    approved = 0
    approval_time = reviewed_at or datetime.now(timezone.utc)
    for item in batch.items:
        task = item.audio_task
        if (
            task is None
            or task.status != AudioTaskStatus.AWAITING_REVIEW.value
            or not task.output_path
            or item.segments
            or item.generation_task
        ):
            continue
        _approve_item(item, approval_time)
        approved += 1
    if not approved:
        raise AudioReviewError("没有等待审核的语音")
    return approved


def regenerate_item_audio(batch: GenerationBatch, item_id: str) -> int:
    """Reject the current version and reset provider fields for a paid retry."""

    item = reviewable_item(batch, item_id)
    task = item.audio_task
    if task is None:
        raise AudioReviewError("当前任务没有可重新生成的语音")
    current_attempt(task).status = "REJECTED"
    task.generation_version += 1
    task.provider_task_id = None
    task.provider_file_id = None
    task.provider_submitted_at = None
    task.output_path = None
    task.subtitle_path = None
    task.alignment_method = None
    task.reviewed_at = None
    task.error_code = None
    task.error_message = None
    task.completed_at = None
    task.status = AudioTaskStatus.PENDING.value
    item.audio_status = "PENDING"
    item.status = "AUDIO_REGENERATING"
    return task.generation_version
