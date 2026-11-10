from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps


COVER_TRACK_PREFIX = "__jyd_cover__"


@dataclass(frozen=True)
class CoverConfig:
    frame_time_us: int
    frame_source: str = "preview_material"
    image_path: str = ""
    fps: float = 30.0
    frame_count: int = 3
    text_line_1: str = "默认文本"
    text_line_2: str = "默认文本"
    text_size: float = 12.0
    text_scale: float = 1.4
    line_1_x: float = 0.0
    line_1_y: float = -0.28
    line_2_x: float = 0.0
    line_2_y: float = -0.55
    line_1_size: float = 12.0
    line_2_size: float = 12.0
    line_1_color: str = "#FFFFFF"
    line_2_color: str = "#FFFFFF"
    frame_scale: float = 1.0
    frame_offset_x: float = 0.0
    frame_offset_y: float = 0.0
    overlay_alpha: float = 0.5
    overlay_x_ratio: float = 0.5
    overlay_y_ratio: float = 0.68
    overlay_width_ratio: float = 1.0
    overlay_height_ratio: float = 0.36
    overlay_top_ratio: float = 0.50
    overlay_bottom_ratio: float = 0.86
    font_id: str = ""
    font_path: str = ""
    font_title: str = ""
    letter_spacing: int = 0
    line_spacing: int = 6
    auto_wrapping: bool = False
    max_line_width: float = 0.86
    line_1_shadow_color: str = "#000000"
    line_1_shadow_alpha: float = 0.9
    line_1_shadow_smoothing: float = 0.15
    line_1_shadow_distance: float = 5.0
    line_1_shadow_angle: float = -45.0
    line_2_shadow_color: str = "#1F1A05"
    line_2_shadow_alpha: float = 0.5
    line_2_shadow_smoothing: float = 0.15
    line_2_shadow_distance: float = 5.0
    line_2_shadow_angle: float = -45.0

    @property
    def duration_us(self) -> int:
        return max(1, int(round(self.frame_count * 1_000_000 / self.fps)))


@dataclass(frozen=True)
class PreparedCover:
    frame_path: Path
    duration_us: int


