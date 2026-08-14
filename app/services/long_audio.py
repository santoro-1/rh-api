from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    LongAudioProject,
    LongAudioProjectStatus,
    User,
)
from app.services.alignment import get_alignment_provider
from app.services.audio import format_duration_timecode, inspect_audio_duration
from app.services.media_segmentation import (
    DIGITAL_HUMAN_MAX_SEGMENT_SECONDS,
    MAX_SEGMENT_SECONDS,
    SegmentPlan,
    cut_audio_segment,
    cut_video_segment,
    detect_silence_midpoints,
    inspect_media_duration,
    plan_silence_segments,
)
from app.services.storage import (
    long_audio_project_dir,
    remove_directory,
    safe_relative_path,
    save_upload,
    task_upload_dir,
    to_relative_data_path,
)
from app.services.task_creation import (
    ValidatedTaskInput,
    create_generation_task,
    ensure_user_can_create_workflow,
    validate_task_input,
)
from app.services.workflow_configs import get_user_workflow_config
from app.workflows.base import WorkflowAsset


LTX_WORKFLOW = "ltx_lip_sync"
DIGITAL_HUMAN_WORKFLOW = "digital_human"
SUPPORTED_WORKFLOWS = {LTX_WORKFLOW, DIGITAL_HUMAN_WORKFLOW}
MAX_LONG_AUDIO_SECONDS = 60 * 60


class LongAudioError(ValueError):
    """A long-audio project cannot advance to the requested state."""


