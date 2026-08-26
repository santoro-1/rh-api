from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from app.models import (
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    GenerationTaskAttempt,
    GenerationTaskEnhancement,
    RunningHubConfig,
    RunningHubExecutionAccount,
    RunningHubPoolMembership,
    SeedVR2ExecutionAccount,
    SystemWorkflowConfig,
    TaskStatus,
    User,
)
from app.services.security import (
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
)
from app.services.runninghub_balance import balance_summary


ACTIVE_POOL_TASK_STATUSES = {
    TaskStatus.UPLOADING.value,
    TaskStatus.SUBMITTED.value,
    TaskStatus.RUNNING.value,
}
UNHEALTHY_ACCOUNT_STATUSES = {"UNHEALTHY", "ERROR"}


class RunningHubPoolValidationError(ValueError):
    pass


class DuplicateRunningHubCredentialError(RunningHubPoolValidationError):
    pass


class RunningHubPoolSelectionFormatError(RunningHubPoolValidationError):
    pass


class RunningHubPoolSelectionPermissionError(RunningHubPoolValidationError):
    pass


class RunningHubPoolSelectionUnavailableError(RunningHubPoolValidationError):
    pass


class RunningHubPoolSnapshotConflictError(RunningHubPoolValidationError):
    pass


def credential_activity_filter(task_alias, fingerprint: str):
    """Match active tasks using one real credential across old and pool paths."""

    pool_account_ids = select(RunningHubExecutionAccount.id).where(
        RunningHubExecutionAccount.credential_fingerprint == fingerprint
    )
    legacy_user_ids = select(RunningHubConfig.user_id).where(
        RunningHubConfig.credential_fingerprint == fingerprint
    )
    dual_item_ids = select(GenerationBatchItem.id).join(GenerationBatch).where(
        GenerationBatch.execution_mode == BATCH_EXECUTION_MODE_DUAL_POOL_V1
    )
    dual_segment_ids = select(GenerationSegment.id).where(
        GenerationSegment.batch_item_id.in_(dual_item_ids)
    )
    source_persisted_dual_task_ids = select(
        GenerationTaskEnhancement.generation_task_id
    ).where(
        GenerationTaskEnhancement.generation_task_id == task_alias.id,
        or_(
            task_alias.batch_item_id.in_(dual_item_ids),
            task_alias.segment_id.in_(dual_segment_ids),
        ),
    )
    return and_(
        or_(
            task_alias.status.in_(ACTIVE_POOL_TASK_STATUSES),
            task_alias.error_code.in_(
                {"SUBMIT_OUTCOME_UNKNOWN", "VIDEO_ENHANCEMENT_SUBMIT_UNKNOWN"}
            ),
        ),
        or_(
            task_alias.execution_account_id.in_(pool_account_ids),
            and_(
                task_alias.execution_account_id.is_(None),
                task_alias.user_id.in_(legacy_user_ids),
            ),
        ),
        task_alias.id.not_in(source_persisted_dual_task_ids),
    )


def credential_active_task_count(db: Session, fingerprint: str) -> int:
    task_alias = aliased(GenerationTask)
    return int(
        db.scalar(
            select(func.count(task_alias.id)).where(
                credential_activity_filter(task_alias, fingerprint)
            )
        )
        or 0
    )


def credential_active_count_subquery(fingerprint: str):
    task_alias = aliased(GenerationTask)
    return (
        select(func.count(task_alias.id))
        .where(credential_activity_filter(task_alias, fingerprint))
        .scalar_subquery()
    )


