from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "h3.project.v1"
DEFAULT_ASPECT_RATIO = "9:16 (Portrait Widescreen)"
DEFAULT_MEGAPIXELS = 1.0
DEFAULT_MULTIPLE = 32
DEFAULT_GENERATION_TAIL_SECONDS = 0.5
CONTINUITY_MODES = {"fast", "soft_chain"}
_ASPECT_RE = re.compile(r"^\d{1,3}:\d{1,3}(?: \([^\r\n]{1,80}\))?$")


def _text(value: object, field: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{field}不合法")
    return result


def _asset_list(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field}最多 {maximum} 个")
    result = tuple(_text(item, field, maximum=2000) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field}不能重复")
    return result


@dataclass(frozen=True)
class H3Resolution:
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    megapixels: float = DEFAULT_MEGAPIXELS
    multiple: int = DEFAULT_MULTIPLE


@dataclass(frozen=True)
class H3ItemInput:
    item_id: str
    script_text: str
    reference_video: str
    full_audio: str
    raw_cues: tuple[dict[str, Any], ...]
    user_direction: str = ""
    resolution: H3Resolution | None = None


@dataclass(frozen=True)
class H3ProjectInput:
    schema_version: str
    project_id: str
    batch_identity_images: tuple[str, ...]
    continuity_mode: str
    generation_tail_seconds: float
    resolution: H3Resolution
    items: tuple[H3ItemInput, ...]


def _resolution(value: object, *, fallback: H3Resolution | None = None) -> H3Resolution:
    if value is None:
        if fallback is None:
            return H3Resolution()
        return fallback
    if not isinstance(value, dict):
        raise ValueError("H3 分辨率不合法")
    base = fallback or H3Resolution()
    aspect_ratio = str(value.get("aspect_ratio") or base.aspect_ratio).strip()
    if not _ASPECT_RE.fullmatch(aspect_ratio):
        raise ValueError("H3 画幅不合法")
    try:
        megapixels = float(value.get("megapixels", base.megapixels))
    except (TypeError, ValueError) as exc:
        raise ValueError("H3 megapixels 不合法") from exc
    if not math.isfinite(megapixels) or not 0.2 <= megapixels <= 2.0:
        raise ValueError("H3 megapixels 必须在 0.2 到 2.0 之间")
    multiple = value.get("multiple", base.multiple)
    if isinstance(multiple, bool) or multiple != 32:
        raise ValueError("H3 第一版 multiple 必须为 32")
    return H3Resolution(aspect_ratio, megapixels, 32)


def parse_project_input(value: object) -> H3ProjectInput:
    if not isinstance(value, dict):
        raise ValueError("H3 项目输入必须是 JSON 对象")
    schema_version = str(value.get("schema_version") or SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise ValueError("H3 项目 schema 版本不受支持")
    project_id = _text(value.get("project_id"), "H3 project_id", maximum=200)
    identity_images = _asset_list(
        value.get("batch_identity_images"),
        "H3 批次人物参考图",
        maximum=5,
    )
    defaults = value.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError("H3 项目默认参数不合法")
    continuity_mode = str(defaults.get("continuity_mode") or "fast").strip()
    if continuity_mode not in CONTINUITY_MODES:
        raise ValueError("H3 连续性模式不合法")
    try:
        tail = float(
            defaults.get(
                "generation_tail_seconds",
                DEFAULT_GENERATION_TAIL_SECONDS,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("H3 生成尾部余量不合法") from exc
    if not math.isfinite(tail) or not 0 <= tail <= 1:
        raise ValueError("H3 生成尾部余量必须在 0 到 1 秒之间")
    resolution = _resolution(defaults.get("resolution"))

    raw_items = value.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("H3 项目至少需要一个脚本行")
    items = []
    item_ids = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("H3 脚本行不合法")
        item_id = _text(raw_item.get("item_id"), "H3 item_id", maximum=200)
        if item_id in item_ids:
            raise ValueError("H3 item_id 不能重复")
        item_ids.add(item_id)
        raw_cues = raw_item.get("raw_cues")
        if not isinstance(raw_cues, list) or not raw_cues:
            raise ValueError(f"H3 脚本行 {item_id} 缺少 raw cues")
        items.append(
            H3ItemInput(
                item_id=item_id,
                script_text=_text(raw_item.get("script_text"), "H3 脚本", maximum=100000),
                reference_video=_text(
                    raw_item.get("reference_video"),
                    "H3 逐行参考视频",
                    maximum=2000,
                ),
                full_audio=_text(raw_item.get("full_audio"), "H3 完整音频", maximum=2000),
                raw_cues=tuple(raw_cues),
                user_direction=str(raw_item.get("user_direction") or "").strip()[:1000],
                resolution=(
                    _resolution(raw_item.get("resolution"), fallback=resolution)
                    if raw_item.get("resolution") is not None
                    else None
                ),
            )
        )
    return H3ProjectInput(
        schema_version=schema_version,
        project_id=project_id,
        batch_identity_images=identity_images,
        continuity_mode=continuity_mode,
        generation_tail_seconds=tail,
        resolution=resolution,
        items=tuple(items),
    )

