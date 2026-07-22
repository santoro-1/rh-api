from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.models import RunningHubConfig, User, WorkflowConfig
from app.workflows.registry import get_workflow


@dataclass(frozen=True)
class ResolvedWorkflowConfig:
    """Effective per-user configuration consumed by a workflow adapter."""

    workflow_key: str
    ai_app_id: str
    instance_type: str
    default_prompt: str
    is_enabled: bool
    settings: dict[str, Any]


def _decode_settings(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def get_user_workflow_config(user: User, workflow_key: str) -> ResolvedWorkflowConfig:
    """Resolve a workflow config, including pre-migration digital-human rows.

    The legacy fallback is intentional: tasks and accounts already present in
    the local SQLite file still run before the administrator opens and saves
    their configuration page after the migration.
    """

    workflow = get_workflow(workflow_key)
    config = next(
        (item for item in user.workflow_configs if item.workflow_key == workflow_key),
        None,
    )
    if config:
        return ResolvedWorkflowConfig(
            workflow_key=workflow_key,
            ai_app_id=config.ai_app_id,
            instance_type=config.instance_type,
            default_prompt=config.default_prompt,
            is_enabled=config.is_enabled,
            settings=_decode_settings(config.settings_json),
        )

    legacy = user.runninghub_config
    if workflow_key == "digital_human" and legacy:
        return ResolvedWorkflowConfig(
            workflow_key=workflow_key,
            ai_app_id=legacy.ai_app_id,
            instance_type=legacy.instance_type,
            default_prompt=legacy.default_prompt,
            is_enabled=True,
            settings={},
        )
    return ResolvedWorkflowConfig(
        workflow_key=workflow_key,
        ai_app_id=workflow.default_ai_app_id,
        instance_type="plus",
        default_prompt=workflow.default_prompt,
        is_enabled=False,
        settings={},
    )


def save_workflow_config(
    user: User,
    workflow_key: str,
    *,
    ai_app_id: str,
    instance_type: str,
    default_prompt: str,
    is_enabled: bool = True,
    settings: dict[str, Any] | None = None,
) -> WorkflowConfig:
    """Create or update the one workflow-specific row for a user."""

    get_workflow(workflow_key)  # Reject invalid keys before storing configuration.
    if instance_type not in {"default", "plus"}:
        raise ValueError("实例类型只能为 default 或 plus")
    config = next(
        (item for item in user.workflow_configs if item.workflow_key == workflow_key),
        None,
    )
    if config is None:
        config = WorkflowConfig(
            user=user,
            workflow_key=workflow_key,
            ai_app_id=ai_app_id.strip(),
            instance_type=instance_type,
            default_prompt=default_prompt.strip(),
            is_enabled=is_enabled,
            settings_json=json.dumps(settings or {}, ensure_ascii=False),
        )
    else:
        config.ai_app_id = ai_app_id.strip()
        config.instance_type = instance_type
        config.default_prompt = default_prompt.strip()
        config.is_enabled = is_enabled
        config.settings_json = json.dumps(settings or {}, ensure_ascii=False)
    return config
