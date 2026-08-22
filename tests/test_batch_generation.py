from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook
import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AudioGenerationTask,
    BATCH_SOURCE_LEGACY_WEB,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    LongAudioProject,
    LongAudioProjectStatus,
    MiniMaxConfig,
    MiniMaxVoiceAsset,
    TaskStatus,
    User,
    VoiceAssetStatus,
)
from app.services.security import encrypt_secret
from app.services.batch_generation import BatchPlan, BatchValidationError, create_batch
from app.services.speech.accounts import credential_fingerprint
from app.services.batch_manifests import parse_manifest
from app.services.long_audio import (
    materialize_long_audio_project,
    serialize_plans,
)
from app.services.media_segmentation import SegmentPlan
from app.services.storage import task_upload_dir, to_relative_data_path
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


def test_legacy_batch_page_hides_new_workbench_batches(client):
    user = create_user("batch-source-isolation-user")
    login(client, user.username)
    with SessionLocal() as db:
        for source_channel, batch_id, name in (
            (BATCH_SOURCE_LEGACY_WEB, "legacy-visible-batch", "旧网页可见批次"),
            (BATCH_SOURCE_NEW_WORKBENCH, "workbench-hidden-batch", "新工作台隐藏批次"),
        ):
            db.add(
                GenerationBatch(
                    id=batch_id,
                    user_id=user.id,
                    name=name,
                    workflow_type="digital_human",
                    source_channel=source_channel,
                    audio_mode="upload",
                    request_key=batch_id,
                    status="ACTIVE",
                    total_items=0,
                )
            )
        db.commit()

    page = client.get("/batches")
    assert page.status_code == 200
    assert "旧网页可见批次" in page.text
    assert "新工作台隐藏批次" not in page.text


def test_idempotency_key_cannot_cross_batch_sources():
    user = create_user("batch-source-key-user")
    with SessionLocal() as db:
        db.add(
            GenerationBatch(
                id="legacy-source-key-batch",
                user_id=user.id,
                name="旧入口批次",
                workflow_type="digital_human",
                source_channel=BATCH_SOURCE_LEGACY_WEB,
                audio_mode="upload",
                request_key="shared-source-key",
                status="ACTIVE",
                total_items=0,
            )
        )
        db.commit()
        with pytest.raises(BatchValidationError) as exc_info:
            create_batch(
                db,
                db.get(User, user.id),
                get_settings(),
                name="新工作台批次",
                request_key="shared-source-key",
                plan=BatchPlan(
                    workflow_type="digital_human",
                    audio_mode="minimax",
                    rows=[],
                    assets=[],
                    source_channel=BATCH_SOURCE_NEW_WORKBENCH,
                ),
            )
        assert "另一入口" in exc_info.value.errors[0]["message"]


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
    assert "长音频拆分后先试听确认" in page.text
    assert "长音频拆分" not in page.text.split("<nav", 1)[-1].split("</nav>", 1)[0]
    assert "输入文案并选择音色" in page.text
    assert 'id="batch-name"' in page.text
    assert "上传音频时默认使用首个音频文件名" in page.text
    assert page.text.index('id="batch-name"') < page.text.index(
        '<details class="advanced-settings">'
    )
    script = client.get("/static/batch_generate.js")
    assert script.status_code == 200
    assert "batchParameters" in script.text
    assert 'id="batch-seedvr2-enabled"' in page.text
    seedvr2_input = page.text.split('id="batch-seedvr2-enabled"', 1)[1].split(
        ">", 1
    )[0]
    assert "checked" not in seedvr2_input
    assert 'seedvr2_enabled' in script.text
    assert "moveAsset" in script.text
    assert "reuseAsset" in script.text
    assert "syncAutoBatchName" in script.text
    assert "identifierWithoutExtension(audioAssets[0].originalName)" in script.text
    assert "batchNameWasEdited" in script.text
    batches_page = client.get("/batches")
    assert batches_page.status_code == 200
    assert "<th>任务名称</th>" in batches_page.text
    assert "image_asset_id: primary.assetId" in script.text
    assert "source_video_asset_id: primary.assetId" in script.text
    assert "不重新上传，复制一条素材引用" in script.text
    assert "同一画面需要对应多条音频" in page.text
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
        assert batch.source_channel == BATCH_SOURCE_LEGACY_WEB
        assert batch.correlation_id
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


def test_digital_batch_rejects_dual_mode_while_unavailable(
    client, monkeypatch
):
    create_user("digital-batch-user")
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
    assert validation.status_code == 400
    assert "双人数字人模式暂未开放" in validation.text


