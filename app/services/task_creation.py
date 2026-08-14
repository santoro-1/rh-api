from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import GenerationTask, TaskStatus, User
from app.services.workflow_configs import get_user_workflow_config
from app.workflows import get_workflow
from app.workflows.base import WorkflowAsset


class TaskCreationError(ValueError):
    """A user-correctable problem found before a local task is queued."""


@dataclass(frozen=True)
class ValidatedTaskInput:
    workflow_key: str
    assets: list[WorkflowAsset]
    parameters: dict[str, Any]
    asset_metadata: dict[str, Any]
    input_payload: dict[str, Any]


def ensure_user_can_create_workflow(user: User, workflow_key: str) -> None:
    """Apply the same account and workflow checks to single and batch creation."""

    account_config = user.runninghub_config
    if not account_config or not account_config.api_key_encrypted:
        raise TaskCreationError("当前账号尚未配置 RunningHub API Key")

    workflow_config = get_user_workflow_config(user, workflow_key)
    if not workflow_config.is_enabled:
        display_name = get_workflow(workflow_key).display_name
        raise TaskCreationError(f"当前账号尚未启用{display_name}工作流")
    if not workflow_config.ai_app_id:
        raise TaskCreationError("当前工作流尚未配置 RunningHub App ID")


def validate_task_input(
    user: User,
    workflow_key: str,
    assets: list[WorkflowAsset],
    parameters: dict[str, Any],
    asset_metadata: dict[str, Any],
) -> ValidatedTaskInput:
    """Normalize adapter input without writing a database row."""

    ensure_user_can_create_workflow(user, workflow_key)
    workflow = get_workflow(workflow_key)
    try:
        validated_parameters = workflow.validate_parameters(
            parameters, asset_metadata
        )
        input_payload = workflow.serialize_input(
            assets, validated_parameters, asset_metadata
        )
    except ValueError as exc:
        raise TaskCreationError(str(exc)) from exc
    return ValidatedTaskInput(
        workflow_key=workflow_key,
        assets=assets,
        parameters=validated_parameters,
        asset_metadata=asset_metadata,
        input_payload=input_payload,
    )


def create_generation_task(
    db: Session,
    user: User,
    validated: ValidatedTaskInput,
    *,
    task_id: str | None = None,
    batch_item_id: str | None = None,
    segment_id: str | None = None,
    created_at: datetime | None = None,
) -> GenerationTask:
    """Persist one PENDING task; the caller owns the surrounding transaction."""

    assets_by_name = {asset.name: asset for asset in validated.assets}
    primary = assets_by_name.get("image") or assets_by_name.get("video")
    audio = assets_by_name.get("audio")
    if primary is None or audio is None:
        raise TaskCreationError("任务缺少主要画面素材或音频素材")

    parameters = validated.parameters
    metadata = validated.asset_metadata
    task = GenerationTask(
        id=task_id or str(uuid.uuid4()),
        user_id=user.id,
        batch_item_id=batch_item_id,
        segment_id=segment_id,
        workflow_type=validated.workflow_key,
        seedvr2_enabled=bool(parameters.get("seedvr2_enabled", False)),
        input_payload=json.dumps(validated.input_payload, ensure_ascii=False),
        # These columns predate workflow adapters and remain populated so old
        # task pages and existing SQLite data stay compatible.
        image_path=primary.relative_path,
        audio_path=audio.relative_path,
        image_original_name=primary.original_name,
        audio_original_name=audio.original_name,
        audio_duration_seconds=float(
            metadata.get("audio_duration_seconds") or 0
        ),
        start_seconds=float(parameters.get("start_seconds") or 0),
        end_seconds=float(parameters.get("end_seconds") or 0),
        prompt=str(parameters.get("prompt") or ""),
        status=TaskStatus.PENDING.value,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(task)
    return task
