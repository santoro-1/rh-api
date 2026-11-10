from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_EXECUTION_MODE_DUAL_POOL_V1,
    BATCH_SOURCE_NEW_WORKBENCH,
    EnhancementStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationTask,
    GenerationTaskEnhancement,
    TaskStatus,
    User,
)
from app.services.runninghub_attempts import (
    SUBMIT_OUTCOME_UNKNOWN,
    enhancement_execution_account_for_remote,
)
from app.services.runninghub import RunningHubError
from app.services.runninghub_dual_pool import set_dual_pool_grant
from app.services.runninghub_pool import create_execution_account
from app.services.security import hash_password
from app.services.seedvr2_dispatch import (
    reserve_seedvr2_account,
    seedvr2_active_count,
)
from app.services.seedvr2_pool import create_seedvr2_execution_account
from app.services.storage import to_relative_data_path
from app.services.task_cancellation import cancel_generation_task
from app.services.task_management import TaskManagementError, prepare_task_retry
from app.workers import task_worker
from tests.test_worker import FakeRunningHub


def _build_dual_task(
    db,
    *,
    suffix: str,
    seed_count: int = 2,
    seed_limit: int = 5,
) -> tuple[GenerationTask, list]:
    user = User(
        username=f"dual-runtime-{suffix}",
        password_hash=hash_password("password123"),
        is_admin=True,
        is_active=True,
    )
    db.add(user)
    db.flush()
    set_dual_pool_grant(db, user=user, is_enabled=True)
    digital = create_execution_account(
        db,
        label=f"数字人-{suffix}",
        api_key=f"digital-key-{suffix}",
        base_url="https://digital.example",
        digital_human_ai_app_id=f"digital-app-{suffix}",
        max_concurrent_tasks=5,
        is_enabled=True,
        admin_user_ids=[user.id],
    )
    seeds = [
        create_seedvr2_execution_account(
            db,
            label=f"放大-{suffix}-{index}",
            api_key=f"seed-key-{suffix}-{index}",
            base_url=f"https://seed-{index}.example",
            seedvr2_ai_app_id=f"seed-app-{suffix}-{index}",
            max_concurrent_tasks=seed_limit,
            is_enabled=True,
            user_ids=[user.id],
        )
        for index in range(1, seed_count + 1)
    ]
    batch = GenerationBatch(
        id=f"dual-runtime-batch-{suffix}",
        user_id=user.id,
        name="双池运行时测试",
        workflow_type="digital_human",
        runninghub_execution_account_ids_json=json.dumps([digital.id]),
        seedvr2_execution_account_ids_json=json.dumps([seed.id for seed in seeds]),
        execution_mode=BATCH_EXECUTION_MODE_DUAL_POOL_V1,
        source_channel=BATCH_SOURCE_NEW_WORKBENCH,
        audio_mode="upload",
        request_key=f"dual-runtime-request-{suffix}",
        status="ACTIVE",
        total_items=1,
    )
    item = GenerationBatchItem(
        id=f"dual-runtime-item-{suffix}",
        batch=batch,
        row_number=1,
        row_key="1",
        manifest_json="{}",
    )
    task = GenerationTask(
        id=f"dual-runtime-task-{suffix}",
        user_id=user.id,
        batch_item=item,
        execution_account_id=digital.id,
        workflow_type="digital_human",
        image_path="uploads/image.png",
        audio_path="uploads/audio.mp3",
        image_original_name="image.png",
        audio_original_name="audio.mp3",
        audio_duration_seconds=1,
        start_seconds=0,
        end_seconds=1,
        prompt="测试",
        status=TaskStatus.RUNNING.value,
    )
    source = (
        get_settings().outputs_dir
        / str(user.id)
        / task.id
        / "source"
        / "digital.mp4"
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\x00\x00\x00\x18ftypisomsource")
    task.enhancement = GenerationTaskEnhancement(
        id=f"dual-runtime-enhancement-{suffix}",
        generation_task_id=task.id,
        status=EnhancementStatus.PENDING.value,
        source_result_path=to_relative_data_path(source, get_settings()),
        source_filename=source.name,
        execution_account_id=digital.id,
    )
    db.add_all([batch, item, task])
    db.flush()
    return task, seeds


def _add_second_enhancement(db, first: GenerationTask, *, suffix: str):
    batch = first.batch_item.batch
    item = GenerationBatchItem(
        id=f"dual-runtime-item-{suffix}",
        batch=batch,
        row_number=2,
        row_key="2",
        manifest_json="{}",
    )
    task = GenerationTask(
        id=f"dual-runtime-task-{suffix}",
        user_id=first.user_id,
        batch_item=item,
        execution_account_id=first.execution_account_id,
        workflow_type="digital_human",
        image_path="uploads/image.png",
        audio_path="uploads/audio.mp3",
        image_original_name="image.png",
        audio_original_name="audio.mp3",
        audio_duration_seconds=1,
        start_seconds=0,
        end_seconds=1,
        prompt="测试",
        status=TaskStatus.RUNNING.value,
    )
    task.enhancement = GenerationTaskEnhancement(
        id=f"dual-runtime-enhancement-{suffix}",
        generation_task_id=task.id,
        status=EnhancementStatus.PENDING.value,
        source_result_path=first.enhancement.source_result_path,
        source_filename="digital.mp4",
        execution_account_id=first.execution_account_id,
    )
    db.add_all([item, task])
    db.flush()
    return task


def test_seedvr2_ambiguous_submit_keeps_original_account_and_capacity():
    with SessionLocal() as db:
        task, seeds = _build_dual_task(
            db, suffix="ambiguous", seed_count=1, seed_limit=1
        )
        seed = reserve_seedvr2_account(db, task, task.enhancement)
        assert seed is not None
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        attempt.status = "SUBMIT_UNKNOWN"
        attempt.finished_at = datetime.now(timezone.utc)
        task.enhancement.status = EnhancementStatus.FAILED.value
        task.error_code = SUBMIT_OUTCOME_UNKNOWN
        task.status = TaskStatus.FAILED.value
        second = _add_second_enhancement(db, task, suffix="ambiguous-second")
        db.flush()

        assert seedvr2_active_count(db, seeds[0].id) == 1
        assert reserve_seedvr2_account(db, second, second.enhancement) is None
        assert (
            enhancement_execution_account_for_remote(task, task.enhancement).id
            == seeds[0].id
        )
        with pytest.raises(TaskManagementError, match="禁止盲目重提"):
            prepare_task_retry(task, get_settings())


def test_confirmed_unusable_seed_account_switches_only_next_paid_attempt():
    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="unusable")
        first = reserve_seedvr2_account(db, task, task.enhancement)
        assert first is not None and first.id == seeds[0].id
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        attempt.remote_task_id = "seed-unusable-remote"
        attempt.status = "RUNNING"
        task.enhancement.remote_task_id = "seed-unusable-remote"
        task.enhancement.status = EnhancementStatus.RUNNING.value
        task.enhancement.submitted_at = datetime.now(timezone.utc)
        fake = FakeRunningHub(
            query_results={
                "seed-unusable-remote": {
                    "taskId": "seed-unusable-remote",
                    "status": "FAILED",
                    "errorCode": "HTTP 401 UNAUTHORIZED",
                    "errorMessage": "invalid api key",
                }
            }
        )

        task_worker._handle_enhancement_remote_status(
            db, task, task.enhancement, fake
        )
        db.refresh(task.enhancement)
        assert task.enhancement.status == EnhancementStatus.PENDING.value
        assert task.enhancement.remote_task_id is None
        assert task.enhancement.seedvr2_execution_account_id is None
        assert attempt.seedvr2_execution_account_id == seeds[0].id
        assert seeds[0].health_status == "UNHEALTHY"

        replacement = reserve_seedvr2_account(db, task, task.enhancement)
        assert replacement is not None and replacement.id == seeds[1].id
        new_attempt = task_worker._new_enhancement_attempt(task.enhancement)
        assert new_attempt.attempt_number == 2
        assert new_attempt.seedvr2_execution_account_id == seeds[1].id


