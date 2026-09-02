from __future__ import annotations

from app.services.device_auth.admission import WorkbenchIdentity, require_new_work
from app.services.device_auth.queued_work import bind_new_operation

import hashlib
import json
import math
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import (
    BATCH_SOURCE_H3_WORKBENCH,
    BATCH_SOURCE_NEW_WORKBENCH,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    H3BatchConfig,
    H3ItemConfig,
    H3RemoteAsrJob,
    H3RemoteAsrJobStatus,
    H3SegmentConfig,
    RunningHubExecutionAccount,
    StagedAsset,
    TaskStatus,
    User,
)
from app.services.audio import inspect_audio_duration
from app.services.audio_review import current_attempt
from app.services.batch_assets import load_available_assets
from app.services.h3.duration import plan_h3_duration
from app.services.h3.quote_lifecycle import (
    claim_quote, finish_quote_cancellation, quote_capability, quote_token,
)
from app.services.h3.delivery import parse_h3_direct_delivery
from app.services.h3.graph import (
    H3_ADAPTER_VERSION,
    H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256,
)
from app.services.h3.motion_references import (
    H3_MOTION_ASSIGNMENT_VERSION,
    H3_MOTION_CLIP_SECONDS,
    H3MotionReference,
    assign_h3_motion_references,
    split_h3_motion_reference,
)
from app.services.h3.prompt import (
    H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION,
    H3_MANUAL_PROMPT_OVERRIDE_VERSION,
    H3_PROMPT_TEMPLATE_VERSION,
    normalize_h3_prompt_override,
)
from app.services.h3.remote_asr import (
    H3_ASR_ACTION_AUDIO_ALIGNMENT,
    H3_SAFE_CUT_ALIGNMENT_SCHEMA,
    aligned_tokens_from_safe_cut,
    h3_audio_alignment_result_payload,
    normalize_h3_audio_alignment_result,
)
from app.services.h3.postprocess import (
    H3_OUTPUT_CONTRACT_VERSION,
    H3PostprocessError,
    extract_reference_frame,
)
from app.services.h3.segmentation import (
    H3_SEGMENTER_VERSION,
    H3TimestampedSegment,
    plan_h3_aligned_segments,
    plan_h3_timestamped_segments,
)
from app.services.h3_pool import (
    H3PoolValidationError,
    h3_execution_account_summary,
    refresh_h3_execution_account_balances,
    validate_h3_account_selection,
)
from app.services.workflow_configs import get_system_workflow_config
from app.services.alignment import get_alignment_provider
from app.services.media_segmentation import MediaSegmentationError
from app.services.media_segmentation import cut_audio_segment
from app.services.runninghub_dispatch import task_uses_execution_pool
from app.services.speech.async_outputs import (
    SubtitleCue,
    dump_subtitle_cues,
    load_subtitle_cues,
)
from app.services.storage import (
    materialize_staged_asset,
    remove_directory,
    safe_relative_path,
    task_output_dir,
    to_relative_data_path,
)
from app.services.task_management import (
    RETRYABLE_TASK_STATUSES,
    TaskManagementError,
    prepare_task_retry,
)
from app.services.task_cancellation import (
    TaskCancellationError,
    cancel_generation_task,
)
from app.workflows.base import WorkflowAsset
from app.workflows.h3_ref2va import (
    H3_CONTINUITY_MODES,
    H3_DEFAULT_ASPECT_RATIO,
    H3_DEFAULT_CONTINUITY_MODE,
    H3_DEFAULT_GENERATION_TAIL_SECONDS,
    H3_DEFAULT_MEGAPIXELS,
    H3_DEFAULT_MULTIPLE,
    H3Ref2VAWorkflow,
)


H3_WORKFLOW = "minimax_h3_ref2va"
H3_BATCH_SCHEMA = "jyd.h3-generation-batch.v1"
H3_PROMPT_PROFILE_ID = "dual_reference_talking_v2"
H3_MANUAL_PROMPT_PROFILE_ID = "manual_prompt_override_v1"
H3_RAW_CUES_VERSION = "minimax.raw-cues.v1"
H3_UPLOADED_AUDIO_CUES_VERSION = "funasr.aligned-cues.v1"
H3_UPLOADED_AUDIO_BATCH_MARKER = "uploaded-audio"
H3_REGENERATION_WAITING = "WAITING_REGENERATION_DEPENDENCY"
H3_CANCELLABLE_TASK_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
    H3_REGENERATION_WAITING,
}
H3_REUSABLE_AUDIO_STATUSES = {
    AudioTaskStatus.AWAITING_REVIEW.value,
    AudioTaskStatus.SUCCESS.value,
}


def _h3_audio_source_is_reusable(task: AudioGenerationTask) -> bool:
    """Keep reviewed MiniMax masters reusable without reopening voice review."""

    if task.status == AudioTaskStatus.AWAITING_REVIEW.value:
        return task.reviewed_at is None
    return (
        task.status == AudioTaskStatus.SUCCESS.value
        and task.reviewed_at is not None
    )


def _plan_h3_audio_segments(
    script_text: str,
    cues: list[object],
    audio_source: Path,
    audio_duration: float,
    *,
    generation_tail_seconds: float,
    client_alignment: dict[str, object] | None = None,
    audio_sha256: str = "",
    allow_server_asr: bool = True,
) -> list[H3TimestampedSegment]:
    """Use MiniMax cues first, then repair overlong cues with FunASR."""

    if client_alignment is not None:
        if str(client_alignment.get("audio_sha256") or "") != audio_sha256:
            raise H3WorkbenchError("JYD 本地 ASR 切点与当前 MiniMax 音频不一致")
        aligned_tokens = aligned_tokens_from_safe_cut(script_text, client_alignment)
        try:
            return plan_h3_aligned_segments(
                script_text,
                aligned_tokens,
                audio_duration,
                generation_tail_seconds=generation_tail_seconds,
            )
        except (MediaSegmentationError, ValueError) as exc:
            raise H3WorkbenchError(
                f"ASR 切点无法形成 H3 分段：{exc}"
            ) from exc

    if cues:
        try:
            return plan_h3_timestamped_segments(
                script_text,
                cues,
                audio_duration,
                generation_tail_seconds=generation_tail_seconds,
            )
        except ValueError as raw_error:
            if "无法在 4～15 秒请求窗口内安全分段" not in str(raw_error):
                raise
    if not allow_server_asr:
        raise H3WorkbenchError("H3 音频需要先由远程媒体节点完成 ASR 对齐")
    try:
        alignment = get_alignment_provider("funasr_http").align(
            audio_source,
            script_text,
        )
        if not alignment.tokens:
            raise H3WorkbenchError("FunASR 未返回字词时间戳")
        return plan_h3_aligned_segments(
            script_text,
            alignment.tokens,
            audio_duration,
            generation_tail_seconds=generation_tail_seconds,
        )
    except (MediaSegmentationError, ValueError) as exc:
        raise H3WorkbenchError(
            "音频与原稿无法建立 H3 安全切点："
            f"{exc}"
        ) from exc


def _clean_client_audio_alignment(
    raw: object,
    *,
    position: int,
    script_text: str,
    audio_batch_id: str,
    audio_item_id: str,
    audio_version: int,
) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 切点格式错误")
    if str(raw.get("schema") or "") != H3_SAFE_CUT_ALIGNMENT_SCHEMA:
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 切点版本不受支持")
    script_sha = str(raw.get("script_sha256") or "").lower()
    audio_sha = str(raw.get("audio_sha256") or "").lower()
    if script_sha != _sha256_text(script_text):
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 切点与冻结原稿不一致")
    if len(audio_sha) != 64 or any(value not in "0123456789abcdef" for value in audio_sha):
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 音频摘要无效")
    try:
        bound_version = int(raw.get("audio_generation_version") or 0)
    except (TypeError, ValueError) as exc:
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 声音版本无效") from exc
    if (
        str(raw.get("audio_batch_id") or "") != audio_batch_id
        or str(raw.get("audio_item_id") or "") != audio_item_id
        or bound_version != audio_version
    ):
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 切点绑定了另一份声音")
    raw_ranges = raw.get("ranges")
    if not isinstance(raw_ranges, list) or not 2 <= len(raw_ranges) <= 10_000:
        raise H3WorkbenchError(f"第 {position} 行本地 ASR 切点数量无效")
    ranges: list[dict[str, int]] = []
    previous_script_end = -1
    previous_audio_end = -1
    for range_position, value in enumerate(raw_ranges, start=1):
        if not isinstance(value, dict):
            raise H3WorkbenchError(
                f"第 {position} 行本地 ASR 第 {range_position} 个切点无效"
            )
        try:
            script_start = int(value["script_start"])
            script_end = int(value["script_end"])
            start_us = int(value["start_us"])
            end_us = int(value["end_us"])
        except (KeyError, TypeError, ValueError) as exc:
            raise H3WorkbenchError(
                f"第 {position} 行本地 ASR 第 {range_position} 个切点无效"
            ) from exc
        if (
            script_start < 0
            or script_end <= script_start
            or script_end > len(script_text)
            or not script_text[script_start:script_end].strip()
            or start_us < 0
            or end_us <= start_us
            or script_start < previous_script_end
            or start_us + 250_000 < previous_audio_end
        ):
            raise H3WorkbenchError(
                f"第 {position} 行本地 ASR 第 {range_position} 个切点无效"
            )
        ranges.append(
            {
                "script_start": script_start,
                "script_end": script_end,
                "start_us": start_us,
                "end_us": end_us,
            }
        )
        previous_script_end = script_end
        previous_audio_end = end_us
    return {
        "schema": H3_SAFE_CUT_ALIGNMENT_SCHEMA,
        "source": str(raw.get("source") or "client_funasr")[:100],
        "script_sha256": script_sha,
        "audio_sha256": audio_sha,
        "audio_batch_id": audio_batch_id,
        "audio_item_id": audio_item_id,
        "audio_generation_version": audio_version,
        "ranges": ranges,
    }


def _plan_uploaded_h3_audio(
    script_text: str,
    audio_source: Path,
    audio_duration: float,
    *,
    generation_tail_seconds: float,
    client_alignment: dict[str, object] | None,
    audio_sha256: str,
    allow_server_asr: bool,
) -> tuple[list[H3TimestampedSegment], str]:
    """Align one uploaded final audio track to its immutable source script."""

    try:
        if client_alignment is not None:
            if str(client_alignment.get("audio_sha256") or "") != audio_sha256:
                raise H3WorkbenchError("ASR 切点与当前上传音频不一致")
            tokens = aligned_tokens_from_safe_cut(script_text, client_alignment)
        elif allow_server_asr:
            alignment = get_alignment_provider("funasr_http").align(
                audio_source, script_text
            )
            tokens = list(alignment.tokens)
        else:
            raise H3WorkbenchError("上传音频需要先由远程媒体节点完成 ASR 对齐")
        if not tokens:
            raise H3WorkbenchError("FunASR 未返回字词时间戳")
        plans = plan_h3_aligned_segments(
            script_text,
            tokens,
            audio_duration,
            generation_tail_seconds=generation_tail_seconds,
        )
    except (MediaSegmentationError, ValueError) as exc:
        raise H3WorkbenchError(f"上传音频与原稿无法安全对齐：{exc}") from exc
    cues = [
        SubtitleCue(
            text=token.text,
            start_seconds=token.start_seconds,
            end_seconds=token.end_seconds,
        )
        for token in tokens
    ]
    return plans, dump_subtitle_cues(cues)
H3_ACTIVE_TASK_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
    H3_REGENERATION_WAITING,
}
_FORBIDDEN_FIELDS = {
    "prompt",
    "workflow",
    "workflow_json",
    "nodeInfoList",
    "node_info_list",
}


