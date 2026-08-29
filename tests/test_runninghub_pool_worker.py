from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationTask,
    GenerationTaskAttempt,
    RunningHubConfig,
    RunningHubExecutionAccount,
    TaskStatus,
)
from app.services.runninghub import RunningHubAccountStatus, RunningHubError
from app.services.runninghub_dispatch import reserve_pool_task, task_uses_execution_pool
from app.services.runninghub_pool import create_execution_account
from app.services.runninghub_pool import credential_active_task_count
from app.services.security import encrypt_secret, secret_fingerprint
from app.services.storage import to_relative_data_path
from app.services.task_management import TaskManagementError, prepare_task_retry
from app.workers import task_worker
from app.workflows import get_workflow
from app.workflows.base import WorkflowAsset
from tests.conftest import create_user
from tests.test_worker import FakeRunningHub


@pytest.fixture(autouse=True)
def clear_pool_worker_capacity_state():
    task_worker._capacity_check_after.clear()
    task_worker._remote_account_task_counts.clear()
    yield
    task_worker._capacity_check_after.clear()
    task_worker._remote_account_task_counts.clear()


def _create_account(
    db,
    admin_id: int,
    *,
    label: str,
    api_key: str,
    max_concurrent_tasks: int = 5,
    enabled: bool = True,
) -> RunningHubExecutionAccount:
    return create_execution_account(
        db,
        label=label,
        api_key=api_key,
        base_url="https://rh.example",
        digital_human_ai_app_id=f"app-{label}",
        max_concurrent_tasks=max_concurrent_tasks,
        is_enabled=enabled,
        admin_user_ids=[admin_id],
    )


def _create_pool_tasks(
    admin_id: int,
    account_ids: list[int],
    task_ids: list[str],
    *,
    workflow_type: str = "digital_human",
) -> None:
    with SessionLocal() as db:
        batch = GenerationBatch(
            id=f"batch-{task_ids[0]}",
            user_id=admin_id,
            name="资源池 Worker 测试",
            workflow_type=workflow_type,
            runninghub_execution_account_ids_json=json.dumps(account_ids),
            source_channel=BATCH_SOURCE_NEW_WORKBENCH,
            audio_mode="upload",
            request_key=f"request-{task_ids[0]}",
            status="ACTIVE",
            total_items=len(task_ids),
        )
        db.add(batch)
        for index, task_id in enumerate(task_ids, start=1):
            item = GenerationBatchItem(
                id=f"item-{task_id}",
                batch=batch,
                row_number=index,
                row_key=f"ROW-{index}",
                manifest_json="{}",
            )
            db.add(item)
            db.add(
                GenerationTask(
                    id=task_id,
                    user_id=admin_id,
                    batch_item=item,
                    workflow_type=workflow_type,
                    image_path="uploads/placeholder/image.png",
                    audio_path="uploads/placeholder/audio.mp3",
                    image_original_name="image.png",
                    audio_original_name="audio.mp3",
                    audio_duration_seconds=10,
                    start_seconds=0,
                    end_seconds=10,
                    prompt="测试",
                    status=TaskStatus.PENDING.value,
                    created_at=datetime.now(timezone.utc)
                    + timedelta(microseconds=index),
                )
            )
        db.commit()


def _prepare_real_pool_task_files(task_id: str) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task is not None
        upload_dir = settings.uploads_dir / str(task.user_id) / task.id
        upload_dir.mkdir(parents=True)
        image = upload_dir / "image.png"
        audio = upload_dir / "audio.mp3"
        image.write_bytes(b"image")
        audio.write_bytes(b"audio")
        workflow = get_workflow("digital_human")
        parameters = workflow.validate_parameters(
            {
                "prompt": "资源池提交",
                "start_time": "0:00",
                "end_time": "0:10",
                "timing_mode": "exact_timestamps",
            },
            {"audio_duration_seconds": 10},
        )
        task.image_path = to_relative_data_path(image, settings)
        task.audio_path = to_relative_data_path(audio, settings)
        task.input_payload = json.dumps(
            workflow.serialize_input(
                [
                    WorkflowAsset(
                        "image",
                        "image",
                        task.image_path,
                        image.name,
                    ),
                    WorkflowAsset(
                        "audio",
                        "audio",
                        task.audio_path,
                        audio.name,
                    ),
                ],
                parameters,
                {"audio_duration_seconds": 10},
            ),
            ensure_ascii=False,
        )
        db.commit()


