from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus, WorkflowConfig
from app.config import get_settings
from app.services.runninghub import RunningHubError
from app.services.security import encrypt_secret
from app.services.storage import to_relative_data_path
from app.workers import task_worker
from app.workflows import get_workflow
from app.workflows.base import WorkflowAsset

from tests.conftest import create_user


class FakeRunningHub:
    def __init__(
        self,
        *,
        current_task_count: int = 0,
        capacity_error: bool = False,
        upload_error: bool = False,
        submit_network_error: bool = False,
        query_not_found: bool = False,
        query_error: bool = False,
        cancel_error: bool = False,
        query_result: dict | None = None,
    ):
        self.submissions = 0
        self.query_calls = 0
        self.upload_calls = 0
        self.capacity_checks = 0
        self.current_task_count = current_task_count
        self.capacity_error = capacity_error
        self.upload_error = upload_error
        self.submit_network_error = submit_network_error
        self.query_not_found = query_not_found
        self.query_error = query_error
        self.cancel_error = cancel_error
        self.query_result = query_result
        self.cancel_calls = 0

    def get_account_current_task_count(self):
        self.capacity_checks += 1
        return self.current_task_count

    def upload_file(self, path):
        self.upload_calls += 1
        if self.upload_error:
            raise RunningHubError(
                "上传素材到 RunningHub 时网络请求失败",
                retry_safe=True,
                diagnostics={
                    "runninghub_operation": "asset_upload",
                    "endpoint_host": "rh.example",
                    "network_error_type": "ReadTimeout",
                    "elapsed_ms": 600001,
                    "asset_size_bytes": 240 * 1024 * 1024,
                    "asset_size_mb": 240.0,
                    "upload_size_warning": (
                        "素材体积 240.0MB，超过 RunningHub 官方建议的 "
                        "30MB，可能导致上传失败"
                    ),
                },
            )
        return "openapi/" + path.name

    def submit_task(self, payload):
        if self.submit_network_error:
            raise RunningHubError("提交 RunningHub 任务时网络请求失败")
        if self.capacity_error:
            raise RunningHubError(
                "提交任务失败：api queue limit reached, please retry later",
                error_code="421",
            )
        self.submissions += 1
        self.last_payload = payload
        assert payload["instanceType"] in {"default", "plus"}
        assert payload["usePersonalQueue"] is False
        assert "retainSeconds" not in payload
        return "submitted-remote-id"

    def query_task(self, task_id):
        self.query_calls += 1
        if self.query_error:
            raise RunningHubError("查询 RunningHub 任务时网络请求失败")
        if self.query_not_found:
            raise RunningHubError(
                "查询任务失败：Task not found | 任务不存在或已过期",
                error_code="1004",
            )
        if self.query_result is not None:
            return self.query_result
        return {"taskId": task_id, "status": "RUNNING", "usage": None}

    def cancel_task(self, task_id):
        self.cancel_calls += 1
        if self.cancel_error:
            raise RunningHubError(
                "取消 RunningHub 任务时网络请求失败",
                diagnostics={"runninghub_operation": "task_cancel"},
            )


@pytest.fixture(autouse=True)
def clear_worker_capacity_state():
    task_worker._capacity_check_after.clear()
    yield
    task_worker._capacity_check_after.clear()


def _add_task(user_id: int, task_id: str, status: str, remote_id: str | None = None):
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id=task_id,
                user_id=user_id,
                runninghub_task_id=remote_id,
                image_path="uploads/does-not-matter/image.png",
                audio_path="uploads/does-not-matter/audio.mp3",
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=20,
                start_seconds=0,
                end_seconds=10,
                prompt="测试",
                status=status,
            )
        )
        db.commit()


def test_worker_does_not_resubmit_existing_remote_task(monkeypatch):
    user = create_user("worker-user")
    _add_task(user.id, "remote-task", TaskStatus.SUBMITTED.value, "already-submitted")
    fake = FakeRunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "remote-task")
        task = db.get(GenerationTask, "remote-task")
        assert task.status == TaskStatus.RUNNING.value
    assert fake.query_calls == 1
    assert fake.submissions == 0


