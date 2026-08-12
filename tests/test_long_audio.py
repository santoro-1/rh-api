from __future__ import annotations

from pathlib import Path
import json

from app.database import SessionLocal
from app.models import (
    GenerationBatch,
    GenerationTask,
    LongAudioProject,
    LongAudioProjectStatus,
    User,
)
from app.services.alignment import AlignmentResult
from app.services.media_segmentation import SegmentPlan
from app.services.workflow_configs import save_workflow_config
from app.workers.media_worker import process_next
from tests.conftest import create_user, login


SCRIPT = "今天是星期四。我要吃肯德基。但是我下班很晚。"


class FakeAlignmentProvider:
    name = "heuristic"

    def align(self, audio_path: Path, script: str) -> AlignmentResult:
        assert audio_path.is_file()
        assert script == SCRIPT
        return AlignmentResult(
            provider=self.name,
            plans=(
                SegmentPlan(
                    index=1,
                    script_text="今天是星期四。",
                    start_seconds=0,
                    end_seconds=30,
                    alignment_method="punctuation_silence",
                ),
                SegmentPlan(
                    index=2,
                    script_text="我要吃肯德基。",
                    start_seconds=30,
                    end_seconds=60,
                    alignment_method="punctuation_silence",
                ),
                SegmentPlan(
                    index=3,
                    script_text="但是我下班很晚。",
                    start_seconds=60,
                    end_seconds=90,
                    alignment_method="punctuation_estimate",
                ),
            ),
        )


def _configure_ltx(username: str) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        config = save_workflow_config(
            user,
            "ltx_lip_sync",
            ai_app_id="ltx-test-app",
            instance_type="default",
            default_prompt="测试",
            is_enabled=True,
        )
        db.add(config)
        db.commit()


def _fake_cut(_source, output, **_kwargs):
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix == ".mp3":
        output.write_bytes(b"ID3segment")
    else:
        output.write_bytes(b"\x00\x00\x00\x18ftypisomsegment")