def _clean_account_fields(
    *,
    label: str,
    base_url: str,
    digital_human_ai_app_id: str,
    max_concurrent_tasks: int,
) -> tuple[str, str, str, int]:
    clean_label = label.strip()
    clean_url = base_url.strip().rstrip("/")
    clean_app_id = digital_human_ai_app_id.strip()
    if not clean_label or len(clean_label) > 100:
        raise RunningHubPoolValidationError("账号备注名称长度必须在 1 到 100 之间")
    parsed_url = urlsplit(clean_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RunningHubPoolValidationError(
            "RunningHub Base URL 必须是有效的 http:// 或 https:// 地址"
        )
    if len(clean_url) > 500:
        raise RunningHubPoolValidationError("RunningHub Base URL 不能超过 500 个字符")
    if not clean_app_id or len(clean_app_id) > 100:
        raise RunningHubPoolValidationError(
            "数字人 AI App ID 长度必须在 1 到 100 之间"
        )
    if not 1 <= max_concurrent_tasks <= 5:
        raise RunningHubPoolValidationError("单个 RunningHub 账号最大并发必须在 1 到 5 之间")
    return clean_label, clean_url, clean_app_id, max_concurrent_tasks


def _validated_member_ids(db: Session, user_ids: list[int]) -> set[int]:
    """Validate website users assigned to one real RunningHub account.

    The persisted membership column keeps its historical ``admin_user_id`` name
    for upgrade compatibility, but membership is intentionally available to any
    active website user. H3 access remains an independent user-level switch.
    """

    requested = {int(user_id) for user_id in user_ids}
    if not requested:
        return set()
    valid = set(
        db.scalars(
            select(User.id).where(
                User.id.in_(requested),
                User.is_active.is_(True),
            )
        ).all()
    )
    if valid != requested:
        raise RunningHubPoolValidationError(
            "资源池成员必须全部是已启用的网站账号"
        )
    return valid


def backfill_runninghub_config_fingerprints(db: Session) -> int:
    """Lazily identify usable legacy credentials without changing their ownership."""

    changed = 0
    configs = db.scalars(
        select(RunningHubConfig).where(
            RunningHubConfig.api_key_encrypted.is_not(None),
            RunningHubConfig.credential_fingerprint.is_(None),
        )
    ).all()
    for config in configs:
        try:
            api_key = decrypt_secret(config.api_key_encrypted)
        except ValueError:
            # An unreadable legacy key is already unusable. Leave it untouched so
            # an administrator can repair that user's configuration separately.
            continue
        config.credential_fingerprint = secret_fingerprint(api_key)
        changed += 1
    if changed:
        db.flush()
    return changed


def _ensure_unique_pool_fingerprint(
    db: Session,
    fingerprint: str,
    *,
    exclude_account_id: int | None = None,
) -> None:
    if db.scalar(
        select(SeedVR2ExecutionAccount.id).where(
            SeedVR2ExecutionAccount.credential_fingerprint == fingerprint
        )
    ) is not None:
        raise DuplicateRunningHubCredentialError(
            "该 RunningHub API Key 已存在于 SeedVR2 账号池，不能跨池重复计算容量"
        )
    statement = select(RunningHubExecutionAccount.id).where(
        RunningHubExecutionAccount.credential_fingerprint == fingerprint
    )
    if exclude_account_id is not None:
        statement = statement.where(RunningHubExecutionAccount.id != exclude_account_id)
    if db.scalar(statement) is not None:
        raise DuplicateRunningHubCredentialError(
            "该 RunningHub API Key 已存在于执行账号资源池，不能重复计算容量"
        )


def _sync_memberships(
    db: Session,
    account: RunningHubExecutionAccount,
    user_ids: set[int],
) -> bool:
    memberships = db.scalars(
        select(RunningHubPoolMembership).where(
            RunningHubPoolMembership.execution_account_id == account.id
        )
    ).all()
    existing_ids = {membership.admin_user_id for membership in memberships}
    if existing_ids == user_ids:
        return False
    for membership in memberships:
        if membership.admin_user_id not in user_ids:
            db.delete(membership)
    for user_id in sorted(user_ids - existing_ids):
        db.add(
            RunningHubPoolMembership(
                admin_user_id=user_id,
                execution_account=account,
            )
        )
    return True


def create_execution_account(
    db: Session,
    *,
    label: str,
    api_key: str,
    base_url: str,
    digital_human_ai_app_id: str,
    max_concurrent_tasks: int,
    is_enabled: bool,
    user_ids: list[int] | None = None,
    admin_user_ids: list[int] | None = None,
) -> RunningHubExecutionAccount:
    clean_key = api_key.strip()
    if not clean_key or len(clean_key) > 4096:
        raise RunningHubPoolValidationError(
            "RunningHub API Key 长度必须在 1 到 4096 之间"
        )
    clean_label, clean_url, clean_app_id, concurrency = _clean_account_fields(
        label=label,
        base_url=base_url,
        digital_human_ai_app_id=digital_human_ai_app_id,
        max_concurrent_tasks=max_concurrent_tasks,
    )
    # ``admin_user_ids`` is accepted while older code-update packages or an
    # already-open admin page are still posting the historical field name.
    member_ids = _validated_member_ids(
        db, user_ids if user_ids is not None else (admin_user_ids or [])
    )
    fingerprint = secret_fingerprint(clean_key)
    _ensure_unique_pool_fingerprint(db, fingerprint)
    backfill_runninghub_config_fingerprints(db)
    account = RunningHubExecutionAccount(
        label=clean_label,
        api_key_encrypted=encrypt_secret(clean_key),
        credential_fingerprint=fingerprint,
        base_url=clean_url,
        digital_human_ai_app_id=clean_app_id,
        max_concurrent_tasks=concurrency,
        is_enabled=is_enabled,
        health_status="UNKNOWN",
    )
    db.add(account)
    db.flush()
    if db.scalar(
        select(SystemWorkflowConfig.id).where(
            SystemWorkflowConfig.workflow_key == "digital_human"
        )
    ) is None:
        # Compatibility for a fresh database or an older caller that creates
        # the first execution account before the workflow admin page is saved.
        db.add(
            SystemWorkflowConfig(
                workflow_key="digital_human",
                ai_app_id=clean_app_id,
                instance_type="plus",
                default_prompt=(
                    "人物自然地说话，表情自然，动作自然，镜头保持稳定。"
                ),
                is_enabled=True,
                settings_json="{}",
            )
        )
    _sync_memberships(db, account, member_ids)
    db.flush()
    return account


def _account_has_task_history(db: Session, account_id: int) -> bool:
    task_count = db.scalar(
        select(func.count(GenerationTask.id)).where(
            GenerationTask.execution_account_id == account_id
        )
    ) or 0
    if task_count:
        return True
    attempt_count = db.scalar(
        select(func.count(GenerationTaskAttempt.id)).where(
            GenerationTaskAttempt.execution_account_id == account_id
        )
    ) or 0
    return bool(attempt_count)


def update_execution_account(
    db: Session,
    account: RunningHubExecutionAccount,
    *,
    label: str,
    api_key: str,
    base_url: str,
    digital_human_ai_app_id: str,
    max_concurrent_tasks: int,
    is_enabled: bool,
    user_ids: list[int] | None = None,
    admin_user_ids: list[int] | None = None,
) -> set[str]:
    clean_label, clean_url, clean_app_id, concurrency = _clean_account_fields(
        label=label,
        base_url=base_url,
        digital_human_ai_app_id=digital_human_ai_app_id,
        max_concurrent_tasks=max_concurrent_tasks,
    )
    member_ids = _validated_member_ids(
        db, user_ids if user_ids is not None else (admin_user_ids or [])
    )
    changed_fields: set[str] = set()
    clean_key = api_key.strip()
    if clean_key:
        if len(clean_key) > 4096:
            raise RunningHubPoolValidationError(
                "RunningHub API Key 不能超过 4096 个字符"
            )
        fingerprint = secret_fingerprint(clean_key)
        if fingerprint != account.credential_fingerprint:
            if _account_has_task_history(db, account.id):
                raise RunningHubPoolValidationError(
                    "该执行账号已有任务历史，不能原地更换 API Key；请停用旧账号并新建账号"
                )
            _ensure_unique_pool_fingerprint(
                db,
                fingerprint,
                exclude_account_id=account.id,
            )
            account.api_key_encrypted = encrypt_secret(clean_key)
            account.credential_fingerprint = fingerprint
            changed_fields.add("api_key")
    field_values = {
        "label": clean_label,
        "base_url": clean_url,
        "digital_human_ai_app_id": clean_app_id,
        "max_concurrent_tasks": concurrency,
        "is_enabled": is_enabled,
    }
    for field, value in field_values.items():
        if getattr(account, field) != value:
            setattr(account, field, value)
            changed_fields.add(field)
    if _sync_memberships(db, account, member_ids):
        changed_fields.add("user_ids")
    backfill_runninghub_config_fingerprints(db)
    db.flush()
    return changed_fields


def execution_account_for_admin_page(
    db: Session, account_id: int
) -> RunningHubExecutionAccount | None:
    return db.scalar(
        select(RunningHubExecutionAccount)
        .options(
            selectinload(RunningHubExecutionAccount.pool_memberships).selectinload(
                RunningHubPoolMembership.admin_user
            ),
            selectinload(RunningHubExecutionAccount.h3_capability),
        )
        .where(RunningHubExecutionAccount.id == account_id)
    )


def execution_accounts_for_admin_page(
    db: Session,
) -> list[RunningHubExecutionAccount]:
    return list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .options(
                selectinload(RunningHubExecutionAccount.pool_memberships).selectinload(
                    RunningHubPoolMembership.admin_user
                ),
                selectinload(RunningHubExecutionAccount.h3_capability),
            )
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )


