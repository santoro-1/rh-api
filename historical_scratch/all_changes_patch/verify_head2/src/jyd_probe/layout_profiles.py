from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


DEFAULT_LAYOUT_PROFILE = "standing"
LAYOUT_PROFILE_FONT_IDENTITY = "resource_id:7086699209738424840"


_PROFILES: dict[str, dict[str, Any]] = {
    "standing": {
        "id": "standing",
        "label": "站姿",
        "caption": {
            "font_size": 11.0,
            "clip_scale": 1.32,
            "transform_x": 0.0,
            "transform_y": -0.382336816305469,
            "max_width_ratio": 0.8,
            "shadow_alpha": 0.8999999761581421,
        },
        "title": {
            "font_size": 19.0,
            "clip_scale": 0.9744358456743959,
            "transform_y": 0.8155959933996199,
            "shadow_alpha": 0.8999999761581421,
        },
        "disclaimer": {
            "font_size": 6.0,
            "clip_scale": 1.0,
            "transform_y": -0.916666666666667,
            "opacity": 0.5,
            "shadow_alpha": 0.0,
        },
        "nameplate": {
            "bundle": "fixed/nameplate_standing",
            "preview_url": "/api/new/fixed-visuals/nameplate/preview?layout_profile=standing",
            "scale": 0.8941348042237189,
            "rotation": -90.0,
            "transform_x": -0.22939889867171298,
            "transform_y": -0.11377708978328174,
            "texts": [
                {"text": "张雒", "size": 11.0, "scale": 0.730086445985374, "x": -0.6479077925336624, "y": -0.10062777724609877, "letter_spacing": 0.0},
                {"text": "世界蹦床冠军", "size": 11.0, "scale": 0.698199220466561, "x": -0.08069015465985288, "y": -0.06835219192755801, "letter_spacing": 0.1},
                {"text": "专注35+女性身材管理", "size": 11.0, "scale": 0.6654920116734253, "x": -0.06369486962992593, "y": -0.14366189100415258, "letter_spacing": 0.1},
            ],
        },
        "cover": {
            "text_scale": 1.1045453049181124,
            "overlay_y_ratio": 0.615,
            "overlay_height_ratio": 0.28,
            "line_1_x": 0.0,
            "line_1_y": -0.083333333333333,
            "line_2_x": 0.0,
            "line_2_y": -0.341145833333333,
            "line_1_size": 30.0,
            "line_2_size": 22.0,
            "line_1_color": "#FADF4A",
            "line_2_color": "#F5F6F0",
            "auto_wrapping": False,
            "max_line_width": 0.86,
            "shadow_color": "#000000",
            "shadow_alpha": 0.8999999761581421,
            "shadow_smoothing": 0.45000001788139343,
            "shadow_distance": 5.0,
            "shadow_angle": -45.0,
        },
        "image": {
            "default": {"visible_width_ratio": 0.4436932871810325, "transform_x": 0.0, "transform_y": -0.5676356945952492},
        },
    },
    "seated": {
        "id": "seated",
        "label": "坐姿",
        "caption": {
            "font_size": 15.0,
            "clip_scale": 1.0,
            "transform_x": 0.0,
            "transform_y": -0.32080308951309267,
            "max_width_ratio": 0.8,
            "shadow_alpha": 1.0,
        },
        "title": {
            "font_size": 19.0,
            "clip_scale": 1.0,
            "transform_y": 0.799479166666667,
            "shadow_alpha": 1.0,
        },
        "disclaimer": {
            "font_size": 6.0,
            "clip_scale": 1.0,
            "transform_y": -0.916666666666667,
            "opacity": 0.5,
            "shadow_alpha": 1.0,
        },
        "nameplate": {
            "bundle": "fixed/nameplate_seated",
            "preview_url": "/api/new/fixed-visuals/nameplate/preview?layout_profile=seated",
            "scale": 1.08873624376896,
            "rotation": -90.0,
            "transform_x": -0.3528198074277854,
            "transform_y": -0.13235294117647034,
            "texts": [
                {"text": "张雒", "size": 15.0, "scale": 0.7216448973816557, "x": -0.689338395446377, "y": -0.053984155827776145, "letter_spacing": 0.0},
                {"text": "蹦床世界冠军", "size": 15.0, "scale": 0.5396930020714986, "x": -0.48267319374057716, "y": -0.11465676383966864, "letter_spacing": 0.0},
                {"text": "专注35+女性身材管理", "size": 15.0, "scale": 0.47589366282274725, "x": -0.3366514707652578, "y": -0.18291344785304778, "letter_spacing": 0.0},
            ],
        },
        "cover": {
            "text_scale": 1.0,
            "overlay_y_ratio": 0.615,
            "overlay_height_ratio": 0.28,
            "line_1_x": 0.0,
            "line_1_y": -0.083333333333333,
            "line_2_x": 0.0,
            "line_2_y": -0.341145833333333,
            "line_1_size": 30.0,
            "line_2_size": 22.0,
            "line_1_color": "#FADF4A",
            "line_2_color": "#F5F6F0",
            "auto_wrapping": False,
            "max_line_width": 0.86,
            "shadow_color": "#000000",
            "shadow_alpha": 1.0,
            "shadow_smoothing": 0.45000001788139343,
            "shadow_distance": 5.0,
            "shadow_angle": -45.0,
        },
        "image": {
            "portrait": {"visible_width_ratio": 0.150470, "transform_x": -0.041906, "transform_y": -0.499735},
            "landscape": {"visible_width_ratio": 0.381488, "transform_x": 0.0, "transform_y": -0.506992},
        },
    },
}


