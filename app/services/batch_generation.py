from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AudioGenerationTask,
    BATCH_SOURCE_LEGACY_WEB,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    LongAudioProject,
    LongAudioProjectStatus,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    StagedAsset,
    User,
    VoiceAssetStatus,
)
from app.services.audio import format_duration_timecode, inspect_audio_duration
from app.services.batch_assets import load_available_assets
from app.services.batch_manifests import (
    DIGITAL_HUMAN_WORKFLOW,
    LTX_LIP_SYNC_WORKFLOW,
    canonical_filename,
)
from app.services.long_audio import MAX_LONG_AUDIO_SECONDS
from app.services.runninghub_pool import (
    RunningHubPoolSelectionUnavailableError,
    assigned_execution_account_ids,
)
from app.services.media_segmentation import (
    DIGITAL_HUMAN_GENERATION_TAIL_SECONDS,
    DIGITAL_HUMAN_MAX_SEGMENT_SECONDS,
    MAX_SEGMENT_SECONDS,
    inspect_media_duration,
)
from app.services.storage import (
    long_audio_project_dir,
    materialize_staged_asset,
    remove_directory,
    safe_relative_path,
    task_upload_dir,
    to_relative_data_path,
)
from app.services.speech.minimax import (
    parse_pronunciation_tones,
    validate_synthesis_options,
)
from app.services.task_creation import (
    TaskCreationError,
    create_generation_task,
    ensure_user_can_create_workflow,
    validate_task_input,
)
from app.services.workflow_configs import get_user_workflow_config
from app.workflows.base import WorkflowAsset


class BatchValidationError(ValueError):
    """The batch cannot be created until all reported rows are corrected."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("批量清单包含无效数据")
        self.errors = errors


@dataclass(frozen=True)
class BatchRowPlan:
    """Validated plan for one manifest row, before any paid task is created.

    row_number is the human-facing Excel/CSV line; row_key is the user's stable
    script/task number. staged_assets owns temporary uploads, while parameters
    and asset_metadata are the exact validated values passed to task creation.
    """

    row_number: int
    row_key: str
    manifest: dict[str, str]
    staged_assets: dict[str, StagedAsset]
    parameters: dict[str, Any]
    asset_metadata: dict[str, Any]


@dataclass(frozen=True)
class SpeechBatchOptions:
    """One batch-wide MiniMax configuration shared by all script rows.

    config and voice are bound to the current local user and MiniMax account;
    numerical fields use MiniMax API units. pronunciation_tones contains parsed
    replacement rules, and review_required controls the audio approval gate.
    """

    config: MiniMaxConfig
    voice: MiniMaxVoiceAsset
    model: str
    speed: float
    volume: float
    pitch: int
    language_boost: str
    output_format: str
    pronunciation_tones: list[str]
    review_required: bool


@dataclass(frozen=True)
class BatchPlan:
    """All validated inputs required for one atomic local batch creation."""

    workflow_type: str
    audio_mode: str
    rows: list[BatchRowPlan]
    assets: list[StagedAsset]
    speech_options: SpeechBatchOptions | None = None
    review_required: bool = False
    video_review_required: bool = False
    defer_primary_until_video: bool = False
    source_channel: str = BATCH_SOURCE_LEGACY_WEB


def _person_mode(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or normalized in {"1", "单人", "single"}:
        return "1"
    if normalized in {"0", "双人", "dual"}:
        raise ValueError("双人数字人模式暂未开放")
    raise ValueError("人物模式不合法")


def _instance_type(value: str, fallback: str) -> str:
    normalized = value.strip().casefold()
    if not normalized:
        normalized = fallback
    aliases = {
        "stand": "default",
        "stand 24g": "default",
        "24g": "default",
        "default": "default",
        "plus": "plus",
        "plus 48g": "plus",
        "48g": "plus",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("运行实例只能填写 Stand 或 Plus") from exc


def _asset_index(assets: list[StagedAsset]) -> dict[tuple[str, str], StagedAsset]:
    index: dict[tuple[str, str], StagedAsset] = {}
    duplicates: set[str] = set()
    for asset in assets:
        key = (asset.kind, canonical_filename(asset.original_name))
        if key in index:
            duplicates.add(asset.original_name)
        index[key] = asset
    if duplicates:
        names = "、".join(sorted(duplicates))
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": f"存在同名素材：{names}"}]
        )
    return index


def _resolve_asset(
    index: dict[tuple[str, str], StagedAsset],
    *,
    filename: str,
    kind: str,
    label: str,
) -> StagedAsset:
    if not filename.strip():
        raise ValueError(f"缺少{label}")
    asset = index.get((kind, canonical_filename(filename)))
    if asset is None:
        raise ValueError(f"找不到{label}：{filename}")
    return asset


def _resolve_asset_reference(
    assets_by_id: dict[str, StagedAsset],
    index: dict[tuple[str, str], StagedAsset],
    *,
    asset_id: str,
    filename: str,
    kind: str,
    label: str,
) -> StagedAsset:
    """Prefer the page's explicit sequence binding; fall back for old manifests."""

    if asset_id.strip():
        asset = assets_by_id.get(asset_id.strip())
        if asset is None:
            raise ValueError(f"{label}序号对应的素材不存在")
        if asset.kind != kind:
            raise ValueError(f"{label}序号对应的素材类型不正确")
        return asset
    return _resolve_asset(
        index,
        filename=filename,
        kind=kind,
        label=label,
    )


