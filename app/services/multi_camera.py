from __future__ import annotations

import hashlib
import itertools
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    BATCH_SOURCE_MULTI_CAMERA_WEB,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    MultiCameraBatchConfig,
    MultiCameraImageGroup,
    MultiCameraImageGroupAsset,
    MultiCameraItemBinding,
    MultiCameraSegmentBinding,
    StagedAsset,
    User,
)
from app.services.audio import (
    AudioInspectionError,
    format_duration_timecode,
    inspect_audio_duration,
)
from app.services.batch_assets import StagedAssetError, load_available_assets
from app.services.media_segmentation import (
    MediaSegmentationError,
    SegmentPlan,
    cut_audio_segment,
    detect_silence_midpoints,
)
from app.services.storage import (
    materialize_staged_asset,
    remove_directory,
    safe_relative_path,
    task_upload_dir,
    to_relative_data_path,
)
from app.services.task_creation import (
    TaskCreationError,
    create_generation_task,
    ensure_user_can_create_workflow,
    validate_task_input,
)
from app.services.workflow_configs import get_user_workflow_config
from app.workflows.base import WorkflowAsset


WORKFLOW = "digital_human"
MAX_SEGMENT_SECONDS = 20.0
MIN_AUDIO_SECONDS = 1.0
SEGMENTATION_POLICY = "multi-camera-natural-pause-balanced-20s-v1"
ORDERING_POLICY = "multi-camera-permutation-v1"
THREE_CAMERA_PERMUTATIONS = (
    (1, 2, 3),
    (1, 3, 2),
    (2, 1, 3),
    (2, 3, 1),
    (3, 2, 1),
    (3, 1, 2),
)


class MultiCameraError(ValueError):
    """A controlled multi-camera request cannot be planned or persisted."""


@dataclass(frozen=True)
class PlannedGroup:
    client_key: str
    name: str
    assets: tuple[StagedAsset, ...]


@dataclass(frozen=True)
class PlannedRow:
    row_key: str
    audio: StagedAsset
    group: PlannedGroup
    duration_seconds: float
    segments: tuple[SegmentPlan, ...]
    cameras: tuple[int, ...]