def sync_user_execution_account_memberships(
    db: Session, user_id: int, execution_account_ids: list[int]
) -> None:
    requested = {int(account_id) for account_id in execution_account_ids}
    if requested:
        existing_account_ids = set(
            db.scalars(
                select(RunningHubExecutionAccount.id).where(
                    RunningHubExecutionAccount.id.in_(requested)
                )
            ).all()
        )
        if existing_account_ids != requested:
            raise RunningHubPoolValidationError("选择中包含不存在的 RunningHub 账号")
    memberships = db.scalars(
        select(RunningHubPoolMembership).where(
            RunningHubPoolMembership.admin_user_id == user_id
        )
    ).all()
    existing = {item.execution_account_id for item in memberships}
    for membership in memberships:
        if membership.execution_account_id not in requested:
            db.delete(membership)
    for account_id in sorted(requested - existing):
        db.add(
            RunningHubPoolMembership(
                admin_user_id=user_id,
                execution_account_id=account_id,
            )
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def execution_account_configuration_ready(
    account: RunningHubExecutionAccount,
) -> bool:
    try:
        _clean_account_fields(
            label=account.label,
            base_url=account.base_url,
            digital_human_ai_app_id=account.digital_human_ai_app_id,
            max_concurrent_tasks=account.max_concurrent_tasks,
        )
        api_key = decrypt_secret(account.api_key_encrypted)
    except ValueError:
        return False
    return bool(
        account.credential_fingerprint
        and secret_fingerprint(api_key) == account.credential_fingerprint
    )


def workbench_execution_account_summary(
    db: Session,
    admin_user: User,
) -> dict[str, object]:
    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(RunningHubPoolMembership.admin_user_id == admin_user.id)
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )
    now = datetime.now(timezone.utc)
    summaries: list[dict[str, object]] = []
    default_selected_ids: list[int] = []
    for account in accounts:
        configured = execution_account_configuration_ready(account)
        cooldown_active = bool(
            account.cooldown_until and _as_utc(account.cooldown_until) > now
        )
        health_status = (account.health_status or "UNKNOWN").upper()
        if not account.is_enabled:
            availability = "DISABLED"
        elif not configured:
            availability = "INCOMPLETE"
        elif cooldown_active:
            availability = "COOLDOWN"
        elif health_status in UNHEALTHY_ACCOUNT_STATUSES:
            availability = "UNHEALTHY"
        else:
            availability = "AVAILABLE"
        # The operation snapshot defaults to every globally enabled, complete
        # account. Health/cooldown/capacity are dispatch-time filters; keeping
        # the account selectable lets it recover during a long batch without
        # silently shrinking the administrator's chosen scope.
        selectable = bool(account.is_enabled and configured)
        if selectable:
            default_selected_ids.append(account.id)
        active_tasks = credential_active_task_count(
            db, account.credential_fingerprint
        )
        summaries.append(
            {
                "id": account.id,
                "label": account.label,
                "max_concurrent_tasks": account.max_concurrent_tasks,
                "active_tasks": active_tasks,
                "available_slots": max(
                    account.max_concurrent_tasks - active_tasks,
                    0,
                ),
                "health_status": health_status,
                "health_checked_at": (
                    account.health_checked_at.isoformat()
                    if account.health_checked_at
                    else None
                ),
                "cooldown_until": (
                    account.cooldown_until.isoformat()
                    if account.cooldown_until
                    else None
                ),
                "is_enabled": account.is_enabled,
                "selectable": selectable,
                "availability": availability,
                "balance": balance_summary(db, account.credential_fingerprint),
            }
        )
    return {
        "schema": "runninghub.workbench-execution-accounts.v1",
        "accounts": summaries,
        "default_selected_account_ids": default_selected_ids,
    }


