from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import (
    GenerationBatch,
    GenerationBatchItem,
    GenerationTaskEnhancement,
    GenerationTaskEnhancementAttempt,
    RunningHubDualPoolGrant,
    RunningHubConfig,
    RunningHubExecutionAccount,
    SeedVR2ExecutionAccount,
    SeedVR2PoolMembership,
    User,
)
from app.services.security import decrypt_secret, encrypt_secret, secret_fingerprint
from app.services.runninghub_balance import balance_summary


class SeedVR2PoolValidationError(ValueError):
    pass


class DuplicateSeedVR2CredentialError(SeedVR2PoolValidationError):
    pass


class SeedVR2PoolSelectionError(SeedVR2PoolValidationError):
    pass


class SeedVR2PoolSelectionFormatError(SeedVR2PoolSelectionError):
    pass


class SeedVR2PoolSelectionPermissionError(SeedVR2PoolSelectionError):
    pass


class SeedVR2PoolSelectionUnavailableError(SeedVR2PoolSelectionError):
    pass


class SeedVR2PoolSnapshotConflictError(SeedVR2PoolValidationError):
    pass


def _clean_account_fields(
    *,
    label: str,
    base_url: str,
    seedvr2_ai_app_id: str,
    max_concurrent_tasks: int,
) -> tuple[str, str, str, int]:
    clean_label = label.strip()
    clean_url = base_url.strip().rstrip("/")
    clean_app_id = seedvr2_ai_app_id.strip()
    if not clean_label or len(clean_label) > 100:
        raise SeedVR2PoolValidationError("账号备注名称长度必须在 1 到 100 之间")
    parsed_url = urlsplit(clean_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise SeedVR2PoolValidationError(
            "RunningHub Base URL 必须是有效的 http:// 或 https:// 地址"
        )
    if len(clean_url) > 500:
        raise SeedVR2PoolValidationError("RunningHub Base URL 不能超过 500 个字符")
    if not clean_app_id or len(clean_app_id) > 100:
        raise SeedVR2PoolValidationError(
            "SeedVR2 AI App ID 长度必须在 1 到 100 之间"
        )
    if not 1 <= max_concurrent_tasks <= 5:
        raise SeedVR2PoolValidationError(
            "单个 RunningHub 账号最大并发必须在 1 到 5 之间"
        )
    return clean_label, clean_url, clean_app_id, max_concurrent_tasks


def _validated_member_ids(db: Session, user_ids: list[int]) -> set[int]:
    requested = {int(user_id) for user_id in user_ids}
    if not requested:
        raise SeedVR2PoolValidationError("至少选择一个可使用此 SeedVR2 账号的用户")
    valid = set(
        db.scalars(
            select(User.id)
            .join(RunningHubDualPoolGrant)
            .where(
                User.id.in_(requested),
                User.is_active.is_(True),
                RunningHubDualPoolGrant.is_enabled.is_(True),
                (User.is_admin.is_(True))
                | (RunningHubDualPoolGrant.allow_non_admin.is_(True)),
            )
        ).all()
    )
    if valid != requested:
        raise SeedVR2PoolValidationError(
            "SeedVR2 资源池成员必须具有有效的双账号池授权"
        )
    return valid


def _ensure_unique_fingerprint(
    db: Session,
    fingerprint: str,
    *,
    exclude_seedvr2_account_id: int | None = None,
) -> None:
    # Historical single-account rows may predate fingerprints.  Backfill them
    # in the current transaction before deciding whether this credential would
    # create a second, falsely independent SeedVR2 capacity bucket.
    from app.services.runninghub_pool import backfill_runninghub_config_fingerprints

    backfill_runninghub_config_fingerprints(db)
    if db.scalar(
        select(RunningHubConfig.user_id).where(
            RunningHubConfig.credential_fingerprint == fingerprint
        )
    ) is not None:
        raise DuplicateSeedVR2CredentialError(
            "该 RunningHub API Key 已用于现有单账号配置，不能在 SeedVR2 池重复计算容量"
        )
    if db.scalar(
        select(RunningHubExecutionAccount.id).where(
            RunningHubExecutionAccount.credential_fingerprint == fingerprint
        )
    ) is not None:
        raise DuplicateSeedVR2CredentialError(
            "该 RunningHub API Key 已存在于数字人账号池，不能跨池重复计算容量"
        )
    statement = select(SeedVR2ExecutionAccount.id).where(
        SeedVR2ExecutionAccount.credential_fingerprint == fingerprint
    )
    if exclude_seedvr2_account_id is not None:
        statement = statement.where(
            SeedVR2ExecutionAccount.id != exclude_seedvr2_account_id
        )
    if db.scalar(statement) is not None:
        raise DuplicateSeedVR2CredentialError(
            "该 RunningHub API Key 已存在于 SeedVR2 账号池，不能重复计算容量"
        )


def _sync_memberships(
    db: Session, account: SeedVR2ExecutionAccount, user_ids: set[int]
) -> bool:
    memberships = db.scalars(
        select(SeedVR2PoolMembership).where(
            SeedVR2PoolMembership.execution_account_id == account.id
        )
    ).all()
    existing_ids = {membership.user_id for membership in memberships}
    if existing_ids == user_ids:
        return False
    for membership in memberships:
        if membership.user_id not in user_ids:
            db.delete(membership)
    for user_id in sorted(user_ids - existing_ids):
        db.add(SeedVR2PoolMembership(user_id=user_id, execution_account=account))
    return True


def create_seedvr2_execution_account(
    db: Session,
    *,
    label: str,
    api_key: str,
    base_url: str,
    seedvr2_ai_app_id: str,
    max_concurrent_tasks: int,
    is_enabled: bool,
    user_ids: list[int],
) -> SeedVR2ExecutionAccount:
    clean_key = api_key.strip()
    if not clean_key or len(clean_key) > 4096:
        raise SeedVR2PoolValidationError(
            "RunningHub API Key 长度必须在 1 到 4096 之间"
        )
    clean_label, clean_url, clean_app_id, concurrency = _clean_account_fields(
        label=label,
        base_url=base_url,
        seedvr2_ai_app_id=seedvr2_ai_app_id,
        max_concurrent_tasks=max_concurrent_tasks,
    )
    member_ids = _validated_member_ids(db, user_ids)
    fingerprint = secret_fingerprint(clean_key)
    _ensure_unique_fingerprint(db, fingerprint)
    account = SeedVR2ExecutionAccount(
        label=clean_label,
        api_key_encrypted=encrypt_secret(clean_key),
        credential_fingerprint=fingerprint,
        base_url=clean_url,
        seedvr2_ai_app_id=clean_app_id,
        max_concurrent_tasks=concurrency,
        is_enabled=is_enabled,
        health_status="UNKNOWN",
    )
    db.add(account)
    db.flush()
    _sync_memberships(db, account, member_ids)
    db.flush()
    return account


def _account_has_attempt_history(db: Session, account_id: int) -> bool:
    enhancement_count = db.scalar(
        select(func.count(GenerationTaskEnhancement.id)).where(
            GenerationTaskEnhancement.seedvr2_execution_account_id == account_id
        )
    ) or 0
    if enhancement_count:
        return True
    attempt_count = db.scalar(
        select(func.count(GenerationTaskEnhancementAttempt.id)).where(
            GenerationTaskEnhancementAttempt.seedvr2_execution_account_id == account_id
        )
    ) or 0
    return bool(attempt_count)


def update_seedvr2_execution_account(
    db: Session,
    account: SeedVR2ExecutionAccount,
    *,
    label: str,
    api_key: str,
    base_url: str,
    seedvr2_ai_app_id: str,
    max_concurrent_tasks: int,
    is_enabled: bool,
    user_ids: list[int],
) -> set[str]:
    clean_label, clean_url, clean_app_id, concurrency = _clean_account_fields(
        label=label,
        base_url=base_url,
        seedvr2_ai_app_id=seedvr2_ai_app_id,
        max_concurrent_tasks=max_concurrent_tasks,
    )
    member_ids = _validated_member_ids(db, user_ids)
    changed_fields: set[str] = set()
    clean_key = api_key.strip()
    if clean_key:
        if len(clean_key) > 4096:
            raise SeedVR2PoolValidationError("RunningHub API Key 不能超过 4096 个字符")
        fingerprint = secret_fingerprint(clean_key)
        if fingerprint != account.credential_fingerprint:
            if _account_has_attempt_history(db, account.id):
                raise SeedVR2PoolValidationError(
                    "该 SeedVR2 执行账号已有任务历史，不能原地更换 API Key"
                )
            _ensure_unique_fingerprint(
                db,
                fingerprint,
                exclude_seedvr2_account_id=account.id,
            )
            account.api_key_encrypted = encrypt_secret(clean_key)
            account.credential_fingerprint = fingerprint
            changed_fields.add("api_key")
    field_values = {
        "label": clean_label,
        "base_url": clean_url,
        "seedvr2_ai_app_id": clean_app_id,
        "max_concurrent_tasks": concurrency,
        "is_enabled": is_enabled,
    }
    for field, value in field_values.items():
        if getattr(account, field) != value:
            setattr(account, field, value)
            changed_fields.add(field)
    if _sync_memberships(db, account, member_ids):
        changed_fields.add("user_ids")
    db.flush()
    return changed_fields


def seedvr2_account_configuration_ready(account: SeedVR2ExecutionAccount) -> bool:
    try:
        api_key = decrypt_secret(account.api_key_encrypted)
    except ValueError:
        return False
    return bool(
        account.credential_fingerprint
        and secret_fingerprint(api_key) == account.credential_fingerprint
        and account.base_url
        and account.seedvr2_ai_app_id
        and 1 <= account.max_concurrent_tasks <= 5
    )


def seedvr2_execution_accounts_for_admin_page(
    db: Session,
) -> list[SeedVR2ExecutionAccount]:
    return list(
        db.scalars(
            select(SeedVR2ExecutionAccount)
            .options(
                selectinload(SeedVR2ExecutionAccount.pool_memberships).selectinload(
                    SeedVR2PoolMembership.user
                )
            )
            .order_by(SeedVR2ExecutionAccount.id)
        ).all()
    )


def seedvr2_execution_account_for_admin_page(
    db: Session, account_id: int
) -> SeedVR2ExecutionAccount | None:
    return db.scalar(
        select(SeedVR2ExecutionAccount)
        .options(
            selectinload(SeedVR2ExecutionAccount.pool_memberships).selectinload(
                SeedVR2PoolMembership.user
            )
        )
        .where(SeedVR2ExecutionAccount.id == account_id)
    )


def seedvr2_workbench_account_summary(db: Session, user: User) -> dict[str, object]:
    from app.services.seedvr2_dispatch import seedvr2_active_count

    accounts = list(
        db.scalars(
            select(SeedVR2ExecutionAccount)
            .join(SeedVR2PoolMembership)
            .where(SeedVR2PoolMembership.user_id == user.id)
            .order_by(SeedVR2ExecutionAccount.id)
        ).all()
    )
    summaries: list[dict[str, object]] = []
    default_ids: list[int] = []
    now = datetime.now(timezone.utc)
    for account in accounts:
        configured = seedvr2_account_configuration_ready(account)
        selectable = bool(account.is_enabled and configured)
        if selectable:
            default_ids.append(account.id)
        active_tasks = seedvr2_active_count(db, account.id)
        health = (account.health_status or "UNKNOWN").upper()
        availability = (
            "DISABLED" if not account.is_enabled else
            "INCOMPLETE" if not configured else
            "COOLDOWN" if account.cooldown_until is not None and (
                account.cooldown_until.replace(tzinfo=timezone.utc)
                if account.cooldown_until.tzinfo is None else account.cooldown_until
            ) > now else
            "UNHEALTHY" if health in {"UNHEALTHY", "ERROR", "DISABLED"} else
            "AVAILABLE"
        )
        summaries.append(
            {
                "id": account.id,
                "label": account.label,
                "max_concurrent_tasks": account.max_concurrent_tasks,
                "active_tasks": active_tasks,
                "available_slots": max(account.max_concurrent_tasks - active_tasks, 0),
                "health_status": health,
                "health_checked_at": account.health_checked_at.isoformat() if account.health_checked_at else None,
                "cooldown_until": account.cooldown_until.isoformat() if account.cooldown_until else None,
                "is_enabled": account.is_enabled,
                "selectable": selectable,
                "availability": availability,
                "balance": balance_summary(db, account.credential_fingerprint),
            }
        )
    return {"accounts": summaries, "default_selected_account_ids": default_ids}


def validate_seedvr2_account_selection(
    db: Session, *, user: User, raw_selection: object
) -> list[int]:
    if not isinstance(raw_selection, list) or not raw_selection:
        raise SeedVR2PoolSelectionFormatError("至少选择一个 SeedVR2 执行账号")
    if len(raw_selection) > 100:
        raise SeedVR2PoolSelectionFormatError(
            "单次最多选择 100 个 SeedVR2 执行账号"
        )
    if any(type(account_id) is not int or account_id <= 0 for account_id in raw_selection):
        raise SeedVR2PoolSelectionFormatError(
            "SeedVR2 执行账号 ID 必须是正整数"
        )
    if len(set(raw_selection)) != len(raw_selection):
        raise SeedVR2PoolSelectionFormatError("SeedVR2 执行账号 ID 不能重复")
    selected_ids = sorted(raw_selection)
    accounts = list(
        db.scalars(
            select(SeedVR2ExecutionAccount)
            .join(SeedVR2PoolMembership)
            .where(
                SeedVR2ExecutionAccount.id.in_(selected_ids),
                SeedVR2PoolMembership.user_id == user.id,
            )
            .order_by(SeedVR2ExecutionAccount.id)
        ).all()
    )
    if [account.id for account in accounts] != selected_ids:
        raise SeedVR2PoolSelectionPermissionError(
            "选择中包含不存在或无权使用的 SeedVR2 执行账号"
        )
    if any(
        not account.is_enabled or not seedvr2_account_configuration_ready(account)
        for account in accounts
    ):
        raise SeedVR2PoolSelectionUnavailableError(
            "选择中包含已停用或 SeedVR2 工作流配置不完整的执行账号"
        )
    return selected_ids


def seedvr2_batch_account_snapshot(batch: GenerationBatch) -> list[int] | None:
    if not batch.seedvr2_execution_account_ids_json:
        return None
    try:
        value = json.loads(batch.seedvr2_execution_account_ids_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SeedVR2PoolSnapshotConflictError("SeedVR2 执行账号操作快照已损坏") from exc
    if (
        not isinstance(value, list)
        or not value
        or any(type(account_id) is not int or account_id <= 0 for account_id in value)
        or len(set(value)) != len(value)
    ):
        raise SeedVR2PoolSnapshotConflictError("SeedVR2 执行账号操作快照已损坏")
    return sorted(value)


def seedvr2_item_account_snapshot(
    item: GenerationBatchItem,
) -> list[int] | None:
    if item.seedvr2_execution_account_ids_json:
        try:
            value = json.loads(item.seedvr2_execution_account_ids_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SeedVR2PoolSnapshotConflictError(
                "SeedVR2 行级执行账号快照已损坏"
            ) from exc
        if (
            not isinstance(value, list)
            or not value
            or any(type(account_id) is not int or account_id <= 0 for account_id in value)
            or len(set(value)) != len(value)
        ):
            raise SeedVR2PoolSnapshotConflictError(
                "SeedVR2 行级执行账号快照已损坏"
            )
        return sorted(value)
    return seedvr2_batch_account_snapshot(item.batch)


def bind_seedvr2_item_account_snapshot(
    db: Session,
    item: GenerationBatchItem,
    selected_account_ids: list[int],
    *,
    allow_replace: bool = False,
) -> list[int]:
    canonical_ids = sorted(selected_account_ids)
    if not canonical_ids:
        raise SeedVR2PoolSelectionFormatError("至少选择一个 SeedVR2 执行账号")
    serialized = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
    current = (
        seedvr2_item_account_snapshot(item)
        if item.seedvr2_execution_account_ids_json
        else None
    )
    if current == canonical_ids:
        return canonical_ids
    if current is not None and not allow_replace:
        raise SeedVR2PoolSnapshotConflictError(
            "该画面生成任务的 SeedVR2 执行账号快照已锁定，不能修改"
        )
    expected = item.seedvr2_execution_account_ids_json
    result = db.execute(
        update(GenerationBatchItem)
        .where(
            GenerationBatchItem.id == item.id,
            GenerationBatchItem.seedvr2_execution_account_ids_json == expected,
        )
        .values(seedvr2_execution_account_ids_json=serialized)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        item.seedvr2_execution_account_ids_json = serialized
        return canonical_ids
    db.refresh(item, attribute_names=["seedvr2_execution_account_ids_json"])
    if seedvr2_item_account_snapshot(item) != canonical_ids:
        raise SeedVR2PoolSnapshotConflictError(
            "该画面生成任务的 SeedVR2 执行账号快照已被其他请求修改"
        )
    return canonical_ids


def bind_seedvr2_batch_account_snapshot(
    db: Session, batch: GenerationBatch, selected_account_ids: list[int]
) -> list[int]:
    canonical_ids = sorted(selected_account_ids)
    if not canonical_ids:
        raise SeedVR2PoolSelectionFormatError("至少选择一个 SeedVR2 执行账号")
    serialized = json.dumps(canonical_ids, ensure_ascii=False, separators=(",", ":"))
    if batch.seedvr2_execution_account_ids_json:
        if seedvr2_batch_account_snapshot(batch) != canonical_ids:
            raise SeedVR2PoolSnapshotConflictError(
                "该画面生成操作的 SeedVR2 执行账号快照已锁定，不能修改"
            )
        return canonical_ids
    result = db.execute(
        update(GenerationBatch)
        .where(
            GenerationBatch.id == batch.id,
            GenerationBatch.seedvr2_execution_account_ids_json.is_(None),
        )
        .values(seedvr2_execution_account_ids_json=serialized)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        batch.seedvr2_execution_account_ids_json = serialized
        return canonical_ids
    db.refresh(batch, attribute_names=["seedvr2_execution_account_ids_json"])
    if seedvr2_batch_account_snapshot(batch) != canonical_ids:
        raise SeedVR2PoolSnapshotConflictError(
            "该画面生成操作的 SeedVR2 执行账号快照已锁定，不能修改"
        )
    return canonical_ids
