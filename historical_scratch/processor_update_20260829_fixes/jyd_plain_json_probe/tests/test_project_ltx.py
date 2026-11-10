from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_ltx import ProjectLtxCoordinator  # noqa: E402
from jyd_probe.project_store import ProjectStore  # noqa: E402


class FakeLtxClient:
    def __init__(self) -> None:
        self.sync_payload = None
        self.download_calls = 0

    def sync(self, token, project_id, payload):
        assert token == "cloud-token"
        assert project_id
        self.sync_payload = payload
        return {"state": {"items": [], "active": False}}

    def refresh(self, token, project_id, item_ids):
        assert token == "cloud-token"
        return {
            "active": False,
            "items": [
                {
                    "item_id": item_ids[0],
                    "status": "COMPLETED",
                    "source_video": {
                        "filename": "source.mp4",
                        "sha256": "source-sha",
                        "version": 1,
                    },
                    "remote_batch_id": "ltx-batch",
                    "remote_item_id": "ltx-item",
                    "segments": [{"segment_id": "segment-1"}],
                    "base_video_ready": True,
                }
            ],
        }

    def upload_source_video(self, token, project_id, item_id, path, *, filename):
        assert token == "cloud-token"
        assert Path(path).read_bytes()
        return {
            "active": False,
            "items": [
                {
                    "item_id": item_id,
                    "status": "READY",
                    "source_video": {"filename": filename, "version": 2},
                }
            ],
        }

    def download_base_video(self, token, project_id, item_id, destination):
        assert token == "cloud-token"
        self.download_calls += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"ltx-seedvr2-video")
        return destination


def test_switching_back_to_h3_restores_the_h3_result_view(tmp_path):
    store = ProjectStore(tmp_path / "projects.sqlite3")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="模式结果切换",
        items=[{"row_key": "1", "script_text": "切换回来仍显示 H3。"}],
        settings={"generation_mode": "runninghub_digital_human"},
    )
    project_id = project["project_id"]
    item_id = project["items"][0]["item_id"]
    defaults = {
        "continuity_mode": "loop_anchor",
        "aspect_ratio": "9:16 (Portrait Widescreen)",
        "megapixels": 1,
        "generation_tail_seconds": 0.1,
    }
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[],
        defaults=defaults,
    )
    h3_audio_path = tmp_path / "h3.wav"
    h3_audio_path.write_bytes(b"h3-audio")
    h3_cues = [
        {"start_us": 0, "duration_us": 1_000_000, "text": "H3 字幕。"}
    ]
    h3_audio = store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="audio",
        source_type="h3",
        status="READY",
        filename="h3.wav",
        managed_path=str(h3_audio_path),
        metadata={"subtitle_cues": h3_cues},
        make_current=True,
    )
    h3_video_path = tmp_path / "h3.mp4"
    h3_video_path.write_bytes(b"h3-video")
    h3_video = store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="base_video",
        source_type="h3",
        status="READY",
        filename="h3.mp4",
        managed_path=str(h3_video_path),
        make_current=True,
    )
    subtitles = store.get_project("user-1", project_id)["items"][0]["subtitles"]
    subtitles.update(
        {
            "source": "h3_generated_audio",
            "raw_cues": h3_cues,
            "render_cues": h3_cues,
            "bound_audio_asset_id": h3_audio["asset_id"],
            "bound_video_asset_id": h3_video["asset_id"],
            "status": "READY",
        }
    )
    store.set_item_subtitles("user-1", project_id, item_id, subtitles)

    ltx = store.set_generation_mode("user-1", project_id, "ltx_lip_sync")
    assert ltx["items"][0]["outputs"]["base_video"] is None
    h3_view = ltx["items"][0]["settings"]["generation_mode_views"][
        "minimax_h3_ref2va"
    ]
    assert h3_view["base_video_asset_id"] == h3_video["asset_id"]

    restored = store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[],
        defaults=defaults,
    )
    restored_item = restored["items"][0]
    assert restored["settings"]["generation_mode"] == "minimax_h3_ref2va"
    assert restored_item["outputs"]["audio"]["asset_id"] == h3_audio["asset_id"]
    assert (
        restored_item["outputs"]["base_video"]["asset_id"]
        == h3_video["asset_id"]
    )
    assert restored_item["subtitles"]["raw_cues"] == h3_cues
    assert restored_item["subtitles"]["bound_video_asset_id"] == h3_video["asset_id"]


def test_running_h3_batch_survives_switch_to_ltx_and_back(tmp_path):
    store = ProjectStore(tmp_path / "projects.sqlite3")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 后台运行切换",
        items=[{"row_key": "1", "script_text": "H3 切走后继续运行。"}],
        settings={"generation_mode": "runninghub_digital_human"},
    )
    project_id = project["project_id"]
    item_id = project["items"][0]["item_id"]
    defaults = {
        "continuity_mode": "loop_anchor",
        "aspect_ratio": "9:16 (Portrait Widescreen)",
        "megapixels": 1,
        "generation_tail_seconds": 0.1,
    }
    store.set_h3_configuration(
        "user-1", project_id, identity_image_ids=[], defaults=defaults
    )
    running = store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="prepare-1",
        snapshot={
            "batch_id": "h3-batch-running",
            "status": "ACTIVE",
            "items": [
                {
                    "row_id": "1",
                    "item_id": "h3-item-running",
                    "status": "RUNNING",
                    "segments": [],
                }
            ],
        },
    )
    assert running["items"][0]["status"] == "H3_RUNNING"

    ltx = store.set_generation_mode("user-1", project_id, "ltx_lip_sync")
    assert ltx["settings"]["generation_mode"] == "ltx_lip_sync"
    assert ltx["settings"]["h3"]["remote_batch_id"] == "h3-batch-running"
    assert (
        ltx["items"][0]["settings"]["h3"]["remote_item_id"]
        == "h3-item-running"
    )

    restored = store.set_h3_configuration(
        "user-1", project_id, identity_image_ids=[], defaults=defaults
    )
    assert restored["settings"]["generation_mode"] == "minimax_h3_ref2va"
    assert restored["settings"]["h3"]["remote_status"] == "ACTIVE"
    assert restored["settings"]["h3"]["remote_batch_id"] == "h3-batch-running"