def test_worker_watchdog_queries_then_cancels_after_four_hours(monkeypatch):
    user = create_user("watchdog-expired-worker-user")
    _add_task(
        user.id,
        "watchdog-expired-task",
        TaskStatus.RUNNING.value,
        "watchdog-expired-remote-id",
    )
    fake = FakeRunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task = db.get(GenerationTask, "watchdog-expired-task")
        task.runninghub_submitted_at = datetime.now(timezone.utc) - timedelta(
            hours=4,
            seconds=1,
        )
        db.commit()

        task_worker.process_task(db, task.id)
        task = db.get(GenerationTask, task.id)
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "REMOTE_WATCHDOG_TIMEOUT"
        assert "4 小时" in task.error_message
        assert task.completed_at is not None
    assert fake.query_calls == 1
    assert fake.cancel_calls == 1
    assert fake.submissions == 0


def test_worker_watchdog_keeps_remote_task_before_four_hours(monkeypatch):
    user = create_user("watchdog-active-worker-user")
    _add_task(
        user.id,
        "watchdog-active-task",
        TaskStatus.RUNNING.value,
        "watchdog-active-remote-id",
    )
    fake = FakeRunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task = db.get(GenerationTask, "watchdog-active-task")
        task.runninghub_submitted_at = datetime.now(timezone.utc) - timedelta(
            hours=3,
            minutes=59,
        )
        db.commit()

        task_worker.process_task(db, task.id)
        task = db.get(GenerationTask, task.id)
        assert task.status == TaskStatus.RUNNING.value
        assert task.completed_at is None
    assert fake.query_calls == 1
    assert fake.cancel_calls == 0


