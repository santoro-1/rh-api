import copy
from dataclasses import replace
import json
from pathlib import Path
import wave

import pytest

from jyd_probe import project_h3 as h3
from jyd_probe.h3_video_segments import H3_VIDEO_SEQUENCE_VERSION, h3_video_sequence_ready
from jyd_probe.project_h3_media import build_segment_cues
from jyd_probe.project_video_source import build_project_video_source, project_segment_boundaries
from test_h3_audio_cleanup_coordinator import setup_coordinator, fake_ready
from test_project_h3 import fake_media_preparer


def three_segments(tmp_path, monkeypatch):
    store, project, client, coordinator, calls, request = setup_coordinator(tmp_path, monkeypatch)
    texts = ["你", "的", "体重"]
    durations = [8.0, 11.541667, 9.416667]
    row = client.snapshot["items"][0]
    row["segments"] = [dict(row["segments"][0], segment_id=f"s{i + 1}", index=i, script_text=text)
                       for i, text in reversed(list(enumerate(texts)))]
    monkeypatch.setattr(h3, "read_cleanup", fake_ready)
    monkeypatch.setattr(h3, "_probe_duration", lambda path: durations[int(path.read_bytes()[-1:]) - 1])
    request.return_value = {"status": "READY"}

    def prepare(**kwargs):
        calls.append(copy.copy(kwargs))
        kwargs.pop("segment_audio_paths")
        kwargs.pop("segment_audio_offsets_seconds")
        return replace(fake_media_preparer(**kwargs), segment_durations_seconds=tuple(durations),
                       raw_cues=tuple(build_segment_cues(texts, durations, script_text="你的体重")))

    coordinator.media_preparer = prepare
    project = coordinator.sync("u1", project["project_id"], "token")["project"]
    return store, project, client, coordinator, calls


def test_h3_keeps_exact_order_duration_and_clean_audio(tmp_path, monkeypatch):
    store, project, client, coordinator, calls = three_segments(tmp_path, monkeypatch)
    item = project["items"][0]
    assert h3_video_sequence_ready(item)
    source = build_project_video_source(item)
    assert source["type"] == "video_sequence"
    assert [Path(s["media_path"]).read_bytes() for s in source["items"]] == [b"video:s1", b"video:s2", b"video:s3"]
    assert [s["target_duration_us"] for s in source["items"]] == [8_000_000, 11_541_667, 9_416_667]
    assert [s.get("transition_after_us", 0) for s in source["items"]] == [500_000, 500_000, 0]
    assert all(s["volume"] == 0 for s in source["items"])
    assert [b["boundary_us"] for b in project_segment_boundaries(item)] == [8_000_000, 19_541_667]
    assert all("segment-cache" not in s["media_path"] for s in source["items"])
    assert len(calls[0]["segment_audio_paths"]) == 3
    again = coordinator.sync("u1", project["project_id"], "token")["project"]["items"][0]
    assert again["outputs"]["audio"]["asset_id"] == item["outputs"]["audio"]["asset_id"]
    assert len(again["outputs"]["original_video_segments"]) == 3
    assert len(calls) == 1


def test_h3_never_uses_input_tail_or_newest_unbound_asset(tmp_path, monkeypatch):
    _, project, _, _, _ = three_segments(tmp_path, monkeypatch)
    item = project["items"][0]
    expected = build_project_video_source(item)
    item["outputs"]["base_video"]["metadata"]["planned_duration_us"] = 40_000_000
    for asset in item["outputs"]["original_video_segments"]:
        asset["metadata"].update(speech_duration_seconds=1, generation_tail_seconds=0.1)
    decoy = copy.deepcopy(item["outputs"]["original_video_segments"][0])
    decoy.update(asset_id="newer-unbound", version=999)
    decoy["external_ref"]["batch_id"] = "other-batch"
    item["outputs"]["original_video_segments"].append(decoy)
    assert build_project_video_source(item) == expected
    item["outputs"]["original_video_segments"][0]["external_ref"]["batch_id"] = "other-batch"
    with pytest.raises(ValueError, match="版本不匹配"):
        build_project_video_source(item)
    assert project_segment_boundaries(item) == []