class H3WorkbenchError(ValueError):
    pass


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_h3_audio_alignment_job(
    db: Session,
    user: User,
    settings: Settings,
    *,
    audio_asset_id: str,
    script_text: str,
) -> tuple[H3RemoteAsrJob, bool]:
    """Create or reuse one immutable preflight alignment job for the H3 page."""

    clean_asset_id = str(audio_asset_id or "").strip()
    clean_script = str(script_text or "").strip()
    if not clean_asset_id:
        raise H3WorkbenchError("H3 音频素材 ID 不能为空")
    if not clean_script or len(clean_script) > 100_000:
        raise H3WorkbenchError("H3 音频原稿长度必须为 1–100000")
    assets = load_available_assets(db, user, [clean_asset_id])
    asset = assets[0]
    if asset.kind != "audio":
        raise H3WorkbenchError("H3 对齐任务只能使用音频素材")
    source = safe_relative_path(asset.relative_path, settings.data_dir)
    if not source.is_file():
        raise H3WorkbenchError("H3 对齐音频文件不存在")
    audio_sha = _sha256_file(source)
    script_sha = _sha256_text(clean_script)
    idempotency_sha = _sha256_text(
        _canonical_json(
            {
                "action": H3_ASR_ACTION_AUDIO_ALIGNMENT,
                "user_id": user.id,
                "audio_asset_id": asset.id,
                "audio_sha256": audio_sha,
                "script_sha256": script_sha,
            }
        )
    )
    existing = db.scalar(
        select(H3RemoteAsrJob).where(
            H3RemoteAsrJob.idempotency_sha256 == idempotency_sha
        )
    )
    if existing is not None:
        if existing.status == H3RemoteAsrJobStatus.FAILED.value:
            existing.status = H3RemoteAsrJobStatus.PENDING.value
            existing.result_json = None
            existing.error_code = None
            existing.error_message = None
            existing.completed_at = None
            existing.remote_worker_id = None
            existing.remote_lease_id = None
            existing.remote_lease_expires_at = None
            existing.remote_last_heartbeat_at = None
            db.commit()
            db.refresh(existing)
        return existing, False

    job = H3RemoteAsrJob(
        id=str(uuid.uuid4()),
        user_id=user.id,
        generation_task_id=None,
        staged_asset_id=asset.id,
        action=H3_ASR_ACTION_AUDIO_ALIGNMENT,
        idempotency_sha256=idempotency_sha,
        source_path=asset.relative_path,
        source_name=asset.original_name,
        source_sha256=audio_sha,
        script_text=clean_script,
        script_sha256=script_sha,
        audio_batch_id=H3_UPLOADED_AUDIO_BATCH_MARKER,
        audio_item_id=asset.id,
        audio_generation_version=0,
        status=H3RemoteAsrJobStatus.PENDING.value,
    )
    db.add(job)
    db.flush()
    if settings.media_processing_mode != "remote":
        try:
            alignment = get_alignment_provider("funasr_http").align(
                source, clean_script
            )
            normalized = normalize_h3_audio_alignment_result(
                h3_audio_alignment_result_payload(alignment),
                script_text=job.script_text,
                script_sha256=job.script_sha256,
                audio_sha256=job.source_sha256,
                audio_batch_id=str(job.audio_batch_id),
                audio_item_id=str(job.audio_item_id),
                audio_generation_version=int(job.audio_generation_version or 0),
            )
            job.result_json = json.dumps(normalized, ensure_ascii=False)
            job.status = H3RemoteAsrJobStatus.SUCCESS.value
            job.completed_at = datetime.now(timezone.utc)
        except (MediaSegmentationError, ValueError, OSError) as exc:
            job.status = H3RemoteAsrJobStatus.FAILED.value
            job.error_code = "LOCAL_ASR_FAILED"
            job.error_message = str(exc)[:4000]
            job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job, True


def h3_audio_alignment_job_payload(job: H3RemoteAsrJob) -> dict[str, object]:
    if job.action != H3_ASR_ACTION_AUDIO_ALIGNMENT:
        raise H3WorkbenchError("H3 音频对齐任务类型不匹配")
    alignment = None
    if job.status == H3RemoteAsrJobStatus.SUCCESS.value:
        try:
            alignment = json.loads(job.result_json or "null")
        except json.JSONDecodeError as exc:
            raise H3WorkbenchError("H3 音频对齐结果损坏") from exc
        if not isinstance(alignment, dict):
            raise H3WorkbenchError("H3 音频对齐结果损坏")
    return {
        "job_id": job.id,
        "status": job.status,
        "alignment": alignment,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retry_after_seconds": (
            2
            if job.status
            in {
                H3RemoteAsrJobStatus.PENDING.value,
                H3RemoteAsrJobStatus.RUNNING.value,
            }
            else None
        ),
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _strict_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise H3WorkbenchError(f"{field}必须是布尔值")


