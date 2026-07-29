from __future__ import annotations

import json

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
        query_not_found: bool = False,
    ):
        self.submissions = 0
        self.query_calls = 0
        self.upload_calls = 0
        self.capacity_checks = 0
        self.current_task_count = current_task_count
        self.capacity_error = capacity_error
        self.query_not_found = query_not_found

    def get_account_current_task_count(self):
        self.capacity_checks += 1
        return self.current_task_count

    def upload_file(self, path):
        self.upload_calls += 1
        return "openapi/" + path.name

    def submit_task(self, payload):
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
        if self.query_not_found:
            raise RunningHubError(
                "查询任务失败：Task not found | 任务不存在或已过期",
                error_code="1004",
            )
        return {"taskId": task_id, "status": "RUNNING", "usage": None}


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


def test_worker_recovers_unsubmitted_uploading_task():
    user = create_user("recover-user")
    _add_task(user.id, "recover-task", TaskStatus.UPLOADING.value)
    with SessionLocal() as db:
        assert task_worker.recover_interrupted_tasks(db) == 1
        assert db.get(GenerationTask, "recover-task").status == TaskStatus.PENDING.value


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
    assert fake.last_payload["instanceType"] == "default"


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
