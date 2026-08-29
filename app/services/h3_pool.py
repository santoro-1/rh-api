from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import (
    RunningHubExecutionAccount,
    RunningHubH3Capability,
    RunningHubPoolMembership,
    SystemWorkflowConfig,
    User,
)
from app.services.runninghub_pool import (
    UNHEALTHY_ACCOUNT_STATUSES,
    credential_active_task_count,
    execution_account_configuration_ready,
)
from app.services.runninghub_balance import (
    balance_summary,
    refresh_pool_account_balance,
)
from app.services.workflow_configs import get_system_workflow_config
from app.services.security import encrypt_secret


class H3PoolValidationError(ValueError):
    pass


def _verified_positive_balance(balance: dict[str, object]) -> bool:
    if balance.get("status") != "AVAILABLE":
        return False
    raw_coins = balance.get("remain_coins")
    if raw_coins is None or isinstance(raw_coins, bool):
        return False
    try:
        return Decimal(str(raw_coins)) > 0
    except (InvalidOperation, ValueError):
        return False


def _assigned_h3_accounts(
    db: Session,
    user: User,
) -> list[RunningHubExecutionAccount]:
    return list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(RunningHubPoolMembership.admin_user_id == user.id)
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )


def refresh_h3_execution_account_balances(db: Session, user: User) -> None:
    """Force a safe accountStatus refresh before presenting H3 account choices."""

    if not user_has_h3_pool_entitlement(db, user):
        raise H3PoolValidationError("当前账号尚未开通 H3 多账号执行池")
    for account in _assigned_h3_accounts(db, user):
        if not account.is_enabled or not execution_account_configuration_ready(account):
            continue
        refresh_pool_account_balance(db, account)
        db.commit()


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
    session = object_session(account)
    if session is not None:
        shared = next(
            (
                item
                for item in session.new
                if isinstance(item, SystemWorkflowConfig)
                and item.workflow_key == "minimax_h3_ref2va"
            ),
            None,
        )
        if shared is None:
            shared = session.scalar(
                select(SystemWorkflowConfig).where(
                    SystemWorkflowConfig.workflow_key == "minimax_h3_ref2va"
                )
            )
        if shared is None:
            shared = SystemWorkflowConfig(
                workflow_key="minimax_h3_ref2va",
                ai_app_id=clean_workflow_id,
                instance_type=instance_type,
                default_prompt="由 H3 PromptProfile 根据每段台词自动编译",
                is_enabled=bool(is_enabled),
                settings_json="{}",
            )
            if capability.access_password_encrypted:
                shared.settings_json = json.dumps(
                    {
                        "access_password_encrypted": (
                            capability.access_password_encrypted
                        )
                    },
                    ensure_ascii=False,
                )
            session.add(shared)
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

    config = get_system_workflow_config(db, "minimax_h3_ref2va")
    if not config.is_enabled or not config.ai_app_id:
        return []
    accounts = _assigned_h3_accounts(db, user)
    return [
        H3ExecutionCapabilitySnapshot(
            execution_account_id=account.id,
            label=account.label,
            workflow_id=config.ai_app_id,
            instance_type=config.instance_type,
            max_concurrent_tasks=account.max_concurrent_tasks,
            safe_note="",
            has_access_password=bool(config.settings.get("access_password_encrypted")),
        )
        for account in accounts
        if execution_account_configuration_ready(account)
    ]


def h3_execution_account_summary(db: Session, user: User) -> dict[str, object]:
    """Return a browser-safe fee-confirmation view with no key or workflow ID."""

    if not user_has_h3_pool_entitlement(db, user):
        raise H3PoolValidationError("当前账号尚未开通 H3 多账号执行池")
    config = get_system_workflow_config(db, "minimax_h3_ref2va")
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
        balance = balance_summary(db, account.credential_fingerprint)
        balance_verified = _verified_positive_balance(balance)
        configured = bool(
            execution_account_configuration_ready(account)
            and config.is_enabled
            and config.ai_app_id
        )
        cooldown_active = bool(
            account.cooldown_until and _as_utc(account.cooldown_until) > now
        )
        health_status = str(account.health_status or "UNKNOWN").upper()
        if not account.is_enabled or not config.is_enabled:
            availability = "DISABLED"
        elif not configured:
            availability = "INCOMPLETE"
        elif balance.get("status") == "AVAILABLE" and not balance_verified:
            availability = "NO_BALANCE"
        elif not balance_verified:
            availability = "BALANCE_UNAVAILABLE"
        elif cooldown_active:
            availability = "COOLDOWN"
        elif health_status in UNHEALTHY_ACCOUNT_STATUSES:
            availability = "UNHEALTHY"
        else:
            availability = "AVAILABLE"
        selectable = bool(configured and balance_verified)
        if selectable:
            defaults.append(account.id)
        active = credential_active_task_count(db, account.credential_fingerprint)
        limit = account.max_concurrent_tasks
        summaries.append(
            {
                "id": account.id,
                "label": account.label,
                "instance_type": config.instance_type,
                "max_concurrent_tasks": limit,
                "active_tasks": active,
                "available_slots": max(limit - active, 0),
                "health_status": health_status,
                "is_enabled": bool(account.is_enabled and config.is_enabled),
                "selectable": selectable,
                "availability": availability,
                "safe_note": "",
                "has_access_password": bool(config.settings.get("access_password_encrypted")),
                "balance": balance,
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
    config = get_system_workflow_config(db, "minimax_h3_ref2va")
    if not config.is_enabled or not config.ai_app_id:
        raise H3PoolValidationError("H3 系统工作流未启用或未配置")
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
    if any(not execution_account_configuration_ready(account) for account in accounts):
        raise H3PoolValidationError("所选账号存在已停用或未配置的 RunningHub 账号")
    balance_blocked_labels: list[str] = []
    for account in accounts:
        balance = balance_summary(db, account.credential_fingerprint)
        # Historical/internal callers without a balance row remain compatible;
        # the public account-list endpoint always refreshes first, and the
        # Worker performs another authoritative check immediately before submit.
        if balance.get("status") == "UNKNOWN" and balance.get("checked_at") is None:
            continue
        if not _verified_positive_balance(balance):
            balance_blocked_labels.append(account.label)
    if balance_blocked_labels:
        labels = "、".join(balance_blocked_labels)
        raise H3PoolValidationError(
            f"所选 H3 执行账号余额为 0 或本次余额读取失败，不能用于生成：{labels}"
        )
    return selected