def validate_workbench_execution_account_selection(
    db: Session,
    user: User,
    *,
    selection_provided: bool,
    raw_selection: object,
    allow_non_admin: bool = False,
) -> list[int] | None:
    """Validate untrusted workbench IDs without accepting account configuration."""

    if not user.is_admin and not allow_non_admin:
        if selection_provided:
            raise RunningHubPoolSelectionPermissionError(
                "普通用户不能指定 RunningHub 执行账号资源池"
            )
        return None
    if not selection_provided or not isinstance(raw_selection, list):
        raise RunningHubPoolSelectionFormatError(
            "管理员画面生成必须提交 RunningHub 执行账号 ID 列表"
        )
    if not raw_selection:
        raise RunningHubPoolSelectionFormatError("至少选择一个 RunningHub 执行账号")
    if len(raw_selection) > 100:
        raise RunningHubPoolSelectionFormatError(
            "单次最多选择 100 个 RunningHub 执行账号"
        )
    if any(type(account_id) is not int or account_id <= 0 for account_id in raw_selection):
        raise RunningHubPoolSelectionFormatError(
            "RunningHub 执行账号 ID 必须是正整数"
        )
    if len(set(raw_selection)) != len(raw_selection):
        raise RunningHubPoolSelectionFormatError(
            "RunningHub 执行账号 ID 不能重复"
        )
    selected_ids = sorted(raw_selection)
    accounts = list(
        db.scalars(
            select(RunningHubExecutionAccount)
            .join(RunningHubPoolMembership)
            .where(
                RunningHubExecutionAccount.id.in_(selected_ids),
                RunningHubPoolMembership.admin_user_id == user.id,
            )
            .order_by(RunningHubExecutionAccount.id)
        ).all()
    )
    if [account.id for account in accounts] != selected_ids:
        raise RunningHubPoolSelectionPermissionError(
            "选择中包含不存在或无权使用的 RunningHub 执行账号"
        )
    unavailable = [
        account.id
        for account in accounts
        if not account.is_enabled
        or not execution_account_configuration_ready(account)
    ]
    if unavailable:
        raise RunningHubPoolSelectionUnavailableError(
            "选择中包含已停用或数字人工作流配置不完整的 RunningHub 执行账号"
        )
    return selected_ids