def _number(value: Any, label: str, cast: type[int] | type[float]):
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}格式不正确") from exc


def _validate_speech_options(
    db: Session,
    user: User,
    raw: dict[str, Any] | None,
) -> SpeechBatchOptions:
    config = user.minimax_config
    if (
        config is None
        or not config.api_key_encrypted
        or not config.account_binding_id
        or not config.credential_fingerprint
    ):
        raise ValueError("当前账号尚未配置 MiniMax API Key")
    raw = raw or {}
    voice_asset_id = str(raw.get("voiceAssetId") or "")
    voice = db.scalar(
        select(MiniMaxVoiceAsset).where(
            MiniMaxVoiceAsset.id == voice_asset_id,
            MiniMaxVoiceAsset.user_id == user.id,
            MiniMaxVoiceAsset.config_id == config.id,
            MiniMaxVoiceAsset.account_binding_id
            == config.account_binding_id,
            MiniMaxVoiceAsset.is_saved.is_(True),
            MiniMaxVoiceAsset.status.in_(
                {
                    VoiceAssetStatus.READY.value,
                    VoiceAssetStatus.ACTIVE.value,
                }
            ),
        )
    )
    if voice is None:
        raise ValueError("请选择声音管理中已经保存成功的音色")
    if raw.get("costConfirmed") is not True:
        raise ValueError("请先确认语音文本生成及可能的音色费用")

    model = str(raw.get("model") or "speech-2.8-hd").strip()
    if not model or len(model) > 100:
        raise ValueError("MiniMax 语音模型名称不合法")
    speed = _number(raw.get("speed", 1), "语速", float)
    volume = _number(raw.get("volume", 1), "音量", float)
    pitch = _number(raw.get("pitch", 0), "音调", int)
    language_boost = str(raw.get("languageBoost") or "auto").strip()
    output_format = str(raw.get("outputFormat") or "mp3").strip().lower()
    pronunciation_tones = parse_pronunciation_tones(
        str(raw.get("pronunciationTones") or "")
    )
    review_required = raw.get("reviewRequired") is True
    if not language_boost or len(language_boost) > 50:
        raise ValueError("语言增强参数不合法")
    # A short sample verifies every numeric option before row-level scripts.
    validate_synthesis_options(
        text="校验",
        speed=speed,
        volume=volume,
        pitch=pitch,
        output_format=output_format,
    )
    return SpeechBatchOptions(
        config=config,
        voice=voice,
        model=model,
        speed=speed,
        volume=volume,
        pitch=pitch,
        language_boost=language_boost,
        output_format=output_format,
        pronunciation_tones=pronunciation_tones,
        review_required=review_required,
    )


