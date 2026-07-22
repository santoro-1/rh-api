from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus

from tests.conftest import create_user, login


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
        assert payload["parameters"]["overall_mode"] == "2"
        assert payload["parameters"]["person_mode"] == "1"
        assert "left_audio" not in payload["assets"]
        assert "right_audio" not in payload["assets"]


def test_dual_person_task_persists_both_person_audio_files(client, monkeypatch):
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
            "overallMode": "1",
            "personMode": "0",
        },
        files={
            "image": ("people.png", b"\x89PNG\r\n\x1a\npayload", "image/png"),
            "audio": ("reference.mp3", b"ID3reference", "audio/mpeg"),
            "leftAudio": ("left.mp3", b"ID3left", "audio/mpeg"),
            "rightAudio": ("right.mp3", b"ID3right", "audio/mpeg"),
        },
    )
    assert response.status_code == 201
    with SessionLocal() as db:
        task = db.get(GenerationTask, response.json()["taskId"])
        payload = json.loads(task.input_payload)
        assert payload["parameters"]["resolution"] == "2048"
        assert payload["parameters"]["overall_mode"] == "1"
        assert payload["parameters"]["person_mode"] == "0"
        assert payload["assets"]["left_audio"]["original_name"] == "left.mp3"
        assert payload["assets"]["right_audio"]["original_name"] == "right.mp3"


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
    assert "左边人物音频和右边人物音频" in response.json()["detail"]


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
