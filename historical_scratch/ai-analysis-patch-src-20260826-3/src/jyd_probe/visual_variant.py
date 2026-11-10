from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable, Protocol
import math
import uuid


FACE_HEADROOM_FACTOR = 0.75
FACE_CROP_TOP_SNAP_THRESHOLD = 0.10


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


class FaceCenterLocator(Protocol):
    def locate(
        self,
        media_path: str,
        source_start_us: int,
        source_duration_us: int,
        sample_count: int,
    ) -> tuple[float, float] | None: ...


@dataclass(frozen=True)
class VisualVariant:
    mirror_interval_us: int = 10_000_000
    crop_ratio: str = "1:1"
    background_color: str = "#000000FF"
    face_centered: bool = True
    face_sample_count: int = 3
    video_track_index: int = 0
    crop_offset_x: float = 0.0
    crop_offset_y: float = 0.0
    crop_zoom: float = 1.0


class OpenCvFaceCenterLocator:
    """Locate a stable frontal-face center from a few source frames."""

    def __init__(self) -> None:
        self._cv2: Any | None = None
        self._classifier: Any | None = None

    def _load(self) -> tuple[Any, Any]:
        if self._cv2 is not None and self._classifier is not None:
            return self._cv2, self._classifier
        import cv2  # Imported lazily so draft-only workflows still start without OpenCV.

        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        classifier = cv2.CascadeClassifier(str(cascade_path))
        if classifier.empty():
            raise RuntimeError(f"Unable to load face detector: {cascade_path}")
        self._cv2 = cv2
        self._classifier = classifier
        return cv2, classifier

    def locate(
        self,
        media_path: str,
        source_start_us: int,
        source_duration_us: int,
        sample_count: int,
    ) -> tuple[float, float] | None:
        path = Path(media_path).expanduser()
        if not path.is_file():
            return None
        try:
            cv2, classifier = self._load()
        except (ImportError, RuntimeError):
            return None

        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            return None
        try:
            duration_us = max(0, int(source_duration_us))
            if duration_us <= 0:
                fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
                frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
                if fps > 0 and frame_count > 0:
                    duration_us = int(frame_count / fps * 1_000_000)
            count = min(9, max(1, int(sample_count)))
            fractions = [(index + 1) / (count + 1) for index in range(count)]
            centers: list[tuple[float, float]] = []
            for fraction in fractions:
                timestamp_us = max(0, int(source_start_us)) + int(duration_us * fraction)
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp_us / 1000.0)
                ok, frame = capture.read()
                if not ok or frame is None:
                    continue
                height, width = frame.shape[:2]
                if width <= 0 or height <= 0:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = classifier.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(max(32, width // 25), max(32, height // 25)),
                )
                if len(faces) == 0:
                    continue
                x, y, face_width, face_height = max(
                    faces, key=lambda item: int(item[2]) * int(item[3])
                )
                centers.append(
                    _face_center_with_headroom(
                        int(x), int(y), int(face_width), int(face_height), width, height
                    )
                )
            if not centers:
                return None
            return (
                min(1.0, max(0.0, float(median(point[0] for point in centers)))),
                min(1.0, max(0.0, float(median(point[1] for point in centers)))),
            )
        finally:
            capture.release()


def _face_center_with_headroom(
    x: int,
    y: int,
    face_width: int,
    face_height: int,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float]:
    """Bias the crop anchor above the detected face so hair and head room remain visible."""

    center_x = (float(x) + float(face_width) / 2.0) / float(frame_width)
    face_center_y = float(y) + float(face_height) / 2.0
    center_y = (face_center_y - float(face_height) * FACE_HEADROOM_FACTOR) / float(frame_height)
    return (
        min(1.0, max(0.0, center_x)),
        min(1.0, max(0.0, center_y)),
    )


def apply_visual_variant_to_data(
    data: dict[str, Any],
    variant: VisualVariant,
    *,
    face_locator: FaceCenterLocator | None = None,
    warning: Callable[[str], None] | None = None,
) -> int:
    """Crop each source segment once, then split it for alternating mirror."""

    tracks = data.get("tracks", [])
    track_items = tracks if isinstance(tracks, list) else []
    video_tracks = [
        track
        for track in track_items
        if isinstance(track, dict) and track.get("type") == "video"
    ]
    if variant.video_track_index < 0 or variant.video_track_index >= len(video_tracks):
        raise IndexError(f"Video track index out of range: {variant.video_track_index}")
    track = video_tracks[variant.video_track_index]

    changed = 0
    if variant.crop_ratio:
        locator = face_locator
        if locator is None and variant.face_centered:
            locator = OpenCvFaceCenterLocator()
        changed += _apply_crop_and_color_background(
            data,
            track,
            ratio_text=variant.crop_ratio,
            color=variant.background_color,
            face_locator=locator if variant.face_centered else None,
            face_sample_count=variant.face_sample_count,
            crop_offset_x=variant.crop_offset_x,
            crop_offset_y=variant.crop_offset_y,
            crop_zoom=variant.crop_zoom,
            warning=warning,
        )

    if variant.mirror_interval_us > 0:
        changed += _apply_alternating_mirror(
            data,
            track,
            variant.mirror_interval_us,
            warning=warning,
        )
    return changed


def _apply_alternating_mirror(
    data: dict[str, Any],
    track: dict[str, Any],
    interval_us: int,
    *,
    warning: Callable[[str], None] | None,
) -> int:
    if interval_us <= 0:
        raise ValueError("Mirror interval must be positive")
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        return 0

    materials = data.get("materials", {})
    transition_ids = _material_ids(materials, "transitions")
    curve_speed_ids = {
        str(item.get("id"))
        for item in _material_items(materials, "speeds")
        if item.get("id") and item.get("curve_speed")
    }
    output: list[dict[str, Any]] = []
    changes = 0
    for segment in segments:
        if not isinstance(segment, dict):
            output.append(segment)
            continue
        target = segment.get("target_timerange", {})
        if not isinstance(target, dict):
            output.append(segment)
            continue
        start = int(target.get("start", 0) or 0)
        duration = int(target.get("duration", 0) or 0)
        if duration <= 0:
            output.append(segment)
            continue
        end = start + duration
        first_boundary = (start // interval_us + 1) * interval_us
        boundaries = list(range(first_boundary, end, interval_us))
        if boundaries and _segment_split_is_unsafe(segment, curve_speed_ids):
            midpoint = start + duration // 2
            _set_interval_flip(segment, midpoint, interval_us)
            output.append(segment)
            changes += 1
            if warning is not None:
                warning(
                    f"Mirror boundary crossed a complex segment at {start}us; "
                    "the complete segment was assigned by its midpoint."
                )
            continue

        points = [start, *boundaries, end]
        original_flip = bool(
            segment.get("clip", {}).get("flip", {}).get("horizontal", False)
            if isinstance(segment.get("clip"), dict)
            else False
        )
        for piece_index, (piece_start, piece_end) in enumerate(zip(points, points[1:])):
            piece = deepcopy(segment)
            if piece_index > 0:
                piece["id"] = _new_id()
            piece_duration = piece_end - piece_start
            piece["target_timerange"] = {"start": piece_start, "duration": piece_duration}
            _slice_source_timerange(piece, segment, piece_start - start, piece_duration, duration)
            mirrored = ((piece_start // interval_us) % 2) == 1
            _set_horizontal_flip(piece, (not original_flip) if mirrored else original_flip)
            if piece_index < len(points) - 2:
                refs = piece.get("extra_material_refs", [])
                if isinstance(refs, list):
                    piece["extra_material_refs"] = [
                        ref for ref in refs if str(ref) not in transition_ids
                    ]
            output.append(piece)
        if boundaries:
            changes += len(points) - 1
        else:
            expected = (not original_flip) if ((start // interval_us) % 2 == 1) else original_flip
            current = bool(
                segment.get("clip", {}).get("flip", {}).get("horizontal", False)
                if isinstance(segment.get("clip"), dict)
                else False
            )
            if expected != current:
                changes += 1
    track["segments"] = output
    return changes


def _segment_split_is_unsafe(segment: dict[str, Any], curve_speed_ids: set[str]) -> bool:
    if bool(segment.get("reverse")):
        return True
    if segment.get("common_keyframes") or segment.get("keyframe_refs"):
        return True
    refs = segment.get("extra_material_refs", [])
    return isinstance(refs, list) and any(str(ref) in curve_speed_ids for ref in refs)


def _slice_source_timerange(
    piece: dict[str, Any],
    original: dict[str, Any],
    target_offset_us: int,
    target_duration_us: int,
    original_target_duration_us: int,
) -> None:
    source = original.get("source_timerange")
    if not isinstance(source, dict):
        return
    source_start = int(source.get("start", 0) or 0)
    source_duration = int(source.get("duration", 0) or 0)
    if source_duration <= 0 or original_target_duration_us <= 0:
        return
    ratio = source_duration / float(original_target_duration_us)
    piece["source_timerange"] = {
        "start": source_start + int(round(target_offset_us * ratio)),
        "duration": max(1, int(round(target_duration_us * ratio))),
    }


def _set_interval_flip(segment: dict[str, Any], timestamp_us: int, interval_us: int) -> None:
    clip = segment.get("clip", {})
    original = bool(
        clip.get("flip", {}).get("horizontal", False) if isinstance(clip, dict) else False
    )
    mirrored = ((timestamp_us // interval_us) % 2) == 1
    _set_horizontal_flip(segment, (not original) if mirrored else original)


def _set_horizontal_flip(segment: dict[str, Any], enabled: bool) -> None:
    clip = segment.setdefault("clip", {})
    if not isinstance(clip, dict):
        clip = {}
        segment["clip"] = clip
    flip = clip.setdefault("flip", {})
    if not isinstance(flip, dict):
        flip = {}
        clip["flip"] = flip
    flip["horizontal"] = bool(enabled)
    flip.setdefault("vertical", False)


def _apply_crop_and_color_background(
    data: dict[str, Any],
    track: dict[str, Any],
    *,
    ratio_text: str,
    color: str,
    face_locator: FaceCenterLocator | None,
    face_sample_count: int,
    crop_offset_x: float,
    crop_offset_y: float,
    crop_zoom: float,
    warning: Callable[[str], None] | None,
) -> int:
    target_ratio = _parse_ratio(ratio_text)
    normalized_color = _normalize_color(color)
    materials = data.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("Draft materials must be an object")
    videos = materials.setdefault("videos", [])
    canvases = materials.setdefault("canvases", [])
    if not isinstance(videos, list) or not isinstance(canvases, list):
        raise RuntimeError("Draft video and canvas materials must be arrays")
    video_by_id = {
        str(item.get("id")): item for item in videos if isinstance(item, dict) and item.get("id")
    }
    old_canvas_ids = {
        str(item.get("id")) for item in canvases if isinstance(item, dict) and item.get("id")
    }

    changed = 0
    segments = track.get("segments", [])
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        material = video_by_id.get(str(segment.get("material_id", "")))
        if not isinstance(material, dict) or str(material.get("type", "video")) != "video":
            continue
        width = int(material.get("width", 0) or 0)
        height = int(material.get("height", 0) or 0)
        if width <= 0 or height <= 0:
            continue
        source = segment.get("source_timerange", {})
        source_start = int(source.get("start", 0) or 0) if isinstance(source, dict) else 0
        source_duration = int(source.get("duration", 0) or 0) if isinstance(source, dict) else 0
        face_center = None
        media_path = str(material.get("path", "")).strip()
        if face_locator is not None and media_path:
            face_center = face_locator.locate(
                media_path,
                source_start,
                source_duration,
                face_sample_count,
            )
        center_x, center_y = face_center or (0.5, 0.5)
        crop = _crop_rectangle(
            width,
            height,
            target_ratio,
            center_x,
            center_y,
            offset_x=crop_offset_x,
            offset_y=crop_offset_y,
            zoom=crop_zoom,
        )
        if face_center is not None and crop_offset_x == 0.0 and crop_offset_y == 0.0 and crop_zoom == 1.0:
            crop = _snap_crop_to_top(crop)
        elif width / float(height) < target_ratio:
            crop = _anchor_crop_to_top(crop)
            if warning is not None:
                warning(
                    f"No face was detected in {media_path or 'the source video'}; "
                    "the portrait crop was anchored to the top edge."
                )

        material_copy = deepcopy(material)
        material_copy["id"] = _new_id()
        if "material_id" in material_copy:
            material_copy["material_id"] = material_copy["id"]
        if "local_material_id" in material_copy:
            material_copy["local_material_id"] = _new_id()
        material_copy["crop"] = crop
        material_copy["crop_ratio"] = "free"
        material_copy["crop_scale"] = 1.0
        videos.append(material_copy)
        segment["material_id"] = material_copy["id"]

        refs = segment.setdefault("extra_material_refs", [])
        if not isinstance(refs, list):
            refs = []
            segment["extra_material_refs"] = refs
        refs[:] = [ref for ref in refs if str(ref) not in old_canvas_ids]
        canvas_id = _new_id()
        canvases.append(
            {
                "id": canvas_id,
                "type": "canvas_color",
                "blur": 0.0,
                "color": normalized_color,
                "source_platform": 0,
            }
        )
        refs.append(canvas_id)
        changed += 1
    return changed


def _parse_ratio(value: str) -> float:
    text = str(value).strip().replace("：", ":")
    if ":" not in text:
        raise ValueError(f"Invalid crop ratio: {value!r}")
    left, right = text.split(":", 1)
    width = float(left)
    height = float(right)
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError(f"Invalid crop ratio: {value!r}")
    return width / height


def _crop_rectangle(
    width: int,
    height: int,
    target_ratio: float,
    center_x: float,
    center_y: float,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    zoom: float = 1.0,
) -> dict[str, float]:
    if not -1.0 <= offset_x <= 1.0 or not -1.0 <= offset_y <= 1.0:
        raise ValueError("Crop offsets must be between -1 and 1")
    if not 1.0 <= zoom <= 4.0:
        raise ValueError("Crop zoom must be between 1 and 4")
    source_ratio = width / float(height)
    if source_ratio < target_ratio:
        crop_width = 1.0 / zoom
        crop_height = source_ratio / target_ratio / zoom
    else:
        crop_width = target_ratio / source_ratio / zoom
        crop_height = 1.0 / zoom
    center_x += float(offset_x) * max(0.0, 1.0 - crop_width)
    center_y += float(offset_y) * max(0.0, 1.0 - crop_height)
    left = min(1.0 - crop_width, max(0.0, center_x - crop_width / 2.0))
    top = min(1.0 - crop_height, max(0.0, center_y - crop_height / 2.0))
    right = left + crop_width
    bottom = top + crop_height
    return {
        "upper_left_x": left,
        "upper_left_y": top,
        "upper_right_x": right,
        "upper_right_y": top,
        "lower_left_x": left,
        "lower_left_y": bottom,
        "lower_right_x": right,
        "lower_right_y": bottom,
    }


def _snap_crop_to_top(crop: dict[str, float]) -> dict[str, float]:
    """Avoid shaving a small strip from the top of a face-centered portrait crop."""

    top = float(crop.get("upper_left_y", 0.0))
    if top <= 0.0 or top > FACE_CROP_TOP_SNAP_THRESHOLD:
        return crop
    adjusted = dict(crop)
    for key in ("upper_left_y", "upper_right_y"):
        adjusted[key] = 0.0
    for key in ("lower_left_y", "lower_right_y"):
        adjusted[key] = max(0.0, float(crop[key]) - top)
    return adjusted


def _anchor_crop_to_top(crop: dict[str, float]) -> dict[str, float]:
    """Move a portrait crop to the source top while preserving its height."""

    top = float(crop.get("upper_left_y", 0.0))
    if top <= 0.0:
        return crop
    adjusted = dict(crop)
    for key in ("upper_left_y", "upper_right_y"):
        adjusted[key] = 0.0
    for key in ("lower_left_y", "lower_right_y"):
        adjusted[key] = max(0.0, float(crop[key]) - top)
    return adjusted


def _normalize_color(value: str) -> str:
    text = str(value).strip().upper()
    if not text.startswith("#"):
        text = f"#{text}"
    if len(text) == 7:
        text += "FF"
    if len(text) != 9 or any(char not in "0123456789ABCDEF" for char in text[1:]):
        raise ValueError(f"Invalid background color: {value!r}")
    return text


def _material_items(materials: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(materials, dict):
        return []
    values = materials.get(key, [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def _material_ids(materials: Any, key: str) -> set[str]:
    return {
        str(item.get("id"))
        for item in _material_items(materials, key)
        if item.get("id")
    }