def test_old_clean_result_backfills_without_asr_or_audio_rebuild(tmp_path, monkeypatch):
    store, project, client, coordinator, calls = three_segments(tmp_path, monkeypatch)
    item = project["items"][0]
    old_base = item["outputs"]["base_video"]
    audio = item["outputs"]["audio"]
    with store._transaction() as connection:
        metadata = dict(old_base["metadata"])
        for key in ("segment_count", "source_segment_asset_ids", "video_sequence_version"):
            metadata.pop(key)
        connection.execute("UPDATE project_assets SET metadata_json=? WHERE asset_id=?", (json.dumps(metadata), old_base["asset_id"]))
    legacy = store.get_project("u1", project["project_id"])["items"][0]
    assert coordinator._h3_item_needs_materialization(legacy)
    with pytest.raises(ValueError, match="独立片段"):
        build_project_video_source(legacy)
    coordinator.caption_aligner.align = lambda *a, **k: pytest.fail("backfill must not rerun ASR")
    rebuilt = coordinator.sync("u1", project["project_id"], "token")["project"]["items"][0]
    assert h3_video_sequence_ready(rebuilt)
    assert rebuilt["outputs"]["audio"] == audio
    assert rebuilt["outputs"]["base_video"]["asset_id"] != old_base["asset_id"]
    assert rebuilt["outputs"]["base_video"]["managed_path"] == old_base["managed_path"]
    assert rebuilt["subtitles"]["asr_alignment"] == item["subtitles"]["asr_alignment"]
    assert len(calls) == 1
    assert len(client.downloads) == 3


def test_regeneration_does_not_change_old_draft_paths(tmp_path, monkeypatch):
    _, project, client, coordinator, _ = three_segments(tmp_path, monkeypatch)
    old_item = project["items"][0]
    old_paths = [Path(s["media_path"]) for s in build_project_video_source(old_item)["items"]]
    old_bytes = [p.read_bytes() for p in old_paths]
    client.video_payloads["s2"] = b"regenerated:s2"
    next(s for s in client.snapshot["items"][0]["segments"] if s["segment_id"] == "s2")["completed_at"] = "v2"
    new_item = coordinator.sync("u1", project["project_id"], "token")["project"]["items"][0]
    assert h3_video_sequence_ready(new_item)
    assert [p.read_bytes() for p in old_paths] == old_bytes
    assert Path(build_project_video_source(new_item)["items"][1]["media_path"]).read_bytes() == b"regenerated:s2"
    assert new_item["outputs"]["base_video"]["metadata"]["source_segment_asset_ids"] != old_item["outputs"]["base_video"]["metadata"]["source_segment_asset_ids"]


def test_missing_frozen_clip_recovers_from_local_cache(tmp_path, monkeypatch):
    _, project, client, coordinator, calls = three_segments(tmp_path, monkeypatch)
    item = project["items"][0]
    Path(build_project_video_source(item)["items"][1]["media_path"]).unlink()
    assert not h3_video_sequence_ready(item)
    with pytest.raises(ValueError, match="缺失"):
        build_project_video_source(item)
    restored = coordinator.sync("u1", project["project_id"], "token")["project"]["items"][0]
    assert h3_video_sequence_ready(restored)
    assert len(calls) == 1 and len(client.downloads) == 3


