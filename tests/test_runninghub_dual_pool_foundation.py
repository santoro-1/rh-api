from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationTask,
    GenerationTaskEnhancement,
    EnhancementStatus,
    RunningHubExecutionAccount,
    RunningHubConfig,
    User,
    TaskStatus,
)
from app.services.runninghub_dual_pool import (
    RunningHubExecutionModeConflictError,
    batch_execution_mode,
    bind_batch_execution_mode,
    resolve_execution_mode,
    set_dual_pool_grant,
)
from app.services.runninghub_pool import (
    DuplicateRunningHubCredentialError,
    create_execution_account,
    credential_active_task_count,
)
from app.services.seedvr2_dispatch import reserve_seedvr2_account
from app.services.security import decrypt_secret, encrypt_secret, hash_password, secret_fingerprint
from app.services.seedvr2_pool import (
    DuplicateSeedVR2CredentialError,
    SeedVR2PoolSnapshotConflictError,
    bind_seedvr2_batch_account_snapshot,
    create_seedvr2_execution_account,
    seedvr2_batch_account_snapshot,
    validate_seedvr2_account_selection,
)


def _user(db, username: str, *, is_admin: bool) -> User:
    user = User(
        username=username,
        password_hash=hash_password("password123"),
        is_admin=is_admin,
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user


def _batch(db, user: User, request_key: str = "dual-pool-request") -> GenerationBatch:
    batch = GenerationBatch(
        id=f"batch-{request_key}",
        user_id=user.id,
        name="双池测试",
        workflow_type="digital_human",
        source_channel=BATCH_SOURCE_NEW_WORKBENCH,
        audio_mode="upload",
        request_key=request_key,
        status="ACTIVE",
        total_items=0,
    )
    db.add(batch)
    db.flush()
    return batch


def test_dual_pool_feature_flag_defaults_off_and_accepts_explicit_true(monkeypatch):
    monkeypatch.delenv("RUNNINGHUB_DUAL_POOL_ENABLED", raising=False)
    assert Settings.from_environment().runninghub_dual_pool_enabled is False

    monkeypatch.setenv("RUNNINGHUB_DUAL_POOL_ENABLED", "true")
    assert Settings.from_environment().runninghub_dual_pool_enabled is True


def test_execution_mode_requires_server_switch_entitlement_and_supported_scope():
    with SessionLocal() as db:
        cx_user = _user(db, "Cx_ceshi", is_admin=False)
        ungranted_admin = _user(db, "ungranted-admin", is_admin=True)
        set_dual_pool_grant(
            db,
            user=cx_user,
            is_enabled=True,
            allow_non_admin=True,
            note="受控测试账号",
        )

        assert resolve_execution_mode(
            db,
            user=cx_user,
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            workflow_type="digital_human",
            dual_pool_enabled=False,
        ) == BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
        assert resolve_execution_mode(
            db,
            user=cx_user,
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            workflow_type="digital_human",
            dual_pool_enabled=True,
        ) == BATCH_EXECUTION_MODE_DUAL_POOL_V1
        assert resolve_execution_mode(
            db,
            user=ungranted_admin,
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            workflow_type="digital_human",
            dual_pool_enabled=True,
        ) == BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
        assert resolve_execution_mode(
            db,
            user=cx_user,
            source_channel="legacy_web",
            workflow_type="digital_human",
            dual_pool_enabled=True,
        ) == BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
        assert resolve_execution_mode(
            db,
            user=cx_user,
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            workflow_type="ltx_lip_sync",
            dual_pool_enabled=True,
        ) == BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1


def test_batch_execution_mode_is_atomic_and_historical_null_remains_same_account():
    with SessionLocal() as db:
        user = _user(db, "mode-admin", is_admin=True)
        batch = _batch(db, user)

        assert batch_execution_mode(batch) == BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
        assert (
            bind_batch_execution_mode(db, batch, BATCH_EXECUTION_MODE_DUAL_POOL_V1)
            == BATCH_EXECUTION_MODE_DUAL_POOL_V1
        )
        assert batch_execution_mode(batch) == BATCH_EXECUTION_MODE_DUAL_POOL_V1
        with pytest.raises(RunningHubExecutionModeConflictError):
            bind_batch_execution_mode(
                db, batch, BATCH_EXECUTION_MODE_SAME_ACCOUNT_V1
            )


def test_seedvr2_pool_encrypts_key_allows_controlled_user_and_locks_snapshot():
    secret = "seedvr2-dedicated-secret"
    with SessionLocal() as db:
        cx_user = _user(db, "Cx_ceshi", is_admin=False)
        set_dual_pool_grant(
            db,
            user=cx_user,
            is_enabled=True,
            allow_non_admin=True,
            note="受控测试账号",
        )
        account = create_seedvr2_execution_account(
            db,
            label="SeedVR2 一号",
            api_key=secret,
            base_url="https://www.runninghub.cn/",
            seedvr2_ai_app_id="seedvr2-app-1",
            max_concurrent_tasks=5,
            is_enabled=True,
            user_ids=[cx_user.id],
        )
        batch = _batch(db, cx_user, "seed-snapshot")

        assert account.api_key_encrypted != secret
        assert decrypt_secret(account.api_key_encrypted) == secret
        assert account.credential_fingerprint == secret_fingerprint(secret)
        assert validate_seedvr2_account_selection(
            db, user=cx_user, raw_selection=[account.id]
        ) == [account.id]
        assert bind_seedvr2_batch_account_snapshot(db, batch, [account.id]) == [account.id]
        assert seedvr2_batch_account_snapshot(batch) == [account.id]
        with pytest.raises(SeedVR2PoolSnapshotConflictError):
            bind_seedvr2_batch_account_snapshot(db, batch, [account.id + 1])


def test_controlled_user_receives_only_safe_dual_pool_summaries(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.runninghub_dual_pool.get_settings",
        lambda: SimpleNamespace(runninghub_dual_pool_enabled=True),
    )
    digital_secret = "controlled-digital-secret"
    seed_secret = "controlled-seed-secret"
    with SessionLocal() as db:
        user = _user(db, "controlled-dual-user", is_admin=False)
        set_dual_pool_grant(
            db,
            user=user,
            is_enabled=True,
            allow_non_admin=True,
            note="受控测试",
        )
        digital = create_execution_account(
            db,
            label="受控数字人账号",
            api_key=digital_secret,
            base_url="https://digital.example",
            digital_human_ai_app_id="digital-app",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[user.id],
        )
        seed = create_seedvr2_execution_account(
            db,
            label="受控放大账号",
            api_key=seed_secret,
            base_url="https://seed.example",
            seedvr2_ai_app_id="seed-app",
            max_concurrent_tasks=5,
            is_enabled=True,
            user_ids=[user.id],
        )
        db.commit()
        digital_id = digital.id
        seed_id = seed.id

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "controlled-dual-user", "password": "password123"},
    )
    assert login_response.status_code == 200
    response = client.post(
        "/api/workbench/runninghub-dual-pool-accounts",
        json={"access_token": login_response.json()["access_token"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_mode"] == BATCH_EXECUTION_MODE_DUAL_POOL_V1
    assert payload["digital_human"]["default_selected_account_ids"] == [digital_id]
    assert payload["seedvr2"]["default_selected_account_ids"] == [seed_id]
    serialized = response.text
    for forbidden in (
        digital_secret,
        seed_secret,
        "credential_fingerprint",
        "api_key",
        "base_url",
        "digital_human_ai_app_id",
        "seedvr2_ai_app_id",
    ):
        assert forbidden not in serialized


def test_same_real_key_is_rejected_across_digital_human_and_seedvr2_pools():
    shared_key = "same-real-runninghub-key"
    with SessionLocal() as db:
        admin = _user(db, "pool-admin", is_admin=True)
        set_dual_pool_grant(db, user=admin, is_enabled=True)
        seed_account = create_seedvr2_execution_account(
            db,
            label="SeedVR2 池账号",
            api_key=shared_key,
            base_url="https://www.runninghub.cn",
            seedvr2_ai_app_id="seed-app",
            max_concurrent_tasks=5,
            is_enabled=True,
            user_ids=[admin.id],
        )
        with pytest.raises(DuplicateRunningHubCredentialError):
            create_execution_account(
                db,
                label="数字人重复账号",
                api_key=shared_key,
                base_url="https://www.runninghub.cn",
                digital_human_ai_app_id="digital-app",
                max_concurrent_tasks=5,
                is_enabled=True,
                admin_user_ids=[admin.id],
            )

        db.delete(seed_account)
        db.flush()
        digital_account = create_execution_account(
            db,
            label="数字人池账号",
            api_key=shared_key,
            base_url="https://www.runninghub.cn",
            digital_human_ai_app_id="digital-app",
            max_concurrent_tasks=5,
            is_enabled=True,
            admin_user_ids=[admin.id],
        )
        assert isinstance(digital_account, RunningHubExecutionAccount)
        with pytest.raises(DuplicateSeedVR2CredentialError):
            create_seedvr2_execution_account(
                db,
                label="SeedVR2 重复账号",
                api_key=shared_key,
                base_url="https://www.runninghub.cn",
                seedvr2_ai_app_id="seed-app",
                max_concurrent_tasks=5,
                is_enabled=True,
                user_ids=[admin.id],
            )


def test_seedvr2_pool_rejects_key_already_used_by_legacy_single_account():
    shared_key = "legacy-and-seedvr2-key"
    with SessionLocal() as db:
        user = _user(db, "legacy-key-user", is_admin=True)
        set_dual_pool_grant(db, user=user, is_enabled=True)
        db.add(
            RunningHubConfig(
                user_id=user.id,
                api_key_encrypted=encrypt_secret(shared_key),
                credential_fingerprint=None,
                base_url="https://rh.example",
                ai_app_id="legacy-app",
                max_concurrent_tasks=5,
            )
        )
        db.flush()
        with pytest.raises(DuplicateSeedVR2CredentialError):
            create_seedvr2_execution_account(
                db,
                label="重复 SeedVR2 账号",
                api_key=shared_key,
                base_url="https://rh.example",
                seedvr2_ai_app_id="seed-app",
                max_concurrent_tasks=5,
                is_enabled=True,
                user_ids=[user.id],
            )


def test_dual_pool_releases_digital_slot_and_reserves_seedvr2_independently():
    with SessionLocal() as db:
        user = _user(db, "dual-stage-admin", is_admin=True)
        set_dual_pool_grant(db, user=user, is_enabled=True)
        digital = create_execution_account(
            db, label="数字人专用", api_key="digital-stage-key",
            base_url="https://rh.example", digital_human_ai_app_id="digital-app",
            max_concurrent_tasks=1, is_enabled=True, admin_user_ids=[user.id],
        )
        seed = create_seedvr2_execution_account(
            db, label="放大专用", api_key="seed-stage-key",
            base_url="https://rh.example", seedvr2_ai_app_id="seed-app",
            max_concurrent_tasks=1, is_enabled=True, user_ids=[user.id],
        )
        batch = _batch(db, user, "stage-dispatch")
        batch.execution_mode = BATCH_EXECUTION_MODE_DUAL_POOL_V1
        batch.runninghub_execution_account_ids_json = f"[{digital.id}]"
        batch.seedvr2_execution_account_ids_json = f"[{seed.id}]"
        item = GenerationBatchItem(
            id="dual-stage-item", batch=batch, row_number=1, row_key="1",
            manifest_json="{}",
        )
        task = GenerationTask(
            id="dual-stage-task", user_id=user.id, batch_item=item,
            workflow_type="digital_human", status=TaskStatus.RUNNING.value,
            execution_account_id=digital.id, prompt="test",
            image_path="uploads/image.png", audio_path="uploads/audio.mp3",
            image_original_name="image.png", audio_original_name="audio.mp3",
            audio_duration_seconds=1, start_seconds=0, end_seconds=1,
        )
        enhancement = GenerationTaskEnhancement(
            id="dual-stage-enhancement", task=task,
            status=EnhancementStatus.PENDING.value,
            source_result_path="outputs/source.mp4", source_filename="source.mp4",
            execution_account_id=digital.id,
        )
        db.add_all([item, task, enhancement])
        db.flush()

        assert credential_active_task_count(db, digital.credential_fingerprint) == 0
        selected = reserve_seedvr2_account(db, task, enhancement)
        assert selected is not None and selected.id == seed.id
        assert enhancement.execution_account_id == digital.id
        assert enhancement.seedvr2_execution_account_id == seed.id


def test_seedvr2_capacity_is_independent_and_normal_retry_keeps_original_account():
    from app.workers import task_worker

    with SessionLocal() as db:
        user = _user(db, "dual-retry-admin", is_admin=True)
        set_dual_pool_grant(db, user=user, is_enabled=True)
        seed = create_seedvr2_execution_account(
            db, label="Seed 单并发", api_key="seed-retry-key",
            base_url="https://rh.example", seedvr2_ai_app_id="seed-app",
            max_concurrent_tasks=1, is_enabled=True, user_ids=[user.id],
        )
        batch = _batch(db, user, "seed-retry")
        batch.execution_mode = BATCH_EXECUTION_MODE_DUAL_POOL_V1
        batch.seedvr2_execution_account_ids_json = f"[{seed.id}]"
        enhancements = []
        for index in (1, 2):
            item = GenerationBatchItem(
                id=f"seed-retry-item-{index}", batch=batch, row_number=index,
                row_key=str(index), manifest_json="{}",
            )
            task = GenerationTask(
                id=f"seed-retry-task-{index}", user_id=user.id, batch_item=item,
                workflow_type="digital_human", status=TaskStatus.RUNNING.value,
                prompt="test", image_path="uploads/image.png",
                audio_path="uploads/audio.mp3", image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=1, start_seconds=0, end_seconds=1,
            )
            enhancement = GenerationTaskEnhancement(
                id=f"seed-retry-enhancement-{index}", task=task,
                status=EnhancementStatus.PENDING.value,
                source_result_path="outputs/source.mp4", source_filename="source.mp4",
            )
            db.add_all([item, task, enhancement])
            enhancements.append((task, enhancement))
        db.flush()
        first_task, first = enhancements[0]
        second_task, second = enhancements[1]
        assert reserve_seedvr2_account(db, first_task, first).id == seed.id
        assert reserve_seedvr2_account(db, second_task, second) is None

        task_worker._new_enhancement_attempt(first)
        scheduled = task_worker._schedule_enhancement_retry(
            first_task, first, message="普通工作流失败"
        )
        assert scheduled is not None
        assert first.seedvr2_execution_account_id == seed.id
        assert first.attempts[-1].seedvr2_execution_account_id == seed.id