def test_worker_watchdog_retries_cancel_without_releasing_slot(monkeypatch):
    user = create_user("watchdog-cancel-retry-user")
    _add_task(
        user.id,
        "watchdog-cancel-retry-task",
        TaskStatus.RUNNING.value,
        "watchdog-cancel-retry-remote-id",
    )
    fake = FakeRunningHub(cancel_error=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task = db.get(GenerationTask, "watchdog-cancel-retry-task")
        task.runninghub_submitted_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()

        task_worker.process_task(db, task.id)
        task = db.get(GenerationTask, task.id)
        assert task.status == TaskStatus.RUNNING.value
        assert task.error_code == task_worker.REMOTE_WATCHDOG_CANCEL_ERROR_CODE
        assert "继续查询、重试取消" in task.error_message
        assert task.completed_at is None

        fake.query_error = True
        task_worker.process_task(db, task.id)
        task = db.get(GenerationTask, task.id)
        assert task.status == TaskStatus.RUNNING.value
        assert task.error_code == task_worker.REMOTE_WATCHDOG_CANCEL_ERROR_CODE
    assert fake.query_calls == 2
    assert fake.cancel_calls == 2
    assert fake.submissions == 0


def test_worker_watchdog_consumes_remote_terminal_status_before_cancel(monkeypatch):
    user = create_user("watchdog-terminal-worker-user")
    _add_task(
        user.id,
        "watchdog-terminal-task",
        TaskStatus.RUNNING.value,
        "watchdog-terminal-remote-id",
    )
    fake = FakeRunningHub(query_not_found=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task = db.get(GenerationTask, "watchdog-terminal-task")
        task.runninghub_submitted_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()

        task_worker.process_task(db, task.id)
        task = db.get(GenerationTask, task.id)
        assert task.status == TaskStatus.CANCELLED.value
        assert task.error_code == "REMOTE_TASK_NOT_FOUND"
    assert fake.query_calls == 1
    assert fake.cancel_calls == 0


def test_worker_closes_remote_task_removed_after_manual_cancel(monkeypatch):
    user = create_user("cancelled-worker-user")
    _add_task(
        user.id,
        "cancelled-remote-task",
        TaskStatus.RUNNING.value,
        "removed-runninghub-task",
    )
    fake = FakeRunningHub(query_not_found=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "cancelled-remote-task")
        task = db.get(GenerationTask, "cancelled-remote-task")
        assert task.status == TaskStatus.CANCELLED.value
        assert task.runninghub_task_id == "removed-runninghub-task"
        assert task.error_code == "REMOTE_TASK_NOT_FOUND"
        assert task.completed_at is not None
    assert fake.query_calls == 1
    assert fake.submissions == 0


def test_worker_persists_failure_and_retries_three_times_with_backoff(
    monkeypatch,
):
    user = create_user("failed-detail-worker-user")
    _add_task(
        user.id,
        "failed-detail-task",
        TaskStatus.RUNNING.value,
        "failed-remote-id",
    )
    failed_reason = {
        "exception_type": "torch.OutOfMemoryError",
        "node_name": "WanVideoEncode",
        "node_id": "298",
        "exception_message": "显存耗尽导致进程中断，请降低分辨率",
        "traceback": '["technical stack"]',
    }
    fake = FakeRunningHub(
        query_result={
            "taskId": "failed-remote-id",
            "status": "FAILED",
            "errorCode": "805",
            "errorMessage": "工作流运行失败",
            "failedReason": failed_reason,
            "usage": {"taskCostTime": "0"},
        }
    )
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        task_worker.process_task(db, "failed-detail-task")
        task = db.get(GenerationTask, "failed-detail-task")
        assert task.status == TaskStatus.PENDING.value
        assert task.error_code == "805"
        assert "显存耗尽导致进程中断" in task.error_message
        assert "失败节点：WanVideoEncode" in task.error_message
        assert "节点 ID：298" in task.error_message
        assert "第 1/3 次自动重试" in task.error_message
        assert json.loads(task.runninghub_failed_reason) == failed_reason
        assert len(json.loads(task.runninghub_attempt_history)) == 1
        assert task.runninghub_auto_retry_count == 1
        assert task.runninghub_auto_retry_after is not None

        # The shared FIFO must not claim a retry before its backoff expires.
        assert task_worker.claim_next_pending_task(db) is None

        for retry_number in range(1, 4):
            task = db.get(GenerationTask, "failed-detail-task")
            task.runninghub_auto_retry_after = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            db.commit()
            assert (
                task_worker.claim_next_pending_task(db)
                == "failed-detail-task"
            )
            task_worker.process_task(db, "failed-detail-task")
            assert (
                db.get(GenerationTask, "failed-detail-task").status
                == TaskStatus.SUBMITTED.value
            )
            task_worker.process_task(db, "failed-detail-task")

            task = db.get(GenerationTask, "failed-detail-task")
            if retry_number < 3:
                assert task.status == TaskStatus.PENDING.value
                assert task.runninghub_auto_retry_count == retry_number + 1
            else:
                assert task.status == TaskStatus.FAILED.value
                assert task.runninghub_auto_retry_count == 3
                assert "已用完 3 次自动重试" in task.error_message
                assert task.runninghub_auto_retry_after is None
                assert len(json.loads(task.runninghub_attempt_history)) == 4

        # Once all retries are exhausted, later Worker passes are inert.
        task_worker.process_task(db, "failed-detail-task")

    assert fake.query_calls == 4
    assert fake.submissions == 3


def test_worker_recovers_unsubmitted_uploading_task():
    user = create_user("recover-user")
    _add_task(user.id, "recover-task", TaskStatus.UPLOADING.value)
    with SessionLocal() as db:
        assert task_worker.recover_interrupted_tasks(db) == 1
        assert db.get(GenerationTask, "recover-task").status == TaskStatus.PENDING.value


def test_worker_retries_safe_upload_network_failures_three_times(
    monkeypatch,
    caplog,
):
    user = create_user("upload-retry-worker-user")
    _add_task(user.id, "upload-retry-task", TaskStatus.PENDING.value)
    fake = FakeRunningHub(upload_error=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    caplog.set_level("WARNING", logger=task_worker.__name__)

    with SessionLocal() as db:
        for attempt in range(4):
            task_worker.process_task(db, "upload-retry-task")
            task = db.get(GenerationTask, "upload-retry-task")
            if attempt < 3:
                assert task.status == TaskStatus.PENDING.value
                assert task.runninghub_auto_retry_count == attempt + 1
                assert task.runninghub_auto_retry_after is not None
                assert f"第 {attempt + 1}/3 次自动重试" in task.error_message
                task.runninghub_auto_retry_after = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
                db.commit()
            else:
                assert task.status == TaskStatus.FAILED.value
                assert task.runninghub_auto_retry_count == 3
                assert task.runninghub_auto_retry_after is None
                assert "已用完 3 次自动重试" in task.error_message

    assert fake.upload_calls == 4
    assert fake.submissions == 0
    assert '"runninghub_operation":"asset_upload"' in caplog.text
    assert '"network_error_type":"ReadTimeout"' in caplog.text
    assert '"elapsed_ms":600001' in caplog.text
    assert "超过 RunningHub 官方建议的 30MB" in caplog.text
    assert '"asset_slot":"image"' in caplog.text


def test_worker_does_not_retry_ambiguous_submit_network_failure(
    monkeypatch,
):
    user = create_user("submit-network-worker-user")
    _add_task(user.id, "submit-network-task", TaskStatus.PENDING.value)
    fake = FakeRunningHub(submit_network_error=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)

    with SessionLocal() as db:
        task_worker.process_task(db, "submit-network-task")
        task = db.get(GenerationTask, "submit-network-task")
        assert task.status == TaskStatus.FAILED.value
        assert task.runninghub_auto_retry_count == 0
        assert task.runninghub_auto_retry_after is None
        assert task.runninghub_task_id is None

    assert fake.upload_calls == 2


def test_worker_claims_fifo_tasks_only_up_to_user_concurrency_limit():
    user = create_user("queue-slots-user")
    _add_task(user.id, "queue-task-1", TaskStatus.PENDING.value)
    _add_task(user.id, "queue-task-2", TaskStatus.PENDING.value)
    _add_task(user.id, "queue-task-3", TaskStatus.PENDING.value)

    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) == "queue-task-1"
        assert task_worker.claim_next_pending_task(db) == "queue-task-2"
        assert task_worker.claim_next_pending_task(db) is None
        first = db.get(GenerationTask, "queue-task-1")
        first.status = TaskStatus.SUCCESS.value
        db.commit()
        assert task_worker.claim_next_pending_task(db) == "queue-task-3"


def test_worker_submits_through_workflow_adapter(monkeypatch):
    user = create_user("adapter-worker-user")
    settings = get_settings()
    upload_dir = settings.uploads_dir / str(user.id) / "adapter-task"
    upload_dir.mkdir(parents=True)
    image = upload_dir / "image.png"
    audio = upload_dir / "audio.mp3"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {"prompt": "测试适配器", "start_time": "0:00", "end_time": "0:10"},
        {"audio_duration_seconds": 10},
    )
    input_payload = workflow.serialize_input(
        [
            WorkflowAsset("image", "image", to_relative_data_path(image, settings), "image.png"),
            WorkflowAsset("audio", "audio", to_relative_data_path(audio, settings), "audio.mp3"),
        ],
        parameters,
        {"audio_duration_seconds": 10},
    )
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id="adapter-task",
                user_id=user.id,
                workflow_type="digital_human",
                input_payload=json.dumps(input_payload, ensure_ascii=False),
                image_path=to_relative_data_path(image, settings),
                audio_path=to_relative_data_path(audio, settings),
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt="测试适配器",
                status=TaskStatus.PENDING.value,
            )
        )
        db.commit()

    fake = FakeRunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "adapter-task")
        task = db.get(GenerationTask, "adapter-task")
        assert task.runninghub_task_id == "submitted-remote-id"
        assert task.status == TaskStatus.SUBMITTED.value
    assert fake.submissions == 1
    assert fake.last_payload["instanceType"] == "plus"