@pytest.mark.parametrize("has_placeholder", [False, True])
def test_postprocess_template_receives_sequence_not_merged_video(tmp_path, monkeypatch, has_placeholder):
    from jyd_probe import project_postprocess as pp
    _, project, _, _, _ = three_segments(tmp_path, monkeypatch)
    item = project["items"][0]
    item["subtitles"]["status"] = "PREVIEW_READY"
    template = tmp_path / "template"
    template.mkdir()
    (template / "draft_content.json").write_text('{"tracks": []}', encoding="utf-8")
    item["settings"]["postprocess"] = {"jianying_template": {"template_id": "template", "draft_dir": str(template),
        "profile": {"main_video": {"typed_track_index": 0, "segment_index": 0} if has_placeholder else {}}}}
    monkeypatch.setattr(pp, "layout_font", lambda *args: {"path": str(tmp_path / "font.ttf"), "resource_id": "font", "name": "font"})
    monkeypatch.setattr(pp, "detect_caption_tracks", lambda *args, **kwargs: [{"track_id": "captions", "typed_track_index": 0}])
    coordinator = object.__new__(pp.ProjectPostprocessCoordinator)
    coordinator.fonts = []
    coordinator.bgm_assets = {}
    coordinator.draft_root = tmp_path / "output"
    job = coordinator._build_draft_job(item, draft_name="independent", skip_export=True)
    assert job["source"]["type"] == "template"
    assert len(job["main_video_sequence"]["items"]) == 3
    assert job["main_video_sequence"]["track_index"] == (0 if has_placeholder else -1)
    assert "video_replacements" not in job and "visual_overlays" not in job
    assert job["audios"][0]["media_path"] == item["outputs"]["audio"]["managed_path"]


@pytest.fixture
def real_clips(tmp_path):
    import cv2
    import numpy as np
    paths = []
    for i in range(3):
        path = tmp_path / f"part-{i + 1}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 96))
        assert writer.isOpened()
        for _ in range(10):
            writer.write(np.full((96, 64, 3), 50 + i * 60, dtype=np.uint8))
        writer.release()
        paths.append(path)
    return paths


@pytest.mark.parametrize("template_mode", [False, True, "no-placeholder"])
def test_real_render_draft_keeps_three_clips_and_one_audio(tmp_path, real_clips, template_mode):
    from jyd_probe.cli import load_plain_draft_json, save_plain_draft_json
    from jyd_probe.draft_factory import create_plain_draft_from_video
    from jyd_probe.render_job import run_render_job
    speech = tmp_path / "clean-authority.wav"
    with wave.open(str(speech), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 48_000)
    items = [{"media_path": str(p), "target_duration_us": 1_000_000, "volume": 0.0,
              "transition_after_us": 500_000 if i < 2 else 0} for i, p in enumerate(real_clips)]
    config = {"source": {"type": "video_sequence", "items": items, "work_root": str(tmp_path / "work")},
              "audios": [{"type": "add", "media_path": str(speech), "fit_to_video": True}],
              "original_video_volume": 0.0,
              "output": {"draft_root": str(tmp_path / "drafts"), "draft_name": "independent", "skip_export": True}}
    if template_mode:
        template = create_plain_draft_from_video(real_clips[0], tmp_path / "templates", draft_name="template")
        original = load_plain_draft_json(template.draft_dir)
        if template_mode == "no-placeholder":
            original["tracks"] = []
        else:
            original["tracks"][0]["segments"][0]["clip"]["transform"]["x"] = 0.15
        save_plain_draft_json(template.draft_dir, original)
        before = (template.draft_dir / "draft_content.json").read_bytes()
        config["source"] = {"type": "template", "template_draft_dir": str(template.draft_dir)}
        config["timeline_duration_us"] = 3_000_000
        config["main_video_sequence"] = {"items": items, "track_index": -1 if template_mode == "no-placeholder" else 0}
    result = run_render_job(config)
    data = load_plain_draft_json(result.output_draft_dir)
    main = next(t for t in data["tracks"] if t["type"] == "video")
    assert len(main["segments"]) == 3
    assert [s["target_timerange"]["start"] for s in main["segments"]] == [0, 1_000_000, 2_000_000]
    assert all(s["speed"] == 1.0 and s["volume"] == 0 for s in main["segments"])
    assert data["duration"] == 3_000_000
    videos = {m["id"]: m for m in data["materials"]["videos"]}
    assert [Path(videos[s["material_id"]]["path"]) for s in main["segments"]] == real_clips
    assert len(data["materials"]["transitions"]) == 2
    tracks = [t for t in data["tracks"] if t["type"] == "audio"]
    assert len(tracks) == 1 and len(tracks[0]["segments"]) == 1
    if template_mode:
        assert (template.draft_dir / "draft_content.json").read_bytes() == before
        if template_mode is True:
            assert all(s["clip"]["transform"]["x"] == 0.15 for s in main["segments"])
