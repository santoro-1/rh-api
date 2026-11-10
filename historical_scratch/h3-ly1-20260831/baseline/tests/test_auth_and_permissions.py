from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from zipfile import ZipFile

from app.routes import operations
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GenerationTask,
    MiniMaxConfig,
    TaskStatus,
    SystemWorkflowConfig,
    User,
    WorkflowConfig,
)
from app.services.security import decrypt_secret
from app.services.storage import to_relative_data_path

from tests.conftest import create_user, login


def test_post_requires_csrf_token(client):
    create_user("csrf-user")
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text
    rejected = client.post(
        "/login",
        data={"username": "csrf-user", "password": "password123"},
    )
    assert rejected.status_code == 403


def test_healthcheck_reports_database_ready(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    assert client.get("/admin/operations").status_code == 403
    assert client.get("/admin/operations/updates").status_code == 403
    assert client.get("/admin/operations/logs/download").status_code == 403


def test_admin_can_view_operations_without_terminal(client):
    create_user("operations-admin", is_admin=True)
    login(client, "operations-admin")

    response = client.get("/admin/operations")

    assert response.status_code == 200
    assert "运行状态与日志" in response.text
    assert "语音 Worker" in response.text
    assert "视频 Worker" in response.text
    assert "下载最近 7 天日志" in response.text
    assert 'name="source_channel"' in response.text
    assert "旧网页" in response.text
    assert "新工作台" in response.text

    update = client.get(
        "/admin/operations/updates",
        params={
            "web": 0,
            "audio_worker": 0,
            "video_worker": 0,
            "launcher": 0,
        },
    )
    assert update.status_code == 200
    payload = update.json()
    assert set(payload) == {"services", "queue", "resources", "logs"}
    assert {"cpuPercent", "memory", "disk", "project", "ffmpeg"} <= set(
        payload["resources"]
    )
    assert set(payload["logs"]) == {
        "web",
        "audio_worker",
        "media_worker",
        "video_worker",
        "launcher",
    }

    filtered_update = client.get(
        "/admin/operations/updates",
        params={"source_channel": "new_workbench"},
    )
    assert filtered_update.status_code == 200


def test_production_operations_hide_local_launcher(client, monkeypatch):
    create_user("production-operations-admin", is_admin=True)
    login(client, "production-operations-admin")
    production_settings = replace(get_settings(), app_env="production")
    monkeypatch.setattr(
        operations,
        "get_settings",
        lambda: production_settings,
    )

    page = client.get("/admin/operations")
    assert page.status_code == 200
    assert "本地总控" not in page.text

    update = client.get("/admin/operations/updates")
    assert update.status_code == 200
    assert set(update.json()["services"]) == {
        "web",
        "audio_worker",
        "media_worker",
        "video_worker",
    }
    assert set(update.json()["logs"]) == {
        "web",
        "audio_worker",
        "media_worker",
        "video_worker",
    }


def test_admin_can_download_retained_service_logs(client):
    create_user("log-download-admin", is_admin=True)
    login(client, "log-download-admin")
    logs_dir = get_settings().logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "web.log").write_text(
        "2026-07-29 INFO web: started\n",
        encoding="utf-8",
    )
    (logs_dir / "audio_worker.log.2026-07-28").write_text(
        "2026-07-28 ERROR audio: failed\n",
        encoding="utf-8",
    )
    (logs_dir / "video_worker.log").write_text(
        (
            "2026-07-29 WARNING worker: [EVENT "
            "video.pre_submit_retry_scheduled] retry "
            '{"network_error_type":"ReadTimeout","elapsed_ms":600001}\n'
        ),
        encoding="utf-8",
    )
    (logs_dir / "unrelated.log").write_text(
        "must not be exported\n",
        encoding="utf-8",
    )

    response = client.get("/admin/operations/logs/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "runninghub-video-logs-" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "web/web.log",
            "audio_worker/audio_worker.log.2026-07-28",
            "video_worker/video_worker.log",
        }
        assert archive.read("web/web.log").decode("utf-8").splitlines()[-1].endswith(
            "started"
        )
        assert archive.read(
            "audio_worker/audio_worker.log.2026-07-28"
        ).decode("utf-8").splitlines()[-1].endswith("failed")
        video_log = archive.read(
            "video_worker/video_worker.log"
        ).decode("utf-8")
        assert '"network_error_type":"ReadTimeout"' in video_log
        assert '"elapsed_ms":600001' in video_log


def test_task_list_auto_refreshes_only_while_tasks_are_active(client):
    user = create_user("refresh-user")
    _make_task(user.id, "refresh-task")
    login(client, "refresh-user")

    active_page = client.get("/tasks")
    assert active_page.status_code == 200
    assert "setTimeout(refreshTaskList, 5000)" in active_page.text

    with SessionLocal() as db:
        task = db.get(GenerationTask, "refresh-task")
        task.status = TaskStatus.FAILED.value
        db.commit()

    terminal_page = client.get("/tasks")
    assert terminal_page.status_code == 200
    assert "setTimeout(refreshTaskList, 5000)" not in terminal_page.text


