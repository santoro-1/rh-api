from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationTask,
    GenerationBatch,
    GenerationTask,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceAssetStatus,
)
from app.services.security import encrypt_secret
from app.services.speech.accounts import credential_fingerprint
from app.services.batch_manifests import parse_manifest
from app.services.storage import to_relative_data_path
from app.services.workflow_configs import save_workflow_config
from tests.conftest import create_user, login


def _stage(client, kind: str, name: str, content: bytes, mime: str) -> str:
    response = client.post(
        "/api/batch-assets",
        data={"kind": kind},
        files={"file": (name, content, mime)},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


def _digital_assets(client):
    return [
        _stage(
            client,
            "image",
            "person.png",
            b"\x89PNG\r\n\x1a\npayload",
            "image/png",
        ),
        _stage(client, "audio", "voice.mp3", b"ID3voice", "audio/mpeg"),
        _stage(client, "audio", "left.mp3", b"ID3left", "audio/mpeg"),
        _stage(client, "audio", "right.mp3", b"ID3right", "audio/mpeg"),
    ]


def _configure_minimax(username: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = MiniMaxConfig(
            user=user,
            api_key_encrypted=encrypt_secret("test-minimax-key"),
            credential_fingerprint=credential_fingerprint(
                "test-minimax-key"
            ),
            base_url="https://api.minimax.io",
            requests_per_minute=20,
        )
        db.add(config)
        db.flush()
        voice = MiniMaxVoiceAsset(
            id=f"voice-{username}",
            user_id=user.id,
            config_id=config.id,
            name="已保存测试音色",
            voice_id=f"provider-{username}",
            account_binding_id=config.account_binding_id,
            credential_fingerprint=config.credential_fingerprint,
            status=VoiceAssetStatus.READY.value,
            method="clone",
            is_saved=True,
        )
        db.add(voice)
        db.commit()
        return voice.id


def test_batch_page_and_templates_support_excel_and_csv(client):
    create_user("batch-template-user")
    login(client, "batch-template-user")
    page = client.get("/generate/batch")
    assert page.status_code == 200
    assert "下载 Excel 模板" in page.text
    assert ".xlsx / .csv" in page.text
    assert "快速创建" in page.text
    assert "Excel / CSV 表格导入" in page.text
    assert "请注意上传顺序" in page.text
    assert "音频时间由系统自动计算" in page.text
    assert "输入文案并选择音色" in page.text
    script = client.get("/static/batch_generate.js")
    assert script.status_code == 200
    assert "batchParameters" in script.text
    assert "moveAsset" in script.text
    assert "buildAdvancedRows" in script.text
    assert "advancedLeftAudio" in script.text
    assert "每类素材序号与表格行序号一致" in script.text
    assert "speechOptions" in script.text
    assert 'id="speech-pronunciation-tones"' in page.text
    assert 'id="quick-copy-preview-button"' in page.text
    assert 'id="advanced-copy-preview-button"' in page.text
    assert 'id="copy-preview-dialog"' in page.text
    assert "pronunciationTones" in script.text
    assert "openCopyPreview" in script.text
    assert "任务编号,口播脚本" in client.get(
        "/api/batch-templates/script.csv"
    ).content.decode("utf-8-sig")
    assert page.text.index('id="primary-upload-title"') < page.text.index(
        'id="quick-direct-audio-group"'
    )
    assert page.text.index('id="quick-direct-audio-group"') < page.text.index(
        'id="main-audio-upload-title"'
    )
    assert page.text.index('<option value="upload">') < page.text.index(
        '<option value="minimax">'
    )

    xlsx = client.get("/api/batch-templates/digital_human.xlsx")
    csv = client.get("/api/batch-templates/digital_human.csv")
    assert xlsx.status_code == 200
    assert xlsx.content.startswith(b"PK")
    workbook = load_workbook(BytesIO(xlsx.content), read_only=True, data_only=True)
    try:
        sheet = workbook["批量任务"]
        headers = [cell.value for cell in sheet[1]]
        assert headers == ["任务编号", "提示词"]
        assert sheet["B2"].value
        assert "口播脚本" not in headers
    finally:
        workbook.close()
    parsed_digital = parse_manifest(
        "digital.xlsx",
        xlsx.content,
        "digital_human",
        "upload",
    )
    assert parsed_digital.rows[0]["row_id"] == "TASK-001"
    assert parsed_digital.rows[0]["prompt"]
    assert "image_file" not in parsed_digital.rows[0]
    assert "audio_file" not in parsed_digital.rows[0]
    assert csv.status_code == 200
    csv_text = csv.content.decode("utf-8-sig")
    assert csv_text.startswith("任务编号,提示词")
    assert "口播脚本" not in csv_text
    assert "口播脚本（语音生成模式填写）" not in csv_text
    assert "单双人模式" not in csv_text
    assert "开始时间" not in csv_text


def test_minimax_batch_creates_persistent_audio_tasks_before_video_tasks(
    client, monkeypatch
):
    create_user("speech-batch-user")
    voice_id = _configure_minimax("speech-batch-user")
    login(client, "speech-batch-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 20.0,
    )
    image_id = _stage(
        client,
        "image",
        "person.png",
        b"\x89PNG\r\n\x1a\npayload",
        "image/png",
    )
    payload = {
        "name": "脚本语音批次",
        "workflowType": "digital_human",
        "audioMode": "minimax",
        "requestKey": "speech-batch-request",
        "assetIds": [image_id],
        "batchParameters": {"person_mode": "单人", "resolution": "1024"},
        "speechOptions": {
            "voiceAssetId": voice_id,
            "model": "speech-2.8-hd",
            "speed": 1.1,
            "volume": 1,
            "pitch": 0,
            "languageBoost": "Chinese",
            "outputFormat": "mp3",
            "pronunciationTones": (
                '["燕少飞/(yan4)(shao3)(fei1)", "omg/oh my god"]'
            ),
            "costConfirmed": True,
        },
        "rows": [
            {
                "row_id": "speech-001",
                "image_file": "person.png",
                "audio_file": "",
                "speech_script": "这是一条用于生成语音的口播脚本。",
                "prompt": "人物自然地说话。",
            }
        ],
    }
    payload["speechOptions"]["costConfirmed"] = False
    rejected = client.post("/api/batches", json=payload)
    assert rejected.status_code == 400
    assert "确认" in rejected.text

    payload["speechOptions"]["costConfirmed"] = True
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text

    with SessionLocal() as db:
        batch = db.query(GenerationBatch).one()
        audio_task = db.query(AudioGenerationTask).one()
        voices = db.query(MiniMaxVoiceAsset).all()
        assert batch.audio_mode == "minimax"
        assert db.query(GenerationTask).count() == 0
        assert audio_task.status == "PENDING"
        assert json.loads(audio_task.pronunciation_dict_json) == [
            "燕少飞/(yan4)(shao3)(fei1)",
            "omg/oh my god",
        ]
        assert audio_task.speech_script.startswith("这是一条")
        assert [voice.status for voice in voices] == ["READY"]
        assert audio_task.account_binding_id == voices[0].account_binding_id



def test_xlsx_template_can_be_parsed():
    path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "templates"
        / "ltx_lip_sync-batch-template.xlsx"
    )
    parsed = parse_manifest("template.xlsx", path.read_bytes(), "ltx_lip_sync")
    assert parsed.source_format == "xlsx"
    assert parsed.rows[0]["row_id"] == "TASK-001"
    assert parsed.rows[0]["speech_script"].startswith("今天给大家")
    assert "positive_prompt" not in parsed.rows[0]
    assert "source_video_file" not in parsed.rows[0]
    assert "audio_file" not in parsed.rows[0]

    script_path = path.with_name("script-batch-template.xlsx")
    script_manifest = parse_manifest(
        "script.xlsx",
        script_path.read_bytes(),
        "ltx_lip_sync",
        "minimax",
    )
    assert set(script_manifest.rows[0]) >= {
        "row_id",
        "speech_script",
    }
    assert script_manifest.rows[0]["row_id"] == "SCRIPT-001"


def test_legacy_ltx_upload_template_with_positive_prompt_still_parses():
    content = (
        "\ufeff任务编号,源视频文件,音频文件,视频正向提示词\r\n"
        "TASK-001,source.mp4,voice.mp3,一名女性用中文说：“旧模板内容。”\r\n"
    ).encode("utf-8")
    parsed = parse_manifest(
        "legacy.csv",
        content,
        "ltx_lip_sync",
        "upload",
    )
    assert parsed.rows[0]["positive_prompt"] == "一名女性用中文说：“旧模板内容。”"


def test_digital_batch_applies_uniform_dual_settings_and_full_audio_duration(
    client, monkeypatch
):
    user = create_user("digital-batch-user")
    login(client, "digital-batch-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 15.5,
    )
    asset_ids = _digital_assets(client)
    rows = [
        {
            "row_id": "dual-001",
            "image_file": "person.png",
            "audio_file": "voice.mp3",
            "prompt": "第一条双人对话",
            "person_mode": "单人",
            "resolution": "2048",
            "end_time": "0:01",
            "left_audio_file": "left.mp3",
            "right_audio_file": "right.mp3",
        },
        {
            "row_id": "dual-002",
            "image_file": "person.png",
            "audio_file": "voice.mp3",
            "prompt": "第二条双人对话",
            "left_audio_file": "left.mp3",
            "right_audio_file": "right.mp3",
        },
    ]
    payload = {
        "name": "单双人批次",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "digital-batch-request",
        "rows": rows,
        "assetIds": asset_ids,
        "batchParameters": {
            "person_mode": "双人",
            "resolution": "768",
        },
    }
    validation = client.post("/api/batches/validate", json=payload)
    assert validation.status_code == 200
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text

    with SessionLocal() as db:
        batch = db.get(GenerationBatch, response.json()["batchId"])
        tasks = (
            db.query(GenerationTask)
            .filter(GenerationTask.user_id == user.id)
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert batch is not None
        assert batch.total_items == 2
        assert [task.status for task in tasks] == ["PENDING", "PENDING"]
        first = json.loads(tasks[0].input_payload)
        second = json.loads(tasks[1].input_payload)
        assert first["parameters"]["person_mode"] == "0"
        assert first["parameters"]["end_time"] == "0:16"
        assert first["parameters"]["resolution"] == "768"
        assert second["parameters"]["person_mode"] == "0"
        assert set(first["assets"]) == set(second["assets"]) == {
            "image",
            "audio",
            "left_audio",
            "right_audio",
        }


def test_sequence_asset_ids_allow_same_audio_filename_in_separate_groups(
    client, monkeypatch
):
    create_user("sequence-binding-user")
    login(client, "sequence-binding-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 8.0,
    )
    image_id = _stage(
        client,
        "image",
        "person.png",
        b"\x89PNG\r\n\x1a\npayload",
        "image/png",
    )
    total_audio_id = _stage(
        client, "audio", "voice.mp3", b"ID3total", "audio/mpeg"
    )
    left_audio_id = _stage(
        client, "audio", "voice.mp3", b"ID3left", "audio/mpeg"
    )
    right_audio_id = _stage(
        client, "audio", "voice.mp3", b"ID3right", "audio/mpeg"
    )
    payload = {
        "name": "序号绑定批次",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "sequence-binding-request",
        "assetIds": [
            image_id,
            total_audio_id,
            left_audio_id,
            right_audio_id,
        ],
        "batchParameters": {
            "person_mode": "双人",
            "resolution": "1024",
        },
        "rows": [
            {
                "row_id": "TASK-001",
                "prompt": "双人自然对话",
                "image_asset_id": image_id,
                "audio_asset_id": total_audio_id,
                "left_audio_asset_id": left_audio_id,
                "right_audio_asset_id": right_audio_id,
            }
        ],
    }
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text


def test_batch_validation_is_atomic(client, monkeypatch):
    create_user("invalid-batch-user")
    login(client, "invalid-batch-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 10.0,
    )
    asset_ids = _digital_assets(client)
    payload = {
        "name": "无效批次",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "invalid-batch-request",
        "assetIds": asset_ids,
        "rows": [
            {
                "row_id": "valid",
                "image_file": "person.png",
                "audio_file": "voice.mp3",
                "prompt": "有效任务",
            },
            {
                "row_id": "invalid",
                "image_file": "missing.png",
                "audio_file": "voice.mp3",
                "prompt": "无效任务",
            },
        ],
    }
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 400
    assert response.json()["errors"][0]["rowId"] == "invalid"
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 0
        assert db.query(GenerationTask).count() == 0


def test_ltx_upload_derives_positive_prompt_from_script(client, monkeypatch):
    create_user("ltx-batch-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ltx-batch-user").one()
        config = save_workflow_config(
            user,
            "ltx_lip_sync",
            ai_app_id="2080551073030434817",
            instance_type="plus",
            default_prompt="默认正向提示词",
            is_enabled=True,
        )
        db.add(config)
        db.commit()
    login(client, "ltx-batch-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 20.0,
    )
    video_id = _stage(
        client,
        "video",
        "source.mp4",
        b"\x00\x00\x00\x18ftypisompayload",
        "video/mp4",
    )
    audio_id = _stage(
        client,
        "audio",
        "voice.mp3",
        b"ID3voice",
        "audio/mpeg",
    )
    payload = {
        "name": "对口型批次",
        "workflowType": "ltx_lip_sync",
        "audioMode": "upload",
        "requestKey": "ltx-batch-request",
        "assetIds": [video_id, audio_id],
        "rows": [
            {
                "row_id": "ltx-001",
                "source_video_file": "source.mp4",
                "audio_file": "voice.mp3",
                "speech_script": "这是音频中的完整内容。",
                "instance_type": "plus",
            }
        ],
        "batchParameters": {
            "instance_type": "default",
            "prompt_prefix": "一名女性用中文说",
        },
    }
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        manifest = json.loads(task.batch_item.manifest_json)
        task_input = json.loads(task.input_payload)
        assert manifest["speech_script"] == "这是音频中的完整内容。"
        assert manifest["positive_prompt"] == (
            "一名女性用中文说：“这是音频中的完整内容。”"
        )
        assert task_input["parameters"]["prompt"] == (
            "一名女性用中文说：“这是音频中的完整内容。”"
        )
        assert task_input["parameters"]["instance_type"] == "default"


def test_ltx_minimax_prompt_is_generated_from_original_script(client):
    create_user("ltx-speech-prompt-user")
    voice_id = _configure_minimax("ltx-speech-prompt-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(username="ltx-speech-prompt-user").one()
        db.add(
            save_workflow_config(
                user,
                "ltx_lip_sync",
                ai_app_id="2080551073030434817",
                instance_type="plus",
                default_prompt="默认正向提示词",
                is_enabled=True,
            )
        )
        db.commit()
    login(client, "ltx-speech-prompt-user")
    video_id = _stage(
        client,
        "video",
        "source.mp4",
        b"\x00\x00\x00\x18ftypisompayload",
        "video/mp4",
    )
    payload = {
        "name": "对口型脚本批次",
        "workflowType": "ltx_lip_sync",
        "audioMode": "minimax",
        "requestKey": "ltx-speech-prompt-request",
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
                "row_id": "ltx-speech-001",
                "speech_script": "今天给大家介绍我们的新产品。",
            }
        ],
    }

    response = client.post("/api/batches", json=payload)

    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        audio_task = db.query(AudioGenerationTask).one()
        parameters = json.loads(audio_task.video_parameters_json)
        assert parameters["prompt"] == (
            "一名女性用中文说：“今天给大家介绍我们的新产品。”"
        )


def test_batch_retry_and_terminal_delete(client, monkeypatch):
    create_user("batch-manage-user")
    login(client, "batch-manage-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 10.0,
    )
    asset_ids = _digital_assets(client)
    payload = {
        "name": "管理批次",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "batch-manage-request",
        "assetIds": asset_ids,
        "rows": [
            {
                "row_id": "row-1",
                "image_file": "person.png",
                "audio_file": "voice.mp3",
                "prompt": "批次任务",
            }
        ],
    }
    created = client.post("/api/batches", json=payload)
    batch_id = created.json()["batchId"]
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        task.status = TaskStatus.FAILED.value
        task.completed_at = task.updated_at
        db.commit()

    retried = client.post(f"/batches/{batch_id}/retry", follow_redirects=False)
    assert retried.status_code == 303
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        assert task.status == TaskStatus.PENDING.value
        task.status = TaskStatus.SUCCESS.value
        task.completed_at = task.updated_at
        db.commit()

    deleted = client.post(f"/batches/{batch_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 0
        assert db.query(GenerationTask).count() == 0


def test_batch_navigation_detail_return_and_zip_download(client, monkeypatch):
    create_user("batch-download-user")
    login(client, "batch-download-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 10.0,
    )
    asset_ids = _digital_assets(client)
    payload = {
        "name": "可下载批次",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "batch-download-request",
        "assetIds": asset_ids,
        "rows": [
            {
                "row_id": "row-1",
                "image_file": "person.png",
                "audio_file": "voice.mp3",
                "prompt": "批次下载测试",
            }
        ],
    }
    created = client.post("/api/batches", json=payload)
    assert created.status_code == 201
    batch_id = created.json()["batchId"]
    settings = get_settings()
    result = settings.data_dir / "outputs" / "batch-download-result.mp4"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(b"batch video")
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        task_id = task.id
        task.status = TaskStatus.SUCCESS.value
        task.result_path = to_relative_data_path(result, settings)
        db.commit()

    batch_list = client.get("/batches")
    assert "查看单次任务" in batch_list.text
    assert "旧版" not in batch_list.text
    single_list = client.get("/tasks")
    assert "查看批次任务" in single_list.text

    detail = client.get(f"/batches/{batch_id}")
    assert detail.status_code == 200
    assert "下载当前批次（1 个视频）" in detail.text
    task_detail = client.get(f"/tasks/{task_id}")
    assert f'href="/batches/{batch_id}"' in task_detail.text
    assert "返回所属批次" in task_detail.text

    client.post("/logout")
    create_user("batch-download-other-user")
    login(client, "batch-download-other-user")
    assert client.get(f"/batches/{batch_id}/download").status_code == 404
    client.post("/logout")
    login(client, "batch-download-user")

    archive_response = client.get(f"/batches/{batch_id}/download")
    assert archive_response.status_code == 200
    assert archive_response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(archive_response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert names[0].startswith("001-row-1-")
        assert archive.read(names[0]) == b"batch video"
