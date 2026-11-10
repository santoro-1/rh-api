from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_h3 import (  # noqa: E402
    ProjectH3Coordinator,
    current_h3_segment_preview_path,
)
from jyd_probe.project_h3_media import H3MediaAssets  # noqa: E402
from jyd_probe.project_store import ProjectStore  # noqa: E402


class FakeH3Client:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.approvals: list[dict[str, object]] = []
        self.downloads: list[str] = []
        self.video_payloads: dict[str, bytes] = {}
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
        self.downloads.append(segment_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            self.video_payloads.get(segment_id, f"video:{segment_id}".encode())
        )
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


def test_h3_sync_downloads_successful_segments_incrementally_and_versions_preview(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 逐段下载",
        items=[{"row_key": "1", "script_text": "第一段，第二段。"}],
    )
    project_id = str(project["project_id"])
    item_id = str(project["items"][0]["item_id"])
    store.set_h3_configuration(
        "user-1",
        project_id,
        identity_image_ids=[],
        defaults={
            "continuity_mode": "soft_chain",
            "aspect_ratio": "9:16 (Portrait Widescreen)",
            "megapixels": 1,
            "generation_tail_seconds": 0.1,
        },
    )
    first_payload = b"video:h3-segment-1"
    first_sha256 = hashlib.sha256(first_payload).hexdigest()
    partial_snapshot = {
        "batch_id": "h3-batch-1",
        "status": "ACTIVE",
        "items": [
            {
                "item_id": "remote-row-1",
                "row_id": "1",
                "status": "RUNNING",
                "segments": [
                    {
                        "segment_id": "h3-segment-1",
                        "index": 0,
                        "script_text": "第一段，",
                        "status": "SUCCESS",
                        "normalized_video_download_url": "/segment-1/video",
                        "normalized_video_sha256": first_sha256,
                        "completed_at": "2026-08-28T01:00:00+00:00",
                    },
                    {
                        "segment_id": "h3-segment-2",
                        "index": 1,
                        "script_text": "第二段。",
                        "status": "RUNNING",
                        "normalized_video_download_url": None,
                        "normalized_video_sha256": None,
                        "completed_at": None,
                    },
                ],
            }
        ],
    }
    store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="quote-1",
        snapshot=partial_snapshot,
    )
    client = FakeH3Client()
    client.snapshot = partial_snapshot
    storage_root = tmp_path / "storage"
    coordinator = ProjectH3Coordinator(
        store,
        client,  # type: ignore[arg-type]
        storage_root=storage_root,
        media_preparer=fake_media_preparer,
    )

    first_sync = coordinator.sync("user-1", project_id, "token")["project"]
    first_item = first_sync["items"][0]
    assert client.downloads == ["h3-segment-1"]
    assert first_item["outputs"]["base_video"] is None
    assert first_item["settings"]["h3"]["segments"][0][
        "local_preview_ready"
    ] is True
    assert first_item["settings"]["h3"]["segments"][0][
        "local_preview_is_current"
    ] is True
    preview = current_h3_segment_preview_path(
        first_sync,
        item_id=item_id,
        segment_number=1,
        storage_root=storage_root,
    )
    assert preview.read_bytes() == first_payload

    coordinator.sync("user-1", project_id, "token")
    assert client.downloads == ["h3-segment-1"]

    regenerating_snapshot = copy.deepcopy(partial_snapshot)
    regenerating_segment = regenerating_snapshot["items"][0]["segments"][0]
    regenerating_segment.update(
        {
            "status": "PENDING",
            "normalized_video_download_url": None,
            "normalized_video_sha256": None,
            "completed_at": None,
        }
    )
    client.snapshot = regenerating_snapshot
    regenerating = coordinator.sync("user-1", project_id, "token")["project"]
    regenerating_local = regenerating["items"][0]["settings"]["h3"]["segments"][0]
    assert regenerating_local["local_preview_ready"] is True
    assert regenerating_local["local_preview_is_current"] is False
    assert current_h3_segment_preview_path(
        regenerating,
        item_id=item_id,
        segment_number=1,
        storage_root=storage_root,
    ).read_bytes() == first_payload

    regenerated_payload = b"video:h3-segment-1:version-2"
    regenerated_snapshot = copy.deepcopy(partial_snapshot)
    regenerated_snapshot["items"][0]["segments"][0].update(
        {
            "normalized_video_sha256": hashlib.sha256(
                regenerated_payload
            ).hexdigest(),
            "completed_at": "2026-08-28T02:00:00+00:00",
        }
    )
    client.video_payloads["h3-segment-1"] = regenerated_payload
    client.snapshot = regenerated_snapshot
    regenerated = coordinator.sync("user-1", project_id, "token")["project"]
    assert client.downloads == ["h3-segment-1", "h3-segment-1"]
    assert current_h3_segment_preview_path(
        regenerated,
        item_id=item_id,
        segment_number=1,
        storage_root=storage_root,
    ).read_bytes() == regenerated_payload

    second_payload = b"video:h3-segment-2"
    completed_snapshot = copy.deepcopy(regenerated_snapshot)
    completed_snapshot["status"] = "SUCCESS"
    completed_snapshot["items"][0]["status"] = "SUCCESS"
    completed_snapshot["items"][0]["segments"][1].update(
        {
            "status": "SUCCESS",
            "normalized_video_download_url": "/segment-2/video",
            "normalized_video_sha256": hashlib.sha256(second_payload).hexdigest(),
            "completed_at": "2026-08-28T03:00:00+00:00",
        }
    )
    client.snapshot = completed_snapshot
    completed = coordinator.sync("user-1", project_id, "token")["project"]
    assert client.downloads == [
        "h3-segment-1",
        "h3-segment-1",
        "h3-segment-2",
    ]
    assert completed["items"][0]["status"] == "BASE_VIDEO_READY"
    assert completed["items"][0]["outputs"]["base_video"]["source_type"] == "h3"


