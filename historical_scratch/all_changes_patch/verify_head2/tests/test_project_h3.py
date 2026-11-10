from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_h3 import ProjectH3Coordinator  # noqa: E402
from jyd_probe.project_h3_media import H3MediaAssets  # noqa: E402
from jyd_probe.project_store import ProjectStore  # noqa: E402


class FakeH3Client:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.approvals: list[dict[str, object]] = []
        self.prepared: dict | None = None
        self.snapshot = {
            "batch_id": "h3-batch-1",
            "status": "AWAITING_COST_CONFIRMATION",
            "fee_snapshot": {
                "segment_count": 2,
                "estimated_paid_calls": 2,
            },
            "items": [
                {
                    "item_id": "remote-row-1",
                    "row_id": "1",
                    "status": "AWAITING_COST_CONFIRMATION",
                    "segments": [],
                }
            ],
        }

    def list_h3_execution_accounts(self, token: str) -> dict:
        return {"accounts": [{"id": 7}], "token_seen": token}

    def approve_h3_audio_source(self, token: str, **kwargs: object) -> dict:
        self.approvals.append(dict(kwargs))
        return {
            **kwargs,
            "status": "SUCCESS",
            "reviewed_at": "2026-08-23T00:00:00+00:00",
        }

    def upload_workbench_batch_asset(
        self,
        token: str,
        path: Path,
        *,
        kind: str,
        filename: str,
    ) -> dict:
        self.uploads.append((kind, filename))
        return {"asset_id": f"cloud-{kind}-{len(self.uploads)}"}

    def prepare_h3_batch(self, token: str, payload: dict) -> dict:
        self.prepared = payload
        return dict(self.snapshot)

    def confirm_h3_batch(self, token: str, batch_id: str) -> dict:
        assert batch_id == "h3-batch-1"
        self.snapshot = {
            **self.snapshot,
            "status": "ACTIVE",
            "items": [
                {
                    "item_id": "remote-row-1",
                    "row_id": "1",
                    "status": "PENDING",
                    "segments": [],
                }
            ],
        }
        return dict(self.snapshot)

    def get_h3_batch(self, token: str, batch_id: str) -> dict:
        assert batch_id == "h3-batch-1"
        return dict(self.snapshot)

    def download_h3_segment_video(
        self,
        token: str,
        segment_id: str,
        target: Path,
        *,
        max_bytes: int,
    ) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"video:{segment_id}".encode())
        return target.stat().st_size


class MultiBatchFakeH3Client(FakeH3Client):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: dict[str, dict] = {}
        self.fetched_batch_ids: list[str] = []

    def prepare_h3_batch(self, token: str, payload: dict) -> dict:
        row_id = str(payload["rows"][0]["row_id"])
        batch_id = f"h3-batch-row-{row_id}"
        snapshot = {
            "batch_id": batch_id,
            "status": "AWAITING_COST_CONFIRMATION",
            "fee_snapshot": {"segment_count": 1, "estimated_paid_calls": 1},
            "items": [
                {
                    "item_id": f"remote-row-{row_id}",
                    "row_id": row_id,
                    "status": "AWAITING_COST_CONFIRMATION",
                    "segments": [],
                }
            ],
        }
        self.snapshots[batch_id] = snapshot
        return dict(snapshot)

    def confirm_h3_batch(self, token: str, batch_id: str) -> dict:
        snapshot = self.snapshots[batch_id]
        active = {
            **snapshot,
            "status": "ACTIVE",
            "items": [
                {
                    **snapshot["items"][0],
                    "status": "PENDING",
                }
            ],
        }
        self.snapshots[batch_id] = active
        return dict(active)

    def get_h3_batch(self, token: str, batch_id: str) -> dict:
        self.fetched_batch_ids.append(batch_id)
        return dict(self.snapshots[batch_id])