def test_worker_keeps_task_pending_when_remote_account_is_full(monkeypatch):
    user = create_user("remote-capacity-full-user")
    settings = get_settings()
    upload_dir = settings.uploads_dir / str(user.id) / "capacity-task"
    upload_dir.mkdir(parents=True)
    image = upload_dir / "image.png"
    audio = upload_dir / "audio.mp3"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {"prompt": "容量测试", "start_time": "0:00", "end_time": "0:10"},
        {"audio_duration_seconds": 10},
    )
    input_payload = workflow.serialize_input(
        [
            WorkflowAsset(
                "image",
                "image",
                to_relative_data_path(image, settings),
                "image.png",
            ),
            WorkflowAsset(
                "audio",
                "audio",
                to_relative_data_path(audio, settings),
                "audio.mp3",
            ),
        ],
        parameters,
        {"audio_duration_seconds": 10},
    )
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id="capacity-task",
                user_id=user.id,
                workflow_type="digital_human",
                input_payload=json.dumps(input_payload, ensure_ascii=False),
                image_path=to_relative_data_path(image, settings),
                audio_path=to_relative_data_path(audio, settings),
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt="容量测试",
                status=TaskStatus.UPLOADING.value,
            )
        )
        db.commit()

    fake = FakeRunningHub(current_task_count=2)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "capacity-task")
        task = db.get(GenerationTask, "capacity-task")
        assert task.status == TaskStatus.PENDING.value
        assert task.error_code is None
        assert task.completed_at is None
    assert fake.capacity_checks == 1
    assert fake.upload_calls == 0
    assert fake.submissions == 0
    assert task_worker._capacity_check_is_deferred(user.id) is True
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None


