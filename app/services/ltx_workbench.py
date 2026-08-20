from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import (
    BATCH_SOURCE_LTX_WORKBENCH,
    EnhancementStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    LongAudioProject,
    LongAudioProjectStatus,
    LtxPreparationJob,
    LtxPreparationStatus,
    StagedAsset,
    User,
)
from app.services.audio import inspect_audio_duration
from app.services.batch_assets import StagedAssetError, load_available_assets
from app.services.long_audio import MAX_LONG_AUDIO_SECONDS
from app.services.media_segmentation import inspect_media_duration
from app.services.storage import (
    long_audio_project_dir,
    materialize_staged_asset,
    remove_directory,
    safe_relative_path,
    to_relative_data_path,
)
from app.services.task_creation import ensure_user_can_create_workflow
from app.services.workflow_configs import get_user_workflow_config


LTX_WORKFLOW = "ltx_lip_sync"
FIXED_PROMPT_PREFIX = "一个人用中文说"


class LtxWorkbenchError(ValueError):
    """A workbench row cannot enter the unified LTX preparation flow."""


def compile_ltx_prompt(script_text: str) -> str:
    clean_script = script_text.strip()
    if not clean_script:
        raise LtxWorkbenchError("表格原稿不能为空")
    return f"{FIXED_PROMPT_PREFIX}：“{clean_script}”"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_rows(raw_rows: Any, settings: Settings) -> list[dict[str, str]]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise LtxWorkbenchError("请至少提交一行 LTX 任务")
    if len(raw_rows) > settings.max_batch_items:
        raise LtxWorkbenchError(
            f"单批任务数量不能超过 {settings.max_batch_items}"
        )
    rows: list[dict[str, str]] = []
    seen_row_ids: set[str] = set()
    for position, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise LtxWorkbenchError(f"第 {position} 行格式错误")
        if any(
            str(raw.get(field) or "").strip()
            for field in ("prompt", "prompt_prefix", "promptPrefix")
        ):
            raise LtxWorkbenchError("LTX 提示词由表格原稿固定生成，不能自定义")
        row_id = str(raw.get("row_id") or raw.get("rowId") or "").strip()
        script_text = str(
            raw.get("script_text") or raw.get("scriptText") or ""
        ).strip()
        video_asset_id = str(
            raw.get("video_asset_id") or raw.get("videoAssetId") or ""
        ).strip()
        audio_asset_id = str(
            raw.get("audio_asset_id") or raw.get("audioAssetId") or ""
        ).strip()
        if not row_id or len(row_id) > 100:
            raise LtxWorkbenchError(f"第 {position} 行任务 ID 长度必须为 1–100")
        if row_id in seen_row_ids:
            raise LtxWorkbenchError(f"任务 ID 重复：{row_id}")
        if not script_text or len(script_text) > 100_000:
            raise LtxWorkbenchError(
                f"第 {position} 行表格原稿长度必须为 1–100000"
            )
        if not video_asset_id or not audio_asset_id:
            raise LtxWorkbenchError(
                f"第 {position} 行必须同时绑定源视频和自定义音频"
            )
        compile_ltx_prompt(script_text)
        seen_row_ids.add(row_id)
        rows.append(
            {
                "row_id": row_id,
                "script_text": script_text,
                "video_asset_id": video_asset_id,
                "audio_asset_id": audio_asset_id,
            }
        )
    return rows


def _assets_for_rows(
    db: Session,
    user: User,
    rows: list[dict[str, str]],
) -> dict[str, StagedAsset]:
    asset_ids = [
        asset_id
        for row in rows
        for asset_id in (row["video_asset_id"], row["audio_asset_id"])
    ]
    assets = load_available_assets(db, user, asset_ids)
    result = {asset.id: asset for asset in assets}
    for position, row in enumerate(rows, start=1):
        video = result[row["video_asset_id"]]
        audio = result[row["audio_asset_id"]]
        if video.kind != "video":
            raise LtxWorkbenchError(f"第 {position} 行源视频素材类型错误")
        if audio.kind != "audio":
            raise LtxWorkbenchError(f"第 {position} 行自定义音频素材类型错误")
    return result