def test_admin_can_create_a_user_with_legacy_default_instance(client):
    create_user("admin", is_admin=True)
    login(client, "admin")
    response = client.post(
        "/admin/users",
        data={
            "username": "created-user",
            "password": "password123",
            "is_active": "true",
            "h3_access_enabled": "true",
            "api_key": "new-test-key",
            "base_url": "https://www.runninghub.cn",
            "ai_app_id": "2062251097452007426",
            "instance_type": "default",
            "default_prompt": "默认提示词",
            "max_concurrent_tasks": "1",
            "minimax_api_key": "new-minimax-key",
            "minimax_base_url": "https://api.minimax.io",
            "minimax_requests_per_minute": "20",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="created-user").one()
        assert user.runninghub_config is not None
        assert user.h3_access_enabled is True
        assert user.runninghub_config.instance_type == "default"
        assert len(user.runninghub_config.credential_fingerprint or "") == 64
        workflow_config = db.query(WorkflowConfig).filter_by(
            user_id=user.id, workflow_key="digital_human"
        ).one()
        assert workflow_config.ai_app_id == "2062251097452007426"
        assert workflow_config.instance_type == "default"
        ltx_config = db.query(WorkflowConfig).filter_by(
            user_id=user.id, workflow_key="ltx_lip_sync"
        ).one()
        assert ltx_config.ai_app_id == "2080551073030434817"
        assert ltx_config.is_enabled is False
        minimax_config = db.query(MiniMaxConfig).filter_by(user_id=user.id).one()
        assert (
            decrypt_secret(
                minimax_config.api_key_encrypted,
                label="MiniMax API Key",
            )
            == "new-minimax-key"
        )
        assert len(minimax_config.credential_fingerprint or "") == 64


def test_admin_can_delete_another_account_and_only_its_files(client):
    admin = create_user("delete-admin", is_admin=True)
    target = create_user("delete-target")
    survivor = create_user("delete-survivor")
    _make_task(target.id, "delete-target-terminal-task")
    with SessionLocal() as db:
        task = db.get(GenerationTask, "delete-target-terminal-task")
        task.status = TaskStatus.FAILED.value
        db.commit()
    settings = get_settings()
    target_directories = (
        settings.uploads_dir / str(target.id),
        settings.outputs_dir / str(target.id),
        settings.staged_assets_dir / str(target.id),
        settings.voice_sources_dir / str(target.id),
        settings.voice_creations_dir / str(target.id),
    )
    survivor_directory = settings.uploads_dir / str(survivor.id)
    for directory in target_directories:
        directory.mkdir(parents=True)
        (directory / "owned.txt").write_text("target", encoding="utf-8")
    survivor_directory.mkdir(parents=True)
    (survivor_directory / "keep.txt").write_text("survivor", encoding="utf-8")

    login(client, admin.username)
    response = client.post(
        f"/admin/users/{target.id}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    with SessionLocal() as db:
        assert db.get(User, target.id) is None
        assert db.get(User, survivor.id) is not None
    assert all(not directory.exists() for directory in target_directories)
    assert (survivor_directory / "keep.txt").read_text(encoding="utf-8") == "survivor"


def test_admin_cannot_delete_self_or_account_with_active_task(client):
    admin = create_user("protected-admin", is_admin=True)
    target = create_user("busy-target")
    _make_task(target.id, "busy-target-task")
    login(client, admin.username)

    self_delete = client.post(
        f"/admin/users/{admin.id}/delete",
        follow_redirects=False,
    )
    busy_delete = client.post(
        f"/admin/users/{target.id}/delete",
        follow_redirects=False,
    )

    assert self_delete.status_code == 409
    assert "不能删除当前登录" in self_delete.text
    assert busy_delete.status_code == 409
    assert "运行中任务" in busy_delete.text
    with SessionLocal() as db:
        assert db.get(User, admin.id) is not None
        assert db.get(User, target.id) is not None


def test_admin_encrypts_preserves_and_clears_shared_ltx_access_password(client):
    create_user("password-admin", is_admin=True)
    login(client, "password-admin")
    create_response = client.post(
        "/admin/runninghub-pool/workflows/ltx_lip_sync",
        data={
            "ai_app_id": "2080551073030434817",
            "instance_type": "plus",
            "default_prompt": "对口型提示词",
            "access_password": "private-workflow-password",
            "is_enabled": "true",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303

    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="ltx_lip_sync",
        ).one()
        settings = json.loads(config.settings_json or "{}")
        encrypted = settings["access_password_encrypted"]
        assert "private-workflow-password" not in (config.settings_json or "")
        assert (
            decrypt_secret(encrypted, label="视频对口型工作流访问密码")
            == "private-workflow-password"
        )

    edit_page = client.get("/admin/runninghub-pool/workflows")
    assert edit_page.status_code == 200
    assert "已加密保存，留空不修改" in edit_page.text
    assert "private-workflow-password" not in edit_page.text
    assert "视频对口型" in edit_page.text

    common_update = {
        "ai_app_id": "2080551073030434817",
        "instance_type": "plus",
        "default_prompt": "对口型提示词",
        "is_enabled": "true",
    }
    preserve_response = client.post(
        "/admin/runninghub-pool/workflows/ltx_lip_sync",
        data=common_update,
        follow_redirects=False,
    )
    assert preserve_response.status_code == 303
    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="ltx_lip_sync",
        ).one()
        assert json.loads(config.settings_json or "{}")[
            "access_password_encrypted"
        ] == encrypted

    clear_response = client.post(
        "/admin/runninghub-pool/workflows/ltx_lip_sync",
        data={**common_update, "clear_access_password": "true"},
        follow_redirects=False,
    )
    assert clear_response.status_code == 303
    with SessionLocal() as db:
        config = db.query(SystemWorkflowConfig).filter_by(
            workflow_key="ltx_lip_sync",
        ).one()
        assert "access_password_encrypted" not in json.loads(
            config.settings_json or "{}"
        )


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
