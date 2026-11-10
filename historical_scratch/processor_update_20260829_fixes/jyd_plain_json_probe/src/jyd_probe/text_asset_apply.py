from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any
import uuid


TEXT_EFFECT_SCHEMA = "jyd_probe.text_effect.v1"
TEXT_TEMPLATE_SCHEMA = "jyd_probe.text_template.v1"


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _load_asset(path_value: str | Path, schema: str) -> tuple[Path, dict[str, Any]]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文字素材 JSON 不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"文字素材 JSON 无法读取: {path}") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise RuntimeError(f"不支持的文字素材 schema: {data.get('schema')!r}")
    return path, data


def _path_key(value: str) -> str:
    return value.strip().replace("\\", "/").casefold()


def _resource_path_map(metadata_path: Path, data: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    resources = data.get("resources", [])
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            original = str(resource.get("original_path", "")).strip()
            library_path = str(resource.get("library_path", "")).strip()
            if original and library_path:
                resolved = (metadata_path.parent / library_path).resolve()
                if not resolved.exists():
                    raise FileNotFoundError(f"模板资源文件不存在: {resolved}")
                result[_path_key(original)] = str(resolved)

    resource = data.get("resource")
    if isinstance(resource, dict):
        original = str(resource.get("original_path", "")).strip()
        library_path = str(resource.get("library_path", "")).strip()
        if original and library_path:
            resolved = (metadata_path.parent / library_path).resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"花字资源文件不存在: {resolved}")
            result[_path_key(original)] = str(resolved)
    return result


def _rewrite_paths(value: Any, path_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and (key == "path" or key.endswith("_path")):
                rewritten[key] = path_map.get(_path_key(item), item)
            elif key == "content" and isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    rewritten[key] = item
                else:
                    rewritten[key] = json.dumps(
                        _rewrite_paths(parsed, path_map),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
            else:
                rewritten[key] = _rewrite_paths(item, path_map)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_paths(item, path_map) for item in value]
    return value


def _replace_ids(value: Any, id_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_ids(item, id_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, id_map) for item in value]
    if isinstance(value, str):
        return id_map.get(value, value)
    return value


def _materials(data: dict[str, Any]) -> dict[str, Any]:
    materials = data.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("草稿 materials 不是对象")
    return materials


def _material_list(materials: dict[str, Any], key: str) -> list[Any]:
    values = materials.setdefault(key, [])
    if not isinstance(values, list):
        raise RuntimeError(f"materials.{key} 不是数组")
    return values


def _text_tracks(data: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [track for track in tracks if isinstance(track, dict) and track.get("type") == "text"]


def _find_text_track(data: dict[str, Any], track_name: str) -> dict[str, Any]:
    for track in _text_tracks(data):
        if str(track.get("name", "")) == track_name:
            return track
    raise RuntimeError(f"没有找到新增文字轨道: {track_name!r}")


def _parse_text_content(material: dict[str, Any]) -> dict[str, Any]:
    content = material.get("content", "")
    if not isinstance(content, str):
        raise RuntimeError(f"文字素材 content 不是字符串: {material.get('id')!r}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"文字素材 content 不是合法 JSON: {material.get('id')!r}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"文字素材 content 顶层不是对象: {material.get('id')!r}")
    return parsed


def _scaled_range(value: Any, old_length: int, new_length: int) -> list[int]:
    if not isinstance(value, list) or len(value) != 2 or old_length <= 0:
        return [0, new_length]
    try:
        start = round(int(value[0]) / old_length * new_length)
        end = round(int(value[1]) / old_length * new_length)
    except (TypeError, ValueError):
        return [0, new_length]
    start = max(0, min(new_length, start))
    end = max(start, min(new_length, end))
    return [start, end]


def _set_text(material: dict[str, Any], text: str) -> None:
    content = _parse_text_content(material)
    old_text = str(content.get("text", ""))
    styles = content.get("styles", [])
    if not isinstance(styles, list) or not styles:
        styles = [{"range": [0, len(text)]}]
        content["styles"] = styles
    for style in styles:
        if isinstance(style, dict):
            style["range"] = _scaled_range(style.get("range"), len(old_text), len(text))
    content["text"] = text
    material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))


