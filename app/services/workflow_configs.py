from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import RunningHubConfig, SystemWorkflowConfig, User, WorkflowConfig
from app.workflows.registry import get_workflow


_LEGACY_LTX_DEFAULT_PROMPT = (
    "人物自然地说话，口型与语音一致，保持原视频动作、构图和镜头稳定。"
)


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
    session = object_session(user) if isinstance(user, User) else None
    if session is not None:
        shared = session.scalar(
            select(SystemWorkflowConfig).where(
                SystemWorkflowConfig.workflow_key == workflow_key
            )
        )
        if shared is not None:
            return ResolvedWorkflowConfig(
                workflow_key=workflow_key,
                ai_app_id=shared.ai_app_id,
                instance_type=shared.instance_type,
                default_prompt=shared.default_prompt,
                is_enabled=shared.is_enabled,
                settings=_decode_settings(shared.settings_json),
            )
    config = next(
        (item for item in user.workflow_configs if item.workflow_key == workflow_key),
        None,
    )
    if config:
        default_prompt = config.default_prompt
        # Replace only the obsolete built-in text; custom administrator prompts
        # remain untouched.
        if (
            workflow_key == "ltx_lip_sync"
            and default_prompt == _LEGACY_LTX_DEFAULT_PROMPT
        ):
            default_prompt = workflow.default_prompt
        return ResolvedWorkflowConfig(
            workflow_key=workflow_key,
            ai_app_id=config.ai_app_id,
            instance_type=config.instance_type,
            default_prompt=default_prompt,
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


def get_system_workflow_config(
    db: Session, workflow_key: str
) -> ResolvedWorkflowConfig:
    workflow = get_workflow(workflow_key)
    config = db.scalar(
        select(SystemWorkflowConfig).where(
            SystemWorkflowConfig.workflow_key == workflow_key
        )
    )
    if config is None:
        return ResolvedWorkflowConfig(
            workflow_key=workflow_key,
            ai_app_id=workflow.default_ai_app_id,
            instance_type="plus",
            default_prompt=workflow.default_prompt,
            is_enabled=False,
            settings={},
        )
    return ResolvedWorkflowConfig(
        workflow_key=workflow_key,
        ai_app_id=config.ai_app_id,
        instance_type=config.instance_type,
        default_prompt=config.default_prompt,
        is_enabled=config.is_enabled,
        settings=_decode_settings(config.settings_json),
    )


def save_system_workflow_config(
    db: Session,
    workflow_key: str,
    *,
    ai_app_id: str,
    instance_type: str,
    default_prompt: str,
    is_enabled: bool = True,
    settings: dict[str, Any] | None = None,
) -> SystemWorkflowConfig:
    get_workflow(workflow_key)
    clean_id = ai_app_id.strip()
    if not clean_id:
        raise ValueError("Workflow ID / AI App ID 不能为空")
    if instance_type not in {"default", "plus"}:
        raise ValueError("实例类型只能为 default 或 plus")
    config = db.scalar(
        select(SystemWorkflowConfig).where(
            SystemWorkflowConfig.workflow_key == workflow_key
        )
    )
    if config is None:
        config = SystemWorkflowConfig(workflow_key=workflow_key)
        db.add(config)
    config.ai_app_id = clean_id
    config.instance_type = instance_type
    config.default_prompt = default_prompt.strip()
    config.is_enabled = bool(is_enabled)
    config.settings_json = json.dumps(settings or {}, ensure_ascii=False)
    return config
