from __future__ import annotations

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus, User, WorkflowConfig
from app.services.storage import to_relative_data_path

from tests.conftest import create_user, login


def _make_task(owner_id: int, task_id: str, *, result_path: str | None = None) -> None:
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id=task_id,
                user_id=owner_id,
                image_path="uploads/1/task/image.png",
                audio_path="uploads/1/task/audio.mp3",
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt="测试",
                status=TaskStatus.SUCCESS.value if result_path else TaskStatus.PENDING.value,
                result_path=result_path,
            )
        )
        db.commit()


def test_login_and_admin_permission(client):
    create_user("normal")
    login(client, "normal")
    assert client.get("/generate").status_code == 200
    assert client.get("/admin/users").status_code == 403


def test_admin_can_create_a_user_and_configure_default_plus(client):
    create_user("admin", is_admin=True)
    login(client, "admin")
    response = client.post(
        "/admin/users",
        data={
            "username": "created-user",
            "password": "password123",
            "is_active": "true",
            "api_key": "new-test-key",
            "base_url": "https://www.runninghub.cn",
            "ai_app_id": "2062251097452007426",
            "instance_type": "plus",
            "default_prompt": "默认提示词",
            "max_concurrent_tasks": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="created-user").one()
        assert user.runninghub_config is not None
        assert user.runninghub_config.instance_type == "plus"
        workflow_config = db.query(WorkflowConfig).filter_by(
            user_id=user.id, workflow_key="digital_human"
        ).one()
        assert workflow_config.ai_app_id == "2062251097452007426"
        assert workflow_config.instance_type == "plus"


def test_task_isolation_and_admin_visibility(client):
    alice = create_user("alice")
    create_user("bob")
    create_user("admin", is_admin=True)
    _make_task(alice.id, "task-alice")

    login(client, "bob")
    assert client.get("/api/tasks/task-alice").status_code == 404
    client.post("/logout")

    login(client, "admin")
    assert client.get("/api/tasks/task-alice").status_code == 200


def test_download_requires_owner_and_rejects_path_traversal(client):
    alice = create_user("alice")
    create_user("bob")
    settings = get_settings()
    video = settings.data_dir / "outputs" / str(alice.id) / "safe" / "result.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake video")
    _make_task(alice.id, "task-video", result_path=to_relative_data_path(video, settings))
    _make_task(alice.id, "task-traversal", result_path="../../outside.mp4")

    login(client, "bob")
    assert client.get("/api/tasks/task-video/download").status_code == 404
    client.post("/logout")
    login(client, "alice")
    assert client.get("/api/tasks/task-video/download").status_code == 200
    assert client.get("/api/tasks/task-traversal/download").status_code == 404