def test_zero_balance_account_is_released_before_upload_and_task_uses_funded_account(
    monkeypatch,
):
    admin = create_user("pool-zero-balance-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        empty = _create_account(
            db,
            admin.id,
            label="零余额",
            api_key="pool-zero-balance-key",
            max_concurrent_tasks=1,
        )
        funded = _create_account(
            db,
            admin.id,
            label="有余额",
            api_key="pool-funded-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        empty_id, funded_id = empty.id, funded.id
    _create_pool_tasks(admin.id, [empty_id, funded_id], ["pool-balance-task"])
    _prepare_real_pool_task_files("pool-balance-task")

    empty_client = FakeRunningHub(current_task_count=0)
    empty_client.last_account_status = RunningHubAccountStatus(
        current_task_count=0,
        remain_coins=Decimal("0"),
        remain_money=None,
        currency=None,
        api_type="NORMAL",
    )
    funded_client = FakeRunningHub(current_task_count=0)
    funded_client.last_account_status = RunningHubAccountStatus(
        current_task_count=0,
        remain_coins=Decimal("100"),
        remain_money=None,
        currency=None,
        api_type="NORMAL",
    )
    clients = {empty_id: empty_client, funded_id: funded_client}
    monkeypatch.setattr(task_worker, "_make_client", lambda config: clients[config.id])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-balance-task"
        assert db.get(GenerationTask, "pool-balance-task").execution_account_id == empty_id
        task_worker.process_task(db, "pool-balance-task")
        task = db.get(GenerationTask, "pool-balance-task")
        assert task.status == TaskStatus.PENDING.value
        assert task.execution_account_id is None
        assert task.runninghub_auto_retry_count == 0
        assert empty_client.upload_calls == 0
        assert empty_client.submissions == 0

        assert task_worker.claim_next_pending_task(db) == "pool-balance-task"
        assert db.get(GenerationTask, "pool-balance-task").execution_account_id == funded_id
        task_worker.process_task(db, "pool-balance-task")
        task = db.get(GenerationTask, "pool-balance-task")
        assert task.execution_account_id == funded_id
        assert task.status == TaskStatus.SUBMITTED.value
        assert funded_client.submissions == 1


def test_pool_dispatches_fifo_subtasks_across_independent_account_slots():
    admin = create_user("pool-dispatch-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        first = _create_account(
            db,
            admin.id,
            label="一号",
            api_key="pool-dispatch-key-1",
            max_concurrent_tasks=1,
        )
        second = _create_account(
            db,
            admin.id,
            label="二号",
            api_key="pool-dispatch-key-2",
            max_concurrent_tasks=1,
        )
        db.commit()
        account_ids = [first.id, second.id]
    _create_pool_tasks(admin.id, account_ids, ["pool-task-1", "pool-task-2", "pool-task-3"])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-task-1"
        assert task_worker.claim_next_pending_task(db) == "pool-task-2"
        assert task_worker.claim_next_pending_task(db) is None
        assignments = {
            db.get(GenerationTask, task_id).execution_account_id
            for task_id in ("pool-task-1", "pool-task-2")
        }
        assert assignments == set(account_ids)
        first_task = db.get(GenerationTask, "pool-task-1")
        first_task.status = TaskStatus.SUCCESS.value
        db.commit()
        assert task_worker.claim_next_pending_task(db) == "pool-task-3"
        third = db.get(GenerationTask, "pool-task-3")
        assert third.user_id == admin.id
        assert third.execution_account_id in account_ids


def test_pool_keeps_digital_human_and_seedvr2_on_one_pipeline_account(monkeypatch):
    admin = create_user("pool-pipeline-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="流水线账号",
            api_key="pool-pipeline-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(admin.id, [account_id], ["pool-pipeline-task"])
    _prepare_real_pool_task_files("pool-pipeline-task")
    fake = FakeRunningHub(
        query_results={
            "submitted-remote-id": {
                "taskId": "submitted-remote-id",
                "status": "SUCCESS",
                "results": [
                    {
                        "nodeId": "digital-output",
                        "outputType": "mp4",
                        "url": "https://x/digital.mp4",
                    }
                ],
            },
            "submitted-remote-id-2": {
                "taskId": "submitted-remote-id-2",
                "status": "SUCCESS",
                "results": [
                    {
                        "nodeId": "seed-output",
                        "outputType": "mp4",
                        "url": "https://x/seed.mp4",
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-pipeline-task"
        task_worker.process_task(db, "pool-pipeline-task")
        task_worker.process_task(db, "pool-pipeline-task")
        task = db.get(GenerationTask, "pool-pipeline-task")
        assert task.enhancement is not None
        assert task.enhancement.execution_account_id == account_id

        # Disabling the account stops new pipelines only. This already-paid
        # pipeline must still finish SeedVR2 on its original account.
        db.get(RunningHubExecutionAccount, account_id).is_enabled = False
        db.commit()
        task_worker.process_task(db, "pool-pipeline-task")
        task_worker.process_task(db, "pool-pipeline-task")
        db.expire_all()
        task = db.get(GenerationTask, "pool-pipeline-task")
        assert task.status == TaskStatus.SUCCESS.value
        assert len(task.runninghub_attempts) == 1
        assert task.runninghub_attempts[0].execution_account_id == account_id
        assert task.runninghub_attempts[0].status == "SUCCESS"
        assert len(task.enhancement.attempts) == 1
        assert task.enhancement.attempts[0].execution_account_id == account_id
        assert task.enhancement.attempts[0].status == "SUCCESS"


def test_explicit_digital_failure_retries_on_original_pipeline_account(monkeypatch):
    admin = create_user("pool-reselect-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        first = _create_account(
            db, admin.id, label="B", api_key="pool-reselect-b", max_concurrent_tasks=1
        )
        second = _create_account(
            db, admin.id, label="C", api_key="pool-reselect-c", max_concurrent_tasks=1
        )
        db.commit()
        account_ids = [first.id, second.id]
    _create_pool_tasks(admin.id, account_ids, ["pool-reselect-task"])
    _prepare_real_pool_task_files("pool-reselect-task")
    fake = FakeRunningHub(
        query_results={
            "submitted-remote-id": {
                "taskId": "submitted-remote-id",
                "status": "FAILED",
                "errorCode": "805",
                "errorMessage": "明确失败",
            },
            "submitted-remote-id-2": {
                "taskId": "submitted-remote-id-2",
                "status": "SUCCESS",
                "results": [
                    {
                        "nodeId": "digital-output",
                        "outputType": "mp4",
                        "url": "https://x/digital-2.mp4",
                    }
                ],
            },
        }
    )
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-reselect-task"
        task_worker.process_task(db, "pool-reselect-task")
        first_account_id = db.get(GenerationTask, "pool-reselect-task").execution_account_id
        task_worker.process_task(db, "pool-reselect-task")
        task = db.get(GenerationTask, "pool-reselect-task")
        assert task.status == TaskStatus.PENDING.value
        assert task.execution_account_id == first_account_id
        task.runninghub_auto_retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert task_worker.claim_next_pending_task(db) == "pool-reselect-task"
        task = db.get(GenerationTask, "pool-reselect-task")
        assert task.execution_account_id == first_account_id
        retry_account_id = task.execution_account_id
        task_worker.process_task(db, "pool-reselect-task")
        task_worker.process_task(db, "pool-reselect-task")
        db.expire_all()
        task = db.get(GenerationTask, "pool-reselect-task")
        assert [attempt.execution_account_id for attempt in task.runninghub_attempts] == [
            first_account_id,
            first_account_id,
        ]
        assert task.runninghub_attempts[0].status == "FAILED"
        assert task.enhancement.execution_account_id == retry_account_id


def test_manual_retry_keeps_cloud_accepted_pipeline_account():
    admin = create_user("pool-manual-retry-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="手动重试原账号",
            api_key="pool-manual-retry-key",
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(admin.id, [account_id], ["pool-manual-retry-task"])
    _prepare_real_pool_task_files("pool-manual-retry-task")

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-manual-retry-task"
        task = db.get(GenerationTask, "pool-manual-retry-task")
        assert task is not None
        assert task.execution_account_id == account_id
        task.status = TaskStatus.FAILED.value
        task.runninghub_task_id = "failed-remote-task"
        attempt = task.runninghub_attempts[-1]
        attempt.status = "FAILED"
        attempt.remote_task_id = task.runninghub_task_id

        prepare_task_retry(task, get_settings())

        assert task.status == TaskStatus.PENDING.value
        assert task.runninghub_task_id is None
        assert task.execution_account_id == account_id


def test_manual_retry_moves_snapshotless_legacy_task_to_current_user_pool():
    user = create_user("legacy-retry-pool-user")
    with SessionLocal() as db:
        account = _create_account(
            db,
            user.id,
            label="当前分配账号",
            api_key="legacy-retry-current-key",
        )
        task = GenerationTask(
            id="legacy-retry-pool-task",
            user_id=user.id,
            workflow_type="digital_human",
            image_path="uploads/placeholder/image.png",
            audio_path="uploads/placeholder/audio.mp3",
            image_original_name="image.png",
            audio_original_name="audio.mp3",
            audio_duration_seconds=10,
            start_seconds=0,
            end_seconds=10,
            prompt="测试",
            status=TaskStatus.FAILED.value,
        )
        db.add(task)
        db.commit()
        account_id = account.id

    _prepare_real_pool_task_files("legacy-retry-pool-task")
    with SessionLocal() as db:
        task = db.get(GenerationTask, "legacy-retry-pool-task")
        prepare_task_retry(task, get_settings())

        assert json.loads(task.runninghub_execution_account_ids_json) == [account_id]
        assert task.execution_account_id is None
        assert task_uses_execution_pool(task) is True
        db.commit()
        assert task_worker.claim_next_pending_task(db) == task.id
        assert db.get(GenerationTask, task.id).execution_account_id == account_id


def test_ambiguous_pool_submit_keeps_account_capacity_and_blocks_retry(monkeypatch):
    admin = create_user("pool-ambiguous-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="模糊提交账号",
            api_key="pool-ambiguous-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        account_id = account.id
        fingerprint = account.credential_fingerprint
    _create_pool_tasks(admin.id, [account_id], ["pool-ambiguous-task"])
    _prepare_real_pool_task_files("pool-ambiguous-task")
    fake = FakeRunningHub(submit_network_error=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-ambiguous-task"
        task_worker.process_task(db, "pool-ambiguous-task")
        task = db.get(GenerationTask, "pool-ambiguous-task")
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "SUBMIT_OUTCOME_UNKNOWN"
        assert task.execution_account_id == account_id
        attempt = db.scalar(
            select(GenerationTaskAttempt).where(
                GenerationTaskAttempt.generation_task_id == task.id
            )
        )
        assert attempt.status == "SUBMIT_UNKNOWN"
        assert attempt.remote_task_id is None
        assert credential_active_task_count(db, fingerprint) == 1
        with pytest.raises(TaskManagementError, match="禁止盲目重提"):
            prepare_task_retry(task, get_settings())


def test_pool_reservation_uses_conditional_capacity_check_with_stale_sessions():
    admin = create_user("pool-atomic-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="原子",
            api_key="pool-atomic-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(admin.id, [account_id], ["atomic-task-1", "atomic-task-2"])

    first_session = SessionLocal()
    second_session = SessionLocal()
    try:
        first_task = task_worker._load_task(first_session, "atomic-task-1")
        second_task = task_worker._load_task(second_session, "atomic-task-2")
        assert first_task is not None and second_task is not None
        first_reservation = reserve_pool_task(first_session, first_task)
        second_reservation = reserve_pool_task(second_session, second_task)
        assert first_reservation is not None
        assert second_reservation is None
    finally:
        first_session.close()
        second_session.close()

    with SessionLocal() as db:
        assert db.get(GenerationTask, "atomic-task-1").status == TaskStatus.UPLOADING.value
        assert db.get(GenerationTask, "atomic-task-2").status == TaskStatus.PENDING.value


def test_pool_load_ratio_uses_remote_observation_and_only_snapshot_accounts():
    admin = create_user("pool-load-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        busy = _create_account(
            db,
            admin.id,
            label="远程忙",
            api_key="pool-load-key-busy",
        )
        idle = _create_account(
            db,
            admin.id,
            label="远程闲",
            api_key="pool-load-key-idle",
        )
        excluded = _create_account(
            db,
            admin.id,
            label="未勾选",
            api_key="pool-load-key-excluded",
        )
        db.commit()
        busy_id, idle_id, excluded_id = busy.id, idle.id, excluded.id
    _create_pool_tasks(admin.id, [busy_id, idle_id], ["load-task"])
    task_worker._remote_account_task_counts.update({busy_id: 4, idle_id: 1, excluded_id: 0})

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "load-task"
        task = db.get(GenerationTask, "load-task")
        assert task.execution_account_id == idle_id
        assert task.execution_account_id != excluded_id


def test_dispatch_skips_globally_disabled_and_cooling_snapshot_accounts():
    admin = create_user("pool-filter-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        disabled = _create_account(
            db,
            admin.id,
            label="已停用",
            api_key="pool-filter-disabled-key",
            enabled=False,
        )
        cooling = _create_account(
            db,
            admin.id,
            label="冷却中",
            api_key="pool-filter-cooling-key",
        )
        healthy = _create_account(
            db,
            admin.id,
            label="可调度",
            api_key="pool-filter-healthy-key",
        )
        cooling.health_status = "UNHEALTHY"
        cooling.cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
        account_ids = [disabled.id, cooling.id, healthy.id]
        healthy_id = healthy.id
    _create_pool_tasks(admin.id, account_ids, ["pool-filter-task"])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-filter-task"
        assert db.get(GenerationTask, "pool-filter-task").execution_account_id == healthy_id


def test_remote_full_pool_account_cools_and_same_task_fails_over_to_other_account(
    monkeypatch,
):
    admin = create_user("pool-cooldown-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        full = _create_account(
            db,
            admin.id,
            label="已满",
            api_key="pool-full-key",
            max_concurrent_tasks=1,
        )
        available = _create_account(
            db,
            admin.id,
            label="可用",
            api_key="pool-available-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        full_id, available_id = full.id, available.id
    _create_pool_tasks(admin.id, [full_id, available_id], ["pool-failover-task"])

    clients = {
        full_id: FakeRunningHub(current_task_count=1),
        available_id: FakeRunningHub(current_task_count=0),
    }
    monkeypatch.setattr(task_worker, "_make_client", lambda config: clients[config.id])
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-failover-task"
        assert db.get(GenerationTask, "pool-failover-task").execution_account_id == full_id
        task_worker.process_task(db, "pool-failover-task")
        requeued = db.get(GenerationTask, "pool-failover-task")
        assert requeued.status == TaskStatus.PENDING.value
        assert requeued.execution_account_id is None
        cooled = db.get(RunningHubExecutionAccount, full_id)
        assert cooled.health_status == "HEALTHY"
        assert cooled.health_error_code == "CAPACITY_FULL"
        assert cooled.cooldown_until is not None
        assert task_worker.claim_next_pending_task(db) == "pool-failover-task"
        assert db.get(GenerationTask, "pool-failover-task").execution_account_id == available_id


def test_one_pool_account_probe_error_does_not_block_other_healthy_account(
    monkeypatch,
):
    admin = create_user("pool-health-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        failing = _create_account(
            db,
            admin.id,
            label="探测失败",
            api_key="pool-health-failing-key",
            max_concurrent_tasks=1,
        )
        healthy = _create_account(
            db,
            admin.id,
            label="健康",
            api_key="pool-health-good-key",
            max_concurrent_tasks=1,
        )
        db.commit()
        failing_id, healthy_id = failing.id, healthy.id
    _create_pool_tasks(admin.id, [failing_id, healthy_id], ["pool-health-task"])

    failing_client = FakeRunningHub()

    def fail_capacity_probe():
        failing_client.capacity_checks += 1
        raise RunningHubError(
            "读取 RunningHub 账号状态失败",
            error_code="INVALID_CREDENTIAL",
        )

    failing_client.get_account_current_task_count = fail_capacity_probe
    clients = {
        failing_id: failing_client,
        healthy_id: FakeRunningHub(current_task_count=0),
    }
    monkeypatch.setattr(task_worker, "_make_client", lambda config: clients[config.id])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-health-task"
        assert db.get(GenerationTask, "pool-health-task").execution_account_id == failing_id
        task_worker.process_task(db, "pool-health-task")
        failed_account = db.get(RunningHubExecutionAccount, failing_id)
        assert failed_account.health_status == "UNHEALTHY"
        assert failed_account.health_error_code == "INVALID_CREDENTIAL"
        assert failed_account.cooldown_until is not None
        assert task_worker.claim_next_pending_task(db) == "pool-health-task"
        assert db.get(GenerationTask, "pool-health-task").execution_account_id == healthy_id


def test_pool_uses_execution_account_credentials_without_owner_single_account(
    monkeypatch,
):
    admin = create_user("pool-client-admin", is_admin=True, with_config=False)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="独立凭据",
            api_key="pool-client-key",
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(admin.id, [account_id], ["pool-client-task"])
    _prepare_real_pool_task_files("pool-client-task")

    captured: list[RunningHubExecutionAccount] = []
    fake = FakeRunningHub()

    def make_client(config):
        captured.append(config)
        return fake

    monkeypatch.setattr(task_worker, "_make_client", make_client)
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "pool-client-task"
        task_worker.process_task(db, "pool-client-task")
        task = db.get(GenerationTask, "pool-client-task")
        assert task.user_id == admin.id
        assert task.execution_account_id == account_id
        assert task.runninghub_task_id == "submitted-remote-id"
        assert task.status == TaskStatus.SUBMITTED.value
    assert len(captured) == 1
    assert isinstance(captured[0], RunningHubExecutionAccount)
    assert fake.ai_app_id == "app-独立凭据"


def test_same_key_legacy_activity_consumes_pool_capacity_without_duplicate_slots():
    admin = create_user("pool-fingerprint-admin", is_admin=True, with_config=False)
    legacy_user = create_user("pool-fingerprint-legacy")
    shared_key = "one-real-runninghub-account"
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="共享真实账号",
            api_key=shared_key,
            max_concurrent_tasks=2,
        )
        config = db.scalar(
            select(RunningHubConfig).where(RunningHubConfig.user_id == legacy_user.id)
        )
        assert config is not None
        config.api_key_encrypted = encrypt_secret(shared_key)
        config.credential_fingerprint = secret_fingerprint(shared_key)
        db.add(
            GenerationTask(
                id="same-key-legacy-active",
                user_id=legacy_user.id,
                image_path="uploads/legacy/image.png",
                audio_path="uploads/legacy/audio.mp3",
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt="旧任务",
                status=TaskStatus.RUNNING.value,
            )
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(admin.id, [account_id], ["same-key-pool-1", "same-key-pool-2"])

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "same-key-pool-1"
        assert task_worker.claim_next_pending_task(db) is None


def test_new_workbench_ltx_stays_on_legacy_single_account_path():
    admin = create_user("pool-ltx-admin", is_admin=True)
    with SessionLocal() as db:
        account = _create_account(
            db,
            admin.id,
            label="LTX 不应使用",
            api_key="pool-ltx-excluded-key",
        )
        db.commit()
        account_id = account.id
    _create_pool_tasks(
        admin.id,
        [account_id],
        ["new-workbench-ltx-task"],
        workflow_type="ltx_lip_sync",
    )

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "new-workbench-ltx-task"
        task = db.get(GenerationTask, "new-workbench-ltx-task")
        assert task.status == TaskStatus.UPLOADING.value
        assert task.execution_account_id is None