class FakeCaptionAligner:
    def align(self, audio_path: Path, **kwargs: object) -> dict:
        assert audio_path.is_file()
        script = str(kwargs["script"])
        split = max(1, len(script) // 2)
        return {
            "schema": "jyd.asr-caption-alignment.v1",
            "status": "SUCCESS",
            "audio_asset_id": kwargs["audio_asset_id"],
            "audio_version": kwargs["audio_version"],
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "ranges": [
                {
                    "start": 0,
                    "end": split,
                    "start_us": 100_000,
                    "end_us": 900_000,
                },
                {
                    "start": split,
                    "end": len(script),
                    "start_us": 1_000_000,
                    "end_us": 1_900_000,
                },
            ],
        }


def fake_media_preparer(
    *,
    segment_paths: list[Path],
    segment_texts: list[str],
    script_text: str,
    target_dir: Path,
) -> H3MediaAssets:
    assert segment_paths and len(segment_paths) == len(segment_texts)
    assert "".join(segment_texts) == script_text
    target_dir.mkdir(parents=True, exist_ok=True)
    master = target_dir / "h3-master-av.mp4"
    audio = target_dir / "h3-authoritative-full.wav"
    base = target_dir / "h3-base-video-silent.mp4"
    master.write_bytes(b"master-av")
    audio.write_bytes(b"h3-audio")
    base.write_bytes(b"silent-base")
    return H3MediaAssets(
        master_av_path=master,
        silent_base_video_path=base,
        authoritative_audio_path=audio,
        raw_cues=tuple(
            {
                "text": text,
                "start_us": index * 1_000_000,
                "duration_us": 1_000_000,
                "end_us": (index + 1) * 1_000_000,
                "segment_index": index,
            }
            for index, text in enumerate(segment_texts)
        ),
        segment_durations_seconds=tuple(1.0 for _ in segment_texts),
    )


def test_h3_batch_registry_keeps_multiple_rows_independent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 多批次项目",
        items=[
            {"row_key": "1", "script_text": "第一条"},
            {"row_key": "2", "script_text": "第二条"},
        ],
    )
    project_id = project["project_id"]
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[],
        defaults={
            "continuity_mode": "loop_anchor",
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1,
            "generation_tail_seconds": 0.1,
        },
    )

    first = store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="quote-row-1",
        snapshot={
            "batch_id": "h3-batch-row-1",
            "status": "ACTIVE",
            "fee_snapshot": {"segment_count": 1},
            "items": [
                {
                    "item_id": "remote-row-1",
                    "row_id": "1",
                    "status": "RUNNING",
                    "segments": [],
                }
            ],
        },
    )
    assert first["items"][0]["status"] == "H3_RUNNING"
    assert first["items"][1]["status"] == "DRAFT"

    second = store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="quote-row-2",
        snapshot={
            "batch_id": "h3-batch-row-2",
            "status": "AWAITING_COST_CONFIRMATION",
            "fee_snapshot": {"segment_count": 1},
            "items": [
                {
                    "item_id": "remote-row-2",
                    "row_id": "2",
                    "status": "AWAITING_COST_CONFIRMATION",
                    "segments": [],
                }
            ],
        },
    )

    batches = second["settings"]["h3"]["batches"]
    assert [value["batch_id"] for value in batches] == [
        "h3-batch-row-1",
        "h3-batch-row-2",
    ]
    assert batches[0]["status"] == "ACTIVE"
    assert batches[0]["row_ids"] == ["1"]
    assert batches[1]["status"] == "AWAITING_COST_CONFIRMATION"
    assert batches[1]["row_ids"] == ["2"]
    assert second["items"][0]["status"] == "H3_RUNNING"
    assert second["items"][0]["settings"]["h3"]["remote_batch_id"] == "h3-batch-row-1"
    assert second["items"][1]["status"] == "H3_COST_PENDING"
    assert second["items"][1]["settings"]["h3"]["remote_batch_id"] == "h3-batch-row-2"