def test_long_audio_review_and_handoff_flow(client, monkeypatch):
    create_user("long-audio-user")
    _configure_ltx("long-audio-user")
    login(client, "long-audio-user")

    monkeypatch.setattr(
        "app.services.long_audio.get_alignment_provider",
        lambda _name: FakeAlignmentProvider(),
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration",
        lambda path: 30.0 if "segment-" in Path(path).name else 90.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration",
        lambda _path: 100.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.cut_audio_segment",
        _fake_cut,
    )
    monkeypatch.setattr(
        "app.services.long_audio.cut_video_segment",
        _fake_cut,
    )

    response = client.post(
        "/api/long-audio-projects",
        data={
            "name": "七分钟口播",
            "scriptText": SCRIPT,
            "promptPrefix": "一名人物用中文说",
            "instanceType": "default",
            "alignmentProvider": "heuristic",
            "reviewRequired": "true",
        },
        files={
            "customAudio": ("long.mp3", b"ID3long-audio", "audio/mpeg"),
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisomvideo",
                "video/mp4",
            ),
        },
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["projectId"]

    with SessionLocal() as db:
        assert process_next(db) is True

    status = client.get(f"/api/long-audio-projects/{project_id}")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == LongAudioProjectStatus.REVIEW.value
    assert len(payload["segments"]) == 3
    assert payload["segments"][0]["confidence"] == "high"
    assert payload["segments"][2]["confidence"] == "low"

    saved = client.put(
        f"/api/long-audio-projects/{project_id}/plan",
        json={
            "segments": [
                {
                    "startSeconds": 0,
                    "endSeconds": 30,
                    "scriptText": "今天是星期四。",
                },
                {
                    "startSeconds": 30,
                    "endSeconds": 60,
                    "scriptText": "我要吃肯德基。",
                },
                {
                    "startSeconds": 60,
                    "endSeconds": 90,
                    "scriptText": "但是我下班很晚。",
                },
            ]
        },
    )
    assert saved.status_code == 200, saved.text

    confirmed = client.post(
        f"/api/long-audio-projects/{project_id}/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == LongAudioProjectStatus.PENDING_CUT.value

    with SessionLocal() as db:
        assert process_next(db) is True

    completed = client.get(f"/api/long-audio-projects/{project_id}").json()
    assert completed["status"] == LongAudioProjectStatus.COMPLETED.value
    assert completed["batchId"]

    with SessionLocal() as db:
        project = db.get(LongAudioProject, project_id)
        batch = db.get(GenerationBatch, completed["batchId"])
        tasks = (
            db.query(GenerationTask)
            .filter(GenerationTask.segment_id.is_not(None))
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert project is not None
        assert batch is not None
        assert batch.total_items == 1
        assert len(tasks) == 3
        assert tasks[0].prompt.endswith("“今天是星期四。”")
        assert tasks[1].audio_duration_seconds == 30

    duplicate = client.post(
        f"/api/long-audio-projects/{project_id}/confirm"
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["batchId"] == completed["batchId"]


def test_review_plan_rejects_gaps_and_missing_original_script(client, monkeypatch):
    create_user("long-audio-plan-user")
    _configure_ltx("long-audio-plan-user")
    login(client, "long-audio-plan-user")
    monkeypatch.setattr(
        "app.services.long_audio.get_alignment_provider",
        lambda _name: FakeAlignmentProvider(),
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration",
        lambda _path: 90.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration",
        lambda _path: 100.0,
    )
    created = client.post(
        "/api/long-audio-projects",
        data={
            "name": "检查分段",
            "scriptText": SCRIPT,
            "promptPrefix": "一名人物用中文说",
            "instanceType": "default",
            "reviewRequired": "true",
        },
        files={
            "customAudio": ("long.mp3", b"ID3long-audio", "audio/mpeg"),
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisomvideo",
                "video/mp4",
            ),
        },
    )
    project_id = created.json()["projectId"]
    with SessionLocal() as db:
        process_next(db)

    response = client.put(
        f"/api/long-audio-projects/{project_id}/plan",
        json={
            "segments": [
                {
                    "startSeconds": 0,
                    "endSeconds": 30,
                    "scriptText": "今天是星期四。",
                },
                {
                    "startSeconds": 31,
                    "endSeconds": 60,
                    "scriptText": "漏掉内容。",
                },
                {
                    "startSeconds": 60,
                    "endSeconds": 90,
                    "scriptText": "但是我下班很晚。",
                },
            ]
        },
    )
    assert response.status_code == 400
    assert "时间不连续" in response.json()["detail"]

    reanalyze = client.post(f"/long-audio/{project_id}/reanalyze")
    assert reanalyze.status_code == 200
    with SessionLocal() as db:
        project = db.get(LongAudioProject, project_id)
        assert project is not None
        assert project.status == LongAudioProjectStatus.PENDING_ANALYSIS.value
        assert project.alignment_provider == "funasr_http"
        assert project.plan_json is None


def test_digital_human_long_audio_auto_splits_without_asr(client, monkeypatch):
    create_user("digital-long-audio-user")
    login(client, "digital-long-audio-user")
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration",
        lambda path: 30.0 if "segment-" in Path(path).name else 90.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.detect_silence_midpoints",
        lambda _path: [30.0, 60.0],
    )
    monkeypatch.setattr(
        "app.services.long_audio.cut_audio_segment",
        _fake_cut,
    )
    monkeypatch.setattr(
        "app.services.long_audio.get_alignment_provider",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("数字人长音频不应调用 ASR")
        ),
    )

    created = client.post(
        "/api/long-audio-projects",
        data={
            "name": "数字人长音频",
            "workflowType": "digital_human",
            "digitalPrompt": "人物自然说话并轻微挥手。",
            "seedvr2Enabled": "false",
        },
        files={
            "customAudio": ("long.mp3", b"ID3long-audio", "audio/mpeg"),
            "sourceImage": (
                "person.png",
                b"\x89PNG\r\n\x1a\nperson",
                "image/png",
            ),
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["projectId"]

    with SessionLocal() as db:
        assert process_next(db) is True
    status = client.get(f"/api/long-audio-projects/{project_id}").json()
    assert status["status"] == LongAudioProjectStatus.PENDING_CUT.value
    assert status["workflowType"] == "digital_human"
    assert status["reviewRequired"] is False
    assert len(status["segments"]) == 3

    with SessionLocal() as db:
        assert process_next(db) is True
    completed = client.get(f"/api/long-audio-projects/{project_id}").json()
    assert completed["status"] == LongAudioProjectStatus.COMPLETED.value

    with SessionLocal() as db:
        tasks = (
            db.query(GenerationTask)
            .filter(GenerationTask.workflow_type == "digital_human")
            .order_by(GenerationTask.created_at)
            .all()
        )
        assert len(tasks) == 3
        assert all(task.prompt == "人物自然说话并轻微挥手。" for task in tasks)
        assert all(task.start_seconds == 0 for task in tasks)
        assert all(task.end_seconds == 30 for task in tasks)
        payloads = [json.loads(task.input_payload) for task in tasks]
        assert all("image" in payload["assets"] for payload in payloads)
        assert all(payload["parameters"]["instance_type"] == "plus" for payload in payloads)
        assert all(payload["parameters"]["seedvr2_enabled"] is False for payload in payloads)
        assert all(task.seedvr2_enabled is False for task in tasks)


def test_long_audio_ltx_defaults_to_plus_instance(client, monkeypatch):
    create_user("long-audio-plus-user")
    _configure_ltx("long-audio-plus-user")
    login(client, "long-audio-plus-user")
    monkeypatch.setattr(
        "app.services.long_audio.get_alignment_provider",
        lambda _name: FakeAlignmentProvider(),
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_audio_duration",
        lambda _path: 90.0,
    )
    monkeypatch.setattr(
        "app.services.long_audio.inspect_media_duration",
        lambda _path: 100.0,
    )
    created = client.post(
        "/api/long-audio-projects",
        data={
            "name": "默认 48G",
            "scriptText": SCRIPT,
            "promptPrefix": "一名人物用中文说",
        },
        files={
            "customAudio": ("long.mp3", b"ID3long-audio", "audio/mpeg"),
            "sourceVideo": (
                "source.mp4",
                b"\x00\x00\x00\x18ftypisomvideo",
                "video/mp4",
            ),
        },
    )
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        project = db.get(LongAudioProject, created.json()["projectId"])
        assert json.loads(project.parameters_json)["instance_type"] == "plus"
