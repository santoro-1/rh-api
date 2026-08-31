from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    GenerationBatch,
    GenerationSegment,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    RunningHubConfig,
    RunningHubExecutionAccount,
    SystemWorkflowConfig,
    User,
)
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.task_creation import TaskCreationError, ensure_user_can_create_workflow
from app.workers import audio_worker
from tests.async_speech_fakes import make_async_speech_bundle
from tests.conftest import assign_runninghub_account, create_user


@pytest.fixture
def audio_request(client, monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("Audio validation must not call external providers")

    monkeypatch.setattr("requests.sessions.Session.request", forbid_network)
    user = create_user("audio-only-user", with_config=False)
    with SessionLocal() as db:
        config = MiniMaxConfig(
            user_id=user.id,
            api_key_encrypted=encrypt_secret("audio-only-minimax-key"),
            credential_fingerprint=credential_fingerprint("audio-only-minimax-key"),
            base_url="https://minimax.example",
            requests_per_minute=60,
        )
        db.add(config)
        db.flush()
        db.add(
            MiniMaxVoiceAsset(
                id="audio-only-voice",
                user_id=user.id,
                config_id=config.id,
                name="声音测试",
                voice_id="mock-system-voice",
                account_binding_id=config.account_binding_id,
                credential_fingerprint=config.credential_fingerprint,
                status="ACTIVE",
                method="system",
                is_saved=True,
            )
        )
        db.commit()
    login = client.post(
        "/api/auth/center/login",
        json={"username": user.username, "password": "password123"},
    )
    assert login.status_code == 200
    return {
        "access_token": login.json()["access_token"],
        "name": "仅生成声音",
        "request_key": "audio-only-validation",
        "rows": [{"row_id": "1", "speech_script": "只生成声音。"}],
        "speech_options": {
            "voiceAssetId": "audio-only-voice",
            "speed": 1.04,
            "costConfirmed": True,
            "reviewRequired": False,
        },
    }


@pytest.mark.parametrize(
    ("video_state", "video_error"),
    [
        ("unconfigured", "没有可用的 RunningHub 执行账号"),
        ("empty_legacy_key", "没有可用的 RunningHub 执行账号"),
        ("bound_pool_empty_legacy_key", None),
        ("disabled_pool", "没有可用的 RunningHub 执行账号"),
        ("disabled_workflow", "尚未启用"),
        ("missing_app_id", "尚未配置 RunningHub App ID"),
    ],
)
def test_audio_creation_is_independent_of_video_configuration(
    client, audio_request, video_state, video_error
):
    if video_state not in {"unconfigured", "empty_legacy_key"}:
        account_id = assign_runninghub_account("audio-only-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="audio-only-user").one()
        if "empty_legacy_key" in video_state:
            db.add(
                RunningHubConfig(
                    user_id=user.id,
                    api_key_encrypted=None,
                    base_url="https://rh.example",
                    ai_app_id="mock-video-app",
                    instance_type="plus",
                    default_prompt="保留配置的提示词",
                    max_concurrent_tasks=1,
                )
            )
        if video_state == "disabled_pool":
            db.get(RunningHubExecutionAccount, account_id).is_enabled = False
        if video_state != "unconfigured":
            workflow = db.query(SystemWorkflowConfig).filter_by(
                workflow_key="digital_human"
            ).one_or_none()
            if workflow is None:
                workflow = SystemWorkflowConfig(workflow_key="digital_human")
                db.add(workflow)
            workflow.ai_app_id = (
                "" if video_state == "missing_app_id" else "mock-video-app"
            )
            workflow.instance_type = "plus"
            workflow.default_prompt = "保留配置的提示词"
            workflow.is_enabled = video_state != "disabled_workflow"
        db.commit()

    created = client.post("/api/workbench/audio-batches", json=audio_request)
    assert created.status_code == 201, created.text
    repeated = client.post("/api/workbench/audio-batches", json=audio_request)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["batch_id"] == created.json()["batch_id"]
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        batch = db.query(GenerationBatch).one()
        assert batch.source_channel == "new_workbench"
        assert batch.review_required is True
        assert task.status == "PENDING"
        assert task.speed == 1.04
        assert task.primary_path is None
        assert batch.runninghub_execution_account_ids_json is None
        assert db.query(GenerationTask).count() == 0
        assert db.query(GenerationSegment).count() == 0
        if video_state != "unconfigured":
            assert json.loads(task.video_parameters_json)["prompt"] == "保留配置的提示词"
        if video_error:
            with pytest.raises(TaskCreationError, match=video_error):
                ensure_user_can_create_workflow(task.user, "digital_human")
        else:
            ensure_user_can_create_workflow(task.user, "digital_human")


@pytest.mark.parametrize(
    ("invalid_case", "status_code", "error"),
    [
        ("missing_minimax_key", 400, "MiniMax API Key"),
        ("wrong_voice_binding", 400, "已经保存成功的音色"),
        ("unsaved_voice", 404, "声音原型不存在"),
        ("foreign_voice", 404, "声音原型不存在"),
        ("inactive_custom_voice", 409, "激活该音色"),
        ("cost_not_confirmed", 400, "请先确认语音"),
        ("invalid_speed", 400, "语速"),
        ("empty_script", 400, "不能为空"),
    ],
)
def test_audio_only_creation_keeps_speech_validation(
    client, audio_request, invalid_case, status_code, error
):
    if invalid_case == "foreign_voice":
        other_user = create_user("other-audio-owner", with_config=False)
    with SessionLocal() as db:
        voice = db.get(MiniMaxVoiceAsset, "audio-only-voice")
        if invalid_case == "missing_minimax_key":
            voice.config.api_key_encrypted = None
        elif invalid_case == "wrong_voice_binding":
            voice.account_binding_id = "old-minimax-account"
        elif invalid_case == "unsaved_voice":
            voice.is_saved = False
        elif invalid_case == "foreign_voice":
            voice.user_id = other_user.id
        elif invalid_case == "inactive_custom_voice":
            voice.method = "clone"
            voice.status = "READY"
        db.commit()
    if invalid_case == "cost_not_confirmed":
        audio_request["speech_options"]["costConfirmed"] = False
    elif invalid_case == "invalid_speed":
        audio_request["speech_options"]["speed"] = 0.1
    elif invalid_case == "empty_script":
        audio_request["rows"][0]["speech_script"] = ""

    response = client.post("/api/workbench/audio-batches", json=audio_request)
    assert response.status_code == status_code, response.text
    assert error in response.text
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 0
        assert db.query(AudioGenerationTask).count() == 0
        assert db.query(GenerationTask).count() == 0


def test_audio_only_worker_completes_without_runninghub_configuration(
    client, audio_request, monkeypatch
):
    created = client.post("/api/workbench/audio-batches", json=audio_request)
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    item_id = created.json()["items"][0]["item_id"]
    provider = Mock()
    provider.create_async_speech_task.return_value = ("mock-task", "mock-file", {})
    provider.query_async_speech_task.return_value = ("success", "mock-file", {})
    provider.download_file_content.return_value = make_async_speech_bundle(
        b"ID3audio-only", [(0.0, 1.2, "只生成声音。")]
    )
    monkeypatch.setattr(audio_worker, "_make_client", lambda task: provider)
    monkeypatch.setattr(
        audio_worker,
        "master_generated_speech",
        lambda source, target: target.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(
        "app.routes.workbench.ensure_generated_speech_mastered", lambda path: True
    )
    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 1
    assert audio_worker.run_once() == 0
    provider.create_async_speech_task.assert_called_once()
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).one()
        assert task.status == "AWAITING_REVIEW"
        assert task.reviewed_at is None
        assert task.user.runninghub_config is None
        assert task.user.runninghub_pool_memberships == []
        assert db.query(AudioGenerationAttempt).one().status == "READY"
        assert db.query(GenerationTask).count() == 0
        assert db.query(GenerationSegment).count() == 0
    status = client.post(
        f"/api/workbench/audio-batches/{batch_id}",
        json={"access_token": audio_request["access_token"]},
    )
    assert status.status_code == 200
    assert status.json()["items"][0]["audio_ready"] is True
    assert status.json()["items"][0]["captions"]["source"] == "minimax_timestamps"
    audio = client.get(
        f"/api/workbench/audio-batches/{batch_id}/items/{item_id}/audio",
        headers={"Authorization": f"Bearer {audio_request['access_token']}"},
    )
    assert audio.status_code == 200
    assert audio.content == b"ID3audio-only"