def _clean_resolution(
    value: object,
    *,
    fallback: dict[str, object] | None = None,
) -> dict[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise H3WorkbenchError("H3 分辨率参数必须是对象")
    base = fallback or {
        "aspect_ratio": H3_DEFAULT_ASPECT_RATIO,
        "megapixels": H3_DEFAULT_MEGAPIXELS,
        "multiple": H3_DEFAULT_MULTIPLE,
    }
    aspect_ratio = str(value.get("aspect_ratio") or base["aspect_ratio"]).strip()
    try:
        megapixels = float(value.get("megapixels", base["megapixels"]))
    except (TypeError, ValueError) as exc:
        raise H3WorkbenchError("H3 megapixels 参数不合法") from exc
    multiple = value.get("multiple", base["multiple"])
    if (
        not aspect_ratio
        or len(aspect_ratio) > 100
        or not math.isfinite(megapixels)
        or not 0.2 <= megapixels <= 2.0
        or isinstance(multiple, bool)
        or multiple != 32
    ):
        raise H3WorkbenchError("H3 分辨率参数不合法")
    return {
        "aspect_ratio": aspect_ratio,
        "megapixels": megapixels,
        "multiple": 32,
    }


def _clean_defaults(value: object) -> dict[str, object]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise H3WorkbenchError("H3 批次默认参数必须是对象")
    if _FORBIDDEN_FIELDS.intersection(value):
        raise H3WorkbenchError(
            "H3 工作流 JSON 或 prompt 字段不受支持；人工总体提示词请使用 prompt_override"
        )
    continuity_mode = str(
        value.get("continuity_mode") or H3_DEFAULT_CONTINUITY_MODE
    ).strip()
    if continuity_mode not in H3_CONTINUITY_MODES:
        raise H3WorkbenchError("H3 连续性模式不合法")
    user_direction = str(value.get("user_direction") or "").strip()
    if len(user_direction) > 1000:
        raise H3WorkbenchError("H3 用户补充方向不能超过 1000 个字符")
    try:
        prompt_override = normalize_h3_prompt_override(
            value.get("prompt_override")
        )
    except ValueError as exc:
        raise H3WorkbenchError(str(exc)) from exc
    if prompt_override:
        user_direction = ""
    try:
        tail = float(
            value.get(
                "generation_tail_seconds",
                H3_DEFAULT_GENERATION_TAIL_SECONDS,
            )
        )
    except (TypeError, ValueError) as exc:
        raise H3WorkbenchError("H3 生成尾部余量不合法") from exc
    if not math.isfinite(tail) or not 0 <= tail <= 1:
        raise H3WorkbenchError("H3 生成尾部余量必须在 0 到 1 秒之间")
    return {
        "prompt_profile_id": (
            H3_MANUAL_PROMPT_PROFILE_ID
            if prompt_override
            else H3_PROMPT_PROFILE_ID
        ),
        "prompt_template_version": (
            H3_MANUAL_PROMPT_OVERRIDE_VERSION
            if prompt_override
            else (
                H3_LOOP_ANCHOR_PROMPT_TEMPLATE_VERSION
                if continuity_mode == "loop_anchor"
                else H3_PROMPT_TEMPLATE_VERSION
            )
        ),
        "prompt_override": prompt_override,
        "user_direction": user_direction,
        "continuity_mode": continuity_mode,
        "resolution": _clean_resolution(value.get("resolution")),
        "generation_tail_seconds": tail,
    }


def _clean_rows(
    raw_rows: object,
    settings: Settings,
    defaults: dict[str, object],
) -> list[dict[str, object]]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise H3WorkbenchError("请至少提交一行 H3 任务")
    if len(raw_rows) > settings.max_batch_items:
        raise H3WorkbenchError(f"单批任务数量不能超过 {settings.max_batch_items}")
    rows = []
    seen_rows: set[str] = set()
    seen_audio_sources: set[str] = set()
    for position, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise H3WorkbenchError(f"第 {position} 行格式错误")
        if _FORBIDDEN_FIELDS.intersection(raw):
            raise H3WorkbenchError(
                "H3 工作流 JSON 或 prompt 字段不受支持；人工总体提示词请使用 prompt_override"
            )
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
        audio_batch_id = str(
            raw.get("audio_batch_id") or raw.get("audioBatchId") or ""
        ).strip()
        audio_item_id = str(
            raw.get("audio_item_id") or raw.get("audioItemId") or ""
        ).strip()
        raw_reference_image_ids = raw.get(
            "reference_image_asset_ids",
            raw.get("referenceImageAssetIds", []),
        )
        if not isinstance(raw_reference_image_ids, list) or len(raw_reference_image_ids) > 4:
            raise H3WorkbenchError(f"第 {position} 行人物参考图必须为 0～4 张")
        reference_image_ids = [
            str(value or "").strip() for value in raw_reference_image_ids
        ]
        if any(not value for value in reference_image_ids) or len(
            set(reference_image_ids)
        ) != len(reference_image_ids):
            raise H3WorkbenchError(f"第 {position} 行人物参考图 ID 不能为空或重复")
        try:
            audio_version = int(
                raw.get("audio_generation_version")
                or raw.get("audioGenerationVersion")
                or 0
            )
        except (TypeError, ValueError) as exc:
            raise H3WorkbenchError(f"第 {position} 行声音版本格式错误") from exc
        if not row_id or len(row_id) > 100 or row_id in seen_rows:
            raise H3WorkbenchError(f"第 {position} 行任务 ID 缺失、过长或重复")
        if not script_text or len(script_text) > 100_000:
            raise H3WorkbenchError(f"第 {position} 行表格原稿长度必须为 1–100000")
        if not video_asset_id:
            raise H3WorkbenchError(f"第 {position} 行必须绑定 H3 参考视频")
        has_uploaded_audio = bool(audio_asset_id)
        has_generated_audio = bool(
            audio_batch_id and audio_item_id and audio_version >= 1
        )
        if has_uploaded_audio == has_generated_audio:
            raise H3WorkbenchError(
                f"第 {position} 行必须且只能绑定一份上传音频或已生成音频"
            )
        raw_audio_alignment = raw.get(
            "audio_alignment", raw.get("audioAlignment")
        )
        bound_audio_batch_id = (
            H3_UPLOADED_AUDIO_BATCH_MARKER if has_uploaded_audio else audio_batch_id
        )
        bound_audio_item_id = audio_asset_id if has_uploaded_audio else audio_item_id
        bound_audio_version = 0 if has_uploaded_audio else audio_version
        audio_alignment = _clean_client_audio_alignment(
            raw_audio_alignment,
            position=position,
            script_text=script_text,
            audio_batch_id=bound_audio_batch_id,
            audio_item_id=bound_audio_item_id,
            audio_version=bound_audio_version,
        )
        audio_source_key = audio_asset_id or audio_item_id
        if audio_source_key in seen_audio_sources:
            raise H3WorkbenchError(
                "同一 MiniMax 音频行不能在一个 H3 批次中重复绑定"
                if has_generated_audio
                else "同一上传音频不能在一个 H3 批次中重复绑定"
            )
        overrides = raw.get("overrides") or {}
        if not isinstance(overrides, dict) or _FORBIDDEN_FIELDS.intersection(overrides):
            raise H3WorkbenchError(f"第 {position} 行覆盖参数不合法")
        continuity = str(
            overrides.get("continuity_mode")
            or defaults["continuity_mode"]
        ).strip()
        if continuity not in H3_CONTINUITY_MODES:
            raise H3WorkbenchError(f"第 {position} 行连续性模式不合法")
        user_direction = ""
        if not defaults["prompt_override"]:
            user_direction = (
                str(overrides["user_direction"]).strip()
                if overrides.get("user_direction") is not None
                else str(defaults["user_direction"])
            )
        if len(user_direction) > 1000:
            raise H3WorkbenchError(f"第 {position} 行补充方向不能超过 1000 个字符")
        resolution_override = {
            key: overrides[key]
            for key in ("aspect_ratio", "megapixels", "multiple")
            if overrides.get(key) is not None
        }
        resolution = _clean_resolution(
            resolution_override,
            fallback=defaults["resolution"],
        )
        seen_rows.add(row_id)
        seen_audio_sources.add(audio_source_key)
        rows.append(
            {
                "row_id": row_id,
                "script_text": script_text,
                "video_asset_id": video_asset_id,
                "audio_asset_id": audio_asset_id,
                "audio_batch_id": audio_batch_id,
                "audio_item_id": audio_item_id,
                "audio_generation_version": audio_version,
                "audio_alignment": audio_alignment,
                "reference_image_asset_ids": reference_image_ids,
                "user_direction": user_direction,
                "continuity_mode": continuity,
                "resolution": resolution,
            }
        )
    return rows


def _audio_task_for_row(
    db: Session,
    user: User,
    row: dict[str, object],
) -> AudioGenerationTask:
    item = db.scalar(
        select(GenerationBatchItem)
        .join(GenerationBatch, GenerationBatchItem.batch_id == GenerationBatch.id)
        .options(selectinload(GenerationBatchItem.audio_task))
        .where(
            GenerationBatchItem.id == row["audio_item_id"],
            GenerationBatch.id == row["audio_batch_id"],
            GenerationBatch.user_id == user.id,
            GenerationBatch.source_channel == BATCH_SOURCE_NEW_WORKBENCH,
        )
    )
    task = item.audio_task if item is not None else None
    if task is None:
        raise H3WorkbenchError("MiniMax 声音任务不存在或不属于当前账号")
    if task.generation_version != row["audio_generation_version"]:
        raise H3WorkbenchError("MiniMax 声音版本已经变化，请刷新后重新准备")
    if not _h3_audio_source_is_reusable(task):
        raise H3WorkbenchError("MiniMax 声音尚未审核完成或已不再可复用")
    if not task.output_path or not task.subtitle_path:
        raise H3WorkbenchError("MiniMax 声音或 raw cues 尚未准备完成")
    try:
        source_manifest = json.loads(item.manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise H3WorkbenchError("MiniMax 声音原稿快照损坏") from exc
    source_script = str(source_manifest.get("speech_script") or "").strip()
    if source_script != row["script_text"]:
        raise H3WorkbenchError("H3 表格原稿与所选 MiniMax 声音原稿不一致")
    return task


def approve_h3_audio_source(
    db: Session,
    user: User,
    *,
    audio_batch_id: str,
    audio_item_id: str,
    audio_generation_version: int,
) -> AudioGenerationTask:
    """Approve one MiniMax master for H3 without starting the 4A pipeline."""

    item = db.scalar(
        select(GenerationBatchItem)
        .join(GenerationBatch, GenerationBatchItem.batch_id == GenerationBatch.id)
        .options(selectinload(GenerationBatchItem.audio_task))
        .where(
            GenerationBatchItem.id == str(audio_item_id or "").strip(),
            GenerationBatch.id == str(audio_batch_id or "").strip(),
            GenerationBatch.user_id == user.id,
            GenerationBatch.source_channel == BATCH_SOURCE_NEW_WORKBENCH,
        )
    )
    task = item.audio_task if item is not None else None
    if task is None:
        raise H3WorkbenchError("MiniMax 声音任务不存在或不属于当前账号")
    if task.generation_version != int(audio_generation_version):
        raise H3WorkbenchError("MiniMax 声音版本已经变化，请刷新后重新审核")
    if not task.output_path or not task.subtitle_path:
        raise H3WorkbenchError("MiniMax 声音或 raw cues 尚未准备完成")
    # _audio_task_for_row also protects ownership and generation version, but its
    # script check needs the immutable source text rather than a client value.
    try:
        source_manifest = json.loads(item.manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise H3WorkbenchError("MiniMax 声音原稿快照损坏") from exc
    source_script = str(source_manifest.get("speech_script") or "").strip()
    if not source_script:
        raise H3WorkbenchError("MiniMax 声音原稿快照为空")

    if task.status == AudioTaskStatus.SUCCESS.value and task.reviewed_at is not None:
        return task
    if task.status != AudioTaskStatus.AWAITING_REVIEW.value:
        raise H3WorkbenchError("当前 MiniMax 声音不在 H3 审核阶段")
    attempt = current_attempt(task)
    attempt.status = "APPROVED"
    task.reviewed_at = datetime.now(timezone.utc)
    task.status = AudioTaskStatus.SUCCESS.value
    task.batch_item.audio_status = "AUDIO_APPROVED_H3"
    task.batch_item.status = "AUDIO_APPROVED_H3"
    db.commit()
    return task


def _available_assets(
    db: Session,
    user: User,
    image_ids: list[str],
    rows: list[dict[str, object]],
) -> dict[str, StagedAsset]:
    all_ids = image_ids + [str(row["video_asset_id"]) for row in rows]
    all_ids.extend(
        str(row["audio_asset_id"])
        for row in rows
        if row.get("audio_asset_id")
    )
    if len(set(all_ids)) != len(all_ids):
        raise H3WorkbenchError("H3 图片和逐行视频素材 ID 不能重复占用")
    assets = load_available_assets(db, user, all_ids)
    result = {asset.id: asset for asset in assets}
    for asset_id in image_ids:
        if result[asset_id].kind != "image":
            raise H3WorkbenchError("H3 批次人物参考图素材类型错误")
    for position, row in enumerate(rows, start=1):
        if result[str(row["video_asset_id"])].kind != "video":
            raise H3WorkbenchError(f"第 {position} 行 H3 参考视频素材类型错误")
        audio_asset_id = str(row.get("audio_asset_id") or "")
        if audio_asset_id and result[audio_asset_id].kind != "audio":
            raise H3WorkbenchError(f"第 {position} 行 H3 音频素材类型错误")
    return result


def _selected_instance_type(
    db: Session,
    account_ids: list[int],
) -> str:
    del account_ids
    config = get_system_workflow_config(db, "minimax_h3_ref2va")
    if not config.is_enabled or not config.ai_app_id:
        raise H3WorkbenchError("H3 系统工作流未启用或未配置")
    return config.instance_type


def _batch_directory(settings: Settings, user_id: int, batch_id: str) -> Path:
    return settings.data_dir / "h3-workbench" / str(user_id) / batch_id


def _content_digest(value: object) -> str:
    return _sha256_text(_canonical_json(value))


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def h3_segment_video_delivery(
    segment: GenerationSegment,
) -> dict[str, Any] | None:
    """Describe the current result without requiring a server-owned MP4."""

    task = segment.generation_task
    config = segment.h3_config
    if (
        task is None
        or config is None
        or task.status != TaskStatus.SUCCESS.value
        or config.invalidated_at is not None
    ):
        return None
    direct = parse_h3_direct_delivery(task.output_metadata)
    if direct is not None:
        return {
            "mode": direct["mode"],
            "download_url": direct["download_url"],
            "result_signature": direct["result_signature"],
            "provider_task_id": direct.get("provider_task_id"),
            "received_at": direct.get("received_at"),
        }
    if not config.normalized_video_path:
        return None
    result_signature = str(config.normalized_video_sha256 or "").strip().lower()
    if not result_signature:
        result_signature = _content_digest(
            {
                "segment_id": segment.id,
                "normalized_video_path": config.normalized_video_path,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            }
        )
    return {
        "mode": "auth_center",
        "download_url": f"/api/workbench/h3-segments/{segment.id}/video",
        "result_signature": result_signature,
        "provider_task_id": task.runninghub_task_id,
        "received_at": task.completed_at.isoformat() if task.completed_at else None,
    }


def refresh_h3_segment_video_delivery(
    db: Session,
    user: User,
    segment: GenerationSegment,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Refresh one signed provider URL without submitting a new paid task."""

    task = segment.generation_task
    current = h3_segment_video_delivery(segment)
    if (
        task is None
        or task.user_id != user.id
        or not task.runninghub_task_id
        or current is None
        or current.get("mode") != "runninghub_direct"
    ):
        raise H3WorkbenchError("当前 H3 分段没有可刷新的直达结果")

    from app.services.h3.delivery import build_h3_direct_output_metadata
    from app.services.runninghub import RunningHubClient, RunningHubError
    from app.services.runninghub_attempts import task_execution_account_for_remote
    from app.services.security import decrypt_secret
    from app.workflows import get_workflow

    account = task_execution_account_for_remote(task) or user.runninghub_config
    if account is None or not account.api_key_encrypted:
        raise H3WorkbenchError("H3 执行账号已不可用，无法刷新交付地址")
    workflow_config = get_system_workflow_config(db, H3_WORKFLOW)
    workflow = get_workflow(H3_WORKFLOW)
    try:
        client = RunningHubClient(
            api_key=decrypt_secret(account.api_key_encrypted),
            base_url=account.base_url,
            ai_app_id=workflow_config.ai_app_id,
            submission_type=workflow.submission_type,
        )
        result = client.query_task(str(task.runninghub_task_id))
    except (RunningHubError, ValueError) as exc:
        raise H3WorkbenchError("RunningHub H3 交付地址刷新失败，请稍后重试") from exc
    if str(result.get("status") or "").upper() != "SUCCESS":
        raise H3WorkbenchError("RunningHub H3 结果当前不可下载，请稍后重试")
    output = workflow.select_output(task, result)
    if output is None:
        raise H3WorkbenchError("RunningHub H3 结果缺少可下载视频")
    task.output_metadata = json.dumps(
        build_h3_direct_output_metadata(
            provider_task_id=task.runninghub_task_id,
            provider_url=output.url,
            output_type=output.extension,
            source_metadata=output.metadata,
            received_at=datetime.now(timezone.utc),
            allowed_hosts=settings.h3_provider_allowed_hosts,
            resolve_dns=settings.app_env != "test",
        ),
        ensure_ascii=False,
    )
    db.commit()
    refreshed = h3_segment_video_delivery(segment)
    if refreshed is None:
        raise H3WorkbenchError("RunningHub H3 交付地址刷新后仍不可用")
    return refreshed


def _h3_segment_has_current_result(segment: GenerationSegment) -> bool:
    return h3_segment_video_delivery(segment) is not None


def _h3_segment_output_value(segment: GenerationSegment, key: str) -> object:
    task = segment.generation_task
    if task is None:
        return None
    return _json_object(task.output_metadata).get(key)


def _archive_h3_task_result(
    task: GenerationTask,
    segment: GenerationSegment,
    settings: Settings,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Move owned outputs into immutable history before a paid regeneration."""

    config = segment.h3_config
    if config is None:
        raise H3WorkbenchError("H3 当前成功分段缺少结果配置，不能主动重生成")
    direct = parse_h3_direct_delivery(task.output_metadata)
    if direct is not None:
        payload = _json_object(task.input_payload)
        history = payload.get("_h3_regeneration_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "archived_at": now.isoformat(),
                "task_status": task.status,
                "remote_task_id": task.runninghub_task_id,
                "normalized_video_path": None,
                "normalized_video_sha256": None,
                "provider_result_signature": direct["result_signature"],
                "output_metadata": _json_object(task.output_metadata),
            }
        )
        payload["_h3_regeneration_history"] = history
        return payload
    if not config.normalized_video_path:
        raise H3WorkbenchError("H3 当前成功分段缺少标准化结果，不能主动重生成")
    try:
        normalized = safe_relative_path(config.normalized_video_path, settings.data_dir)
    except ValueError as exc:
        raise H3WorkbenchError("H3 当前标准化结果路径不合法") from exc
    if not normalized.is_file():
        raise H3WorkbenchError("H3 当前标准化结果文件不存在，不能主动重生成")

    payload = _json_object(task.input_payload)
    history = payload.get("_h3_regeneration_history")
    if not isinstance(history, list):
        history = []
    output_dir = task_output_dir(settings, task.user_id, task.id).resolve()
    archived_normalized = normalized
    if normalized.is_relative_to(output_dir) and output_dir.is_dir():
        archive_dir = output_dir / "history" / f"regeneration-{len(history) + 1:03d}"
        archive_dir.mkdir(parents=True, exist_ok=False)
        relative_normalized = normalized.relative_to(output_dir)
        for child in list(output_dir.iterdir()):
            if child.name == "history":
                continue
            shutil.move(str(child), str(archive_dir / child.name))
        archived_normalized = archive_dir / relative_normalized

    history.append(
        {
            "archived_at": now.isoformat(),
            "task_status": task.status,
            "remote_task_id": task.runninghub_task_id,
            "normalized_video_path": to_relative_data_path(
                archived_normalized, settings
            ),
            "normalized_video_sha256": config.normalized_video_sha256,
            "output_metadata": _json_object(task.output_metadata),
        }
    )
    payload["_h3_regeneration_history"] = history
    return payload


def _reset_h3_task_runtime(
    task: GenerationTask,
    *,
    status: str,
    now: datetime,
) -> None:
    task.h3_remote_asr_job = None
    if task_uses_execution_pool(task):
        task.execution_account_id = None
        task.execution_account = None
    task.runninghub_task_id = None
    task.runninghub_submitted_at = None
    task.runninghub_failed_reason = None
    task.runninghub_usage = None
    task.result_path = None
    task.output_metadata = None
    task.status = status
    task.created_at = now
    task.error_code = None
    task.error_message = None
    task.runninghub_auto_retry_count = 0
    task.runninghub_auto_retry_after = None
    task.completed_at = None


def _regeneration_seed(
    original_seed: object,
    *,
    request_key: str,
    segment_id: str,
) -> int:
    value = f"{original_seed}:{request_key}:{segment_id}".encode("utf-8")
    return int(hashlib.sha256(value).hexdigest()[:16], 16)


def _reusable_task_for_input(
    db: Session,
    user: User,
    input_sha256: str,
) -> GenerationTask | None:
    candidates = db.scalars(
        select(GenerationTask)
        .join(GenerationSegment, GenerationTask.segment_id == GenerationSegment.id)
        .join(H3SegmentConfig, H3SegmentConfig.segment_id == GenerationSegment.id)
        .where(
            GenerationTask.user_id == user.id,
            GenerationTask.workflow_type == H3_WORKFLOW,
            GenerationTask.status == TaskStatus.SUCCESS.value,
            H3SegmentConfig.input_sha256 == input_sha256,
            H3SegmentConfig.invalidated_at.is_(None),
        )
        .order_by(GenerationTask.completed_at.desc())
    ).all()
    return next(
        (
            task
            for task in candidates
            if task.result_path or parse_h3_direct_delivery(task.output_metadata)
        ),
        None,
    )


def h3_batch_query():
    return select(GenerationBatch).options(
        selectinload(GenerationBatch.h3_config),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.h3_config),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.h3_config),
        selectinload(GenerationBatch.items)
        .selectinload(GenerationBatchItem.segments)
        .selectinload(GenerationSegment.generation_task),
    )


def get_h3_batch(db: Session, user: User, batch_id: str) -> GenerationBatch | None:
    return db.scalar(
        h3_batch_query().where(
            GenerationBatch.id == batch_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == H3_WORKFLOW,
            GenerationBatch.source_channel == BATCH_SOURCE_H3_WORKBENCH,
        )
    )


def _prepare_segment(
    *,
    user: User,
    batch: GenerationBatch,
    item: GenerationBatchItem,
    item_config_values: dict[str, object],
    plan: H3TimestampedSegment,
    motion_reference: H3MotionReference,
    segment_audio: Path,
    segment_audio_duration: float,
    segment_audio_sha256: str,
    image_sha256s: list[str],
    instance_type: str,
    settings: Settings,
) -> tuple[GenerationSegment, H3SegmentConfig]:
    duration = plan_h3_duration(
        segment_audio_duration,
        batch.h3_config.generation_tail_seconds,
    )
    identity_count = len(image_sha256s)
    has_anchor = (
        item_config_values["continuity_mode"] == "soft_chain" and plan.index > 0
    )
    parameters = H3Ref2VAWorkflow().validate_parameters(
        {
            "segment_text": plan.script_text,
            "segment_index": plan.index,
            "segment_count": item_config_values["segment_count"],
            "prompt_override": batch.h3_config.prompt_override,
            "user_direction": item_config_values["user_direction"],
            "continuity_mode": item_config_values["continuity_mode"],
            "generation_tail_seconds": batch.h3_config.generation_tail_seconds,
            "aspect_ratio": item_config_values["aspect_ratio"],
            "megapixels": item_config_values["megapixels"],
            "multiple": 32,
            "seed": 0,
            "instance_type": instance_type,
        },
        {
            "audio_duration_seconds": segment_audio_duration,
            "identity_image_count": identity_count,
            "has_continuity_anchor": has_anchor,
        },
    )
    segment_id = str(uuid.uuid4())
    prompt = str(parameters["prompt"])
    prompt_sha = str(parameters["prompt_sha256"])
    content_input: dict[str, object] = {
        "schema": "h3.segment-input.v1",
        "user_id": user.id,
        "segment_text": plan.script_text,
        "segment_audio_sha256": segment_audio_sha256,
        "prompt_sha256": prompt_sha,
        "reference_video_sha256": motion_reference.sha256,
        "ordered_identity_image_sha256s": image_sha256s,
        "continuity_mode": item_config_values["continuity_mode"],
        "continuity_anchor_sha256": "PENDING" if has_anchor else None,
        "resolution": {
            "aspect_ratio": item_config_values["aspect_ratio"],
            "megapixels": item_config_values["megapixels"],
            "multiple": 32,
        },
        "duration": {
            "audio_seconds": segment_audio_duration,
            "generation_tail_seconds": batch.h3_config.generation_tail_seconds,
            "requested_seconds": duration.requested_generation_duration_seconds,
            "frames": duration.quantized_frame_count,
        },
        "workflow_template_sha256": H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256,
        "adapter_version": H3_ADAPTER_VERSION,
        "output_contract_version": H3_OUTPUT_CONTRACT_VERSION,
        "instance_type": instance_type,
    }
    input_sha = _content_digest(content_input)
    idempotency_sha = _content_digest(
        {
            "user_id": user.id,
            "batch_id": batch.id,
            "segment_id": segment_id,
            "content_input_sha256": input_sha,
        }
    )
    segment = GenerationSegment(
        id=segment_id,
        batch_item=item,
        segment_index=plan.index,
        script_text=plan.script_text,
        start_seconds=plan.start_seconds,
        end_seconds=plan.end_seconds,
        audio_path=to_relative_data_path(segment_audio, settings),
        video_path=None,
        prompt=prompt,
        alignment_method=H3_SEGMENTER_VERSION,
        status=(
            "WAITING_DEPENDENCY" if has_anchor else "AWAITING_COST_CONFIRMATION"
        ),
    )
    config = H3SegmentConfig(
        segment=segment,
        idempotency_sha256=idempotency_sha,
        input_sha256=input_sha,
        segment_audio_sha256=segment_audio_sha256,
        prompt_sha256=prompt_sha,
        prompt_template_version=str(parameters["prompt_template_version"]),
        motion_reference_index=motion_reference.index,
        motion_reference_path=to_relative_data_path(motion_reference.path, settings),
        motion_reference_sha256=motion_reference.sha256,
        requested_generation_duration_seconds=(
            duration.requested_generation_duration_seconds
        ),
        quantized_frame_count=duration.quantized_frame_count,
        effective_generation_duration_seconds=(
            duration.effective_generation_duration_seconds
        ),
        continuity_mode=str(item_config_values["continuity_mode"]),
    )
    return segment, config


def prepare_h3_workbench_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    name: str,
    request_key: str,
    correlation_id: str | None,
    reference_image_asset_ids: object,
    defaults: object,
    rows: object,
    selected_account_ids: object,
) -> tuple[GenerationBatch, bool]:
    clean_request_key = str(request_key or "").strip()
    if not clean_request_key or len(clean_request_key) > 64:
        raise H3WorkbenchError("request_key 长度必须为 1–64")
    existing = db.scalar(
        h3_batch_query().where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == clean_request_key,
        )
    )
    if existing is not None:
        if (
            existing.workflow_type != H3_WORKFLOW
            or existing.source_channel != BATCH_SOURCE_H3_WORKBENCH
        ):
            raise H3WorkbenchError("request_key 已被其他类型任务使用")
        return existing, False

    if not isinstance(reference_image_asset_ids, list) or len(reference_image_asset_ids) > 4:
        raise H3WorkbenchError("H3 批次人物参考图必须为 0～4 张")
    image_ids = [str(value or "").strip() for value in reference_image_asset_ids]
    if any(not value for value in image_ids) or len(set(image_ids)) != len(image_ids):
        raise H3WorkbenchError("H3 批次人物参考图 ID 不能为空或重复")
    clean_defaults = _clean_defaults(defaults)
    clean_rows = _clean_rows(rows, settings, clean_defaults)
    for row in clean_rows:
        row_image_ids = list(row.get("reference_image_asset_ids") or image_ids)
        row["reference_image_asset_ids"] = row_image_ids
    if any(
        str(row["continuity_mode"]) == "loop_anchor"
        and not row["reference_image_asset_ids"]
        for row in clean_rows
    ):
        raise H3WorkbenchError("H3 首尾同图模式至少需要选择 1 张人物参考图")
    all_image_ids = list(
        dict.fromkeys(
            image_id
            for row in clean_rows
            for image_id in row["reference_image_asset_ids"]
        )
    )
    try:
        account_ids = validate_h3_account_selection(db, user, selected_account_ids)
    except H3PoolValidationError as exc:
        raise H3WorkbenchError(str(exc)) from exc
    instance_type = _selected_instance_type(db, account_ids)
    assets = _available_assets(db, user, all_image_ids, clean_rows)

    batch_id = str(uuid.uuid4())
    batch_dir = _batch_directory(settings, user.id, batch_id)
    clean_name = str(name or "").strip() or "H3 多参考批次"
    if len(clean_name) > 100:
        raise H3WorkbenchError("H3 批次名称不能超过 100 个字符")
    clean_correlation = str(correlation_id or "").strip() or None
    if clean_correlation and len(clean_correlation) > 64:
        raise H3WorkbenchError("correlation_id 不能超过 64 个字符")

    batch = GenerationBatch(
        id=batch_id,
        user=user,
        name=clean_name,
        workflow_type=H3_WORKFLOW,
        source_channel=BATCH_SOURCE_H3_WORKBENCH,
        correlation_id=clean_correlation,
        audio_mode="minimax",
        review_required=False,
        video_review_required=False,
        request_key=clean_request_key,
        status="AWAITING_COST_CONFIRMATION",
        total_items=len(clean_rows),
        runninghub_execution_account_ids_json=json.dumps(account_ids),
        execution_mode="h3_pool_v1",
    )
    now = datetime.now(timezone.utc)
    reference_images: list[dict[str, object]] = []
    normalized_contract: dict[str, object] = {
        "schema": H3_BATCH_SCHEMA,
        "source_channel": BATCH_SOURCE_H3_WORKBENCH,
        "defaults": clean_defaults,
        "selected_account_ids": account_ids,
        "instance_type": instance_type,
        "reference_images": [],
        "items": [],
        "workflow_adapter_version": H3_ADAPTER_VERSION,
    }
    try:
        shared_dir = batch_dir / "shared"
        for order, asset_id in enumerate(all_image_ids):
            asset = assets[asset_id]
            source = safe_relative_path(asset.relative_path, settings.data_dir)
            target = materialize_staged_asset(source, shared_dir, kind="identity-image")
            snapshot = {
                "asset_id": asset.id,
                "order": order,
                "path": to_relative_data_path(target, settings),
                "original_name": asset.original_name,
                "sha256": _sha256_file(target),
            }
            reference_images.append(snapshot)
            normalized_contract["reference_images"].append(
                {key: snapshot[key] for key in ("asset_id", "order", "sha256")}
            )
        reference_images_by_id = {
            str(image["asset_id"]): image for image in reference_images
        }
        batch_config = H3BatchConfig(
            batch=batch,
            contract_schema=H3_BATCH_SCHEMA,
            input_sha256="0" * 64,
            prompt_profile_id=str(clean_defaults["prompt_profile_id"]),
            prompt_template_version=str(clean_defaults["prompt_template_version"]),
            prompt_override=str(clean_defaults["prompt_override"]) or None,
            continuity_mode=str(clean_defaults["continuity_mode"]),
            aspect_ratio=str(clean_defaults["resolution"]["aspect_ratio"]),
            megapixels=float(clean_defaults["resolution"]["megapixels"]),
            multiple=32,
            generation_tail_seconds=float(clean_defaults["generation_tail_seconds"]),
            adapter_version=H3_ADAPTER_VERSION,
            reference_images_json=json.dumps(reference_images, ensure_ascii=False),
        )
        batch.h3_config = batch_config

        for position, row in enumerate(clean_rows, start=1):
            row_reference_images = [
                reference_images_by_id[str(asset_id)]
                for asset_id in row["reference_image_asset_ids"]
            ]
            video_asset = assets[str(row["video_asset_id"])]
            video_source = safe_relative_path(video_asset.relative_path, settings.data_dir)
            uploaded_audio_id = str(row.get("audio_asset_id") or "")
            if uploaded_audio_id:
                audio_asset = assets[uploaded_audio_id]
                audio_source = safe_relative_path(
                    audio_asset.relative_path, settings.data_dir
                )
                audio_batch_id = H3_UPLOADED_AUDIO_BATCH_MARKER
                audio_item_id = audio_asset.id
                audio_generation_version = 0
                audio_original_name = audio_asset.original_name
                raw_cues_version = H3_UPLOADED_AUDIO_CUES_VERSION
                cues_source = None
            else:
                audio_task = _audio_task_for_row(db, user, row)
                audio_source = safe_relative_path(
                    str(audio_task.output_path), settings.data_dir
                )
                cues_source = safe_relative_path(
                    str(audio_task.subtitle_path), settings.data_dir
                )
                audio_batch_id = str(row["audio_batch_id"])
                audio_item_id = str(row["audio_item_id"])
                audio_generation_version = int(row["audio_generation_version"])
                audio_original_name = Path(audio_source).name
                raw_cues_version = H3_RAW_CUES_VERSION
            if not video_source.is_file() or not audio_source.is_file():
                raise H3WorkbenchError("H3 参考视频或音频文件不存在")
            if cues_source is not None and not cues_source.is_file():
                raise H3WorkbenchError("H3 MiniMax raw cues 文件不存在")
            audio_duration = inspect_audio_duration(audio_source)
            item_id = str(uuid.uuid4())
            item_dir = batch_dir / "items" / item_id
            video_target = materialize_staged_asset(video_source, item_dir, kind="reference-video")
            audio_target = materialize_staged_asset(audio_source, item_dir, kind="full-audio")
            audio_sha = _sha256_file(audio_target)
            if cues_source is None:
                plans, cues_text = _plan_uploaded_h3_audio(
                    str(row["script_text"]),
                    audio_target,
                    audio_duration,
                    generation_tail_seconds=float(
                        clean_defaults["generation_tail_seconds"]
                    ),
                    client_alignment=row.get("audio_alignment"),
                    audio_sha256=audio_sha,
                    allow_server_asr=settings.media_processing_mode != "remote",
                )
                cues_target = item_dir / "raw-cues.json"
                cues_target.write_text(cues_text, encoding="utf-8")
            else:
                cues = load_subtitle_cues(cues_source.read_text(encoding="utf-8"))
                plans = _plan_h3_audio_segments(
                    str(row["script_text"]),
                    cues,
                    audio_target,
                    audio_duration,
                    generation_tail_seconds=float(
                        clean_defaults["generation_tail_seconds"]
                    ),
                    client_alignment=row.get("audio_alignment"),
                    audio_sha256=audio_sha,
                    allow_server_asr=settings.media_processing_mode != "remote",
                )
                cues_target = materialize_staged_asset(
                    cues_source, item_dir, kind="raw-cues"
                )
            video_sha = _sha256_file(video_target)
            cues_sha = _sha256_file(cues_target)
            motion_references = split_h3_motion_reference(
                video_target,
                item_dir / "motion-references",
            )
            assigned_motion_references = assign_h3_motion_references(
                motion_references,
                len(plans),
                seed_material="\0".join(
                    (
                        video_sha,
                        audio_sha,
                        _sha256_text(str(row["script_text"])),
                    )
                ),
            )
            primary_frame: dict[str, object] | None = None
            is_loop_anchor = str(row["continuity_mode"]) == "loop_anchor"
            if len(row_reference_images) >= 2 and not is_loop_anchor:
                frame_target = item_dir / "primary-reference-frame.png"
                try:
                    extract_reference_frame(video_target, frame_target)
                except H3PostprocessError as exc:
                    raise H3WorkbenchError(str(exc)) from exc
                primary_frame = {
                    "path": to_relative_data_path(frame_target, settings),
                    "original_name": frame_target.name,
                    "sha256": _sha256_file(frame_target),
                    "role": "primary_visual_anchor_from_reference_video",
                    "selection": "0.5s_with_first_frame_fallback",
                }
            effective_reference_images = (
                [primary_frame, *row_reference_images]
                if primary_frame
                else row_reference_images
            )
            item = GenerationBatchItem(
                id=item_id,
                batch=batch,
                row_number=position,
                row_key=str(row["row_id"]),
                manifest_json="{}",
                runninghub_execution_account_ids_json=json.dumps(account_ids),
                audio_status="AUDIO_FROZEN_H3_DRAFT",
                status="AWAITING_COST_CONFIRMATION",
                merged_video_status="NOT_STARTED",
            )
            resolution = row["resolution"]
            item_config_values: dict[str, object] = {
                "reference_video_sha256": video_sha,
                "user_direction": row["user_direction"],
                "continuity_mode": row["continuity_mode"],
                "aspect_ratio": resolution["aspect_ratio"],
                "megapixels": resolution["megapixels"],
                "segment_count": len(plans),
            }
            item_config = H3ItemConfig(
                batch_item=item,
                script_sha256=_sha256_text(str(row["script_text"])),
                reference_video_asset_id=video_asset.id,
                reference_video_path=to_relative_data_path(video_target, settings),
                reference_video_original_name=video_asset.original_name,
                reference_video_sha256=video_sha,
                audio_batch_id=audio_batch_id,
                audio_item_id=audio_item_id,
                audio_generation_version=audio_generation_version,
                full_audio_path=to_relative_data_path(audio_target, settings),
                full_audio_original_name=audio_original_name,
                full_audio_sha256=audio_sha,
                raw_cues_path=to_relative_data_path(cues_target, settings),
                raw_cues_sha256=cues_sha,
                raw_cues_version=raw_cues_version,
                audio_duration_seconds=audio_duration,
                user_direction=str(row["user_direction"]),
                continuity_mode=str(row["continuity_mode"]),
                aspect_ratio=str(resolution["aspect_ratio"]),
                megapixels=float(resolution["megapixels"]),
                multiple=32,
                segment_count=len(plans),
            )
            item.h3_config = item_config
            segment_snapshots = []
            previous_segment: GenerationSegment | None = None
            for plan, motion_reference in zip(
                plans, assigned_motion_references, strict=True
            ):
                segment_audio = item_dir / "segments" / f"segment-{plan.index + 1:03d}.mp3"
                if (
                    len(plans) == 1
                    and plan.start_seconds <= 0.001
                    and abs(plan.end_seconds - audio_duration) <= 0.01
                ):
                    segment_audio = materialize_staged_asset(
                        audio_source,
                        item_dir / "segments",
                        kind="segment-audio",
                    )
                else:
                    cut_audio_segment(
                        audio_source,
                        segment_audio,
                        start_seconds=plan.start_seconds,
                        end_seconds=plan.end_seconds,
                    )
                probed_duration = inspect_audio_duration(segment_audio)
                segment, segment_config = _prepare_segment(
                    user=user,
                    batch=batch,
                    item=item,
                    item_config_values=item_config_values,
                    plan=plan,
                    motion_reference=motion_reference,
                    segment_audio=segment_audio,
                    segment_audio_duration=probed_duration,
                    segment_audio_sha256=_sha256_file(segment_audio),
                    image_sha256s=[
                        str(image["sha256"]) for image in effective_reference_images
                    ],
                    instance_type=instance_type,
                    settings=settings,
                )
                if segment_config.continuity_mode == "soft_chain" and previous_segment:
                    segment_config.previous_segment = previous_segment
                previous_segment = segment
                segment_snapshots.append(
                    {
                        "segment_id": segment.id,
                        "index": segment.segment_index,
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "script_text": segment.script_text,
                        "audio_sha256": segment_config.segment_audio_sha256,
                        "prompt_sha256": segment_config.prompt_sha256,
                        "input_sha256": segment_config.input_sha256,
                        "motion_reference_index": motion_reference.index,
                        "motion_reference_sha256": motion_reference.sha256,
                    }
                )
            item.manifest_json = json.dumps(
                {
                    "schema": "jyd.h3-generation-item.v1",
                    "row_id": row["row_id"],
                    "script_text": row["script_text"],
                    "script_sha256": item_config.script_sha256,
                    "reference_video": {
                        "asset_id": video_asset.id,
                        "sha256": video_sha,
                        "role": "identity_motion_camera_environment_reference",
                    },
                    "motion_reference_pool": {
                        "schema": "h3.motion-reference-pool.v1",
                        "clip_seconds": H3_MOTION_CLIP_SECONDS,
                        "assignment_version": H3_MOTION_ASSIGNMENT_VERSION,
                        "source_duration_seconds": motion_references[-1].end_seconds,
                        "clip_count": len(motion_references),
                    },
                    "reference_mode": (
                        "loop_anchor_picture_primary"
                        if is_loop_anchor
                        else "picture_primary" if primary_frame else "video_primary"
                    ),
                    "primary_reference_frame": primary_frame,
                    "identity_reference_asset_ids": list(
                        row["reference_image_asset_ids"]
                    ),
                    "loop_anchor_image_sha256": (
                        str(row_reference_images[0]["sha256"])
                        if is_loop_anchor
                        else None
                    ),
                    "audio_binding": {
                        "source": (
                            "uploaded" if uploaded_audio_id else "minimax_generated"
                        ),
                        "audio_batch_id": audio_batch_id,
                        "audio_item_id": audio_item_id,
                        "generation_version": audio_generation_version,
                        "raw_cues_version": raw_cues_version,
                        "duration_seconds": audio_duration,
                        "safe_cut_alignment": (
                            {
                                "schema": H3_SAFE_CUT_ALIGNMENT_SCHEMA,
                                "source": "jyd_local_funasr",
                                "script_sha256": row["audio_alignment"][
                                    "script_sha256"
                                ],
                                "audio_sha256": row["audio_alignment"][
                                    "audio_sha256"
                                ],
                                "ranges_sha256": _content_digest(
                                    row["audio_alignment"]["ranges"]
                                ),
                            }
                            if row.get("audio_alignment") is not None
                            else None
                        ),
                    },
                    "effective": {
                        "user_direction": row["user_direction"],
                        "continuity_mode": row["continuity_mode"],
                        "resolution": resolution,
                        "instance_type": instance_type,
                    },
                    "segments": segment_snapshots,
                },
                ensure_ascii=False,
            )
            normalized_contract["items"].append(
                {
                    "item_id": item.id,
                    "row_id": row["row_id"],
                    "script_sha256": item_config.script_sha256,
                    "reference_video_sha256": video_sha,
                    "motion_reference_assignment_version": (
                        H3_MOTION_ASSIGNMENT_VERSION
                    ),
                    "motion_reference_sha256s": [
                        snapshot["motion_reference_sha256"]
                        for snapshot in segment_snapshots
                    ],
                    "reference_mode": (
                        "loop_anchor_picture_primary"
                        if is_loop_anchor
                        else "picture_primary" if primary_frame else "video_primary"
                    ),
                    "primary_reference_frame_sha256": (
                        str(primary_frame["sha256"]) if primary_frame else None
                    ),
                    "identity_reference_image_sha256s": [
                        str(image["sha256"]) for image in row_reference_images
                    ],
                    "loop_anchor_image_sha256": (
                        str(row_reference_images[0]["sha256"])
                        if is_loop_anchor
                        else None
                    ),
                    "full_audio_sha256": audio_sha,
                    "raw_cues_sha256": cues_sha,
                    "effective": {
                        "user_direction": row["user_direction"],
                        "continuity_mode": row["continuity_mode"],
                        "resolution": resolution,
                    },
                    "segment_input_sha256s": [
                        snapshot["input_sha256"] for snapshot in segment_snapshots
                    ],
                }
            )
        batch_config.input_sha256 = _content_digest(normalized_contract)
        all_segments = [segment for item in batch.items for segment in item.segments]
        reusable = sum(
            1
            for segment in all_segments
            if segment.h3_config.continuity_mode != "soft_chain"
            and _reusable_task_for_input(db, user, segment.h3_config.input_sha256)
            is not None
        )
        dependent_unknown = sum(
            1
            for segment in all_segments
            if segment.h3_config.continuity_mode == "soft_chain"
            and segment.segment_index > 0
        )
        fee_snapshot = {
            "schema": "runninghub.h3-fee-preview.v1",
            "cost_confirmed": False,
            "segment_count": len(all_segments),
            "estimated_paid_calls": len(all_segments) - reusable,
            "reusable_result_count": reusable,
            "dependency_reuse_pending_count": dependent_unknown,
            "continuity_modes": sorted(
                {item.h3_config.continuity_mode for item in batch.items}
            ),
            "selected_account_ids": account_ids,
            "instance_type": instance_type,
            "prepared_at": now.isoformat(),
        }
        batch_config.fee_snapshot_json = json.dumps(fee_snapshot, ensure_ascii=False)
        for asset in assets.values():
            asset.consumed_at = now
        db.add(batch)
        db.commit()
    except Exception:
        db.rollback()
        remove_directory(batch_dir)
        raise
    prepared = get_h3_batch(db, user, batch_id)
    if prepared is None:
        raise H3WorkbenchError("H3 批次准备后无法读取")
    return prepared, True


