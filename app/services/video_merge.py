from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import (
    GenerationBatchItem,
    GenerationSegment,
    TaskStatus,
)
from app.services.processes import hidden_creation_flags
from app.services.storage import remove_directory, safe_relative_path, to_relative_data_path


logger = logging.getLogger(__name__)

MERGE_NOT_APPLICABLE = "NOT_APPLICABLE"
MERGE_PENDING = "MERGE_PENDING"
MERGING = "MERGING"
AWAITING_VIDEO_REVIEW = "AWAITING_VIDEO_REVIEW"
MERGED_VIDEO_READY = "READY"
MERGED_PREVIEW_READY = "PREVIEW_READY"
MERGE_FAILED = "MERGE_FAILED"


class VideoMergeError(RuntimeError):
    """A segmented row could not produce its complete video."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def merged_video_output_dir(settings: Settings, user_id: int, item_id: str) -> Path:
    return settings.outputs_dir / str(user_id) / "merged" / item_id


def _probe_video(path: Path) -> tuple[int, int]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise VideoMergeError("当前环境没有安装 ffprobe") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoMergeError("读取分段视频信息超时") from exc
    if completed.returncode != 0:
        raise VideoMergeError(f"无法读取分段视频：{path.name}")
    try:
        streams = json.loads(completed.stdout or "{}").get("streams", [])
    except json.JSONDecodeError as exc:
        raise VideoMergeError(f"分段视频信息损坏：{path.name}") from exc
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    if video is None or audio is None:
        raise VideoMergeError(f"分段视频缺少画面或声音：{path.name}")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise VideoMergeError(f"分段视频分辨率无效：{path.name}")
    return width - width % 2, height - height % 2


def merge_segment_videos(inputs: list[Path], target: Path) -> None:
    """Normalize provider outputs and concatenate them in segment order."""

    if not inputs:
        raise VideoMergeError("没有可合并的分段视频")
    width, height = _probe_video(inputs[0])
    for path in inputs[1:]:
        _probe_video(path)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part.mp4")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        command.extend(["-i", str(path)])

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index in range(len(inputs)):
        filters.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,fps=30,format=yuv420p"
            f"[v{index}]"
        )
        filters.append(
            f"[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo"
            f"[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(inputs)}:v=1:a=1[vout][aout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise VideoMergeError("当前环境没有安装 ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        temporary.unlink(missing_ok=True)
        raise VideoMergeError("合并完整视频超时") from exc
    if completed.returncode != 0 or not temporary.is_file():
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        logger.warning("合并分段视频失败：%s", diagnostic[-1500:])
        temporary.unlink(missing_ok=True)
        raise VideoMergeError("合并完整视频失败")
    os.replace(temporary, target)


def invalidate_merged_video(
    item: GenerationBatchItem,
    settings: Settings,
) -> None:
    """Remove a stale complete video before one child is regenerated."""

    directory = merged_video_output_dir(settings, item.batch.user_id, item.id)
    try:
        remove_directory(directory)
    except OSError:
        # The next merge writes a temporary file and atomically replaces the
        # old result, so a transient cleanup failure must not leave the paid
        # child task reset while the database still advertises a stale video.
        logger.warning("无法立即清理旧完整视频：%s", directory)
    item.merged_video_status = MERGE_PENDING
    item.merged_video_path = None
    item.merged_video_error = None
    item.merged_at = None
    item.merged_reviewed_at = None


def _load_merge_item(db: Session, item_id: str) -> GenerationBatchItem | None:
    return db.scalar(
        select(GenerationBatchItem)
        .where(GenerationBatchItem.id == item_id)
        .options(
            selectinload(GenerationBatchItem.batch),
            selectinload(GenerationBatchItem.segments).selectinload(
                GenerationSegment.generation_task
            ),
        )
    )


def merge_batch_item(
    db: Session,
    item_id: str,
    settings: Settings,
) -> bool:
    """Merge one row when every ordered RunningHub child is available."""

    item = _load_merge_item(db, item_id)
    if item is None or len(item.segments) <= 1:
        return False
    if item.merged_video_status != MERGE_PENDING:
        return False
    ordered = sorted(item.segments, key=lambda segment: segment.segment_index)
    tasks = [segment.generation_task for segment in ordered]
    if any(task is None or task.status != TaskStatus.SUCCESS.value for task in tasks):
        return False

    inputs: list[Path] = []
    for task in tasks:
        assert task is not None
        if not task.result_path:
            item.merged_video_status = MERGE_FAILED
            item.merged_video_error = "分段任务成功但没有本地视频文件"
            db.commit()
            return True
        try:
            path = safe_relative_path(task.result_path, settings.data_dir)
        except ValueError:
            path = Path()
        if not path.is_file():
            item.merged_video_status = MERGE_FAILED
            item.merged_video_error = "至少一个分段视频已经丢失"
            db.commit()
            return True
        inputs.append(path)

    item.merged_video_status = MERGING
    item.merged_video_error = None
    db.commit()
    target = merged_video_output_dir(
        settings, item.batch.user_id, item.id
    ) / "complete.mp4"
    try:
        merge_segment_videos(inputs, target)
    except (OSError, VideoMergeError) as exc:
        item = _load_merge_item(db, item_id)
        if item is not None:
            item.merged_video_status = MERGE_FAILED
            item.merged_video_path = None
            item.merged_video_error = str(exc)
            item.merged_at = None
            db.commit()
        return True

    item = _load_merge_item(db, item_id)
    if item is None:
        target.unlink(missing_ok=True)
        return True
    item.merged_video_path = to_relative_data_path(target, settings)
    # Independent image-to-video children do not have continuous boundary
    # frames.  The concatenation is therefore an inspection aid only; human
    # rough cutting remains required before packaging.
    item.merged_video_status = MERGED_PREVIEW_READY
    item.merged_video_error = None
    item.merged_at = _now()
    item.merged_reviewed_at = None
    db.commit()
    return True


def process_pending_video_merges(
    db: Session,
    settings: Settings,
    *,
    limit: int = 5,
) -> int:
    item_ids = db.scalars(
        select(GenerationBatchItem.id)
        .where(GenerationBatchItem.merged_video_status == MERGE_PENDING)
        .order_by(GenerationBatchItem.updated_at)
    ).all()
    processed = 0
    for item_id in item_ids:
        if merge_batch_item(db, item_id, settings):
            processed += 1
            if processed >= limit:
                break
    return processed


def recover_interrupted_video_merges(db: Session) -> int:
    result = db.execute(
        update(GenerationBatchItem)
        .where(GenerationBatchItem.merged_video_status == MERGING)
        .values(
            merged_video_status=MERGE_PENDING,
            merged_video_error="视频 Worker 重启，已重新安排合并",
        )
    )
    db.commit()
    return int(result.rowcount or 0)


def approve_merged_video(item: GenerationBatchItem) -> None:
    if (
        item.merged_video_status != AWAITING_VIDEO_REVIEW
        or not item.merged_video_path
    ):
        raise VideoMergeError("当前完整视频不在待审核状态")
    item.merged_video_status = MERGED_VIDEO_READY
    item.merged_reviewed_at = _now()


def retry_video_merge(item: GenerationBatchItem, settings: Settings) -> None:
    if item.merged_video_status != MERGE_FAILED:
        raise VideoMergeError("只有合并失败的完整视频可以重试")
    invalidate_merged_video(item, settings)
