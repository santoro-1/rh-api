import copy
import hashlib
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jyd_probe import project_h3 as h3
from jyd_probe.h3_audio_cleanup import CleanedSegment, H3_AUDIO_CLEANUP_VERSION
from jyd_probe.project_store import ProjectStore
from test_project_h3 import FakeH3Client, FakeCaptionAligner, fake_media_preparer


def setup_coordinator(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(owner_user_id="u1", owner_username="tester", name="cleanup", items=[{"row_key": "1", "script_text": "你的体重"}])
    client = FakeH3Client()
    client.snapshot = {"batch_id": "h3-batch-1", "status": "SUCCESS", "items": [{"item_id": "remote-row-1", "row_id": "1", "status": "SUCCESS", "segments": [{"segment_id": "s1", "index": 0, "script_text": "你的体重", "status": "SUCCESS", "normalized_video_download_url": "/s1/video", "completed_at": "v1"}]}]}
    store.set_h3_batch_snapshot("u1", project["project_id"], prepare_key="quote-1", snapshot=client.snapshot)
    calls = []
    def media(**kwargs):
        calls.append(copy.copy(kwargs))
        kwargs.pop("segment_audio_paths")
        kwargs.pop("segment_audio_offsets_seconds")
        return fake_media_preparer(**kwargs)
    coordinator = h3.ProjectH3Coordinator(store, client, storage_root=tmp_path / "storage", caption_aligner=FakeCaptionAligner(), media_preparer=media, head_cleanup_enabled=True)
    request = Mock(return_value={"status": "PROCESSING", "version": H3_AUDIO_CLEANUP_VERSION})
    monkeypatch.setattr(h3, "request_cleanup", request)
    return store, project, client, coordinator, calls, request


def fake_ready(source, script):
    key = hashlib.sha256(source.read_bytes()).hexdigest()
    directory = source.parent / key
    directory.mkdir(exist_ok=True)
    audio, preview = directory / "clean.wav", directory / "preview.mp4"
    audio.write_bytes(b"clean PCM")
    preview.write_bytes(b"clean preview")
    return CleanedSegment(directory, key, key, audio, preview, {"audio_offset_seconds": 0.0})


def test_cleanup_pending_blocks_assembly_but_not_raw_download(tmp_path, monkeypatch):
    store, project, client, coordinator, calls, request = setup_coordinator(tmp_path, monkeypatch)
    monkeypatch.setattr(h3, "read_cleanup", lambda *_args: None)
    project = coordinator.sync("u1", project["project_id"], "token")["project"]
    assert not calls
    item = project["items"][0]
    assert item["outputs"]["base_video"] is None
    assert item["settings"]["h3"]["segments"][0]["local_audio_cleanup"]["status"] == "PROCESSING"
    args = dict(item_id=item["item_id"], segment_number=1, storage_root=coordinator.storage_root)
    with pytest.raises(FileNotFoundError, match="清理声音"):
        h3.current_h3_segment_preview_path(project, **args)
    raw = h3.current_h3_segment_preview_path(project, original=True, **args)
    assert raw.read_bytes() == b"video:s1"
    monkeypatch.setattr(h3, "read_cleanup", fake_ready)
    request.return_value = {"status": "READY", "version": H3_AUDIO_CLEANUP_VERSION}
    project = coordinator.sync("u1", project["project_id"], "token")["project"]
    assert len(calls) == 1
    item = project["items"][0]
    assert item["outputs"]["audio"]["metadata"]["head_cleanup_version"] == H3_AUDIO_CLEANUP_VERSION
    assert calls[0]["segment_audio_paths"][0].name == "clean.wav"
    assert calls[0]["segment_paths"][0] == raw
    assert h3.current_h3_segment_preview_path(project, **args).read_bytes() == b"clean preview"
    assert h3.current_h3_segment_preview_path(project, original=True, **args) == raw
    coordinator.sync("u1", project["project_id"], "token")
    assert len(calls) == 1
    assert client.downloads == ["s1"]
    old_audio = item["outputs"]["audio"]["managed_path"]
    client.video_payloads["s1"] = b"replacement"
    client.snapshot["items"][0]["segments"][0]["completed_at"] = "v2"
    changed = coordinator.sync("u1", project["project_id"], "token")["project"]
    assert len(calls) == 2
    assert changed["items"][0]["outputs"]["audio"]["managed_path"] != old_audio
    assert Path(old_audio).exists()


def test_cleanup_failed_is_local_review_and_retry_never_calls_provider(tmp_path, monkeypatch):
    store, project, client, coordinator, calls, request = setup_coordinator(tmp_path, monkeypatch)
    monkeypatch.setattr(h3, "read_cleanup", lambda *_args: None)
    request.return_value = {"status": "FAILED", "error": "ASR offline"}
    project = coordinator.sync("u1", project["project_id"], "token")["project"]
    assert not calls
    assert project["items"][0]["status"] == "H3_REVIEW_REQUIRED"
    client.get_h3_batch = Mock(side_effect=AssertionError("No cloud call on local retry"))
    result = coordinator.retry_local_head_cleanup("u1", project["project_id"], "s1")
    assert result["status"] == "FAILED"
    assert request.call_args.kwargs["force_retry"] is True
    client.get_h3_batch.assert_not_called()
    assert client.downloads == ["s1"]
    with pytest.raises(KeyError):
        coordinator.retry_local_head_cleanup("other-user", project["project_id"], "s1")
    with pytest.raises(ValueError):
        coordinator.retry_local_head_cleanup("u1", project["project_id"], "stale-id")


def test_finished_item_without_cleanup_version_requires_local_upgrade(tmp_path, monkeypatch):
    _, project, _, coordinator, _, _ = setup_coordinator(tmp_path, monkeypatch)
    source = tmp_path / "exists.mp4"
    source.write_bytes(b"exists")
    item = {"settings": {"h3": {"remote_batch_id": "old", "remote_status": "SUCCESS"}}, "outputs": {key: {"managed_path": str(source), "metadata": {}} for key in ("audio", "base_video")}}
    assert coordinator._h3_item_needs_materialization(item)
    item["outputs"]["audio"]["metadata"]["head_cleanup_version"] = H3_AUDIO_CLEANUP_VERSION
    assert not coordinator._h3_item_needs_materialization(item)
