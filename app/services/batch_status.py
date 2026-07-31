from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    TaskStatus,
)


# Video states rendered as live work in batch pages. PENDING is active for the
# user interface but is removed when calculating "processing" because it has
# its own queued counter.
ACTIVE_VIDEO_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
}

# Audio states that actively advance without user input. AWAITING_REVIEW is
# intentionally separate so the UI can show a clear manual approval count.
PROCESSING_AUDIO_STATUSES = {
    AudioTaskStatus.CLONING.value,
    AudioTaskStatus.SYNTHESIZING.value,
    AudioTaskStatus.REMOTE_PENDING.value,
    AudioTaskStatus.ALIGNING.value,
    AudioTaskStatus.SEGMENTING.value,
    AudioTaskStatus.HANDOFF.value,
}

STATUS_LABELS = {
    "PENDING": "等待处理",
    "CLONING": "准备音色",
    "SYNTHESIZING": "提交语音生成",
    "REMOTE_PENDING": "MiniMax 生成中",
    "AWAITING_REVIEW": "等待语音审核",
    "ALIGNING": "解析语音时间戳",
    "SEGMENTING": "切分音频",
    "HANDOFF": "创建视频任务",
    "UPLOADING": "上传素材",
    "SUBMITTED": "已提交 RunningHub",
    "RUNNING": "RunningHub 生成中",
    "SUCCESS": "成功",
    "PARTIAL_FAILED": "部分失败",
    "FAILED": "失败",
    "DOWNLOAD_FAILED": "下载失败",
    "CANCELLED": "已取消",
}

TERMINAL_VIDEO_STATUSES = {
    TaskStatus.SUCCESS.value,
    TaskStatus.FAILED.value,
    TaskStatus.DOWNLOAD_FAILED.value,
    TaskStatus.CANCELLED.value,
}


def item_display_status(item: GenerationBatchItem) -> str:
    """Aggregate one user-visible row without hiding segment failures."""

    if item.generation_task:
        return item.generation_task.status
    if item.segments:
        statuses = [
            (
                segment.generation_task.status
                if segment.generation_task
                else segment.status
            )
            for segment in item.segments
        ]
        if not all(status in TERMINAL_VIDEO_STATUSES for status in statuses):
            return next(
                (
                    status
                    for status in statuses
                    if status not in TERMINAL_VIDEO_STATUSES
                ),
                item.status,
            )
        if all(status == TaskStatus.SUCCESS.value for status in statuses):
            return TaskStatus.SUCCESS.value
        if any(status == TaskStatus.SUCCESS.value for status in statuses):
            return "PARTIAL_FAILED"
        return TaskStatus.FAILED.value
    if item.audio_task:
        return item.audio_task.status
    return item.status


def _attempt_history(task) -> list[dict]:
    if task is None or not task.runninghub_attempt_history:
        return []
    try:
        value = json.loads(task.runninghub_attempt_history)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _last_failed_task_id(task) -> str | None:
    history = _attempt_history(task)
    if not history:
        return None
    task_id = history[-1].get("taskId")
    return str(task_id) if task_id else None


def batch_query():
    """Load all relationships required by summary/detail rendering once."""

    return select(GenerationBatch).options(
        selectinload(GenerationBatch.user),
        selectinload(GenerationBatch.items).selectinload(
            GenerationBatchItem.generation_task
        ),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.audio_task)
        .selectinload(AudioGenerationTask.attempts),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.generation_task),
    )


def _video_tasks(batch: GenerationBatch):
    """Flatten direct and segmented RunningHub tasks in display order."""

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


def _audio_only_tasks(batch: GenerationBatch):
    """Return TTS rows that have not created any RunningHub child yet."""

    return [
        item.audio_task
        for item in batch.items
        if item.generation_task is None
        and not item.segments
        and item.audio_task is not None
    ]


