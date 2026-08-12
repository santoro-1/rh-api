from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.models import GenerationBatchItem, GenerationTask, TaskStatus
from app.services.speech.async_outputs import load_subtitle_cues
from app.services.storage import safe_relative_path
from app.services.video_enhancement import (
    task_processing_stage,
    task_quality_variant,
)


AUTO_POSTPROCESS = "AUTO_POSTPROCESS"
MANUAL_EDIT_REQUIRED = "MANUAL_EDIT_REQUIRED"


def ordered_video_tasks(item: GenerationBatchItem) -> list[GenerationTask]:
    """Return provider tasks in the order expected by an editor."""

    if item.segments:
        return [
            segment.generation_task
            for segment in sorted(
                item.segments, key=lambda value: value.segment_index
            )
            if segment.generation_task is not None
        ]
    return [item.generation_task] if item.generation_task is not None else []


def postproduction_mode(item: GenerationBatchItem) -> str:
    """Only one uncut TTS video may continue without manual editing."""

    if item.batch.audio_mode == "minimax" and len(item.segments) == 1:
        return AUTO_POSTPROCESS
    return MANUAL_EDIT_REQUIRED


def manual_edit_reason(item: GenerationBatchItem) -> str | None:
    if postproduction_mode(item) == AUTO_POSTPROCESS:
        return None
    if item.batch.audio_mode == "upload":
        return "UPLOADED_AUDIO"
    if len(item.segments) > 1:
        return "SEGMENTED_VIDEO"
    return "MANUAL_SOURCE"


def postproduction_status(item: GenerationBatchItem) -> str:
    tasks = ordered_video_tasks(item)
    if not tasks:
        return "WAITING_VIDEO"
    statuses = [task.status for task in tasks]
    if any(
        status
        not in {
            TaskStatus.SUCCESS.value,
            TaskStatus.FAILED.value,
            TaskStatus.DOWNLOAD_FAILED.value,
            TaskStatus.CANCELLED.value,
        }
        for status in statuses
    ):
        return "WAITING_VIDEO"
    if all(status == TaskStatus.SUCCESS.value for status in statuses):
        return (
            "AUTO_READY"
            if postproduction_mode(item) == AUTO_POSTPROCESS
            else "MANUAL_READY"
        )
    if all(status == TaskStatus.CANCELLED.value for status in statuses):
        return "CANCELLED"
    if any(status == TaskStatus.SUCCESS.value for status in statuses):
        return "PARTIAL_FAILED"
    return "FAILED"


def _caption_payload(
    item: GenerationBatchItem,
    settings: Settings,
) -> dict[str, Any] | None:
    audio_task = item.audio_task
    if audio_task is None or not audio_task.subtitle_path:
        return None
    try:
        subtitle_path = safe_relative_path(audio_task.subtitle_path, settings.data_dir)
    except ValueError:
        return None
    if not subtitle_path.is_file():
        return None
    try:
        cues = load_subtitle_cues(subtitle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return {
        "source": "minimax_timestamps",
        "text": audio_task.speech_script,
        "cues": [
            {
                "start_us": round(cue.start_seconds * 1_000_000),
                "end_us": round(cue.end_seconds * 1_000_000),
                "duration_us": round(
                    (cue.end_seconds - cue.start_seconds) * 1_000_000
                ),
                "text": cue.text,
            }
            for cue in cues
        ],
        "valid_after_manual_edit": False,
    }


def postproduction_manifest(
    item: GenerationBatchItem,
    settings: Settings,
) -> dict[str, Any]:
    tasks = ordered_video_tasks(item)
    mode = postproduction_mode(item)
    videos = []
    ordered_segments = sorted(
        item.segments, key=lambda value: value.segment_index
    )
    for index, task in enumerate(tasks, start=1):
        segment = (
            ordered_segments[index - 1]
            if len(ordered_segments) == len(tasks)
            else None
        )
        videos.append(
            {
                "index": index,
                "task_id": task.id,
                "status": task.status,
                "download_url": f"/api/tasks/{task.id}/download",
                "preview_url": f"/api/tasks/{task.id}/preview",
                "source_download_url": (
                    f"/api/tasks/{task.id}/source-video"
                    if task.workflow_type == "digital_human" and task.enhancement is not None
                    else None
                ),
                "processing_stage": task_processing_stage(task),
                "enhancement_status": (
                    task.enhancement.status if task.enhancement is not None else None
                ),
                "quality_variant": task_quality_variant(task),
                "seedvr2_enabled": task.seedvr2_enabled,
                "script_text": segment.script_text if segment is not None else "",
                "start_seconds": segment.start_seconds if segment is not None else 0.0,
                "end_seconds": segment.end_seconds if segment is not None else task.audio_duration_seconds,
            }
        )
    captions = _caption_payload(item, settings)
    return {
        "schema": "runninghub.postproduction.v1",
        "batch_id": item.batch_id,
        "item_id": item.id,
        "row_key": item.row_key,
        "workflow_type": item.batch.workflow_type,
        "input_mode": "text" if item.batch.audio_mode == "minimax" else "audio",
        "mode": mode,
        "status": postproduction_status(item),
        "requires_manual_edit": mode == MANUAL_EDIT_REQUIRED,
        "manual_edit_reason": manual_edit_reason(item),
        "source": {
            "type": "single_video" if len(videos) == 1 else "ordered_segments",
            "videos": videos,
            "quick_preview_url": (
                f"/batches/{item.batch_id}/items/{item.id}/merged-video"
                if item.merged_video_path
                else None
            ),
        },
        "captions": captions,
        "caption_timeline_is_final": bool(
            captions is not None and mode == AUTO_POSTPROCESS
        ),
    }