def _inspect_pair(
    settings: Settings,
    video: StagedAsset,
    audio: StagedAsset,
) -> tuple[float, float]:
    video_path = safe_relative_path(video.relative_path, settings.data_dir)
    audio_path = safe_relative_path(audio.relative_path, settings.data_dir)
    if not video_path.is_file() or not audio_path.is_file():
        raise LtxWorkbenchError("暂存的源视频或自定义音频文件不存在")
    audio_duration = inspect_audio_duration(audio_path)
    video_duration = inspect_media_duration(video_path)
    if audio_duration > MAX_LONG_AUDIO_SECONDS:
        raise LtxWorkbenchError("单行上传音频最多支持 60 分钟")
    if video_duration + 0.05 < audio_duration:
        raise LtxWorkbenchError(
            f"源视频时长不足：视频 {video_duration:.1f} 秒，"
            f"音频 {audio_duration:.1f} 秒"
        )
    return video_duration, audio_duration


def validate_ltx_workbench_rows(
    db: Session,
    user: User,
    settings: Settings,
    raw_rows: Any,
) -> dict[str, Any]:
    ensure_user_can_create_workflow(user, LTX_WORKFLOW)
    rows = _clean_rows(raw_rows, settings)
    assets = _assets_for_rows(db, user, rows)
    summaries: list[dict[str, Any]] = []
    for row in rows:
        video = assets[row["video_asset_id"]]
        audio = assets[row["audio_asset_id"]]
        video_duration, audio_duration = _inspect_pair(
            settings, video, audio
        )
        summaries.append(
            {
                "row_id": row["row_id"],
                "script_length": len(row["script_text"]),
                "video": {
                    "asset_id": video.id,
                    "original_name": video.original_name,
                    "size_bytes": video.size_bytes,
                    "duration_seconds": round(video_duration, 3),
                },
                "audio": {
                    "asset_id": audio.id,
                    "original_name": audio.original_name,
                    "size_bytes": audio.size_bytes,
                    "duration_seconds": round(audio_duration, 3),
                },
            }
        )
    return {
        "schema": "runninghub.ltx-workbench-validation.v1",
        "valid": True,
        "row_count": len(summaries),
        "rows": summaries,
    }


def ltx_batch_query():
    return select(GenerationBatch).options(
        selectinload(GenerationBatch.user),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.ltx_preparation_job),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.long_audio_project),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.generation_task)
        .selectinload(GenerationTask.enhancement),
    )


def get_ltx_batch(
    db: Session, user: User, batch_id: str
) -> GenerationBatch | None:
    return db.scalar(
        ltx_batch_query().where(
            GenerationBatch.id == batch_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == LTX_WORKFLOW,
            GenerationBatch.source_channel == BATCH_SOURCE_LTX_WORKBENCH,
        )
    )


def get_ltx_item(
    db: Session, user: User, item_id: str
) -> GenerationBatchItem | None:
    batch = db.scalar(
        ltx_batch_query()
        .join(
            GenerationBatchItem,
            GenerationBatchItem.batch_id == GenerationBatch.id,
        )
        .where(
            GenerationBatchItem.id == item_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == LTX_WORKFLOW,
            GenerationBatch.source_channel == BATCH_SOURCE_LTX_WORKBENCH,
        )
    )
    if batch is None:
        return None
    return next((item for item in batch.items if item.id == item_id), None)