def test_direct_batch_request_cannot_bypass_disabled_dual_mode(
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
    assert response.status_code == 400
    assert "双人数字人模式暂未开放" in response.text


def test_digital_batch_reuses_one_image_for_multiple_audio_rows(
    client, monkeypatch
):
    create_user("digital-one-to-many-user")
    login(client, "digital-one-to-many-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 8.0,
    )
    image_id = _stage(
        client,
        "image",
        "same-person.png",
        b"\x89PNG\r\n\x1a\nsame-person",
        "image/png",
    )
    first_audio_id = _stage(
        client, "audio", "voice.mp3", b"ID3first", "audio/mpeg"
    )
    second_audio_id = _stage(
        client, "audio", "voice.mp3", b"ID3second", "audio/mpeg"
    )
    payload = {
        "name": "一图多音频",
        "workflowType": "digital_human",
        "audioMode": "upload",
        "requestKey": "digital-one-to-many-request",
        "assetIds": [image_id, first_audio_id, second_audio_id],
        "batchParameters": {
            "person_mode": "单人",
            "resolution": "1024",
        },
        "rows": [
            {
                "row_id": "TASK-001",
                "image_asset_id": image_id,
                "image_file": "same-person.png",
                "audio_asset_id": first_audio_id,
                "audio_file": "voice.mp3",
                "prompt": "第一条声音",
            },
            {
                "row_id": "TASK-002",
                "image_asset_id": image_id,
                "image_file": "same-person.png",
                "audio_asset_id": second_audio_id,
                "audio_file": "voice.mp3",
                "prompt": "第二条声音",
            },
        ],
    }

    validation = client.post("/api/batches/validate", json=payload)
    assert validation.status_code == 200, validation.text
    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text

    with SessionLocal() as db:
        tasks = db.query(GenerationTask).order_by(
            GenerationTask.created_at
        ).all()
        assert len(tasks) == 2
        assert [task.image_original_name for task in tasks] == [
            "same-person.png",
            "same-person.png",
        ]
        assert [task.audio_original_name for task in tasks] == [
            "voice.mp3",
            "voice.mp3",
        ]
        assert tasks[0].image_path != tasks[1].image_path
        assert tasks[0].audio_path != tasks[1].audio_path
        assert all(task.seedvr2_enabled is False for task in tasks)
        assert all(
            json.loads(task.input_payload)["parameters"]["seedvr2_enabled"]
            is False
            for task in tasks
        )


def test_ltx_batch_reuses_one_video_for_multiple_audio_rows(
    client, monkeypatch
):
    create_user("ltx-one-to-many-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(
            username="ltx-one-to-many-user"
        ).one()
        db.add(
            save_workflow_config(
                user,
                "ltx_lip_sync",
                ai_app_id="2080551073030434817",
                instance_type="default",
                default_prompt="默认正向提示词",
                is_enabled=True,
            )
        )
        db.commit()
    login(client, "ltx-one-to-many-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 12.0,
    )
    video_id = _stage(
        client,
        "video",
        "same-source.mp4",
        b"\x00\x00\x00\x18ftypisomsame-video",
        "video/mp4",
    )
    first_audio_id = _stage(
        client, "audio", "speech.mp3", b"ID3first", "audio/mpeg"
    )
    second_audio_id = _stage(
        client, "audio", "speech.mp3", b"ID3second", "audio/mpeg"
    )
    payload = {
        "name": "一视频多音频",
        "workflowType": "ltx_lip_sync",
        "audioMode": "upload",
        "requestKey": "ltx-one-to-many-request",
        "assetIds": [video_id, first_audio_id, second_audio_id],
        "batchParameters": {
            "instance_type": "default",
            "prompt_prefix": "一名女性用中文说",
        },
        "rows": [
            {
                "row_id": "TASK-001",
                "source_video_asset_id": video_id,
                "source_video_file": "same-source.mp4",
                "audio_asset_id": first_audio_id,
                "audio_file": "speech.mp3",
                "speech_script": "这是第一条声音。",
            },
            {
                "row_id": "TASK-002",
                "source_video_asset_id": video_id,
                "source_video_file": "same-source.mp4",
                "audio_asset_id": second_audio_id,
                "audio_file": "speech.mp3",
                "speech_script": "这是第二条声音。",
            },
        ],
    }

    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        tasks = db.query(GenerationTask).order_by(
            GenerationTask.created_at
        ).all()
        assert len(tasks) == 2
        assert [task.image_original_name for task in tasks] == [
            "same-source.mp4",
            "same-source.mp4",
        ]
        assert tasks[0].image_path != tasks[1].image_path


def test_uploaded_batch_automatically_expands_only_long_audio_rows(
    client, monkeypatch
):
    create_user("mixed-duration-batch-user")
    with SessionLocal() as db:
        user = db.query(User).filter_by(
            username="mixed-duration-batch-user"
        ).one()
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
    login(client, "mixed-duration-batch-user")

    def duration_for_file(path):
        content = Path(path).read_bytes()
        return 21.0 if b"long-audio" in content else 20.0

    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        duration_for_file,
    )
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_media_duration",
        lambda path: 90.0,
    )
    video_id = _stage(
        client,
        "video",
        "source.mp4",
        b"\x00\x00\x00\x18ftypisomsource-video",
        "video/mp4",
    )
    short_audio_id = _stage(
        client,
        "audio",
        "short.mp3",
        b"ID3short-audio",
        "audio/mpeg",
    )
    long_audio_id = _stage(
        client,
        "audio",
        "long.mp3",
        b"ID3long-audio",
        "audio/mpeg",
    )
    payload = {
        "name": "长短音频混合批次",
        "workflowType": "ltx_lip_sync",
        "audioMode": "upload",
        "requestKey": "mixed-duration-batch-request",
        "longAudioReviewRequired": True,
        "videoReviewRequired": True,
        "assetIds": [video_id, short_audio_id, long_audio_id],
        "batchParameters": {
            "instance_type": "plus",
            "prompt_prefix": "一名人物用中文说",
        },
        "rows": [
            {
                "row_id": "TASK-001",
                "source_video_asset_id": video_id,
                "audio_asset_id": short_audio_id,
                "speech_script": "这是短音频。",
            },
            {
                "row_id": "TASK-002",
                "source_video_asset_id": video_id,
                "audio_asset_id": long_audio_id,
                "speech_script": "这是需要自动拆分的完整长音频脚本。",
            },
        ],
    }

    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text
    batch_id = response.json()["batchId"]
    with SessionLocal() as db:
        batch = db.get(GenerationBatch, batch_id)
        items = (
            db.query(GenerationBatchItem)
            .filter_by(batch_id=batch_id)
            .order_by(GenerationBatchItem.row_number)
            .all()
        )
        project = db.query(LongAudioProject).one()
        assert batch is not None
        assert batch.total_items == 2
        assert batch.review_required is True
        assert batch.video_review_required is True
        assert items[0].generation_task is not None
        assert items[1].generation_task is None
        assert items[1].status == "SEGMENTING"
        assert project.batch_item_id == items[1].id
        assert project.batch_id is None
        assert project.review_required is True
        assert project.status == LongAudioProjectStatus.PENDING_ANALYSIS.value

        project.plan_json = serialize_plans(
            [
                SegmentPlan(
                    index=1,
                    script_text="这是需要自动拆分的",
                    start_seconds=0.0,
                    end_seconds=10.5,
                    alignment_method="test",
                ),
                SegmentPlan(
                    index=2,
                    script_text="完整长音频脚本。",
                    start_seconds=10.5,
                    end_seconds=21.0,
                    alignment_method="test",
                ),
            ]
        )
        project.status = LongAudioProjectStatus.PENDING_CUT.value
        db.commit()

        def fake_audio_cut(source, target, **kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ID3segment")

        def fake_video_cut(source, target, **kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00\x00\x00\x18ftypisomsegment")

        monkeypatch.setattr(
            "app.services.long_audio.cut_audio_segment",
            fake_audio_cut,
        )
        monkeypatch.setattr(
            "app.services.long_audio.cut_video_segment",
            fake_video_cut,
        )
        monkeypatch.setattr(
            "app.services.long_audio.inspect_audio_duration",
            lambda path: 10.5,
        )
        monkeypatch.setattr(
            "app.services.long_audio.inspect_media_duration",
            lambda path: 10.5,
        )
        materialized = materialize_long_audio_project(
            db,
            project,
            get_settings(),
        )
        db.commit()
        db.refresh(project)
        db.refresh(items[1])
        assert materialized.id == batch_id
        assert project.status == LongAudioProjectStatus.COMPLETED.value
        assert project.batch_id is None
        assert items[1].status == "SEGMENTS_CREATED"
        assert len(items[1].segments) == 2
        assert db.query(GenerationTask).count() == 3


def test_cancelling_long_audio_review_stays_local_and_updates_batch_page(
    client,
):
    user = create_user("cancel-long-review-user")
    login(client, "cancel-long-review-user")
    batch_id = "cancel-long-review-batch"
    item_id = "cancel-long-review-item"
    project_id = "cancel-long-review-project"
    with SessionLocal() as db:
        user = db.get(User, user.id)
        batch = GenerationBatch(
            id=batch_id,
            user_id=user.id,
            name="取消审核测试",
            workflow_type="ltx_lip_sync",
            audio_mode="upload",
            review_required=True,
            request_key="cancel-long-review-request",
            status="ACTIVE",
            total_items=1,
        )
        item = GenerationBatchItem(
            id=item_id,
            batch=batch,
            row_number=1,
            row_key="TASK-001",
            manifest_json=json.dumps(
                {
                    "row_id": "TASK-001",
                    "source_video_file": "source.mp4",
                    "audio_file": "long.mp3",
                    "speech_script": "等待审核的长音频。",
                },
                ensure_ascii=False,
            ),
            audio_status="AWAITING_REVIEW",
            status="AWAITING_REVIEW",
        )
        project = LongAudioProject(
            id=project_id,
            user_id=user.id,
            batch_item=item,
            name="取消审核测试",
            workflow_type="ltx_lip_sync",
            review_required=True,
            script_text="等待审核的长音频。",
            audio_path="long-audio/cancel/audio.mp3",
            audio_original_name="long.mp3",
            video_path="long-audio/cancel/source.mp4",
            video_original_name="source.mp4",
            duration_seconds=60.0,
            parameters_json="{}",
            plan_json="[]",
            alignment_provider="funasr_http",
            status=LongAudioProjectStatus.REVIEW.value,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add_all([batch, item, project])
        db.commit()

    response = client.post(
        f"/batches/{batch_id}/cancel",
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        project = db.get(LongAudioProject, project_id)
        item = db.get(GenerationBatchItem, item_id)
        assert project.status == LongAudioProjectStatus.CANCELLED.value
        assert item.status == "CANCELLED"
        assert db.query(GenerationTask).count() == 0

    page = client.get(f"/batches/{batch_id}")
    assert page.status_code == 200
    assert "已取消（尚未提交生成）" in page.text
    assert "正在自动处理" not in page.text


def test_minimax_batch_reuses_one_primary_asset_for_multiple_scripts(client):
    create_user("speech-one-to-many-user")
    voice_id = _configure_minimax("speech-one-to-many-user")
    login(client, "speech-one-to-many-user")
    image_id = _stage(
        client,
        "image",
        "same-person.png",
        b"\x89PNG\r\n\x1a\nsame-person",
        "image/png",
    )
    payload = {
        "name": "一图多脚本",
        "workflowType": "digital_human",
        "audioMode": "minimax",
        "requestKey": "speech-one-to-many-request",
        "assetIds": [image_id],
        "batchParameters": {
            "person_mode": "单人",
            "resolution": "1024",
        },
        "speechOptions": {
            "voiceAssetId": voice_id,
            "costConfirmed": True,
        },
        "rows": [
            {
                "row_id": "SCRIPT-001",
                "image_asset_id": image_id,
                "image_file": "same-person.png",
                "speech_script": "第一条生成语音。",
                "prompt": "人物自然说话",
            },
            {
                "row_id": "SCRIPT-002",
                "image_asset_id": image_id,
                "image_file": "same-person.png",
                "speech_script": "第二条生成语音。",
                "prompt": "人物自然说话",
            },
        ],
    }

    response = client.post("/api/batches", json=payload)
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        audio_tasks = db.query(AudioGenerationTask).order_by(
            AudioGenerationTask.created_at
        ).all()
        assert len(audio_tasks) == 2
        assert [task.primary_original_name for task in audio_tasks] == [
            "same-person.png",
            "same-person.png",
        ]
        assert audio_tasks[0].primary_path != audio_tasks[1].primary_path


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
    initial_view_revision = client.get(
        f"/api/batches/{batch_id}"
    ).json()["viewRevision"]
    active_list = client.get("/batches")
    assert (
        f'action="/batches/{batch_id}/delete"'
        not in active_list.text
    )
    assert "处理中不可删除" in active_list.text
    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        task.status = TaskStatus.FAILED.value
        task.runninghub_task_id = "failed-batch-remote-id"
        task.error_code = "805"
        task.error_message = "显存不足；失败节点：WanVideoEncode"
        task.runninghub_auto_retry_count = 2
        task.runninghub_attempt_history = json.dumps(
            [{"taskId": "previous-failed-remote-id"}]
        )
        task.completed_at = task.updated_at
        db.commit()

    terminal_list = client.get("/batches")
    assert f'action="/batches/{batch_id}/delete"' in terminal_list.text
    assert "删除记录" in terminal_list.text
    detail = client.get(f"/batches/{batch_id}")
    assert "所属账号：" in detail.text
    assert "batch-manage-user" in detail.text
    assert "failed-batch-remote-id" in detail.text
    assert "显存不足；失败节点：WanVideoEncode" in detail.text
    assert "自动重试 2/3" in detail.text
    status = client.get(f"/api/batches/{batch_id}").json()
    assert status["viewRevision"] != initial_view_revision
    assert status["items"][0]["runninghubTaskId"] == "failed-batch-remote-id"
    assert status["items"][0]["errorMessage"] == (
        "显存不足；失败节点：WanVideoEncode"
    )
    assert status["items"][0]["autoRetryCount"] == 2
    assert (
        status["items"][0]["lastFailedRunninghubTaskId"]
        == "previous-failed-remote-id"
    )

    retried = client.post(f"/batches/{batch_id}/retry", follow_redirects=False)
    assert retried.status_code == 303
    assert "retried=1" in retried.headers["location"]
    retry_page = client.get(retried.headers["location"])
    assert "每次重试都会生成新的 RunningHub 任务 ID" in retry_page.text
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


def test_locally_stuck_batch_without_worker_task_can_be_deleted(
    client,
    monkeypatch,
):
    create_user("stuck-batch-delete-user")
    login(client, "stuck-batch-delete-user")
    monkeypatch.setattr(
        "app.services.batch_generation.inspect_audio_duration",
        lambda path: 10.0,
    )
    asset_ids = _digital_assets(client)
    created = client.post(
        "/api/batches",
        json={
            "name": "本地卡住批次",
            "workflowType": "digital_human",
            "audioMode": "upload",
            "requestKey": "stuck-batch-delete-request",
            "assetIds": asset_ids,
            "rows": [
                {
                    "row_id": "row-1",
                    "image_file": "person.png",
                    "audio_file": "voice.mp3",
                    "prompt": "本地尚未提交的卡住任务",
                }
            ],
        },
    )
    batch_id = created.json()["batchId"]

    with SessionLocal() as db:
        task = db.query(GenerationTask).one()
        db.delete(task)
        db.commit()

    history = client.get("/batches")
    assert f'action="/batches/{batch_id}/delete"' in history.text
    deleted = client.post(
        f"/batches/{batch_id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    with SessionLocal() as db:
        assert db.get(GenerationBatch, batch_id) is None


def test_locally_stuck_segment_batch_deletes_derived_upload_directory(client):
    user = create_user("stuck-segment-delete-user")
    login(client, "stuck-segment-delete-user")
    batch_id = "stuck-segment-batch"
    planned_task_id = "stuck-segment-planned-task"
    upload_dir = task_upload_dir(
        get_settings(),
        user.id,
        planned_task_id,
    )
    segment_dir = upload_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    audio_path = segment_dir / "segment-001.mp3"
    audio_path.write_bytes(b"stuck segment")

    with SessionLocal() as db:
        batch = GenerationBatch(
            id=batch_id,
            user_id=user.id,
            name="卡住的分段批次",
            workflow_type="digital_human",
            audio_mode="minimax",
            request_key="stuck-segment-batch-request",
            status="ACTIVE",
            total_items=1,
        )
        item = GenerationBatchItem(
            id="stuck-segment-item",
            batch=batch,
            row_number=1,
            row_key="row-1",
            manifest_json=json.dumps({"row_id": "row-1"}),
            audio_status="SEGMENTING",
            status="SEGMENTS_CREATED",
        )
        item.segments.append(
            GenerationSegment(
                id="stuck-segment",
                segment_index=1,
                script_text="卡住的分段",
                start_seconds=0,
                end_seconds=10,
                audio_path=to_relative_data_path(
                    audio_path,
                    get_settings(),
                ),
                prompt="卡住的分段",
                status="TASK_CREATED",
            )
        )
        db.add(batch)
        db.commit()

    deleted = client.post(
        f"/batches/{batch_id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert not upload_dir.exists()
    with SessionLocal() as db:
        assert db.get(GenerationBatch, batch_id) is None


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
    assert "下载当前批次分段（1 个视频）" in detail.text
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
