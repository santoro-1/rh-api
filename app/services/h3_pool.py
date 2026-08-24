from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    RunningHubExecutionAccount,
    RunningHubH3Capability,
    RunningHubPoolMembership,
    User,
)
from app.services.runninghub_pool import (
    UNHEALTHY_ACCOUNT_STATUSES,
    credential_active_task_count,
    execution_account_configuration_ready,
)
from app.services.security import encrypt_secret


class H3PoolValidationError(ValueError):
    pass


def user_has_h3_pool_entitlement(db: Session, user: User) -> bool:
    """Authorize H3 independently from the digital-human dual-pool switch."""

    del db
    return bool(user.is_active and user.h3_access_enabled)


@dataclass(frozen=True)
class H3ExecutionCapabilitySnapshot:
    execution_account_id: int
    label: str
    workflow_id: str
    instance_type: str
    max_concurrent_tasks: int
    safe_note: str
    has_access_password: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def configure_h3_capability(
    account: RunningHubExecutionAccount,
    *,
    workflow_id: str,
    instance_type: str,
    max_concurrent_tasks: int,
    safe_note: str = "",
    access_password: str | None = None,
    is_enabled: bool,
) -> RunningHubH3Capability:
    clean_workflow_id = str(workflow_id or "").strip()
    if is_enabled and not clean_workflow_id:
        raise H3PoolValidationError("启用 H3 账号能力时必须配置 workflowId")
    if len(clean_workflow_id) > 100:
        raise H3PoolValidationError("H3 workflowId 不能超过 100 个字符")
    if instance_type not in {"default", "plus"}:
        raise H3PoolValidationError("H3 实例类型只能为 default 或 plus")
    if isinstance(max_concurrent_tasks, bool):
        raise H3PoolValidationError("H3 单账号最大并发必须在 1 到 5 之间")
    try:
        concurrency = int(max_concurrent_tasks)
    except (TypeError, ValueError) as exc:
        raise H3PoolValidationError("H3 单账号最大并发必须在 1 到 5 之间") from exc
    if concurrency != max_concurrent_tasks or not 1 <= concurrency <= 5:
        raise H3PoolValidationError("H3 单账号最大并发必须在 1 到 5 之间")
    clean_note = str(safe_note or "").strip()
    if len(clean_note) > 500:
        raise H3PoolValidationError("H3 安全备注不能超过 500 个字符")
    capability = account.h3_capability
    if capability is None:
        capability = RunningHubH3Capability(execution_account=account)
    capability.workflow_id = clean_workflow_id
    capability.instance_type = instance_type
    capability.max_concurrent_tasks = concurrency
    capability.safe_note = clean_note
    if access_password is not None:
        clean_password = str(access_password).strip()
        if len(clean_password) > 500:
            raise H3PoolValidationError("H3 工作流访问密码不能超过 500 个字符")
        capability.access_password_encrypted = (
            encrypt_secret(clean_password) if clean_password else None
        )
    capability.is_enabled = bool(is_enabled)
    return capability


def h3_capability_ready(account: RunningHubExecutionAccount) -> bool:
    capability = account.h3_capability
    return bool(
        account.is_enabled
        and capability
        and capability.is_enabled
        and capability.workflow_id.strip()
        and capability.instance_type in {"default", "plus"}
        and 1 <= capability.max_concurrent_tasks <= 5
    )


def h3_capability_snapshots_for_user(
    db: Session,
    user: User,
) -> list[H3ExecutionCapabilitySnapshot]:
    """Return only H3-ready accounts authorized for this user; never return keys."""

    if not user_has_h3_pool_entitlement(db, user):
        return []

    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(RunningHubPoolMembership.admin_user_id == user.id)
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )
    return [
        H3ExecutionCapabilitySnapshot(
            execution_account_id=account.id,
            label=account.label,
            workflow_id=account.h3_capability.workflow_id,
            instance_type=account.h3_capability.instance_type,
            max_concurrent_tasks=account.h3_capability.max_concurrent_tasks,
            safe_note=account.h3_capability.safe_note,
            has_access_password=bool(
                account.h3_capability.access_password_encrypted
            ),
        )
        for account in accounts
        if h3_capability_ready(account)
    ]


def h3_execution_account_summary(db: Session, user: User) -> dict[str, object]:
    """Return a browser-safe fee-confirmation view with no key or workflow ID."""

    if not user_has_h3_pool_entitlement(db, user):
        raise H3PoolValidationError("当前账号尚未开通 H3 多账号执行池")
    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(RunningHubPoolMembership.admin_user_id == user.id)
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )
    now = datetime.now(timezone.utc)
    summaries: list[dict[str, object]] = []
    defaults: list[int] = []
    for account in accounts:
        capability = account.h3_capability
        configured = bool(
            execution_account_configuration_ready(account)
            and h3_capability_ready(account)
        )
        cooldown_active = bool(
            account.cooldown_until and _as_utc(account.cooldown_until) > now
        )
        health_status = str(account.health_status or "UNKNOWN").upper()
        if not account.is_enabled or not capability or not capability.is_enabled:
            availability = "DISABLED"
        elif not configured:
            availability = "INCOMPLETE"
        elif cooldown_active:
            availability = "COOLDOWN"
        elif health_status in UNHEALTHY_ACCOUNT_STATUSES:
            availability = "UNHEALTHY"
        else:
            availability = "AVAILABLE"
        selectable = configured
        if selectable:
            defaults.append(account.id)
        active = credential_active_task_count(db, account.credential_fingerprint)
        limit = capability.max_concurrent_tasks if capability else 0
        summaries.append(
            {
                "id": account.id,
                "label": account.label,
                "instance_type": capability.instance_type if capability else None,
                "max_concurrent_tasks": limit,
                "active_tasks": active,
                "available_slots": max(limit - active, 0),
                "health_status": health_status,
                "is_enabled": bool(account.is_enabled and capability and capability.is_enabled),
                "selectable": selectable,
                "availability": availability,
                "safe_note": capability.safe_note if capability else "",
                "has_access_password": bool(
                    capability and capability.access_password_encrypted
                ),
            }
        )
    return {
        "schema": "runninghub.h3-execution-accounts.v1",
        "accounts": summaries,
        "default_selected_account_ids": defaults,
    }


def validate_h3_account_selection(
    db: Session,
    user: User,
    raw_selection: object,
) -> list[int]:
    if not user_has_h3_pool_entitlement(db, user):
        raise H3PoolValidationError("当前账号尚未开通 H3 多账号执行池")
    if not isinstance(raw_selection, list) or not raw_selection:
        raise H3PoolValidationError("至少选择一个 H3 执行账号")
    if len(raw_selection) > 100 or any(
        type(account_id) is not int or account_id <= 0
        for account_id in raw_selection
    ):
        raise H3PoolValidationError("H3 执行账号 ID 列表不合法")
    if len(set(raw_selection)) != len(raw_selection):
        raise H3PoolValidationError("H3 执行账号 ID 不能重复")
    selected = sorted(raw_selection)
    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(
                RunningHubPoolMembership.admin_user_id == user.id,
                RunningHubExecutionAccount.id.in_(selected),
            )
        ).all()
    )
    if {account.id for account in accounts} != set(selected):
        raise H3PoolValidationError("所选 H3 执行账号不存在或不属于当前用户")
    if any(not h3_capability_ready(account) for account in accounts):
        raise H3PoolValidationError("所选账号存在未启用或未配置的 H3 能力")
    return selected
