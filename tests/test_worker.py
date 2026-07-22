from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus
from app.config import get_settings
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
        assert payload["instanceType"] == "plus"
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