def validate_workbench_audio_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    rows: list[dict[str, str]],
    speech_options: dict[str, Any],
    resolution: str = "1024",
) -> BatchPlan:
    """Validate a workbench TTS batch without accepting picture assets.

    The selected project picture belongs to Module 4A.  It is deliberately
    absent here and will be validated against the digital-human adapter when
    the approved audio is handed off to video generation.
    """

    if not rows:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "批量清单没有数据行"}]
        )
    if len(rows) > settings.max_batch_items:
        raise BatchValidationError(
            [
                {
                    "rowNumber": 0,
                    "rowId": "",
                    "message": f"单批最多 {settings.max_batch_items} 条任务",
                }
            ]
        )
    try:
        ensure_user_can_create_workflow(user, DIGITAL_HUMAN_WORKFLOW)
        resolved_speech = _validate_speech_options(db, user, speech_options)
        workflow_config = get_user_workflow_config(user, DIGITAL_HUMAN_WORKFLOW)
    except (TaskCreationError, ValueError) as exc:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": str(exc)}]
        ) from exc

    errors: list[dict[str, Any]] = []
    plans: list[BatchRowPlan] = []
    seen_row_keys: set[str] = set()
    for position, row in enumerate(rows, start=1):
        row_key = str(row.get("row_id") or "").strip()
        try:
            if not row_key:
                raise ValueError("任务编号不能为空")
            if len(row_key) > 100:
                raise ValueError("任务编号不能超过 100 个字符")
            folded_key = row_key.casefold()
            if folded_key in seen_row_keys:
                raise ValueError("任务编号重复")
            seen_row_keys.add(folded_key)
            script = str(row.get("speech_script") or "").strip()
            validate_synthesis_options(
                text=script,
                speed=resolved_speech.speed,
                volume=resolved_speech.volume,
                pitch=resolved_speech.pitch,
                output_format=resolved_speech.output_format,
            )
            prompt = str(
                row.get("prompt")
                or workflow_config.default_prompt
                or "人物自然地说话"
            ).strip()
            plans.append(
                BatchRowPlan(
                    row_number=position,
                    row_key=row_key,
                    manifest={
                        "row_id": row_key,
                        "speech_script": script,
                        "prompt": prompt,
                    },
                    staged_assets={},
                    parameters={
                        "prompt": prompt,
                        "start_time": "0:00",
                        "end_time": "0:01",
                        "resolution": str(resolution or "1024"),
                        "person_mode": "1",
                        "instance_type": workflow_config.instance_type,
                        # New workbench 4A always includes SeedVR2.
                        "seedvr2_enabled": True,
                    },
                    asset_metadata={},
                )
            )
        except ValueError as exc:
            errors.append(
                {
                    "rowNumber": position,
                    "rowId": row_key,
                    "message": str(exc),
                }
            )
    if errors:
        raise BatchValidationError(errors)
    return BatchPlan(
        workflow_type=DIGITAL_HUMAN_WORKFLOW,
        audio_mode="minimax",
        rows=plans,
        assets=[],
        speech_options=resolved_speech,
        review_required=True,
        video_review_required=False,
        defer_primary_until_video=True,
        source_channel=BATCH_SOURCE_NEW_WORKBENCH,
    )


