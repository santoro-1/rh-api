from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .cli import append_track_compat, import_pyjianyingdraft, log


@dataclass(frozen=True)
class CreatedVideoDraft:
    draft_dir: Path
    draft_name: str
    media_path: Path
    duration_us: int
    width: int
    height: int
    fps: int


@dataclass(frozen=True)
class VideoSequenceItem:
    media_path: Path
    target_duration_us: int = 0
    source_start_us: int = 0
    transition_after_us: int = 0
    volume: float = 1.0


def _safe_draft_name(stem: str) -> str:
    keep = []
    for char in stem.strip():
        if char in '<>:"/\\|?*':
            keep.append("_")
        else:
            keep.append(char)
    name = "".join(keep).strip(" ._")
    return name or "uploaded_video"


def probe_video_duration_us(media_path: str | Path) -> int:
    """Read the same media duration fact that Jianying draft creation will use."""

    media = Path(media_path).expanduser().resolve()
    if not media.is_file():
        raise FileNotFoundError(f"输入视频不存在: {media}")
    draft = import_pyjianyingdraft()
    duration_us = int(draft.VideoMaterial(str(media)).duration)
    if duration_us <= 0:
        raise RuntimeError(f"输入视频时长无效: duration_us={duration_us}")
    return duration_us


def create_plain_draft_from_video(
    media_path: str | Path,
    output_root: str | Path,
    *,
    draft_name: str = "",
    width: int = 0,
    height: int = 0,
    fps: int = 30,
    source_start_us: int = 0,
    source_duration_us: int = 0,
    fade_out_us: int = 0,
) -> CreatedVideoDraft:
    """Create a plain Jianying draft containing one top-level video segment."""

    draft = import_pyjianyingdraft()
    media = Path(media_path).expanduser().resolve()
    root = Path(output_root).expanduser().resolve()
    if not media.exists():
        raise FileNotFoundError(f"输入视频不存在: {media}")
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    material = draft.VideoMaterial(str(media))
    canvas_width = int(width) if width and width > 0 else int(material.width)
    canvas_height = int(height) if height and height > 0 else int(material.height)
    canvas_fps = int(fps) if fps and fps > 0 else 30

    source_start = max(0, int(source_start_us))
    available_duration = max(0, int(material.duration) - source_start)
    segment_duration = int(source_duration_us) if source_duration_us and source_duration_us > 0 else available_duration
    if segment_duration <= 0:
        raise RuntimeError(
            f"输入视频可用时长无效: material_duration={material.duration}, source_start_us={source_start}"
        )
    if source_start + segment_duration > int(material.duration):
        raise RuntimeError(
            "输入视频截取范围超过素材时长: "
            f"source_start_us={source_start}, source_duration_us={segment_duration}, "
            f"material_duration={material.duration}"
        )

    if not draft_name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        draft_name = f"{_safe_draft_name(media.stem)}_base_{stamp}"

    folder = draft.DraftFolder(str(root))
    script = folder.create_draft(
        draft_name,
        canvas_width,
        canvas_height,
        canvas_fps,
        maintrack_adsorb=True,
        allow_replace=False,
    )
    append_track_compat(draft, script, draft.TrackType.video)
    video_segment = draft.VideoSegment(
        material,
        draft.Timerange(0, segment_duration),
        source_timerange=draft.Timerange(source_start, segment_duration),
    )
    fade_out = max(0, int(fade_out_us))
    if fade_out > segment_duration:
        raise ValueError("视频渐隐时长不能超过最后一个主视频片段")
    if fade_out > 0:
        video_segment.add_animation(draft.OutroType.渐隐, duration=fade_out)
    script.add_segment(video_segment)
    script.save()

    draft_dir = root / draft_name
    log(
        "已从输入视频创建基础草稿: "
        f"draft_dir={draft_dir}, duration={segment_duration}, canvas={canvas_width}x{canvas_height}, fps={canvas_fps}"
    )
    return CreatedVideoDraft(
        draft_dir=draft_dir,
        draft_name=draft_name,
        media_path=media,
        duration_us=segment_duration,
        width=canvas_width,
        height=canvas_height,
        fps=canvas_fps,
    )


