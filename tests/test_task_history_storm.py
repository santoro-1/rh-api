from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus
from app.services.storage import to_relative_data_path
from tests.conftest import create_user, login


def _add_task(
    owner_id: int,
    task_id: str,
    *,
    created_at: datetime,
    status: str = TaskStatus.FAILED.value,
    image_path: str = "uploads/missing/image.png",
    result_path: str | None = None,
    error_message: str | None = None,
) -> None:
    with SessionLocal() as db:
        db.add(
            GenerationTask(
                id=task_id,
                user_id=owner_id,
                image_path=image_path,
                audio_path="uploads/missing/audio.mp3",
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=10,
                start_seconds=0,
                end_seconds=10,
                prompt=f"prompt-{task_id}",
                status=status,
                result_path=result_path,
                error_message=error_message,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        db.commit()


def test_tasks_page_is_paginated_and_missing_images_do_not_create_requests(client):
    user = create_user("history-page-user")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for index in range(25):
        _add_task(
            user.id,
            f"history-task-{index:02d}",
            created_at=started_at + timedelta(minutes=index),
        )
    login(client, user.username)

    first_page = client.get("/tasks")

    assert first_page.status_code == 200
    assert first_page.text.count('class="task-card"') == 20
    assert "history-task-24" in first_page.text
    assert "history-task-05" in first_page.text
    assert "history-task-04" not in first_page.text
    assert "/api/tasks/history-task-24/image" not in first_page.text
    assert "暂无参考图" in first_page.text
    assert "共 25 条，第 1 / 2 页" in first_page.text
    assert 'rel="next" href="/tasks?page=2"' in first_page.text

    second_page = client.get("/tasks?page=2")

    assert second_page.status_code == 200
    assert second_page.text.count('class="task-card"') == 5
    assert "history-task-04" in second_page.text
    assert "history-task-05" not in second_page.text
    assert 'rel="prev" href="/tasks"' in second_page.text


def test_tasks_page_accepts_fifty_and_rejects_unbounded_page_sizes(client):
    user = create_user("history-page-size-user")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for index in range(25):
        _add_task(
            user.id,
            f"page-size-task-{index:02d}",
            created_at=started_at + timedelta(minutes=index),
        )
    login(client, user.username)

    page = client.get("/tasks?page_size=50")
    assert page.status_code == 200
    assert page.text.count('class="task-card"') == 25
    assert client.get("/tasks?page_size=100").status_code == 400
    assert client.get("/tasks?page=0").status_code == 400


def test_tasks_page_only_emits_lazy_image_for_existing_safe_file(client):
    user = create_user("history-image-user")
    settings = get_settings()
    image = settings.data_dir / "uploads" / str(user.id) / "existing" / "image.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"not-a-real-png")
    _add_task(
        user.id,
        "existing-image-task",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        image_path=to_relative_data_path(image, settings),
    )
    _add_task(
        user.id,
        "unsafe-image-task",
        created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        image_path="../../outside.png",
    )
    login(client, user.username)

    page = client.get("/tasks")

    assert page.status_code == 200
    assert '/api/tasks/existing-image-task/image' in page.text
    assert 'loading="lazy"' in page.text
    assert 'decoding="async"' in page.text
    assert '/api/tasks/unsafe-image-task/image' not in page.text

    image_response = client.get("/api/tasks/existing-image-task/image")
    assert image_response.status_code == 200
    assert image_response.headers["cache-control"] == "private, max-age=3600"


def test_task_statuses_are_batched_authorized_and_not_cached(client):
    owner = create_user("history-status-owner")
    other = create_user("history-status-other")
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    _add_task(
        owner.id,
        "visible-active-task",
        created_at=now,
        status=TaskStatus.RUNNING.value,
    )
    _add_task(
        other.id,
        "hidden-active-task",
        created_at=now,
        status=TaskStatus.RUNNING.value,
    )
    login(client, owner.username)

    response = client.get(
        "/api/tasks/statuses?ids=visible-active-task,hidden-active-task"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "tasks": [
            {
                "taskId": "visible-active-task",
                "status": "RUNNING",
                "statusLabel": "正在生成",
                "errorMessage": None,
                "downloadAvailable": False,
                "downloadUrl": None,
                "updatedAt": now.replace(tzinfo=None).isoformat(),
            }
        ]
    }

    too_many_ids = ",".join(f"task-{index}" for index in range(51))
    assert client.get(f"/api/tasks/statuses?ids={too_many_ids}").status_code == 400


def test_status_polling_updates_page_without_periodic_full_reload(client):
    user = create_user("history-poll-user")
    _add_task(
        user.id,
        "poll-active-task",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        status=TaskStatus.PENDING.value,
    )
    login(client, user.username)

    page = client.get("/tasks")

    assert page.status_code == 200
    assert "/api/tasks/statuses?ids=" in page.text
    assert 'document.visibilityState === "hidden"' in page.text
    assert "taskPollController" in page.text
    assert "retryDelays = [5000, 10000, 20000, 30000]" in page.text
    assert "location.reload()" not in page.text


def test_batch_polling_is_single_flight_visibility_aware_and_backed_off():
    template = (
        Path(__file__).resolve().parents[1] / "app" / "templates" / "batch_detail.html"
    ).read_text(encoding="utf-8")

    assert "if (pollController" in template
    assert 'document.visibilityState === "hidden"' in template
    assert "pollController.abort()" in template
    assert "retryDelays = [5000, 10000, 20000, 30000]" in template
    assert "location.reload()" not in template