def test_h3_coordinator_can_prepare_an_idle_row_while_another_row_runs(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 连续挂任务",
        items=[
            {"row_key": "1", "script_text": "第一条。"},
            {"row_key": "2", "script_text": "第二条。"},
        ],
    )
    project_id = project["project_id"]
    image_path = tmp_path / "identity.png"
    image_path.write_bytes(b"identity")
    image = store.register_input_image(
        owner_user_id="user-1",
        project_id=project_id,
        filename="identity.png",
        content_type="image/png",
        size_bytes=image_path.stat().st_size,
        sha256="a" * 64,
        managed_path=str(image_path),
    )
    store.apply_image_strategy("user-1", project_id, strategy="loop", reuse_count=1)
    for item in project["items"]:
        row_key = str(item["row_key"])
        audio_path = tmp_path / f"voice-{row_key}.mp3"
        audio_path.write_bytes(f"audio-{row_key}".encode())
        store.add_asset(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=item["item_id"],
            asset_type="audio",
            source_type="minimax",
            status="READY",
            filename=audio_path.name,
            managed_path=str(audio_path),
            external_ref={
                "batch_id": f"audio-batch-{row_key}",
                "remote_item_id": f"audio-item-{row_key}",
                "generation_version": 1,
            },
            metadata={"provider_status": "SUCCESS"},
            make_current=True,
        )
        reference_path = tmp_path / f"reference-{row_key}.mp4"
        reference_path.write_bytes(f"video-{row_key}".encode())
        store.add_h3_reference_video(
            owner_user_id="user-1",
            project_id=project_id,
            item_id=item["item_id"],
            filename=reference_path.name,
            managed_path=str(reference_path),
            metadata={"sha256": row_key * 64},
        )
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[image["image_id"]],
        defaults={
            "continuity_mode": "loop_anchor",
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1,
            "generation_tail_seconds": 0.1,
        },
    )
    client = MultiBatchFakeH3Client()
    coordinator = ProjectH3Coordinator(store, client)  # type: ignore[arg-type]
    item_ids = [str(value["item_id"]) for value in project["items"]]

    first = coordinator.prepare(
        "user-1",
        project_id,
        "token",
        idempotency_key="quote-row-1",
        selected_account_ids=[7],
        item_ids=[item_ids[0]],
    )
    coordinator.confirm(
        "user-1",
        project_id,
        "token",
        batch_id=first["h3_batch"]["batch_id"],
    )
    second = coordinator.prepare(
        "user-1",
        project_id,
        "token",
        idempotency_key="quote-row-2",
        selected_account_ids=[7],
        item_ids=[item_ids[1]],
    )

    assert second["h3_batch"]["batch_id"] == "h3-batch-row-2"
    assert [
        value["batch_id"] for value in second["project"]["settings"]["h3"]["batches"]
    ] == ["h3-batch-row-1", "h3-batch-row-2"]
    assert second["project"]["items"][0]["status"] == "H3_RUNNING"
    assert second["project"]["items"][1]["status"] == "H3_COST_PENDING"


def test_h3_sync_recovers_missing_files_from_a_non_latest_successful_batch(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 旧批次本地恢复",
        items=[
            {"row_key": "1", "script_text": "第一条。"},
            {"row_key": "2", "script_text": "第二条。"},
        ],
    )
    project_id = project["project_id"]
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[],
        defaults={
            "continuity_mode": "loop_anchor",
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1,
            "generation_tail_seconds": 0.1,
        },
    )
    client = MultiBatchFakeH3Client()
    item_by_row = {str(item["row_key"]): item for item in project["items"]}
    for row_key in ("1", "2"):
        batch_id = f"h3-success-{row_key}"
        snapshot = {
            "batch_id": batch_id,
            "status": "SUCCESS",
            "items": [
                {
                    "item_id": f"remote-{row_key}",
                    "row_id": row_key,
                    "status": "SUCCESS",
                    "segments": [
                        {
                            "segment_id": f"segment-{row_key}",
                            "index": 0,
                            "script_text": f"第{'一' if row_key == '1' else '二'}条。",
                            "status": "SUCCESS",
                            "normalized_video_download_url": f"/{row_key}.mp4",
                        }
                    ],
                }
            ],
        }
        client.snapshots[batch_id] = snapshot
        store.set_h3_batch_snapshot(
            "user-1",
            project_id,
            prepare_key=f"quote-{row_key}",
            snapshot=snapshot,
        )
        item_id = str(item_by_row[row_key]["item_id"])
        for asset_type, suffix in (("audio", ".wav"), ("base_video", ".mp4")):
            path = tmp_path / f"recorded-{row_key}-{asset_type}{suffix}"
            if row_key == "2":
                path.write_bytes(f"existing-{asset_type}".encode())
            store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=item_id,
                asset_type=asset_type,
                source_type="h3",
                status="READY",
                filename=path.name,
                managed_path=str(path),
                make_current=True,
            )

    coordinator = ProjectH3Coordinator(
        store,
        client,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
        media_preparer=fake_media_preparer,
    )
    synced = coordinator.sync("user-1", project_id, "token")["project"]
    first = next(item for item in synced["items"] if item["row_key"] == "1")

    assert client.fetched_batch_ids == ["h3-success-1", "h3-success-2"]
    assert Path(first["outputs"]["audio"]["managed_path"]).is_file()
    assert Path(first["outputs"]["base_video"]["managed_path"]).is_file()
    assert first["outputs"]["audio"]["metadata"]["h3_segment_signature"]
    assert first["outputs"]["base_video"]["metadata"]["h3_segment_signature"]