def test_normal_seedvr2_failure_retries_same_account():
    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="normal-failure")
        first = reserve_seedvr2_account(db, task, task.enhancement)
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        attempt.remote_task_id = "seed-normal-failure"
        attempt.status = "RUNNING"
        task.enhancement.remote_task_id = "seed-normal-failure"
        task.enhancement.status = EnhancementStatus.RUNNING.value
        task.enhancement.submitted_at = datetime.now(timezone.utc)
        fake = FakeRunningHub(
            query_results={
                "seed-normal-failure": {
                    "taskId": "seed-normal-failure",
                    "status": "FAILED",
                    "errorCode": "805",
                    "errorMessage": "显存任务失败",
                }
            }
        )
        task_worker._handle_enhancement_remote_status(
            db, task, task.enhancement, fake
        )
        assert task.enhancement.status == EnhancementStatus.PENDING.value
        assert task.enhancement.seedvr2_execution_account_id == first.id
        assert attempt.seedvr2_execution_account_id == seeds[0].id


def test_seedvr2_capacity_query_switches_only_for_confirmed_unusable_account():
    class CapacityQueryFailure:
        def __init__(self, error: RunningHubError):
            self.error = error

        def get_account_current_task_count(self):
            raise self.error

    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="capacity-transient")
        first = reserve_seedvr2_account(db, task, task.enhancement)
        task_worker._process_enhancement(
            db,
            task,
            task.enhancement,
            CapacityQueryFailure(RunningHubError("temporary timeout")),
            first,
        )
        assert task.enhancement.seedvr2_execution_account_id == first.id
        assert task.enhancement.auto_retry_after is not None
        assert first.health_status == "HEALTHY"

    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="capacity-unusable")
        first = reserve_seedvr2_account(db, task, task.enhancement)
        task_worker._process_enhancement(
            db,
            task,
            task.enhancement,
            CapacityQueryFailure(
                RunningHubError(
                    "HTTP 401 invalid api key",
                    error_code="HTTP 401",
                )
            ),
            first,
        )
        assert task.enhancement.seedvr2_execution_account_id is None
        assert first.health_status == "UNHEALTHY"
        replacement = reserve_seedvr2_account(db, task, task.enhancement)
        assert replacement is not None and replacement.id == seeds[1].id


