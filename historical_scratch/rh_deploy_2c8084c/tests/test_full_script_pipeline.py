from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import (
    GenerationSegment,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    User,
    VoiceAssetStatus,
)
from app.services.media_segmentation import plan_audio_segments, plan_silence_segments
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint, save_minimax_config
from app.services.workflow_configs import save_workflow_config
from app.workers import audio_worker
from tests.async_speech_fakes import make_async_speech_bundle
from tests.conftest import create_user, login


def _stage(client, kind: str, name: str, content: bytes, mime: str) -> str:
    response = client.post(
        "/api/batch-assets",
        data={"kind": kind},
        files={"file": (name, content, mime)},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


def _configure_saved_voice(username: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("full-flow-key"),
            credential_fingerprint=credential_fingerprint("full-flow-key"),
            base_url="https://api.minimax.io",
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id="full-flow-voice",
            user_id=user.id,
            config_id=config.id,
            name="完整流程音色",
            voice_id="provider-full-flow-voice",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            status=VoiceAssetStatus.READY.value,
            method="clone",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        return voice.id


class _FakeMiniMaxClient:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.bundle = b""

    def create_async_speech_task(self, **kwargs):
        self.texts.append(kwargs["text"])
        return "full-flow-task", "full-flow-file", {}

    def query_async_speech_task(self, task_id):
        assert task_id == "full-flow-task"
        return "success", "full-flow-file", {}

    def download_file_content(self, file_id):
        assert file_id == "full-flow-file"
        return self.bundle


def _write_audio_segment(source, target, **kwargs):
    del source, kwargs
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ID3segment")


def _copy_mastered_speech(source, target, **kwargs):
    del kwargs
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())


def test_local_segment_plan_preserves_script_and_hard_limit():
    script = "".join(
        [
            "第一段介绍产品。",
            " 第二段说明主要特点。\n",
            "第三段介绍使用方法。 ",
            "第四段说明注意事项。",
            "第五段进行总结。",
        ]
    )
    plans = plan_audio_segments(
        script,
        82.0,
        silence_midpoints=[29.5, 56.4],
    )

    assert len(plans) == 4
    assert "".join(plan.script_text for plan in plans) == script
    assert all(plan.duration_seconds <= 30.0 for plan in plans)
    assert plans[0].end_seconds == 29.5
    assert plans[-1].end_seconds == 82.0


def test_silence_plan_avoids_tiny_tail_and_never_needs_transcript():
    plans = plan_silence_segments(91.0, silence_midpoints=[44.0])
    assert all(plan.script_text == "" for plan in plans)
    assert all(12.0 <= plan.duration_seconds <= 30.0 for plan in plans)
    assert plans[0].start_seconds == 0
    assert plans[-1].end_seconds == 91.0


def test_digital_human_silence_plan_hard_limits_segments_to_35_seconds():
    plans = plan_silence_segments(
        70.0,
        silence_midpoints=[29.8, 59.9],
        max_segment_seconds=35.0,
    )

    assert len(plans) == 3
    assert all(plan.duration_seconds <= 35.0 for plan in plans)
    assert plans[0].end_seconds == 29.8
    assert plans[-1].end_seconds == 70.0


def test_minimax_account_binding_survives_key_rotation_but_not_account_switch():
    create_user("minimax-binding-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="minimax-binding-user").one()
        config = save_minimax_config(
            db,
            user,
            api_key="first-key",
            base_url="https://api.minimax.io",
            requests_per_minute=20,
            account_label="公司主账号",
        )
        db.commit()
        original_binding = config.account_binding_id
        original_fingerprint = config.credential_fingerprint

        save_minimax_config(
            db,
            user,
            api_key="rotated-key",
            base_url="https://api.minimax.io",
            requests_per_minute=20,
            account_label="公司主账号",
        )
        db.commit()
        assert config.account_binding_id == original_binding
        assert config.credential_fingerprint != original_fingerprint

        save_minimax_config(
            db,
            user,
            api_key="another-account-key",
            base_url="https://api.minimax.io",
            requests_per_minute=20,
            account_label="另一个官网账号",
            start_new_account_binding=True,
        )
        db.commit()
        assert config.account_binding_id != original_binding


def test_ltx_full_flow_calls_tts_once_and_creates_sequential_children(
    client,
    monkeypatch,
):
    create_user("full-ltx-user")
    voice_id = _configure_saved_voice("full-ltx-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="full-ltx-user").one()
        db.add(
            save_workflow_config(
                user,
                "ltx_lip_sync",
                ai_app_id="2080551073030434817",
                instance_type="plus",
                default_prompt="一名人物用中文说",
                is_enabled=True,
            )
        )
        db.commit()
    login(client, "full-ltx-user")
    video_id = _stage(
        client,
        "video",
        "source.mp4",
        b"\x00\x00\x00\x18ftypisompayload",
        "video/mp4",
    )
    first_script = "这是第一段完整台词。"
    second_script = "这是第二段完整台词。"
    complete_script = first_script + second_script
    payload = {
        "name": "完整对口型流程",
        "workflowType": "ltx_lip_sync",
        "audioMode": "minimax",
        "requestKey": "full-ltx-request",
        "assetIds": [video_id],
        "batchParameters": {
            "instance_type": "plus",
            "prompt_prefix": "一名女性用中文说",
        },
        "speechOptions": {
            "voiceAssetId": voice_id,
            "costConfirmed": True,
        },
        "rows": [
            {
                "row_id": "SCRIPT-001",
                "speech_script": complete_script,
            }
        ],
    }
    created = client.post("/api/batches", json=payload)
    assert created.status_code == 201, created.text
    batch_id = created.json()["batchId"]

    fake_client = _FakeMiniMaxClient()
    fake_client.bundle = make_async_speech_bundle(
        b"ID3full-audio",
        [
            (0.0, 30.0, first_script),
            (30.0, 55.0, second_script),
        ],
    )
    video_cuts: list[tuple[float, float]] = []
    monkeypatch.setattr(audio_worker, "_make_client", lambda task: fake_client)
    monkeypatch.setattr(
        audio_worker, "master_generated_speech", _copy_mastered_speech
    )
    monkeypatch.setattr(
        audio_worker,
        "inspect_audio_duration",
        lambda path: (
            30.0
            if "segment-001" in path.name
            else 25.0
            if "segment-002" in path.name
            else 55.0
        ),
    )
    monkeypatch.setattr(audio_worker, "inspect_media_duration", lambda path: 65.0)
    monkeypatch.setattr(audio_worker, "cut_audio_segment", _write_audio_segment)

    def fake_video_cut(source, target, *, start_seconds, duration_seconds):
        del source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypisompayload")
        video_cuts.append((start_seconds, duration_seconds))

    monkeypatch.setattr(audio_worker, "cut_video_segment", fake_video_cut)

    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 1

    with SessionLocal() as db:
        segments = (
            db.query(GenerationSegment)
            .order_by(GenerationSegment.segment_index)
            .all()
        )
        tasks = (
            db.query(GenerationTask)
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert [segment.script_text for segment in segments] == [
            first_script,
            second_script,
        ]
        assert len(tasks) == 2
        assert all(task.status == "PENDING" for task in tasks)
        prompts = [
            json.loads(task.input_payload)["parameters"]["prompt"]
            for task in tasks
        ]
        assert prompts == [
            f"一名女性用中文说：“{first_script}”",
            f"一名女性用中文说：“{second_script}”",
        ]
        first_segment_id = segments[0].id
    assert fake_client.texts == [complete_script]
    assert video_cuts == [(0.0, 30.0), (30.0, 25.0)]

    status = client.get(f"/api/batches/{batch_id}")
    assert status.status_code == 200
    assert len(status.json()["items"][0]["segments"]) == 2

    audio = client.get(
        f"/batches/{batch_id}/segments/{first_segment_id}/audio"
    )
    assert audio.status_code == 200
    assert audio.content == b"ID3segment"
