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
    GenerationTask,
    TaskStatus,
)
from app.services.video_enhancement import task_processing_stage


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
    "MERGE_PENDING": "等待拼接完整视频",
    "MERGING": "正在拼接完整视频",
    "AWAITING_VIDEO_REVIEW": "等待完整视频审核",
    "READY": "完整视频可用",
    "PREVIEW_READY": "快速拼接预览可用，等待人工处理",
    "MERGE_FAILED": "完整视频拼接失败",
    "AUTO_READY": "可自动进入BGM和字幕",
    "MANUAL_READY": "等待人工处理",
    "VIDEO_ENHANCING": "视频清晰化中（SeedVR2 48G）",
}

TERMINAL_VIDEO_STATUSES = {
    TaskStatus.SUCCESS.value,
    TaskStatus.FAILED.value,
    TaskStatus.DOWNLOAD_FAILED.value,
    TaskStatus.CANCELLED.value,
}


def generation_task_display_status(task: GenerationTask) -> str:
    """Return the user-visible phase for a two-stage video task."""

    if task_processing_stage(task) == "VIDEO_ENHANCING":
        return "VIDEO_ENHANCING"
    return task.status


def current_runninghub_task_id(task: GenerationTask) -> str | None:
    """Expose the remote id that belongs to the phase currently being shown."""

    enhancement = task.enhancement
    if enhancement is not None and enhancement.remote_task_id:
        return enhancement.remote_task_id
    return task.runninghub_task_id


def current_auto_retry_count(task: GenerationTask) -> int:
    """Expose retry progress for the active digital-human or SeedVR2 phase."""

    enhancement = task.enhancement
    if enhancement is not None and enhancement.status != "SUCCESS":
        return enhancement.auto_retry_count
    return task.runninghub_auto_retry_count


def current_auto_retry_after(task: GenerationTask):
    """Expose the next retry time for the active phase, if one is scheduled."""

    enhancement = task.enhancement
    if enhancement is not None and enhancement.status != "SUCCESS":
        return enhancement.auto_retry_after
    return task.runninghub_auto_retry_after