def normalize_layout_profile(value: Any) -> str:
    normalized = str(value or DEFAULT_LAYOUT_PROFILE).strip().lower().replace("-", "_")
    aliases = {"stand": "standing", "站": "standing", "站姿": "standing", "sit": "seated", "坐": "seated", "坐姿": "seated"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in _PROFILES:
        raise ValueError("人物姿态只能是 standing（站姿）或 seated（坐姿）")
    return normalized


def layout_profile(value: Any) -> dict[str, Any]:
    return deepcopy(_PROFILES[normalize_layout_profile(value)])


def public_layout_profiles() -> list[dict[str, Any]]:
    return [layout_profile(profile_id) for profile_id in ("standing", "seated")]


def layout_font(fonts: Mapping[str, dict[str, Any]], fallback_identity: str = "") -> dict[str, Any]:
    font = fonts.get(LAYOUT_PROFILE_FONT_IDENTITY)
    if isinstance(font, dict) and font.get("path"):
        return font
    fallback = fonts.get(str(fallback_identity or ""))
    if isinstance(fallback, dict) and fallback.get("path"):
        return fallback
    raise ValueError("规范字体“金陵体”不可用")


def nameplate_overlay(library_root: str | Path, profile_value: Any) -> dict[str, Any]:
    profile = layout_profile(profile_value)
    nameplate = profile["nameplate"]
    bundle = Path(library_root).expanduser().resolve() / str(nameplate["bundle"])
    return {
        "enabled": True,
        "renderer": "sticker",
        "bundle_path": str(bundle),
        "preview_url": str(nameplate["preview_url"]),
        "start_us": 0,
        "duration_us": 0,
        "corner": "center",
        "scale": float(nameplate["scale"]),
        "rotation": float(nameplate["rotation"]),
        "transform_x": float(nameplate["transform_x"]),
        "transform_y": float(nameplate["transform_y"]),
        "opacity": 1.0,
        "track_name": f"固定人名牌·{profile['label']}",
    }


def nameplate_texts(profile_value: Any, *, font: Mapping[str, Any]) -> list[dict[str, Any]]:
    profile = layout_profile(profile_value)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(profile["nameplate"]["texts"], start=1):
        result.append(
            {
                "type": "add",
                "scope": "top",
                "text": row["text"],
                "track_name": f"固定人名牌·{profile['label']}·文字{index}",
                "start_us": 0,
                "duration_us": 0,
                "relative_index": 940 + index,
                "transform_x": float(row["x"]),
                "transform_y": float(row["y"]),
                "scale": float(row["scale"]),
                "size": float(row["size"]),
                "align": 1,
                "auto_wrapping": False,
                "line_max_width": 0.92,
                "letter_spacing": float(row["letter_spacing"]),
                "color": "#FCFCFC" if profile["id"] == "seated" and index == 1 else "#FFFFFF",
                "opacity": 1.0,
                "shadow_color": "#000000",
                "shadow_alpha": 1.0,
                "shadow_distance": 5.0,
                "shadow_angle": -45.0,
                "shadow_smoothing": 0.45000001788139343,
                "font_id": str(font.get("resource_id") or "7086699209738424840"),
                "font_path": str(font.get("path") or ""),
                "font_title": str(font.get("name") or "金陵体"),
            }
        )
    return result


def apply_layout_to_visual_overlays(
    overlays: list[dict[str, Any]], profile_value: Any
) -> list[dict[str, Any]]:
    """Apply the selected human-layout geometry without mutating the saved recipe."""

    profile = layout_profile(profile_value)
    result: list[dict[str, Any]] = []
    for raw in overlays:
        overlay = dict(raw)
        if str(overlay.get("media_type") or "image") != "image":
            result.append(overlay)
            continue
        image_rules = profile["image"]
        if profile["id"] == "standing":
            rule = image_rules["default"]
        else:
            rule = image_rules[_image_shape(overlay.get("bundle_path"))]
        geometry = _compensated_image_geometry(overlay.get("bundle_path"), rule)
        overlay.update({"corner": "center", **geometry, "layout_profile": profile["id"]})
        result.append(overlay)
    return result


def _image_path(bundle_value: Any) -> Path | None:
    text = str(bundle_value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser().resolve()
    if path.is_dir():
        path = path / "resources" / "sticker" / "singleImage.png"
    return path if path.is_file() else None


def _alpha_box(bundle_value: Any) -> tuple[int, int, int, int, int, int] | None:
    path = _image_path(bundle_value)
    if path is None:
        return None
    try:
        from PIL import Image

        with Image.open(path).convert("RGBA") as image:
            box = image.getchannel("A").getbbox() or (0, 0, image.width, image.height)
            return image.width, image.height, *box
    except (OSError, ValueError):
        return None


def _image_shape(bundle_value: Any) -> str:
    geometry = _alpha_box(bundle_value)
    if geometry is None:
        return "landscape"
    _width, _height, left, top, right, bottom = geometry
    return "portrait" if bottom - top > right - left else "landscape"


def _compensated_image_geometry(
    bundle_value: Any, rule: Mapping[str, Any]
) -> dict[str, float]:
    visible_scale = float(rule["visible_width_ratio"])
    target_x = float(rule["transform_x"])
    target_y = float(rule["transform_y"])
    geometry = _alpha_box(bundle_value)
    if geometry is None:
        return {"scale": visible_scale, "transform_x": target_x, "transform_y": target_y}
    width, height, left, top, right, bottom = geometry
    content_width = max(1, right - left)
    raw_scale = visible_scale * width / content_width
    content_center_x = (left + right) / 2
    content_center_y = (top + bottom) / 2
    segment_x = target_x - 2 * (content_center_x - width / 2) / width * raw_scale
    segment_y = target_y + 2 * (content_center_y - height / 2) / width * raw_scale * (1080 / 1920)
    return {"scale": raw_scale, "transform_x": segment_x, "transform_y": segment_y}