def test_worker_requeues_submit_time_capacity_race(monkeypatch):
    user = create_user("submit-capacity-race-user")
    settings = get_settings()
    upload_dir = settings.uploads_dir / str(user.id) / "capacity-race-task"
    upload_dir.mkdir(parents=True)
    image = upload_dir / "image.png"
    audio = upload_dir / "audio.mp3"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    workflow = get_workflow("digital_human")
    parameters = workflow.validate_parameters(
        {"prompt": "竞态测试", "start_time": "0:00", "end_time": "0:10"},
        {"audio_duration_seconds": 10},
    )
    input_payload = workflow.serialize_input(
        [
            WorkflowAsset(
                "image",
                "image",
                to_relative_data_path(image, settings),
                "image.png",
            ),
            WorkflowAsset(
                "audio",
                "audio",
                to_relative_data_path(audio, settings),
                "audio.mp3",
            ),
        ],
        parameters,
        {"audio_duration_seconds": 10},
    )
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id="capacity-race-task",
                user_id=user.id,
                workflow_type="digital_human",
                input_payload=json.dumps(input_payload, ensure_ascii=False),
                image_path=to_relative_data_path(image, settings),
                audio_path=to_relative_data_path(audio, settings),
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt="竞态测试",
                status=TaskStatus.UPLOADING.value,
            )
        )
        db.commit()

    fake = FakeRunningHub(capacity_error=True)
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "capacity-race-task")
        task = db.get(GenerationTask, "capacity-race-task")
        assert task.status == TaskStatus.PENDING.value
        assert task.runninghub_task_id is None
        assert task.error_code is None
        assert task.completed_at is None
    assert fake.capacity_checks == 1
    assert fake.upload_calls == 2
    assert fake.submissions == 0
    assert task_worker._capacity_check_is_deferred(user.id) is True
    with SessionLocal() as db:
        assert task_worker.claim_next_pending_task(db) is None


def test_worker_submits_ltx_task_to_workflow_endpoint(monkeypatch):
    user = create_user("ltx-worker-user")
    settings = get_settings()
    upload_dir = settings.uploads_dir / str(user.id) / "ltx-worker-task"
    upload_dir.mkdir(parents=True)
    video = upload_dir / "video.mp4"
    audio = upload_dir / "audio.mp3"
    video.write_bytes(b"\x00\x00\x00\x18ftypisompayload")
    audio.write_bytes(b"ID3audio")
    workflow = get_workflow("ltx_lip_sync")
    parameters = workflow.validate_parameters(
        {"prompt": "女人用中文说：你好"},
        {"has_custom_audio": True},
    )
    input_payload = workflow.serialize_input(
        [
            WorkflowAsset("video", "video", to_relative_data_path(video, settings), "video.mp4"),
            WorkflowAsset("audio", "audio", to_relative_data_path(audio, settings), "audio.mp3"),
        ],
        parameters,
        {"has_custom_audio": True},
    )
    with SessionLocal() as db:
        db.add(
            WorkflowConfig(
                user_id=user.id,
                workflow_key="ltx_lip_sync",
                ai_app_id="2080551073030434817",
                instance_type="plus",
                default_prompt="女人用中文说：你好",
                is_enabled=True,
                settings_json=json.dumps(
                    {
                        "access_password_encrypted": encrypt_secret(
                            "private-workflow-password"
                        )
                    }
                ),
            )
        )
        db.add(
            GenerationTask(
                id="ltx-worker-task",
                user_id=user.id,
                workflow_type="ltx_lip_sync",
                input_payload=json.dumps(input_payload, ensure_ascii=False),
                image_path=to_relative_data_path(video, settings),
                audio_path=to_relative_data_path(audio, settings),
                image_original_name="video.mp4",
                audio_original_name="audio.mp3",
                audio_duration_seconds=0,
                start_seconds=0,
                end_seconds=0,
                prompt="女人用中文说：你好",
                status=TaskStatus.PENDING.value,
            )
        )
        db.commit()

    fake = FakeRunningHub()
    monkeypatch.setattr(task_worker, "_make_client", lambda config: fake)
    with SessionLocal() as db:
        task_worker.process_task(db, "ltx-worker-task")
        task = db.get(GenerationTask, "ltx-worker-task")
        assert task.runninghub_task_id == "submitted-remote-id"
    assert fake.submissions == 1
    assert fake.ai_app_id == "2080551073030434817"
    assert fake.submission_type == "workflow"
    assert fake.last_payload["instanceType"] == "plus"
    assert fake.last_payload["accessPassword"] == "private-workflow-password"
