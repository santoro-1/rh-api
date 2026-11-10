from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any
import uuid

from .sticker_export import STICKER_SCHEMA, corner_alpha_reveal, visible_content_bounds


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _max_render_index(data: dict[str, Any]) -> int:
    maximum = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            try:
                maximum = max(maximum, int(segment.get("render_index", 0) or 0))
            except (TypeError, ValueError):
                continue
    return maximum


def _render_index_below_text(data: dict[str, Any]) -> int:
    """Choose a layer above visual tracks but below the first text layer."""

    non_text_maximum = 0
    text_indices: list[int] = []
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        is_text = str(track.get("type") or "") == "text"
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            try:
                render_index = int(segment.get("render_index", 0) or 0)
            except (TypeError, ValueError):
                continue
            if is_text:
                text_indices.append(render_index)
            else:
                non_text_maximum = max(non_text_maximum, render_index)
    if not text_indices:
        return non_text_maximum + 1
    first_text_index = min(text_indices)
    if non_text_maximum < first_text_index:
        return non_text_maximum + 1
    # Legacy drafts normally reserve a wide range before text (for example
    # visual effects around 11000 and captions around 14000).  If a custom
    # draft does not, keep the semantic image below text without renumbering
    # user-authored tracks.
    return max(0, first_text_index - 1)


