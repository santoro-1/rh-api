from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    RunningHubExecutionAccount,
    RunningHubCredentialBalance,
    SystemWorkflowConfig,
    SeedVR2ExecutionAccount,
    TaskStatus,
    User,
    VoiceAssetStatus,
)
from app.services.runninghub_pool import (
    RunningHubPoolSelectionFormatError,
    RunningHubPoolSelectionPermissionError,
    RunningHubPoolSelectionUnavailableError,
    RunningHubPoolSnapshotConflictError,
    batch_execution_account_snapshot,
    bind_batch_execution_account_snapshot,
    bind_item_execution_account_snapshot,
    create_execution_account,
    item_execution_account_snapshot,
    validate_workbench_execution_account_selection,
)
from app.services.runninghub_dual_pool import (
    dual_pool_runtime_enabled,
    set_dual_pool_grant,
)
from app.services.runninghub import RunningHubAccountStatus
from app.services.seedvr2_pool import create_seedvr2_execution_account
from app.services.security import decrypt_secret, encrypt_secret, secret_fingerprint
from app.services.storage import to_relative_data_path
from tests.conftest import create_user, login


def _pool_form(
    admin_user_ids: list[int],
    *,
    label: str = "RunningHub 一号",
    api_key: str = "pool-secret-key",
    enabled: bool = True,
    max_concurrent_tasks: int = 5,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "label": label,
        "api_key": api_key,
        "base_url": "https://www.runninghub.cn",
        "digital_human_ai_app_id": "pool-digital-human-app",
        "max_concurrent_tasks": str(max_concurrent_tasks),
        "admin_user_ids": [str(user_id) for user_id in admin_user_ids],
    }
    if enabled:
        payload["is_enabled"] = "true"
    return payload


def _seedvr2_pool_form(
    user_ids: list[int],
    *,
    label: str = "SeedVR2 一号",
    api_key: str = "seedvr2-secret-key",
    enabled: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "label": label,
        "api_key": api_key,
        "base_url": "https://www.runninghub.cn",
        "seedvr2_ai_app_id": "seedvr2-app-id",
        "max_concurrent_tasks": "5",
        "user_ids": [str(user_id) for user_id in user_ids],
    }
    if enabled:
        payload["is_enabled"] = "true"
    return payload


def test_admin_manages_shared_h3_workflow_password_without_exposing_it(
    client, caplog
):
    administrator = create_user("h3-pool-admin", is_admin=True)
    login(client, administrator.username)
    password = "h3-private-workflow-password"

    with caplog.at_level(logging.INFO):
        created = client.post(
            "/admin/runninghub-pool/workflows/minimax_h3_ref2va",
            data={
                "ai_app_id": "h3-workflow-id",
                "instance_type": "plus",
                "default_prompt": "由 H3 PromptProfile 自动编译",
                "access_password": password,
                "is_enabled": "true",
            },
            follow_redirects=False,
        )
    assert created.status_code == 303
    assert password not in caplog.text

    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="minimax_h3_ref2va"
        ).one()
        settings = json.loads(config.settings_json or "{}")
        original_ciphertext = settings["access_password_encrypted"]
        assert config.ai_app_id == "h3-workflow-id"
        assert config.instance_type == "plus"
        assert config.is_enabled is True
        assert config.default_prompt == "由 H3 PromptProfile 根据每段台词自动编译"
        assert original_ciphertext != password
        assert decrypt_secret(original_ciphertext) == password

    page = client.get("/admin/runninghub-pool/workflows")
    assert page.status_code == 200
    assert "已加密保存，留空不修改" in page.text
    assert "h3.prompt.ref2va.v8" in page.text
    assert "h3.prompt.ref2va.loop_anchor.v1" in page.text
    assert "此处不提供手工覆盖" in page.text
    assert password not in page.text
    assert original_ciphertext not in page.text

    preserved = client.post(
        "/admin/runninghub-pool/workflows/minimax_h3_ref2va",
        data={
            "ai_app_id": "h3-workflow-id",
            "instance_type": "plus",
            "default_prompt": "由 H3 PromptProfile 自动编译",
            "is_enabled": "true",
        },
        follow_redirects=False,
    )
    assert preserved.status_code == 303
    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="minimax_h3_ref2va"
        ).one()
        assert json.loads(config.settings_json or "{}")[
            "access_password_encrypted"
        ] == original_ciphertext

    cleared = client.post(
        "/admin/runninghub-pool/workflows/minimax_h3_ref2va",
        data={
            "ai_app_id": "h3-workflow-id",
            "instance_type": "plus",
            "default_prompt": "由 H3 PromptProfile 自动编译",
            "clear_access_password": "true",
            "is_enabled": "true",
        },
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="minimax_h3_ref2va"
        ).one()
        assert "access_password_encrypted" not in json.loads(
            config.settings_json or "{}"
        )


