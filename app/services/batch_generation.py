from __future__ import annotations

import json
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
    GenerationBatch,
    GenerationBatchItem,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    StagedAsset,
    User,
    VoiceAssetStatus,
)
from app.services.audio import format_timecode, inspect_audio_duration
from app.services.batch_assets import load_available_assets
from app.services.batch_manifests import (
    DIGITAL_HUMAN_WORKFLOW,
    LTX_LIP_SYNC_WORKFLOW,
    canonical_filename,
)
from app.services.media_segmentation import MAX_SEGMENT_SECONDS
from app.services.storage import (
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


def _person_mode(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or normalized in {"1", "单人", "single"}:
        return "1"
    if normalized in {"0", "双人", "dual"}:
        return "0"
    raise ValueError("单双人模式只能填写单人或双人")


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
        index = _asset_index(assets) if audio_mode == "upload" else {}
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
    if audio_mode == "minimax":
        expected_kind = "image" if workflow_type == DIGITAL_HUMAN_WORKFLOW else "video"
        ordered_primary_assets = [
            asset for asset in assets if asset.kind == expected_kind
        ]
        if len(ordered_primary_assets) != len(rows) or len(assets) != len(rows):
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
            batch_instance_type = "default"
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
    except ValueError as exc:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": str(exc)}]
        ) from exc

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
                        if audio_mode == "minimax"
                        else _resolve_asset(
                            index,
                            filename=str(row.get("image_file") or ""),
                            kind="image",
                            label="参考图片",
                        )
                    ),
                }
                if audio_mode == "upload":
                    staged["audio"] = _resolve_asset(
                        index,
                        filename=str(row.get("audio_file") or ""),
                        kind="audio",
                        label="总参考音频",
                    )
                if audio_mode == "upload" and batch_person_mode == "0":
                    staged["left_audio"] = _resolve_asset(
                        index,
                        filename=str(row.get("left_audio_file") or ""),
                        kind="audio",
                        label="左人物音频",
                    )
                    staged["right_audio"] = _resolve_asset(
                        index,
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
                            > MAX_SEGMENT_SECONDS + 0.01
                        ):
                            raise ValueError(
                                f"{label}不能超过 45 秒，请先拆成多行任务"
                            )
                if audio_mode == "upload":
                    audio_path = safe_relative_path(
                        staged["audio"].relative_path, settings.data_dir
                    )
                    duration = inspect_audio_duration(audio_path)
                    if duration > MAX_SEGMENT_SECONDS + 0.01:
                        raise ValueError(
                            "上传音频不能超过 45 秒，请先拆成多行任务"
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
                    "end_time": format_timecode(duration),
                    "resolution": batch_resolution,
                    "person_mode": batch_person_mode,
                    "instance_type": "default",
                }
                metadata = {"audio_duration_seconds": duration}
            else:
                staged = {
                    "video": (
                        ordered_primary_assets[position - 1]
                        if audio_mode == "minimax"
                        else _resolve_asset(
                            index,
                            filename=str(row.get("source_video_file") or ""),
                            kind="video",
                            label="源视频",
                        )
                    ),
                }
                if audio_mode == "upload":
                    staged["audio"] = _resolve_asset(
                        index,
                        filename=str(row.get("audio_file") or ""),
                        kind="audio",
                        label="自定义音频",
                    )
                    audio_path = safe_relative_path(
                        staged["audio"].relative_path, settings.data_dir
                    )
                    duration = inspect_audio_duration(audio_path)
                    if duration > MAX_SEGMENT_SECONDS + 0.01:
                        raise ValueError(
                            "上传音频不能超过 45 秒，请先拆成多行任务"
                        )
                prompt_prefix = str(
                    row.get("prompt_prefix")
                    or batch_parameters.get("prompt_prefix")
                    or "一名人物用中文说"
                ).strip().rstrip("：:")
                parameters = {
                    # Future TTS metadata may contain speech_script, but V2V
                    # always receives the independently editable positive prompt.
                    "prompt": str(row.get("positive_prompt") or ""),
                    "prompt_prefix": prompt_prefix,
                    "instance_type": batch_instance_type,
                }
                metadata = {
                    "has_custom_audio": True,
                    **(
                        {"audio_duration_seconds": duration}
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
                    parameters["prompt"] = (
                        f"{parameters['prompt_prefix']}：“{script}”"
                    )
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
    )


def create_batch(
    db: Session,
    user: User,
    settings: Settings,
    *,
    name: str,
    request_key: str,
    plan: BatchPlan,
) -> tuple[GenerationBatch, list[Path]]:
    """Atomically create a batch and its independently queued task rows."""

    existing = db.scalar(
        select(GenerationBatch).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    if existing is not None:
        return existing, []

    clean_name = name.strip()
    if not 1 <= len(clean_name) <= 100:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "批次名称需为 1–100 个字符"}]
        )
    if not request_key or len(request_key) > 64:
        raise BatchValidationError(
            [{"rowNumber": 0, "rowId": "", "message": "批次请求标识不合法"}]
        )

    batch = GenerationBatch(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=clean_name,
        workflow_type=plan.workflow_type,
        audio_mode=plan.audio_mode,
        review_required=bool(
            plan.speech_options and plan.speech_options.review_required
        ),
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
            item = GenerationBatchItem(
                id=str(uuid.uuid4()),
                batch=batch,
                row_number=row_plan.row_number,
                row_key=row_plan.row_key,
                manifest_json=json.dumps(row_plan.manifest, ensure_ascii=False),
                audio_status=(
                    "AUDIO_READY" if plan.audio_mode == "upload" else "PENDING"
                ),
                status=(
                    "TASK_CREATED"
                    if plan.audio_mode == "upload"
                    else "AUDIO_PENDING"
                ),
            )
            db.add(item)
            db.flush()

            task_id = str(uuid.uuid4())
            upload_dir = task_upload_dir(settings, user.id, task_id)
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

            if plan.audio_mode == "upload":
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
                    asset
                    for asset in final_assets
                    if asset.name in {"image", "video"}
                )
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
                    primary_kind=primary.kind,
                    primary_path=primary.relative_path,
                    primary_original_name=primary.original_name,
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
