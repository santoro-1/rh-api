from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid


BOTTOM_IMAGE_MAX_HEIGHT_RATIO = 0.30
BOTTOM_CENTER_MAX_VISIBLE_HEIGHT_RATIO = 0.37
BOTTOM_CENTER_BOTTOM_MARGIN = 0.008


def _new_id() -> str:
    return uuid.uuid4().hex


def _segment_layer(segment: dict[str, Any]) -> int:
    for key in ("render_index", "track_render_index"):
        if key not in segment:
            continue
        try:
            return int(segment.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _render_index_below_text(data: dict[str, Any]) -> int:
    non_text_maximum = 0
    text_segments: list[dict[str, Any]] = []
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        is_text = str(track.get("type") or "") == "text"
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            layer = _segment_layer(segment)
            if is_text:
                text_segments.append(segment)
            else:
                non_text_maximum = max(non_text_maximum, layer)
    reserved = non_text_maximum + 1
    if not text_segments:
        return reserved
    first_text_index = min(_segment_layer(segment) for segment in text_segments)
    if first_text_index <= reserved:
        offset = reserved - first_text_index + 1
        for segment in text_segments:
            shifted = _segment_layer(segment) + offset
            layer_keys = [
                key for key in ("render_index", "track_render_index") if key in segment
            ]
            for key in layer_keys or ["render_index"]:
                segment[key] = shifted
    return reserved


def _max_layer(data: dict[str, Any]) -> int:
    maximum = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if isinstance(segment, dict):
                maximum = max(maximum, _segment_layer(segment))
    return maximum


def _canvas_dimensions(data: dict[str, Any]) -> tuple[float, float]:
    canvas = data.get("canvas_config", {})
    if not isinstance(canvas, dict):
        canvas = {}
    try:
        width = float(canvas.get("width", 0) or 0)
        height = float(canvas.get("height", 0) or 0)
    except (TypeError, ValueError):
        width = height = 0.0
    if width <= 0 or height <= 0:
        raise ValueError("无法确定图片贴图的画布尺寸")
    return width, height


def _image_transform(
    *,
    corner: str,
    width_ratio: float,
    image_width: float,
    image_height: float,
    canvas_width: float,
    canvas_height: float,
) -> tuple[float, float, float]:
    normalized = str(corner or "center").strip().lower().replace("-", "_")
    aliases = {
        "left_top": "top_left",
        "right_top": "top_right",
        "left_bottom": "bottom_left",
        "right_bottom": "bottom_right",
        "center_bottom": "bottom_center",
        "bottom_middle": "bottom_center",
        "center_left": "middle_left",
        "left_center": "middle_left",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
        "bottom_center",
        "middle_left",
        "center",
    }:
        raise ValueError(f"不支持的图片贴图位置: {corner!r}")

    # CapCut/Jianying fits these local photos to canvas width before applying
    # clip scale.  ``width_ratio`` therefore remains the same user-facing
    # value as browser preview: visible image width / canvas width.
    resolved_width_ratio = float(width_ratio)
    height_factor = canvas_width / canvas_height * image_height / image_width
    half_height = resolved_width_ratio * height_factor
    if normalized in {"bottom_left", "bottom_right"} and half_height > BOTTOM_IMAGE_MAX_HEIGHT_RATIO:
        resolved_width_ratio = BOTTOM_IMAGE_MAX_HEIGHT_RATIO / height_factor
        half_height = BOTTOM_IMAGE_MAX_HEIGHT_RATIO
    half_width = resolved_width_ratio
    # Transform coordinates use half-canvas units: a 6% pixel margin is 0.12
    # here.  Ordinary semantic corner images keep the existing 4% safe area;
    # the persistent chest nameplate uses a slightly wider 6% left margin.
    margin_x = 0.12 if normalized == "middle_left" else 0.08
    margin_y = 0.08
    if normalized == "bottom_center":
        x = 0.0
    elif normalized.endswith("left"):
        x = -1.0 + margin_x + half_width
    elif normalized.endswith("right"):
        x = 1.0 - margin_x - half_width
    else:
        x = 0.0
    if normalized == "bottom_center":
        # The accepted talking-head layout is a wide window aligned to the
        # canvas axis. Tall/square media may extend slightly below the frame,
        # keeping only the lower 37% visible instead of shrinking to a narrow
        # tile; shorter media sits almost flush with the bottom edge.
        if half_height > BOTTOM_CENTER_MAX_VISIBLE_HEIGHT_RATIO:
            y = (
                -1.0
                + 2.0 * BOTTOM_CENTER_MAX_VISIBLE_HEIGHT_RATIO
                - half_height
            )
        else:
            y = -1.0 + BOTTOM_CENTER_BOTTOM_MARGIN + half_height
    elif normalized.startswith("top"):
        y = 1.0 - margin_y - half_height
    elif normalized.startswith("bottom"):
        y = -1.0 + margin_y + half_height
    else:
        y = 0.0
    return (
        max(-1.0, min(1.0, x)),
        max(-1.0, min(1.0, y)),
        resolved_width_ratio,
    )


def add_image_overlay_to_data(
    draft: Any,
    data: dict[str, Any],
    image_path: str | Path,
    *,
    start_us: int = 0,
    duration_us: int = 0,
    corner: str = "center",
    scale: float = 1.0,
    rotation: float = 0.0,
    opacity: float = 1.0,
    track_name: str = "图片贴图",
    render_below_text: bool = True,
    transform_x: float | None = None,
    transform_y: float | None = None,
) -> int:
    """Add a PNG/JPG as a real Jianying photo material on a video track."""

    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"图片贴图素材不存在: {path}")
    if not 0.0 <= float(opacity) <= 1.0:
        raise ValueError("图片贴图透明度必须在 0.0 到 1.0 之间")
    if not 0.05 <= float(scale) <= 2.0:
        raise ValueError("图片贴图缩放必须在 0.05 到 2.0 之间")

    try:
        timeline_duration = int(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        timeline_duration = 0
    if start_us < 0 or timeline_duration <= 0 or start_us >= timeline_duration:
        raise ValueError(f"图片贴图开始时间超出视频时长: {start_us}")
    resolved_duration = duration_us if duration_us > 0 else timeline_duration - start_us
    if resolved_duration <= 0 or start_us + resolved_duration > timeline_duration:
        raise ValueError("图片贴图时间范围超出视频时长")

    material_instance = draft.VideoMaterial(str(path))
    image_width = float(material_instance.width)
    image_height = float(material_instance.height)
    canvas_width, canvas_height = _canvas_dimensions(data)
    override_x = transform_x
    override_y = transform_y
    transform_x, transform_y, resolved_scale = _image_transform(
        corner=corner,
        width_ratio=float(scale),
        image_width=image_width,
        image_height=image_height,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    if override_x is not None:
        transform_x = float(override_x)
    if override_y is not None:
        transform_y = float(override_y)
    if not -2.0 <= transform_x <= 2.0 or not -2.0 <= transform_y <= 2.0:
        raise ValueError("图片贴图自定义位置必须在 -2.0 到 2.0 之间")
    segment_instance = draft.VideoSegment(
        material_instance,
        draft.Timerange(start_us, resolved_duration),
        volume=0.0,
        clip_settings=draft.ClipSettings(
            alpha=float(opacity),
            rotation=float(rotation),
            scale_x=resolved_scale,
            scale_y=resolved_scale,
            transform_x=transform_x,
            transform_y=transform_y,
        ),
    )

    material = material_instance.export_json()
    # pyJianYingDraft 0.3 corrupts non-ASCII paths on some Windows locales.
    # Keep its complete photo metadata but restore the exact Unicode path.
    material["path"] = str(path)
    material["type"] = "photo"
    segment = segment_instance.export_json()
    layer = (
        _render_index_below_text(data)
        if render_below_text
        else _max_layer(data) + 1
    )
    segment["track_render_index"] = layer

    materials = data.setdefault("materials", {})
    tracks = data.setdefault("tracks", [])
    if not isinstance(materials, dict) or not isinstance(tracks, list):
        raise RuntimeError("草稿 materials 或 tracks 结构无效")
    videos = materials.setdefault("videos", [])
    speeds = materials.setdefault("speeds", [])
    if not isinstance(videos, list) or not isinstance(speeds, list):
        raise RuntimeError("草稿视频或变速素材集合无效")
    videos.append(material)
    speeds.append(segment_instance.speed.export_json())

    new_track = {
        "id": _new_id(),
        "is_default_name": False,
        "name": str(track_name or "图片贴图").strip(),
        "segments": [segment],
        "type": "video",
    }
    if render_below_text:
        text_track_index = next(
            (
                index
                for index, track in enumerate(tracks)
                if isinstance(track, dict) and track.get("type") == "text"
            ),
            len(tracks),
        )
        tracks.insert(text_track_index, new_track)
    else:
        tracks.append(new_track)
    return 1