def summarize_batch(batch: GenerationBatch) -> dict[str, Any]:
    """Build the single source of truth for list/detail progress counters."""

    tasks = _video_tasks(batch)
    audio_only = _audio_only_tasks(batch)
    counts = {
        "queued": (
            sum(task.status == TaskStatus.PENDING.value for task in tasks)
            + sum(
                task.status == AudioTaskStatus.PENDING.value
                for task in audio_only
            )
        ),
        "processing": (
            sum(
                task.status
                in ACTIVE_VIDEO_STATUSES - {TaskStatus.PENDING.value}
                for task in tasks
            )
            + sum(
                task.status in PROCESSING_AUDIO_STATUSES
                for task in audio_only
            )
        ),
        "success": sum(
            task.status == TaskStatus.SUCCESS.value for task in tasks
        ),
        "failed": sum(
            task.status
            in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}
            for task in tasks
        )
        + sum(
            task.status == AudioTaskStatus.FAILED.value
            for task in audio_only
        ),
        "cancelled": sum(
            task.status == TaskStatus.CANCELLED.value for task in tasks
        ),
        "audioPreparing": sum(
            task.status
            in PROCESSING_AUDIO_STATUSES | {AudioTaskStatus.PENDING.value}
            for task in audio_only
        ),
        "awaitingReview": sum(
            task.status == AudioTaskStatus.AWAITING_REVIEW.value
            for task in audio_only
        ),
    }

    # row_results evaluates one manifest row, not one provider call. A long
    # script with five segments is still one finished row in batch progress.
    row_results: list[str] = []
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
        if item_tasks and all(
            task.status in TERMINAL_VIDEO_STATUSES for task in item_tasks
        ):
            if all(
                task.status == TaskStatus.SUCCESS.value for task in item_tasks
            ):
                row_results.append("SUCCESS")
            elif any(
                task.status == TaskStatus.SUCCESS.value for task in item_tasks
            ):
                row_results.append("PARTIAL_FAILED")
            else:
                row_results.append("FAILED")
        elif (
            item.audio_task
            and item.audio_task.status == AudioTaskStatus.FAILED.value
            and not item_tasks
        ):
            row_results.append("FAILED")
        else:
            row_results.append("ACTIVE")

    finished = sum(result != "ACTIVE" for result in row_results)
    total = batch.total_items
    if finished < total:
        status = "ACTIVE"
    elif all(result == "SUCCESS" for result in row_results):
        status = "SUCCESS"
    elif any(
        result in {"SUCCESS", "PARTIAL_FAILED"} for result in row_results
    ):
        status = "PARTIAL_FAILED"
    else:
        status = "FAILED"
    return {
        "batchId": batch.id,
        "name": batch.name,
        "workflowType": batch.workflow_type,
        "audioMode": batch.audio_mode,
        "total": total,
        "childTotal": len(tasks),
        **counts,
        "finished": finished,
        "progress": round(finished * 100 / total) if total else 0,
        "status": status,
        "createdAt": batch.created_at.isoformat(),
    }


def batch_detail_status(batch: GenerationBatch) -> list[dict[str, Any]]:
    """Return the compact item/segment payload used by in-place polling."""

    return [
        {
            "id": item.id,
            "status": item_display_status(item),
            "runninghubTaskId": (
                item.generation_task.runninghub_task_id
                if item.generation_task
                else None
            ),
            "errorMessage": (
                item.generation_task.error_message
                if item.generation_task
                else None
            ),
            "autoRetryCount": (
                item.generation_task.runninghub_auto_retry_count
                if item.generation_task
                else 0
            ),
            "autoRetryAfter": (
                item.generation_task.runninghub_auto_retry_after.isoformat()
                if (
                    item.generation_task
                    and item.generation_task.runninghub_auto_retry_after
                )
                else None
            ),
            "lastFailedRunninghubTaskId": _last_failed_task_id(
                item.generation_task
            ),
            "segments": [
                {
                    "id": segment.id,
                    "status": (
                        segment.generation_task.status
                        if segment.generation_task
                        else segment.status
                    ),
                    "runninghubTaskId": (
                        segment.generation_task.runninghub_task_id
                        if segment.generation_task
                        else None
                    ),
                    "errorMessage": (
                        segment.generation_task.error_message
                        if segment.generation_task
                        else None
                    ),
                    "autoRetryCount": (
                        segment.generation_task.runninghub_auto_retry_count
                        if segment.generation_task
                        else 0
                    ),
                    "lastFailedRunninghubTaskId": _last_failed_task_id(
                        segment.generation_task
                    ),
                }
                for segment in item.segments
            ],
        }
        for item in batch.items
    ]