def create_ltx_workbench_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    name: str,
    request_key: str,
    correlation_id: str | None,
    raw_rows: Any,
) -> tuple[GenerationBatch, bool]:
    clean_request_key = request_key.strip()
    if not clean_request_key or len(clean_request_key) > 64:
        raise LtxWorkbenchError("request_key 长度必须为 1–64")
    existing = db.scalar(
        ltx_batch_query().where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == clean_request_key,
        )
    )
    if existing is not None:
        if (
            existing.workflow_type != LTX_WORKFLOW
            or existing.source_channel != BATCH_SOURCE_LTX_WORKBENCH
        ):
            raise LtxWorkbenchError("request_key 已被其他类型任务使用")
        return existing, False
    ensure_user_can_create_workflow(user, LTX_WORKFLOW)
    rows = _clean_rows(raw_rows, settings)
    assets = _assets_for_rows(db, user, rows)
    clean_name = name.strip() or "LTX 对口型批次"
    if len(clean_name) > 100:
        raise LtxWorkbenchError("批次名称不能超过 100 个字符")
    clean_correlation_id = (correlation_id or "").strip() or None
    if clean_correlation_id and len(clean_correlation_id) > 64:
        raise LtxWorkbenchError("correlation_id 不能超过 64 个字符")
    workflow_config = get_user_workflow_config(user, LTX_WORKFLOW)
    now = datetime.now(timezone.utc)
    batch = GenerationBatch(
        id=str(uuid.uuid4()),
        user=user,
        name=clean_name,
        workflow_type=LTX_WORKFLOW,
        source_channel=BATCH_SOURCE_LTX_WORKBENCH,
        correlation_id=clean_correlation_id,
        audio_mode="upload",
        review_required=False,
        video_review_required=False,
        request_key=clean_request_key,
        status="ACTIVE",
        total_items=len(rows),
    )
    created_directories: list[Path] = []
    try:
        for position, row in enumerate(rows, start=1):
            video_asset = assets[row["video_asset_id"]]
            audio_asset = assets[row["audio_asset_id"]]
            video_duration, audio_duration = _inspect_pair(
                settings, video_asset, audio_asset
            )
            project_id = str(uuid.uuid4())
            project_directory = long_audio_project_dir(
                settings, user.id, project_id
            )
            created_directories.append(project_directory)
            video_source = safe_relative_path(
                video_asset.relative_path, settings.data_dir
            )
            audio_source = safe_relative_path(
                audio_asset.relative_path, settings.data_dir
            )
            video_path = materialize_staged_asset(
                video_source, project_directory, kind="video"
            )
            audio_path = materialize_staged_asset(
                audio_source, project_directory, kind="audio"
            )
            video_relative = to_relative_data_path(video_path, settings)
            audio_relative = to_relative_data_path(audio_path, settings)
            item = GenerationBatchItem(
                id=str(uuid.uuid4()),
                batch=batch,
                row_number=position,
                row_key=row["row_id"],
                manifest_json=json.dumps(
                    {
                        "row_id": row["row_id"],
                        "speech_script": row["script_text"],
                        "source_video_asset_id": video_asset.id,
                        "source_video_file": video_asset.original_name,
                        "audio_asset_id": audio_asset.id,
                        "audio_file": audio_asset.original_name,
                    },
                    ensure_ascii=False,
                ),
                audio_status="ASR_PENDING",
                status="PREPARING_LTX",
                merged_video_status="NOT_APPLICABLE",
            )
            project = LongAudioProject(
                id=project_id,
                user=user,
                batch_item=item,
                name=f"{clean_name}-{row['row_id']}",
                workflow_type=LTX_WORKFLOW,
                review_required=False,
                script_text=row["script_text"],
                audio_path=audio_relative,
                audio_original_name=audio_asset.original_name,
                video_path=video_relative,
                video_original_name=video_asset.original_name,
                duration_seconds=audio_duration,
                parameters_json=json.dumps(
                    {
                        "prompt_prefix": FIXED_PROMPT_PREFIX,
                        "instance_type": workflow_config.instance_type,
                        "seedvr2_enabled": True,
                    },
                    ensure_ascii=False,
                ),
                alignment_provider="funasr_http",
                status=LongAudioProjectStatus.PENDING_ANALYSIS.value,
                expires_at=now + timedelta(days=settings.upload_retention_days),
            )
            preparation = LtxPreparationJob(
                id=str(uuid.uuid4()),
                user=user,
                batch_item=item,
                long_audio_project=project,
                idempotency_key=f"{clean_request_key}:{position}",
                source_video_path=video_relative,
                source_video_original_name=video_asset.original_name,
                source_video_sha256=_sha256_file(video_path),
                source_audio_path=audio_relative,
                source_audio_original_name=audio_asset.original_name,
                source_audio_sha256=_sha256_file(audio_path),
                script_text=row["script_text"],
                script_sha256=_sha256_text(row["script_text"]),
                duration_seconds=audio_duration,
                video_duration_seconds=video_duration,
                status=LtxPreparationStatus.ASR_PENDING.value,
            )
            db.add_all([item, project, preparation])
        for asset in assets.values():
            asset.consumed_at = now
        db.add(batch)
        db.commit()
    except Exception:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        raise
    created = get_ltx_batch(db, user, batch.id)
    if created is None:
        raise LtxWorkbenchError("LTX 批次创建后无法读取")
    return created, True


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def ltx_item_payload(item: GenerationBatchItem) -> dict[str, Any]:
    preparation = item.ltx_preparation_job
    project = item.long_audio_project
    segments: list[dict[str, Any]] = []
    for segment in sorted(item.segments, key=lambda value: value.segment_index):
        task = segment.generation_task
        enhancement = task.enhancement if task is not None else None
        enhancement_active = (
            enhancement is not None
            and enhancement.status
            not in {
                EnhancementStatus.SUCCESS.value,
                EnhancementStatus.FAILED.value,
                EnhancementStatus.DOWNLOAD_FAILED.value,
                EnhancementStatus.CANCELLED.value,
            }
        )
        segments.append(
            {
                "segment_id": segment.id,
                "index": segment.segment_index,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "script_text": segment.script_text,
                "status": (
                    "VIDEO_ENHANCING"
                    if enhancement_active
                    else task.status if task is not None else segment.status
                ),
                "runninghub_task_id": (
                    task.runninghub_task_id if task is not None else None
                ),
                "error_code": task.error_code if task is not None else segment.error_code,
                "error_message": (
                    task.error_message if task is not None else segment.error_message
                ),
                "failed_reason": (
                    _json_object(task.runninghub_failed_reason)
                    if task is not None
                    else None
                ),
                "usage": (
                    _json_value(task.runninghub_usage) if task is not None else None
                ),
                "seedvr2_task_id": (
                    enhancement.remote_task_id if enhancement is not None else None
                ),
                "seedvr2_status": (
                    enhancement.status if enhancement is not None else None
                ),
                "seedvr2_usage": (
                    _json_value(enhancement.usage_json)
                    if enhancement is not None
                    else None
                ),
                "seedvr2_failed_reason": (
                    _json_object(enhancement.failed_reason_json)
                    if enhancement is not None
                    else None
                ),
                "source_video_url": (
                    f"/api/workbench/ltx-items/{item.id}/segments/"
                    f"{segment.segment_index}/source-video"
                    if enhancement is not None and enhancement.source_result_path
                    else None
                ),
                "video_url": (
                    f"/api/workbench/ltx-items/{item.id}/segments/"
                    f"{segment.segment_index}/video"
                    if task is not None and task.result_path
                    else None
                ),
            }
        )
    return {
        "item_id": item.id,
        "row_id": item.row_key,
        "status": item.status,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "preparation": (
            {
                "id": preparation.id,
                "status": preparation.status,
                "audio_duration_seconds": preparation.duration_seconds,
                "video_duration_seconds": preparation.video_duration_seconds,
                "alignment_provider": preparation.alignment_provider,
                "match_ratio": preparation.alignment_score,
                "aligned_script": _json_object(
                    preparation.alignment_timeline_json
                ),
                "error_code": preparation.error_code,
                "error_message": preparation.error_message,
            }
            if preparation is not None
            else None
        ),
        "media_status": project.status if project is not None else None,
        "segments": segments,
        "merge_status": item.merged_video_status,
        "merge_error": item.merged_video_error,
        "base_video_url": (
            f"/api/workbench/ltx-items/{item.id}/base-video"
            if item.merged_video_path
            else None
        ),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def ltx_batch_payload(batch: GenerationBatch) -> dict[str, Any]:
    return {
        "schema": "runninghub.ltx-workbench-batch.v1",
        "batch_id": batch.id,
        "name": batch.name,
        "workflow_type": batch.workflow_type,
        "source_channel": batch.source_channel,
        "correlation_id": batch.correlation_id,
        "status": batch.status,
        "total_items": batch.total_items,
        "items": [ltx_item_payload(item) for item in batch.items],
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
    }


__all__ = [
    "FIXED_PROMPT_PREFIX",
    "LtxWorkbenchError",
    "compile_ltx_prompt",
    "create_ltx_workbench_batch",
    "get_ltx_batch",
    "get_ltx_item",
    "ltx_batch_payload",
    "ltx_item_payload",
    "validate_ltx_workbench_rows",
]
