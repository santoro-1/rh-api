from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationTask,
    EnhancementStatus,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    GenerationTaskEnhancement,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
)
from app.services.postproduction import (
    AUTO_POSTPROCESS,
    MANUAL_EDIT_REQUIRED,
    postproduction_manifest,
    postproduction_mode,
)
from app.services.batch_status import batch_detail_status, batch_query
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from tests.conftest import create_user, login


def _text_item(*, segment_count: int) -> tuple[str, str]:
    user = create_user(f"postproduction-text-{segment_count}")
    settings = get_settings()
    subtitle_file = settings.uploads_dir / str(user.id) / "captions.json"
    subtitle_file.parent.mkdir(parents=True, exist_ok=True)
    subtitle_file.write_text(
        json.dumps(
            [
                {"text": "第一句。", "start_seconds": 0.0, "end_seconds": 1.25},
                {"text": "第二句。", "start_seconds": 1.4, "end_seconds": 3.0},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    batch_id = f"postproduction-batch-{segment_count}"
    item_id = f"postproduction-item-{segment_count}"
    with SessionLocal() as db:
        fingerprint = credential_fingerprint("minimax-key")
        config = MiniMaxConfig(
            user_id=user.id,
            api_key_encrypted=encrypt_secret("minimax-key"),
            account_binding_id=f"binding-{segment_count}",
            credential_fingerprint=fingerprint,
        )
        voice = MiniMaxVoiceAsset(
            id=f"voice-{segment_count}",
            user_id=user.id,
            config=config,
            account_binding_id=f"binding-{segment_count}",
            voice_id=f"voice-id-{segment_count}",
            name="测试音色",
            credential_fingerprint=fingerprint,
            method="system",
            status="ACTIVE",
            is_saved=True,
        )
        batch = GenerationBatch(
            id=batch_id,
            user_id=user.id,
            name="后处理分流",
            workflow_type="digital_human",
            audio_mode="minimax",
            request_key=f"postproduction-{segment_count}",
            status="ACTIVE",
            total_items=1,
        )
        item = GenerationBatchItem(
            id=item_id,
            batch=batch,
            row_number=1,
            row_key="TEXT-001",
            manifest_json=json.dumps({"speech_script": "第一句。第二句。"}),
            audio_status="SUCCESS",
            status="SEGMENTS_CREATED",
        )
        AudioGenerationTask(
            id=f"audio-task-{segment_count}",
            user_id=user.id,
            config=config,
            account_binding_id=f"binding-{segment_count}",
            credential_fingerprint=fingerprint,
            batch_item=item,
            voice_asset=voice,
            voice_a_id=voice.id,
            voice_b_id=voice.id,
            planned_generation_task_id=f"planned-{segment_count}",
            primary_kind="image",
            primary_path="image.png",
            primary_original_name="image.png",
            speech_script="第一句。第二句。",
            pronunciation_dict_json="[]",
            video_parameters_json="{}",
            model="speech-2.8-hd",
            weight_a=100,
            weight_b=0,
            output_format="mp3",
            status="SUCCESS",
            subtitle_path=subtitle_file.relative_to(settings.data_dir).as_posix(),
            cost_confirmed_at=datetime.now(timezone.utc),
        )
        for index in range(1, segment_count + 1):
            segment = GenerationSegment(
                id=f"segment-{segment_count}-{index}",
                batch_item=item,
                segment_index=index,
                script_text=f"第{index}段",
                start_seconds=float(index - 1),
                end_seconds=float(index),
                audio_path=f"segment-{index}.mp3",
                prompt="自然说话",
                status="TASK_CREATED",
            )
            GenerationTask(
                id=f"video-task-{segment_count}-{index}",
                user_id=user.id,
                segment=segment,
                workflow_type="digital_human",
                input_payload="{}",
                image_path="image.png",
                audio_path="audio.mp3",
                image_original_name="image.png",
                audio_original_name="audio.mp3",
                audio_duration_seconds=3.0,
                start_seconds=0,
                end_seconds=3,
                prompt="自然说话",
                status=TaskStatus.SUCCESS.value,
                result_path=f"outputs/{user.id}/result-{index}.mp4",
            )
        db.add(batch)
        db.commit()
    return batch_id, item_id


def test_single_text_video_is_the_only_automatic_branch():
    batch_id, item_id = _text_item(segment_count=1)
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        assert postproduction_mode(item) == AUTO_POSTPROCESS
        manifest = postproduction_manifest(item, get_settings())
        assert manifest["mode"] == AUTO_POSTPROCESS
        assert manifest["status"] == "AUTO_READY"
        assert manifest["source"]["type"] == "single_video"
        assert manifest["caption_timeline_is_final"] is True
        assert manifest["captions"]["cues"][0] == {
            "start_us": 0,
            "end_us": 1_250_000,
            "duration_us": 1_250_000,
            "text": "第一句。",
        }


def test_segmented_text_video_requires_manual_edit(client):
    batch_id, item_id = _text_item(segment_count=2)
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        assert postproduction_mode(item) == MANUAL_EDIT_REQUIRED
        manifest = postproduction_manifest(item, get_settings())
        assert manifest["manual_edit_reason"] == "SEGMENTED_VIDEO"
        assert manifest["caption_timeline_is_final"] is False

    login(client, "postproduction-text-2")
    response = client.get(
        f"/api/batches/{batch_id}/items/{item_id}/postproduction"
    )
    assert response.status_code == 200
    assert response.json()["source"]["type"] == "ordered_segments"


def test_human_readable_postproduction_page_has_direct_segment_downloads(client):
    batch_id, item_id = _text_item(segment_count=2)
    login(client, "postproduction-text-2")
    response = client.get(f"/batches/{batch_id}/items/{item_id}/postproduction")
    assert response.status_code == 200
    assert "需要人工粗剪" in response.text
    assert "下载原始片段" in response.text
    assert f"/api/tasks/video-task-2-1/download" in response.text


def test_workbench_uses_existing_account_and_lists_only_its_tasks(client):
    _text_item(segment_count=1)
    _text_item(segment_count=2)
    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-1", "password": "password123"},
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    verified = client.post(
        "/api/auth/center/verify", json={"access_token": token}
    )
    assert verified.status_code == 200
    assert verified.json()["user"]["username"] == "postproduction-text-1"

    inbox = client.post(
        "/api/workbench/tasks", json={"access_token": token, "limit": 50}
    )
    assert inbox.status_code == 200
    tasks = inbox.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "AUTO_READY"
    assert tasks[0]["item_id"] == "postproduction-item-1"
    assert tasks[0]["source"]["videos"][0]["download_url"].startswith(
        "/api/workbench/tasks/postproduction-item-1/videos/1"
    )
    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.username == "postproduction-text-1"))
        output = get_settings().outputs_dir / str(owner.id) / "result-1.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video-result")
    download = client.get(
        "/api/workbench/tasks/postproduction-item-1/videos/1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert download.status_code == 200
    assert download.content == b"video-result"


def test_workbench_manifest_exposes_seedvr2_quality_and_protected_source(client):
    _batch_id, item_id = _text_item(segment_count=1)
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, "video-task-1-1")
        source = settings.outputs_dir / str(task.user_id) / task.id / "source" / "digital.mp4"
        result = settings.outputs_dir / str(task.user_id) / "result-1.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        result.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"digital-source")
        result.write_bytes(b"seedvr2-result")
        task.enhancement = GenerationTaskEnhancement(
            id="postproduction-seedvr2-enhancement",
            generation_task_id=task.id,
            status=EnhancementStatus.SUCCESS.value,
            source_result_path=source.relative_to(settings.data_dir).as_posix(),
            result_path=result.relative_to(settings.data_dir).as_posix(),
        )
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-1", "password": "password123"},
    )
    token = login_response.json()["access_token"]
    response = client.post(
        f"/api/workbench/tasks/{item_id}",
        json={"access_token": token},
    )
    assert response.status_code == 200
    video = response.json()["source"]["videos"][0]
    assert video["quality_variant"] == "seedvr2_upscaled"
    assert video["enhancement_status"] == "SUCCESS"
    assert video["source_download_url"].endswith("/videos/1/source")
    source_download = client.get(
        video["source_download_url"],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert source_download.status_code == 200
    assert source_download.content == b"digital-source"


def test_workbench_composition_reports_video_enhancing_stage(client):
    _batch_id, item_id = _text_item(segment_count=1)
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, "video-task-1-1")
        source = settings.outputs_dir / str(task.user_id) / task.id / "source" / "digital.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"digital-source")
        task.status = TaskStatus.RUNNING.value
        task.result_path = None
        task.completed_at = None
        task.enhancement = GenerationTaskEnhancement(
            id="postproduction-active-seedvr2",
            generation_task_id=task.id,
            status=EnhancementStatus.RUNNING.value,
            source_result_path=source.relative_to(settings.data_dir).as_posix(),
            remote_task_id="seedvr2-active-id",
        )
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-1", "password": "password123"},
    )
    response = client.post(
        f"/api/workbench/tasks/{item_id}",
        json={"access_token": login_response.json()["access_token"]},
    )
    assert response.status_code == 200
    assert response.json()["composition"]["status"] == "VIDEO_ENHANCING"
    assert response.json()["composition"]["enhancement_status"] == "RUNNING"