def validate_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    workflow_type: str,
    rows: list[dict[str, str]],
    asset_ids: list[str],
    batch_parameters: dict[str, str] | None = None,
    audio_mode: str = "upload",
    speech_options: dict[str, Any] | None = None,
    review_required: bool = False,
    video_review_required: bool = False,
) -> BatchPlan:
    """Return every row error together; no task is written during validation."""

    batch_parameters = batch_parameters or {}
    if audio_mode not in {"upload", "minimax"}:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "不支持的音频准备方式"}]
        )
    if workflow_type not in {
        DIGITAL_HUMAN_WORKFLOW,
        LTX_LIP_SYNC_WORKFLOW,
    }:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "不支持的批量工作流"}]
        )
    if not rows:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "批量清单没有数据行"}]
        )
    if len(rows) > settings.max_batch_items:
        raise BatchValidationError(
            [
                {
                    "rowNumber": 0,
                    "rowId": "",
                    "message": f"单批最多 {settings.max_batch_items} 条任务",
                }
            ]
        )
    try:
        ensure_user_can_create_workflow(user, workflow_type)
        assets = load_available_assets(db, user, asset_ids)
        assets_by_id = {asset.id: asset for asset in assets}
        index: dict[tuple[str, str], StagedAsset] = {}
        resolved_speech_options = (
            _validate_speech_options(db, user, speech_options)
            if audio_mode == "minimax"
            else None
        )
    except (TaskCreationError, ValueError) as exc:
        if isinstance(exc, BatchValidationError):
            raise
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": str(exc)}]
        ) from exc

    errors: list[dict[str, Any]] = []
    plans: list[BatchRowPlan] = []
    seen_row_keys: set[str] = set()
    workflow_config = get_user_workflow_config(user, workflow_type)
    ordered_primary_assets: list[StagedAsset] = []
    primary_asset_id_key = (
        "image_asset_id"
        if workflow_type == DIGITAL_HUMAN_WORKFLOW
        else "source_video_asset_id"
    )
    uses_primary_sequence_bindings = all(
        str(row.get(primary_asset_id_key) or "").strip()
        for row in rows
    )
    if audio_mode == "minimax":
        expected_kind = (
            "image"
            if workflow_type == DIGITAL_HUMAN_WORKFLOW
            else "video"
        )
        ordered_primary_assets = [
            asset for asset in assets if asset.kind == expected_kind
        ]
        if uses_primary_sequence_bindings:
            if len(ordered_primary_assets) != len(assets):
                raise BatchValidationError(
                    [
                        {
                            "rowNumber": 0,
                            "rowId": "",
                            "message": "完整流程素材类型与当前工作流不一致",
                        }
                    ]
                )
        elif (
            len(ordered_primary_assets) != len(rows)
            or len(assets) != len(rows)
        ):
            raise BatchValidationError(
                [
                    {
                        "rowNumber": 0,
                        "rowId": "",
                        "message": (
                            f"完整流程需要按脚本顺序上传 {len(rows)} 个"
                            f"{'图片' if expected_kind == 'image' else '视频'}素材"
                        ),
                    }
                ]
            )
    first_row = rows[0]
    try:
        if workflow_type == DIGITAL_HUMAN_WORKFLOW:
            batch_person_mode = _person_mode(
                str(
                    batch_parameters.get("person_mode")
                    or first_row.get("person_mode")
                    or ""
                )
            )
            batch_resolution = str(
                batch_parameters.get("resolution")
                or first_row.get("resolution")
                or "1024"
            )
            batch_instance_type = _instance_type(
                "", workflow_config.instance_type
            )
            batch_seedvr2_enabled = batch_parameters.get(
                "seedvr2_enabled", False
            )
            if audio_mode == "minimax" and batch_person_mode != "1":
                raise ValueError("脚本生成语音只支持数字人单人模式")
        else:
            batch_person_mode = ""
            batch_resolution = ""
            batch_instance_type = _instance_type(
                str(
                    batch_parameters.get("instance_type")
                    or first_row.get("instance_type")
                    or ""
                ),
                workflow_config.instance_type,
            )
            batch_seedvr2_enabled = True
    except ValueError as exc:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": str(exc)}]
        ) from exc
    if audio_mode == "upload":
        required_asset_id_keys = (
            ["image_asset_id", "audio_asset_id"]
            if workflow_type == DIGITAL_HUMAN_WORKFLOW
            else ["source_video_asset_id", "audio_asset_id"]
        )
        if (
            workflow_type == DIGITAL_HUMAN_WORKFLOW
            and batch_person_mode == "0"
        ):
            required_asset_id_keys.extend(
                ["left_audio_asset_id", "right_audio_asset_id"]
            )
        uses_sequence_bindings = all(
            all(str(row.get(key) or "").strip() for key in required_asset_id_keys)
            for row in rows
        )
        if not uses_sequence_bindings:
            index = _asset_index(assets)

    for position, row in enumerate(rows, start=1):
        row_key = str(row.get("row_id") or "").strip()
        try:
            if not row_key:
                raise ValueError("任务编号不能为空")
            if len(row_key) > 100:
                raise ValueError("任务编号不能超过 100 个字符")
            folded_key = row_key.casefold()
            if folded_key in seen_row_keys:
                raise ValueError("任务编号重复")
            seen_row_keys.add(folded_key)

            if workflow_type == DIGITAL_HUMAN_WORKFLOW:
                staged = {
                    "image": (
                        ordered_primary_assets[position - 1]
                        if (
                            audio_mode == "minimax"
                            and not uses_primary_sequence_bindings
                        )
                        else _resolve_asset_reference(
                            assets_by_id,
                            index,
                            asset_id=str(row.get("image_asset_id") or ""),
                            filename=str(row.get("image_file") or ""),
                            kind="image",
                            label="参考图片",
                        )
                    ),
                }
                if audio_mode == "upload":
                    staged["audio"] = _resolve_asset_reference(
                        assets_by_id,
                        index,
                        asset_id=str(row.get("audio_asset_id") or ""),
                        filename=str(row.get("audio_file") or ""),
                        kind="audio",
                        label="总参考音频",
                    )
                if audio_mode == "upload" and batch_person_mode == "0":
                    staged["left_audio"] = _resolve_asset_reference(
                        assets_by_id,
                        index,
                        asset_id=str(row.get("left_audio_asset_id") or ""),
                        filename=str(row.get("left_audio_file") or ""),
                        kind="audio",
                        label="左人物音频",
                    )
                    staged["right_audio"] = _resolve_asset_reference(
                        assets_by_id,
                        index,
                        asset_id=str(row.get("right_audio_asset_id") or ""),
                        filename=str(row.get("right_audio_file") or ""),
                        kind="audio",
                        label="右人物音频",
                    )
                    for asset_name, label in (
                        ("left_audio", "左人物音频"),
                        ("right_audio", "右人物音频"),
                    ):
                        auxiliary_path = safe_relative_path(
                            staged[asset_name].relative_path,
                            settings.data_dir,
                        )
                        if (
                            inspect_audio_duration(auxiliary_path)
                            > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01
                        ):
                            raise ValueError(
                                f"{label}不能超过 "
                                f"{DIGITAL_HUMAN_MAX_SEGMENT_SECONDS:g} 秒，请先拆成多行任务"
                            )
                if audio_mode == "upload":
                    audio_path = safe_relative_path(
                        staged["audio"].relative_path, settings.data_dir
                    )
                    duration = inspect_audio_duration(audio_path)
                    if duration > MAX_LONG_AUDIO_SECONDS:
                        raise ValueError(
                            "单个音频最长支持 60 分钟"
                        )
                else:
                    duration = 1.0
                parameters = {
                    "prompt": str(
                        row.get("prompt")
                        or batch_parameters.get("default_prompt")
                        or workflow_config.default_prompt
                    ),
                    # Batch generation always uses the complete uploaded audio.
                    "start_time": "0:00",
                    "end_time": format_duration_timecode(duration),
                    "resolution": batch_resolution,
                    "person_mode": batch_person_mode,
                    "instance_type": batch_instance_type,
                    "seedvr2_enabled": batch_seedvr2_enabled,
                    "generation_tail_seconds": DIGITAL_HUMAN_GENERATION_TAIL_SECONDS,
                }
                metadata = {
                    "audio_duration_seconds": duration,
                    "requires_segmentation": (
                        audio_mode == "upload"
                        and duration > DIGITAL_HUMAN_MAX_SEGMENT_SECONDS + 0.01
                    ),
                }
            else:
                staged = {
                    "video": (
                        ordered_primary_assets[position - 1]
                        if (
                            audio_mode == "minimax"
                            and not uses_primary_sequence_bindings
                        )
                        else _resolve_asset_reference(
                            assets_by_id,
                            index,
                            asset_id=str(
                                row.get("source_video_asset_id") or ""
                            ),
                            filename=str(row.get("source_video_file") or ""),
                            kind="video",
                            label="源视频",
                        )
                    ),
                }
                if audio_mode == "upload":
                    staged["audio"] = _resolve_asset_reference(
                        assets_by_id,
                        index,
                        asset_id=str(row.get("audio_asset_id") or ""),
                        filename=str(row.get("audio_file") or ""),
                        kind="audio",
                        label="自定义音频",
                    )
                    audio_path = safe_relative_path(
                        staged["audio"].relative_path, settings.data_dir
                    )
                    duration = inspect_audio_duration(audio_path)
                    if duration > MAX_LONG_AUDIO_SECONDS:
                        raise ValueError(
                            "单个音频最长支持 60 分钟"
                        )
                    if duration > MAX_SEGMENT_SECONDS + 0.01:
                        video_path = safe_relative_path(
                            staged["video"].relative_path,
                            settings.data_dir,
                        )
                        video_duration = inspect_media_duration(video_path)
                        if video_duration + 0.05 < duration:
                            raise ValueError(
                                "源视频时长不能短于对应音频"
                            )
                prompt_prefix = str(
                    row.get("prompt_prefix")
                    or batch_parameters.get("prompt_prefix")
                    or "一名人物用中文说"
                ).strip().rstrip("：:")
                script = str(row.get("speech_script") or "").strip()
                legacy_positive_prompt = str(
                    row.get("positive_prompt") or ""
                ).strip()
                if not script and not legacy_positive_prompt:
                    raise ValueError("口播脚本不能为空")
                parameters = {
                    # Users enter one script. LTX receives the derived positive
                    # prompt; legacy manifests with a completed positive prompt
                    # remain accepted during the transition.
                    "prompt": (
                        f"{prompt_prefix}：“{script}”"
                        if script
                        else legacy_positive_prompt
                    ),
                    "prompt_prefix": prompt_prefix,
                    "instance_type": batch_instance_type,
                }
                metadata = {
                    "has_custom_audio": True,
                    **(
                        {
                            "audio_duration_seconds": duration,
                            "requires_segmentation": (
                                duration > MAX_SEGMENT_SECONDS + 0.01
                            ),
                        }
                        if audio_mode == "upload"
                        else {}
                    ),
                }

            if audio_mode == "minimax":
                script = str(row.get("speech_script") or "").strip()
                validate_synthesis_options(
                    text=script,
                    speed=resolved_speech_options.speed,
                    volume=resolved_speech_options.volume,
                    pitch=resolved_speech_options.pitch,
                    output_format=resolved_speech_options.output_format,
                )
                if workflow_type == LTX_LIP_SYNC_WORKFLOW:
                    if not parameters["prompt_prefix"]:
                        raise ValueError("请填写对口型人物和语言")
                    parameters["prompt"] = f"{parameters['prompt_prefix']}：“{script}”"
                # Adapter validation still sees the future generated audio,
                # while no video task is written until the real file exists.
                preview_audio = WorkflowAsset(
                    name="audio",
                    kind="audio",
                    relative_path="generated-audio-placeholder.mp3",
                    original_name="generated-audio.mp3",
                )
            else:
                preview_audio = None

            preview_assets = [
                WorkflowAsset(
                    name=name,
                    kind=asset.kind,
                    relative_path=asset.relative_path,
                    original_name=asset.original_name,
                )
                for name, asset in staged.items()
            ]
            if preview_audio is not None:
                preview_assets.append(preview_audio)
            validate_task_input(
                user,
                workflow_type,
                preview_assets,
                parameters,
                metadata,
            )
            plans.append(
                BatchRowPlan(
                    row_number=position,
                    row_key=row_key,
                    manifest={
                        **{key: str(value) for key, value in row.items()},
                        **(
                            {
                                "image_file": staged["image"].original_name,
                                "prompt": parameters["prompt"],
                            }
                            if workflow_type == DIGITAL_HUMAN_WORKFLOW
                            else {
                                "source_video_file": staged["video"].original_name,
                                "positive_prompt": parameters["prompt"],
                                "prompt_prefix": parameters["prompt_prefix"],
                            }
                        ),
                    },
                    staged_assets=staged,
                    parameters=parameters,
                    asset_metadata=metadata,
                )
            )
        except ValueError as exc:
            errors.append(
                {
                    "rowNumber": position,
                    "sourceRowNumber": row.get("source_row_number"),
                    "rowId": row_key,
                    "message": str(exc),
                }
            )

    if errors:
        raise BatchValidationError(errors)
    return BatchPlan(
        workflow_type=workflow_type,
        audio_mode=audio_mode,
        rows=plans,
        assets=assets,
        speech_options=resolved_speech_options,
        review_required=(
            bool(review_required)
            if audio_mode == "upload"
            else bool(
                resolved_speech_options
                and resolved_speech_options.review_required
            )
        ),
        video_review_required=bool(video_review_required),
    )