def test_h3_retry_rejects_a_segment_replaced_by_a_newer_batch(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 旧分段重试",
        items=[{"row_key": "1", "script_text": "测试脚本。"}],
    )
    project_id = str(project["project_id"])
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
    store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="old-quote",
        snapshot={
            "batch_id": "old-batch",
            "status": "FAILED",
            "items": [{
                "item_id": "old-item",
                "row_id": "1",
                "status": "FAILED",
                "segments": [{
                    "segment_id": "old-segment",
                    "segment_index": 1,
                    "status": "FAILED",
                    "can_retry": True,
                }],
            }],
        },
    )
    store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="new-quote",
        snapshot={
            "batch_id": "new-batch",
            "status": "FAILED",
            "items": [{
                "item_id": "new-item",
                "row_id": "1",
                "status": "FAILED",
                "segments": [{
                    "segment_id": "new-segment",
                    "segment_index": 1,
                    "status": "FAILED",
                    "can_retry": True,
                }],
            }],
        },
    )
    coordinator = ProjectH3Coordinator(store, FakeH3Client())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="当前 H3 批次已更新"):
        coordinator.prepare_retry("user-1", project_id, "token", "old-segment")


def test_h3_snapshot_does_not_regress_completed_local_postprocess(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 后期状态单向前进",
        items=[{"row_key": "1", "script_text": "已经完成的脚本。"}],
    )
    project_id = str(project["project_id"])
    item_id = str(project["items"][0]["item_id"])
    base_path = tmp_path / "base.mp4"
    base_path.write_bytes(b"base-video")
    store.add_asset(
        owner_user_id="user-1",
        project_id=project_id,
        item_id=item_id,
        asset_type="base_video",
        source_type="h3",
        status="READY",
        filename=base_path.name,
        managed_path=str(base_path),
        make_current=True,
    )
    store.set_item_subtitles(
        "user-1",
        project_id,
        item_id,
        {
            "source": "h3_generated_audio",
            "raw_cues": [],
            "render_cues": [{"text": "已经完成的脚本。", "start_us": 0, "end_us": 1_000_000}],
            "status": "PREVIEW_READY",
        },
    )
    with store._transaction() as connection:
        connection.execute(
            "UPDATE project_items SET status='COMPOSITION_READY' WHERE item_id=?",
            (item_id,),
        )

    active_snapshot = {
        "batch_id": "h3-batch-1",
        "status": "ACTIVE",
        "items": [
            {
                "item_id": "remote-row-1",
                "row_id": "1",
                "status": "SUCCESS",
                "segments": [],
            }
        ],
    }
    active = store.set_h3_batch_snapshot(
        "user-1", project_id, prepare_key="quote-1", snapshot=active_snapshot
    )
    assert active["items"][0]["status"] == "COMPOSITION_READY"

    # Reproduce the stale state from DH-20260826-0003.  A later terminal batch
    # sync must recover from durable local evidence instead of preserving
    # H3_RUNNING forever.
    with store._transaction() as connection:
        connection.execute(
            "UPDATE project_items SET status='H3_RUNNING' WHERE item_id=?",
            (item_id,),
        )
    terminal = store.set_h3_batch_snapshot(
        "user-1",
        project_id,
        prepare_key="quote-1",
        snapshot={**active_snapshot, "status": "FAILED"},
    )
    assert terminal["items"][0]["status"] == "COMPOSITION_READY"


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