def test_shared_h3_workflow_requires_workflow_id(client):
    administrator = create_user("invalid-h3-pool-admin", is_admin=True)
    login(client, administrator.username)
    response = client.post(
        "/admin/runninghub-pool/workflows/minimax_h3_ref2va",
        data={
            "ai_app_id": "",
            "instance_type": "plus",
            "default_prompt": "由 H3 PromptProfile 自动编译",
            "is_enabled": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "不能为空" in response.text
    with SessionLocal() as db:
        assert db.query(SystemWorkflowConfig).count() == 0


def test_admin_refreshes_pool_account_rh_coins_without_exposing_key(
    client, monkeypatch, caplog
):
    administrator = create_user("pool-balance-admin", is_admin=True)
    login(client, administrator.username)
    secret = "pool-balance-secret"
    response = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form([administrator.id], api_key=secret),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).one()
        account_id = account.id
        fingerprint = account.credential_fingerprint

    class FakeBalanceClient:
        def __init__(self, api_key, base_url, app_id):
            assert api_key == secret

        def get_account_status(self):
            return RunningHubAccountStatus(
                current_task_count=1,
                remain_coins=Decimal("321.50"),
                remain_money=Decimal("12.30"),
                currency="CNY",
                api_type="NORMAL",
            )

    monkeypatch.setattr(
        "app.services.runninghub_balance.RunningHubClient", FakeBalanceClient
    )
    with caplog.at_level(logging.INFO):
        refreshed = client.post(
            f"/admin/runninghub-pool/accounts/{account_id}/refresh-balance",
            data={},
            follow_redirects=False,
        )
    assert refreshed.status_code == 303
    assert refreshed.headers["location"].endswith("balance_refreshed=1")
    assert secret not in caplog.text
    with SessionLocal() as db:
        balance = db.get(RunningHubCredentialBalance, fingerprint)
        assert balance is not None
        assert balance.remain_coins == "321.5"

    page = client.get("/admin/runninghub-pool")
    assert "321.5" in page.text


def _workbench_token(client, username: str) -> str:
    response = client.post(
        "/api/auth/center/login",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _task(owner_id: int, task_id: str, account_id: int, status: str) -> GenerationTask:
    return GenerationTask(
        id=task_id,
        user_id=owner_id,
        execution_account_id=account_id,
        image_path="uploads/person.png",
        audio_path="uploads/speech.mp3",
        image_original_name="person.png",
        audio_original_name="speech.mp3",
        audio_duration_seconds=10,
        start_seconds=0,
        end_seconds=10,
        prompt="测试",
        status=status,
    )


def test_admin_creates_encrypted_pool_account_and_legacy_fingerprints_are_shared(
    client,
    caplog,
):
    administrator = create_user("pool-admin", is_admin=True)
    peer = create_user("pool-peer", is_admin=True)
    login(client, administrator.username)
    secret = "test-runninghub-key"

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/admin/runninghub-pool/accounts",
            data=_pool_form([administrator.id, peer.id], api_key=secret),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/runninghub-pool?created=1"
    assert secret not in caplog.text
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).one()
        assert decrypt_secret(account.api_key_encrypted) == secret
        assert account.api_key_encrypted != secret
        assert account.credential_fingerprint == secret_fingerprint(secret)
        assert {membership.admin_user_id for membership in account.pool_memberships} == {
            administrator.id,
            peer.id,
        }
        legacy_fingerprints = {
            user.runninghub_config.credential_fingerprint
            for user in db.query(User).order_by(User.id)
        }
        assert legacy_fingerprints == {account.credential_fingerprint}

    page = client.get("/admin/runninghub-pool")
    assert page.status_code == 200
    assert "RunningHub 一号" in page.text
    assert "API Key 已加密保存" in page.text
    assert "剩余 RH 币" in page.text
    assert "刷新 RH 币" in page.text
    assert secret not in page.text
    assert 'value="pool-secret-key"' not in page.text


def test_admin_page_can_switch_new_workbench_between_same_account_and_dual_pool(
    client, caplog
):
    administrator = create_user("pool-mode-admin", is_admin=True)
    login(client, administrator.username)
    page = client.get("/admin/runninghub-pool")
    assert page.status_code == 200
    assert "新版工作台运行模式" in page.text

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/admin/runninghub-pool/runtime-mode",
            data={"dual_pool_enabled": "true"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/runninghub-pool?mode_updated=1"
    with SessionLocal() as db:
        assert dual_pool_runtime_enabled(db) is True
    assert "runninghub_pool.runtime_mode_updated" in caplog.text

    response = client.post(
        "/admin/runninghub-pool/runtime-mode", data={}, follow_redirects=False
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        assert dual_pool_runtime_enabled(db) is False


def test_admin_creates_encrypted_seedvr2_account_for_controlled_user(
    client,
    caplog,
):
    administrator = create_user("seedvr2-pool-admin", is_admin=True)
    controlled = create_user("Cx_ceshi_seedvr2_pool")
    with SessionLocal() as db:
        user = db.get(User, controlled.id)
        assert user is not None
        set_dual_pool_grant(
            db,
            user=user,
            is_enabled=True,
            allow_non_admin=True,
            note="受控测试账号",
        )
        db.commit()
    login(client, administrator.username)
    secret = "seedvr2-admin-page-secret"

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/admin/runninghub-pool/seedvr2/accounts",
            data=_seedvr2_pool_form([controlled.id], api_key=secret),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/runninghub-pool/seedvr2?created=1"
    )
    assert secret not in caplog.text
    with SessionLocal() as db:
        account = db.query(SeedVR2ExecutionAccount).one()
        assert decrypt_secret(account.api_key_encrypted) == secret
        assert account.credential_fingerprint == secret_fingerprint(secret)
        assert {member.user_id for member in account.pool_memberships} == {
            controlled.id
        }

    page = client.get("/admin/runninghub-pool/seedvr2")
    assert page.status_code == 200
    assert "SeedVR2 一号" in page.text
    assert "API Key 已加密保存" in page.text
    assert "剩余 RH 币" in page.text
    assert "刷新 RH 币" in page.text
    assert secret not in page.text


def test_seedvr2_admin_page_rejects_controlled_non_admin(client):
    controlled = create_user("Cx_ceshi_seedvr2_page")
    with SessionLocal() as db:
        user = db.get(User, controlled.id)
        assert user is not None
        set_dual_pool_grant(
            db,
            user=user,
            is_enabled=True,
            allow_non_admin=True,
        )
        db.commit()
    login(client, controlled.username)

    response = client.get("/admin/runninghub-pool/seedvr2")

    assert response.status_code == 403


def test_duplicate_pool_key_is_rejected_without_creating_fake_capacity(client):
    administrator = create_user("duplicate-pool-admin", is_admin=True)
    login(client, administrator.username)
    first = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form([administrator.id], api_key="same-real-account-key"),
        follow_redirects=False,
    )
    assert first.status_code == 303

    duplicate = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form(
            [administrator.id],
            label="伪重复容量",
            api_key=" same-real-account-key ",
        ),
        follow_redirects=False,
    )

    assert duplicate.status_code == 409
    assert "不能重复计算容量" in duplicate.text
    with SessionLocal() as db:
        assert db.query(RunningHubExecutionAccount).count() == 1


