from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import GenerationTask, TaskStatus
from app.services.storage import remove_directory, task_output_dir, task_upload_dir


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def main() -> None:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    upload_cutoff = now - timedelta(days=settings.upload_retention_days)
    output_cutoff = now - timedelta(days=settings.output_retention_days)
    terminal_statuses = [
        TaskStatus.SUCCESS.value,
        TaskStatus.FAILED.value,
        TaskStatus.DOWNLOAD_FAILED.value,
        TaskStatus.CANCELLED.value,
    ]
    removed_uploads = removed_outputs = 0
    with SessionLocal() as db:
        tasks = db.scalars(
            select(GenerationTask).where(GenerationTask.status.in_(terminal_statuses))
        ).all()
        for task in tasks:
            if _as_utc(task.created_at) < upload_cutoff:
                remove_directory(task_upload_dir(settings, task.user_id, task.id))
                removed_uploads += 1
            if (
                task.status == TaskStatus.SUCCESS.value
                and task.completed_at
                and _as_utc(task.completed_at) < output_cutoff
            ):
                remove_directory(task_output_dir(settings, task.user_id, task.id))
                task.result_path = None
                removed_outputs += 1
        db.commit()
    print(f"已清理上传目录 {removed_uploads} 个，输出目录 {removed_outputs} 个。")


if __name__ == "__main__":
    main()
