from __future__ import annotations

import json

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
    TaskStatus,
)
from app.services.storage import to_relative_data_path
from app.services.video_merge import (
    MERGED_PREVIEW_READY,
    process_pending_video_merges,
)
from tests.conftest import create_user, login


def _segmented_batch(*, review_required: bool) -> tuple[str, str]:
    user = create_user(
        "merge-review-user" if review_required else "merge-auto-user"
    )
    settings = get_settings()
    sources = []
    for index in (1, 2):
        source = settings.outputs_dir / str(user.id) / f"source-{index}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"segment-{index}".encode())
        sources.append(source)

    batch_id = "merge-review-batch" if review_required else "merge-auto-batch"
    item_id = "merge-review-item" if review_required else "merge-auto-item"
    with SessionLocal() as db:
        batch = GenerationBatch(
            id=batch_id,
            user_id=user.id,
            name="自动拼接测试",
            workflow_type="digital_human",
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
            segment = GenerationSegment(
                id=f"{item_id}-segment-{index}",
                batch_item=item,
                segment_index=index,
                script_text=f"第 {index} 段",
                start_seconds=float((index - 1) * 30),
                end_seconds=float(index * 30),
                audio_path=f"uploads/{user.id}/audio-{index}.mp3",
                prompt=f"第 {index} 段",
                status="TASK_CREATED",
            )
            GenerationTask(
                id=f"{item_id}-task-{index}",
                user_id=user.id,
                segment=segment,
                workflow_type="digital_human",
                input_payload=json.dumps({"assets": [], "parameters": {}}),
                image_path="placeholder.png",
                audio_path="placeholder.mp3",
                image_original_name="placeholder.png",
                audio_original_name="placeholder.mp3",
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


def _fake_merge(inputs, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"|".join(path.read_bytes() for path in inputs))


def test_segment_outputs_create_ordered_quick_preview(monkeypatch):
    batch_id, item_id = _segmented_batch(review_required=False)
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_merge,
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
        _fake_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 1
        assert db.get(GenerationBatchItem, item_id).merged_video_status == MERGED_PREVIEW_READY

    login(client, "merge-review-user")
    detail = client.get(f"/batches/{batch_id}")
    assert "快速拼接预览 · 仅用于检查片段顺序，仍需人工粗剪" in detail.text


def test_single_segment_is_not_sent_to_preview_merger(monkeypatch):
    _batch_id, item_id = _segmented_batch(review_required=False)
    with SessionLocal() as db:
        item = db.get(GenerationBatchItem, item_id)
        item.segments.pop()
        db.commit()
    monkeypatch.setattr(
        "app.services.video_merge.merge_segment_videos",
        _fake_merge,
    )
    with SessionLocal() as db:
        assert process_pending_video_merges(db, get_settings()) == 0