def test_ltx_is_a_third_pipeline_and_reuses_the_unmodified_minimax_source(tmp_path):
    store = ProjectStore(tmp_path / "projects.sqlite3")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="三引擎项目",
        items=[{"row_key": "1", "script_text": "测试对口型。"}],
        settings={"generation_mode": "runninghub_digital_human"},
    )
    project_id = project["project_id"]
    item_id = project["items"][0]["item_id"]
    audio_path = tmp_path / "speech.mp3"
    audio_path.write_bytes(b"original-minimax-speech-without-provider-tail")
    audio = store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="audio",
        source_type="minimax",
        status="READY",
        filename="speech.mp3",
        managed_path=str(audio_path),
        external_ref={
            "batch_id": "audio-batch",
            "remote_item_id": "audio-item",
            "generation_version": 3,
        },
        metadata={
            "provider_status": "SUCCESS",
            "subtitle_cues": [
                {
                    "start_us": 0,
                    "duration_us": 900_000,
                    "text": "测试对口型。",
                }
            ],
        },
        make_current=True,
    )
    subtitles = store.get_project("user-1", project_id)["items"][0]["subtitles"]
    subtitles["raw_cues"] = [{"start_us": 0, "duration_us": 900_000, "text": "测试对口型。"}]
    subtitles["bound_audio_asset_id"] = audio["asset_id"]
    subtitles["status"] = "READY"
    store.set_item_subtitles("user-1", project_id, item_id, subtitles)

    # H3 owns a different authoritative audio while its route is active.  When
    # switching to LTX, the original MiniMax source and its cues must be restored.
    h3_path = tmp_path / "h3.wav"
    h3_path.write_bytes(b"h3-authoritative-audio")
    h3_audio = store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="audio",
        source_type="h3",
        status="READY",
        filename="h3.wav",
        managed_path=str(h3_path),
        make_current=True,
    )
    h3_subtitles = store.get_project("user-1", project_id)["items"][0]["subtitles"]
    h3_subtitles.update(
        {
            "source": "h3_generated_audio",
            "raw_cues": [
                {"start_us": 0, "duration_us": 800_000, "text": "H3 字幕。"}
            ],
            "bound_audio_asset_id": h3_audio["asset_id"],
            "status": "READY",
        }
    )
    store.set_item_subtitles("user-1", project_id, item_id, h3_subtitles)

    ltx_project = store.set_generation_mode("user-1", project_id, "ltx_lip_sync")
    assert ltx_project["settings"]["generation_mode"] == "ltx_lip_sync"
    assert ltx_project["items"][0]["outputs"]["audio"]["asset_id"] == audio["asset_id"]
    assert ltx_project["items"][0]["subtitles"]["raw_cues"][0]["text"] == "测试对口型。"

    client = FakeLtxClient()
    result = ProjectLtxCoordinator(
        store, client, storage_root=tmp_path / "storage"
    ).refresh("user-1", project_id, "cloud-token")

    synced_audio = client.sync_payload["items"][0]["audio"]
    assert synced_audio == {
        "batch_id": "audio-batch",
        "item_id": "audio-item",
        "generation_version": 3,
    }
    item = result["project"]["items"][0]
    assert item["outputs"]["audio"]["asset_id"] == audio["asset_id"]
    assert item["outputs"]["base_video"]["source_type"] == "ltx"
    assert item["outputs"]["base_video"]["metadata"]["enhanced_by"] == "seedvr2"
    assert item["subtitles"]["raw_cues"][0]["text"] == "测试对口型。"
    assert client.download_calls == 1

    # Completed LTX results are idempotent, while normal/H3 remain separate modes.
    ProjectLtxCoordinator(store, client, storage_root=tmp_path / "storage").refresh(
        "user-1", project_id, "cloud-token"
    )
    assert client.download_calls == 1
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement-source-video")
    replaced = ProjectLtxCoordinator(
        store, client, storage_root=tmp_path / "storage"
    ).upload_source_video(
        "user-1",
        project_id,
        item_id,
        "cloud-token",
        replacement,
        filename="replacement.mp4",
    )
    assert replaced["project"]["items"][0]["outputs"]["base_video"] is None
    assert (
        replaced["project"]["items"][0]["settings"][
            "composition_invalidated_reason"
        ]
        == "LTX_SOURCE_VIDEO_CHANGED"
    )
    normal = store.set_generation_mode(
        "user-1", project_id, "runninghub_digital_human"
    )
    assert normal["settings"]["generation_mode"] == "runninghub_digital_human"
