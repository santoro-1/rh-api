from __future__ import annotations

import json
from io import BytesIO
from zipfile import ZipFile

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_MULTI_CAMERA_WEB,
    GenerationBatch,
    GenerationTask,
    MultiCameraUserAccess,
    TaskStatus,
    User,
)
from app.services.multi_camera import camera_sequence, plan_segments
from app.services.storage import to_relative_data_path
from tests.conftest import create_user, login


def _stage(client, kind: str, name: str, content: bytes, mime: str) -> str:
    response = client.post(
        "/api/multi-camera/assets",
        data={"kind": kind},
        files={"file": (name, content, mime)},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


def _payload(image_ids: list[str], audio_ids: list[str]) -> dict:
    return {
        "name": "多机位批量测试",
        "requestKey": "multi-camera-test-request",
        "prompt": "人物自然地说话，镜头稳定。",
        "resolution": "1024",
        "seedvr2Enabled": False,
        "groups": [
            {
                "clientKey": "group-a",
                "name": "客厅三机位",
                "imageAssetIds": image_ids,
            }
        ],
        "rows": [
            {
                "rowKey": f"audio-{index:04d}",
                "audioAssetId": audio_id,
                "groupClientKey": "group-a",
            }
            for index, audio_id in enumerate(audio_ids, start=1)
        ],
    }


def _fake_cut(_source, target, **_kwargs):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ID3segment")


def test_segment_planner_honors_twenty_second_ceiling_and_avoids_tiny_tail():
    short = plan_segments(19.8, [10.0])
    assert [(part.start_seconds, part.end_seconds) for part in short] == [(0.0, 19.8)]

    plans = plan_segments(47.8, [15.7, 32.1, 39.5])
    assert len(plans) == 3
    assert plans[0].start_seconds == 0
    assert plans[-1].end_seconds == 47.8
    assert all(part.duration_seconds <= 20.01 for part in plans)
    assert all(part.duration_seconds >= 4 for part in plans)
    assert plans[0].end_seconds == 15.7
    assert plans[1].end_seconds == 32.1


def test_camera_sequences_match_confirmed_rules_and_restart_per_audio():
    assert camera_sequence(1, 5) == [1, 1, 1, 1, 1]
    assert camera_sequence(2, 7) == [1, 2, 1, 2, 1, 2, 1]
    assert camera_sequence(3, 18) == [
        1,
        2,
        3,
        1,
        3,
        2,
        2,
        1,
        3,
        2,
        3,
        1,
        3,
        2,
        1,
        3,
        1,
        2,
    ]
    assert camera_sequence(3, 4) == camera_sequence(3, 4)
    assert camera_sequence(4, 12) == [1, 2, 3, 4, 1, 2, 4, 3, 1, 3, 2, 4]


@pytest.mark.parametrize("username", ["admin", "Cx_ceshi"])
def test_only_controlled_account_names_are_bootstrapped_to_user_id_grants(
    client, username
):
    user = create_user(username, is_admin=username == "admin")
    login(client, username)
    page = client.get("/generate/multi-camera")
    assert page.status_code == 200
    assert "多机位图片 + 单音频" in page.text
    with SessionLocal() as db:
        grant = db.get(MultiCameraUserAccess, user.id)
        assert grant is not None and grant.is_enabled is True


def test_unlisted_admin_cannot_open_page_or_use_api(client):
    create_user("another-admin", is_admin=True)
    login(client, "another-admin")
    assert client.get("/generate/multi-camera").status_code == 403
    response = client.post(
        "/api/multi-camera/assets",
        data={"kind": "image"},
        files={"file": ("person.png", b"\x89PNG\r\n\x1a\npayload", "image/png")},
    )
    assert response.status_code == 403


def test_multi_camera_batch_creates_standard_tasks_without_merge(client, monkeypatch):
    create_user("admin", is_admin=True)
    login(client, "admin")
    monkeypatch.setattr(
        "app.services.multi_camera.inspect_audio_duration", lambda _path: 41.0
    )
    monkeypatch.setattr(
        "app.services.multi_camera.detect_silence_midpoints",
        lambda _path: [13.5, 27.2],
    )
    monkeypatch.setattr("app.services.multi_camera.cut_audio_segment", _fake_cut)

    images = [
        _stage(
            client,
            "image",
            f"camera-{index}.png",
            b"\x89PNG\r\n\x1a\n" + bytes([index]),
            "image/png",
        )
        for index in range(1, 4)
    ]
    audios = [
        _stage(
            client,
            "audio",
            f"voice-{index}.mp3",
            b"ID3voice" + bytes([index]),
            "audio/mpeg",
        )
        for index in range(1, 3)
    ]
    payload = _payload(images, audios)

    preflight = client.post("/api/multi-camera/preflight", json=payload)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["totalSegments"] == 6
    assert [part["camera"] for part in preflight.json()["rows"][0]["segments"]] == [
        1,
        2,
        3,
    ]
    assert [part["camera"] for part in preflight.json()["rows"][1]["segments"]] == [
        1,
        2,
        3,
    ]

    created = client.post("/api/multi-camera/batches", json=payload)
    assert created.status_code == 201, created.text
    batch_id = created.json()["batchId"]
    assert "多机位批量测试" not in client.get("/batches").text
    duplicate = client.post("/api/multi-camera/batches", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "batchId": batch_id,
        "created": False,
        "statusUrl": f"/api/multi-camera/batches/{batch_id}",
    }

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, batch_id)
        assert batch is not None
        assert batch.source_channel == BATCH_SOURCE_MULTI_CAMERA_WEB
        assert len(batch.items) == 2
        assert all(item.merged_video_status == "NOT_APPLICABLE" for item in batch.items)
        assert all(len(item.segments) == 3 for item in batch.items)
        assert [
            segment.multi_camera_binding.camera_position
            for segment in batch.items[0].segments
        ] == [1, 2, 3]
        tasks = (
            db.query(GenerationTask)
            .filter(GenerationTask.segment_id.is_not(None))
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert len(tasks) == 6
        for task in tasks:
            task_payload = json.loads(task.input_payload)
            assert task.workflow_type == "digital_human"
            assert task.start_seconds == 0
            assert task.end_seconds <= 14
            assert set(task_payload["assets"]) == {"image", "audio"}
            assert task_payload["parameters"]["person_mode"] == "1"
            assert task_payload["parameters"]["resolution"] == "1024"
            assert task_payload["parameters"]["instance_type"] == "plus"
            assert task_payload["parameters"]["seedvr2_enabled"] is False

    status = client.get(f"/api/multi-camera/batches/{batch_id}")
    assert status.status_code == 200
    assert len(status.json()["rows"]) == 2
    assert status.json()["batch"]["childTotal"] == 6

    with SessionLocal() as db:
        task = db.query(GenerationTask).order_by(GenerationTask.created_at).first()
        original_payload = task.input_payload
        segment_id = task.segment_id
        task.status = TaskStatus.FAILED.value
        task.error_message = "mock failure"
        db.commit()
    retried = client.post(f"/api/multi-camera/segments/{segment_id}/retry")
    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        task = db.query(GenerationTask).filter_by(segment_id=segment_id).one()
        assert task.status == TaskStatus.PENDING.value
        assert task.input_payload == original_payload
        assert task.segment.multi_camera_binding.camera_position == 1


def test_ordered_download_contains_manifest_and_successful_clips(client, monkeypatch):
    create_user("Cx_ceshi")
    login(client, "Cx_ceshi")
    monkeypatch.setattr(
        "app.services.multi_camera.inspect_audio_duration", lambda _path: 39.0
    )
    monkeypatch.setattr(
        "app.services.multi_camera.detect_silence_midpoints", lambda _path: [19.0]
    )
    monkeypatch.setattr("app.services.multi_camera.cut_audio_segment", _fake_cut)
    images = [
        _stage(
            client, "image", f"c{i}.png", b"\x89PNG\r\n\x1a\n" + bytes([i]), "image/png"
        )
        for i in (1, 2)
    ]
    audio = _stage(client, "audio", "voice.mp3", b"ID3voice", "audio/mpeg")
    created = client.post("/api/multi-camera/batches", json=_payload(images, [audio]))
    assert created.status_code == 201, created.text
    batch_id = created.json()["batchId"]

    settings = get_settings()
    with SessionLocal() as db:
        tasks = db.query(GenerationTask).order_by(GenerationTask.created_at).all()
        for index, task in enumerate(tasks, start=1):
            output = settings.outputs_dir / str(task.user_id) / task.id / "result.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video" + bytes([index]))
            task.status = TaskStatus.SUCCESS.value
            task.result_path = to_relative_data_path(output, settings)
        db.commit()

    response = client.get(f"/api/multi-camera/batches/{batch_id}/download")
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names[0] == "manifest.json"
        assert names[1].endswith("001-camera-1.mp4")
        assert names[2].endswith("002-camera-2.mp4")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema"] == "runninghub.multi-camera-batch.v1"
        assert [part["camera"] for part in manifest["rows"][0]["segments"]] == [1, 2]
