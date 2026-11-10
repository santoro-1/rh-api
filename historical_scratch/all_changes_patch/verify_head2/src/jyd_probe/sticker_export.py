from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import shutil
from typing import Any

import numpy as np
from PIL import Image


STICKER_SCHEMA = "jyd_probe.fullscreen_sticker.v1"
STICKER_MANIFEST_SCHEMA = "jyd_probe.fullscreen_sticker_library_manifest.v1"


@dataclass(frozen=True)
class StickerExportResult:
    output_dir: Path
    manifest_path: Path
    scanned_segment_count: int
    exported_count: int
    existing_count: int
    duplicate_count: int
    missing_material_count: int
    missing_resource_count: int
    stickers: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "scanned_segment_count": self.scanned_segment_count,
            "exported_count": self.exported_count,
            "existing_count": self.existing_count,
            "duplicate_count": self.duplicate_count,
            "missing_material_count": self.missing_material_count,
            "missing_resource_count": self.missing_resource_count,
            "stickers": self.stickers,
        }


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_sticker"


def _identity(material: dict[str, Any]) -> str:
    for key in ("resource_id", "sticker_id", "id"):
        value = str(material.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    path = str(material.get("path", "")).strip()
    return f"path:{Path(path).as_posix().casefold()}" if path else ""


def _material_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    values = materials.get(key, []) if isinstance(materials, dict) else []
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _sticker_tracks(data: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == "sticker"
    ]


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": STICKER_MANIFEST_SCHEMA, "stickers": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": STICKER_MANIFEST_SCHEMA, "stickers": []}
    if not isinstance(data, dict) or data.get("schema") != STICKER_MANIFEST_SCHEMA:
        return {"schema": STICKER_MANIFEST_SCHEMA, "stickers": []}
    if not isinstance(data.get("stickers"), list):
        data["stickers"] = []
    return data