def _effect_style(data: dict[str, Any], effect_path: str) -> dict[str, Any]:
    sample = data.get("sample_text_material", {})
    if isinstance(sample, dict):
        try:
            content = _parse_text_content(sample)
        except RuntimeError:
            content = {}
        styles = content.get("styles", []) if isinstance(content, dict) else []
        if isinstance(styles, list):
            for style in styles:
                if isinstance(style, dict) and isinstance(style.get("effectStyle"), dict):
                    result = deepcopy(style["effectStyle"])
                    result["path"] = effect_path
                    return result
    material = data.get("material", {})
    resource_id = ""
    if isinstance(material, dict):
        resource_id = str(material.get("resource_id") or material.get("effect_id") or "")
    return {"id": resource_id, "path": effect_path}


def apply_text_effect_to_track(
    data: dict[str, Any],
    text_effect_json_path: str | Path,
    track_name: str,
) -> int:
    """Apply one collected flower-text effect to every segment on a named text track."""

    metadata_path, asset = _load_asset(text_effect_json_path, TEXT_EFFECT_SCHEMA)
    path_map = _resource_path_map(metadata_path, asset)
    material_value = asset.get("material")
    if not isinstance(material_value, dict):
        raise RuntimeError("花字素材缺少 material 对象")
    source_material = _rewrite_paths(deepcopy(material_value), path_map)
    effect_path = str(source_material.get("path", ""))
    if not effect_path:
        raise RuntimeError("花字素材没有可用的资源路径")

    materials = _materials(data)
    texts = {
        str(item.get("id")): item
        for item in _material_list(materials, "texts")
        if isinstance(item, dict) and item.get("id")
    }
    effect_materials = _material_list(materials, "effects")
    track = _find_text_track(data, track_name)
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"文字轨道 segments 不是数组: {track_name!r}")

    changed = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text_material = texts.get(str(segment.get("material_id", "")))
        if text_material is None:
            continue
        effect_material = deepcopy(source_material)
        effect_material["id"] = _new_id()
        effect_materials.append(effect_material)

        content = _parse_text_content(text_material)
        styles = content.get("styles", [])
        if not isinstance(styles, list) or not styles:
            styles = [{"range": [0, len(str(content.get('text', '')))]}]
            content["styles"] = styles
        for style in styles:
            if isinstance(style, dict):
                style["effectStyle"] = _effect_style(asset, effect_path)
        text_material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        refs = segment.setdefault("extra_material_refs", [])
        if not isinstance(refs, list):
            refs = []
            segment["extra_material_refs"] = refs
        refs.append(effect_material["id"])
        changed += 1
    if not changed:
        raise RuntimeError(f"文字轨道中没有可应用花字的普通文字片段: {track_name!r}")
    return changed


def _max_segment_number(data: dict[str, Any], key: str) -> int:
    maximum = -1
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return maximum
    for track in tracks:
        if not isinstance(track, dict):
            continue
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                maximum = max(maximum, int(segment.get(key, -1)))
            except (TypeError, ValueError):
                continue
    return maximum