def _max_segment_seconds(workflow_type: str) -> float:
    return (
        DIGITAL_HUMAN_MAX_SEGMENT_SECONDS
        if workflow_type == DIGITAL_HUMAN_WORKFLOW
        else MAX_SEGMENT_SECONDS
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sync_linked_batch_item(project: LongAudioProject) -> None:
    """Keep an automatic batch row readable while media work is in flight."""

    item = project.batch_item
    if item is None:
        return
    if project.status == LongAudioProjectStatus.REVIEW.value:
        item.status = "AWAITING_REVIEW"
        item.audio_status = "AWAITING_REVIEW"
    elif project.status == LongAudioProjectStatus.FAILED.value:
        item.status = "FAILED"
        item.audio_status = "FAILED"
        item.error_code = project.error_code
        item.error_message = project.error_message
    elif project.status == LongAudioProjectStatus.CANCELLED.value:
        item.status = "CANCELLED"
        item.audio_status = "CANCELLED"
        item.error_code = None
        item.error_message = None
    elif project.status == LongAudioProjectStatus.COMPLETED.value:
        item.status = "SEGMENTS_CREATED"
        item.audio_status = "AUDIO_READY"
        item.error_code = None
        item.error_message = None
    else:
        item.status = "SEGMENTING"
        item.audio_status = "SEGMENTING"
        item.error_code = None
        item.error_message = None


def _confidence_for_method(method: str) -> str:
    if "timestamp" in method or "silence" in method:
        return "high"
    if method == "manual_review":
        return "reviewed"
    return "low"


def serialize_plans(plans: list[SegmentPlan] | tuple[SegmentPlan, ...]) -> str:
    return json.dumps(
        [
            {
                "index": plan.index,
                "startSeconds": round(plan.start_seconds, 3),
                "endSeconds": round(plan.end_seconds, 3),
                "scriptText": plan.script_text,
                "alignmentMethod": plan.alignment_method,
                "confidence": _confidence_for_method(plan.alignment_method),
            }
            for plan in plans
        ],
        ensure_ascii=False,
    )


def load_plans(project: LongAudioProject) -> list[SegmentPlan]:
    try:
        raw = json.loads(project.plan_json or "[]")
    except json.JSONDecodeError as exc:
        raise LongAudioError("分段方案损坏，请重新分析") from exc
    if not isinstance(raw, list) or not raw:
        raise LongAudioError("尚未生成可用的分段方案")
    plans: list[SegmentPlan] = []
    for position, value in enumerate(raw, start=1):
        if not isinstance(value, dict):
            raise LongAudioError("分段方案格式错误")
        try:
            plans.append(
                SegmentPlan(
                    index=position,
                    script_text=str(value["scriptText"]).strip(),
                    start_seconds=float(value["startSeconds"]),
                    end_seconds=float(value["endSeconds"]),
                    alignment_method=str(
                        value.get("alignmentMethod") or "manual_review"
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LongAudioError("分段方案格式错误") from exc
    return plans


def create_long_audio_project(
    db: Session,
    user: User,
    settings: Settings,
    *,
    name: str,
    script_text: str,
    audio: UploadFile,
    source_video: UploadFile,
    prompt_prefix: str,
    instance_type: str,
    seedvr2_enabled: bool = False,
    alignment_provider: str = "funasr_http",
    workflow_type: str = LTX_WORKFLOW,
    review_required: bool = False,
    digital_prompt: str = (
        "人物自然地说话，他的身体和手部随着说话的节奏做着自然且随意的动作，"
        "镜头保持稳定。"
    ),
    digital_resolution: int = 1024,
    digital_person_mode: int = 1,
) -> LongAudioProject:
    if workflow_type not in SUPPORTED_WORKFLOWS:
        raise LongAudioError("不支持的长音频工作流")
    ensure_user_can_create_workflow(user, workflow_type)
    clean_name = name.strip()
    clean_script = script_text.strip()
    clean_prefix = prompt_prefix.strip().rstrip("：:")
    if not 1 <= len(clean_name) <= 100:
        raise LongAudioError("项目名称需为 1–100 个字符")
    if workflow_type == LTX_WORKFLOW and not clean_script:
        raise LongAudioError("完整口播脚本不能为空")
    if len(clean_script) > 100_000:
        raise LongAudioError("完整口播脚本不能超过 100000 个字符")
    if workflow_type == LTX_WORKFLOW and (
        not clean_prefix or len(clean_prefix) > 500
    ):
        raise LongAudioError("人物与语言提示需为 1—500 个字符")
    if instance_type not in {"default", "plus"}:
        raise LongAudioError("实例类型只能为普通版 default 或 Plus")
    if workflow_type == LTX_WORKFLOW:
        get_alignment_provider(alignment_provider)
    else:
        if digital_person_mode != 1:
            raise LongAudioError("双人数字人模式暂未开放")
        clean_digital_prompt = digital_prompt.strip()
        if not clean_digital_prompt or len(clean_digital_prompt) > 2000:
            raise LongAudioError("数字人动作提示需为 1—2000 个字符")

    project_id = str(uuid.uuid4())
    directory = long_audio_project_dir(settings, user.id, project_id)
    try:
        audio_path, audio_name = save_upload(
            audio, directory, "audio", settings
        )
        visual_kind = "video" if workflow_type == LTX_WORKFLOW else "image"
        video_path, video_name = save_upload(
            source_video, directory, visual_kind, settings
        )
        duration = inspect_audio_duration(audio_path)
        max_segment_seconds = _max_segment_seconds(workflow_type)
        if duration <= max_segment_seconds + 0.01:
            raise LongAudioError(
                f"音频不超过 {max_segment_seconds:g} 秒，请直接使用普通上传入口"
            )
        if duration > MAX_LONG_AUDIO_SECONDS:
            raise LongAudioError("第一版长音频最多支持 60 分钟")
        if workflow_type == LTX_WORKFLOW:
            video_duration = inspect_media_duration(video_path)
            if video_duration + 0.05 < duration:
                raise LongAudioError(
                    f"源视频时长不足：视频 {video_duration:.1f} 秒，"
                    f"音频 {duration:.1f} 秒"
                )
    except Exception:
        remove_directory(directory)
        raise

    digital_instance_type = (
        get_user_workflow_config(user, DIGITAL_HUMAN_WORKFLOW).instance_type
        if workflow_type == DIGITAL_HUMAN_WORKFLOW
        else instance_type
    )
    project = LongAudioProject(
        id=project_id,
        user_id=user.id,
        name=clean_name,
        workflow_type=workflow_type,
        review_required=review_required,
        script_text=clean_script,
        audio_path=to_relative_data_path(audio_path, settings),
        audio_original_name=audio_name,
        video_path=to_relative_data_path(video_path, settings),
        video_original_name=video_name,
        duration_seconds=duration,
        parameters_json=json.dumps(
            {
                "prompt_prefix": clean_prefix,
                "instance_type": digital_instance_type,
                "seedvr2_enabled": (
                    bool(seedvr2_enabled)
                    if workflow_type == DIGITAL_HUMAN_WORKFLOW
                    else True
                ),
                "digital_prompt": (
                    clean_digital_prompt
                    if workflow_type == DIGITAL_HUMAN_WORKFLOW
                    else ""
                ),
                "resolution": digital_resolution,
                "person_mode": digital_person_mode,
            },
            ensure_ascii=False,
        ),
        alignment_provider=(
            alignment_provider
            if workflow_type == LTX_WORKFLOW
            else "vad_silence"
        ),
        status=LongAudioProjectStatus.PENDING_ANALYSIS.value,
        expires_at=_now() + timedelta(days=settings.upload_retention_days),
    )
    db.add(project)
    return project


def analyze_long_audio_project(
    project: LongAudioProject,
    settings: Settings,
) -> None:
    audio_path = safe_relative_path(project.audio_path, settings.data_dir)
    if not audio_path.is_file():
        raise LongAudioError("原始长音频文件不存在")
    if project.workflow_type == DIGITAL_HUMAN_WORKFLOW:
        plans = plan_silence_segments(
            project.duration_seconds,
            detect_silence_midpoints(audio_path),
            max_segment_seconds=DIGITAL_HUMAN_MAX_SEGMENT_SECONDS,
        )
        apply_alignment_plans(project, settings, plans, provider="vad_silence")
    else:
        provider = get_alignment_provider(project.alignment_provider)
        result = provider.align(audio_path, project.script_text)
        apply_alignment_plans(
            project,
            settings,
            list(result.plans),
            provider=result.provider,
        )


def apply_alignment_plans(
    project: LongAudioProject,
    settings: Settings,
    plans: list[SegmentPlan] | tuple[SegmentPlan, ...],
    *,
    provider: str,
) -> None:
    """Validate an alignment result before making it visible for review."""

    if not plans:
        raise LongAudioError("对齐服务没有生成分段")
    if len(plans) > settings.max_batch_items:
        raise LongAudioError(
            f"自动分段数量超过当前上限 {settings.max_batch_items}"
        )
    previous_end = 0.0
    max_segment_seconds = _max_segment_seconds(project.workflow_type)
    for position, plan in enumerate(plans, start=1):
        if plan.index != position:
            raise LongAudioError("对齐服务返回的分段序号不连续")
        if abs(plan.start_seconds - previous_end) > 0.05:
            raise LongAudioError("对齐服务返回的时间轴不连续")
        if (
            plan.end_seconds <= plan.start_seconds
            or plan.duration_seconds > max_segment_seconds + 0.01
        ):
            raise LongAudioError(
                f"对齐服务返回了无效或超过{max_segment_seconds:g}秒的分段"
            )
        previous_end = plan.end_seconds
    if abs(previous_end - project.duration_seconds) > 0.1:
        raise LongAudioError("对齐服务没有覆盖完整音频")
    if project.workflow_type == LTX_WORKFLOW:
        if _normalized_script(
            "".join(plan.script_text for plan in plans)
        ) != _normalized_script(project.script_text):
            raise LongAudioError("对齐服务没有完整映射原始脚本")
    project.plan_json = serialize_plans(plans)
    project.alignment_provider = provider
    project.status = (
        LongAudioProjectStatus.REVIEW.value
        if project.review_required
        else LongAudioProjectStatus.PENDING_CUT.value
    )
    if not project.review_required:
        project.confirmed_at = _now()
    project.error_code = None
    project.error_message = None
    sync_linked_batch_item(project)


def _normalized_script(value: str) -> str:
    return re.sub(r"\s+", "", value)


def validate_reviewed_plan(
    project: LongAudioProject,
    raw_segments: Any,
    *,
    max_segments: int,
) -> list[SegmentPlan]:
    if not isinstance(raw_segments, list) or not raw_segments:
        raise LongAudioError("分段方案不能为空")
    if len(raw_segments) > max_segments:
        raise LongAudioError(f"分段数量不能超过 {max_segments}")

    plans: list[SegmentPlan] = []
    previous_end = 0.0
    max_segment_seconds = _max_segment_seconds(project.workflow_type)
    for position, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise LongAudioError(f"第 {position} 段格式错误")
        try:
            start = float(raw.get("startSeconds"))
            end = float(raw.get("endSeconds"))
        except (TypeError, ValueError) as exc:
            raise LongAudioError(f"第 {position} 段时间格式错误") from exc
        text = str(raw.get("scriptText") or "").strip()
        if project.workflow_type == LTX_WORKFLOW and not text:
            raise LongAudioError(f"第 {position} 段脚本不能为空")
        expected_start = 0.0 if position == 1 else previous_end
        if abs(start - expected_start) > 0.05:
            raise LongAudioError(f"第 {position} 段与上一段时间不连续")
        start = expected_start
        if end <= start:
            raise LongAudioError(f"第 {position} 段结束时间必须大于开始时间")
        if end - start > max_segment_seconds + 0.01:
            raise LongAudioError(
                f"第 {position} 段不能超过 {max_segment_seconds:g} 秒"
            )
        plans.append(
            SegmentPlan(
                index=position,
                script_text=text,
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                alignment_method="manual_review",
            )
        )
        previous_end = end

    if abs(previous_end - project.duration_seconds) > 0.1:
        raise LongAudioError("最后一段必须覆盖到完整音频结尾")
    plans[-1] = SegmentPlan(
        index=plans[-1].index,
        script_text=plans[-1].script_text,
        start_seconds=plans[-1].start_seconds,
        end_seconds=project.duration_seconds,
        alignment_method="manual_review",
    )
    if project.workflow_type == LTX_WORKFLOW:
        joined = "".join(plan.script_text for plan in plans)
        if _normalized_script(joined) != _normalized_script(project.script_text):
            raise LongAudioError("所有分段脚本拼接后必须与原始脚本一致")
    return plans


def save_reviewed_plan(
    project: LongAudioProject,
    raw_segments: Any,
    settings: Settings,
) -> list[SegmentPlan]:
    if project.status != LongAudioProjectStatus.REVIEW.value:
        raise LongAudioError("当前状态不能修改分段方案")
    plans = validate_reviewed_plan(
        project,
        raw_segments,
        max_segments=settings.max_batch_items,
    )
    project.plan_json = serialize_plans(plans)
    project.error_code = None
    project.error_message = None
    return plans


def confirm_long_audio_project(project: LongAudioProject) -> None:
    if project.status == LongAudioProjectStatus.COMPLETED.value:
        return
    if project.status not in {
        LongAudioProjectStatus.REVIEW.value,
        LongAudioProjectStatus.FAILED.value,
    }:
        raise LongAudioError("当前状态不能确认生成")
    load_plans(project)
    project.status = LongAudioProjectStatus.PENDING_CUT.value
    project.confirmed_at = _now()
    project.error_code = None
    project.error_message = None
    sync_linked_batch_item(project)


def materialize_long_audio_project(
    db: Session,
    project: LongAudioProject,
    settings: Settings,
    *,
    precut_directory: Path | None = None,
) -> GenerationBatch:
    if (
        project.status == LongAudioProjectStatus.COMPLETED.value
        and project.batch_item is not None
    ):
        return project.batch_item.batch
    if (
        project.status == LongAudioProjectStatus.COMPLETED.value
        and project.batch_id
        and project.batch is not None
    ):
        return project.batch
    user = project.user
    workflow_type = project.workflow_type or LTX_WORKFLOW
    ensure_user_can_create_workflow(user, workflow_type)
    plans = load_plans(project)
    audio_source = safe_relative_path(project.audio_path, settings.data_dir)
    visual_source = safe_relative_path(project.video_path, settings.data_dir)
    if not audio_source.is_file() or not visual_source.is_file():
        raise LongAudioError("长音频项目的原始素材已丢失")
    try:
        parameters = json.loads(project.parameters_json)
    except json.JSONDecodeError as exc:
        raise LongAudioError("项目参数损坏") from exc
    prompt_prefix = str(
        parameters.get("prompt_prefix") or "一名人物用中文说"
    ).strip().rstrip("：:")
    instance_type = str(parameters.get("instance_type") or "plus")
    digital_prompt = str(
        parameters.get("digital_prompt")
        or "人物自然地说话，表情自然，动作自然，镜头保持稳定。"
    ).strip()

    if project.batch_item is not None:
        item = project.batch_item
        batch = item.batch
        item.status = "CREATING_SEGMENTS"
        item.audio_status = "SEGMENTING"
    else:
        request_key = f"long-audio:{project.id}"
        existing = db.scalar(
            select(GenerationBatch).where(
                GenerationBatch.user_id == project.user_id,
                GenerationBatch.request_key == request_key,
            )
        )
        if existing is not None:
            project.batch_id = existing.id
            project.status = LongAudioProjectStatus.COMPLETED.value
            project.completed_at = project.completed_at or _now()
            return existing

        batch = GenerationBatch(
            id=str(uuid.uuid4()),
            user_id=project.user_id,
            name=project.name,
            workflow_type=workflow_type,
            audio_mode="upload",
            review_required=False,
            request_key=request_key,
            status="ACTIVE",
            total_items=1,
        )
        item = GenerationBatchItem(
            id=str(uuid.uuid4()),
            batch=batch,
            row_number=1,
            row_key=f"LONG-{project.id[:8]}",
            manifest_json=json.dumps(
                {
                    "row_id": f"LONG-{project.id[:8]}",
                    "speech_script": project.script_text,
                    (
                        "source_video_file"
                        if workflow_type == LTX_WORKFLOW
                        else "source_image_file"
                    ): project.video_original_name,
                    "audio_file": project.audio_original_name,
                    "long_audio_project_id": project.id,
                },
                ensure_ascii=False,
            ),
            audio_status="AUDIO_READY",
            status="CREATING_SEGMENTS",
        )

    created_directories: list[Path] = []
    prepared_segments: list[
        tuple[GenerationSegment, ValidatedTaskInput, str, datetime]
    ] = []
    base_time = _now()
    try:
        for plan in plans:
            task_id = str(uuid.uuid4())
            upload_dir = task_upload_dir(settings, user.id, task_id)
            created_directories.append(upload_dir)
            segment_audio = upload_dir / f"segment-{plan.index:03d}.mp3"
            visual_suffix = (
                ".mp4"
                if workflow_type == LTX_WORKFLOW
                else (Path(project.video_original_name).suffix or ".png")
            )
            segment_visual = upload_dir / (
                f"segment-{plan.index:03d}{visual_suffix}"
            )
            if precut_directory is None:
                cut_audio_segment(
                    audio_source,
                    segment_audio,
                    start_seconds=plan.start_seconds,
                    end_seconds=plan.end_seconds,
                )
            else:
                source_audio = (
                    precut_directory
                    / "audio"
                    / f"segment-{plan.index:03d}.mp3"
                )
                source_video = precut_directory / "video" / (
                    f"segment-{plan.index:03d}.mp4"
                )
                if not source_audio.is_file() or (
                    workflow_type == LTX_WORKFLOW and not source_video.is_file()
                ):
                    raise LongAudioError(
                        f"远程节点没有返回第 {plan.index} 段完整素材"
                    )
                segment_audio.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_audio, segment_audio)
                if workflow_type == LTX_WORKFLOW:
                    shutil.copyfile(source_video, segment_visual)
            segment_duration = inspect_audio_duration(segment_audio)
            max_segment_seconds = _max_segment_seconds(workflow_type)
            if segment_duration <= 0 or segment_duration > max_segment_seconds + 0.2:
                raise LongAudioError(
                    f"第 {plan.index} 段远程音频时长无效"
                )
            if abs(segment_duration - plan.duration_seconds) > 0.75:
                raise LongAudioError(
                    f"第 {plan.index} 段远程音频时长与分段方案不一致"
                )
            if workflow_type == LTX_WORKFLOW and precut_directory is None:
                cut_video_segment(
                    visual_source,
                    segment_visual,
                    start_seconds=plan.start_seconds,
                    duration_seconds=segment_duration,
                )
            elif workflow_type == LTX_WORKFLOW:
                video_duration = inspect_media_duration(segment_visual)
                if video_duration + 0.1 < segment_duration:
                    raise LongAudioError(
                        f"第 {plan.index} 段远程视频短于对应音频"
                    )
            else:
                shutil.copyfile(visual_source, segment_visual)
            audio_relative = to_relative_data_path(segment_audio, settings)
            visual_relative = to_relative_data_path(segment_visual, settings)
            prompt = (
                f"{prompt_prefix}：“{plan.script_text}”"
                if workflow_type == LTX_WORKFLOW
                else digital_prompt
            )
            segment = GenerationSegment(
                id=str(uuid.uuid4()),
                batch_item_id=item.id,
                segment_index=plan.index,
                script_text=plan.script_text,
                start_seconds=plan.start_seconds,
                end_seconds=plan.end_seconds,
                audio_path=audio_relative,
                video_path=(
                    visual_relative if workflow_type == LTX_WORKFLOW else None
                ),
                prompt=prompt,
                alignment_method=plan.alignment_method,
                status="TASK_CREATED",
            )
            primary_name = (
                f"{Path(project.video_original_name).stem}-"
                f"{plan.index:03d}{visual_suffix}"
            )
            assets = [
                WorkflowAsset(
                    name=(
                        "video"
                        if workflow_type == LTX_WORKFLOW
                        else "image"
                    ),
                    kind=(
                        "video"
                        if workflow_type == LTX_WORKFLOW
                        else "image"
                    ),
                    relative_path=visual_relative,
                    original_name=primary_name,
                ),
                WorkflowAsset(
                    name="audio",
                    kind="audio",
                    relative_path=audio_relative,
                    original_name=(
                        f"{Path(project.audio_original_name).stem}-"
                        f"{plan.index:03d}.mp3"
                    ),
                ),
            ]
            task_parameters = {
                "prompt": prompt,
                "instance_type": instance_type,
            }
            if workflow_type == DIGITAL_HUMAN_WORKFLOW:
                task_parameters.update(
                    {
                        "start_time": "0:00",
                        "end_time": format_duration_timecode(segment_duration),
                        "person_mode": str(
                            parameters.get("person_mode") or "1"
                        ),
                        "resolution": str(
                            parameters.get("resolution") or "1024"
                        ),
                        "instance_type": instance_type,
                        "seedvr2_enabled": parameters.get(
                            "seedvr2_enabled", False
                        ),
                    }
                )
            validated = validate_task_input(
                user,
                workflow_type,
                assets,
                task_parameters,
                {
                    "has_custom_audio": True,
                    "audio_duration_seconds": segment_duration,
                },
            )
            prepared_segments.append(
                (
                    segment,
                    validated,
                    task_id,
                    base_time + timedelta(microseconds=plan.index),
                )
            )

        # FFmpeg may run for minutes. Do all CPU/disk work before opening the
        # SQLite write transaction, then persist the complete handoff at once.
        db.add_all([batch, item])
        for segment, validated, task_id, created_at in prepared_segments:
            db.add(segment)
            create_generation_task(
                db,
                user,
                validated,
                task_id=task_id,
                segment_id=segment.id,
                created_at=created_at,
            )
        item.status = "SEGMENTS_CREATED"
        item.merged_video_status = "MERGE_PENDING"
        item.merged_video_path = None
        item.merged_video_error = None
        if project.batch_item_id is None:
            project.batch_id = batch.id
            project.batch_item = item
        project.status = LongAudioProjectStatus.COMPLETED.value
        project.completed_at = _now()
        project.error_code = None
        project.error_message = None
        sync_linked_batch_item(project)
        db.flush()
    except Exception:
        for directory in created_directories:
            remove_directory(directory)
        raise

    return batch
