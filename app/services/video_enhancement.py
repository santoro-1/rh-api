from __future__ import annotations

from app.models import EnhancementStatus, GenerationTask, TaskStatus


def task_processing_stage(task: GenerationTask) -> str | None:
    if task.workflow_type != "digital_human":
        return None
    enhancement = task.enhancement
    if enhancement is not None:
        if enhancement.status == EnhancementStatus.SUCCESS.value:
            return "BASE_VIDEO_READY"
        if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}:
            return "VIDEO_ENHANCEMENT_FAILED"
        if enhancement.status == EnhancementStatus.CANCELLED.value:
            return "CANCELLED"
        return "VIDEO_ENHANCING"
    if task.status == TaskStatus.SUCCESS.value:
        # Historical tasks completed before SeedVR2 was introduced.
        return "BASE_VIDEO_READY"
    if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}:
        return "DIGITAL_HUMAN_FAILED"
    if task.status == TaskStatus.CANCELLED.value:
        return "CANCELLED"
    return "DIGITAL_HUMAN_RUNNING"


def task_quality_variant(task: GenerationTask) -> str | None:
    enhancement = task.enhancement
    if (
        task.workflow_type == "digital_human"
        and enhancement is not None
        and enhancement.status == EnhancementStatus.SUCCESS.value
    ):
        return "seedvr2_upscaled"
    return None


def task_status_text(task: GenerationTask, fallback: str) -> str:
    stage = task_processing_stage(task)
    if stage == "VIDEO_ENHANCING":
        return "视频清晰化中（SeedVR2 48G）"
    if stage == "VIDEO_ENHANCEMENT_FAILED":
        return "视频清晰化失败"
    if stage == "BASE_VIDEO_READY" and task_quality_variant(task):
        return "清晰视频已完成"
    return fallback