def create_plain_draft_from_videos(
    items: Iterable[VideoSequenceItem],
    output_root: str | Path,
    *,
    draft_name: str = "",
    width: int = 0,
    height: int = 0,
    fps: int = 30,
    fade_out_us: int = 0,
) -> CreatedVideoDraft:
    """Create one main track whose source videos remain separate, ordered clips."""

    draft = import_pyjianyingdraft()
    sequence = [
        VideoSequenceItem(
            Path(item.media_path).expanduser().resolve(),
            max(0, int(item.target_duration_us)),
            max(0, int(item.source_start_us)),
            max(0, int(item.transition_after_us)),
            float(item.volume),
        )
        for item in items
    ]
    if not sequence:
        raise ValueError("多段主轨道至少需要一个视频素材")
    for item in sequence:
        if not item.media_path.is_file():
            raise FileNotFoundError(f"分段视频不存在: {item.media_path}")
        if not 0.0 <= item.volume <= 2.0:
            raise ValueError("分段视频音量必须在 0.0 到 2.0 之间")

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    materials = [draft.VideoMaterial(str(item.media_path)) for item in sequence]
    first = materials[0]
    canvas_width = int(width) if width and width > 0 else int(first.width)
    canvas_height = int(height) if height and height > 0 else int(first.height)
    canvas_fps = int(fps) if fps and fps > 0 else 30
    if not draft_name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        draft_name = f"{_safe_draft_name(sequence[0].media_path.stem)}_sequence_{stamp}"

    folder = draft.DraftFolder(str(root))
    script = folder.create_draft(
        draft_name,
        canvas_width,
        canvas_height,
        canvas_fps,
        maintrack_adsorb=True,
        allow_replace=False,
    )
    append_track_compat(draft, script, draft.TrackType.video)
    cursor = 0
    for index, (item, material) in enumerate(zip(sequence, materials), start=1):
        available = max(0, int(material.duration) - item.source_start_us)
        if available <= 0:
            raise RuntimeError(f"分段视频可用时长无效: {item.media_path}")
        requested = item.target_duration_us or available
        source_duration = min(requested, available)
        clip_duration = requested
        video_segment = draft.VideoSegment(
            material,
            draft.Timerange(cursor, clip_duration),
            source_timerange=draft.Timerange(item.source_start_us, source_duration),
            volume=item.volume,
        )
        if item.transition_after_us > 0 and index < len(sequence):
            next_item = sequence[index]
            next_material = materials[index]
            next_available = max(
                0,
                int(next_material.duration) - next_item.source_start_us,
            )
            next_requested = next_item.target_duration_us or next_available
            next_clip_duration = next_requested
            transition_duration = min(
                item.transition_after_us,
                clip_duration,
                next_clip_duration,
            )
            if transition_duration > 0:
                video_segment.add_transition(
                    draft.TransitionType.叠化,
                    duration=transition_duration,
                )
        if index == len(sequence):
            fade_out = max(0, int(fade_out_us))
            if fade_out > clip_duration:
                raise ValueError("视频渐隐时长不能超过最后一个主视频片段")
            if fade_out > 0:
                video_segment.add_animation(draft.OutroType.渐隐, duration=fade_out)
        script.add_segment(video_segment)
        cursor += clip_duration
    script.save()

    draft_dir = root / draft_name
    log(
        "已从原始分段创建独立主轨道: "
        f"draft_dir={draft_dir}, clips={len(sequence)}, duration={cursor}, "
        f"canvas={canvas_width}x{canvas_height}, fps={canvas_fps}"
    )
    return CreatedVideoDraft(
        draft_dir=draft_dir,
        draft_name=draft_name,
        media_path=sequence[0].media_path,
        duration_us=cursor,
        width=canvas_width,
        height=canvas_height,
        fps=canvas_fps,
    )
