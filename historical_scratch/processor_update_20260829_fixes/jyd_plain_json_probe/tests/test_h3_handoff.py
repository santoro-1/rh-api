from __future__ import annotations

import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from jyd_probe.h3_handoff import H3HandoffError, import_h3_handoff, load_h3_handoff
from jyd_probe.project_store import ProjectStore


def _manifest(
    tmp_path: Path,
    *,
    audio_policy: str = "separate_h3_generated_audio",
    schema_version: str = "h3.jyd_handoff.v2",
) -> Path:
    base = tmp_path / "h3-base-silent.mp4"
    master = tmp_path / "h3-master-av.mp4"
    audio = tmp_path / "h3-generated-full.wav"
    cues = tmp_path / "h3-segment-cue-windows.json"
    base.write_bytes(b"video-only")
    master.write_bytes(b"h3-generated-audio-video")
    audio.write_bytes(b"h3-generated-audio")
    cues.write_text(
        json.dumps(
            [
                {"text": "第一句。", "start_seconds": 0, "end_seconds": 2.5},
                {"text": "第二句。", "start_seconds": 2.5, "end_seconds": 5},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "jyd-handoff.json"
    project_id = "h3-local:row-1"
    segment_ids = ["segment-1", "segment-2"]
    identity = {"project_id": project_id, "segment_ids": segment_ids}
    if schema_version == "h3.jyd_handoff.v2":
        identity["schema_version"] = schema_version
    handoff_id = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "project_id": project_id,
                "handoff_id": handoff_id,
                "source": {"row_key": "ROW-001", "script_text": "第一句。第二句。"},
                **(
                    {
                        "h3_master": {
                            "path": str(master),
                            "audio_video_pair": "h3_generated",
                        }
                    }
                    if schema_version == "h3.jyd_handoff.v2"
                    else {}
                ),
                "base_video": {
                    "path": str(base),
                    "role": "base_video",
                    "audio_policy": audio_policy,
                    "source_segment_ids": segment_ids,
                },
                "authoritative_audio": {
                    "path": str(audio),
                    **(
                        {"source": "h3_generated_audio"}
                        if schema_version == "h3.jyd_handoff.v2"
                        else {}
                    ),
                    "timeline_start_seconds": 0.0,
                    "reuse_once": True,
                },
                "subtitles": {
                    "raw_cues_asset": str(cues),
                    "timing_source": (
                        "h3_segment_windows_then_funasr"
                        if schema_version == "h3.jyd_handoff.v2"
                        else "authoritative_full_audio"
                    ),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest


def test_import_h3_handoff_creates_base_video_audio_and_bound_raw_cues(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = import_h3_handoff(
        store,
        owner_user_id="user-1",
        owner_username="tester",
        project_name="H3 自动后期",
        manifest_path=_manifest(tmp_path),
    )

    assert project["status"] == "BASE_VIDEO_READY"
    item = project["items"][0]
    assert item["row_key"] == "ROW-001"
    assert item["script_text"] == "第一句。第二句。"
    assert item["outputs"]["audio"]["source_type"] == "h3_handoff"
    assert item["outputs"]["base_video"]["source_type"] == "h3_handoff"
    assert item["outputs"]["base_video"]["metadata"]["audio_policy"] == (
        "separate_h3_generated_audio"
    )
    assert item["outputs"]["audio"]["metadata"]["source"] == "h3_generated_audio"
    assert item["subtitles"]["source"] == "h3_segment_windows"
    assert item["subtitles"]["bound_audio_asset_id"] == item["outputs"]["audio"]["asset_id"]
    assert item["subtitles"]["raw_cues"][1]["start_us"] == 2_500_000
    assert item["subtitles"]["render_cues"] == item["subtitles"]["raw_cues"]


def test_h3_handoff_rejects_replacing_generated_audio_policy(tmp_path: Path) -> None:
    with pytest.raises(H3HandoffError, match="拆分后的 H3 生成音轨"):
        load_h3_handoff(_manifest(tmp_path, audio_policy="no_h3_audio"))


def test_legacy_v1_handoff_remains_readable(tmp_path: Path) -> None:
    handoff = load_h3_handoff(
        _manifest(
            tmp_path,
            audio_policy="no_h3_audio",
            schema_version="h3.jyd_handoff.v1",
        )
    )

    assert handoff["schema_version"] == "h3.jyd_handoff.v1"
    assert handoff["master_path"] is None


def test_h3_handoff_rejects_tampered_identity_or_script_cues(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["handoff_id"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(H3HandoffError, match="编号与分段来源不一致"):
        load_h3_handoff(manifest_path)

    manifest_path = _manifest(tmp_path)
    cues_path = Path(
        json.loads(manifest_path.read_text(encoding="utf-8"))["subtitles"][
            "raw_cues_asset"
        ]
    )
    cues = json.loads(cues_path.read_text(encoding="utf-8"))
    cues[1]["text"] = "被替换的字幕。"
    cues_path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(H3HandoffError, match="raw cues 与冻结脚本不一致"):
        load_h3_handoff(manifest_path)


def test_repeated_h3_handoff_import_reuses_existing_project(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "control.db")
    manifest = _manifest(tmp_path)

    first = import_h3_handoff(
        store,
        owner_user_id="user-1",
        owner_username="tester",
        project_name="H3 自动后期",
        manifest_path=manifest,
    )
    repeated = import_h3_handoff(
        store,
        owner_user_id="user-1",
        owner_username="tester",
        project_name="H3 自动后期（重复）",
        manifest_path=manifest,
    )

    assert repeated["project_id"] == first["project_id"]
    assert store.list_projects("user-1")["total"] == 1


def test_concurrent_h3_handoff_import_is_idempotent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "control.db")
    manifest = _manifest(tmp_path)

    def run_import() -> str:
        project = import_h3_handoff(
            store,
            owner_user_id="user-1",
            owner_username="tester",
            project_name="H3 并发交接",
            manifest_path=manifest,
        )
        return project["project_id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        project_ids = list(executor.map(lambda _: run_import(), range(2)))

    assert project_ids[0] == project_ids[1]
    assert store.list_projects("user-1")["total"] == 1


def test_h3_handoff_import_rolls_back_everything_on_mid_transaction_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    manifest = _manifest(tmp_path)
    original = store._set_current_asset
    calls = 0

    def fail_on_base_video(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected base-video failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_set_current_asset", fail_on_base_video)
    with pytest.raises(RuntimeError, match="injected base-video failure"):
        import_h3_handoff(
            store,
            owner_user_id="user-1",
            owner_username="tester",
            project_name="H3 原子交接",
            manifest_path=manifest,
        )

    assert store.list_projects("user-1")["total"] == 0