def test_seedvr2_presubmission_credential_failure_switches_next_attempt():
    class InvalidCredentialUpload:
        def get_account_current_task_count(self):
            return 0

        def upload_file(self, path):
            raise RunningHubError(
                "HTTP 403 no permission",
                error_code="HTTP 403",
                retry_safe=True,
            )

    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="upload-unusable")
        first = reserve_seedvr2_account(db, task, task.enhancement)
        task_worker._process_enhancement(
            db,
            task,
            task.enhancement,
            InvalidCredentialUpload(),
            first,
        )
        assert task.enhancement.seedvr2_execution_account_id is None
        assert first.health_status == "UNHEALTHY"
        assert task.enhancement.attempts[-1].seedvr2_execution_account_id == first.id
        replacement = reserve_seedvr2_account(db, task, task.enhancement)
        assert replacement is not None and replacement.id == seeds[1].id


def test_cancel_and_recovery_keep_seedvr2_attempt_account(monkeypatch):
    captured: dict[str, object] = {}

    class FakeCancelClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def cancel_task(self, remote_id):
            captured["remote_id"] = remote_id

    monkeypatch.setattr(
        "app.services.task_cancellation.RunningHubClient", FakeCancelClient
    )
    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="cancel")
        seed = reserve_seedvr2_account(db, task, task.enhancement)
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        attempt.remote_task_id = "seed-cancel-remote"
        attempt.status = "RUNNING"
        task.enhancement.remote_task_id = "seed-cancel-remote"
        task.enhancement.status = EnhancementStatus.RUNNING.value
        cancel_generation_task(db, task)
        assert captured["remote_id"] == "seed-cancel-remote"
        assert captured["base_url"] == seed.base_url
        assert captured["ai_app_id"] == seed.seedvr2_ai_app_id
        assert captured["api_key"] == f"seed-key-cancel-1"
        assert task.status == TaskStatus.CANCELLED.value
        assert attempt.seedvr2_execution_account_id == seeds[0].id

    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="recovery", seed_count=1)
        seed = reserve_seedvr2_account(db, task, task.enhancement)
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        task.enhancement.status = EnhancementStatus.UPLOADING.value
        db.commit()
        recovered = task_worker.recover_interrupted_tasks(db)
        db.refresh(task.enhancement)
        assert recovered == 1
        assert task.enhancement.status == EnhancementStatus.PENDING.value
        assert task.enhancement.seedvr2_execution_account_id == seed.id
        assert attempt.seedvr2_execution_account_id == seeds[0].id
        assert attempt.status == "INTERRUPTED_BEFORE_SUBMIT"


def test_running_remote_seedvr2_uses_saved_account_even_if_later_disabled(
    monkeypatch,
):
    captured = []
    fake = FakeRunningHub(
        query_results={
            "seed-saved-account-remote": {
                "taskId": "seed-saved-account-remote",
                "status": "RUNNING",
            }
        }
    )

    def make_client(config):
        captured.append(config)
        fake.ai_app_id = config.seedvr2_ai_app_id
        fake.submission_type = "ai-app"
        return fake

    monkeypatch.setattr(task_worker, "_make_client", make_client)
    with SessionLocal() as db:
        task, seeds = _build_dual_task(db, suffix="saved-account", seed_count=1)
        seed = reserve_seedvr2_account(db, task, task.enhancement)
        attempt = task_worker._new_enhancement_attempt(task.enhancement)
        attempt.remote_task_id = "seed-saved-account-remote"
        attempt.status = "RUNNING"
        task.enhancement.remote_task_id = "seed-saved-account-remote"
        task.enhancement.status = EnhancementStatus.RUNNING.value
        task.enhancement.submitted_at = datetime.now(timezone.utc)
        seed.is_enabled = False
        seed.health_status = "UNHEALTHY"
        db.commit()
        task_id = task.id
        seed_id = seed.id

    with SessionLocal() as db:
        task_worker.process_task(db, task_id)
        task = db.get(GenerationTask, task_id)
        assert task.enhancement.remote_task_id == "seed-saved-account-remote"
        assert task.enhancement.seedvr2_execution_account_id == seed_id
    assert len(captured) == 1
    assert captured[0].id == seed_id
    assert fake.query_calls == 1
    assert fake.submissions == 0