def test_pool_update_preserves_blank_key_and_accepts_active_website_users(client):
    administrator = create_user("update-pool-admin", is_admin=True)
    peer = create_user("update-pool-peer", is_admin=True)
    normal = create_user("update-pool-normal")
    login(client, administrator.username)
    created = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form([administrator.id]),
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).one()
        account_id = account.id
        original_ciphertext = account.api_key_encrypted

    updated = client.post(
        f"/admin/runninghub-pool/accounts/{account_id}",
        data=_pool_form(
            [peer.id],
            label="夜间批量账号",
            api_key="",
            enabled=False,
            max_concurrent_tasks=3,
        ),
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with SessionLocal() as db:
        account = db.get(RunningHubExecutionAccount, account_id)
        assert account is not None
        assert account.label == "夜间批量账号"
        assert account.api_key_encrypted == original_ciphertext
        assert account.max_concurrent_tasks == 3
        assert account.is_enabled is False
        assert {membership.admin_user_id for membership in account.pool_memberships} == {
            peer.id
        }

    reassigned = client.post(
        f"/admin/runninghub-pool/accounts/{account_id}",
        data=_pool_form([normal.id], label="普通用户账号", api_key=""),
        follow_redirects=False,
    )
    assert reassigned.status_code == 303
    with SessionLocal() as db:
        account = db.get(RunningHubExecutionAccount, account_id)
        assert account is not None
        assert account.label == "普通用户账号"
        assert {membership.admin_user_id for membership in account.pool_memberships} == {
            normal.id
        }


def test_pool_key_cannot_be_rotated_after_account_has_task_history(client):
    administrator = create_user("history-pool-admin", is_admin=True)
    login(client, administrator.username)
    created = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form([administrator.id], api_key="original-pool-key"),
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).one()
        account_id = account.id
        original_fingerprint = account.credential_fingerprint
        db.add(
            _task(
                administrator.id,
                "pool-history-task",
                account.id,
                TaskStatus.SUCCESS.value,
            )
        )
        db.commit()

    response = client.post(
        f"/admin/runninghub-pool/accounts/{account_id}",
        data=_pool_form(
            [administrator.id],
            api_key="replacement-pool-key",
        ),
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "不能原地更换 API Key" in response.text
    with SessionLocal() as db:
        account = db.get(RunningHubExecutionAccount, account_id)
        assert account is not None
        assert account.credential_fingerprint == original_fingerprint
        assert decrypt_secret(account.api_key_encrypted) == "original-pool-key"


def test_unused_pool_key_rotation_is_encrypted_and_safely_audited(client, caplog):
    administrator = create_user("rotation-pool-admin", is_admin=True)
    login(client, administrator.username)
    created = client.post(
        "/admin/runninghub-pool/accounts",
        data=_pool_form([administrator.id], api_key="unused-original-key"),
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).one()
        account_id = account.id
        original_ciphertext = account.api_key_encrypted

    replacement = "unused-replacement-key"
    with caplog.at_level(logging.INFO):
        response = client.post(
            f"/admin/runninghub-pool/accounts/{account_id}",
            data=_pool_form(
                [administrator.id],
                api_key=replacement,
            ),
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "runninghub_pool.account_updated" in caplog.text
    assert '"api_key"' in caplog.text
    assert replacement not in caplog.text
    with SessionLocal() as db:
        account = db.get(RunningHubExecutionAccount, account_id)
        assert account is not None
        assert account.api_key_encrypted != original_ciphertext
        assert decrypt_secret(account.api_key_encrypted) == replacement
        assert account.credential_fingerprint == secret_fingerprint(replacement)


def test_workbench_summary_is_admin_only_and_never_returns_credentials(client):
    administrator = create_user("summary-pool-admin", is_admin=True)
    peer = create_user("summary-pool-peer", is_admin=True)
    normal = create_user("summary-pool-normal")
    with SessionLocal() as db:
        admin_user = db.get(User, administrator.id)
        peer_user = db.get(User, peer.id)
        assert admin_user is not None and peer_user is not None
        available = create_execution_account(
            db,
            label="可用账号",
            api_key="summary-available-key",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="summary-app-available",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[admin_user.id],
        )
        disabled = create_execution_account(
            db,
            label="已停用账号",
            api_key="summary-disabled-key",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="summary-app-disabled",
            max_concurrent_tasks=4,
            is_enabled=False,
            admin_user_ids=[admin_user.id],
        )
        unhealthy = create_execution_account(
            db,
            label="异常账号",
            api_key="summary-unhealthy-key",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="summary-app-unhealthy",
            max_concurrent_tasks=3,
            is_enabled=True,
            admin_user_ids=[admin_user.id],
        )
        unhealthy.health_status = "UNHEALTHY"
        peer_only = create_execution_account(
            db,
            label="其他管理员账号",
            api_key="summary-peer-key",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="summary-app-peer",
            max_concurrent_tasks=2,
            is_enabled=True,
            admin_user_ids=[peer_user.id],
        )
        db.add(
            _task(
                admin_user.id,
                "pool-active-task",
                available.id,
                TaskStatus.RUNNING.value,
            )
        )
        db.commit()
        account_ids = {
            "available": available.id,
            "disabled": disabled.id,
            "unhealthy": unhealthy.id,
            "peer": peer_only.id,
        }
        forbidden_values = {
            available.api_key_encrypted,
            available.credential_fingerprint,
            available.base_url,
            available.digital_human_ai_app_id,
            "summary-available-key",
        }

    token = _workbench_token(client, administrator.username)
    response = client.post(
        "/api/workbench/runninghub-execution-accounts",
        json={"access_token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "runninghub.workbench-execution-accounts.v1"
    assert {account["id"] for account in payload["accounts"]} == {
        account_ids["available"],
        account_ids["disabled"],
        account_ids["unhealthy"],
    }
    assert payload["default_selected_account_ids"] == [
        account_ids["available"],
        account_ids["unhealthy"],
    ]
    available_payload = next(
        account
        for account in payload["accounts"]
        if account["id"] == account_ids["available"]
    )
    assert available_payload["active_tasks"] == 1
    assert available_payload["available_slots"] == 4
    assert available_payload["selectable"] is True
    disabled_payload = next(
        account
        for account in payload["accounts"]
        if account["id"] == account_ids["disabled"]
    )
    assert disabled_payload["availability"] == "DISABLED"
    unhealthy_payload = next(
        account
        for account in payload["accounts"]
        if account["id"] == account_ids["unhealthy"]
    )
    assert unhealthy_payload["availability"] == "UNHEALTHY"
    assert unhealthy_payload["selectable"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert all(value not in serialized for value in forbidden_values)
    assert "api_key" not in serialized
    assert "credential_fingerprint" not in serialized
    assert "base_url" not in serialized
    assert "digital_human_ai_app_id" not in serialized

    normal_token = _workbench_token(client, normal.username)
    forbidden = client.post(
        "/api/workbench/runninghub-execution-accounts",
        json={"access_token": normal_token},
    )
    assert forbidden.status_code == 403


def test_pool_admin_page_rejects_non_admin_user(client):
    user = create_user("pool-page-normal")
    login(client, user.username)

    response = client.get("/admin/runninghub-pool")

    assert response.status_code == 403


def test_pool_admin_can_assign_an_active_normal_website_user(client):
    administrator = create_user("pool-member-admin", is_admin=True)
    normal = create_user("ly1-style-pool-member")
    inactive = create_user("inactive-pool-member")
    with SessionLocal() as db:
        inactive_user = db.get(User, inactive.id)
        assert inactive_user is not None
        inactive_user.is_active = False
        db.commit()

    login(client, administrator.username)
    created = client.post(
        "/admin/runninghub-pool/accounts",
        data={
            "label": "普通用户可用账号",
            "api_key": "normal-member-pool-key",
            "base_url": "https://www.runninghub.cn",
            "digital_human_ai_app_id": "normal-member-app",
            "max_concurrent_tasks": "5",
            "is_enabled": "true",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    with SessionLocal() as db:
        account = db.query(RunningHubExecutionAccount).filter_by(
            label="普通用户可用账号"
        ).one()
        account_id = account.id

    edit_page = client.get(f"/admin/users/{normal.id}")
    assert edit_page.status_code == 200
    assert "普通用户可用账号" in edit_page.text
    assigned = client.post(
        f"/admin/users/{normal.id}",
        data={
            "username": normal.username,
            "is_active": "true",
            "runninghub_execution_account_ids": str(account_id),
            "base_url": "https://www.runninghub.cn",
            "ai_app_id": "2062251097452007426",
            "instance_type": "plus",
            "default_prompt": "测试提示词",
            "max_concurrent_tasks": "2",
        },
        follow_redirects=False,
    )
    assert assigned.status_code == 303
    with SessionLocal() as db:
        assigned_user = db.get(User, normal.id)
        assert [
            membership.execution_account_id
            for membership in assigned_user.runninghub_pool_memberships
        ] == [account_id]


def test_admin_selection_is_revalidated_and_saved_as_immutable_batch_snapshot():
    administrator = create_user("selection-pool-admin", is_admin=True)
    peer = create_user("selection-pool-peer", is_admin=True)
    normal = create_user("selection-pool-normal")
    with SessionLocal() as db:
        available = create_execution_account(
            db,
            label="选择账号一",
            api_key="selection-key-one",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="selection-app-one",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        disabled = create_execution_account(
            db,
            label="停用选择账号",
            api_key="selection-key-disabled",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="selection-app-disabled",
            max_concurrent_tasks=5,
            is_enabled=False,
            admin_user_ids=[administrator.id],
        )
        peer_only = create_execution_account(
            db,
            label="其他管理员选择账号",
            api_key="selection-key-peer",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="selection-app-peer",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[peer.id],
        )
        batch = GenerationBatch(
            id="selection-batch",
            user_id=administrator.id,
            name="选择快照批次",
            workflow_type="digital_human",
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            request_key="selection-request",
            total_items=0,
        )
        db.add(batch)
        db.commit()
        available_id = available.id
        disabled_id = disabled.id
        peer_only_id = peer_only.id

    with SessionLocal() as db:
        admin_user = db.get(User, administrator.id)
        normal_user = db.get(User, normal.id)
        batch = db.get(GenerationBatch, "selection-batch")
        assert admin_user is not None and normal_user is not None and batch is not None
        with pytest.raises(RunningHubPoolSelectionFormatError):
            validate_workbench_execution_account_selection(
                db,
                admin_user,
                selection_provided=False,
                raw_selection=None,
            )
        for invalid in ([], [available_id, available_id], [str(available_id)], [True]):
            with pytest.raises(RunningHubPoolSelectionFormatError):
                validate_workbench_execution_account_selection(
                    db,
                    admin_user,
                    selection_provided=True,
                    raw_selection=invalid,
                )
        with pytest.raises(RunningHubPoolSelectionPermissionError):
            validate_workbench_execution_account_selection(
                db,
                admin_user,
                selection_provided=True,
                raw_selection=[peer_only_id],
            )
        with pytest.raises(RunningHubPoolSelectionUnavailableError):
            validate_workbench_execution_account_selection(
                db,
                admin_user,
                selection_provided=True,
                raw_selection=[disabled_id],
            )
        assert (
            validate_workbench_execution_account_selection(
                db,
                normal_user,
                selection_provided=False,
                raw_selection=None,
            )
            is None
        )
        with pytest.raises(RunningHubPoolSelectionPermissionError):
            validate_workbench_execution_account_selection(
                db,
                normal_user,
                selection_provided=True,
                raw_selection=[available_id],
            )

        selected = validate_workbench_execution_account_selection(
            db,
            admin_user,
            selection_provided=True,
            raw_selection=[available_id],
        )
        assert selected == [available_id]
        assert bind_batch_execution_account_snapshot(db, batch, selected) == [available_id]
        db.commit()

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, "selection-batch")
        assert batch is not None
        assert batch_execution_account_snapshot(batch) == [available_id]
        assert bind_batch_execution_account_snapshot(db, batch, [available_id]) == [
            available_id
        ]
        with pytest.raises(RunningHubPoolSnapshotConflictError):
            bind_batch_execution_account_snapshot(db, batch, [peer_only_id])


def test_batch_snapshot_compare_and_set_rejects_stale_competing_selection():
    administrator = create_user("snapshot-race-admin", is_admin=True)
    with SessionLocal() as db:
        first = create_execution_account(
            db,
            label="竞态账号一",
            api_key="snapshot-race-key-one",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="snapshot-race-app-one",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        second = create_execution_account(
            db,
            label="竞态账号二",
            api_key="snapshot-race-key-two",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="snapshot-race-app-two",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        db.add(
            GenerationBatch(
                id="snapshot-race-batch",
                user_id=administrator.id,
                name="竞态快照批次",
                workflow_type="digital_human",
                source_channel=BATCH_SOURCE_NEW_WORKBENCH,
                request_key="snapshot-race-request",
                total_items=0,
            )
        )
        db.commit()
        first_id, second_id = first.id, second.id

    first_session = SessionLocal()
    second_session = SessionLocal()
    try:
        first_batch = first_session.get(GenerationBatch, "snapshot-race-batch")
        second_batch = second_session.get(GenerationBatch, "snapshot-race-batch")
        assert first_batch is not None and second_batch is not None
        bind_batch_execution_account_snapshot(first_session, first_batch, [first_id])
        first_session.commit()
        with pytest.raises(RunningHubPoolSnapshotConflictError):
            bind_batch_execution_account_snapshot(
                second_session,
                second_batch,
                [second_id],
            )
        second_session.rollback()
    finally:
        first_session.close()
        second_session.close()

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, "snapshot-race-batch")
        assert batch is not None
        assert batch_execution_account_snapshot(batch) == [first_id]


def test_unpaid_item_can_replace_legacy_batch_account_snapshot():
    administrator = create_user("item-snapshot-admin", is_admin=True)
    with SessionLocal() as db:
        batch = GenerationBatch(
            id="item-snapshot-batch",
            user_id=administrator.id,
            name="行级账号快照",
            workflow_type="digital_human",
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            request_key="item-snapshot-request",
            total_items=1,
            runninghub_execution_account_ids_json="[3]",
        )
        item = GenerationBatchItem(
            id="item-snapshot-item",
            batch=batch,
            row_number=1,
            row_key="001",
            manifest_json="{}",
            runninghub_execution_account_ids_json="[3]",
        )
        db.add(batch)
        db.flush()

        assert bind_item_execution_account_snapshot(
            db, item, [3, 5], allow_replace=True
        ) == [3, 5]
        db.commit()
        assert item_execution_account_snapshot(item) == [3, 5]
        assert batch_execution_account_snapshot(batch) == [3]
        with pytest.raises(RunningHubPoolSnapshotConflictError):
            bind_item_execution_account_snapshot(db, item, [3])


def test_admin_composition_route_revalidates_ids_and_locks_snapshot(client):
    administrator = create_user("composition-selection-admin", is_admin=True)
    peer = create_user("composition-selection-peer", is_admin=True)
    minimax_key = "composition-selection-minimax-key"
    with SessionLocal() as db:
        user = db.get(User, administrator.id)
        assert user is not None
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret(minimax_key),
            credential_fingerprint=secret_fingerprint(minimax_key),
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id=str(uuid.uuid4()),
            user_id=user.id,
            config_id=config.id,
            name="资源池测试声音",
            voice_id="PoolSelectionVoice01",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint or "",
            status=VoiceAssetStatus.ACTIVE.value,
            method="system",
            category="中文普通话",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        voice_id = voice.id

    token = _workbench_token(client, administrator.username)
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "资源池快照声音批次",
            "request_key": "composition-selection-audio",
            "correlation_id": "composition-selection-correlation",
            "rows": [
                {
                    "row_id": "1",
                    "speech_script": "管理员资源池快照测试。",
                    "prompt": "人物自然说话",
                }
            ],
            "speech_options": {
                "voiceAssetId": voice_id,
                "model": "speech-2.8-hd",
                "speed": 1,
                "volume": 1,
                "pitch": 0,
                "languageBoost": "Chinese",
                "outputFormat": "mp3",
                "costConfirmed": True,
            },
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    item_id = created.json()["items"][0]["item_id"]
    settings = get_settings()
    with SessionLocal() as db:
        audio_task = db.query(AudioGenerationTask).one()
        output = settings.outputs_dir / "pool-selection-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3pool-selection")
        subtitles = settings.outputs_dir / "pool-selection-audio.json"
        subtitles.write_text(
            json.dumps(
                [{"text": "测试。", "start_seconds": 0, "end_seconds": 1}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        audio_task.output_path = to_relative_data_path(output, settings)
        audio_task.subtitle_path = to_relative_data_path(subtitles, settings)
        audio_task.status = AudioTaskStatus.AWAITING_REVIEW.value
        audio_task.batch_item.audio_status = "AWAITING_REVIEW"
        audio_task.batch_item.status = "AWAITING_AUDIO_REVIEW"
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=audio_task.id,
                version=audio_task.generation_version,
                output_path=audio_task.output_path,
                subtitle_path=audio_task.subtitle_path,
                status="READY",
            )
        )
        first = create_execution_account(
            db,
            label="4A 账号一",
            api_key="composition-selection-key-one",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="composition-selection-app-one",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        second = create_execution_account(
            db,
            label="4A 账号二",
            api_key="composition-selection-key-two",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="composition-selection-app-two",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        forbidden = create_execution_account(
            db,
            label="其他管理员 4A 账号",
            api_key="composition-selection-key-forbidden",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="composition-selection-app-forbidden",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[peer.id],
        )
        disabled = create_execution_account(
            db,
            label="停用 4A 账号",
            api_key="composition-selection-key-disabled",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="composition-selection-app-disabled",
            max_concurrent_tasks=5,
            is_enabled=False,
            admin_user_ids=[administrator.id],
        )
        db.commit()
        account_ids = [first.id, second.id]
        forbidden_id = forbidden.id
        disabled_id = disabled.id

    staged = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "image"},
        files={"file": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
    )
    assert staged.status_code == 201, staged.text
    staged_id = staged.json()["asset_id"]
    endpoint = f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition"
    base_payload = {
        "access_token": token,
        "idempotency_key": "composition-selection-operation",
        "cost_confirmed": True,
        "image_asset_id": staged_id,
        "correlation_id": "composition-selection-correlation",
    }

    missing = client.post(endpoint, json=base_payload)
    assert missing.status_code == 422
    forged = client.post(
        endpoint,
        json={**base_payload, "runninghub_execution_account_ids": [forbidden_id]},
    )
    assert forged.status_code == 403
    unavailable = client.post(
        endpoint,
        json={**base_payload, "runninghub_execution_account_ids": [disabled_id]},
    )
    assert unavailable.status_code == 409
    started = client.post(
        endpoint,
        json={
            **base_payload,
            "runninghub_execution_account_ids": list(reversed(account_ids)),
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["composition"]["runninghub_execution_account_ids"] == sorted(
        account_ids
    )
    repeated = client.post(
        endpoint,
        json={**base_payload, "runninghub_execution_account_ids": account_ids},
    )
    assert repeated.status_code == 200, repeated.text
    changed = client.post(
        endpoint,
        json={**base_payload, "runninghub_execution_account_ids": [account_ids[0]]},
    )
    assert changed.status_code == 409
    assert "快照已锁定" in changed.text

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, batch_id)
        audio_task = db.query(AudioGenerationTask).one()
        assert batch is not None
        assert batch_execution_account_snapshot(batch) == sorted(account_ids)
        assert audio_task.reviewed_at is not None
        assert audio_task.status == AudioTaskStatus.PENDING.value


def test_dual_pool_composition_locks_mode_and_both_stage_snapshots(
    client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.runninghub_dual_pool.get_settings",
        lambda: SimpleNamespace(runninghub_dual_pool_enabled=True),
    )
    administrator = create_user("dual-composition-admin", is_admin=True)
    minimax_key = "dual-composition-minimax-key"
    with SessionLocal() as db:
        user = db.get(User, administrator.id)
        assert user is not None
        set_dual_pool_grant(db, user=user, is_enabled=True)
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret(minimax_key),
            credential_fingerprint=secret_fingerprint(minimax_key),
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id=str(uuid.uuid4()),
            user_id=user.id,
            config_id=config.id,
            name="双池测试声音",
            voice_id="DualPoolVoice01",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint or "",
            status=VoiceAssetStatus.ACTIVE.value,
            method="system",
            category="中文普通话",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        voice_id = voice.id

    token = _workbench_token(client, administrator.username)
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "双池快照声音批次",
            "request_key": "dual-composition-audio",
            "correlation_id": "dual-composition-correlation",
            "rows": [
                {
                    "row_id": "1",
                    "speech_script": "双池快照测试。",
                    "prompt": "人物自然说话",
                }
            ],
            "speech_options": {
                "voiceAssetId": voice_id,
                "model": "speech-2.8-hd",
                "speed": 1,
                "volume": 1,
                "pitch": 0,
                "languageBoost": "Chinese",
                "outputFormat": "mp3",
                "costConfirmed": True,
            },
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    item_id = created.json()["items"][0]["item_id"]
    settings = get_settings()
    with SessionLocal() as db:
        audio_task = db.query(AudioGenerationTask).one()
        output = settings.outputs_dir / "dual-composition-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3dual-composition")
        subtitles = settings.outputs_dir / "dual-composition-audio.json"
        subtitles.write_text(
            json.dumps(
                [{"text": "测试。", "start_seconds": 0, "end_seconds": 1}],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        audio_task.output_path = to_relative_data_path(output, settings)
        audio_task.subtitle_path = to_relative_data_path(subtitles, settings)
        audio_task.status = AudioTaskStatus.AWAITING_REVIEW.value
        audio_task.batch_item.audio_status = "AWAITING_REVIEW"
        audio_task.batch_item.status = "AWAITING_AUDIO_REVIEW"
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=audio_task.id,
                version=audio_task.generation_version,
                output_path=audio_task.output_path,
                subtitle_path=audio_task.subtitle_path,
                status="READY",
            )
        )
        digital = create_execution_account(
            db,
            label="双池数字人账号",
            api_key="dual-composition-digital-key",
            base_url="https://runninghub.example.test",
            digital_human_ai_app_id="dual-digital-app",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[administrator.id],
        )
        first_seed = create_seedvr2_execution_account(
            db,
            label="双池放大账号一",
            api_key="dual-composition-seed-key-one",
            base_url="https://runninghub.example.test",
            seedvr2_ai_app_id="dual-seed-app-one",
            max_concurrent_tasks=5,
            is_enabled=True,
            user_ids=[administrator.id],
        )
        second_seed = create_seedvr2_execution_account(
            db,
            label="双池放大账号二",
            api_key="dual-composition-seed-key-two",
            base_url="https://runninghub.example.test",
            seedvr2_ai_app_id="dual-seed-app-two",
            max_concurrent_tasks=5,
            is_enabled=True,
            user_ids=[administrator.id],
        )
        db.commit()
        digital_ids = [digital.id]
        seed_ids = sorted([first_seed.id, second_seed.id])

    staged = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": "image"},
        files={"file": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
    )
    assert staged.status_code == 201, staged.text
    endpoint = f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/composition"
    base_payload = {
        "access_token": token,
        "idempotency_key": "dual-composition-operation",
        "cost_confirmed": True,
        "image_asset_id": staged.json()["asset_id"],
        "correlation_id": "dual-composition-correlation",
        "runninghub_execution_account_ids": digital_ids,
    }
    missing_seed = client.post(endpoint, json=base_payload)
    assert missing_seed.status_code == 422

    started = client.post(
        endpoint,
        json={**base_payload, "seedvr2_execution_account_ids": seed_ids},
    )
    assert started.status_code == 200, started.text
    composition = started.json()["composition"]
    assert composition["execution_mode"] == "dual_pool_v1"
    assert composition["runninghub_execution_account_ids"] == digital_ids
    assert composition["seedvr2_execution_account_ids"] == seed_ids

    # Once the first row locks the batch, a later switch change cannot migrate
    # remaining rows or retries back to the current same-account branch.
    monkeypatch.setattr(
        "app.services.runninghub_dual_pool.get_settings",
        lambda: SimpleNamespace(runninghub_dual_pool_enabled=False),
    )
    repeated = client.post(
        endpoint,
        json={**base_payload, "seedvr2_execution_account_ids": seed_ids},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["composition"]["execution_mode"] == "dual_pool_v1"

    changed = client.post(
        endpoint,
        json={**base_payload, "seedvr2_execution_account_ids": [seed_ids[0]]},
    )
    assert changed.status_code == 409
    assert "快照已锁定" in changed.text
