from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

from app.config import Settings
from app.models import (
    BATCH_SOURCE_LTX_WORKBENCH,
    EnhancementStatus,
    GenerationTask,
    GenerationTaskEnhancement,
    TaskStatus,
)
from app.services.storage import safe_relative_path


class VideoEnhancementBackfillError(ValueError):
    """A historical digital-human result cannot enter SeedVR2 safely."""


def task_is_ltx_workbench(task: GenerationTask) -> bool:
    """Identify only the independent LTX workbench, never the legacy LTX page."""

    item = task.batch_item or (
        task.segment.batch_item if task.segment is not None else None
    )
    batch = item.batch if item is not None else None
    return bool(
        task.workflow_type == "ltx_lip_sync"
        and batch is not None
        and batch.source_channel == BATCH_SOURCE_LTX_WORKBENCH
    )


def task_uses_seedvr2_pipeline(task: GenerationTask) -> bool:
    """Return whether source success must continue into a SeedVR2 stage."""

    if not task.seedvr2_enabled:
        return False
    return task.workflow_type == "digital_human" or task_is_ltx_workbench(task)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def historical_source_path(
    task: GenerationTask,
    settings: Settings,
) -> Path:
    """Validate and return a pre-SeedVR2 digital-human result."""

    if task.workflow_type != "digital_human":
        raise VideoEnhancementBackfillError("当前任务不是数字人视频任务")
    if task.enhancement is not None:
        try:
            source = safe_relative_path(
                task.enhancement.source_result_path, settings.data_dir
            )
        except ValueError as exc:
            raise VideoEnhancementBackfillError(
                "数字人源片段路径不合法，不能执行 SeedVR2 清晰化"
            ) from exc
        if not source.is_file():
            raise VideoEnhancementBackfillError(
                "数字人源片段已丢失，不能只补跑 SeedVR2 清晰化"
            )
        return source
    if task.status != TaskStatus.SUCCESS.value or not task.result_path:
        raise VideoEnhancementBackfillError(
            "数字人源片段不存在或未成功，不能只补跑 SeedVR2 清晰化"
        )
    try:
        source = safe_relative_path(task.result_path, settings.data_dir)
    except ValueError as exc:
        raise VideoEnhancementBackfillError(
            "历史数字人结果路径不合法，不能执行 SeedVR2 清晰化"
        ) from exc
    if not source.is_file():
        raise VideoEnhancementBackfillError(
            "历史数字人源片段已丢失，不能只补跑 SeedVR2 清晰化"
        )
    return source


def queue_historical_seedvr2_enhancement(
    task: GenerationTask,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> GenerationTaskEnhancement:
    """Attach SeedVR2 to a successful historical result without rerunning 4A."""

    if task.enhancement is not None:
        raise VideoEnhancementBackfillError("当前数字人片段已经存在 SeedVR2 阶段")
    source = historical_source_path(task, settings)
    enhancement = GenerationTaskEnhancement(
        id=str(uuid.uuid4()),
        generation_task_id=task.id,
        status=EnhancementStatus.PENDING.value,
        source_result_path=str(task.result_path),
        source_filename=source.name,
        source_size=source.stat().st_size,
        source_sha256=_file_sha256(source),
        source_output_metadata_json=task.output_metadata,
        execution_account_id=task.execution_account_id,
    )
    task.enhancement = enhancement
    task.status = TaskStatus.RUNNING.value
    task.result_path = None
    task.output_metadata = None
    task.error_code = None
    task.error_message = None
    task.runninghub_failed_reason = None
    task.runninghub_auto_retry_after = None
    task.completed_at = None
    task.updated_at = now or datetime.now(timezone.utc)
    return enhancement


def task_processing_stage(task: GenerationTask) -> str | None:
    enhancement = task.enhancement
    if enhancement is not None:
        if enhancement.status == EnhancementStatus.SUCCESS.value:
            return "BASE_VIDEO_READY"
        if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}:
            return "VIDEO_ENHANCEMENT_FAILED"
        if enhancement.status == EnhancementStatus.CANCELLED.value:
            return "CANCELLED"
        return "VIDEO_ENHANCING"
    if task_is_ltx_workbench(task):
        if task.status == TaskStatus.SUCCESS.value:
            return "BASE_VIDEO_READY"
        if task.status in {TaskStatus.FAILED.value, TaskStatus.DOWNLOAD_FAILED.value}:
            return "LTX_FAILED"
        if task.status == TaskStatus.CANCELLED.value:
            return "CANCELLED"
        return "LTX_RUNNING"
    if task.workflow_type != "digital_human":
        return None
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
        enhancement is not None
        and enhancement.status == EnhancementStatus.SUCCESS.value
    ):
        return "seedvr2_upscaled"
    if (
        task.workflow_type == "digital_human"
        and not task.seedvr2_enabled
        and task.status == TaskStatus.SUCCESS.value
    ):
        return "digital_human_source"
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
