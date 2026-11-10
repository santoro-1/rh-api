from __future__ import annotations

from pathlib import Path
from typing import Any

from .image_apply import (
    _canvas_dimensions,
    _image_transform,
    _max_layer,
    _new_id,
    _render_index_below_text,
)


def _video_transform(
    *,
    corner: str,
    width_ratio: float,
    fit: str,
    video_width: float,
    video_height: float,
    canvas_width: float,
    canvas_height: float,
) -> tuple[float, float, float]:
    normalized = str(corner or "center").strip().lower().replace("-", "_")
    if normalized == "center" and width_ratio >= 0.95:
        height_factor = canvas_width / canvas_height * video_height / video_width
        frame_scale = (
            max(1.0, 1.0 / height_factor)
            if fit == "cover"
            else min(1.0, 1.0 / height_factor)
        )
        return 0.0, 0.0, width_ratio * frame_scale
    return _image_transform(
        corner=corner,
        width_ratio=width_ratio,
        image_width=video_width,
        image_height=video_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )


def add_video_overlay_to_data(
    draft: Any,
    data: dict[str, Any],
    video_path: str | Path,
    *,
    start_us: int,
    duration_us: int,
    source_start_us: int = 0,
    source_duration_us: int = 0,
    loop_to_target: bool = False,
    mute: bool = True,
    fit: str = "cover",
    corner: str = "center",
    scale: float = 1.0,
    opacity: float = 1.0,
    track_name: str = "语义前景视频",
    render_below_text: bool = True,
) -> int:
    """Insert a semantic video as a native Jianying video track."""

    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"语义视频素材不存在: {path}")
    if start_us < 0 or duration_us <= 0 or source_start_us < 0:
        raise ValueError("语义视频时间范围无效")
    if fit not in {"cover", "contain"}:
        raise ValueError("语义视频填充方式无效")
    if not 0.05 <= float(scale) <= 2.0:
        raise ValueError("语义视频缩放必须在 0.05 到 2.0 之间")
    if not 0.0 <= float(opacity) <= 1.0:
        raise ValueError("语义视频透明度必须在 0.0 到 1.0 之间")
    timeline_duration = int(data.get("duration", 0) or 0)
    if timeline_duration <= 0 or start_us >= timeline_duration:
        raise ValueError("语义视频开始时间超出视频时长")
    requested_duration = min(duration_us, timeline_duration - start_us)

    material_instance = draft.VideoMaterial(str(path))
    material_duration = int(material_instance.duration)
    available_duration = material_duration - source_start_us
    if available_duration <= 0:
        raise ValueError("语义视频截取起点超出素材时长")
    clip_duration = min(
        available_duration,
        source_duration_us if source_duration_us > 0 else available_duration,
    )
    if clip_duration <= 0:
        raise ValueError("语义视频有效源片段时长无效")
    target_duration = requested_duration if loop_to_target else min(requested_duration, clip_duration)
    canvas_width, canvas_height = _canvas_dimensions(data)
    transform_x, transform_y, resolved_scale = _video_transform(
        corner=corner,
        width_ratio=float(scale),
        fit=fit,
        video_width=float(material_instance.width),
        video_height=float(material_instance.height),
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    layer = _render_index_below_text(data) if render_below_text else _max_layer(data) + 1
    segments = []
    segment_instances = []
    cursor = 0
    while cursor < target_duration:
        current_duration = min(clip_duration, target_duration - cursor)
        segment_instance = draft.VideoSegment(
            material_instance,
            draft.Timerange(start_us + cursor, current_duration),
            source_timerange=draft.Timerange(source_start_us, current_duration),
            volume=0.0 if mute else 1.0,
            clip_settings=draft.ClipSettings(
                alpha=float(opacity),
                scale_x=resolved_scale,
                scale_y=resolved_scale,
                transform_x=transform_x,
                transform_y=transform_y,
            ),
        )
        segment = segment_instance.export_json()
        segment["track_render_index"] = layer
        segments.append(segment)
        segment_instances.append(segment_instance)
        cursor += current_duration
        if not loop_to_target:
            break

    material = material_instance.export_json()
    material["path"] = str(path)
    materials = data.setdefault("materials", {})
    tracks = data.setdefault("tracks", [])
    if not isinstance(materials, dict) or not isinstance(tracks, list):
        raise RuntimeError("草稿 materials 或 tracks 结构无效")
    videos = materials.setdefault("videos", [])
    speeds = materials.setdefault("speeds", [])
    if not isinstance(videos, list) or not isinstance(speeds, list):
        raise RuntimeError("草稿视频或变速素材集合无效")
    videos.append(material)
    speeds.extend(item.speed.export_json() for item in segment_instances)
    track = {
        "id": _new_id(),
        "is_default_name": False,
        "name": str(track_name or "语义前景视频").strip(),
        "segments": segments,
        "type": "video",
    }
    if render_below_text:
        text_index = next(
            (
                index
                for index, current in enumerate(tracks)
                if isinstance(current, dict) and current.get("type") == "text"
            ),
            len(tracks),
        )
        tracks.insert(text_index, track)
    else:
        tracks.append(track)
    return len(segments)
