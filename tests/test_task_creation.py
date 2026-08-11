from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus, User
from app.services.workflow_configs import save_workflow_config

from tests.conftest import create_user, login


def test_generate_page_uses_fixed_stable_mode_v2(client):
    create_user("fixed-mode-user")
    login(client, "fixed-mode-user")
    response = client.get("/generate")
    assert response.status_code == 200
    assert "稳定模式 v2" in response.text
    assert 'name="overallMode"' not in response.text
    assert 'data-workflow="digital_human"' in response.text
    assert 'data-workflow="ltx_lip_sync"' in response.text
    assert 'id="task-form"' in response.text
    assert 'id="ltx-task-form"' in response.text
    assert 'id="task-instance-type"' in response.text
    assert "Stand 运行（24G）" in response.text
    assert "Plus 运行（48G）" in response.text
    assert "数字人 API 文档支持" not in response.text
    assert "LTX 2.3 对口型" not in response.text
    assert "视频对口型" in response.text
    assert "任务统一处理" in response.text
    assert "后台 Worker" not in response.text


def test_old_ltx_page_redirects_to_unified_generate_page(client):
    create_user("unified-generate-user")
    login(client, "unified-generate-user")
    response = client.get("/generate/ltx-lip-sync", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/generate?workflow=ltx_lip_sync"


def test_audio_inspection_rounds_fractional_duration_up(client, monkeypatch):
    create_user("duration-rounding-user")
    login(client, "duration-rounding-user")
    monkeypatch.setattr(
        "app.routes.tasks.inspect_audio_duration",
        lambda path: 28.1,
    )

    response = client.post(
        "/api/audio/inspect",
        files={
            "audio": (
                "fractional.mp3",
                b"ID3audio-payload",
                "audio/mpeg",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["durationSeconds"] == 28.1
    assert response.json()["durationText"] == "0:29"
    assert response.json()["suggestedEndTime"] == "0:29"


def test_direct_audio_inspection_rejects_more_than_45_seconds(
    client,
    monkeypatch,
):
    create_user("long-audio-user")
    login(client, "long-audio-user")
    monkeypatch.setattr(
        "app.routes.tasks.inspect_audio_duration",
        lambda path: 45.5,
    )
    response = client.post(
        "/api/audio/inspect",
        files={
            "audio": (
                "long.mp3",
                b"ID3audio-payload",
                "audio/mpeg",
            )
        },
    )
    assert response.status_code == 400
    assert "不能超过 35 秒" in response.json()["detail"]


def test_task_creation_returns_immediately_without_calling_runninghub(client, monkeypatch):
    create_user("creator")
    login(client, "creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 15.5)
    response = client.post(
        "/api/tasks",
        data={"startTime": "0:00", "endTime": "0:15", "prompt": "自定义提示词"},
        files={
            "image": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("voice.mp3", b"ID3audio-payload", "audio/mpeg"),
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "PENDING"
    with SessionLocal() as db:
        task = db.get(GenerationTask, body["taskId"])
        assert task is not None
        assert task.status == TaskStatus.PENDING.value
        assert task.runninghub_task_id is None
        assert task.workflow_type == "digital_human"
        payload = json.loads(task.input_payload)
        assert payload["assets"]["image"]["kind"] == "image"
        assert payload["assets"]["audio"]["kind"] == "audio"
        assert payload["parameters"]["resolution"] == "1024"
        assert payload["parameters"]["person_mode"] == "1"
        assert payload["parameters"]["instance_type"] == "plus"
        assert "overall_mode" not in payload["parameters"]
        assert "left_audio" not in payload["assets"]
        assert "right_audio" not in payload["assets"]


def test_digital_human_task_uses_plus_instance(client, monkeypatch):
    create_user("digital-plus-creator")
    login(client, "digital-plus-creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 15.5)
    response = client.post(
        "/api/tasks",
        data={
            "startTime": "0:00",
            "endTime": "0:15",
            "prompt": "Plus 测试",
            "instanceType": "plus",
        },
        files={
            "image": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("voice.mp3", b"ID3audio-payload", "audio/mpeg"),
        },
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        task = db.get(GenerationTask, response.json()["taskId"])
        payload = json.loads(task.input_payload)
        assert payload["parameters"]["instance_type"] == "plus"


def test_tasks_beyond_concurrency_limit_are_saved_for_queue(client, monkeypatch):
    user = create_user("queued-creator")
    login(client, "queued-creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 15.5)
    task_ids = []
    for index in range(8):
        response = client.post(
            "/api/tasks",
            data={
                "startTime": "0:00",
                "endTime": "0:15",
                "prompt": f"排队任务 {index}",
            },
            files={
                "image": (
                    f"person-{index}.png",
                    b"\x89PNG\r\n\x1a\npayload",
                    "image/png",
                ),
                "audio": (
                    f"voice-{index}.mp3",
                    b"ID3audio-payload",
                    "audio/mpeg",
                ),
            },
        )
        assert response.status_code == 201
        task_ids.append(response.json()["taskId"])

    with SessionLocal() as db:
        queued = (
            db.query(GenerationTask)
            .filter(
                GenerationTask.user_id == user.id,
                GenerationTask.status == TaskStatus.PENDING.value,
            )
            .count()
        )
    assert queued == 8
    assert len(set(task_ids)) == 8


def test_dual_person_task_is_disabled(client, monkeypatch):
    create_user("dual-creator")
    login(client, "dual-creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 15.5)
    response = client.post(
        "/api/tasks",
        data={
            "startTime": "0:00",
            "endTime": "0:15",
            "prompt": "双人对话",
            "resolution": "2048",
            "personMode": "0",
        },
        files={
            "image": ("people.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("reference.mp3", b"ID3reference", "audio/mpeg"),
            "leftAudio": ("left.mp3", b"ID3left", "audio/mpeg"),
            "rightAudio": ("right.mp3", b"ID3right", "audio/mpeg"),
        },
    )
    assert response.status_code == 400
    assert "双人数字人模式暂未开放" in response.json()["detail"]


def test_dual_person_task_requires_both_person_audio_files(client, monkeypatch):
    create_user("incomplete-dual-creator")
    login(client, "incomplete-dual-creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 15.5)
    response = client.post(
        "/api/tasks",
        data={
            "startTime": "0:00",
            "endTime": "0:15",
            "prompt": "双人对话",
            "personMode": "0",
        },
        files={
            "image": ("people.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("reference.mp3", b"ID3reference", "audio/mpeg"),
            "leftAudio": ("left.mp3", b"ID3left", "audio/mpeg"),
        },
    )
    assert response.status_code == 400
    assert "双人数字人模式暂未开放" in response.json()["detail"]


def test_task_creation_rejects_invalid_time_range(client, monkeypatch):
    create_user("creator")
    login(client, "creator")
    monkeypatch.setattr("app.routes.tasks.inspect_audio_duration", lambda path: 10.0)
    response = client.post(
        "/api/tasks",
        data={"startTime": "0:10", "endTime": "0:10", "prompt": "提示词"},
        files={
            "image": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("voice.mp3", b"ID3audio-payload", "audio/mpeg"),
        },
    )
    assert response.status_code == 400


def _enable_ltx_workflow(username: str) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = save_workflow_config(
            user,
            "ltx_lip_sync",
            ai_app_id="2080551073030434817",
            instance_type="plus",
            default_prompt="女人用中文说：你好",
            is_enabled=True,
        )
        db.add(config)
        db.commit()


def test_ltx_task_creation_requires_custom_audio(client):
    create_user("ltx-custom-creator")
    _enable_ltx_workflow("ltx-custom-creator")
    login(client, "ltx-custom-creator")
    response = client.post(
        "/api/tasks/ltx-lip-sync",
        data={"prompt": "自定义配音", "instanceType": "default"},
        files={
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisompayload",
                "video/mp4",
            )
        },
    )
    assert response.status_code == 400
    assert "必须上传自定义音频" in response.json()["detail"]


def test_ltx_task_creation_saves_custom_audio(client, monkeypatch):
    create_user("ltx-audio-creator")
    _enable_ltx_workflow("ltx-audio-creator")
    login(client, "ltx-audio-creator")
    monkeypatch.setattr(
        "app.routes.tasks.inspect_audio_duration",
        lambda path: 15.5,
    )
    response = client.post(
        "/api/tasks/ltx-lip-sync",
        data={"prompt": "自定义配音", "instanceType": "default"},
        files={
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisompayload",
                "video/mp4",
            ),
            "customAudio": ("voice.mp3", b"ID3audio", "audio/mpeg"),
        },
    )
    assert response.status_code == 201
    with SessionLocal() as db:
        task = db.get(GenerationTask, response.json()["taskId"])
        payload = json.loads(task.input_payload)
        assert set(payload["assets"]) == {"video", "audio"}
        assert payload["parameters"] == {
            "prompt": "自定义配音",
            "instance_type": "default",
        }
        assert task.audio_original_name == "voice.mp3"
