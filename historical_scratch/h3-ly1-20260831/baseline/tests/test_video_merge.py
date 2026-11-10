from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_LEGACY_WEB,
    BATCH_SOURCE_NEW_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    TaskStatus,
)
from app.services.storage import to_relative_data_path
from app.services.video_merge import (
    MERGED_PREVIEW_READY,
    MERGE_NOT_APPLICABLE,
    MERGE_PENDING,
    VideoMergeError,
    merge_status_after_handoff,
    merge_segment_videos,
    process_pending_video_merges,
)
from tests.conftest import create_user, login


def _segmented_batch(
    *,
    review_required: bool,
    source_channel: str = BATCH_SOURCE_LEGACY_WEB,
) -> tuple[str, str]:
    user = create_user(
        "merge-review-user" if review_required else "merge-auto-user"
    )
    settings = get_settings()
    image_relative_path = f"uploads/{user.id}/placeholder.png"
    image_path = settings.data_dir / image_relative_path
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"image")
    sources = []
    for index in (1, 2):
        source = settings.outputs_dir / str(user.id) / f"source-{index}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"segment-{index}".encode())
        audio_path = settings.data_dir / f"uploads/{user.id}/audio-{index}.mp3"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(f"audio-{index}".encode())
        sources.append(source)

    batch_id = "merge-review-batch" if review_required else "merge-auto-batch"
    item_id = "merge-review-item" if review_required else "merge-auto-item"
    with SessionLocal() as db:
        batch = GenerationBatch(
            id=batch_id,
            user_id=user.id,
            name="自动拼接测试",
            workflow_type="digital_human",
            source_channel=source_channel,
            audio_mode="upload",
            review_required=False,
            video_review_required=review_required,
            request_key=batch_id,
            status="ACTIVE",
            total_items=1,
        )
        item = GenerationBatchItem(
            id=item_id,
            batch=batch,
            row_number=1,
            row_key="TASK-001",
            manifest_json=json.dumps({"row_id": "TASK-001"}),
            audio_status="SUCCESS",
            status="SEGMENTS_CREATED",
            merged_video_status="MERGE_PENDING",
        )
        for index, source in enumerate(sources, start=1):
            audio_relative_path = f"uploads/{user.id}/audio-{index}.mp3"
            segment = GenerationSegment(
                id=f"{item_id}-segment-{index}",
                batch_item=item,
                segment_index=index,
                script_text=f"第 {index} 段",
                start_seconds=float((index - 1) * 30),
                end_seconds=float(index * 30),
                audio_path=audio_relative_path,
                prompt=f"第 {index} 段",
                status="TASK_CREATED",
            )
            GenerationTask(
                id=f"{item_id}-task-{index}",
                user_id=user.id,
                segment=segment,
                workflow_type="digital_human",
                input_payload=json.dumps(
                    {
                        "assets": {
                            "image": {
                                "kind": "image",
                                "path": image_relative_path,
                                "original_name": "placeholder.png",
                            },
                            "audio": {
                                "kind": "audio",
                                "path": audio_relative_path,
                                "original_name": f"audio-{index}.mp3",
                            },
                        },
                        "parameters": {
                            **(
                                {"generation_tail_seconds": 2.0}
                                if source_channel == BATCH_SOURCE_NEW_WORKBENCH
                                else {}
                            ),
                            **(
                                {"workbench_final_segment_tail_seconds": 1.0}
                                if source_channel == BATCH_SOURCE_NEW_WORKBENCH
                                and index == len(sources)
                                else {}
                            )
                        },
                    }
                ),
                image_path=image_relative_path,
                audio_path=audio_relative_path,
                image_original_name="placeholder.png",
                audio_original_name=f"audio-{index}.mp3",
                audio_duration_seconds=30,
                start_seconds=0,
                end_seconds=30,
                prompt=f"第 {index} 段",
                status=TaskStatus.SUCCESS.value,
                result_path=to_relative_data_path(source, settings),
            )
        db.add(batch)
        db.commit()
    return batch_id, item_id


def _fake_legacy_merge(inputs, target, *, target_durations=None):
    assert target_durations is None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"|".join(path.read_bytes() for path in inputs))


def _fake_workbench_merge(inputs, target, *, target_durations=None):
    assert target_durations == [
        *([30.0] * (len(inputs) - 1)),
        32.0,
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"|".join(path.read_bytes() for path in inputs))


def test_normalization_rejects_materially_truncated_picture(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.video_merge._probe_video",
        lambda _path: (1080, 1920, 28.5),
    )
    with pytest.raises(VideoMergeError, match="明显短于音频"):
        merge_segment_videos(
            [Path("provider.mp4")],
            tmp_path / "base.mp4",
            target_durations=[30.0],
        )