def test_h3_sync_recovers_item_owned_legacy_batch_after_install_path_move(
    tmp_path: Path,
) -> None:
    store = ProjectStore(tmp_path / "control.db")
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="H3 旧安装目录迁移",
        items=[
            {"row_key": "1", "script_text": "最新批次。"},
            {"row_key": "2", "script_text": "旧批次。"},
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
    snapshots = {
        "2": {
            "batch_id": "legacy-item-only-batch",
            "status": "SUCCESS",
            "items": [
                {
                    "item_id": "remote-2",
                    "row_id": "2",
                    "status": "SUCCESS",
                    "segments": [
                        {
                            "segment_id": "segment-2",
                            "index": 0,
                            "script_text": "旧批次。",
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/2.mp4",
                        }
                    ],
                }
            ],
        },
        "1": {
            "batch_id": "latest-project-batch",
            "status": "SUCCESS",
            "items": [
                {
                    "item_id": "remote-1",
                    "row_id": "1",
                    "status": "SUCCESS",
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "index": 0,
                            "script_text": "最新批次。",
                            "status": "SUCCESS",
                            "normalized_video_download_url": "/1.mp4",
                        }
                    ],
                }
            ],
        },
    }
    for row_key in ("2", "1"):
        snapshot = snapshots[row_key]
        client.snapshots[str(snapshot["batch_id"])] = snapshot
        store.set_h3_batch_snapshot(
            "user-1",
            project_id,
            prepare_key=f"quote-{row_key}",
            snapshot=snapshot,
        )

    current = store.get_project("user-1", project_id)
    item_by_row = {str(item["row_key"]): item for item in current["items"]}
    for row_key, item in item_by_row.items():
        for asset_type, suffix in (("audio", ".wav"), ("base_video", ".mp4")):
            path = tmp_path / "old-install" / f"{row_key}-{asset_type}{suffix}"
            if row_key == "1":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"existing-{asset_type}".encode())
            store.add_asset(
                owner_user_id="user-1",
                project_id=project_id,
                item_id=str(item["item_id"]),
                asset_type=asset_type,
                source_type="h3",
                status="READY",
                filename=path.name,
                managed_path=str(path),
                make_current=True,
            )

    # Reproduce the pre-multi-batch schema: the project only remembers the
    # latest batch, while row 2 still owns its older successful batch.
    with store._transaction() as connection:
        row = connection.execute(
            "SELECT settings_json FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        settings = json.loads(str(row["settings_json"]))
        settings["h3"].pop("batches", None)
        connection.execute(
            "UPDATE projects SET settings_json=? WHERE project_id=?",
            (json.dumps(settings, ensure_ascii=False), project_id),
        )

    coordinator = ProjectH3Coordinator(
        store,
        client,  # type: ignore[arg-type]
        storage_root=tmp_path / "new-install" / "storage",
        media_preparer=fake_media_preparer,
    )
    synced = coordinator.sync("user-1", project_id, "token")["project"]
    recovered = next(item for item in synced["items"] if item["row_key"] == "2")

    assert client.fetched_batch_ids == [
        "latest-project-batch",
        "legacy-item-only-batch",
    ]
    assert recovered["status"] == "BASE_VIDEO_READY"
    assert Path(recovered["outputs"]["audio"]["managed_path"]).is_file()
    assert Path(recovered["outputs"]["base_video"]["managed_path"]).is_file()
    assert "new-install" in recovered["outputs"]["audio"]["managed_path"]


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
