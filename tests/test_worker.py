from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus, WorkflowConfig
from app.config import get_settings
from app.services.security import encrypt_secret
from app.services.storage import to_relative_data_path
from app.workers import task_worker
from app.workflows import get_workflow
from app.workflows.base import WorkflowAsset

from tests.conftest import create_user


class FakeRunningHub:
    def __init__(self):
        self.submissions = 0
        self.query_calls = 0

    def upload_file(self, path):
        return "openapi/" + path.name

    def submit_task(self, payload):
        self.submissions += 1
        self.last_payload = payload
        assert payload["instanceType"] in {"default", "plus"}
        assert payload["usePersonalQueue"] is False
        assert "retainSeconds" not in payload
        return "submitted-remote-id"

    def query_task(self, task_id):
        self.query_calls += 1
        return {"taskId": task_id, "status": "RUNNING", "usage": None}


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