def create_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    name: str,
    request_key: str,
    plan: BatchPlan,
    correlation_id: str | None = None,
) -> tuple[GenerationBatch, list[Path]]:
    """Atomically create a batch and its independently queued task rows."""

    existing = db.scalar(
        select(GenerationBatch).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    if existing is not None:
        if existing.source_channel != plan.source_channel:
            raise BatchValidationError(
                [
                    {
                        "rowNumber": 0,
                        "rowId": "",
                        "message": "批次请求标识已被另一入口使用，请重新提交",
                    }
                ]
            )
        return existing, []

    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 100:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "任务名称需为 1–100 个字符"}]
        )
    if not request_key or len(request_key) > 64:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "批次请求标识不合法"}]
        )

    clean_correlation_id = str(correlation_id or "").strip()
    if clean_correlation_id and (
        len(clean_correlation_id) > 64
        or re.fullmatch(r"[A-Za-z0-9._:-]+", clean_correlation_id) is None
    ):
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "日志关联标识不合法"}]
        )
    if not clean_correlation_id:
        clean_correlation_id = uuid.uuid4().hex

    runninghub_execution_account_ids_json = None
    if (
        plan.source_channel == BATCH_SOURCE_LEGACY_WEB
        and plan.workflow_type == DIGITAL_HUMAN_WORKFLOW
    ):
        try:
            assigned_ids = assigned_execution_account_ids(db, user)
        except RunningHubPoolSelectionUnavailableError as exc:
            raise BatchValidationError(
                [{"rowNumber": 0, "rowId": "", "message": str(exc)}]
            ) from exc
        runninghub_execution_account_ids_json = json.dumps(
            assigned_ids, ensure_ascii=False, separators=(",", ":")
        )

    batch = GenerationBatch(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=clean_name,
        workflow_type=plan.workflow_type,
        runninghub_execution_account_ids_json=(
            runninghub_execution_account_ids_json
        ),
        source_channel=plan.source_channel,
        correlation_id=clean_correlation_id,
        audio_mode=plan.audio_mode,
        review_required=plan.review_required,
        video_review_required=plan.video_review_required,
        request_key=request_key,
        status="ACTIVE",
        total_items=len(plan.rows),
    )
    db.add(batch)
    created_directories: list[Path] = []
    base_time = datetime.now(timezone.utc)
    try:
        voice = None
        if plan.audio_mode == "minimax":
            assert plan.speech_options is not None
            voice = plan.speech_options.voice

        for index, row_plan in enumerate(plan.rows):
            requires_segmentation = bool(
                row_plan.asset_metadata.get("requires_segmentation")
            )
            item = GenerationBatchItem(
                id=str(uuid.uuid4()),
                batch=batch,
                row_number=row_plan.row_number,
                row_key=row_plan.row_key,
                manifest_json=json.dumps(row_plan.manifest, ensure_ascii=False),
                audio_status=(
                    (
                        "SEGMENTING"
                        if requires_segmentation
                        else "AUDIO_READY"
                    )
                    if plan.audio_mode == "upload"
                    else "PENDING"
                ),
                status=(
                    (
                        "SEGMENTING"
                        if requires_segmentation
                        else "TASK_CREATED"
                    )
                    if plan.audio_mode == "upload"
                    else "AUDIO_PENDING"
                ),
            )
            db.add(item)
            db.flush()

            task_id = str(uuid.uuid4())
            long_project_id = (
                str(uuid.uuid4()) if requires_segmentation else None
            )
            upload_dir = (
                long_audio_project_dir(
                    settings,
                    user.id,
                    long_project_id,
                )
                if long_project_id
                else task_upload_dir(settings, user.id, task_id)
            )
            created_directories.append(upload_dir)
            final_assets = []
            for asset_name, staged in row_plan.staged_assets.items():
                source = safe_relative_path(staged.relative_path, settings.data_dir)
                target = materialize_staged_asset(
                    source,
                    upload_dir,
                    kind=staged.kind,
                )
                final_assets.append(
                    WorkflowAsset(
                        name=asset_name,
                        kind=staged.kind,
                        relative_path=to_relative_data_path(target, settings),
                        original_name=staged.original_name,
                    )
                )

            if plan.audio_mode == "upload" and requires_segmentation:
                assert long_project_id is not None
                primary = next(
                    asset
                    for asset in final_assets
                    if asset.name in {"image", "video"}
                )
                audio = next(
                    asset for asset in final_assets if asset.name == "audio"
                )
                project = LongAudioProject(
                    id=long_project_id,
                    user_id=user.id,
                    batch_item_id=item.id,
                    name=f"{clean_name} · {row_plan.row_key}"[:100],
                    workflow_type=plan.workflow_type,
                    review_required=plan.review_required,
                    script_text=str(
                        row_plan.manifest.get("speech_script") or ""
                    ).strip(),
                    audio_path=audio.relative_path,
                    audio_original_name=audio.original_name,
                    video_path=primary.relative_path,
                    video_original_name=primary.original_name,
                    duration_seconds=float(
                        row_plan.asset_metadata["audio_duration_seconds"]
                    ),
                    parameters_json=json.dumps(
                        {
                            "prompt_prefix": str(
                                row_plan.parameters.get("prompt_prefix")
                                or "一名人物用中文说"
                            ),
                            "instance_type": str(
                                row_plan.parameters.get("instance_type")
                                or "plus"
                            ),
                            "seedvr2_enabled": row_plan.parameters.get(
                                "seedvr2_enabled", False
                            ),
                            "digital_prompt": (
                                str(row_plan.parameters.get("prompt") or "")
                                if plan.workflow_type
                                == DIGITAL_HUMAN_WORKFLOW
                                else ""
                            ),
                            "resolution": str(
                                row_plan.parameters.get("resolution")
                                or "1024"
                            ),
                            "person_mode": "1",
                        },
                        ensure_ascii=False,
                    ),
                    alignment_provider=(
                        settings.long_audio_alignment_provider
                        if plan.workflow_type == LTX_LIP_SYNC_WORKFLOW
                        else "vad_silence"
                    ),
                    status=LongAudioProjectStatus.PENDING_ANALYSIS.value,
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(days=settings.upload_retention_days)
                    ),
                )
                db.add(project)
                item.manifest_json = json.dumps(
                    {
                        **row_plan.manifest,
                        "long_audio_project_id": project.id,
                    },
                    ensure_ascii=False,
                )
            elif plan.audio_mode == "upload":
                validated = validate_task_input(
                    user,
                    plan.workflow_type,
                    final_assets,
                    row_plan.parameters,
                    row_plan.asset_metadata,
                )
                create_generation_task(
                    db,
                    user,
                    validated,
                    task_id=task_id,
                    batch_item_id=item.id,
                    # Microsecond offsets preserve manifest order inside global FIFO.
                    created_at=base_time + timedelta(microseconds=index),
                )
            else:
                assert plan.speech_options is not None
                assert voice is not None
                primary = next(
                    (
                        asset
                        for asset in final_assets
                        if asset.name in {"image", "video"}
                    ),
                    None,
                )
                if primary is None and not plan.defer_primary_until_video:
                    raise ValueError("声音任务缺少后续画面素材")
                audio_task = AudioGenerationTask(
                    id=str(uuid.uuid4()),
                    user_id=user.id,
                    config_id=plan.speech_options.config.id,
                    batch_item_id=item.id,
                    # Legacy non-null columns temporarily point to the same
                    # final voice; new processing uses voice_asset_id only.
                    voice_a_id=voice.id,
                    voice_b_id=voice.id,
                    voice_asset_id=voice.id,
                    planned_generation_task_id=task_id,
                    account_binding_id=(
                        plan.speech_options.config.account_binding_id
                    ),
                    credential_fingerprint=(
                        plan.speech_options.config.credential_fingerprint
                    ),
                    primary_kind=primary.kind if primary else None,
                    primary_path=primary.relative_path if primary else None,
                    primary_original_name=(primary.original_name if primary else None),
                    speech_script=row_plan.manifest["speech_script"],
                    pronunciation_dict_json=json.dumps(
                        plan.speech_options.pronunciation_tones,
                        ensure_ascii=False,
                    ),
                    video_parameters_json=json.dumps(
                        row_plan.parameters, ensure_ascii=False
                    ),
                    model=plan.speech_options.model,
                    weight_a=100,
                    weight_b=1,
                    speed=plan.speech_options.speed,
                    volume=plan.speech_options.volume,
                    pitch=plan.speech_options.pitch,
                    language_boost=plan.speech_options.language_boost,
                    output_format=plan.speech_options.output_format,
                    status="PENDING",
                    cost_confirmed_at=base_time,
                    created_at=base_time + timedelta(microseconds=index),
                )
                db.add(audio_task)

        consumed_at = datetime.now(timezone.utc)
        used_ids = {
            asset.id
            for row_plan in plan.rows
            for asset in row_plan.staged_assets.values()
        }
        for asset in plan.assets:
            if asset.id in used_ids:
                asset.consumed_at = consumed_at
        return batch, created_directories
    except Exception:
        for directory in created_directories:
            remove_directory(directory)
        raise