def _decode_batch_execution_account_snapshot(
    serialized: str,
) -> list[int]:
    try:
        value = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RunningHubPoolSnapshotConflictError(
            "RunningHub 执行账号操作快照已损坏"
        ) from exc
    if (
        not isinstance(value, list)
        or not value
        or any(type(account_id) is not int or account_id <= 0 for account_id in value)
        or len(set(value)) != len(value)
    ):
        raise RunningHubPoolSnapshotConflictError(
            "RunningHub 执行账号操作快照已损坏"
        )
    return sorted(value)


def batch_execution_account_snapshot(
    batch: GenerationBatch,
) -> list[int] | None:
    if not batch.runninghub_execution_account_ids_json:
        return None
    return _decode_batch_execution_account_snapshot(
        batch.runninghub_execution_account_ids_json
    )


def item_execution_account_snapshot(
    item: GenerationBatchItem,
) -> list[int] | None:
    """Return the row-level pool, falling back to the legacy batch snapshot."""

    if item.runninghub_execution_account_ids_json:
        return _decode_batch_execution_account_snapshot(
            item.runninghub_execution_account_ids_json
        )
    return batch_execution_account_snapshot(item.batch)


def bind_item_execution_account_snapshot(
    db: Session,
    item: GenerationBatchItem,
    selected_account_ids: list[int] | None,
    *,
    allow_replace: bool = False,
) -> list[int] | None:
    """Bind one row's pool; unpaid rows may replace an earlier failed choice."""

    if selected_account_ids is None:
        if item.runninghub_execution_account_ids_json:
            raise RunningHubPoolSnapshotConflictError(
                "该画面生成任务已绑定 RunningHub 执行账号资源池"
            )
        return None
    canonical_ids = sorted(selected_account_ids)
    serialized = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
    current = (
        _decode_batch_execution_account_snapshot(
            item.runninghub_execution_account_ids_json
        )
        if item.runninghub_execution_account_ids_json
        else None
    )
    if current == canonical_ids:
        return canonical_ids
    if current is not None and not allow_replace:
        raise RunningHubPoolSnapshotConflictError(
            "该画面生成任务的 RunningHub 执行账号快照已锁定，不能修改"
        )
    expected = item.runninghub_execution_account_ids_json
    result = db.execute(
        update(GenerationBatchItem)
        .where(
            GenerationBatchItem.id == item.id,
            GenerationBatchItem.runninghub_execution_account_ids_json
            == expected,
        )
        .values(runninghub_execution_account_ids_json=serialized)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        item.runninghub_execution_account_ids_json = serialized
        return canonical_ids
    db.refresh(item, attribute_names=["runninghub_execution_account_ids_json"])
    refreshed = item_execution_account_snapshot(item)
    if refreshed != canonical_ids:
        raise RunningHubPoolSnapshotConflictError(
            "该画面生成任务的 RunningHub 执行账号快照已被其他请求修改"
        )
    return canonical_ids


def bind_batch_execution_account_snapshot(
    db: Session,
    batch: GenerationBatch,
    selected_account_ids: list[int] | None,
) -> list[int] | None:
    """Compare-and-set one immutable operation scope without rebuilding rows."""

    if selected_account_ids is None:
        if batch.runninghub_execution_account_ids_json:
            raise RunningHubPoolSnapshotConflictError(
                "该画面生成操作已绑定 RunningHub 执行账号资源池"
            )
        return None
    canonical_ids = sorted(selected_account_ids)
    serialized = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
    if batch.runninghub_execution_account_ids_json:
        if batch_execution_account_snapshot(batch) != canonical_ids:
            raise RunningHubPoolSnapshotConflictError(
                "该画面生成操作的 RunningHub 执行账号快照已锁定，不能修改"
            )
        return canonical_ids
    result = db.execute(
        update(GenerationBatch)
        .where(
            GenerationBatch.id == batch.id,
            GenerationBatch.runninghub_execution_account_ids_json.is_(None),
        )
        .values(runninghub_execution_account_ids_json=serialized)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        batch.runninghub_execution_account_ids_json = serialized
        return canonical_ids
    db.refresh(batch, attribute_names=["runninghub_execution_account_ids_json"])
    if batch_execution_account_snapshot(batch) != canonical_ids:
        raise RunningHubPoolSnapshotConflictError(
            "该画面生成操作的 RunningHub 执行账号快照已锁定，不能修改"
        )
    return canonical_ids