def test_workbench_merge_uses_length_preserving_dissolve(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        "app.services.video_merge._probe_video",
        lambda _path: (1080, 1920, 3.0),
    )

    def fake_run(command, **_kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"merged")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.services.video_merge.subprocess.run", fake_run)
    target = tmp_path / "base.mp4"
    merge_segment_videos(
        [Path("one.mp4"), Path("two.mp4")],
        target,
        target_durations=[2.0, 3.0],
    )

    filter_graph = captured["command"][
        captured["command"].index("-filter_complex") + 1
    ]
    assert "stop_duration=0.250000" in filter_graph
    assert (
        "[v0][v1]xfade=transition=fade:duration=0.250000:"
        "offset=2.000000[vout]"
    ) in filter_graph
    assert "[a0][a1]concat=n=2:v=0:a=1[aout]" in filter_graph
    assert target.read_bytes() == b"merged"


def test_legacy_merge_keeps_hard_concat(monkeypatch, tmp_path):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        "app.services.video_merge._probe_video",
        lambda _path: (1080, 1920, 3.0),
    )

    def fake_run(command, **_kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"merged")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.services.video_merge.subprocess.run", fake_run)
    merge_segment_videos(
        [Path("one.mp4"), Path("two.mp4")],
        tmp_path / "legacy.mp4",
    )

    filter_graph = captured["command"][
        captured["command"].index("-filter_complex") + 1
    ]
    assert "xfade=" not in filter_graph
    assert "concat=n=2:v=1:a=1[vout][aout]" in filter_graph


def test_handoff_merge_status_keeps_legacy_single_segment_unchanged():
    assert merge_status_after_handoff(BATCH_SOURCE_LEGACY_WEB, 1) == (
        MERGE_NOT_APPLICABLE
    )
    assert merge_status_after_handoff(BATCH_SOURCE_LEGACY_WEB, 2) == MERGE_PENDING
    assert merge_status_after_handoff(BATCH_SOURCE_NEW_WORKBENCH, 1) == MERGE_PENDING


def test_segment_outputs_create_ordered_quick_preview(monkeypatch):
    batch_id, item_id = _segmented_batch(review_required=False)
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_legacy_merge,
    )

    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 1
        item = db.get(GenerationBatchItem, item_id)
        assert item is not None
        assert item.merged_video_status == MERGED_PREVIEW_READY
        merged = get_settings().data_dir / item.merged_video_path
        assert merged.read_bytes() == b"segment-1|segment-2"
        assert db.get(GenerationBatch, batch_id).video_review_required is False


def test_legacy_video_review_flag_does_not_turn_preview_into_a_finished_video(
    client, monkeypatch
):
    batch_id, item_id = _segmented_batch(review_required=True)
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_legacy_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 1
        assert db.get(GenerationBatchItem, item_id).merged_video_status == MERGED_PREVIEW_READY

    login(client, "merge-review-user")
    detail = client.get(f"/batches/{batch_id}")
    assert "快速拼接预览 · 仅用于检查片段顺序，仍需人工粗剪" in detail.text


def test_legacy_batch_detail_can_retry_any_successful_segment_after_merge(
    client, monkeypatch
):
    batch_id, item_id = _segmented_batch(review_required=False)
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_legacy_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 1
        item = db.get(GenerationBatchItem, item_id)
        assert item.merged_video_status == MERGED_PREVIEW_READY
        assert item.merged_video_path

    login(client, "merge-auto-user")
    detail = client.get(f"/batches/{batch_id}")
    assert detail.status_code == 200
    assert detail.text.count(">重试此段</button>") == 2
    target_segment_id = f"{item_id}-segment-1"
    assert (
        f'action="/batches/{batch_id}/segments/{target_segment_id}/regenerate"'
        in detail.text
    )

    response = client.post(
        f"/batches/{batch_id}/segments/{target_segment_id}/regenerate",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/batches/{batch_id}"
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        retried = db.get(GenerationTask, f"{item_id}-task-1")
        untouched = db.get(GenerationTask, f"{item_id}-task-2")
        assert retried.status == TaskStatus.PENDING.value
        assert retried.result_path is None
        assert untouched.status == TaskStatus.SUCCESS.value
        assert untouched.result_path
        assert item.merged_video_status == MERGE_PENDING
        assert item.merged_video_path is None


def test_new_workbench_single_segment_is_normalized_as_base_video(monkeypatch):
    _batch_id, item_id = _segmented_batch(
        review_required=False,
        source_channel=BATCH_SOURCE_NEW_WORKBENCH,
    )
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        item.segments.pop()
        remaining_task = item.segments[0].generation_task
        payload = json.loads(remaining_task.input_payload)
        payload["parameters"]["workbench_final_segment_tail_seconds"] = 1.0
        remaining_task.input_payload = json.dumps(payload)
        db.commit()
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_workbench_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 1
        item = db.get(GenerationBatchItem, item_id)
        assert item.merged_video_status == MERGED_PREVIEW_READY


def test_legacy_single_segment_never_enters_normalization(monkeypatch):
    _batch_id, item_id = _segmented_batch(review_required=False)
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        item.segments.pop()
        db.commit()

    def _unexpected_merge(*_args, **_kwargs):
        raise AssertionError("legacy single segment must bypass merge")

    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _unexpected_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 0
