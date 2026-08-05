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
    BATCH_SOURCE_NEW_WORKBENCH,
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
MAX_VIDEO_SHORTFALL_SECONDS = 1.0


class VideoMergeError(RuntimeError):
    """A segmented row could not produce its complete video."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def merged_video_output_dir(settings: Settings, user_id: int, item_id: str) -> Path:
    return settings.outputs_dir / str(user_id) / "merged" / item_id


def merge_status_after_handoff(source_channel: str, segment_count: int) -> str:
    """Choose the merge path without changing the legacy single-row flow."""

    if source_channel == BATCH_SOURCE_NEW_WORKBENCH or segment_count > 1:
        return MERGE_PENDING
    return MERGE_NOT_APPLICABLE


def _probe_video(path: Path) -> tuple[int, int, float]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
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
        probe_payload = json.loads(completed.stdout or "{}")
        streams = probe_payload.get("streams", [])
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
    duration = 0.0
    # Prefer the video stream clock: a longer embedded audio stream must not
    # hide a materially truncated picture.  Format duration is a fallback for
    # containers that omit per-stream duration.
    for value in (video.get("duration"), probe_payload.get("format", {}).get("duration")):
        try:
            duration = float(value or 0)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            break
    return width - width % 2, height - height % 2, duration


def merge_segment_videos(
    inputs: list[Path],
    target: Path,
    *,
    target_durations: list[float] | None = None,
) -> None:
    """Concatenate legacy outputs or normalize new-workbench outputs."""

    if not inputs:
        raise VideoMergeError("没有可合并的分段视频")
    if target_durations is not None and len(target_durations) != len(inputs):
        raise VideoMergeError("分段视频与目标音频时长数量不一致")
    first_width, first_height, first_duration = _probe_video(inputs[0])
    probes = [(first_width, first_height, first_duration)]
    probes.extend(_probe_video(path) for path in inputs[1:])
    width, height = first_width, first_height
    durations = None
    if target_durations is not None:
        durations = [float(value) for value in target_durations]
        for path, probe, duration in zip(inputs, probes, durations):
            if duration <= 0:
                raise VideoMergeError(f"目标音频时长无效：{path.name}")
            if probe[2] <= 0:
                raise VideoMergeError(f"分段视频时长无效：{path.name}")
            shortfall = duration - probe[2]
            if shortfall > MAX_VIDEO_SHORTFALL_SECONDS:
                raise VideoMergeError(
                    f"分段视频明显短于音频：{path.name} 少 {shortfall:.2f} 秒"
                )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part.mp4")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        command.extend(["-i", str(path)])

    filters: list[str] = []
    concat_inputs: list[str] = []
    for index in range(len(inputs)):
        video_filter = (
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            "setsar=1,fps=30"
        )
        audio_filter = (
            f"[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo"
        )
        if durations is not None:
            duration_text = f"{durations[index]:.6f}"
            video_filter += (
                f",tpad=stop_mode=clone:stop_duration={duration_text},"
                f"trim=duration={duration_text}"
            )
            audio_filter += (
                f",apad=pad_dur={duration_text},atrim=duration={duration_text}"
            )
        filters.append(video_filter + ",format=yuv420p" + f"[v{index}]")
        filters.append(audio_filter + f"[a{index}]")
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
    if item is None or not item.segments:
        return False
    is_new_workbench = (
        item.batch.source_channel == BATCH_SOURCE_NEW_WORKBENCH
    )
    if len(item.segments) == 1 and not is_new_workbench:
        return False
    if item.merged_video_status != MERGE_PENDING:
        return False
    ordered = sorted(item.segments, key=lambda segment: segment.segment_index)
    tasks = [segment.generation_task for segment in ordered]
    if any(task is None or task.status != TaskStatus.SUCCESS.value for task in tasks):
        return False

    inputs: list[Path] = []
    target_durations: list[float] | None = [] if is_new_workbench else None
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
        if target_durations is not None:
            target_durations.append(float(task.audio_duration_seconds or 0))

    item.merged_video_status = MERGING
    item.merged_video_error = None
    db.commit()
    target = merged_video_output_dir(
        settings, item.batch.user_id, item.id
    ) / "complete.mp4"
    try:
        merge_segment_videos(
            inputs,
            target,
            target_durations=target_durations,
        )
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
    # The legacy path remains a quick inspection concat; the workbench path is
    # its normalized base video. Original provider files remain untouched.
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