def item_display_status(item: GenerationBatchItem) -> str:
    """Aggregate one user-visible row without hiding segment failures."""

    if item.generation_task:
        if item.generation_task.status == TaskStatus.SUCCESS.value:
            from app.services.postproduction import postproduction_status

            return postproduction_status(item)
        return generation_task_display_status(item.generation_task)
    if item.segments:
        statuses = [
            (
                generation_task_display_status(segment.generation_task)
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
            if item.merged_video_status in {
                "MERGE_PENDING",
                "MERGING",
                "AWAITING_VIDEO_REVIEW",
            }:
                return item.merged_video_status
            if item.merged_video_status == "MERGE_FAILED":
                return "MERGE_FAILED"
            from app.services.postproduction import postproduction_status

            return postproduction_status(item)
        if all(status == TaskStatus.CANCELLED.value for status in statuses):
            return TaskStatus.CANCELLED.value
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
        .selectinload(GenerationBatchItem.generation_task)
        .selectinload(GenerationTask.enhancement),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.audio_task)
        .selectinload(AudioGenerationTask.attempts),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.generation_task),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.generation_task)
        .selectinload(GenerationTask.enhancement),
        selectinload(GenerationBatch.items).selectinload(
            GenerationBatchItem.long_audio_project
        ),
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
    media_only = [
        item
        for item in batch.items
        if item.generation_task is None
        and not item.segments
        and item.audio_task is None
        and item.long_audio_project is not None
    ]
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
            + sum(
                item.status in {"SEGMENTING", "CREATING_SEGMENTS"}
                for item in media_only
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
        )
        + sum(item.status == "FAILED" for item in media_only),
        "cancelled": sum(
            task.status == TaskStatus.CANCELLED.value for task in tasks
        )
        + sum(item.status == "CANCELLED" for item in media_only),
        "audioPreparing": sum(
            task.status
            in PROCESSING_AUDIO_STATUSES | {AudioTaskStatus.PENDING.value}
            for task in audio_only
        ),
        "awaitingReview": sum(
            task.status == AudioTaskStatus.AWAITING_REVIEW.value
            for task in audio_only
        )
        + sum(item.status == "AWAITING_REVIEW" for item in media_only),
        "merging": sum(
            item.merged_video_status in {"MERGE_PENDING", "MERGING"}
            for item in batch.items
        ),
        "awaitingVideoReview": sum(
            item.merged_video_status == "AWAITING_VIDEO_REVIEW"
            for item in batch.items
        ),
        "completeVideos": sum(
            item.merged_video_status
            in {"AWAITING_VIDEO_REVIEW", "READY", "PREVIEW_READY"}
            for item in batch.items
        ),
        "mergeFailed": sum(
            item.merged_video_status == "MERGE_FAILED"
            for item in batch.items
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
                if item.segments and item.merged_video_status in {
                    "MERGE_PENDING",
                    "MERGING",
                    "AWAITING_VIDEO_REVIEW",
                }:
                    row_results.append("ACTIVE")
                elif (
                    item.segments
                    and item.merged_video_status == "MERGE_FAILED"
                ):
                    row_results.append("PARTIAL_FAILED")
                else:
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
        elif (
            item.long_audio_project
            and item.status in {"FAILED", "CANCELLED"}
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


def batch_view_revision(batch: GenerationBatch) -> str:
    """Return a stable signature for server-rendered controls and child rows."""

    structure = []
    for item in batch.items:
        task = item.generation_task
        audio_task = item.audio_task
        media_project = item.long_audio_project
        structure.append(
            {
                "id": item.id,
                "status": item.status,
                "task": [task.id, task.status] if task else None,
                "audioTask": (
                    [
                        audio_task.id,
                        audio_task.status,
                        audio_task.generation_version,
                        len(audio_task.attempts),
                    ]
                    if audio_task
                    else None
                ),
                "mediaProject": (
                    [media_project.id, media_project.status]
                    if media_project
                    else None
                ),
                "segments": [
                    [
                        segment.id,
                        segment.status,
                        (
                            [
                                segment.generation_task.id,
                                segment.generation_task.status,
                            ]
                            if segment.generation_task
                            else None
                        ),
                    ]
                    for segment in item.segments
                ],
                "mergedVideo": [
                    item.merged_video_status,
                    item.merged_video_path,
                    item.merged_video_error,
                ],
            }
        )
    return json.dumps(structure, ensure_ascii=False, separators=(",", ":"))


def batch_detail_status(batch: GenerationBatch) -> list[dict[str, Any]]:
    """Return the compact item/segment payload used by in-place polling."""

    from app.services.postproduction import (
        postproduction_mode,
        postproduction_status,
    )

    return [
        {
            "id": item.id,
            "status": item_display_status(item),
            "runninghubTaskId": (
                current_runninghub_task_id(item.generation_task)
                if item.generation_task
                else None
            ),
            "errorMessage": (
                item.generation_task.error_message
                if item.generation_task
                else (item.merged_video_error or item.error_message)
            ),
            "mergedVideoStatus": item.merged_video_status,
            "mergedVideoPath": item.merged_video_path,
            "mergedVideoError": item.merged_video_error,
            "postproductionMode": postproduction_mode(item),
            "postproductionStatus": postproduction_status(item),
            "autoRetryCount": (
                current_auto_retry_count(item.generation_task)
                if item.generation_task
                else 0
            ),
            "autoRetryAfter": (
                current_auto_retry_after(item.generation_task).isoformat()
                if item.generation_task
                and current_auto_retry_after(item.generation_task)
                else None
            ),
            "lastFailedRunninghubTaskId": _last_failed_task_id(
                item.generation_task
            ),
            "segments": [
                {
                    "id": segment.id,
                    "status": (
                        generation_task_display_status(segment.generation_task)
                        if segment.generation_task
                        else segment.status
                    ),
                    "runninghubTaskId": (
                        current_runninghub_task_id(segment.generation_task)
                        if segment.generation_task
                        else None
                    ),
                    "errorMessage": (
                        segment.generation_task.error_message
                        if segment.generation_task
                        else None
                    ),
                    "autoRetryCount": (
                        current_auto_retry_count(segment.generation_task)
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