def test_workbench_composition_reports_remote_cancel_as_retryable_failure(client):
    _batch_id, item_id = _text_item(segment_count=3)
    settings = get_settings()
    (settings.data_dir / "image.png").write_bytes(b"image")
    (settings.data_dir / "audio.mp3").write_bytes(b"audio")
    with SessionLocal() as db:
        tasks = db.scalars(select(GenerationTask).order_by(GenerationTask.id)).all()
        for task in tasks:
            task.status = TaskStatus.CANCELLED.value
            task.result_path = None
            task.error_code = "REMOTE_TASK_NOT_FOUND"
            task.error_message = "RunningHub 任务已被手动取消"
            task.input_payload = json.dumps(
                {
                    "assets": {
                        "image": {
                            "kind": "image",
                            "path": "image.png",
                            "original_name": "image.png",
                        },
                        "audio": {
                            "kind": "audio",
                            "path": "audio.mp3",
                            "original_name": "audio.mp3",
                        },
                    },
                    "parameters": {
                        "prompt": "自然说话",
                        "start_time": "0:00",
                        "end_time": "0:03",
                        "resolution": "1920",
                        "person_mode": "1",
                        "instance_type": "plus",
                    },
                },
                ensure_ascii=False,
            )
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-3", "password": "password123"},
    )
    response = client.post(
        f"/api/workbench/tasks/{item_id}",
        json={"access_token": login_response.json()["access_token"]},
    )

    assert response.status_code == 200
    composition = response.json()["composition"]
    assert composition["status"] == "COMPOSITION_FAILED"
    assert composition["processing_stage"] == "COMPOSITION_FAILED"
    assert composition["error_message"] == "RunningHub 任务已被手动取消"

    retried = client.post(
        f"/api/workbench/tasks/{item_id}/composition/retry",
        json={
            "access_token": login_response.json()["access_token"],
            "cost_confirmed": True,
            "resolution": "1024",
        },
    )
    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        tasks = db.scalars(select(GenerationTask).order_by(GenerationTask.id)).all()
        assert all(task.status == TaskStatus.PENDING.value for task in tasks)
        assert all(task.runninghub_task_id is None for task in tasks)
        assert all(
            json.loads(task.input_payload)["parameters"]["resolution"] == "1024"
            for task in tasks
        )


