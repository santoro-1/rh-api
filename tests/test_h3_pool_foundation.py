from __future__ import annotations

import pytest

from app.database import SessionLocal
from app.models import RunningHubExecutionAccount, RunningHubPoolMembership, User
from app.services.h3_pool import (
    H3PoolValidationError,
    configure_h3_capability,
    h3_capability_ready,
    h3_capability_snapshots_for_user,
    h3_execution_account_summary,
)
from app.services.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    secret_fingerprint,
)


def _user(username: str) -> User:
    return User(
        username=username,
        password_hash=hash_password("password123"),
        is_admin=True,
        is_active=True,
        h3_access_enabled=True,
    )


def _account(key: str, label: str) -> RunningHubExecutionAccount:
    return RunningHubExecutionAccount(
        label=label,
        api_key_encrypted=encrypt_secret(key),
        credential_fingerprint=secret_fingerprint(key),
        base_url="https://runninghub.example",
        digital_human_ai_app_id="digital-human-only",
        max_concurrent_tasks=5,
        is_enabled=True,
    )


def test_h3_capability_is_separate_from_digital_human_workflow_id() -> None:
    with SessionLocal() as db:
        user = _user("h3-admin")
        account = _account("h3-pool-key", "H3 一号")
        db.add_all([user, account])
        db.flush()
        db.add(RunningHubPoolMembership(execution_account=account, admin_user=user))
        capability = configure_h3_capability(
            account,
            workflow_id="h3-raw-workflow-id",
            instance_type="plus",
            max_concurrent_tasks=3,
            safe_note="已验证 Ref2VA",
            access_password="private-h3-password",
            is_enabled=True,
        )
        db.add(capability)
        db.commit()

        assert account.digital_human_ai_app_id == "digital-human-only"
        assert account.h3_capability.workflow_id == "h3-raw-workflow-id"
        assert account.h3_capability.access_password_encrypted != "private-h3-password"
        assert decrypt_secret(account.h3_capability.access_password_encrypted) == (
            "private-h3-password"
        )
        assert h3_capability_ready(account) is True


def test_h3_capability_requires_workflow_id_only_when_enabled() -> None:
    account = _account("disabled-h3-key", "停用 H3")
    disabled = configure_h3_capability(
        account,
        workflow_id="",
        instance_type="plus",
        max_concurrent_tasks=5,
        is_enabled=False,
    )
    assert disabled.is_enabled is False

    with pytest.raises(H3PoolValidationError, match="必须配置 workflowId"):
        configure_h3_capability(
            account,
            workflow_id="",
            instance_type="plus",
            max_concurrent_tasks=5,
            is_enabled=True,
        )


def test_h3_capability_blank_password_clears_and_none_preserves() -> None:
    account = _account("password-h3-key", "密码轮换")
    capability = configure_h3_capability(
        account,
        workflow_id="h3-private-id",
        instance_type="plus",
        max_concurrent_tasks=2,
        access_password="initial-password",
        is_enabled=True,
    )
    encrypted = capability.access_password_encrypted

    configure_h3_capability(
        account,
        workflow_id="h3-private-id",
        instance_type="plus",
        max_concurrent_tasks=2,
        access_password=None,
        is_enabled=True,
    )
    assert capability.access_password_encrypted == encrypted

    configure_h3_capability(
        account,
        workflow_id="h3-private-id",
        instance_type="plus",
        max_concurrent_tasks=2,
        access_password="",
        is_enabled=True,
    )
    assert capability.access_password_encrypted is None


def test_h3_account_snapshot_is_membership_scoped_and_contains_no_key() -> None:
    with SessionLocal() as db:
        user = _user("allowed-h3-user")
        peer = _user("peer-h3-user")
        allowed = _account("allowed-key", "允许账号")
        forbidden = _account("forbidden-key", "其他账号")
        db.add_all([user, peer, allowed, forbidden])
        db.flush()
        db.add_all(
            [
                RunningHubPoolMembership(execution_account=allowed, admin_user=user),
                RunningHubPoolMembership(execution_account=forbidden, admin_user=peer),
            ]
        )
        for account, workflow_id in (
            (allowed, "allowed-h3-id"),
            (forbidden, "forbidden-h3-id"),
        ):
            db.add(
                configure_h3_capability(
                    account,
                    workflow_id=workflow_id,
                    instance_type="default",
                    max_concurrent_tasks=2,
                    access_password=(
                        "allowed-private-password"
                        if account is allowed
                        else "forbidden-private-password"
                    ),
                    is_enabled=True,
                )
            )
        db.commit()

        snapshots = h3_capability_snapshots_for_user(db, user)

        assert [snapshot.label for snapshot in snapshots] == ["允许账号"]
        assert snapshots[0].has_access_password is True
        serialized = repr(snapshots)
        assert "allowed-key" not in serialized
        assert "api_key" not in serialized
        assert "allowed-private-password" not in serialized
        assert "forbidden-private-password" not in serialized


def test_user_management_h3_grant_is_independent_from_legacy_dual_pool_grant() -> None:
    with SessionLocal() as db:
        user = _user("h3-admin-without-dual-grant")
        account = _account("admin-h3-key", "管理员 H3 账号")
        db.add_all([user, account])
        db.flush()
        db.add(RunningHubPoolMembership(execution_account=account, admin_user=user))
        db.add(
            configure_h3_capability(
                account,
                workflow_id="admin-h3-workflow",
                instance_type="plus",
                max_concurrent_tasks=1,
                is_enabled=True,
            )
        )
        db.commit()

        summary = h3_execution_account_summary(db, user)

        assert summary["default_selected_account_ids"] == [account.id]
        assert summary["accounts"][0]["selectable"] is True
        assert summary["accounts"][0]["availability"] == "AVAILABLE"


def test_active_admin_without_user_management_h3_grant_is_rejected() -> None:
    with SessionLocal() as db:
        user = _user("h3-admin-disabled-in-user-management")
        user.h3_access_enabled = False
        db.add(user)
        db.commit()

        with pytest.raises(H3PoolValidationError, match="尚未开通 H3"):
            h3_execution_account_summary(db, user)
