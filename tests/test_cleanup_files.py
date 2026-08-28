from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BATCH_SOURCE_H3_WORKBENCH,
    BATCH_SOURCE_LTX_WORKBENCH,
    GenerationBatch,
    GenerationBatchItem,
    LongAudioProject,
    LongAudioProjectStatus,
    LtxPreparationJob,
)
from app.services.storage import long_audio_project_dir
from scripts import cleanup_files
from tests.conftest import create_user


def _batch(
    *,
    batch_id: str,
    user_id: int,
    source_channel: str,
    status: str,
    created_at: datetime,
) -> GenerationBatch:
    return GenerationBatch(
        id=batch_id,
        user_id=user_id,
        name=batch_id,
        workflow_type="minimax_h3_ref2va",
        source_channel=source_channel,
        audio_mode="minimax",
        review_required=False,
        video_review_required=False,
        request_key=f"request-{batch_id}",
        status=status,
        total_items=1,
        created_at=created_at,
        updated_at=created_at,
    )


def test_cleanup_removes_only_expired_unconfirmed_h3_drafts():
    user = create_user("h3-cleanup-user", with_config=False)
    now = datetime.now(timezone.utc)
    old_batch = _batch(
        batch_id="old-h3-draft",
        user_id=user.id,
        source_channel=BATCH_SOURCE_H3_WORKBENCH,
        status="AWAITING_COST_CONFIRMATION",
        created_at=now - timedelta(hours=25),
    )
    recent_batch = _batch(
        batch_id="recent-h3-draft",
        user_id=user.id,
        source_channel=BATCH_SOURCE_H3_WORKBENCH,
        status="AWAITING_COST_CONFIRMATION",
        created_at=now - timedelta(hours=23),
    )
    completed_batch = _batch(
        batch_id="completed-h3-batch",
        user_id=user.id,
        source_channel=BATCH_SOURCE_H3_WORKBENCH,
        status="SUCCESS",
        created_at=now - timedelta(days=3),
    )
    with SessionLocal() as db:
        db.add_all([old_batch, recent_batch, completed_batch])
        db.commit()

    settings = get_settings()
    batch_root = settings.data_dir / "h3-workbench" / str(user.id)
    old_batch_id = "old-h3-draft"
    recent_batch_id = "recent-h3-draft"
    completed_batch_id = "completed-h3-batch"
    old_dir = batch_root / old_batch_id
    recent_dir = batch_root / recent_batch_id
    completed_dir = batch_root / completed_batch_id
    for directory in (old_dir, recent_dir, completed_dir):
        directory.mkdir(parents=True)
        (directory / "marker.txt").write_text("keep-or-remove", encoding="utf-8")

    cleanup_files.main()

    with SessionLocal() as db:
        assert db.get(GenerationBatch, old_batch_id) is None
        assert db.get(GenerationBatch, recent_batch_id) is not None
        assert db.get(GenerationBatch, completed_batch_id) is not None
    assert not old_dir.exists()
    assert recent_dir.exists()
    assert completed_dir.exists()


def test_cleanup_deletes_ltx_job_before_expired_long_audio_project():
    user = create_user("ltx-cleanup-user", with_config=False)
    now = datetime.now(timezone.utc)
    batch = _batch(
        batch_id="ltx-cleanup-batch",
        user_id=user.id,
        source_channel=BATCH_SOURCE_LTX_WORKBENCH,
        status="ACTIVE",
        created_at=now - timedelta(days=3),
    )
    batch.workflow_type = "ltx_lip_sync"
    item = GenerationBatchItem(
        id="ltx-cleanup-item",
        batch=batch,
        row_number=1,
        row_key="row-1",
        manifest_json="{}",
        audio_status="AUDIO_READY",
        status="TASK_CREATED",
    )
    project = LongAudioProject(
        id="expired-ltx-project",
        user_id=user.id,
        batch_item=item,
        name="expired project",
        workflow_type="ltx_lip_sync",
        review_required=False,
        script_text="测试文案",
        audio_path="long-audio/audio.wav",
        audio_original_name="audio.wav",
        video_path="long-audio/video.mp4",
        video_original_name="video.mp4",
        duration_seconds=10.0,
        parameters_json="{}",
        status=LongAudioProjectStatus.COMPLETED.value,
        expires_at=now - timedelta(hours=1),
    )
    job = LtxPreparationJob(
        id="expired-ltx-job",
        user_id=user.id,
        batch_item=item,
        long_audio_project=project,
        idempotency_key="expired-ltx-job-key",
        source_video_path="long-audio/video.mp4",
        source_video_original_name="video.mp4",
        source_video_sha256="1" * 64,
        source_audio_path="long-audio/audio.wav",
        source_audio_original_name="audio.wav",
        source_audio_sha256="2" * 64,
        script_text="测试文案",
        script_sha256="3" * 64,
        duration_seconds=10.0,
        video_duration_seconds=10.0,
        status="COMPLETED",
    )
    with SessionLocal() as db:
        db.add_all([batch, item, project, job])
        db.commit()

    project_id = "expired-ltx-project"
    job_id = "expired-ltx-job"
    item_id = "ltx-cleanup-item"
    project_dir = long_audio_project_dir(get_settings(), user.id, project_id)
    project_dir.mkdir(parents=True)
    (project_dir / "marker.txt").write_text("expired", encoding="utf-8")

    cleanup_files.main()

    with SessionLocal() as db:
        assert db.get(LongAudioProject, project_id) is None
        assert db.get(LtxPreparationJob, job_id) is None
        assert db.get(GenerationBatchItem, item_id) is not None
    assert not project_dir.exists()