def test_workbench_retry_after_seedvr2_cancel_reuses_digital_human_source(client):
    _batch_id, item_id = _text_item(segment_count=1)
    settings = get_settings()
    with SessionLocal() as db:
        task = db.scalar(select(GenerationTask))
        source = settings.data_dir / str(task.result_path)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"saved-digital-human-source")
        task.runninghub_task_id = "successful-digital-human-id"
        task.status = TaskStatus.CANCELLED.value
        task.error_code = "VIDEO_ENHANCEMENT_REMOTE_MISSING"
        task.error_message = "SeedVR2 已在 RunningHub 手动取消"
        task.enhancement = GenerationTaskEnhancement(
            id="postproduction-cancelled-seedvr2",
            generation_task_id=task.id,
            status=EnhancementStatus.CANCELLED.value,
            source_result_path=source.relative_to(settings.data_dir).as_posix(),
            remote_task_id="cancelled-seedvr2-id",
        )
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-1", "password": "password123"},
    )
    retried = client.post(
        f"/api/workbench/tasks/{item_id}/composition/retry",
        json={
            "access_token": login_response.json()["access_token"],
            "cost_confirmed": True,
        },
    )

    assert retried.status_code == 200, retried.text
    with SessionLocal() as db:
        task = db.scalar(select(GenerationTask))
        assert task.status == TaskStatus.RUNNING.value
        assert task.runninghub_task_id == "successful-digital-human-id"
        assert task.enhancement.status == EnhancementStatus.PENDING.value
        assert task.enhancement.remote_task_id is None