def test_h3_project_contract_reuses_existing_audio_and_original_project(
    tmp_path: Path,
) -> None:
    script_text = "第一苹果，第二鸡蛋，第三牛奶。"
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 正式项目",
        items=[{"row_key": "1", "script_text": script_text}],
    )
    project_id = project["project_id"]
    item_id = project["items"][0]["item_id"]
    image_path = tmp_path / "identity.png"
    image_path.write_bytes(b"identity")
    project = store.register_input_image(
        owner_user_id="user-1",
        project_id=project_id,
        filename="identity.png",
        content_type="image/png",
        size_bytes=8,
        sha256="a" * 64,
        managed_path=str(image_path),
    )
    image_id = project["image_id"]
    store.apply_image_strategy(
        "user-1", project_id, strategy="loop", reuse_count=1
    )
    audio_path = tmp_path / "voice.mp3"
    audio_path.write_bytes(b"audio")
    audio_asset = store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="audio",
        source_type="minimax",
        status="READY",
        filename="voice.mp3",
        managed_path=str(audio_path),
        external_ref={
            "batch_id": "audio-batch",
            "remote_item_id": "audio-item",
            "generation_version": 3,
        },
        metadata={"provider_status": "AWAITING_REVIEW"},
        make_current=True,
    )
    store.set_item_subtitles(
        "user-1",
        project_id,
        item_id,
        {
            "source": "minimax_timestamps",
            "raw_cues": [
                {
                    "text": script_text,
                    "start_us": 0,
                    "duration_us": 2_000_000,
                    "end_us": 2_000_000,
                }
            ],
            "render_cues": [],
            "bound_audio_asset_id": audio_asset["asset_id"],
            "status": "READY",
        },
    )
    reference_path = tmp_path / "reference.mp4"
    reference_path.write_bytes(b"video")
    store.add_h3_reference_video(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        filename="reference.mp4",
        managed_path=str(reference_path),
        metadata={"sha256": "b" * 64},
    )
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[image_id],
        defaults={
            "continuity_mode": "loop_anchor",
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1,
            "generation_tail_seconds": 0.1,
        },
    )
    store.set_h3_item_overrides(
        "user-1",
        project_id,
        item_id,
        {
            "user_direction": "人物动作以参考视频为主体，镜头保持稳定。",
            "continuity_mode": "soft_chain",
            "aspect_ratio": "16:9 (Widescreen)",
            "megapixels": 0.8,
        },
    )
    client = FakeH3Client()
    coordinator = ProjectH3Coordinator(
        store,
        client,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
        caption_aligner=FakeCaptionAligner(),
        require_precise_alignment=True,
        media_preparer=fake_media_preparer,
    )

    prepared = coordinator.prepare(
        "user-1",
        project_id,
        "token",
        idempotency_key="quote-1",
        selected_account_ids=[7],
        item_ids=[item_id],
    )
    assert prepared["project"]["project_id"] == project_id
    assert prepared["project"]["items"][0]["status"] == "H3_COST_PENDING"
    assert (
        prepared["project"]["items"][0]["outputs"]["audio"]["asset_id"]
        == audio_asset["asset_id"]
    )
    assert client.approvals == [
        {
            "audio_batch_id": "audio-batch",
            "audio_item_id": "audio-item",
            "audio_generation_version": 3,
        }
    ]
    assert client.prepared is not None
    assert client.prepared["rows"][0]["audio_generation_version"] == 3
    assert client.prepared["rows"][0]["script_text"] == script_text
    alignment = client.prepared["rows"][0]["audio_alignment"]
    assert alignment["schema"] == "jyd.h3-safe-cut-alignment.v1"
    assert alignment["source"] == "jyd_local_funasr"
    assert alignment["audio_sha256"] == hashlib.sha256(b"audio").hexdigest()
    assert alignment["audio_batch_id"] == "audio-batch"
    assert alignment["audio_item_id"] == "audio-item"
    assert alignment["audio_generation_version"] == 3
    assert len(alignment["ranges"]) == 2
    assert client.prepared["rows"][0]["reference_image_asset_ids"] == [
        "cloud-image-1"
    ]
    assert client.prepared["reference_image_asset_ids"] == ["cloud-image-1"]
    assert client.prepared["defaults"]["continuity_mode"] == "loop_anchor"
    assert client.prepared["defaults"]["generation_tail_seconds"] == pytest.approx(0.1)
    assert client.prepared["rows"][0]["overrides"] == {
        "user_direction": "人物动作以参考视频为主体，镜头保持稳定。",
        "continuity_mode": "soft_chain",
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 0.8,
    }
    assert client.uploads == [
        ("image", "identity.png"),
        ("video", "reference.mp4"),
    ]
    with pytest.raises(ValueError, match="未结束"):
        coordinator.prepare(
            "user-1",
            project_id,
            "token",
            idempotency_key="quote-duplicate",
            selected_account_ids=[7],
            item_ids=[item_id],
        )
    confirmed = coordinator.confirm("user-1", project_id, "token")
    assert confirmed["project"]["project_id"] == project_id
    assert confirmed["project"]["settings"]["h3"]["remote_batch_id"] == "h3-batch-1"
    assert confirmed["project"]["items"][0]["status"] == "H3_RUNNING"
    assert confirmed["project"]["items"][0]["outputs"]["audio"]["asset_id"]
    with pytest.raises(ValueError, match="正在生成"):
        store.set_h3_item_overrides(
            "user-1", project_id, item_id, {"megapixels": 1.2}
        )

    client.snapshot = {
        **client.snapshot,
        "status": "SUCCESS",
        "items": [
            {
                "batch_id": "h3-batch-1",
                "item_id": "remote-row-1",
                "row_id": "1",
                "status": "SUCCESS",
                "segments": [
                    {
                        "segment_id": "h3-segment-1",
                        "index": 0,
                        "script_text": "第一苹果，",
                        "status": "SUCCESS",
                        "normalized_video_download_url": "/segment-1/video",
                    },
                    {
                        "segment_id": "h3-segment-2",
                        "index": 1,
                        "script_text": "第二鸡蛋，第三牛奶。",
                        "status": "SUCCESS",
                        "normalized_video_download_url": "/segment-2/video",
                    },
                ],
            }
        ],
    }
    synced = coordinator.sync("user-1", project_id, "token")
    final_item = synced["project"]["items"][0]
    assert final_item["status"] == "BASE_VIDEO_READY"
    assert final_item["outputs"]["audio"]["source_type"] == "h3"
    assert final_item["outputs"]["minimax_audio"]["asset_id"] == audio_asset["asset_id"]
    assert final_item["outputs"]["minimax_audio"]["source_type"] == "minimax"
    assert final_item["outputs"]["base_video"]["source_type"] == "h3"
    assert final_item["subtitles"]["source"] == "h3_generated_audio"
    assert final_item["subtitles"]["asr_alignment"]["status"] == "SUCCESS"
    assert final_item["outputs"]["audio"]["metadata"]["script_sha256"] == (
        hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    )
    assert final_item["outputs"]["audio"]["metadata"]["script_length"] == len(
        script_text
    )
    assert [
        asset["source_type"] for asset in final_item["asset_history"]["audio"]
    ] == ["minimax", "h3"]

    # Reproduce the legacy handoff: the H3 assets have no script binding and a
    # derived preview has already packed two numbered clauses into one cue.
    preview_path = tmp_path / "legacy-preview.mp4"
    preview_path.write_bytes(b"legacy-preview")
    store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="composition_video",
        source_type="postprocess",
        status="READY",
        filename="legacy-preview.mp4",
        managed_path=str(preview_path),
        make_current=True,
    )
    legacy = store.get_project("user-1", project_id)["items"][0]
    legacy_subtitles = dict(legacy["subtitles"])
    legacy_subtitles.update(
        {
            "status": "PREVIEW_READY",
            "render_cues": [
                {
                    "text": "第一苹果第二鸡蛋",
                    "start_us": 0,
                    "end_us": 1_000_000,
                    "duration_us": 1_000_000,
                }
            ],
            "semantic_mapping": {
                "status": "FALLBACK",
                "reason_code": "AUDIO_SCRIPT_VERSION_MISMATCH",
            },
        }
    )
    store.set_item_subtitles(
        "user-1", project_id, item_id, legacy_subtitles
    )
    with store._transaction() as connection:
        for asset_id in (
            legacy["outputs"]["audio"]["asset_id"],
            legacy["outputs"]["base_video"]["asset_id"],
        ):
            row = connection.execute(
                "SELECT metadata_json FROM project_assets WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata.pop("script_sha256", None)
            metadata.pop("script_length", None)
            connection.execute(
                "UPDATE project_assets SET metadata_json=? WHERE asset_id=?",
                (json.dumps(metadata, ensure_ascii=False), asset_id),
            )

    # Re-sync repairs legacy metadata, invalidates only the derived preview and
    # remains idempotent: it must not create duplicate current H3 assets.
    synced_again = coordinator.sync("user-1", project_id, "token")
    repaired_item = synced_again["project"]["items"][0]
    history = repaired_item["asset_history"]
    assert len(history["audio"]) == 2
    assert len(history["base_video"]) == 1
    assert len(history["composition_video"]) == 1
    assert repaired_item["outputs"]["composition_video"] is None
    assert repaired_item["outputs"]["base_video"]["source_type"] == "h3"
    assert repaired_item["subtitles"]["render_cues"] == []
    assert "semantic_mapping" not in repaired_item["subtitles"]
    assert repaired_item["settings"]["composition_invalidated_reason"] == (
        "H3_SCRIPT_BINDING_REPAIRED"
    )
    assert repaired_item["allowed_actions"]["start_postprocess"] is True
    assert repaired_item["outputs"]["audio"]["metadata"]["script_sha256"] == (
        hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    )
    assert repaired_item["outputs"]["audio"]["metadata"]["script_length"] == len(
        script_text
    )

    # A copied deployment may retain database rows while losing derived local
    # files.  Re-sync must rebuild from successful H3 segment results instead
    # of treating matching metadata as proof that the files still exist.
    Path(repaired_item["outputs"]["audio"]["managed_path"]).unlink()
    Path(repaired_item["outputs"]["base_video"]["managed_path"]).unlink()
    healed = coordinator.sync("user-1", project_id, "token")["project"]["items"][0]
    assert Path(healed["outputs"]["audio"]["managed_path"]).is_file()
    assert Path(healed["outputs"]["base_video"]["managed_path"]).is_file()
    assert healed["outputs"]["audio"]["metadata"]["h3_segment_signature"]
    assert healed["outputs"]["base_video"]["metadata"]["h3_segment_signature"]

    # A later H3 quote must still use the reviewed MiniMax input audio, not the
    # H3-generated authoritative output that is now current in JYD.
    coordinator.prepare(
        "user-1",
        project_id,
        "token",
        idempotency_key="quote-2",
        selected_account_ids=[7],
    )
    assert client.prepared is not None
    assert client.prepared["rows"][0]["audio_batch_id"] == "audio-batch"
    assert client.prepared["rows"][0]["audio_item_id"] == "audio-item"
    assert client.prepared["rows"][0]["audio_generation_version"] == 3


def test_h3_uses_the_script_snapshot_bound_to_minimax_audio() -> None:
    cue_script = "第一句，第二句？"
    item = {
        "row_key": "1",
        "script_text": "第一句。第二句？",
        "subtitles": {"raw_cues": [{"text": cue_script}]},
    }
    audio = {
        "metadata": {
            "script_sha256": hashlib.sha256(cue_script.encode("utf-8")).hexdigest()
        }
    }

    assert ProjectH3Coordinator._audio_bound_script(item, audio) == cue_script
