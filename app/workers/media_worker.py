from __future__ import annotations

import logging
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import SessionLocal
from app.models import LongAudioProject, LongAudioProjectStatus
from app.services.logging_config import (
    configure_logging,
    log_event,
    start_heartbeat,
    write_heartbeat,
)
from app.services.long_audio import (
    analyze_long_audio_project,
    materialize_long_audio_project,
)


logger = logging.getLogger(__name__)


def recover_inflight_projects(db: Session) -> None:
    db.execute(
        update(LongAudioProject)
        .where(
            LongAudioProject.status
            == LongAudioProjectStatus.ANALYZING.value
        )
        .values(status=LongAudioProjectStatus.PENDING_ANALYSIS.value)
    )
    db.execute(
        update(LongAudioProject)
        .where(LongAudioProject.status == LongAudioProjectStatus.CUTTING.value)
        .values(status=LongAudioProjectStatus.PENDING_CUT.value)
    )
    db.commit()


def _claim_next(db: Session) -> LongAudioProject | None:
    row = db.execute(
        select(LongAudioProject.id, LongAudioProject.status)
        .where(
            LongAudioProject.status.in_(
                {
                    LongAudioProjectStatus.PENDING_ANALYSIS.value,
                    LongAudioProjectStatus.PENDING_CUT.value,
                }
            )
        )
        .order_by(LongAudioProject.created_at, LongAudioProject.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    project_id, pending_status = row
    claimed_status = (
        LongAudioProjectStatus.ANALYZING.value
        if pending_status == LongAudioProjectStatus.PENDING_ANALYSIS.value
        else LongAudioProjectStatus.CUTTING.value
    )
    result = db.execute(
        update(LongAudioProject)
        .where(
            LongAudioProject.id == project_id,
            LongAudioProject.status == pending_status,
        )
        .values(
            status=claimed_status,
            error_code=None,
            error_message=None,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return db.scalar(
        select(LongAudioProject)
        .options(
            selectinload(LongAudioProject.user),
            selectinload(LongAudioProject.batch),
        )
        .where(LongAudioProject.id == project_id)
    )


def process_next(db: Session) -> bool:
    project = _claim_next(db)
    if project is None:
        return False
    action = project.status
    try:
        if action == LongAudioProjectStatus.ANALYZING.value:
            analyze_long_audio_project(project, get_settings())
            event = "media.analysis_completed"
            message = "长音频分段分析完成，等待用户试听确认"
        else:
            batch = materialize_long_audio_project(db, project, get_settings())
            event = "media.handoff_completed"
            message = "长音频切割完成，视频子任务已进入本地队列"
            log_event(
                logger,
                event,
                message,
                project_id=project.id,
                batch_id=batch.id,
                user_id=project.user_id,
            )
            db.commit()
            return True
        db.commit()
        log_event(
            logger,
            event,
            message,
            project_id=project.id,
            user_id=project.user_id,
        )
    except Exception as exc:
        db.rollback()
        failed = db.get(LongAudioProject, project.id)
        if failed is not None:
            failed.status = LongAudioProjectStatus.FAILED.value
            failed.error_code = (
                "ANALYSIS_FAILED"
                if action == LongAudioProjectStatus.ANALYZING.value
                else "CUT_FAILED"
            )
            failed.error_message = str(exc)
            db.commit()
        log_event(
            logger,
            "media.failed",
            "长音频预处理失败",
            level=logging.WARNING,
            project_id=project.id,
            action=action,
            error=str(exc),
        )
    return True


def main() -> None:
    configure_logging("media_worker")
    start_heartbeat("media_worker")
    settings = get_settings()
    settings.long_audio_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        recover_inflight_projects(db)
    log_event(logger, "media_worker.started", "媒体预处理 Worker 已启动")
    while True:
        try:
            with SessionLocal() as db:
                processed = process_next(db)
            write_heartbeat("media_worker", processed=processed)
        except Exception as exc:
            logger.exception("媒体预处理 Worker 循环异常：%s", exc)
            write_heartbeat("media_worker", error="loop_error")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