def _reference_assets(batch: GenerationBatch) -> list[dict[str, object]]:
    try:
        value = json.loads(batch.h3_config.reference_images_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise H3WorkbenchError("H3 人物参考图快照损坏") from exc
    if not isinstance(value, list):
        raise H3WorkbenchError("H3 人物参考图快照损坏")
    return value


def _effective_reference_assets(
    batch: GenerationBatch,
    item: GenerationBatchItem,
) -> list[dict[str, object]]:
    images = _reference_assets(batch)
    try:
        manifest = json.loads(item.manifest_json)
        row_image_ids = manifest.get("identity_reference_asset_ids", [])
        if not isinstance(row_image_ids, list):
            raise TypeError
        if row_image_ids:
            images_by_id = {str(image.get("asset_id") or ""): image for image in images}
            images = [images_by_id[str(asset_id)] for asset_id in row_image_ids]
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise H3WorkbenchError("H3 逐行人物参考图快照损坏") from exc
    if item.h3_config.continuity_mode == "loop_anchor" or len(images) < 2:
        return images
    try:
        primary = manifest["primary_reference_frame"]
        if not isinstance(primary, dict):
            raise TypeError
        for field in ("path", "original_name", "sha256"):
            if not str(primary.get(field) or "").strip():
                raise TypeError
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise H3WorkbenchError("H3 自动主画面快照损坏") from exc
    return [primary, *images]


def _segment_motion_reference(
    item: GenerationBatchItem,
    segment: GenerationSegment,
) -> tuple[str, str, str]:
    config = segment.h3_config
    if config.motion_reference_path and config.motion_reference_sha256:
        return (
            config.motion_reference_path,
            Path(config.motion_reference_path).name,
            config.motion_reference_sha256,
        )
    return (
        item.h3_config.reference_video_path,
        item.h3_config.reference_video_original_name,
        item.h3_config.reference_video_sha256,
    )


def _create_segment_task(
    user: User,
    batch: GenerationBatch,
    item: GenerationBatchItem,
    segment: GenerationSegment,
    *,
    instance_type: str,
    reused_task: GenerationTask | None,
    bind_relationships: bool = True,
) -> GenerationTask:
    images = _effective_reference_assets(batch, item)
    motion_path, motion_name, _motion_sha256 = _segment_motion_reference(
        item, segment
    )
    assets = [
        WorkflowAsset(
            name="video",
            kind="video",
            relative_path=motion_path,
            original_name=motion_name,
        ),
        WorkflowAsset(
            name="audio",
            kind="audio",
            relative_path=segment.audio_path,
            original_name=Path(segment.audio_path).name,
        ),
    ]
    assets.extend(
        WorkflowAsset(
            name=f"identity_image_{index + 1}",
            kind="image",
            relative_path=str(image["path"]),
            original_name=str(image["original_name"]),
        )
        for index, image in enumerate(images)
    )
    has_continuity_anchor = bool(segment.h3_config.continuity_anchor_path)
    if has_continuity_anchor:
        assets.append(
            WorkflowAsset(
                name="continuity_anchor",
                kind="image",
                relative_path=str(segment.h3_config.continuity_anchor_path),
                original_name=Path(segment.h3_config.continuity_anchor_path).name,
            )
        )
    adapter = H3Ref2VAWorkflow()
    audio_duration_seconds = (
        segment.h3_config.requested_generation_duration_seconds
        - batch.h3_config.generation_tail_seconds
    )
    parameters = adapter.validate_parameters(
        {
            "segment_text": segment.script_text,
            "segment_index": segment.segment_index,
            "segment_count": item.h3_config.segment_count,
            "prompt_override": batch.h3_config.prompt_override,
            "user_direction": item.h3_config.user_direction,
            "continuity_mode": item.h3_config.continuity_mode,
            "generation_tail_seconds": batch.h3_config.generation_tail_seconds,
            "aspect_ratio": item.h3_config.aspect_ratio,
            "megapixels": item.h3_config.megapixels,
            "multiple": 32,
            "seed": int(segment.h3_config.input_sha256[:16], 16),
            "instance_type": instance_type,
        },
        {
            "audio_duration_seconds": audio_duration_seconds,
            "identity_image_count": len(images),
            "has_continuity_anchor": has_continuity_anchor,
        },
    )
    input_payload = adapter.serialize_input(
        assets,
        parameters,
        {"audio_duration_seconds": audio_duration_seconds},
    )
    now = datetime.now(timezone.utc)
    reused_metadata = None
    if reused_task is not None:
        reused_metadata = _json_object(reused_task.output_metadata)
        reused_metadata.update(
            {
                "reused_from_task_id": reused_task.id,
                "reuse_reason": "identical_h3_input_sha256",
            }
        )
    task = GenerationTask(
        id=str(uuid.uuid4()),
        workflow_type=H3_WORKFLOW,
        seedvr2_enabled=False,
        input_payload=json.dumps(input_payload, ensure_ascii=False),
        image_path=motion_path,
        audio_path=segment.audio_path,
        image_original_name=motion_name,
        audio_original_name=Path(segment.audio_path).name,
        audio_duration_seconds=float(parameters["audio_duration_seconds"]),
        start_seconds=0.0,
        end_seconds=float(parameters["audio_duration_seconds"]),
        prompt=str(parameters["prompt"]),
        status=(TaskStatus.SUCCESS.value if reused_task else TaskStatus.PENDING.value),
        result_path=reused_task.result_path if reused_task else None,
        output_metadata=(
            json.dumps(reused_metadata, ensure_ascii=False)
            if reused_task
            else None
        ),
        completed_at=now if reused_task else None,
    )
    if bind_relationships:
        task.user = user
        task.segment = segment
    else:
        task.user_id = user.id
        task.segment_id = segment.id
    return task


def _h3_segment_for_user(
    db: Session,
    user: User,
    segment_id: str,
) -> GenerationSegment | None:
    return db.scalar(
        select(GenerationSegment)
        .join(
            GenerationBatchItem,
            GenerationSegment.batch_item_id == GenerationBatchItem.id,
        )
        .join(GenerationBatch, GenerationBatchItem.batch_id == GenerationBatch.id)
        .options(
            selectinload(GenerationSegment.h3_config),
            selectinload(GenerationSegment.generation_task),
            selectinload(GenerationSegment.batch_item)
            .selectinload(GenerationBatchItem.segments)
            .selectinload(GenerationSegment.h3_config),
            selectinload(GenerationSegment.batch_item)
            .selectinload(GenerationBatchItem.segments)
            .selectinload(GenerationSegment.generation_task),
            selectinload(GenerationSegment.batch_item)
            .selectinload(GenerationBatchItem.batch)
            .selectinload(GenerationBatch.h3_config),
        )
        .where(
            GenerationSegment.id == segment_id,
            GenerationBatch.user_id == user.id,
            GenerationBatch.workflow_type == H3_WORKFLOW,
            GenerationBatch.source_channel == BATCH_SOURCE_H3_WORKBENCH,
        )
    )


def _affected_regeneration_segments(
    segment: GenerationSegment,
) -> list[GenerationSegment]:
    if segment.h3_config is None:
        raise H3WorkbenchError("H3 分段审计快照缺失")
    if segment.h3_config.continuity_mode != "soft_chain":
        return [segment]
    return [
        candidate
        for candidate in sorted(
            segment.batch_item.segments,
            key=lambda value: value.segment_index,
        )
        if candidate.segment_index >= segment.segment_index
    ]


def _manual_regeneration_metadata(task: GenerationTask) -> dict[str, Any] | None:
    value = _json_object(task.input_payload).get("_h3_manual_regeneration")
    return value if isinstance(value, dict) else None


def prepare_h3_segment_regeneration(
    db: Session,
    user: User,
    segment_id: str,
) -> dict[str, object]:
    segment = _h3_segment_for_user(db, user, segment_id)
    if segment is None:
        raise H3WorkbenchError("H3 分段不存在")
    affected = _affected_regeneration_segments(segment)
    state: list[dict[str, object]] = []
    for candidate in affected:
        task = candidate.generation_task
        config = candidate.h3_config
        if (
            task is None
            or config is None
            or task.status != TaskStatus.SUCCESS.value
            or candidate.status != TaskStatus.SUCCESS.value
            or config.invalidated_at is not None
            or not _h3_segment_has_current_result(candidate)
        ):
            raise H3WorkbenchError(
                "主动重生成前，目标段及受影响的连续下游段必须全部成功且仍为当前版本"
            )
        state.append(
            {
                "segment_id": candidate.id,
                "segment_index": candidate.segment_index,
                "task_id": task.id,
                "task_status": task.status,
                "normalized_video_sha256": config.normalized_video_sha256,
                "result_signature": (
                    h3_segment_video_delivery(candidate) or {}
                ).get("result_signature"),
                "attempt_count": len(task.runninghub_attempts),
                "completed_at": (
                    task.completed_at.isoformat() if task.completed_at else None
                ),
            }
        )
    quote_token = _content_digest(
        {
            "schema": "runninghub.h3-regeneration-state.v1",
            "user_id": user.id,
            "batch_id": segment.batch_item.batch_id,
            "target_segment_id": segment.id,
            "affected": state,
        }
    )
    return {
        "schema": "runninghub.h3-regeneration-fee-preview.v1",
        "cost_confirmed": False,
        "batch_id": segment.batch_item.batch_id,
        "item_id": segment.batch_item_id,
        "target_segment_id": segment.id,
        "target_segment_index": segment.segment_index,
        "continuity_mode": segment.h3_config.continuity_mode,
        "cascade_required": len(affected) > 1,
        "affected_segment_ids": [candidate.id for candidate in affected],
        "affected_segment_indexes": [candidate.segment_index for candidate in affected],
        "estimated_paid_calls": len(affected),
        "historical_files_will_be_retained": True,
        "quote_token": quote_token,
    }


def confirm_h3_segment_regeneration(
    db: Session,
    user: User,
    segment_id: str,
    *,
    request_key: str,
    quote_token: str,
    cost_confirmed: object,
    settings: Settings,
    device_identity: WorkbenchIdentity | None = None,
) -> tuple[GenerationBatch, dict[str, object]]:
    if not _strict_bool(cost_confirmed, "H3 重生成费用确认"):
        raise H3WorkbenchError("必须确认 H3 主动重生成的每个受影响分段都会产生费用")
    clean_request_key = str(request_key or "").strip()
    if not clean_request_key or len(clean_request_key) > 100:
        raise H3WorkbenchError("H3 重生成 request_key 长度必须为 1–100")
    clean_quote_token = str(quote_token or "").strip()
    segment = _h3_segment_for_user(db, user, segment_id)
    if segment is None or segment.generation_task is None:
        raise H3WorkbenchError("H3 分段不存在")
    previous = _manual_regeneration_metadata(segment.generation_task)
    if previous is not None and previous.get("request_key") == clean_request_key:
        if previous.get("quote_token") != clean_quote_token:
            raise H3WorkbenchError("同一 H3 重生成 request_key 不能绑定不同费用快照")
        batch = get_h3_batch(db, user, segment.batch_item.batch_id)
        if batch is None:
            raise H3WorkbenchError("H3 批次不存在")
        return batch, previous

    preview = prepare_h3_segment_regeneration(db, user, segment_id)
    if preview["quote_token"] != clean_quote_token:
        raise H3WorkbenchError("H3 重生成费用快照已过期，请重新预览影响范围")
    affected = _affected_regeneration_segments(segment)
    # A real new paid attempt needs current device permission, before archiving
    # any previous result or resetting the task. Idempotent receipt reads above
    # are not a second paid operation.
    bind_new_operation(
        db, user_id=user.id, identity=device_identity, operation_kind="h3.regenerate",
        request_snapshot={"segment_id": segment.id, "request_key": clean_request_key, "quote_token": clean_quote_token},
        resources=[("generation_segment", candidate.id) for candidate in affected],
    )
    now = datetime.now(timezone.utc)
    receipt: dict[str, object] = {
        "schema": "runninghub.h3-regeneration-confirmation.v1",
        "request_key": clean_request_key,
        "quote_token": clean_quote_token,
        "target_segment_id": segment.id,
        "affected_segment_ids": [candidate.id for candidate in affected],
        "estimated_paid_calls": len(affected),
        "cost_confirmed": True,
        "confirmed_at": now.isoformat(),
    }
    for offset, candidate in enumerate(affected):
        task = candidate.generation_task
        config = candidate.h3_config
        assert task is not None and config is not None
        payload = _archive_h3_task_result(
            task,
            candidate,
            settings,
            now=now,
        )
        payload["_h3_manual_regeneration"] = receipt
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            raise H3WorkbenchError("H3 任务输入快照缺少 parameters")
        parameters["seed"] = _regeneration_seed(
            parameters.get("seed"),
            request_key=clean_request_key,
            segment_id=candidate.id,
        )
        task.input_payload = json.dumps(payload, ensure_ascii=False)
        _reset_h3_task_runtime(
            task,
            status=(
                TaskStatus.PENDING.value if offset == 0 else H3_REGENERATION_WAITING
            ),
            now=now,
        )
        config.normalized_video_path = None
        config.normalized_video_sha256 = None
        config.dynamic_workflow_sha256 = None
        config.invalidated_at = now
        candidate.video_path = None
        candidate.status = "TASK_CREATED" if offset == 0 else "WAITING_DEPENDENCY"
        candidate.error_code = None
        candidate.error_message = None
        if offset > 0:
            config.continuity_anchor_path = None
            config.continuity_anchor_sha256 = None
    item = segment.batch_item
    item.status = "RUNNING"
    item.error_code = None
    item.error_message = None
    item.batch.status = "ACTIVE"
    db.commit()
    batch = get_h3_batch(db, user, item.batch_id)
    if batch is None:
        raise H3WorkbenchError("H3 重生成确认后无法读取批次")
    return batch, receipt


def prepare_h3_segment_retry(
    db: Session,
    user: User,
    segment_id: str,
) -> dict[str, object]:
    segment = _h3_segment_for_user(db, user, segment_id)
    task = segment.generation_task if segment is not None else None
    if segment is None or task is None or task.status not in RETRYABLE_TASK_STATUSES:
        raise H3WorkbenchError("只有失败、下载失败或已取消的 H3 分段可以重试")
    download_only = bool(
        task.status == TaskStatus.DOWNLOAD_FAILED.value
        and task.runninghub_task_id
    )
    quote_token = _content_digest(
        {
            "schema": "runninghub.h3-retry-state.v1",
            "user_id": user.id,
            "segment_id": segment.id,
            "task_id": task.id,
            "task_status": task.status,
            "remote_task_id": task.runninghub_task_id,
            "error_code": task.error_code,
            "attempt_count": len(task.runninghub_attempts),
            "updated_at": task.updated_at.isoformat(),
        }
    )
    return {
        "schema": "runninghub.h3-retry-fee-preview.v1",
        "cost_confirmed": False,
        "batch_id": segment.batch_item.batch_id,
        "item_id": segment.batch_item_id,
        "segment_id": segment.id,
        "segment_index": segment.segment_index,
        "retry_scope": "download_only" if download_only else "provider_resubmit",
        "estimated_paid_calls": 0 if download_only else 1,
        "existing_continuity_anchor_will_be_reused": bool(
            segment.h3_config and segment.h3_config.continuity_anchor_path
        ),
        "quote_token": quote_token,
    }


def confirm_h3_segment_retry(
    db: Session,
    user: User,
    segment_id: str,
    *,
    request_key: str,
    quote_token: str,
    cost_confirmed: object,
    settings: Settings,
    device_identity: WorkbenchIdentity | None = None,
) -> tuple[GenerationBatch, dict[str, object]]:
    clean_request_key = str(request_key or "").strip()
    if not clean_request_key or len(clean_request_key) > 100:
        raise H3WorkbenchError("H3 重试 request_key 长度必须为 1–100")
    clean_quote_token = str(quote_token or "").strip()
    segment = _h3_segment_for_user(db, user, segment_id)
    if segment is None or segment.generation_task is None:
        raise H3WorkbenchError("H3 分段不存在")
    task = segment.generation_task
    payload = _json_object(task.input_payload)
    previous = payload.get("_h3_manual_retry")
    if isinstance(previous, dict) and previous.get("request_key") == clean_request_key:
        if previous.get("quote_token") != clean_quote_token:
            raise H3WorkbenchError("同一 H3 重试 request_key 不能绑定不同费用快照")
        batch = get_h3_batch(db, user, segment.batch_item.batch_id)
        if batch is None:
            raise H3WorkbenchError("H3 批次不存在")
        return batch, previous

    preview = prepare_h3_segment_retry(db, user, segment_id)
    if preview["quote_token"] != clean_quote_token:
        raise H3WorkbenchError("H3 重试费用快照已过期，请重新预览")
    paid_calls = int(preview["estimated_paid_calls"])
    if paid_calls and not _strict_bool(cost_confirmed, "H3 重试费用确认"):
        raise H3WorkbenchError("必须确认重新提交 H3 会产生 RunningHub 费用")
    if not paid_calls and not isinstance(cost_confirmed, bool):
        raise H3WorkbenchError("H3 重试费用确认必须是布尔值")
    if paid_calls:
        # Classification comes from the owned task/remote ID, not the request's
        # retry_scope or cost flags. Download-only recovery remains available.
        bind_new_operation(
            db, user_id=user.id, identity=device_identity, operation_kind="h3.retry",
            request_snapshot={"segment_id": segment.id, "request_key": clean_request_key, "quote_token": clean_quote_token},
            resources=[("generation_segment", segment.id)],
        )
    now = datetime.now(timezone.utc)
    receipt: dict[str, object] = {
        "schema": "runninghub.h3-retry-confirmation.v1",
        "request_key": clean_request_key,
        "quote_token": clean_quote_token,
        "segment_id": segment.id,
        "retry_scope": preview["retry_scope"],
        "estimated_paid_calls": paid_calls,
        "cost_confirmed": bool(cost_confirmed),
        "confirmed_at": now.isoformat(),
    }
    payload["_h3_manual_retry"] = receipt
    task.input_payload = json.dumps(payload, ensure_ascii=False)
    try:
        prepare_task_retry(task, settings, now=now, device_identity=device_identity)
    except TaskManagementError as exc:
        raise H3WorkbenchError(str(exc)) from exc
    if task.runninghub_task_id is None:
        task.h3_remote_asr_job = None
    if task.status == TaskStatus.PENDING.value and segment.h3_config is not None:
        segment.h3_config.dynamic_workflow_sha256 = None
    segment.status = task.status
    segment.error_code = None
    segment.error_message = None
    sync_h3_task_hierarchy(task)
    db.commit()
    batch = get_h3_batch(db, user, segment.batch_item.batch_id)
    if batch is None:
        raise H3WorkbenchError("H3 重试确认后无法读取批次")
    return batch, receipt


def cancel_h3_segment(
    db: Session,
    user: User,
    segment_id: str,
    *,
    request_key: str,
) -> tuple[GenerationBatch, dict[str, object]]:
    clean_request_key = str(request_key or "").strip()
    if not clean_request_key or len(clean_request_key) > 100:
        raise H3WorkbenchError("H3 取消 request_key 长度必须为 1–100")
    segment = _h3_segment_for_user(db, user, segment_id)
    if segment is None or segment.generation_task is None:
        raise H3WorkbenchError("H3 分段任务不存在")
    task = segment.generation_task
    payload = _json_object(task.input_payload)
    previous = payload.get("_h3_manual_cancel")
    if isinstance(previous, dict) and previous.get("request_key") == clean_request_key:
        batch = get_h3_batch(db, user, segment.batch_item.batch_id)
        if batch is None:
            raise H3WorkbenchError("H3 批次不存在")
        return batch, previous
    if task.status not in H3_CANCELLABLE_TASK_STATUSES:
        raise H3WorkbenchError("当前 H3 分段状态不能取消")
    now = datetime.now(timezone.utc)
    receipt: dict[str, object] = {
        "schema": "runninghub.h3-cancellation.v1",
        "request_key": clean_request_key,
        "segment_id": segment.id,
        "previous_status": task.status,
        "cancelled_at": now.isoformat(),
    }
    payload["_h3_manual_cancel"] = receipt
    if task.status == H3_REGENERATION_WAITING:
        task.status = TaskStatus.CANCELLED.value
        task.error_code = "CANCELLED_BY_USER"
        task.error_message = "任务已由用户取消"
        task.completed_at = now
    else:
        try:
            cancel_generation_task(db, task)
        except TaskCancellationError as exc:
            raise H3WorkbenchError(str(exc)) from exc
    task.input_payload = json.dumps(payload, ensure_ascii=False)
    segment.status = TaskStatus.CANCELLED.value
    segment.error_code = "CANCELLED_BY_USER"
    segment.error_message = "任务已由用户取消"
    item = segment.batch_item
    sync_h3_task_hierarchy(task)
    db.commit()
    batch = get_h3_batch(db, user, item.batch_id)
    if batch is None:
        raise H3WorkbenchError("H3 取消后无法读取批次")
    return batch, receipt


def sync_h3_task_hierarchy(task: GenerationTask) -> None:
    """Recompute H3 segment, item and batch states after one task changes."""

    segment = task.segment
    if task.workflow_type != H3_WORKFLOW or segment is None:
        return
    segment.status = task.status
    segment.error_code = task.error_code
    segment.error_message = task.error_message
    batch = segment.batch_item.batch
    for item in batch.items:
        child_tasks = [
            candidate.generation_task
            for candidate in item.segments
            if candidate.generation_task is not None
        ]
        if child_tasks and all(
            candidate.status == TaskStatus.SUCCESS.value
            for candidate in child_tasks
        ) and len(child_tasks) == len(item.segments):
            item.status = TaskStatus.SUCCESS.value
            item.error_code = None
            item.error_message = None
            continue
        if any(candidate.status in H3_ACTIVE_TASK_STATUSES for candidate in child_tasks):
            item.status = "RUNNING"
            continue
        failed = next(
            (
                candidate
                for candidate in child_tasks
                if candidate.status
                in {
                    TaskStatus.FAILED.value,
                    TaskStatus.DOWNLOAD_FAILED.value,
                    TaskStatus.CANCELLED.value,
                }
            ),
            None,
        )
        if failed is not None:
            item.status = "FAILED"
            item.error_code = failed.error_code or "H3_SEGMENT_FAILED"
            item.error_message = failed.error_message or "H3 分段处理失败"
        else:
            item.status = "RUNNING"
    all_tasks = [
        candidate.generation_task
        for item in batch.items
        for candidate in item.segments
        if candidate.generation_task is not None
    ]
    if batch.items and all(item.status == TaskStatus.SUCCESS.value for item in batch.items):
        batch.status = TaskStatus.SUCCESS.value
    elif any(candidate.status in H3_ACTIVE_TASK_STATUSES for candidate in all_tasks):
        batch.status = "ACTIVE"
    elif any(item.status == "FAILED" for item in batch.items):
        batch.status = "FAILED"
    else:
        batch.status = "ACTIVE"


def activate_next_h3_soft_chain_segment(
    db: Session,
    completed_task: GenerationTask,
    *,
    anchor_path: str,
    anchor_sha256: str,
) -> GenerationTask | None:
    """Bind the previous visible final frame and enqueue exactly one successor."""

    segment = completed_task.segment
    if (
        completed_task.workflow_type != H3_WORKFLOW
        or segment is None
        or segment.h3_config is None
        or segment.h3_config.continuity_mode != "soft_chain"
    ):
        return None
    item = segment.batch_item
    batch = item.batch
    if segment.segment_index >= item.h3_config.segment_count - 1:
        return None
    next_segment = next(
        (
            candidate
            for candidate in item.segments
            if candidate.segment_index == segment.segment_index + 1
        ),
        None,
    )
    if next_segment is None or next_segment.h3_config is None:
        raise H3WorkbenchError("H3 soft_chain 下一分段快照缺失")
    existing_task = next_segment.generation_task
    if (
        existing_task is not None
        and existing_task.status != H3_REGENERATION_WAITING
    ):
        return existing_task
    if next_segment.h3_config.previous_segment_id != segment.id:
        raise H3WorkbenchError("H3 soft_chain 依赖链不一致")
    images = _effective_reference_assets(batch, item)
    config = next_segment.h3_config
    config.continuity_anchor_path = anchor_path
    config.continuity_anchor_sha256 = anchor_sha256
    audio_seconds = (
        config.requested_generation_duration_seconds
        - batch.h3_config.generation_tail_seconds
    )
    content_input: dict[str, object] = {
        "schema": "h3.segment-input.v1",
        "user_id": completed_task.user_id,
        "segment_text": next_segment.script_text,
        "segment_audio_sha256": config.segment_audio_sha256,
        "prompt_sha256": config.prompt_sha256,
        "reference_video_sha256": _segment_motion_reference(
            item, next_segment
        )[2],
        "ordered_identity_image_sha256s": [str(image["sha256"]) for image in images],
        "continuity_mode": config.continuity_mode,
        "continuity_anchor_sha256": anchor_sha256,
        "resolution": {
            "aspect_ratio": item.h3_config.aspect_ratio,
            "megapixels": item.h3_config.megapixels,
            "multiple": 32,
        },
        "duration": {
            "audio_seconds": audio_seconds,
            "generation_tail_seconds": batch.h3_config.generation_tail_seconds,
            "requested_seconds": config.requested_generation_duration_seconds,
            "frames": config.quantized_frame_count,
        },
        "workflow_template_sha256": H3_WORKFLOW_TEMPLATE_CANONICAL_SHA256,
        "adapter_version": H3_ADAPTER_VERSION,
        "output_contract_version": H3_OUTPUT_CONTRACT_VERSION,
        "instance_type": json.loads(batch.h3_config.fee_snapshot_json)["instance_type"],
    }
    config.input_sha256 = _content_digest(content_input)
    config.idempotency_sha256 = _content_digest(
        {
            "user_id": completed_task.user_id,
            "batch_id": batch.id,
            "segment_id": next_segment.id,
            "content_input_sha256": config.input_sha256,
        }
    )
    task = _create_segment_task(
        completed_task.user,
        batch,
        item,
        next_segment,
        instance_type=str(content_input["instance_type"]),
        reused_task=None,
        bind_relationships=(existing_task is None),
    )
    if existing_task is None:
        next_segment.status = TaskStatus.PENDING.value
        db.add(task)
        return task

    previous_payload = _json_object(existing_task.input_payload)
    replacement_payload = _json_object(task.input_payload)
    regeneration = previous_payload.get("_h3_manual_regeneration")
    history = previous_payload.get("_h3_regeneration_history")
    if not isinstance(regeneration, dict) or not str(
        regeneration.get("request_key") or ""
    ).strip():
        raise H3WorkbenchError("H3 下游重生成缺少确认快照")
    replacement_parameters = replacement_payload.get("parameters")
    if not isinstance(replacement_parameters, dict):
        raise H3WorkbenchError("H3 下游重生成输入快照损坏")
    replacement_parameters["seed"] = _regeneration_seed(
        replacement_parameters.get("seed"),
        request_key=str(regeneration["request_key"]),
        segment_id=next_segment.id,
    )
    replacement_payload["_h3_manual_regeneration"] = regeneration
    if isinstance(history, list):
        replacement_payload["_h3_regeneration_history"] = history
    existing_task.input_payload = json.dumps(replacement_payload, ensure_ascii=False)
    existing_task.image_path = task.image_path
    existing_task.audio_path = task.audio_path
    existing_task.image_original_name = task.image_original_name
    existing_task.audio_original_name = task.audio_original_name
    existing_task.audio_duration_seconds = task.audio_duration_seconds
    existing_task.start_seconds = task.start_seconds
    existing_task.end_seconds = task.end_seconds
    existing_task.prompt = task.prompt
    existing_task.status = TaskStatus.PENDING.value
    existing_task.created_at = datetime.now(timezone.utc)
    next_segment.status = "TASK_CREATED"
    return existing_task


def cancel_h3_workbench_quote(db: Session, user: User, batch_id: str, *, request_key: str, token: str):
    if not request_key or len(request_key) > 100:
        raise H3WorkbenchError("撤销费用预览必须提供有效 request_key")
    batch = get_h3_batch(db, user, batch_id)
    if batch is None or batch.h3_config is None:
        raise H3WorkbenchError("H3 批次不存在")
    if token != quote_token(batch):
        raise H3WorkbenchError("费用预览凭据不匹配，请重新读取状态")
    receipt = json.loads(batch.h3_config.fee_snapshot_json or "{}")
    if batch.status == "CANCELLED" and receipt.get("quote_cancellation"):
        return batch
    if not claim_quote(db, batch, "CANCELLED"):
        raise H3WorkbenchError("批次已确认、状态已变化或存在任务，不能按费用预览撤销")
    return finish_quote_cancellation(db, batch, request_key)


def confirm_h3_workbench_batch(db: Session, user: User, batch_id: str, *, cost_confirmed: object,
                               device_identity: WorkbenchIdentity | None = None):
    if not _strict_bool(cost_confirmed, "H3 费用确认"):
        raise H3WorkbenchError("必须明确确认 H3 费用")
    batch = get_h3_batch(db, user, batch_id)
    if batch is None or batch.h3_config is None:
        raise H3WorkbenchError("H3 批次不存在")
    if batch.h3_config.confirmed_at is not None:
        return batch
    require_new_work(db, user_id=user.id, identity=device_identity)
    if not claim_quote(db, batch, "ACTIVE"):
        batch = get_h3_batch(db, user, batch_id)
        if batch is not None and batch.h3_config.confirmed_at is not None:
            return batch
        raise H3WorkbenchError("费用预览已撤销或状态发生变化，请重新读取状态")
    try:
        bind_new_operation(
            db, user_id=user.id, identity=device_identity, operation_kind="h3.generate",
            request_snapshot={"batch_id": batch.id, "fee_snapshot": batch.h3_config.fee_snapshot_json},
            resources=[("generation_segment", segment.id) for item in batch.items for segment in item.segments],
        )
        return _confirm_h3_workbench_batch(db, user, batch_id, cost_confirmed=cost_confirmed)
    except Exception:
        db.rollback()
        raise


def _confirm_h3_workbench_batch(
    db: Session,
    user: User,
    batch_id: str,
    *,
    cost_confirmed: object,
) -> GenerationBatch:
    if not _strict_bool(cost_confirmed, "H3 费用确认"):
        raise H3WorkbenchError("必须确认 H3 每个未复用分段都会产生 RunningHub 费用")
    batch = get_h3_batch(db, user, batch_id)
    if batch is None or batch.h3_config is None:
        raise H3WorkbenchError("H3 批次不存在")
    if batch.h3_config.confirmed_at is not None:
        return batch
    try:
        account_ids = validate_h3_account_selection(
            db,
            user,
            json.loads(batch.runninghub_execution_account_ids_json or "[]"),
        )
    except (json.JSONDecodeError, H3PoolValidationError) as exc:
        raise H3WorkbenchError(str(exc)) from exc
    instance_type = _selected_instance_type(db, account_ids)
    now = datetime.now(timezone.utc)
    paid_calls = 0
    reused_count = 0
    for item in batch.items:
        if item.h3_config.audio_batch_id != H3_UPLOADED_AUDIO_BATCH_MARKER:
            audio_task = _audio_task_for_row(
                db,
                user,
                {
                    "audio_batch_id": item.h3_config.audio_batch_id,
                    "audio_item_id": item.h3_config.audio_item_id,
                    "audio_generation_version": item.h3_config.audio_generation_version,
                    "script_text": json.loads(item.manifest_json)["script_text"],
                },
            )
            if (
                audio_task.status != AudioTaskStatus.SUCCESS.value
                or audio_task.reviewed_at is None
            ):
                raise H3WorkbenchError(
                    f"第 {item.row_key} 行 MiniMax 声音尚未审核；请先试听并明确审核声音"
                )
        item.status = "QUEUED"
        item.audio_status = "AUDIO_FROZEN_H3"
        for segment in sorted(item.segments, key=lambda value: value.segment_index):
            if (
                segment.h3_config.continuity_mode == "soft_chain"
                and segment.segment_index > 0
            ):
                segment.status = "WAITING_DEPENDENCY"
                paid_calls += 1
                continue
            reused = _reusable_task_for_input(
                db,
                user,
                segment.h3_config.input_sha256,
            )
            task = _create_segment_task(
                user,
                batch,
                item,
                segment,
                instance_type=instance_type,
                reused_task=reused,
            )
            segment.status = (
                TaskStatus.SUCCESS.value if reused else "TASK_CREATED"
            )
            if reused:
                reused_config = (
                    reused.segment.h3_config if reused.segment is not None else None
                )
                direct = parse_h3_direct_delivery(reused.output_metadata)
                if direct is not None:
                    segment.h3_config.normalized_video_path = None
                    segment.h3_config.normalized_video_sha256 = None
                    segment.video_path = None
                else:
                    if (
                        reused_config is None
                        or not reused_config.normalized_video_path
                        or not reused_config.normalized_video_sha256
                    ):
                        raise H3WorkbenchError("H3 可复用任务缺少标准化结果快照")
                    segment.h3_config.normalized_video_path = (
                        reused_config.normalized_video_path
                    )
                    segment.h3_config.normalized_video_sha256 = (
                        reused_config.normalized_video_sha256
                    )
                    segment.video_path = reused_config.normalized_video_path
                reused_count += 1
            else:
                paid_calls += 1
            db.add(task)
    fee_snapshot = {
        "schema": "runninghub.h3-fee-confirmation.v1",
        "cost_confirmed": True,
        "segment_count": sum(len(item.segments) for item in batch.items),
        "estimated_paid_calls": paid_calls,
        "reusable_result_count": reused_count,
        "selected_account_ids": account_ids,
        "instance_type": instance_type,
        "confirmed_at": now.isoformat(),
    }
    batch.h3_config.fee_snapshot_json = json.dumps(fee_snapshot, ensure_ascii=False)
    batch.h3_config.confirmed_at = now
    batch.status = "ACTIVE"
    db.commit()
    confirmed = get_h3_batch(db, user, batch.id)
    if confirmed is None:
        raise H3WorkbenchError("H3 批次确认后无法读取")
    return confirmed


def h3_item_payload(item: GenerationBatchItem) -> dict[str, object]:
    uploaded_audio = (
        item.h3_config.audio_batch_id == H3_UPLOADED_AUDIO_BATCH_MARKER
    )
    return {
        "item_id": item.id,
        "row_id": item.row_key,
        "status": item.status,
        "audio_status": item.audio_status,
        "continuity_mode": item.h3_config.continuity_mode,
        "reference_video_original_name": item.h3_config.reference_video_original_name,
        "audio_duration_seconds": item.h3_config.audio_duration_seconds,
        "input_audio_download_url": (
            f"/api/workbench/h3-items/{item.id}/audio"
            if uploaded_audio
            else (
                f"/api/workbench/audio-batches/{item.h3_config.audio_batch_id}"
                f"/items/{item.h3_config.audio_item_id}/audio"
            )
        ),
        "input_raw_cues_download_url": (
            f"/api/workbench/h3-items/{item.id}/raw-cues"
        ),
        # Compatibility aliases for local workbench builds created before the
        # H3-generated audio became the final authority.
        "authoritative_audio_download_url": (
            f"/api/workbench/h3-items/{item.id}/audio"
            if uploaded_audio
            else (
                f"/api/workbench/audio-batches/{item.h3_config.audio_batch_id}"
                f"/items/{item.h3_config.audio_item_id}/audio"
            )
        ),
        "raw_cues_download_url": f"/api/workbench/h3-items/{item.id}/raw-cues",
        "effective_resolution": {
            "aspect_ratio": item.h3_config.aspect_ratio,
            "megapixels": item.h3_config.megapixels,
            "multiple": item.h3_config.multiple,
        },
        "segments": [
            {
                "segment_id": segment.id,
                "index": segment.segment_index,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "script_text": segment.script_text,
                "status": (
                    segment.generation_task.status
                    if segment.generation_task is not None
                    else segment.status
                ),
                "requested_generation_duration_seconds": (
                    segment.h3_config.requested_generation_duration_seconds
                ),
                "quantized_frame_count": segment.h3_config.quantized_frame_count,
                "normalized_timeline_duration_seconds": (
                    _h3_segment_output_value(
                        segment, "normalized_timeline_duration_seconds"
                    )
                ),
                "head_trim": _h3_segment_output_value(segment, "head_trim"),
                "dependency_segment_id": segment.h3_config.previous_segment_id,
                "has_continuity_anchor": bool(
                    segment.h3_config.continuity_anchor_path
                ),
                "invalidated_at": (
                    segment.h3_config.invalidated_at.isoformat()
                    if segment.h3_config.invalidated_at
                    else None
                ),
                "can_regenerate": bool(
                    segment.generation_task is not None
                    and segment.generation_task.status == TaskStatus.SUCCESS.value
                    and segment.status == TaskStatus.SUCCESS.value
                    and segment.h3_config.invalidated_at is None
                    and _h3_segment_has_current_result(segment)
                ),
                "can_retry": bool(
                    segment.generation_task is not None
                    and segment.generation_task.status in RETRYABLE_TASK_STATUSES
                ),
                "can_cancel": bool(
                    segment.generation_task is not None
                    and segment.generation_task.status in H3_CANCELLABLE_TASK_STATUSES
                    and not (
                        segment.generation_task.status == TaskStatus.UPLOADING.value
                        and not segment.generation_task.runninghub_task_id
                    )
                ),
                "normalized_video_download_url": (
                    f"/api/workbench/h3-segments/{segment.id}/video"
                    if _h3_segment_has_current_result(segment)
                    else None
                ),
                "video_delivery": h3_segment_video_delivery(segment),
                # JYD downloads successful segments incrementally.  The URL is
                # stable across a manual regeneration, so consumers need an
                # immutable result identity to avoid keeping the previous MP4.
                "normalized_video_sha256": (
                    segment.h3_config.normalized_video_sha256
                    if (
                        segment.h3_config.normalized_video_path
                        and segment.h3_config.invalidated_at is None
                        and segment.generation_task is not None
                        and segment.generation_task.status == TaskStatus.SUCCESS.value
                    )
                    else None
                ),
                "completed_at": (
                    segment.generation_task.completed_at.isoformat()
                    if (
                        segment.generation_task is not None
                        and segment.generation_task.status == TaskStatus.SUCCESS.value
                        and segment.generation_task.completed_at is not None
                    )
                    else None
                ),
                "error_code": (
                    segment.generation_task.error_code
                    if segment.generation_task is not None
                    else segment.error_code
                ),
                "error_message": (
                    segment.generation_task.error_message
                    if segment.generation_task is not None
                    else segment.error_message
                ),
            }
            for segment in sorted(item.segments, key=lambda value: value.segment_index)
        ],
    }


def h3_batch_payload(batch: GenerationBatch) -> dict[str, object]:
    try:
        fee_snapshot = json.loads(batch.h3_config.fee_snapshot_json or "null")
    except json.JSONDecodeError:
        fee_snapshot = None
    return {
        "schema": "runninghub.h3-workbench-batch.v1",
        "batch_id": batch.id,
        "name": batch.name,
        "status": batch.status,
        "source_channel": batch.source_channel,
        "contract_sha256": batch.h3_config.input_sha256,
        "continuity_mode": batch.h3_config.continuity_mode,
        "reference_image_count": len(_reference_assets(batch)),
        "fee_snapshot": fee_snapshot,
        "items": [h3_item_payload(item) for item in batch.items],
        "quote_recovery": quote_capability(batch),
        "created_at": batch.created_at.isoformat(),
        "confirmed_at": (
            batch.h3_config.confirmed_at.isoformat()
            if batch.h3_config.confirmed_at
            else None
        ),
    }


def h3_account_payload(db: Session, user: User) -> dict[str, object]:
    try:
        refresh_h3_execution_account_balances(db, user)
        payload = h3_execution_account_summary(db, user)
    except H3PoolValidationError as exc:
        raise H3WorkbenchError(str(exc)) from exc
    payload["adapter_capability"] = {
        "schema": "runninghub.h3-adapter-capability.v1",
        "adapter_version": H3_ADAPTER_VERSION,
        "max_user_reference_images": 4,
        "reserved_continuity_anchor_images": 1,
        "max_effective_reference_images": 6,
        "reference_videos_per_row": 1,
        "continuity_modes": [
            {"value": "loop_anchor", "label": "首尾同图（默认）"},
            {"value": "fast", "label": "快速并行"},
            {"value": "soft_chain", "label": "连续模式（尾帧串行）"},
        ],
        "default_continuity_mode": H3_DEFAULT_CONTINUITY_MODE,
        "aspect_ratios": [
            {
                "value": "9:16 (Portrait Widescreen)",
                "label": "9:16 竖屏",
            },
            {
                "value": "16:9 (Widescreen)",
                "label": "16:9 横屏",
            },
        ],
        "megapixels": {"minimum": 0.2, "maximum": 2.0, "step": 0.1},
        "generation_tail_seconds": {
            "default": H3_DEFAULT_GENERATION_TAIL_SECONDS,
            "minimum": 0,
            "maximum": 1,
        },
        "requested_duration_seconds": {"minimum": 4, "maximum": 15},
    }
    return payload


def h3_audio_sources_payload(db: Session, user: User) -> dict[str, object]:
    items = db.scalars(
        select(GenerationBatchItem)
        .join(GenerationBatch, GenerationBatchItem.batch_id == GenerationBatch.id)
        .join(
            AudioGenerationTask,
            AudioGenerationTask.batch_item_id == GenerationBatchItem.id,
        )
        .options(
            selectinload(GenerationBatchItem.batch),
            selectinload(GenerationBatchItem.audio_task),
        )
        .where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.source_channel == BATCH_SOURCE_NEW_WORKBENCH,
            AudioGenerationTask.status.in_(H3_REUSABLE_AUDIO_STATUSES),
            AudioGenerationTask.output_path.is_not(None),
            AudioGenerationTask.subtitle_path.is_not(None),
        )
        .order_by(
            AudioGenerationTask.completed_at.desc(),
            GenerationBatch.created_at.desc(),
            GenerationBatchItem.row_number,
        )
        .limit(200)
    ).unique().all()
    sources: list[dict[str, object]] = []
    for item in items:
        task = item.audio_task
        if task is None or not _h3_audio_source_is_reusable(task):
            continue
        try:
            manifest = json.loads(item.manifest_json)
        except (TypeError, json.JSONDecodeError):
            continue
        script_text = str(manifest.get("speech_script") or "").strip()
        if not script_text or script_text != task.speech_script.strip():
            continue
        sources.append(
            {
                "audio_batch_id": item.batch_id,
                "audio_item_id": item.id,
                "audio_generation_version": task.generation_version,
                "batch_name": item.batch.name,
                "row_key": item.row_key,
                "script_text": script_text,
                "status": task.status,
                "created_at": task.created_at.isoformat(),
                "audio_download_url": (
                    f"/api/workbench/audio-batches/{item.batch_id}"
                    f"/items/{item.id}/audio"
                ),
            }
        )
    return {
        "schema": "runninghub.h3-audio-sources.v1",
        "sources": sources,
    }


__all__ = [
    "H3WorkbenchError",
    "approve_h3_audio_source",
    "cancel_h3_segment",
    "confirm_h3_workbench_batch",
    "confirm_h3_segment_regeneration",
    "confirm_h3_segment_retry",
    "get_h3_batch",
    "h3_account_payload",
    "h3_audio_alignment_job_payload",
    "h3_audio_sources_payload",
    "h3_batch_payload",
    "prepare_h3_workbench_batch",
    "prepare_h3_segment_regeneration",
    "prepare_h3_segment_retry",
    "prepare_h3_audio_alignment_job",
    "sync_h3_task_hierarchy",
]