def test_workbench_backfills_seedvr2_from_saved_digital_human_results(client):
    _batch_id, item_id = _text_item(segment_count=2)
    settings = get_settings()
    original_remote_ids: dict[str, str] = {}
    with SessionLocal() as db:
        tasks = db.scalars(
            select(GenerationTask).order_by(GenerationTask.id)
        ).all()
        for index, task in enumerate(tasks, start=1):
            source = settings.data_dir / str(task.result_path)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"digital-source-{index}".encode())
            task.runninghub_task_id = f"digital-human-paid-{index}"
            task.output_metadata = json.dumps({"provider": "4A", "index": index})
            original_remote_ids[task.id] = task.runninghub_task_id
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-2", "password": "password123"},
    )
    token = login_response.json()["access_token"]
    response = client.post(
        f"/api/workbench/tasks/{item_id}/enhancement/backfill",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["composition"]["status"] == "VIDEO_ENHANCING"
    assert payload["seedvr2_backfill"] == {
        "queued_count": 2,
        "retried_count": 0,
        "already_attached_count": 0,
        "digital_human_rerun_count": 0,
        "instance_type": "plus",
        "gpu_memory": "48G",
    }

    with SessionLocal() as db:
        tasks = db.scalars(
            select(GenerationTask).order_by(GenerationTask.id)
        ).all()
        assert len(tasks) == 2
        for index, task in enumerate(tasks, start=1):
            assert task.runninghub_task_id == original_remote_ids[task.id]
            assert task.status == TaskStatus.RUNNING.value
            assert task.result_path is None
            assert task.enhancement is not None
            assert task.enhancement.status == EnhancementStatus.PENDING.value
            source = settings.data_dir / task.enhancement.source_result_path
            assert source.read_bytes() == f"digital-source-{index}".encode()
            assert task.enhancement.source_output_metadata_json

    repeated = client.post(
        f"/api/workbench/tasks/{item_id}/enhancement/backfill",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["seedvr2_backfill"]["queued_count"] == 0
    assert repeated.json()["seedvr2_backfill"]["already_attached_count"] == 2


def test_workbench_seedvr2_backfill_is_atomic_when_a_source_is_missing(client):
    _batch_id, item_id = _text_item(segment_count=2)
    settings = get_settings()
    with SessionLocal() as db:
        tasks = db.scalars(
            select(GenerationTask).order_by(GenerationTask.id)
        ).all()
        available = settings.data_dir / str(tasks[0].result_path)
        available.parent.mkdir(parents=True, exist_ok=True)
        available.write_bytes(b"available-digital-source")
        db.commit()

    login_response = client.post(
        "/api/auth/center/login",
        json={"username": "postproduction-text-2", "password": "password123"},
    )
    response = client.post(
        f"/api/workbench/tasks/{item_id}/enhancement/backfill",
        json={
            "access_token": login_response.json()["access_token"],
            "cost_confirmed": True,
        },
    )
    assert response.status_code == 409
    assert "源片段已丢失" in response.json()["detail"]
    with SessionLocal() as db:
        tasks = db.scalars(select(GenerationTask)).all()
        assert all(task.status == TaskStatus.SUCCESS.value for task in tasks)
        assert all(task.enhancement is None for task in tasks)


def test_legacy_batch_page_and_polling_show_current_seedvr2_phase(client):
    batch_id, _item_id = _text_item(segment_count=1)
    settings = get_settings()
    with SessionLocal() as db:
        task = db.get(GenerationTask, "video-task-1-1")
        source = settings.outputs_dir / str(task.user_id) / task.id / "source" / "digital.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"digital-source")
        task.status = TaskStatus.RUNNING.value
        task.result_path = None
        task.completed_at = None
        task.runninghub_task_id = "digital-human-finished-id"
        task.enhancement = GenerationTaskEnhancement(
            id="legacy-batch-active-seedvr2",
            generation_task_id=task.id,
            status=EnhancementStatus.RUNNING.value,
            source_result_path=source.relative_to(settings.data_dir).as_posix(),
            remote_task_id="seedvr2-current-id",
            auto_retry_count=1,
        )
        db.commit()

        batch = db.scalar(batch_query().where(GenerationBatch.id == batch_id))
        payload = batch_detail_status(batch)
        assert payload[0]["status"] == "VIDEO_ENHANCING"
        assert payload[0]["segments"][0]["status"] == "VIDEO_ENHANCING"
        assert payload[0]["segments"][0]["runninghubTaskId"] == "seedvr2-current-id"
        assert payload[0]["segments"][0]["autoRetryCount"] == 1

    login(client, "postproduction-text-1")
    response = client.get(f"/batches/{batch_id}")
    assert response.status_code == 200
    assert "视频清晰化中（SeedVR2 48G）" in response.text
    assert "seedvr2-current-id" in response.text
    assert "自动重试 1/" in response.text


def test_workbench_token_is_invalid_after_password_change(client):
    user = create_user("workbench-password-change")
    response = client.post(
        "/api/auth/center/login",
        json={"username": user.username, "password": "password123"},
    )
    token = response.json()["access_token"]
    from app.services.security import hash_password

    with SessionLocal() as db:
        stored = db.get(type(user), user.id)
        stored.password_hash = hash_password("new-password123")
        db.commit()
    verified = client.post(
        "/api/auth/center/verify", json={"access_token": token}
    )
    assert verified.status_code == 401
