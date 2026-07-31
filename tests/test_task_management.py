from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus
from app.services.storage import task_output_dir, task_upload_dir
from scripts import cleanup_files
from tests.conftest import create_user, login


def _create_digital_task(client, monkeypatch, username: str) -> str:
    create_user(username)
    login(client, username)
    monkeypatch.setattr(
        "app.routes.tasks.inspect_audio_duration",
        lambda path: 10.0,
    )
    return _submit_digital_task(client)


def _submit_digital_task(client) -> str:
    response = client.post(
        "/api/tasks",
        data={
            "startTime": "0:00",
            "endTime": "0:10",
            "prompt": "任务管理测试",
        },
        files={
            "image": (
                "person.png",
                b"\x89PNG\r\n\x1a\npayload",
                "image/png",
            ),
            "audio": (
                "voice.mp3",
                b"ID3audio-payload",
                "audio/mpeg",
            ),
        },
    )
    assert response.status_code == 201
    return response.json()["taskId"]


def _mark_task(
    task_id: str,
    status: str,
    *,
    remote_id: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        task.status = status
        task.runninghub_task_id = remote_id
        task.error_code = status
        task.error_message = "测试失败"
        task.completed_at = completed_at or datetime.now(timezone.utc)
        db.commit()


def test_failed_task_can_retry_with_saved_uploads(client, monkeypatch):
    task_id = _create_digital_task(client, monkeypatch, "retry-user")
    _mark_task(task_id, TaskStatus.FAILED.value, remote_id="old-remote-id")
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        task.runninghub_failed_reason = json.dumps(
            {
                "exception_type": "torch.OutOfMemoryError",
                "node_name": "WanVideoEncode",
                "node_id": "298",
                "exception_message": "显存不足",
            },
            ensure_ascii=False,
        )
        task.runninghub_auto_retry_count = 3
        task.runninghub_auto_retry_after = datetime.now(timezone.utc)
        db.commit()

    history = client.get("/tasks")
    assert history.status_code == 200
    assert f'action="/tasks/{task_id}/retry"' in history.text
    assert f'action="/tasks/{task_id}/delete"' in history.text
    assert "生成新的 RunningHub 任务 ID" in history.text

    detail = client.get(f"/tasks/{task_id}")
    assert detail.status_code == 200
    assert "查看 RunningHub 原始失败详情" in detail.text
    assert "WanVideoEncode" in detail.text
    status = client.get(f"/api/tasks/{task_id}")
    assert status.status_code == 200
    assert status.json()["failedReason"]["node_id"] == "298"
    assert status.json()["runninghubTaskId"] == "old-remote-id"
    assert status.json()["autoRetryCount"] == 3
    assert status.json()["autoRetryLimit"] == 3
    assert status.json()["autoRetryAfter"] is not None

    response = client.post(
        f"/tasks/{task_id}/retry",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/tasks/{task_id}"
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.PENDING.value
        assert task.runninghub_task_id is None
        assert task.runninghub_submitted_at is None
        assert task.error_code is None
        assert task.error_message is None
        assert task.runninghub_failed_reason is None
        assert task.runninghub_auto_retry_count == 0
        assert task.runninghub_auto_retry_after is None
        assert task.completed_at is None


def test_failed_task_retry_rejects_cleaned_uploads(client, monkeypatch):
    task_id = _create_digital_task(client, monkeypatch, "missing-upload-user")
    _mark_task(task_id, TaskStatus.FAILED.value)
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        image_path = settings.data_dir / task.image_path
    image_path.unlink()

    response = client.post(
        f"/tasks/{task_id}/retry",
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "原上传素材已清理" in response.text


def test_download_failure_retry_keeps_remote_task(client, monkeypatch):
    task_id = _create_digital_task(client, monkeypatch, "download-retry-user")
    _mark_task(
        task_id,
        TaskStatus.DOWNLOAD_FAILED.value,
        remote_id="successful-remote-id",
    )

    response = client.post(
        f"/tasks/{task_id}/retry",
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        assert task.status == TaskStatus.RUNNING.value
        assert task.runninghub_task_id == "successful-remote-id"
        assert task.error_message is None
        assert task.completed_at is None


def test_terminal_task_delete_removes_record_and_files(client, monkeypatch):
    task_id = _create_digital_task(client, monkeypatch, "delete-user")
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, task_id)
        user_id = task.user_id
    output_dir = task_output_dir(settings, user_id, task_id)
    output_dir.mkdir(parents=True)
    (output_dir / "partial.mp4").write_bytes(b"partial")

    active_response = client.post(
        f"/tasks/{task_id}/delete",
        follow_redirects=False,
    )
    assert active_response.status_code == 409

    _mark_task(task_id, TaskStatus.FAILED.value)
    delete_response = client.post(
        f"/tasks/{task_id}/delete",
        follow_redirects=False,
    )
    assert delete_response.status_code == 303
    assert delete_response.headers["location"] == "/tasks"
    with SessionLocal() as db:
        assert db.get(GenerationTask, task_id) is None
    assert not task_upload_dir(settings, user_id, task_id).exists()
    assert not output_dir.exists()


def test_other_user_cannot_retry_or_delete_task(client, monkeypatch):
    task_id = _create_digital_task(client, monkeypatch, "task-owner")
    _mark_task(task_id, TaskStatus.FAILED.value)
    client.post("/logout")
    create_user("unrelated-user")
    login(client, "unrelated-user")

    assert client.post(f"/tasks/{task_id}/retry").status_code == 404
    assert client.post(f"/tasks/{task_id}/delete").status_code == 404
    assert client.post(
        "/tasks/bulk-delete",
        data={"task_ids": [task_id]},
    ).status_code == 404


def test_task_history_filters_by_beijing_date(client, monkeypatch):
    first_id = _create_digital_task(client, monkeypatch, "date-filter-user")
    second_id = _submit_digital_task(client)
    third_id = _submit_digital_task(client)
    fourth_id = _submit_digital_task(client)

    with SessionLocal() as db:
        timestamps = {
            first_id: datetime(2026, 7, 26, 15, 59, tzinfo=timezone.utc),
            second_id: datetime(2026, 7, 26, 16, 0, tzinfo=timezone.utc),
            third_id: datetime(2026, 7, 27, 15, 59, tzinfo=timezone.utc),
            fourth_id: datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc),
        }
        for task_id, created_at in timestamps.items():
            db.get(GenerationTask, task_id).created_at = created_at
        db.commit()

    response = client.get(
        "/tasks",
        params={"start_date": "2026-07-27", "end_date": "2026-07-27"},
    )
    assert response.status_code == 200
    assert f'value="{first_id}"' not in response.text
    assert f'value="{second_id}"' in response.text
    assert f'value="{third_id}"' in response.text
    assert f'value="{fourth_id}"' not in response.text
    assert "2026-07-27 00:00" in response.text
    assert client.get(
        "/tasks",
        params={"start_date": "2026-07-28", "end_date": "2026-07-27"},
    ).status_code == 400


def test_bulk_delete_removes_selected_terminal_tasks(client, monkeypatch):
    first_id = _create_digital_task(client, monkeypatch, "bulk-delete-user")
    second_id = _submit_digital_task(client)
    active_id = _submit_digital_task(client)
    _mark_task(first_id, TaskStatus.FAILED.value)
    _mark_task(second_id, TaskStatus.SUCCESS.value)

    settings = get_settings()
    with SessionLocal() as db:
        first = db.get(GenerationTask, first_id)
        second = db.get(GenerationTask, second_id)
        first_dir = task_upload_dir(settings, first.user_id, first.id)
        second_dir = task_upload_dir(settings, second.user_id, second.id)

    history = client.get("/tasks")
    assert 'action="/tasks/bulk-delete"' in history.text
    assert history.text.count('class="task-select"') == 3

    response = client.post(
        "/tasks/bulk-delete",
        data={
            "task_ids": [first_id, second_id],
            "start_date": "2026-07-01",
            "end_date": "2026-07-31",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/tasks?start_date=2026-07-01&end_date=2026-07-31"
    )
    with SessionLocal() as db:
        assert db.get(GenerationTask, first_id) is None
        assert db.get(GenerationTask, second_id) is None
        assert db.get(GenerationTask, active_id) is not None
    assert not first_dir.exists()
    assert not second_dir.exists()


def test_bulk_delete_rejects_active_task_atomically(client, monkeypatch):
    terminal_id = _create_digital_task(client, monkeypatch, "bulk-active-user")
    active_id = _submit_digital_task(client)
    _mark_task(terminal_id, TaskStatus.FAILED.value)

    response = client.post(
        "/tasks/bulk-delete",
        data={"task_ids": [terminal_id, active_id]},
        follow_redirects=False,
    )
    assert response.status_code == 409
    with SessionLocal() as db:
        assert db.get(GenerationTask, terminal_id) is not None
        assert db.get(GenerationTask, active_id) is not None


def test_cleanup_keeps_uploads_until_48_hours_after_terminal_state(
    client,
    monkeypatch,
):
    recent_id = _create_digital_task(client, monkeypatch, "recent-cleanup-user")
    _mark_task(
        recent_id,
        TaskStatus.FAILED.value,
        completed_at=datetime.now(timezone.utc) - timedelta(hours=47),
    )

    client.post("/logout")
    expired_id = _create_digital_task(client, monkeypatch, "expired-cleanup-user")
    _mark_task(
        expired_id,
        TaskStatus.FAILED.value,
        completed_at=datetime.now(timezone.utc) - timedelta(hours=49),
    )

    settings = get_settings()
    with SessionLocal() as db:
        recent = db.get(GenerationTask, recent_id)
        expired = db.get(GenerationTask, expired_id)
        recent_dir = task_upload_dir(settings, recent.user_id, recent.id)
        expired_dir = task_upload_dir(settings, expired.user_id, expired.id)

    assert settings.upload_retention_days == 2
    assert recent_dir.exists()
    assert expired_dir.exists()
    cleanup_files.main()
    assert recent_dir.exists()
    assert not expired_dir.exists()