def add_fullscreen_sticker_to_data(
    data: dict[str, Any],
    sticker_json_path: str | Path,
    *,
    start_us: int = 0,
    duration_us: int = 0,
    corner: str = "",
    visible_ratio: float = 0.05,
    scale: float = 1.0,
    rotation: float = 0.0,
    opacity: float = 1.0,
    track_name: str = "",
    transform_x: float | None = None,
    transform_y: float | None = None,
    inside_canvas: bool = False,
    render_below_text: bool = False,
) -> int:
    metadata_path = Path(sticker_json_path).expanduser().resolve()
    if not metadata_path.is_file():
        raise FileNotFoundError(f"全屏贴纸 JSON 不存在: {metadata_path}")
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != STICKER_SCHEMA:
        raise RuntimeError(f"不支持的全屏贴纸 schema: {payload.get('schema')!r}")
    material = payload.get("material")
    segment = payload.get("segment_template")
    resource = payload.get("resource")
    if not isinstance(material, dict) or not isinstance(segment, dict) or not isinstance(resource, dict):
        raise RuntimeError("全屏贴纸缺少 material、segment_template 或 resource")
    library_path = str(resource.get("library_path", "")).strip()
    resource_path = (metadata_path.parent / library_path).resolve() if library_path else Path()
    if not resource_path.is_dir():
        raise FileNotFoundError(f"全屏贴纸资源目录不存在: {resource_path}")

    try:
        timeline_duration = int(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        timeline_duration = 0
    if start_us < 0 or timeline_duration <= 0 or start_us >= timeline_duration:
        raise ValueError(f"全屏贴纸开始时间超出视频时长: {start_us}")
    resolved_duration = duration_us if duration_us > 0 else timeline_duration - start_us
    if resolved_duration <= 0 or start_us + resolved_duration > timeline_duration:
        raise ValueError("全屏贴纸时间范围超出视频时长")

    materials = data.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("草稿 materials 不是对象")
    stickers = materials.setdefault("stickers", [])
    tracks = data.setdefault("tracks", [])
    if not isinstance(stickers, list) or not isinstance(tracks, list):
        raise RuntimeError("草稿 stickers 或 tracks 不是数组")

    material_copy = deepcopy(material)
    material_copy["id"] = _new_id()
    material_copy["path"] = str(resource_path)
    stickers.append(material_copy)

    segment_copy = deepcopy(segment)
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("贴纸透明度必须在 0 到 1 之间")
    segment_copy["id"] = _new_id()
    segment_copy["material_id"] = material_copy["id"]
    segment_copy["render_index"] = (
        _render_index_below_text(data)
        if render_below_text
        else _max_render_index(data) + 1
    )
    segment_copy["global_alpha"] = float(opacity)
    segment_copy["target_timerange"] = {
        "start": start_us,
        "duration": resolved_duration,
    }
    normalized_corner = _normalize_corner(corner)
    if normalized_corner:
        canvas_width, canvas_height = _canvas_dimensions(data)
        content_bounds = _resolve_content_bounds(payload, metadata_path)
        if inside_canvas:
            _position_sticker_inside_canvas(
                segment_copy,
                normalized_corner,
                scale=scale,
                rotation=rotation,
                content_bounds=content_bounds,
                canvas_width=canvas_width,
                canvas_height=canvas_height,
            )
        else:
            _position_sticker_in_corner(
                segment_copy,
                normalized_corner,
                visible_ratio=visible_ratio,
                scale=scale,
                rotation=rotation,
                content_bounds=content_bounds,
                alpha_reveal=_resolve_corner_alpha_reveal(
                    payload,
                    metadata_path,
                    normalized_corner,
                    visible_ratio,
                ),
                canvas_width=canvas_width,
                canvas_height=canvas_height,
            )
    elif (
        scale != 1.0
        or rotation != 0.0
        or transform_x is not None
        or transform_y is not None
    ):
        _update_clip_transform(
            segment_copy,
            x=transform_x,
            y=transform_y,
            scale=scale,
            rotation=rotation,
        )
    new_track = {
            "id": _new_id(),
            "is_default_name": True,
            "name": str(track_name or "").strip()
            or (
                f"程序角落贴纸_{_corner_label(normalized_corner)}"
                if normalized_corner
                else "程序全屏贴纸"
            ),
            "segments": [segment_copy],
            "type": "sticker",
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


def _normalize_corner(value: str) -> str:
    text = str(value).strip().lower().replace("-", "_")
    aliases = {
        "top_left": "top_left",
        "left_top": "top_left",
        "top_right": "top_right",
        "right_top": "top_right",
        "bottom_left": "bottom_left",
        "left_bottom": "bottom_left",
        "bottom_right": "bottom_right",
        "right_bottom": "bottom_right",
        "center": "center",
    }
    if not text:
        return ""
    if text not in aliases:
        raise ValueError(f"不支持的贴纸角落: {value!r}")
    return aliases[text]


def _position_sticker_inside_canvas(
    segment: dict[str, Any],
    corner: str,
    *,
    scale: float,
    rotation: float,
    content_bounds: dict[str, float],
    canvas_width: float,
    canvas_height: float,
) -> None:
    """Place the complete semantic image inside the frame.

    Here ``scale`` means visible image width as a ratio of canvas width, matching
    the browser preview rather than the legacy collected-sticker multiplier.
    """

    if not 0.05 <= float(scale) <= 2.0:
        raise ValueError("画内贴图缩放必须在 0.05 到 2.0 之间")
    source_width = float(content_bounds.get("source_width", 0.0) or 0.0)
    source_height = float(content_bounds.get("source_height", 0.0) or 0.0)
    if canvas_width <= 0 or canvas_height <= 0 or source_width <= 0 or source_height <= 0:
        raise ValueError("无法确定画内贴图或画布尺寸")
    base_scale_x = _effective_clip_scale(segment, "x", 1.0)
    if base_scale_x <= 0:
        raise ValueError("画内贴图基础缩放无效")
    multiplier = float(scale) * canvas_width / source_width / base_scale_x
    effective_x = _effective_clip_scale(segment, "x", multiplier) * source_width / canvas_width
    effective_y = _effective_clip_scale(segment, "y", multiplier) * source_height / canvas_height
    left = 2.0 * content_bounds["left"] - 1.0
    right = 2.0 * content_bounds["right"] - 1.0
    top = 1.0 - 2.0 * content_bounds["top"]
    bottom = 1.0 - 2.0 * content_bounds["bottom"]
    margin_x = 0.08
    margin_y = 0.08
    if corner.endswith("left"):
        x = -1.0 + margin_x - left * effective_x
    elif corner.endswith("right"):
        x = 1.0 - margin_x - right * effective_x
    else:
        x = -(left + right) * effective_x / 2.0
    if corner.startswith("top"):
        y = 1.0 - margin_y - top * effective_y
    elif corner.startswith("bottom"):
        y = -1.0 + margin_y - bottom * effective_y
    else:
        y = -(top + bottom) * effective_y / 2.0
    _update_clip_transform(
        segment,
        x=x,
        y=y,
        scale=multiplier,
        rotation=rotation,
    )


def _position_sticker_in_corner(
    segment: dict[str, Any],
    corner: str,
    *,
    visible_ratio: float,
    scale: float,
    rotation: float,
    content_bounds: dict[str, float],
    alpha_reveal: dict[str, float] | None,
    canvas_width: float,
    canvas_height: float,
) -> None:
    ratio = float(visible_ratio)
    if ratio <= 0 or ratio > 0.5:
        raise ValueError("角落贴纸露出比例必须大于 0 且不超过 0.5")
    if scale <= 0:
        raise ValueError("角落贴纸缩放必须大于 0")

    geometry = alpha_reveal or {}
    source_width = float(
        geometry.get("source_width", content_bounds.get("source_width", 0.0)) or 0.0
    )
    source_height = float(
        geometry.get("source_height", content_bounds.get("source_height", 0.0)) or 0.0
    )
    if canvas_width <= 0:
        canvas_width = source_width
    if canvas_height <= 0:
        canvas_height = source_height
    if source_width <= 0:
        source_width = canvas_width
    if source_height <= 0:
        source_height = canvas_height
    if canvas_width <= 0 or canvas_height <= 0:
        canvas_width = source_width = 1.0
        canvas_height = source_height = 1.0

    scale_x = (
        _effective_clip_scale(segment, "x", scale)
        * source_width
        / canvas_width
    )
    scale_y = (
        _effective_clip_scale(segment, "y", scale)
        * source_height
        / canvas_height
    )

    if alpha_reveal is not None:
        cut_x = max(0.0, min(1.0, float(alpha_reveal["cut_x"])))
        cut_y = max(0.0, min(1.0, float(alpha_reveal["cut_y"])))
        local_cut_x = 2.0 * cut_x - 1.0
        local_cut_y = 1.0 - 2.0 * cut_y
        margin_x = 2.0 * min(2.0, canvas_width * 0.005) / canvas_width
        margin_y = 2.0 * min(2.0, canvas_height * 0.005) / canvas_height
        if corner.endswith("left"):
            x = -1.0 - local_cut_x * scale_x + margin_x
        else:
            x = 1.0 - local_cut_x * scale_x - margin_x
        if corner.startswith("top"):
            y = 1.0 - local_cut_y * scale_y - margin_y
        else:
            y = -1.0 - local_cut_y * scale_y + margin_y
        _update_clip_transform(
            segment,
            x=x,
            y=y,
            scale=scale,
            rotation=rotation,
        )
        return

    left = 2.0 * content_bounds["left"] - 1.0
    right = 2.0 * content_bounds["right"] - 1.0
    top = 1.0 - 2.0 * content_bounds["top"]
    bottom = 1.0 - 2.0 * content_bounds["bottom"]
    content_width = max(0.000001, right - left)
    content_height = max(0.000001, top - bottom)
    always_left = 2.0 * content_bounds.get("always_visible_left", content_bounds["left"]) - 1.0
    always_right = 2.0 * content_bounds.get("always_visible_right", content_bounds["right"]) - 1.0
    always_top = 1.0 - 2.0 * content_bounds.get("always_visible_top", content_bounds["top"])
    always_bottom = 1.0 - 2.0 * content_bounds.get("always_visible_bottom", content_bounds["bottom"])
    reveal_width = max(
        0.000001,
        2.0 * content_bounds.get("minimum_visible_width", content_width / 2.0),
    )
    reveal_height = max(
        0.000001,
        2.0 * content_bounds.get("minimum_visible_height", content_height / 2.0),
    )
    horizontal_reveal = ratio * reveal_width * scale_x
    vertical_reveal = ratio * reveal_height * scale_y
    if content_bounds.get("visible_frame_count", 1.0) > 1.0:
        minimum_x_pixels = min(12.0, canvas_width * 0.05)
        minimum_y_pixels = min(12.0, canvas_height * 0.05)
        horizontal_reveal = max(horizontal_reveal, 2.0 * minimum_x_pixels / canvas_width)
        vertical_reveal = max(vertical_reveal, 2.0 * minimum_y_pixels / canvas_height)

    if corner.endswith("left"):
        x = -1.0 - always_right * scale_x + horizontal_reveal
    else:
        x = 1.0 - always_left * scale_x - horizontal_reveal
    if corner.startswith("top"):
        y = 1.0 - always_bottom * scale_y - vertical_reveal
    else:
        y = -1.0 - always_top * scale_y + vertical_reveal
    _update_clip_transform(
        segment,
        x=x,
        y=y,
        scale=scale,
        rotation=rotation,
    )


def _resolve_content_bounds(payload: dict[str, Any], metadata_path: Path) -> dict[str, float]:
    # Recompute first so libraries exported by older versions automatically gain
    # sprite-sheet-aware bounds without forcing users to collect every sticker again.
    preview_file = str(payload.get("preview_file", "")).strip()
    if preview_file:
        preview_path = (metadata_path.parent / preview_file).resolve()
        try:
            inside_bundle = preview_path.is_relative_to(metadata_path.parent)
        except ValueError:
            inside_bundle = False
        if inside_bundle:
            parsed = _validated_content_bounds(visible_content_bounds(preview_path))
            if parsed is not None:
                return parsed
    parsed = _validated_content_bounds(payload.get("content_bounds"))
    if parsed is not None:
        return parsed
    return {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}


def _resolve_corner_alpha_reveal(
    payload: dict[str, Any],
    metadata_path: Path,
    corner: str,
    visible_ratio: float,
) -> dict[str, float] | None:
    preview_file = str(payload.get("preview_file", "")).strip()
    if not preview_file:
        return None
    preview_path = (metadata_path.parent / preview_file).resolve()
    try:
        inside_bundle = preview_path.is_relative_to(metadata_path.parent)
    except ValueError:
        inside_bundle = False
    if not inside_bundle:
        return None
    result = corner_alpha_reveal(preview_path, corner, visible_ratio)
    if not isinstance(result, dict):
        return None
    try:
        cut_x = float(result.get("cut_x"))
        cut_y = float(result.get("cut_y"))
        source_width = float(result.get("source_width"))
        source_height = float(result.get("source_height"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= cut_x <= 1.0 and 0.0 <= cut_y <= 1.0):
        return None
    if source_width <= 0.0 or source_height <= 0.0:
        return None
    return {
        "cut_x": cut_x,
        "cut_y": cut_y,
        "source_width": source_width,
        "source_height": source_height,
    }


def _validated_content_bounds(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        left = float(value.get("left"))
        top = float(value.get("top"))
        right = float(value.get("right"))
        bottom = float(value.get("bottom"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        return None
    parsed = {"left": left, "top": top, "right": right, "bottom": bottom}
    for key in ("source_width", "source_height"):
        try:
            dimension = float(value.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            dimension = 0.0
        if dimension > 0:
            parsed[key] = dimension
    for key in (
        "always_visible_left",
        "always_visible_top",
        "always_visible_right",
        "always_visible_bottom",
        "minimum_visible_width",
        "minimum_visible_height",
    ):
        try:
            metric = float(value.get(key))
        except (TypeError, ValueError):
            continue
        if 0.0 <= metric <= 1.0:
            parsed[key] = metric
    try:
        visible_frame_count = float(value.get("visible_frame_count", 1.0) or 1.0)
    except (TypeError, ValueError):
        visible_frame_count = 1.0
    if visible_frame_count > 1.0:
        parsed["visible_frame_count"] = visible_frame_count
    return parsed


def _canvas_dimensions(data: dict[str, Any]) -> tuple[float, float]:
    canvas = data.get("canvas_config", {})
    if not isinstance(canvas, dict):
        return (0.0, 0.0)
    try:
        width = float(canvas.get("width", 0.0) or 0.0)
        height = float(canvas.get("height", 0.0) or 0.0)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    return (max(0.0, width), max(0.0, height))


def _effective_clip_scale(segment: dict[str, Any], axis: str, multiplier: float) -> float:
    clip = segment.get("clip", {})
    clip_scale = clip.get("scale", {}) if isinstance(clip, dict) else {}
    if not isinstance(clip_scale, dict):
        clip_scale = {}
    try:
        base = float(clip_scale.get(axis, 1.0) or 1.0)
    except (TypeError, ValueError):
        base = 1.0
    return max(0.000001, abs(base * float(multiplier)))


def _update_clip_transform(
    segment: dict[str, Any],
    *,
    x: float | None = None,
    y: float | None = None,
    scale: float = 1.0,
    rotation: float = 0.0,
) -> None:
    clip = segment.setdefault("clip", {})
    if not isinstance(clip, dict):
        clip = {}
        segment["clip"] = clip
    transform = clip.setdefault("transform", {})
    if not isinstance(transform, dict):
        transform = {}
        clip["transform"] = transform
    if x is not None:
        transform["x"] = float(x)
    if y is not None:
        transform["y"] = float(y)
    clip_scale = clip.setdefault("scale", {})
    if not isinstance(clip_scale, dict):
        clip_scale = {}
        clip["scale"] = clip_scale
    clip_scale["x"] = float(clip_scale.get("x", 1.0) or 1.0) * float(scale)
    clip_scale["y"] = float(clip_scale.get("y", 1.0) or 1.0) * float(scale)
    clip["rotation"] = float(clip.get("rotation", 0.0) or 0.0) + float(rotation)


def _corner_label(corner: str) -> str:
    return {
        "top_left": "左上",
        "top_right": "右上",
        "bottom_left": "左下",
        "bottom_right": "右下",
    }.get(corner, corner)
