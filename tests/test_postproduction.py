from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationTask,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
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