@dataclass(frozen=True)
class PlannedBatch:
    normalized: dict[str, Any]
    request_sha256: str
    groups: tuple[PlannedGroup, ...]
    rows: tuple[PlannedRow, ...]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_text(value: Any, label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise MultiCameraError(f"{label}不能为空")
    if len(text) > maximum:
        raise MultiCameraError(f"{label}不能超过 {maximum} 个字符")
    return text


def _clean_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raise MultiCameraError(f"{label}必须是布尔值")


def normalize_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MultiCameraError("请求格式不合法")
    groups_raw = payload.get("groups")
    rows_raw = payload.get("rows")
    if not isinstance(groups_raw, list) or not 1 <= len(groups_raw) <= 30:
        raise MultiCameraError("图片组数量必须在 1 到 30 之间")
    if not isinstance(rows_raw, list) or not 1 <= len(rows_raw) <= 200:
        raise MultiCameraError("音频数量必须在 1 到 200 之间")

    groups: list[dict[str, Any]] = []
    group_keys: set[str] = set()
    all_image_ids: set[str] = set()
    for index, raw in enumerate(groups_raw, start=1):
        if not isinstance(raw, dict):
            raise MultiCameraError(f"第 {index} 个图片组格式不合法")
        client_key = _clean_text(
            raw.get("clientKey") or raw.get("client_id"), "图片组标识", 100
        )
        if client_key in group_keys:
            raise MultiCameraError("图片组标识不能重复")
        group_keys.add(client_key)
        image_ids = raw.get("imageAssetIds") or raw.get("image_asset_ids")
        if not isinstance(image_ids, list) or not 1 <= len(image_ids) <= 12:
            raise MultiCameraError("每个图片组必须包含 1 到 12 张图片")
        clean_ids = [_clean_text(value, "图片素材标识", 100) for value in image_ids]
        if len(set(clean_ids)) != len(clean_ids):
            raise MultiCameraError("同一个图片组不能重复使用同一张图片")
        if all_image_ids.intersection(clean_ids):
            raise MultiCameraError("一张暂存图片不能同时属于多个图片组")
        all_image_ids.update(clean_ids)
        groups.append(
            {
                "clientKey": client_key,
                "name": _clean_text(
                    raw.get("name") or f"机位组 {index}", "图片组名称", 100
                ),
                "imageAssetIds": clean_ids,
            }
        )

    rows: list[dict[str, Any]] = []
    row_keys: set[str] = set()
    audio_ids: set[str] = set()
    for index, raw in enumerate(rows_raw, start=1):
        if not isinstance(raw, dict):
            raise MultiCameraError(f"第 {index} 条音频格式不合法")
        row_key = _clean_text(
            raw.get("rowKey") or raw.get("row_key"), "音频行标识", 100
        )
        group_key = _clean_text(
            raw.get("groupClientKey") or raw.get("group_client_id"),
            "绑定图片组",
            100,
        )
        audio_id = _clean_text(
            raw.get("audioAssetId") or raw.get("audio_asset_id"), "音频素材标识", 100
        )
        if row_key in row_keys:
            raise MultiCameraError("音频行标识不能重复")
        if audio_id in audio_ids:
            raise MultiCameraError("同一条暂存音频不能重复提交")
        if group_key not in group_keys:
            raise MultiCameraError(f"音频 {row_key} 绑定的图片组不存在")
        row_keys.add(row_key)
        audio_ids.add(audio_id)
        rows.append(
            {
                "rowKey": row_key,
                "audioAssetId": audio_id,
                "groupClientKey": group_key,
            }
        )

    resolution = str(payload.get("resolution") or "1024").strip()
    try:
        if int(resolution) <= 0:
            raise ValueError
    except ValueError as exc:
        raise MultiCameraError("最长分辨率必须是正整数") from exc
    return {
        "name": _clean_text(payload.get("name") or "多机位批次", "批次名称", 100),
        "requestKey": _clean_text(
            payload.get("requestKey") or payload.get("request_key"), "请求标识", 64
        ),
        "groups": groups,
        "rows": rows,
        "prompt": _clean_text(payload.get("prompt"), "提示词", 5000),
        "resolution": resolution,
        "seedvr2Enabled": _clean_bool(payload.get("seedvr2Enabled"), "SeedVR2 开关"),
    }


def plan_segments(
    duration_seconds: float,
    silence_midpoints: list[float] | None = None,
) -> list[SegmentPlan]:
    """Build a global balanced plan with a hard 20-second ceiling."""

    if duration_seconds < MIN_AUDIO_SECONDS:
        raise MultiCameraError("音频时长不足 1 秒")
    count = max(1, math.ceil((duration_seconds - 0.001) / MAX_SEGMENT_SECONDS))
    if count == 1:
        return [
            SegmentPlan(
                index=1,
                script_text="",
                start_seconds=0.0,
                end_seconds=round(duration_seconds, 3),
                alignment_method="single_segment_under_20s",
            )
        ]

    candidates = sorted(
        {
            round(float(value), 3)
            for value in (silence_midpoints or [])
            if 0.0 < float(value) < duration_seconds
        }
    )
    minimum = min(4.0, duration_seconds / count * 0.45)
    boundaries = [0.0]
    natural_boundaries: set[float] = set()
    for boundary_index in range(1, count):
        previous = boundaries[-1]
        remaining = count - boundary_index
        ideal = duration_seconds * boundary_index / count
        lower = max(
            previous + minimum, duration_seconds - remaining * MAX_SEGMENT_SECONDS
        )
        upper = min(
            previous + MAX_SEGMENT_SECONDS, duration_seconds - remaining * minimum
        )
        viable = [candidate for candidate in candidates if lower <= candidate <= upper]
        if viable:
            selected = min(viable, key=lambda value: (abs(value - ideal), value))
            natural_boundaries.add(selected)
        else:
            selected = min(max(ideal, lower), upper)
        boundaries.append(round(selected, 3))
    boundaries.append(round(duration_seconds, 3))

    plans: list[SegmentPlan] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        if end - start > MAX_SEGMENT_SECONDS + 0.01:
            raise MultiCameraError("无法在 20 秒上限内安全切分音频")
        plans.append(
            SegmentPlan(
                index=index,
                script_text="",
                start_seconds=start,
                end_seconds=end,
                alignment_method=(
                    "natural_silence_20s"
                    if end in natural_boundaries
                    else "balanced_20s"
                ),
            )
        )
    return plans


def camera_sequence(camera_count: int, segment_count: int) -> list[int]:
    if camera_count < 1 or segment_count < 0:
        raise MultiCameraError("机位数量或分段数量不合法")
    if camera_count == 1:
        return [1] * segment_count
    if camera_count == 2:
        return [index % 2 + 1 for index in range(segment_count)]
    if camera_count == 3:
        pattern = tuple(itertools.chain.from_iterable(THREE_CAMERA_PERMUTATIONS))
        return [pattern[index % len(pattern)] for index in range(segment_count)]

    result: list[int] = []
    while len(result) < segment_count:
        for permutation in itertools.permutations(range(1, camera_count + 1)):
            for camera in permutation:
                result.append(camera)
                if len(result) == segment_count:
                    return result
    return result


def _asset_path(asset: StagedAsset, settings: Settings) -> Path:
    path = safe_relative_path(asset.relative_path, settings.data_dir)
    if not path.is_file():
        raise MultiCameraError(f"素材 {asset.original_name} 已不存在")
    return path


def build_plan(
    db: Session,
    user: User,
    payload: Any,
    settings: Settings,
) -> PlannedBatch:
    normalized = normalize_request(payload)
    ensure_user_can_create_workflow(
        user,
        WORKFLOW,
        require_assigned_execution_account=False,
    )
    asset_ids = [
        asset_id
        for group in normalized["groups"]
        for asset_id in group["imageAssetIds"]
    ] + [row["audioAssetId"] for row in normalized["rows"]]
    try:
        assets = load_available_assets(db, user, asset_ids)
    except StagedAssetError as exc:
        raise MultiCameraError(str(exc)) from exc
    by_id = {asset.id: asset for asset in assets}

    groups: list[PlannedGroup] = []
    groups_by_key: dict[str, PlannedGroup] = {}
    for raw in normalized["groups"]:
        group_assets = tuple(by_id[asset_id] for asset_id in raw["imageAssetIds"])
        if any(asset.kind != "image" for asset in group_assets):
            raise MultiCameraError(f"图片组 {raw['name']} 包含非图片素材")
        group = PlannedGroup(raw["clientKey"], raw["name"], group_assets)
        groups.append(group)
        groups_by_key[group.client_key] = group

    rows: list[PlannedRow] = []
    for raw in normalized["rows"]:
        audio = by_id[raw["audioAssetId"]]
        if audio.kind != "audio":
            raise MultiCameraError(f"{raw['rowKey']} 绑定的不是音频素材")
        path = _asset_path(audio, settings)
        try:
            duration = inspect_audio_duration(path)
            segments = tuple(plan_segments(duration, detect_silence_midpoints(path)))
        except (AudioInspectionError, MediaSegmentationError) as exc:
            raise MultiCameraError(f"{audio.original_name}：{exc}") from exc
        group = groups_by_key[raw["groupClientKey"]]
        rows.append(
            PlannedRow(
                row_key=raw["rowKey"],
                audio=audio,
                group=group,
                duration_seconds=duration,
                segments=segments,
                cameras=tuple(camera_sequence(len(group.assets), len(segments))),
            )
        )
    return PlannedBatch(
        normalized, _canonical_digest(normalized), tuple(groups), tuple(rows)
    )


def plan_payload(plan: PlannedBatch) -> dict[str, Any]:
    return {
        "schema": "runninghub.multi-camera-preflight.v1",
        "requestSha256": plan.request_sha256,
        "totalAudios": len(plan.rows),
        "totalSegments": sum(len(row.segments) for row in plan.rows),
        "rows": [
            {
                "rowKey": row.row_key,
                "audioName": row.audio.original_name,
                "groupName": row.group.name,
                "durationSeconds": round(row.duration_seconds, 3),
                "segments": [
                    {
                        "index": segment.index,
                        "startSeconds": segment.start_seconds,
                        "endSeconds": segment.end_seconds,
                        "durationSeconds": round(segment.duration_seconds, 3),
                        "camera": camera,
                        "imageName": row.group.assets[camera - 1].original_name,
                        "cutMethod": segment.alignment_method,
                    }
                    for segment, camera in zip(row.segments, row.cameras)
                ],
            }
            for row in plan.rows
        ],
    }


def _existing_batch(
    db: Session, user: User, request_key: str
) -> GenerationBatch | None:
    return db.scalar(
        select(GenerationBatch).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )


def create_multi_camera_batch(
    db: Session,
    user: User,
    payload: Any,
    settings: Settings,
) -> tuple[GenerationBatch, bool]:
    normalized = normalize_request(payload)
    digest = _canonical_digest(normalized)
    existing = _existing_batch(db, user, normalized["requestKey"])
    if existing is not None:
        if (
            existing.source_channel != BATCH_SOURCE_MULTI_CAMERA_WEB
            or existing.multi_camera_config is None
            or existing.multi_camera_config.request_sha256 != digest
        ):
            raise MultiCameraError("请求标识已被其他内容使用，请刷新后重新提交")
        return existing, False

    plan = build_plan(db, user, normalized, settings)
    workflow_config = get_user_workflow_config(user, WORKFLOW)
    batch = GenerationBatch(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=normalized["name"],
        workflow_type=WORKFLOW,
        source_channel=BATCH_SOURCE_MULTI_CAMERA_WEB,
        audio_mode="upload",
        review_required=False,
        video_review_required=False,
        request_key=normalized["requestKey"],
        status="ACTIVE",
        total_items=len(plan.rows),
    )
    batch.multi_camera_config = MultiCameraBatchConfig(
        request_sha256=plan.request_sha256,
        prompt=normalized["prompt"],
        resolution=normalized["resolution"],
        instance_type=workflow_config.instance_type,
        seedvr2_enabled=normalized["seedvr2Enabled"],
        segmentation_policy=SEGMENTATION_POLICY,
        ordering_policy=ORDERING_POLICY,
    )

    group_entities: dict[str, MultiCameraImageGroup] = {}
    group_asset_entities: dict[tuple[str, int], MultiCameraImageGroupAsset] = {}
    for position, group in enumerate(plan.groups, start=1):
        entity = MultiCameraImageGroup(
            id=str(uuid.uuid4()),
            position=position,
            client_key=group.client_key,
            name=group.name,
        )
        for image_position, staged in enumerate(group.assets, start=1):
            asset_entity = MultiCameraImageGroupAsset(
                id=str(uuid.uuid4()),
                position=image_position,
                original_name=staged.original_name,
                image_sha256=_file_sha256(_asset_path(staged, settings)),
            )
            entity.assets.append(asset_entity)
            group_asset_entities[(group.client_key, image_position)] = asset_entity
        batch.multi_camera_groups.append(entity)
        group_entities[group.client_key] = entity

    created_directories: list[Path] = []
    prepared: list[
        tuple[GenerationSegment, MultiCameraSegmentBinding, Any, str, datetime]
    ] = []
    base_time = datetime.now(timezone.utc)
    try:
        for row_number, row in enumerate(plan.rows, start=1):
            item = GenerationBatchItem(
                id=str(uuid.uuid4()),
                row_number=row_number,
                row_key=row.row_key,
                manifest_json=json.dumps(
                    {
                        "schema": "runninghub.multi-camera-row.v1",
                        "audioName": row.audio.original_name,
                        "groupClientKey": row.group.client_key,
                        "cameraSequence": list(row.cameras),
                    },
                    ensure_ascii=False,
                ),
                audio_status="AUDIO_READY",
                status="SEGMENTS_CREATED",
                merged_video_status="NOT_APPLICABLE",
            )
            item.multi_camera_binding = MultiCameraItemBinding(
                image_group=group_entities[row.group.client_key],
                audio_original_name=row.audio.original_name,
                audio_sha256=_file_sha256(_asset_path(row.audio, settings)),
                duration_seconds=row.duration_seconds,
            )
            batch.items.append(item)
            source_audio = _asset_path(row.audio, settings)
            for segment, camera in zip(row.segments, row.cameras):
                task_id = str(uuid.uuid4())
                upload_dir = task_upload_dir(settings, user.id, task_id)
                created_directories.append(upload_dir)
                staged_image = row.group.assets[camera - 1]
                image_path = materialize_staged_asset(
                    _asset_path(staged_image, settings), upload_dir, kind="image"
                )
                if len(row.segments) == 1:
                    audio_path = materialize_staged_asset(
                        source_audio, upload_dir, kind="audio"
                    )
                else:
                    audio_path = upload_dir / f"audio-{uuid.uuid4().hex}.mp3"
                    cut_audio_segment(
                        source_audio,
                        audio_path,
                        start_seconds=segment.start_seconds,
                        end_seconds=segment.end_seconds,
                    )
                segment_duration = segment.duration_seconds
                image_relative = to_relative_data_path(image_path, settings)
                audio_relative = to_relative_data_path(audio_path, settings)
                segment_entity = GenerationSegment(
                    id=str(uuid.uuid4()),
                    segment_index=segment.index,
                    script_text="",
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    audio_path=audio_relative,
                    prompt=normalized["prompt"],
                    alignment_method=segment.alignment_method,
                    status="TASK_CREATED",
                )
                binding = MultiCameraSegmentBinding(
                    image_asset=group_asset_entities[(row.group.client_key, camera)],
                    camera_position=camera,
                    image_sha256=group_asset_entities[
                        (row.group.client_key, camera)
                    ].image_sha256,
                )
                segment_entity.multi_camera_binding = binding
                assets = [
                    WorkflowAsset(
                        name="image",
                        kind="image",
                        relative_path=image_relative,
                        original_name=staged_image.original_name,
                    ),
                    WorkflowAsset(
                        name="audio",
                        kind="audio",
                        relative_path=audio_relative,
                        original_name=(
                            row.audio.original_name
                            if len(row.segments) == 1
                            else f"{Path(row.audio.original_name).stem}-{segment.index:03d}.mp3"
                        ),
                    ),
                ]
                validated = validate_task_input(
                    user,
                    WORKFLOW,
                    assets,
                    {
                        "prompt": normalized["prompt"],
                        "start_time": "0:00",
                        "end_time": format_duration_timecode(segment_duration),
                        "person_mode": "1",
                        "resolution": normalized["resolution"],
                        "instance_type": workflow_config.instance_type,
                        "seedvr2_enabled": normalized["seedvr2Enabled"],
                    },
                    {"audio_duration_seconds": segment_duration},
                    require_assigned_execution_account=False,
                )
                item.segments.append(segment_entity)
                prepared.append(
                    (
                        segment_entity,
                        binding,
                        validated,
                        task_id,
                        base_time
                        + timedelta(microseconds=(row_number * 1000 + segment.index)),
                    )
                )

        db.add(batch)
        db.flush()
        for segment, binding, validated, task_id, created_at in prepared:
            create_generation_task(
                db,
                user,
                validated,
                task_id=task_id,
                segment_id=segment.id,
                created_at=created_at,
            )
        now = datetime.now(timezone.utc)
        used_assets = {asset.id for group in plan.groups for asset in group.assets} | {
            row.audio.id for row in plan.rows
        }
        for asset in db.scalars(
            select(StagedAsset).where(StagedAsset.id.in_(used_assets))
        ).all():
            asset.consumed_at = now
        db.commit()
        db.refresh(batch)
        return batch, True
    except IntegrityError as exc:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        concurrent = _existing_batch(db, user, normalized["requestKey"])
        if concurrent is None:
            raise
        if (
            concurrent.source_channel == BATCH_SOURCE_MULTI_CAMERA_WEB
            and concurrent.multi_camera_config is not None
            and concurrent.multi_camera_config.request_sha256 == plan.request_sha256
        ):
            return concurrent, False
        raise MultiCameraError("请求标识已被其他内容使用，请刷新后重新提交") from exc
    except Exception:
        db.rollback()
        for directory in created_directories:
            remove_directory(directory)
        raise
