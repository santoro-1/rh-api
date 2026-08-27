from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationAttempt,
    AudioGenerationTask,
    AudioTaskStatus,
    GenerationBatch,
    GenerationTask,
    H3BatchConfig,
    H3ItemConfig,
    H3SegmentConfig,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    RunningHubDualPoolGrant,
    RunningHubExecutionAccount,
    RunningHubPoolMembership,
    User,
    VoiceAssetStatus,
)
from app.services.h3_pool import configure_h3_capability
from app.services.h3.motion_references import H3MotionReference
from app.services.security import encrypt_secret, secret_fingerprint
from app.services.speech.accounts import credential_fingerprint
from app.services.storage import to_relative_data_path
from tests.conftest import create_user


SCRIPT = "真正的优势，是把复杂的事情长期做对。然后稳定地继续向前。"


def _token(client, username: str) -> str:
    response = client.post(
        "/api/auth/center/login",
        json={"username": username, "password": "password123"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _stage(client, token: str, kind: str, name: str, content_type: str) -> str:
    content = {
        "image": b"\x89PNG\r\n\x1a\nidentity-image",
        "video": b"\x00\x00\x00\x18ftypisomh3-video",
    }[kind]
    response = client.post(
        "/api/workbench/batch-assets",
        data={"access_token": token, "kind": kind},
        files={"file": (name, content, content_type)},
    )
    assert response.status_code == 201, response.text
    return response.json()["asset_id"]


def _fake_reference_frame(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nauto-primary-frame")


def _configure_minimax(username: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        user.h3_access_enabled = True
        secret = f"h3-minimax-{username}"
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret(secret),
            credential_fingerprint=credential_fingerprint(secret),
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id=f"h3-voice-{username}",
            user_id=user.id,
            config_id=config.id,
            name="H3 测试音色",
            voice_id=f"h3-provider-{username}",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            status=VoiceAssetStatus.ACTIVE.value,
            method="clone",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        return voice.id


def _finished_audio(
    client, token: str, username: str, *, overlong_cue: bool = False
) -> tuple[str, str]:
    voice_id = _configure_minimax(username)
    created = client.post(
        "/api/workbench/audio-batches",
        json={
            "access_token": token,
            "name": "H3 MiniMax 声音",
            "request_key": f"h3-audio-{username}",
            "correlation_id": f"h3-{username}",
            "rows": [{"row_id": "ROW-001", "speech_script": SCRIPT}],
            "speech_options": {
                "voiceAssetId": voice_id,
                "model": "speech-2.8-hd",
                "speed": 1,
                "volume": 1,
                "pitch": 0,
                "languageBoost": "Chinese",
                "outputFormat": "mp3",
                "costConfirmed": True,
            },
        },
    )
    assert created.status_code == 201, created.text
    batch_id = created.json()["batch_id"]
    item_id = created.json()["items"][0]["item_id"]
    with SessionLocal() as db:
        task = db.query(AudioGenerationTask).filter_by(batch_item_id=item_id).one()
        output = get_settings().outputs_dir / f"{username}-h3-audio.mp3"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ID3h3-generated-audio")
        cues = get_settings().outputs_dir / f"{username}-h3-cues.json"
        cue_payload = (
            [
                {
                    "text": SCRIPT,
                    "start_seconds": 0,
                    "end_seconds": 20,
                }
            ]
            if overlong_cue
            else [
                {
                    "text": "真正的优势，是把复杂的事情长期做对。",
                    "start_seconds": 0,
                    "end_seconds": 10,
                },
                {
                    "text": "然后稳定地继续向前。",
                    "start_seconds": 10,
                    "end_seconds": 20,
                },
            ]
        )
        cues.write_text(
            json.dumps(
                cue_payload,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task.output_path = to_relative_data_path(output, get_settings())
        task.subtitle_path = to_relative_data_path(cues, get_settings())
        task.status = AudioTaskStatus.AWAITING_REVIEW.value
        task.batch_item.audio_status = "AWAITING_REVIEW"
        task.batch_item.status = "AWAITING_AUDIO_REVIEW"
        db.add(
            AudioGenerationAttempt(
                id=str(uuid.uuid4()),
                audio_task_id=task.id,
                version=task.generation_version,
                output_path=task.output_path,
                subtitle_path=task.subtitle_path,
                status="READY",
            )
        )
        db.commit()
    return batch_id, item_id


def _enable_h3_pool(username: str, *, access_password: str | None = None) -> int:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        grant = RunningHubDualPoolGrant(
            user_id=user.id,
            is_enabled=True,
            allow_non_admin=True,
            note="H3 workbench tests",
        )
        key = f"h3-runninghub-{username}"
        account = RunningHubExecutionAccount(
            label="H3 Ref2VA 测试账号",
            api_key_encrypted=encrypt_secret(key),
            credential_fingerprint=secret_fingerprint(key),
            base_url="https://runninghub.example",
            digital_human_ai_app_id="must-not-be-used-for-h3",
            max_concurrent_tasks=5,
            is_enabled=True,
        )
        db.add_all([grant, account])
        db.flush()
        db.add(
            RunningHubPoolMembership(
                execution_account=account,
                admin_user=user,
            )
        )
        db.add(
            configure_h3_capability(
                account,
                workflow_id="h3-raw-workflow-test",
                instance_type="plus",
                max_concurrent_tasks=3,
                safe_note="只运行审核后的 Ref2VA 模板",
                access_password=access_password,
                is_enabled=True,
            )
        )
        db.commit()
        return account.id


def _fake_cut(source: Path, target: Path, *, start_seconds: float, end_seconds: float) -> None:
    del source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(f"ID3segment:{start_seconds:.3f}:{end_seconds:.3f}".encode())


def _fake_motion_references(source: Path, target_dir: Path) -> list[H3MotionReference]:
    del source
    result = []
    for index in range(5):
        target = target_dir / f"motion-{index + 1:03d}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"motion-reference-{index}".encode())
        result.append(
            H3MotionReference(
                index=index,
                start_seconds=index * 3.0,
                end_seconds=(index + 1) * 3.0,
                path=target,
                sha256=f"{index + 1:064x}",
            )
        )
    return result


def test_h3_prepare_quote_and_confirm_are_two_distinct_no_double_submit_steps(
    client,
    monkeypatch,
) -> None:
    username = "h3-workbench-user"
    create_user(username)
    token = _token(client, username)
    account_id = _enable_h3_pool(username)
    image_1 = _stage(client, token, "image", "front.png", "image/png")
    image_2 = _stage(client, token, "image", "side.png", "image/png")
    video_id = _stage(client, token, "video", "row-one.mp4", "video/mp4")
    audio_batch_id, audio_item_id = _finished_audio(
        client, token, username, overlong_cue=True
    )

    audio_sources = client.post(
        "/api/workbench/h3-audio-sources",
        json={"access_token": token},
    )
    assert audio_sources.status_code == 200, audio_sources.text
    assert audio_sources.json()["sources"] == [
        {
            "audio_batch_id": audio_batch_id,
            "audio_item_id": audio_item_id,
            "audio_generation_version": 1,
            "batch_name": "H3 MiniMax 声音",
            "row_key": "ROW-001",
            "script_text": SCRIPT,
            "status": AudioTaskStatus.AWAITING_REVIEW.value,
            "created_at": audio_sources.json()["sources"][0]["created_at"],
            "audio_download_url": (
                f"/api/workbench/audio-batches/{audio_batch_id}"
                f"/items/{audio_item_id}/audio"
            ),
        }
    ]
    serialized_sources = json.dumps(audio_sources.json(), ensure_ascii=False)
    assert "output_path" not in serialized_sources
    assert "subtitle_path" not in serialized_sources
    create_user("h3-audio-source-intruder")
    intruder_sources = client.post(
        "/api/workbench/h3-audio-sources",
        json={"access_token": _token(client, "h3-audio-source-intruder")},
    )
    assert intruder_sources.status_code == 200
    assert intruder_sources.json()["sources"] == []

    monkeypatch.setattr(
        "app.services.h3_workbench.inspect_audio_duration",
        lambda path: 10.0 if "segment-" in Path(path).name else 20.0,
    )
    monkeypatch.setattr("app.services.h3_workbench.cut_audio_segment", _fake_cut)
    monkeypatch.setattr(
        "app.services.h3_workbench.split_h3_motion_reference",
        _fake_motion_references,
    )
    monkeypatch.setattr(
        "app.services.h3_workbench.extract_reference_frame",
        _fake_reference_frame,
    )
    monkeypatch.setattr(
        "app.services.h3_workbench.get_alignment_provider",
        lambda _name: pytest.fail("H3 预检不应调用云端本机 ASR"),
    )

    accounts = client.post(
        "/api/workbench/h3-execution-accounts",
        json={"access_token": token},
    )
    assert accounts.status_code == 200, accounts.text
    serialized_accounts = json.dumps(accounts.json(), ensure_ascii=False)
    assert accounts.json()["default_selected_account_ids"] == [account_id]
    capability = accounts.json()["adapter_capability"]
    assert capability["max_user_reference_images"] == 4
    assert capability["max_effective_reference_images"] == 6
    assert capability["default_continuity_mode"] == "loop_anchor"
    assert capability["generation_tail_seconds"]["default"] == pytest.approx(0.1)
    assert [mode["value"] for mode in capability["continuity_modes"]] == [
        "loop_anchor",
        "fast",
        "soft_chain",
    ]
    assert [aspect["value"] for aspect in capability["aspect_ratios"]] == [
        "9:16 (Portrait Widescreen)",
        "16:9 (Widescreen)",
    ]
    assert "node" not in json.dumps(capability, ensure_ascii=False).casefold()
    assert "h3-raw-workflow-test" not in serialized_accounts
    assert f"h3-runninghub-{username}" not in serialized_accounts

    payload = {
        "access_token": token,
        "name": "H3 两阶段费用测试",
        "request_key": "h3-prepare-001",
        "reference_image_asset_ids": [image_1, image_2],
        "selected_account_ids": [account_id],
        "defaults": {
            "resolution": {
                "aspect_ratio": "9:16 (Portrait Widescreen)",
                "megapixels": 1,
                "multiple": 32,
            },
        },
        "rows": [
            {
                "row_id": "ROW-001",
                "script_text": SCRIPT,
                "video_asset_id": video_id,
                "reference_image_asset_ids": [image_2],
                "audio_batch_id": audio_batch_id,
                "audio_item_id": audio_item_id,
                "audio_generation_version": 1,
                "audio_alignment": {
                    "schema": "jyd.h3-safe-cut-alignment.v1",
                    "source": "jyd_local_funasr",
                    "script_sha256": hashlib.sha256(SCRIPT.encode()).hexdigest(),
                    "audio_sha256": hashlib.sha256(
                        b"ID3h3-generated-audio"
                    ).hexdigest(),
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                    "ranges": [
                        {
                            "script_start": 0,
                            "script_end": len("真正的优势，是把复杂的事情长期做对"),
                            "start_us": 100_000,
                            "end_us": 9_800_000,
                        },
                        {
                            "script_start": len("真正的优势，是把复杂的事情长期做对。"),
                            "script_end": len(SCRIPT) - 1,
                            "start_us": 10_200_000,
                            "end_us": 19_800_000,
                        },
                    ],
                },
            }
        ],
    }
    prepared = client.post("/api/workbench/h3-batches/prepare", json=payload)
    assert prepared.status_code == 201, prepared.text
    body = prepared.json()
    assert body["status"] == "AWAITING_COST_CONFIRMATION"
    assert body["reference_image_count"] == 1
    assert body["fee_snapshot"]["segment_count"] == 2
    assert body["fee_snapshot"]["estimated_paid_calls"] == 2
    assert [segment["status"] for segment in body["items"][0]["segments"]] == [
        "AWAITING_COST_CONFIRMATION",
        "AWAITING_COST_CONFIRMATION",
    ]
    assert body["items"][0]["authoritative_audio_download_url"].endswith("/audio")
    assert body["items"][0]["raw_cues_download_url"].endswith("/raw-cues")
    assert all(
        segment["normalized_video_download_url"] is None
        for segment in body["items"][0]["segments"]
    )
    batch_id = body["batch_id"]

    repeated = client.post("/api/workbench/h3-batches/prepare", json=payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["batch_id"] == batch_id

    with SessionLocal() as db:
        assert db.query(GenerationTask).filter_by(workflow_type="minimax_h3_ref2va").count() == 0
        batch = db.get(GenerationBatch, batch_id)
        assert batch.h3_config.input_sha256 != "0" * 64
        assert batch.h3_config.continuity_mode == "loop_anchor"
        assert batch.h3_config.generation_tail_seconds == pytest.approx(0.1)
        assert (
            batch.h3_config.prompt_template_version
            == "h3.prompt.ref2va.loop_anchor.v2"
        )
        assert db.query(H3BatchConfig).count() == 1
        assert db.query(H3ItemConfig).count() == 1
        assert db.query(H3SegmentConfig).count() == 2
        assert {
            config.prompt_template_version
            for config in db.query(H3SegmentConfig).all()
        } == {"h3.prompt.ref2va.loop_anchor.v2"}
        segment_configs = (
            db.query(H3SegmentConfig).order_by(H3SegmentConfig.segment_id).all()
        )
        assert len({config.motion_reference_index for config in segment_configs}) == 2
        assert all(config.motion_reference_path for config in segment_configs)
        assert all(config.motion_reference_sha256 for config in segment_configs)

    rejected = client.post(
        f"/api/workbench/h3-batches/{batch_id}/confirm",
        json={"access_token": token, "cost_confirmed": False},
    )
    assert rejected.status_code == 409

    unreviewed = client.post(
        f"/api/workbench/h3-batches/{batch_id}/confirm",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert unreviewed.status_code == 400
    assert "尚未审核" in unreviewed.json()["detail"]

    approved = client.post(
        "/api/workbench/h3-audio-sources/approve",
        json={
            "access_token": token,
            "audio_batch_id": audio_batch_id,
            "audio_item_id": audio_item_id,
            "audio_generation_version": 1,
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == AudioTaskStatus.SUCCESS.value
    assert approved.json()["reviewed_at"]

    confirmed = client.post(
        f"/api/workbench/h3-batches/{batch_id}/confirm",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    after_confirm_sources = client.post(
        "/api/workbench/h3-audio-sources",
        json={"access_token": token},
    )
    assert after_confirm_sources.status_code == 200
    assert len(after_confirm_sources.json()["sources"]) == 1
    assert after_confirm_sources.json()["sources"][0]["audio_item_id"] == audio_item_id
    assert after_confirm_sources.json()["sources"][0]["status"] == AudioTaskStatus.SUCCESS.value
    confirmed_body = confirmed.json()
    assert confirmed_body["status"] == "ACTIVE"
    assert confirmed_body["fee_snapshot"]["cost_confirmed"] is True
    assert confirmed_body["fee_snapshot"]["estimated_paid_calls"] == 2
    assert [segment["status"] for segment in confirmed_body["items"][0]["segments"]] == [
        "PENDING",
        "PENDING",
    ]

    second_confirm = client.post(
        f"/api/workbench/h3-batches/{batch_id}/confirm",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert second_confirm.status_code == 200
    with SessionLocal() as db:
        tasks = db.query(GenerationTask).filter_by(workflow_type="minimax_h3_ref2va").all()
        assert len(tasks) == 2
        assert all(task.seedvr2_enabled is False for task in tasks)
        assert all(task.batch_item_id is None for task in tasks)
        assert all(task.segment_id for task in tasks)
        assert all("identity_image_1" in task.input_payload for task in tasks)
        assert all("identity_image_2" not in task.input_payload for task in tasks)
        for task in tasks:
            assets = json.loads(task.input_payload)["assets"]
            assert assets["video"]["original_name"].startswith("motion-")
            assert assets["identity_image_1"]["original_name"] == "side.png"
        assert all(
            "continuity_anchor" not in json.loads(task.input_payload)["assets"]
            for task in tasks
        )

    reused_video_id = _stage(
        client,
        token,
        "video",
        "row-one-reuse.mp4",
        "video/mp4",
    )
    reused_payload = {
        **payload,
        "request_key": "h3-prepare-reuse-approved-audio",
        "reference_image_asset_ids": [],
        "defaults": {**payload["defaults"], "continuity_mode": "fast"},
        "rows": [
            {
                **payload["rows"][0],
                "row_id": "ROW-REUSE-001",
                "video_asset_id": reused_video_id,
                "reference_image_asset_ids": [],
            }
        ],
    }
    reused_prepared = client.post("/api/workbench/h3-batches/prepare", json=reused_payload)
    assert reused_prepared.status_code == 201, reused_prepared.text
    with SessionLocal() as db:
        source_task = db.query(AudioGenerationTask).filter_by(batch_item_id=audio_item_id).one()
        reviewed_at = source_task.reviewed_at
        source_item_status = source_task.batch_item.status
        source_audio_status = source_task.batch_item.audio_status

    reused_confirmed = client.post(
        f"/api/workbench/h3-batches/{reused_prepared.json()['batch_id']}/confirm",
        json={"access_token": token, "cost_confirmed": True},
    )
    assert reused_confirmed.status_code == 200, reused_confirmed.text
    with SessionLocal() as db:
        source_task = db.query(AudioGenerationTask).filter_by(batch_item_id=audio_item_id).one()
        assert source_task.status == AudioTaskStatus.SUCCESS.value
        assert source_task.reviewed_at == reviewed_at
        assert source_task.batch_item.status == source_item_status
        assert source_task.batch_item.audio_status == source_audio_status


def test_h3_prepare_rejects_undeclared_prompt_field_and_mixed_audio_script(client) -> None:
    username = "h3-contract-user"
    create_user(username)
    token = _token(client, username)
    account_id = _enable_h3_pool(username)
    video_id = _stage(client, token, "video", "ref.mp4", "video/mp4")
    audio_batch_id, audio_item_id = _finished_audio(client, token, username)

    response = client.post(
        "/api/workbench/h3-batches/prepare",
        json={
            "access_token": token,
            "request_key": "h3-injection",
            "reference_image_asset_ids": [],
            "selected_account_ids": [account_id],
            "defaults": {"continuity_mode": "fast"},
            "rows": [
                {
                    "row_id": "ROW-001",
                    "script_text": "不是声音绑定的原稿。",
                    "video_asset_id": video_id,
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                    "prompt": "用户覆盖系统 Prompt",
                }
            ],
        },
    )
    assert response.status_code == 400
    assert "人工总体提示词请使用 prompt_override" in response.json()["detail"]

    mismatched_script = client.post(
        "/api/workbench/h3-batches/prepare",
        json={
            "access_token": token,
            "request_key": "h3-script-mismatch",
            "reference_image_asset_ids": [],
            "selected_account_ids": [account_id],
            "defaults": {"continuity_mode": "fast"},
            "rows": [
                {
                    "row_id": "ROW-001",
                    "script_text": "不是声音绑定的原稿。",
                    "video_asset_id": video_id,
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                }
            ],
        },
    )
    assert mismatched_script.status_code == 400
    assert "表格原稿与所选 MiniMax 声音原稿不一致" in mismatched_script.json()["detail"]

    second_video_id = _stage(client, token, "video", "ref-2.mp4", "video/mp4")
    duplicated_audio = client.post(
        "/api/workbench/h3-batches/prepare",
        json={
            "access_token": token,
            "request_key": "h3-duplicate-audio",
            "reference_image_asset_ids": [],
            "selected_account_ids": [account_id],
            "defaults": {"continuity_mode": "fast"},
            "rows": [
                {
                    "row_id": "ROW-001",
                    "script_text": SCRIPT,
                    "video_asset_id": video_id,
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                },
                {
                    "row_id": "ROW-002",
                    "script_text": SCRIPT,
                    "video_asset_id": second_video_id,
                    "audio_batch_id": audio_batch_id,
                    "audio_item_id": audio_item_id,
                    "audio_generation_version": 1,
                },
            ],
        },
    )
    assert duplicated_audio.status_code == 400
    assert "同一 MiniMax 音频行不能" in duplicated_audio.json()["detail"]