def _resolve_duration(data: dict[str, Any], start_us: int, duration_us: int) -> int:
    try:
        draft_duration = int(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        draft_duration = 0
    if start_us < 0:
        raise ValueError("文字开始时间不能为负数")
    if draft_duration <= 0 or start_us >= draft_duration:
        raise ValueError(f"文字开始时间超出视频时长: start_us={start_us}, duration_us={draft_duration}")
    resolved = duration_us if duration_us > 0 else draft_duration - start_us
    if resolved <= 0 or start_us + resolved > draft_duration:
        raise ValueError(
            f"文字时间范围超出视频时长: start_us={start_us}, duration_us={resolved}, video_duration_us={draft_duration}"
        )
    return resolved


def _set_template_duration(template: dict[str, Any], duration_us: int) -> None:
    for key in ("text_info_resources", "non_text_info_resources"):
        resources = template.get(key, [])
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            attach_info = resource.get("attach_info")
            if isinstance(attach_info, dict):
                attach_info["duration"] = duration_us


def add_text_template_to_data(
    data: dict[str, Any],
    text_template_json_path: str | Path,
    texts: list[str],
    *,
    start_us: int = 0,
    duration_us: int = 0,
    track_name: str = "",
) -> int:
    """Clone one collected composite text template into a draft with fresh local IDs."""

    metadata_path, asset = _load_asset(text_template_json_path, TEXT_TEMPLATE_SCHEMA)
    path_map = _resource_path_map(metadata_path, asset)
    template_value = asset.get("template")
    segment_value = asset.get("segment_template")
    slots_value = asset.get("text_slots")
    references_value = asset.get("referenced_materials", {})
    if not isinstance(template_value, dict) or not isinstance(segment_value, dict):
        raise RuntimeError("复合文字模板缺少 template 或 segment_template")
    if not isinstance(slots_value, list) or not isinstance(references_value, dict):
        raise RuntimeError("复合文字模板缺少 text_slots 或 referenced_materials")

    template = _rewrite_paths(deepcopy(template_value), path_map)
    segment = _rewrite_paths(deepcopy(segment_value), path_map)
    slots = _rewrite_paths(deepcopy(slots_value), path_map)
    references = _rewrite_paths(deepcopy(references_value), path_map)

    id_map: dict[str, str] = {}
    for material in [template, segment]:
        if isinstance(material, dict) and material.get("id"):
            id_map.setdefault(str(material["id"]), _new_id())
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        material = slot.get("text_material")
        if isinstance(material, dict) and material.get("id"):
            id_map.setdefault(str(material["id"]), _new_id())
    for values in references.values():
        if not isinstance(values, list):
            continue
        for material in values:
            if isinstance(material, dict) and material.get("id"):
                id_map.setdefault(str(material["id"]), _new_id())

    template = _replace_ids(template, id_map)
    segment = _replace_ids(segment, id_map)
    slots = _replace_ids(slots, id_map)
    references = _replace_ids(references, id_map)

    resolved_duration = _resolve_duration(data, start_us, duration_us)
    timerange = segment.setdefault("target_timerange", {})
    if not isinstance(timerange, dict):
        timerange = {}
        segment["target_timerange"] = timerange
    timerange["start"] = start_us
    timerange["duration"] = resolved_duration
    segment["render_index"] = _max_segment_number(data, "render_index") + 1
    segment["track_render_index"] = _max_segment_number(data, "track_render_index") + 1
    _set_template_duration(template, resolved_duration)

    materials = _materials(data)
    _material_list(materials, "text_templates").append(template)
    target_texts = _material_list(materials, "texts")
    for slot_index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            continue
        material = slot.get("text_material")
        if not isinstance(material, dict):
            raise RuntimeError(f"复合文字模板第 {slot_index + 1} 个文字槽缺少文字素材")
        if slot_index < len(texts):
            _set_text(material, str(texts[slot_index]))
        target_texts.append(material)

    for category, values in references.items():
        if not isinstance(values, list):
            continue
        target = _material_list(materials, str(category))
        target.extend(item for item in values if isinstance(item, dict))

    tracks = data.setdefault("tracks", [])
    if not isinstance(tracks, list):
        raise RuntimeError("草稿 tracks 不是数组")
    tracks.append(
        {
            "attribute": 0,
            "flag": 0,
            "id": _new_id(),
            "is_default_name": False,
            "name": track_name or f"复合文字模板_{len(_text_tracks(data)) + 1}",
            "segments": [segment],
            "type": "text",
        }
    )
    return 1