def _preview_path(resource_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for suffix in ("*.png", "*.webp", "*.jpg", "*.jpeg", "*.gif"):
        candidates.extend(resource_dir.rglob(suffix))
    files = [path for path in candidates if path.is_file()]
    return max(files, key=lambda path: path.stat().st_size) if files else None


def visible_content_bounds(image_path: str | Path) -> dict[str, float | int] | None:
    """Return normalized bounds for pixels that are visibly non-transparent."""

    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            sprite_bounds = _sprite_sheet_visible_bounds(path, image)
            if sprite_bounds is not None:
                return sprite_bounds
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            frame_count = min(60, max(1, int(getattr(image, "n_frames", 1) or 1)))
            union: tuple[int, int, int, int] | None = None
            frame_bounds_list: list[tuple[int, int, int, int]] = []
            for frame_index in range(frame_count):
                image.seek(frame_index)
                alpha = image.convert("RGBA").getchannel("A")
                _, maximum_alpha = alpha.getextrema()
                if maximum_alpha <= 0:
                    continue
                threshold = max(8, int(round(maximum_alpha * 0.05)))
                mask = alpha.point(lambda value: 255 if value >= threshold else 0)
                frame_bounds = mask.getbbox()
                if frame_bounds is None:
                    continue
                frame_bounds_list.append(frame_bounds)
                if union is None:
                    union = frame_bounds
                else:
                    union = (
                        min(union[0], frame_bounds[0]),
                        min(union[1], frame_bounds[1]),
                        max(union[2], frame_bounds[2]),
                        max(union[3], frame_bounds[3]),
                    )
            if union is None:
                return None
            left, top, right, bottom = union
            if right <= left or bottom <= top:
                return None
            result: dict[str, float | int] = {
                "left": left / float(width),
                "top": top / float(height),
                "right": right / float(width),
                "bottom": bottom / float(height),
                "source_width": width,
                "source_height": height,
            }
            result.update(_always_visible_frame_edges(frame_bounds_list, width, height))
            return result
    except (OSError, ValueError):
        return None


def corner_alpha_reveal(
    image_path: str | Path,
    corner: str,
    visible_ratio: float,
) -> dict[str, float | int] | None:
    """Locate a corner cut that exposes the requested share of visible alpha mass."""

    path = Path(image_path).expanduser().resolve()
    normalized_corner = str(corner).strip().lower().replace("-", "_")
    if normalized_corner not in {"top_left", "top_right", "bottom_left", "bottom_right"}:
        return None
    try:
        ratio = float(visible_ratio)
    except (TypeError, ValueError):
        return None
    if not path.is_file() or ratio <= 0.0 or ratio > 0.5:
        return None
    try:
        image_stamp = path.stat().st_mtime_ns
        info_path = path.parent / "ani_info.json"
        info_stamp = info_path.stat().st_mtime_ns if info_path.is_file() else 0
    except OSError:
        return None
    result = _corner_alpha_reveal_cached(
        str(path),
        image_stamp,
        info_stamp,
        normalized_corner,
        round(ratio, 6),
    )
    return dict(result) if result is not None else None


@lru_cache(maxsize=1024)
def _corner_alpha_reveal_cached(
    image_path: str,
    _image_stamp: int,
    _info_stamp: int,
    corner: str,
    visible_ratio: float,
) -> dict[str, float | int] | None:
    loaded = _load_alpha_frames(Path(image_path))
    if loaded is None:
        return None
    frames, width, height = loaded
    if not frames or width <= 0 or height <= 0:
        return None

    x_centers = (np.arange(width, dtype=np.float32) + 0.5) / float(width)
    y_centers = (np.arange(height, dtype=np.float32) + 0.5) / float(height)
    x_depth = 1.0 - x_centers if corner.endswith("left") else x_centers
    y_depth = 1.0 - y_centers if corner.startswith("top") else y_centers
    depth = np.maximum(y_depth[:, None], x_depth[None, :])
    bin_count = max(256, min(2048, max(width, height) * 2))
    depth_bins = np.ceil(depth * bin_count).astype(np.int32)

    required_depths: list[float] = []
    for frame in frames:
        alpha = np.asarray(frame, dtype=np.float32)
        maximum_alpha = float(alpha.max(initial=0.0))
        if maximum_alpha <= 0.0:
            continue
        threshold = max(8.0, maximum_alpha * 0.05)
        weights = np.where(alpha >= threshold, alpha, 0.0)
        total_alpha = float(weights.sum())
        if total_alpha <= 0.0:
            continue
        histogram = np.bincount(
            depth_bins.ravel(),
            weights=weights.ravel(),
            minlength=bin_count + 1,
        )
        cumulative = np.cumsum(histogram)
        target = total_alpha * visible_ratio
        depth_index = int(np.searchsorted(cumulative, target, side="left"))
        required_depths.append(min(1.0, depth_index / float(bin_count)))

    if not required_depths:
        return None
    required_depths.sort()
    robust_index = min(
        len(required_depths) - 1,
        max(0, math.ceil(len(required_depths) * 0.9) - 1),
    )
    selected_depth = required_depths[robust_index]
    cut_x = 1.0 - selected_depth if corner.endswith("left") else selected_depth
    cut_y = 1.0 - selected_depth if corner.startswith("top") else selected_depth
    return {
        "cut_x": max(0.0, min(1.0, cut_x)),
        "cut_y": max(0.0, min(1.0, cut_y)),
        "depth": selected_depth,
        "source_width": width,
        "source_height": height,
        "visible_frame_count": len(required_depths),
        "frame_percentile": 0.9,
    }


def _load_alpha_frames(image_path: Path) -> tuple[list[Image.Image], int, int] | None:
    try:
        with Image.open(image_path) as image:
            sprite_frames = _sprite_sheet_alpha_frames(image_path, image)
            if sprite_frames is not None:
                return sprite_frames
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            frame_count = max(1, int(getattr(image, "n_frames", 1) or 1))
            frames: list[Image.Image] = []
            for frame_index in _sample_frame_indices(frame_count, 60):
                image.seek(frame_index)
                frames.append(image.convert("RGBA").getchannel("A").copy())
            return frames, width, height
    except (OSError, ValueError):
        return None


def _sprite_sheet_alpha_frames(
    image_path: Path,
    atlas: Image.Image,
) -> tuple[list[Image.Image], int, int] | None:
    info_path = image_path.parent / "ani_info.json"
    if not info_path.is_file():
        return None
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta", {})
    if isinstance(meta, dict):
        atlas_name = str(meta.get("image", "")).strip()
        if atlas_name and atlas_name.casefold() != image_path.name.casefold():
            return None
    raw_frames = payload.get("frames", [])
    if not isinstance(raw_frames, list) or not raw_frames:
        return None

    descriptors: list[tuple[dict[str, Any], int, int]] = []
    source_width = source_height = 0
    for frame_index in _sample_frame_indices(len(raw_frames), 60):
        item = raw_frames[frame_index]
        if not isinstance(item, dict):
            continue
        source = item.get("sourceSize", {})
        if not isinstance(source, dict):
            continue
        try:
            current_width = int(source.get("w", 0))
            current_height = int(source.get("h", 0))
        except (TypeError, ValueError):
            continue
        if current_width <= 0 or current_height <= 0:
            continue
        descriptors.append((item, current_width, current_height))
        source_width = max(source_width, current_width)
        source_height = max(source_height, current_height)
    if not descriptors or source_width <= 0 or source_height <= 0:
        return None

    frames: list[Image.Image] = []
    for item, _, _ in descriptors:
        frame = item.get("frame", {})
        sprite = item.get("spriteSourceSize", {})
        if not isinstance(frame, dict) or not isinstance(sprite, dict):
            continue
        try:
            frame_x = int(frame.get("x", 0))
            frame_y = int(frame.get("y", 0))
            frame_width = int(frame.get("w", 0))
            frame_height = int(frame.get("h", 0))
            sprite_x = int(round(float(sprite.get("x", 0))))
            sprite_y = int(round(float(sprite.get("y", 0))))
            sprite_width = int(round(float(sprite.get("w", frame_width))))
            sprite_height = int(round(float(sprite.get("h", frame_height))))
        except (TypeError, ValueError):
            continue
        if min(frame_width, frame_height, sprite_width, sprite_height) <= 0:
            continue
        if frame_x < 0 or frame_y < 0:
            continue
        if frame_x + frame_width > atlas.width or frame_y + frame_height > atlas.height:
            continue
        frame_image = atlas.crop(
            (frame_x, frame_y, frame_x + frame_width, frame_y + frame_height)
        )
        if str(item.get("rotated", "false")).strip().lower() == "true":
            frame_image = frame_image.transpose(Image.Transpose.ROTATE_90)
        alpha = frame_image.convert("RGBA").getchannel("A")
        if alpha.size != (sprite_width, sprite_height):
            alpha = alpha.resize((sprite_width, sprite_height), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (source_width, source_height), 0)
        canvas.paste(alpha, (sprite_x, sprite_y))
        frames.append(canvas)
    return (frames, source_width, source_height) if frames else None


def _sample_frame_indices(frame_count: int, limit: int) -> list[int]:
    if frame_count <= limit:
        return list(range(frame_count))
    if limit <= 1:
        return [0]
    return sorted(
        {
            int(round(index * (frame_count - 1) / float(limit - 1)))
            for index in range(limit)
        }
    )


def _sprite_sheet_visible_bounds(
    image_path: Path,
    atlas: Image.Image,
) -> dict[str, float | int] | None:
    info_path = image_path.parent / "ani_info.json"
    if not info_path.is_file():
        return None
    try:
        payload = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta", {})
    if isinstance(meta, dict):
        atlas_name = str(meta.get("image", "")).strip()
        if atlas_name and atlas_name.casefold() != image_path.name.casefold():
            return None
    frames = payload.get("frames", [])
    if not isinstance(frames, list) or not frames:
        return None

    union: tuple[float, float, float, float] | None = None
    visible_frames: list[tuple[float, float, float, float]] = []
    source_width = source_height = 0
    for item in frames[:120]:
        if not isinstance(item, dict):
            continue
        frame = item.get("frame", {})
        source = item.get("sourceSize", {})
        sprite = item.get("spriteSourceSize", {})
        if not all(isinstance(value, dict) for value in (frame, source, sprite)):
            continue
        try:
            frame_x = int(frame.get("x", 0))
            frame_y = int(frame.get("y", 0))
            frame_width = int(frame.get("w", 0))
            frame_height = int(frame.get("h", 0))
            current_source_width = int(source.get("w", 0))
            current_source_height = int(source.get("h", 0))
            sprite_x = float(sprite.get("x", 0))
            sprite_y = float(sprite.get("y", 0))
            sprite_width = float(sprite.get("w", frame_width))
            sprite_height = float(sprite.get("h", frame_height))
        except (TypeError, ValueError):
            continue
        if min(frame_width, frame_height, current_source_width, current_source_height) <= 0:
            continue
        if frame_x < 0 or frame_y < 0:
            continue
        if frame_x + frame_width > atlas.width or frame_y + frame_height > atlas.height:
            continue
        frame_image = atlas.crop(
            (frame_x, frame_y, frame_x + frame_width, frame_y + frame_height)
        )
        if str(item.get("rotated", "false")).strip().lower() == "true":
            frame_image = frame_image.transpose(Image.Transpose.ROTATE_90)
        frame_bounds = _alpha_visible_bounds(frame_image)
        if frame_bounds is None:
            continue
        left, top, right, bottom = frame_bounds
        visible = (
            (sprite_x + left * sprite_width / frame_image.width) / current_source_width,
            (sprite_y + top * sprite_height / frame_image.height) / current_source_height,
            (sprite_x + right * sprite_width / frame_image.width) / current_source_width,
            (sprite_y + bottom * sprite_height / frame_image.height) / current_source_height,
        )
        visible_frames.append(visible)
        if union is None:
            union = visible
        else:
            union = (
                min(union[0], visible[0]),
                min(union[1], visible[1]),
                max(union[2], visible[2]),
                max(union[3], visible[3]),
            )
        source_width = max(source_width, current_source_width)
        source_height = max(source_height, current_source_height)
    if union is None or source_width <= 0 or source_height <= 0:
        return None
    result: dict[str, float | int] = {
        "left": max(0.0, min(1.0, union[0])),
        "top": max(0.0, min(1.0, union[1])),
        "right": max(0.0, min(1.0, union[2])),
        "bottom": max(0.0, min(1.0, union[3])),
        "source_width": source_width,
        "source_height": source_height,
    }
    result.update(_always_visible_normalized_edges(visible_frames))
    return result


def _always_visible_frame_edges(
    frames: list[tuple[int, int, int, int]],
    width: int,
    height: int,
) -> dict[str, float]:
    normalized = [
        (
            left / float(width),
            top / float(height),
            right / float(width),
            bottom / float(height),
        )
        for left, top, right, bottom in frames
        if right > left and bottom > top
    ]
    return _always_visible_normalized_edges(normalized)


def _always_visible_normalized_edges(
    frames: list[tuple[float, float, float, float]],
) -> dict[str, float]:
    valid = [
        (left, top, right, bottom)
        for left, top, right, bottom in frames
        if 0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0
    ]
    if not valid:
        return {}
    return {
        # Corner placement uses the edge that still exposes content in every non-empty frame.
        "visible_frame_count": float(len(valid)),
        "always_visible_left": max(frame[0] for frame in valid),
        "always_visible_top": max(frame[1] for frame in valid),
        "always_visible_right": min(frame[2] for frame in valid),
        "always_visible_bottom": min(frame[3] for frame in valid),
        "minimum_visible_width": min(frame[2] - frame[0] for frame in valid),
        "minimum_visible_height": min(frame[3] - frame[1] for frame in valid),
    }


def _alpha_visible_bounds(image: Image.Image) -> tuple[int, int, int, int] | None:
    alpha = image.convert("RGBA").getchannel("A")
    _, maximum_alpha = alpha.getextrema()
    if maximum_alpha <= 0:
        return None
    threshold = max(8, int(round(maximum_alpha * 0.05)))
    mask = alpha.point(lambda value: 255 if value >= threshold else 0)
    return mask.getbbox()


def export_sticker_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_label: str = "",
    replace: bool = False,
    usage: str = "fullscreen_overlay",
) -> StickerExportResult:
    """Export top-level sticker segments into a purpose-specific sticker library."""

    if usage not in {"fullscreen_overlay", "corner_decoration"}:
        raise ValueError(f"不支持的贴纸用途: {usage}")

    root = Path(output_dir).expanduser().resolve()
    bundles_dir = root / "bundles"
    manifest_dir = root / "manifest"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "sticker_manifest.json"
    manifest = _load_manifest(manifest_path)
    stored = [item for item in manifest.get("stickers", []) if isinstance(item, dict)]
    stored_by_identity = {str(item.get("identity")): item for item in stored if item.get("identity")}

    materials = _material_list(data, "stickers")
    by_id = {str(item.get("id")): item for item in materials if item.get("id")}
    scanned = exported = existing = duplicates = missing_materials = missing_resources = 0
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for sticker_track_index, (raw_track_index, track) in enumerate(_sticker_tracks(data)):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            scanned += 1
            material_id = str(segment.get("material_id", ""))
            material = by_id.get(material_id)
            if material is None:
                missing_materials += 1
                continue
            identity = _identity(material) or f"material_id:{material_id}"
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)

            fallback_name = "未命名四角贴纸" if usage == "corner_decoration" else "未命名全屏贴纸"
            name = str(material.get("name", "")).strip() or fallback_name
            identity_suffix = _safe_filename(identity.split(":", 1)[-1][-24:])
            stored_record = stored_by_identity.get(identity)
            bundle_dir = (
                root / str(stored_record["bundle"])
                if stored_record and stored_record.get("bundle")
                else bundles_dir / f"{_safe_filename(name)}_{identity_suffix}"
            )
            bundle_dir.mkdir(parents=True, exist_ok=True)
            resource_source = Path(str(material.get("path", ""))).expanduser()
            resource_dir = bundle_dir / "resources" / "sticker"
            resource_status = "missing"
            if resource_source.is_dir():
                if replace and resource_dir.exists():
                    shutil.rmtree(resource_dir)
                if not resource_dir.exists():
                    shutil.copytree(resource_source, resource_dir)
                    resource_status = "copied"
                else:
                    resource_status = "existing"
            else:
                missing_resources += 1

            preview = _preview_path(resource_dir) if resource_dir.is_dir() else None
            metadata_path = bundle_dir / "sticker.json"
            payload = {
                "schema": STICKER_SCHEMA,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "identity": identity,
                "name": name,
                "usage": usage,
                "material": deepcopy(material),
                "segment_template": deepcopy(segment),
                "resource": {
                    "original_path": str(material.get("path", "")),
                    "library_path": "resources/sticker" if resource_dir.is_dir() else "",
                    "status": resource_status,
                },
                "preview_file": preview.relative_to(bundle_dir).as_posix() if preview else "",
                "content_bounds": visible_content_bounds(preview) if preview else None,
                "source": {
                    "label": source_label,
                    "sticker_track_index": sticker_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": material_id,
                    "target_timerange": deepcopy(segment.get("target_timerange")),
                },
            }
            status = "existing"
            if replace or not metadata_path.exists():
                status = "exported"
                exported += 1
            else:
                existing += 1
            metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            record = {
                "identity": identity,
                "name": name,
                "resource_id": str(material.get("resource_id", "")),
                "sticker_id": str(material.get("sticker_id", "")),
                "bundle": bundle_dir.relative_to(root).as_posix(),
                "metadata_file": metadata_path.relative_to(root).as_posix(),
                "preview_file": preview.relative_to(root).as_posix() if preview else "",
                "status": status if resource_dir.is_dir() else f"{status}_resource_missing",
            }
            records.append(record)
            if stored_record is None:
                stored.append({key: value for key, value in record.items() if key != "status"})
            else:
                stored_record.clear()
                stored_record.update({key: value for key, value in record.items() if key != "status"})

    if scanned == 0:
        raise RuntimeError("草稿中没有找到顶层 sticker 轨道片段")
    if not records:
        raise RuntimeError("贴纸片段无法关联到 materials.stickers")

    manifest.update(
        {
            "schema": STICKER_MANIFEST_SCHEMA,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "source": source_label,
            "stickers": stored,
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return StickerExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scanned_segment_count=scanned,
        exported_count=exported,
        existing_count=existing,
        duplicate_count=duplicates,
        missing_material_count=missing_materials,
        missing_resource_count=missing_resources,
        stickers=records,
    )