def prepare_cover_assets(
    data: dict[str, Any],
    config: CoverConfig,
    output_draft_dir: str | Path,
) -> PreparedCover:
    _validate_config(config)
    draft_dir = Path(output_draft_dir).resolve()
    asset_dir = draft_dir / "cover_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    if config.frame_source == "input_image":
        source_path = Path(config.image_path).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Cover input image does not exist: {source_path}")
        source_time_us = 0
    elif config.frame_source == "preview_material":
        source_path, source_time_us = _find_preview_material_frame(
            data,
            config.frame_time_us,
            draft_dir,
        )
    else:
        source_path, source_time_us = _find_source_frame(
            data,
            config.frame_time_us,
            draft_dir,
        )
    frame_path = asset_dir / "__jyd_cover_frame.jpg"
    _extract_frame(source_path, source_time_us, frame_path)

    with Image.open(frame_path) as source_frame:
        frame = source_frame.convert("RGBA")
    if config.frame_source == "input_image":
        canvas = data.get("canvas_config", {})
        try:
            canvas_width = int(canvas.get("width", 1080) or 1080)
            canvas_height = int(canvas.get("height", 1920) or 1920)
        except (TypeError, ValueError):
            canvas_width, canvas_height = 1080, 1920
        if canvas_width <= 0 or canvas_height <= 0:
            canvas_width, canvas_height = 1080, 1920
        frame = ImageOps.fit(
            frame,
            (canvas_width, canvas_height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    frame = _transform_frame(
        frame,
        scale=config.frame_scale,
        offset_x=config.frame_offset_x,
        offset_y=config.frame_offset_y,
    )
    width, height = frame.size
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    overlay_x = config.overlay_x_ratio
    overlay_y = config.overlay_y_ratio
    overlay_width_ratio = config.overlay_width_ratio
    overlay_height_ratio = config.overlay_height_ratio
    if (
        overlay_x == 0.5
        and overlay_y == 0.68
        and overlay_width_ratio == 1.0
        and overlay_height_ratio == 0.36
        and (config.overlay_top_ratio != 0.50 or config.overlay_bottom_ratio != 0.86)
    ):
        overlay_y = (config.overlay_top_ratio + config.overlay_bottom_ratio) / 2.0
        overlay_height_ratio = config.overlay_bottom_ratio - config.overlay_top_ratio
    overlay_width = width * overlay_width_ratio
    overlay_height = height * overlay_height_ratio
    left = int(round(width * overlay_x - overlay_width / 2.0))
    right = int(round(width * overlay_x + overlay_width / 2.0))
    top = int(round(height * overlay_y - overlay_height / 2.0))
    bottom = int(round(height * overlay_y + overlay_height / 2.0))
    left = max(0, min(width, left))
    right = max(0, min(width, right))
    top = max(0, min(height, top))
    bottom = max(0, min(height, bottom))
    alpha = int(round(config.overlay_alpha * 255))
    ImageDraw.Draw(overlay).rectangle(
        (left, top, right, bottom),
        fill=(0, 0, 0, alpha),
    )
    Image.alpha_composite(frame, overlay).convert("RGB").save(frame_path, quality=95)
    return PreparedCover(frame_path, config.duration_us)


def add_cover_tracks(
    draft: Any,
    script: Any,
    prepared: PreparedCover,
    config: CoverConfig,
) -> int:
    duration = prepared.duration_us
    _append_track(draft, script, draft.TrackType.video, f"{COVER_TRACK_PREFIX}frame")
    frame_material = draft.VideoMaterial(str(prepared.frame_path))
    script.add_segment(
        draft.VideoSegment(
            frame_material,
            draft.Timerange(0, duration),
            volume=0.0,
        ),
        f"{COVER_TRACK_PREFIX}frame",
    )

    for index, (text, transform_x, transform_y, text_size) in enumerate(
        (
            (config.text_line_1, config.line_1_x, config.line_1_y, config.line_1_size),
            (config.text_line_2, config.line_2_x, config.line_2_y, config.line_2_size),
        ),
        start=1,
    ):
        track_name = f"{COVER_TRACK_PREFIX}text_{index}"
        _append_track(draft, script, draft.TrackType.text, track_name)
        script.add_segment(
            draft.TextSegment(
                text,
                draft.Timerange(0, duration),
                style=draft.TextStyle(
                    size=text_size,
                    bold=True,
                    align=1,
                    letter_spacing=config.letter_spacing,
                    line_spacing=config.line_spacing,
                    auto_wrapping=config.auto_wrapping,
                    max_line_width=config.max_line_width,
                ),
                clip_settings=draft.ClipSettings(
                    scale_x=config.text_scale,
                    scale_y=config.text_scale,
                    transform_x=transform_x,
                    transform_y=transform_y,
                ),
            ),
            track_name,
        )
    return 3


def apply_cover_timeline_offset(data: dict[str, Any], config: CoverConfig) -> int:
    duration = config.duration_us
    changed = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        track_name = str(track.get("name", ""))
        if track_name.startswith(COVER_TRACK_PREFIX):
            continue
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, dict):
                continue
            try:
                timerange["start"] = int(timerange.get("start", 0) or 0) + duration
            except (TypeError, ValueError):
                continue
            changed += 1
    try:
        data["duration"] = int(data.get("duration", 0) or 0) + duration
    except (TypeError, ValueError):
        data["duration"] = duration
    changed += 1

    # pyJianYingDraft needs a temporary video track while the cover material is
    # created. The final draft must place that segment at the beginning of the
    # first/main video track; leaving the temporary track in place makes the
    # cover an overlay instead of a true prefix clip.
    if isinstance(tracks, list):
        cover_track = next(
            (
                track
                for track in tracks
                if isinstance(track, dict)
                and track.get("type") == "video"
                and str(track.get("name", "")) == f"{COVER_TRACK_PREFIX}frame"
            ),
            None,
        )
        main_track = next(
            (
                track
                for track in tracks
                if isinstance(track, dict)
                and track.get("type") == "video"
                and not str(track.get("name", "")).startswith(COVER_TRACK_PREFIX)
            ),
            None,
        )
        if cover_track is None:
            raise RuntimeError("封面临时视频轨道不存在，无法插入主轨道")
        if main_track is None:
            raise RuntimeError("主视频轨道不存在，无法把封面插到视频之前")
        cover_segments = cover_track.get("segments", [])
        main_segments = main_track.get("segments", [])
        if not isinstance(cover_segments, list) or not cover_segments:
            raise RuntimeError("封面临时视频轨道没有可插入的片段")
        if not isinstance(main_segments, list):
            raise RuntimeError("主视频轨道片段格式无效")
        main_segments.extend(cover_segments)
        main_segments.sort(
            key=lambda segment: int(
                (segment.get("target_timerange") or {}).get("start", 0) or 0
            )
            if isinstance(segment, dict)
            else 0
        )
        tracks.remove(cover_track)
        changed += len(cover_segments) + 1
    return changed


def rebase_cover_material_paths(
    data: dict[str, Any],
    output_draft_dir: str | Path,
) -> int:
    """Keep generated cover materials on real absolute paths that Jianying can export."""
    materials = data.get("materials", {})
    videos = materials.get("videos", []) if isinstance(materials, dict) else []
    if not isinstance(videos, list):
        return 0
    draft_dir = Path(output_draft_dir).resolve()
    changed = 0
    names = {
        "__jyd_cover_frame.jpg": draft_dir / "cover_assets" / "__jyd_cover_frame.jpg",
    }
    for material in videos:
        if not isinstance(material, dict):
            continue
        name = str(material.get("material_name") or material.get("name") or "")
        path = names.get(name)
        if path is None:
            continue
        expected = path.resolve().as_posix()
        if material.get("path") != expected:
            material["path"] = expected
            changed += 1
    return changed


def _validate_config(config: CoverConfig) -> None:
    if config.frame_time_us < 0:
        raise ValueError("Cover frame time cannot be negative")
    if config.fps <= 0 or config.fps > 240:
        raise ValueError("Cover FPS must be greater than 0 and no more than 240")
    if config.frame_count <= 0 or config.frame_count > 30:
        raise ValueError("Cover frame count must be between 1 and 30")
    if config.frame_source not in {"input_image", "preview_material", "timeline"}:
        raise ValueError("Cover frame source must be input_image, preview_material or timeline")
    if config.frame_source == "input_image" and not config.image_path.strip():
        raise ValueError("Cover input image path is required")
    if not config.text_line_1.strip() or not config.text_line_2.strip():
        raise ValueError("Both cover text lines are required")
    if not 0.0 <= config.overlay_alpha <= 1.0:
        raise ValueError("Cover overlay alpha must be between 0 and 1")
    if not 1.0 <= config.frame_scale <= 4.0:
        raise ValueError("Cover frame scale must be between 1 and 4")
    if not -1.0 <= config.frame_offset_x <= 1.0 or not -1.0 <= config.frame_offset_y <= 1.0:
        raise ValueError("Cover frame offsets must be between -1 and 1")
    if not 0.0 <= config.overlay_x_ratio <= 1.0 or not 0.0 <= config.overlay_y_ratio <= 1.0:
        raise ValueError("Cover overlay position is invalid")
    if not 0.01 <= config.overlay_width_ratio <= 1.0 or not 0.01 <= config.overlay_height_ratio <= 1.0:
        raise ValueError("Cover overlay size is invalid")
    for coordinate in (config.line_1_x, config.line_1_y, config.line_2_x, config.line_2_y):
        if not -1.0 <= coordinate <= 1.0:
            raise ValueError("Cover text positions must be between -1 and 1")
    for size in (config.line_1_size, config.line_2_size):
        if size <= 0 or size > 100:
            raise ValueError("Cover text sizes must be greater than 0 and no more than 100")
    if not 0.01 <= config.max_line_width <= 1.0:
        raise ValueError("Cover text maximum width must be between 0.01 and 1")
    for alpha in (config.line_1_shadow_alpha, config.line_2_shadow_alpha):
        if not 0.0 <= alpha <= 1.0:
            raise ValueError("Cover shadow alpha must be between 0 and 1")
    for smoothing in (
        config.line_1_shadow_smoothing,
        config.line_2_shadow_smoothing,
    ):
        if not 0.0 <= smoothing <= 1.0:
            raise ValueError("Cover shadow smoothing must be between 0 and 1")


def _transform_frame(
    frame: Image.Image,
    *,
    scale: float,
    offset_x: float,
    offset_y: float,
) -> Image.Image:
    if scale == 1.0:
        return _reframe_safe_area(frame, offset_x=offset_x, offset_y=offset_y)
    width, height = frame.size
    resized_width = max(width, int(round(width * scale)))
    resized_height = max(height, int(round(height * scale)))
    resized = frame.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
    available_x = max(0, resized_width - width)
    available_y = max(0, resized_height - height)
    left = available_x / 2.0 - float(offset_x) * available_x / 2.0
    top = available_y / 2.0 - float(offset_y) * available_y / 2.0
    left = int(round(max(0.0, min(float(available_x), left))))
    top = int(round(max(0.0, min(float(available_y), top))))
    return resized.crop((left, top, left + width, top + height))


def _reframe_safe_area(
    frame: Image.Image,
    *,
    offset_x: float,
    offset_y: float,
) -> Image.Image:
    if offset_x == 0.0 and offset_y == 0.0:
        return frame
    width, height = frame.size
    target_aspect = 3.0 / 4.0
    if width / height > target_aspect:
        crop_height = height
        crop_width = max(1, int(round(height * target_aspect)))
    else:
        crop_width = width
        crop_height = max(1, int(round(width / target_aspect)))
    available_x = max(0, width - crop_width)
    available_y = max(0, height - crop_height)
    left = available_x / 2.0 - float(offset_x) * available_x / 2.0
    top = available_y / 2.0 - float(offset_y) * available_y / 2.0
    left = int(round(max(0.0, min(float(available_x), left))))
    top = int(round(max(0.0, min(float(available_y), top))))
    selected = frame.crop((left, top, left + crop_width, top + crop_height))
    result = frame.copy()
    paste_left = (width - crop_width) // 2
    paste_top = (height - crop_height) // 2
    result.paste(selected, (paste_left, paste_top))
    return result


def _find_source_frame(
    data: dict[str, Any],
    timeline_time_us: int,
    draft_dir: Path,
) -> tuple[Path, int]:
    materials = data.get("materials", {})
    videos = materials.get("videos", []) if isinstance(materials, dict) else []
    by_id = {
        str(item.get("id")): item
        for item in videos if isinstance(item, dict) and item.get("id")
    }
    tracks = data.get("tracks", [])
    video_tracks = [
        track for track in tracks if isinstance(track, dict) and track.get("type") == "video"
    ] if isinstance(tracks, list) else []
    if not video_tracks:
        raise RuntimeError("Cover creation requires a top-level video track")
    segments = video_tracks[0].get("segments", [])
    candidates = [segment for segment in segments if isinstance(segment, dict)] if isinstance(segments, list) else []
    if not candidates:
        raise RuntimeError("Cover creation requires a video segment")

    selected: dict[str, Any] | None = None
    for segment in candidates:
        target = segment.get("target_timerange", {})
        if not isinstance(target, dict):
            continue
        try:
            start = int(target.get("start", 0) or 0)
            duration = int(target.get("duration", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start <= timeline_time_us < start + duration:
            selected = segment
            break
    if selected is None:
        raise ValueError(f"Cover frame time is outside the main video track: {timeline_time_us}")

    material = by_id.get(str(selected.get("material_id", "")))
    if not isinstance(material, dict):
        raise RuntimeError("The selected cover frame has no video material")
    path = _resolve_media_path(str(material.get("path", "")), draft_dir)
    if not path.is_file():
        raise FileNotFoundError(f"Cover source media does not exist: {path}")

    target = selected.get("target_timerange", {})
    source = selected.get("source_timerange", {})
    target_start = int(target.get("start", 0) or 0)
    target_duration = int(target.get("duration", 0) or 0)
    source_start = int(source.get("start", 0) or 0) if isinstance(source, dict) else 0
    source_duration = int(source.get("duration", 0) or 0) if isinstance(source, dict) else 0
    offset = max(0, timeline_time_us - target_start)
    if target_duration > 0 and source_duration > 0:
        source_time = source_start + int(round(offset * source_duration / target_duration))
    else:
        source_time = source_start
    return path, source_time


def _find_preview_material_frame(
    data: dict[str, Any],
    source_time_us: int,
    draft_dir: Path,
) -> tuple[Path, int]:
    """Select the same first playable material used by the web preview endpoint."""
    materials = data.get("materials", {})
    videos = materials.get("videos", []) if isinstance(materials, dict) else []
    for material in videos if isinstance(videos, list) else []:
        if not isinstance(material, dict):
            continue
        raw_path = str(material.get("path", "")).strip()
        if not raw_path or raw_path.startswith("##_draftpath_placeholder_"):
            continue
        path = _resolve_media_path(raw_path, draft_dir)
        if not path.is_file() or path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            continue
        try:
            duration = int(material.get("duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 0 and source_time_us >= duration:
            raise ValueError(f"Cover frame time is outside the preview video: {source_time_us}")
        return path, source_time_us
    raise RuntimeError("Cover creation requires the same local video used by the mother preview")


def _resolve_media_path(value: str, draft_dir: Path) -> Path:
    text = value.strip().replace("\\", "/")
    marker = "_##/"
    if text.startswith("##_draftpath_placeholder_") and marker in text:
        return (draft_dir / text.split(marker, 1)[1]).resolve()
    return Path(value).expanduser().resolve()


def _extract_frame(source: Path, source_time_us: int, output: Path) -> tuple[int, int]:
    if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        with Image.open(source) as image:
            frame = image.convert("RGB")
            frame.save(output, quality=95)
            return frame.size

    import cv2

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open cover source video: {source}")
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, source_time_us / 1000.0)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise RuntimeError(
                f"Unable to read cover frame at {source_time_us / 1_000_000:.3f}s: {source}"
            )
        height, width = frame.shape[:2]
        encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not encoded:
            raise RuntimeError(f"Unable to encode cover frame: {source}")
        buffer.tofile(str(output))
        return int(width), int(height)
    finally:
        capture.release()


def _append_track(draft: Any, script: Any, track_type: Any, name: str) -> Any:
    append_track = getattr(script, "append_track", None)
    track_spec = getattr(draft, "TrackSpec", None)
    if callable(append_track) and track_spec is not None:
        return append_track(track_spec(track_type, name))
    return script.add_track(track_type, name, relative_index=999)
