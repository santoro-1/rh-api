from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def log(message: str, level: str = "INFO") -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{level}] {message}", flush=True)


def fail(message: str, exit_code: int = 1) -> int:
    log(message, "ERROR")
    return exit_code


def import_pyjianyingdraft() -> Any:
    try:
        import pyJianYingDraft as draft
    except Exception as exc:  # pragma: no cover - environment check
        raise RuntimeError(
            "无法导入 pyJianYingDraft。请先运行: "
            "D:\\Myanaconda\\python.exe -m pip install -r requirements.txt"
        ) from exc

    try:
        version = importlib.metadata.version("pyjianyingdraft")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    log(f"pyJianYingDraft 导入成功，版本: {version}")
    return draft


def append_track_compat(
    draft: Any,
    script: Any,
    track_type: Any,
    track_name: str | None = None,
    *,
    mute: bool = False,
    relative_index: int = 0,
) -> Any:
    """Create a track with either the pyJianYingDraft 0.2 or 0.3 API."""
    append_track = getattr(script, "append_track", None)
    track_spec = getattr(draft, "TrackSpec", None)
    if callable(append_track) and track_spec is not None:
        # 0.3 removed relative_index. New content in this project is intentionally
        # appended above imported tracks; exact template ordering remains untouched.
        return append_track(track_spec(track_type, track_name or None, mute=mute))

    add_track = getattr(script, "add_track", None)
    if not callable(add_track):
        raise RuntimeError("当前 pyJianYingDraft 不支持已知的轨道创建接口")
    return add_track(
        track_type,
        track_name or None,
        mute=mute,
        relative_index=relative_index,
    )


def load_plain_draft_json(draft_dir: Path) -> dict[str, Any]:
    draft_content_path = draft_dir / "draft_content.json"
    if not draft_content_path.exists():
        raise FileNotFoundError(f"找不到 {draft_content_path}")

    try:
        with draft_content_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"{draft_content_path} 不是 UTF-8 明文 JSON；第一阶段只支持剪映 5.9 及以下明文草稿"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{draft_content_path} 不是合法明文 JSON；第一阶段不处理高版本加密/特殊格式草稿"
        ) from exc

    log(f"明文 draft_content.json 读取成功: {draft_content_path}")
    return data


def save_plain_draft_json(draft_dir: Path, data: dict[str, Any]) -> None:
    draft_content_path = draft_dir / "draft_content.json"
    with draft_content_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    log(f"明文 draft_content.json 写入成功: {draft_content_path}")


def new_json_id() -> str:
    return str(uuid.uuid4()).upper()


def safe_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def summarize_draft_json(data: dict[str, Any], label: str) -> dict[str, Any]:
    log(f"开始统计草稿结构: {label}")

    tracks = data.get("tracks", [])
    materials = data.get("materials", {})
    if not isinstance(tracks, list):
        tracks = []
    if not isinstance(materials, dict):
        materials = {}

    track_counter = Counter(track.get("type", "<unknown>") for track in tracks if isinstance(track, dict))
    segment_counter: Counter[str] = Counter()
    for track in tracks:
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type", "<unknown>"))
        segment_counter[track_type] += safe_count(track.get("segments", []))

    log(f"轨道总数: {len(tracks)}")
    for track_type, count in sorted(track_counter.items()):
        log(f"轨道类型 {track_type}: {count} 条，片段 {segment_counter[track_type]} 个")

    material_keys = sorted(materials.keys())
    log(f"materials 分类数: {len(material_keys)}")
    for key in material_keys:
        value = materials.get(key)
        if isinstance(value, list) and key in {
            "videos",
            "audios",
            "texts",
            "video_effects",
            "audio_effects",
            "effects",
            "stickers",
            "transitions",
            "material_animations",
        }:
            log(f"materials.{key}: {len(value)} 个")

    log_track_details(tracks)
    log_material_samples(materials)

    return {
        "track_count": len(tracks),
        "track_counter": track_counter,
        "segment_counter": segment_counter,
        "materials": materials,
    }


def log_track_details(tracks: list[Any]) -> None:
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            continue
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            segments = []
        track_type = track.get("type", "<unknown>")
        track_name = track.get("name", "")
        log(f"轨道[{index}] type={track_type}, name={track_name!r}, segments={len(segments)}")

        if index >= 9:
            remaining = len(tracks) - 10
            if remaining > 0:
                log(f"轨道明细只展示前 10 条，剩余 {remaining} 条略过")
            break


def log_material_samples(materials: dict[str, Any]) -> None:
    videos = materials.get("videos", [])
    audios = materials.get("audios", [])
    texts = materials.get("texts", [])
    video_effects = materials.get("video_effects", [])

    if isinstance(videos, list) and videos:
        first = videos[0]
        if isinstance(first, dict):
            log(
                "首个视频素材: "
                f"name={first.get('material_name')!r}, path={first.get('path')!r}, "
                f"duration={first.get('duration')}"
            )
    if isinstance(audios, list) and audios:
        first = audios[0]
        if isinstance(first, dict):
            log(
                "首个音频素材: "
                f"name={first.get('name')!r}, path={first.get('path')!r}, "
                f"duration={first.get('duration')}"
            )
    if isinstance(texts, list) and texts:
        first_text = extract_text_preview(texts[0])
        log(f"首个文本素材内容预览: {first_text!r}")
    if isinstance(video_effects, list):
        log(f"视频特效素材 materials.video_effects 数量: {len(video_effects)}")
        if video_effects and isinstance(video_effects[0], dict):
            sample = video_effects[0]
            log(
                "首个视频特效素材: "
                f"name={sample.get('name')!r}, type={sample.get('type')!r}, "
                f"resource_id={sample.get('resource_id')!r}, id={sample.get('id')!r}"
            )


def video_effect_materials(data: dict[str, Any]) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return []
    video_effects = materials.get("video_effects", [])
    if not isinstance(video_effects, list):
        return []
    return [item for item in video_effects if isinstance(item, dict)]


def material_index_by_id(materials: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, material in enumerate(materials):
        material_id = material.get("id")
        if material_id:
            result[str(material_id)] = index
    return result


def effect_tracks(data: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == "effect"
    ]


def video_tracks(data: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == "video"
    ]


def audio_tracks(data: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == "audio"
    ]


def text_tracks(data: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []

    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    text_material_ids = {
        str(material.get("id") or "")
        for collection in ("texts", "text_templates")
        for material in (
            materials.get(collection, [])
            if isinstance(materials.get(collection), list)
            else []
        )
        if isinstance(material, dict) and material.get("id")
    }

    def is_text_bearing_track(track: dict[str, Any]) -> bool:
        if track.get("type") == "text":
            return True
        segments = track.get("segments", [])
        return isinstance(segments, list) and any(
            isinstance(segment, dict)
            and str(segment.get("material_id") or "") in text_material_ids
            for segment in segments
        )

    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and is_text_bearing_track(track)
    ]


def nested_draft_refs(data: dict[str, Any]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return []
    drafts = materials.get("drafts", [])
    if not isinstance(drafts, list):
        return []

    refs: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(drafts):
        if not isinstance(item, dict):
            continue
        nested = item.get("draft")
        if isinstance(nested, dict):
            refs.append((index, item, nested))
    return refs


def material_by_id(materials: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for material in materials:
        if not isinstance(material, dict):
            continue
        material_id = material.get("id")
        if material_id:
            result[str(material_id)] = material
    return result


def video_material_label(material: dict[str, Any] | None) -> str:
    if material is None:
        return "<missing>"
    fields = [
        ("name", material.get("material_name")),
        ("type", material.get("type")),
        ("path", material.get("path")),
        ("duration", material.get("duration")),
        ("id", material.get("id")),
    ]
    return ", ".join(f"{key}={value!r}" for key, value in fields if value not in (None, ""))


def log_nested_draft_details(data: dict[str, Any], label: str, limit: int = 30) -> None:
    refs = nested_draft_refs(data)
    log(f"开始统计嵌套模板结构: {label}")
    log(f"materials.drafts 可解析嵌套草稿数量: {len(refs)}")
    if not refs:
        return

    shown_segments = 0
    for draft_index, draft_material, nested in refs:
        materials = nested.get("materials", {})
        tracks = nested.get("tracks", [])
        if not isinstance(materials, dict):
            materials = {}
        if not isinstance(tracks, list):
            tracks = []

        log(
            "嵌套草稿"
            f"[{draft_index}]: name={draft_material.get('name', '')!r}, "
            f"duration={nested.get('duration')}, tracks={len(tracks)}, materials={len(materials)}"
        )

        for key in ["videos", "audios", "texts", "video_effects", "effects", "material_animations", "canvases"]:
            value = materials.get(key)
            if isinstance(value, list):
                log(f"  nested materials.{key}: {len(value)} 个")

        videos = materials.get("videos", [])
        if not isinstance(videos, list):
            videos = []
        videos_by_id = material_by_id(videos)
        for material_index, material in enumerate(videos[:limit]):
            if isinstance(material, dict):
                log(f"  nested videos[{material_index}]: {video_material_label(material)}")
        if len(videos) > limit:
            log(f"  nested videos 只展示前 {limit} 个，剩余 {len(videos) - limit} 个略过")

        for video_track_index, (raw_track_index, track) in enumerate(video_tracks(nested)):
            segments = track.get("segments", [])
            if not isinstance(segments, list):
                segments = []
            log(
                "  nested video track"
                f"[{video_track_index}] raw_track_index={raw_track_index}, "
                f"name={track.get('name', '')!r}, segments={len(segments)}"
            )
            for segment_index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    continue
                material = videos_by_id.get(str(segment.get("material_id", "")))
                log(
                    "    nested video segment"
                    f"[track={video_track_index}, segment={segment_index}] "
                    f"material_id={segment.get('material_id')!r}, "
                    f"target={segment.get('target_timerange')}, "
                    f"source={segment.get('source_timerange')}, "
                    f"material=({video_material_label(material)})"
                )
                shown_segments += 1
                if shown_segments >= limit:
                    log(f"  nested video segment 只展示前 {limit} 个")
                    return


def max_segment_render_index(data: dict[str, Any], default: int = 10000) -> int:
    max_index = default
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return max_index
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
                max_index = max(max_index, int(segment.get("render_index", default)))
            except (TypeError, ValueError):
                continue
    return max_index


def build_timerange(draft: Any, start_us: int, duration_us: int) -> Any | None:
    if start_us < 0 and duration_us <= 0:
        return None
    if start_us < 0:
        start_us = 0
    if duration_us <= 0:
        raise RuntimeError("指定 source start 时必须同时指定正数 source duration")
    return draft.Timerange(start_us, duration_us)


def apply_segment_time_override(segment: Any, start_us: int, duration_us: int, label: str) -> bool:
    changed = False
    if start_us >= 0:
        old_start = segment.start
        segment.start = start_us
        log(f"已修改 {label} 开始时间: {old_start} -> {start_us} 微秒")
        changed = True
    if duration_us > 0:
        old_duration = segment.duration
        segment.duration = duration_us
        log(f"已修改 {label} 持续时长: {old_duration} -> {duration_us} 微秒")
        changed = True
    return changed


def apply_json_timerange_override(timerange: dict[str, Any], start_us: int, duration_us: int, label: str) -> bool:
    changed = False
    if start_us >= 0:
        old_start = timerange.get("start")
        timerange["start"] = start_us
        log(f"已修改 {label} 开始时间: {old_start} -> {start_us} 微秒")
        changed = True
    if duration_us > 0:
        old_duration = timerange.get("duration")
        timerange["duration"] = duration_us
        log(f"已修改 {label} 持续时长: {old_duration} -> {duration_us} 微秒")
        changed = True
    return changed


def effect_material_label(material: dict[str, Any] | None) -> str:
    if material is None:
        return "<missing>"
    fields = [
        ("name", material.get("name")),
        ("type", material.get("type")),
        ("effect_id", material.get("effect_id")),
        ("resource_id", material.get("resource_id")),
        ("id", material.get("id")),
    ]
    return ", ".join(f"{key}={value!r}" for key, value in fields if value not in (None, ""))


def log_effect_details(data: dict[str, Any], label: str, limit: int = 20) -> None:
    effects = video_effect_materials(data)
    effects_by_id = material_index_by_id(effects)
    tracks = effect_tracks(data)

    log(f"开始统计特效明细: {label}")
    log(f"materials.video_effects: {len(effects)} 个")
    for index, material in enumerate(effects[:limit]):
        log(f"video_effects[{index}]: {effect_material_label(material)}")
    if len(effects) > limit:
        log(f"video_effects 明细只展示前 {limit} 个，剩余 {len(effects) - limit} 个略过")

    log(f"tracks[type='effect']: {len(tracks)} 条")
    shown_segments = 0
    for track_index, track in tracks:
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            segments = []
        log(f"effect track[{track_index}] name={track.get('name', '')!r}, segments={len(segments)}")
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material_id = str(segment.get("material_id", ""))
            material = effects[effects_by_id[material_id]] if material_id in effects_by_id else None
            trange = segment.get("target_timerange", {})
            log(
                "  effect segment"
                f"[track={track_index}, segment={segment_index}] "
                f"material_id={material_id!r}, target_timerange={trange}, "
                f"material=({effect_material_label(material)})"
            )
            shown_segments += 1
            if shown_segments >= limit:
                log(f"effect segment 明细只展示前 {limit} 个")
                return


def find_first_effect_ref(data: dict[str, Any]) -> dict[str, Any]:
    effects = video_effect_materials(data)
    effects_by_id = material_index_by_id(effects)

    for track_index, track in effect_tracks(data):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material_id = segment.get("material_id")
            if not material_id:
                continue
            material_index = effects_by_id.get(str(material_id))
            return {
                "track_index": track_index,
                "segment_index": segment_index,
                "segment": segment,
                "material_id": str(material_id),
                "material_index": material_index,
                "material": effects[material_index] if material_index is not None else None,
            }

    if effects:
        return {
            "track_index": None,
            "segment_index": None,
            "segment": None,
            "material_id": str(effects[0].get("id", "")),
            "material_index": 0,
            "material": effects[0],
        }

    raise RuntimeError("没有找到任何特效片段或 materials.video_effects；请先在草稿里添加一个视频特效")


def export_first_effect_json(data: dict[str, Any], output_path: Path) -> None:
    effect_ref = find_first_effect_ref(data)
    material = effect_ref["material"]
    segment = effect_ref["segment"]
    if not isinstance(material, dict) or not isinstance(segment, dict):
        raise RuntimeError("导出特效库需要来源草稿同时存在 effect segment 和 materials.video_effects")

    library_data = {
        "schema": "jyd_probe.video_effect.v1",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "effect_label": effect_material_label(material),
        "material": deepcopy(material),
        "segment_template": deepcopy(segment),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"特效库 JSON 已存在，为避免覆盖已停止: {output_path}")
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(library_data, f, ensure_ascii=False, indent=4)
    log(f"已导出第一个视频特效到 JSON: {output_path}")
    log(f"导出特效: {effect_material_label(material)}")


def load_effect_json(effect_json_path: Path) -> dict[str, Any]:
    if not effect_json_path.exists():
        raise FileNotFoundError(f"特效 JSON 不存在: {effect_json_path}")
    with effect_json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise RuntimeError(f"特效 JSON 顶层必须是对象: {effect_json_path}")
    if data.get("schema") != "jyd_probe.video_effect.v1":
        raise RuntimeError(f"不支持的特效 JSON schema: {data.get('schema')!r}")
    if not isinstance(data.get("material"), dict):
        raise RuntimeError("特效 JSON 缺少 material 对象")
    if not isinstance(data.get("segment_template"), dict):
        raise RuntimeError("特效 JSON 缺少 segment_template 对象")

    log(f"已读取特效 JSON: {effect_json_path}")
    log(f"特效 JSON 内容: {effect_material_label(data['material'])}")
    return data


def find_video_segment_ref(data: dict[str, Any], track_index: int, segment_index: int) -> dict[str, Any]:
    tracks = video_tracks(data)
    if not tracks:
        raise RuntimeError("目标草稿没有视频轨道")
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"视频轨道下标越界: {track_index}，可用范围 [0, {len(tracks)})")

    raw_track_index, track = tracks[track_index]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"视频轨道 {track_index} 的 segments 不是列表")
    if not 0 <= segment_index < len(segments):
        raise IndexError(f"视频片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError(f"视频轨道 {track_index} 的片段 {segment_index} 不是对象")
    target_timerange = segment.get("target_timerange")
    if not isinstance(target_timerange, dict):
        raise RuntimeError(f"视频轨道 {track_index} 的片段 {segment_index} 缺少 target_timerange")

    return {
        "raw_track_index": raw_track_index,
        "track": track,
        "segment": segment,
        "target_timerange": deepcopy(target_timerange),
    }


def append_new_effect_track(data: dict[str, Any], segment: dict[str, Any]) -> None:
    tracks = data.setdefault("tracks", [])
    if not isinstance(tracks, list):
        raise RuntimeError("目标草稿 tracks 不是列表，无法添加特效轨道")

    track = {
        "attribute": 0,
        "flag": 0,
        "id": new_json_id(),
        "is_default_name": True,
        "name": "",
        "segments": [segment],
        "type": "effect",
    }
    tracks.append(track)
    log(f"已新增 effect 轨道: id={track['id']}, segments=1")


def add_effect_json_to_video(
    target_data: dict[str, Any],
    effect_json: dict[str, Any],
    video_track_index: int,
    video_segment_index: int,
    effect_start_us: int,
    effect_duration_us: int,
) -> None:
    video_ref = find_video_segment_ref(target_data, video_track_index, video_segment_index)
    target_timerange = video_ref["target_timerange"]
    apply_json_timerange_override(target_timerange, effect_start_us, effect_duration_us, "新增特效片段")

    new_effect_material_id = new_json_id()
    material = deepcopy(effect_json["material"])
    old_material_id = material.get("id")
    material["id"] = new_effect_material_id
    if "material_id" in material:
        material["material_id"] = new_effect_material_id

    materials = target_data.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("目标草稿 materials 不是对象，无法添加特效素材")
    video_effects = materials.setdefault("video_effects", [])
    if not isinstance(video_effects, list):
        raise RuntimeError("目标草稿 materials.video_effects 不是列表，无法添加特效素材")
    video_effects.append(material)

    segment = deepcopy(effect_json["segment_template"])
    segment["id"] = new_json_id()
    segment["material_id"] = new_effect_material_id
    segment["target_timerange"] = target_timerange

    render_index = segment.get("render_index")
    if render_index in (None, ""):
        render_index = max_segment_render_index(target_data, 10000) + 1
    segment["render_index"] = int(render_index)
    if "track_render_index" in segment:
        segment["track_render_index"] = int(render_index)

    append_new_effect_track(target_data, segment)
    log(
        "已把特效 JSON 添加到目标视频片段: "
        f"video_track_index={video_track_index}, video_segment_index={video_segment_index}, "
        f"target_timerange={target_timerange}, old_effect_id={old_material_id!r}, "
        f"new_effect_id={new_effect_material_id!r}, effect=({effect_material_label(material)})"
    )


def replace_first_effect_from_source(target_data: dict[str, Any], source_data: dict[str, Any]) -> None:
    target_ref = find_first_effect_ref(target_data)
    source_ref = find_first_effect_ref(source_data)
    source_material = source_ref["material"]
    if not isinstance(source_material, dict):
        raise RuntimeError("来源草稿的第一个特效片段没有找到对应的 materials.video_effects 素材")

    target_material_id = target_ref["material_id"]
    if not target_material_id:
        raise RuntimeError("目标草稿的第一个特效没有 material_id，无法做占位替换")

    target_materials = video_effect_materials(target_data)
    target_material_index = target_ref["material_index"]
    replacement = deepcopy(source_material)
    old_source_id = replacement.get("id")
    replacement["id"] = target_material_id
    if "material_id" in replacement:
        replacement["material_id"] = target_material_id

    if target_material_index is None:
        raw_materials = target_data.setdefault("materials", {}).setdefault("video_effects", [])
        if not isinstance(raw_materials, list):
            raise RuntimeError("目标草稿 materials.video_effects 不是列表，无法写入")
        raw_materials.append(replacement)
    else:
        raw_materials = target_data["materials"]["video_effects"]
        raw_materials[target_material_index] = replacement

    log(
        "已用来源草稿第一个特效替换目标草稿第一个特效占位: "
        f"source_id={old_source_id!r}, target_kept_id={target_material_id!r}, "
        f"source=({effect_material_label(source_material)})"
    )


def extract_text_preview(material: Any) -> str:
    if not isinstance(material, dict):
        return ""
    content = material.get("content", "")
    if not isinstance(content, str):
        return str(content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content[:80]
    if isinstance(parsed, dict):
        text = parsed.get("text", "")
        return str(text)[:80]
    return str(parsed)[:80]


def first_material_name(materials: dict[str, Any], media_type: str) -> str | None:
    if media_type == "video":
        items = materials.get("videos", [])
        name_key = "material_name"
    elif media_type == "audio":
        items = materials.get("audios", [])
        name_key = "name"
    else:
        raise ValueError(f"未知素材类型: {media_type}")

    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get(name_key):
            return str(item[name_key])
    return None


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def copy_template_draft(template_dir: Path, output_root: Path, output_name: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    output_dir = (output_root / output_name).resolve()
    template_dir = template_dir.resolve()

    if output_dir == template_dir:
        raise RuntimeError("输出草稿目录不能等于原模板目录")
    if is_relative_to(output_dir, template_dir):
        raise RuntimeError("输出草稿目录不能放在原模板草稿目录内部")
    if output_dir.exists():
        raise FileExistsError(f"输出草稿目录已存在，为避免覆盖已停止: {output_dir}")

    shutil.copytree(template_dir, output_dir)
    _normalize_copied_draft_metadata(output_dir, output_name)
    log(f"模板草稿已复制到新的输出目录: {output_dir}")
    return output_dir


def _normalize_copied_draft_metadata(output_dir: Path, output_name: str) -> None:
    """Give a copied draft its own Jianying identity and current catalogue metadata.

    Collector imports keep ``draft_meta_info.json`` as plain JSON.  Copying that
    file verbatim leaves the old name, folder path, id and modification time in
    the new directory.  Jianying may then hide the copy behind the source draft
    or place it outside the visible recent list, even though ``draft_content``
    is valid.  Normalise only plain metadata; encrypted metadata is left for
    Jianying itself to manage.
    """

    meta_path = output_dir / "draft_meta_info.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log("输出草稿元数据不是明文 JSON，跳过名称和草稿 ID 重建", "WARN")
        return
    if not isinstance(meta, dict):
        log("输出草稿元数据顶层不是对象，跳过名称和草稿 ID 重建", "WARN")
        return

    now_us = int(datetime.now().timestamp() * 1_000_000)
    root_path = str(output_dir.parent).replace("\\", "/")
    fold_path = str(output_dir).replace("\\", "/")
    meta.update(
        {
            "draft_name": output_name,
            "draft_id": str(uuid.uuid4()).upper(),
            "draft_root_path": root_path,
            "draft_fold_path": fold_path,
            "tm_draft_create": now_us,
            "tm_draft_modified": now_us,
            "tm_draft_removed": 0,
            "draft_is_invisible": False,
        }
    )
    anchor = output_dir.anchor.rstrip("\\/")
    if anchor:
        meta["draft_removable_storage_device"] = anchor
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    # This is a decrypted diagnostic copy created by the import workflow.  It
    # still describes the source draft and can conflict with the canonical
    # draft_meta_info.json when the copied folder is scanned by Jianying.
    stale_decrypted_meta = output_dir / "draft_meta.dec.json"
    if stale_decrypted_meta.is_file():
        stale_decrypted_meta.unlink()
    log(
        f"已重建输出草稿元数据: name={output_name!r}, "
        f"draft_id={meta['draft_id']}, fold_path={fold_path}"
    )


def validate_template_with_pyjyd(draft: Any, template_dir: Path) -> None:
    draft_root = template_dir.parent
    draft_name = template_dir.name
    folder = draft.DraftFolder(str(draft_root))
    script = folder.load_template(draft_name)
    imported_tracks = getattr(script, "imported_tracks", [])
    log(f"pyJianYingDraft 成功加载原模板: imported_tracks={len(imported_tracks)}")


def load_output_script(draft: Any, output_root: Path, output_name: str) -> Any:
    folder = draft.DraftFolder(str(output_root))
    script = folder.load_template(output_name)
    log(f"pyJianYingDraft 成功加载输出草稿副本: {output_name}")
    return script


def replace_first_text(draft: Any, script: Any, new_text: str) -> bool:
    try:
        text_track = script.get_imported_track(draft.TrackType.text, index=0)
    except Exception as exc:
        log(f"未找到可替换的文本轨道，跳过文本替换: {exc}", "WARN")
        return False

    if len(text_track) == 0:
        log("文本轨道存在但没有片段，跳过文本替换", "WARN")
        return False

    script.replace_text(text_track, 0, new_text)
    log(f"已替换第 1 条文本片段内容: {new_text!r}")
    return True


def replace_video_by_name(draft: Any, script: Any, material_name: str, media_path: Path) -> bool:
    if not media_path.exists():
        raise FileNotFoundError(f"替换视频素材不存在: {media_path}")

    material = draft.VideoMaterial(str(media_path))
    script.replace_material_by_name(material_name, material)
    log(f"已按素材名替换视频素材: {material_name!r} -> {media_path}")
    return True


def replace_audio_by_name(draft: Any, script: Any, material_name: str, media_path: Path) -> bool:
    if not media_path.exists():
        raise FileNotFoundError(f"替换音频素材不存在: {media_path}")

    material = draft.AudioMaterial(str(media_path))
    script.replace_material_by_name(material_name, material)
    log(f"已按素材名替换音频素材: {material_name!r} -> {media_path}")
    return True


def replace_text_by_index(draft: Any, script: Any, args: argparse.Namespace) -> bool:
    changed = False
    if not args.replace_text and args.text_start_us < 0 and args.text_duration_us <= 0:
        return False

    try:
        text_track = script.get_imported_track(draft.TrackType.text, index=args.target_text_track_index)
    except Exception as exc:
        log(f"未找到可修改的文本轨道，跳过文本片段操作: {exc}", "WARN")
        return False
    if not 0 <= args.target_text_segment_index < len(text_track):
        raise IndexError(
            f"文本片段下标越界: {args.target_text_segment_index}，"
            f"可用范围 [0, {len(text_track)})"
        )

    if args.replace_text:
        script.replace_text(text_track, args.target_text_segment_index, args.replace_text)
        log(
            "已替换指定文本片段内容: "
            f"text_track_index={args.target_text_track_index}, "
            f"text_segment_index={args.target_text_segment_index}, text={args.replace_text!r}"
        )
        changed = True

    changed = apply_segment_time_override(
        text_track.segments[args.target_text_segment_index],
        args.text_start_us,
        args.text_duration_us,
        "指定文本片段",
    ) or changed
    return changed


def replace_video_segment_by_index(draft: Any, script: Any, args: argparse.Namespace) -> bool:
    if not args.replace_video_segment_path:
        return False
    media_path = Path(args.replace_video_segment_path).resolve()
    if not media_path.exists():
        raise FileNotFoundError(f"替换视频素材不存在: {media_path}")

    video_track = script.get_imported_track(draft.TrackType.video, index=args.target_video_track_index)
    if not 0 <= args.target_video_segment_index < len(video_track):
        raise IndexError(
            f"视频片段下标越界: {args.target_video_segment_index}，"
            f"可用范围 [0, {len(video_track)})"
        )

    source_timerange = build_timerange(draft, args.video_source_start_us, args.video_source_duration_us)
    material = draft.VideoMaterial(str(media_path))
    script.replace_material_by_seg(
        video_track,
        args.target_video_segment_index,
        material,
        source_timerange=source_timerange,
    )
    apply_segment_time_override(
        video_track.segments[args.target_video_segment_index],
        args.video_target_start_us,
        args.video_target_duration_us,
        "指定视频片段",
    )
    log(
        "已按轨道/片段替换视频素材: "
        f"video_track_index={args.target_video_track_index}, "
        f"video_segment_index={args.target_video_segment_index}, path={media_path}"
    )
    return True


def replace_audio_segment_by_index(draft: Any, script: Any, args: argparse.Namespace) -> bool:
    if not args.replace_audio_segment_path:
        return False
    media_path = Path(args.replace_audio_segment_path).resolve()
    if not media_path.exists():
        raise FileNotFoundError(f"替换音频素材不存在: {media_path}")

    audio_track = script.get_imported_track(draft.TrackType.audio, index=args.target_audio_track_index)
    if not 0 <= args.target_audio_segment_index < len(audio_track):
        raise IndexError(
            f"音频片段下标越界: {args.target_audio_segment_index}，"
            f"可用范围 [0, {len(audio_track)})"
        )

    source_timerange = build_timerange(draft, args.audio_source_start_us, args.audio_source_duration_us)
    material = draft.AudioMaterial(str(media_path))
    script.replace_material_by_seg(
        audio_track,
        args.target_audio_segment_index,
        material,
        source_timerange=source_timerange,
    )
    apply_segment_time_override(
        audio_track.segments[args.target_audio_segment_index],
        args.audio_target_start_us,
        args.audio_target_duration_us,
        "指定音频片段",
    )
    log(
        "已按轨道/片段替换音频素材: "
        f"audio_track_index={args.target_audio_track_index}, "
        f"audio_segment_index={args.target_audio_segment_index}, path={media_path}"
    )
    return True


def add_audio_track_segment(draft: Any, script: Any, args: argparse.Namespace) -> bool:
    if not args.add_audio_path:
        return False
    media_path = Path(args.add_audio_path).resolve()
    if not media_path.exists():
        raise FileNotFoundError(f"新增音频素材不存在: {media_path}")

    material = draft.AudioMaterial(str(media_path))
    source_timerange = build_timerange(draft, args.audio_source_start_us, args.audio_source_duration_us)
    target_start = args.audio_target_start_us if args.audio_target_start_us >= 0 else 0
    if args.audio_target_duration_us > 0:
        target_duration = args.audio_target_duration_us
    elif source_timerange is not None:
        target_duration = source_timerange.duration
    else:
        target_duration = material.duration

    source_start = source_timerange.start if source_timerange is not None else 0
    available_duration = max(0, int(material.duration) - int(source_start))
    if available_duration <= 0:
        raise RuntimeError(
            f"音频截取开始时间超出素材时长: source_start={source_start}, material_duration={material.duration}"
        )
    loop_to_target = bool(getattr(args, "audio_loop_to_target", False))
    if target_duration > available_duration and not loop_to_target:
        log(
            "BGM 目标时长超过素材可用时长，已截断到素材末尾: "
            f"target_duration={target_duration} -> {available_duration}",
            "WARN",
        )
        target_duration = available_duration
        source_timerange = draft.Timerange(source_start, available_duration)

    loop_duration = available_duration
    if source_timerange is not None:
        loop_duration = min(loop_duration, int(source_timerange.duration))
    if loop_duration <= 0:
        raise RuntimeError("音频循环片段时长必须大于 0")

    align_to_end = bool(getattr(args, "audio_align_to_end", False))
    crossfade_us = max(0, int(getattr(args, "audio_crossfade_us", 0) or 0))
    requested_fade_in_us = max(
        0, int(getattr(args, "audio_fade_in_us", 0) or 0)
    )
    if align_to_end and loop_to_target and target_duration > 0:
        # Lay complete musical phrases backwards from the video end.  When the
        # song is shorter than the video, the first timeline piece is the song's
        # suffix and the last piece is always a complete 0..end playback.
        crossfade_us = min(crossfade_us, loop_duration // 4)
        stride = max(1, loop_duration - crossfade_us)
        relative_start = target_duration - loop_duration
        planned: list[tuple[int, int, int]] = []
        while True:
            trimmed_source = max(0, -relative_start)
            segment_start = max(0, relative_start)
            segment_duration = loop_duration - trimmed_source
            if segment_duration > 0:
                planned.append(
                    (
                        target_start + segment_start,
                        source_start + trimmed_source,
                        segment_duration,
                    )
                )
            if relative_start <= 0:
                break
            relative_start -= stride
        planned.sort(key=lambda item: item[0])

        track_names = [
            f"probe_audio_{uuid.uuid4().hex[:8]}_a",
            f"probe_audio_{uuid.uuid4().hex[:8]}_b",
        ]
        for track_name in track_names[: 2 if len(planned) > 1 else 1]:
            append_track_compat(draft, script, draft.TrackType.audio, track_name)
        for index, (segment_start, segment_source_start, segment_duration) in enumerate(planned):
            audio_segment = draft.AudioSegment(
                material,
                draft.Timerange(segment_start, segment_duration),
                source_timerange=draft.Timerange(
                    segment_source_start,
                    segment_duration,
                ),
                volume=float(getattr(args, "audio_volume", 1.0)),
            )
            fade_in = (
                min(crossfade_us, segment_duration // 2)
                if index > 0
                else min(requested_fade_in_us, segment_duration)
            )
            fade_out = (
                min(crossfade_us, segment_duration // 2)
                if index < len(planned) - 1
                else 0
            )
            if (fade_in or fade_out) and hasattr(audio_segment, "add_fade"):
                audio_segment.add_fade(fade_in, fade_out)
            script.add_segment(audio_segment, track_names[index % len(track_names)])
        log(
            "已从自然结尾反向铺设 BGM: "
            f"target_start={target_start}, target_duration={target_duration}, "
            f"crossfade_us={crossfade_us}, segment_count={len(planned)}"
        )
        return True

    track_name = f"probe_audio_{uuid.uuid4().hex[:8]}"
    append_track_compat(draft, script, draft.TrackType.audio, track_name)

    segment_count = 0
    elapsed = 0
    while elapsed < target_duration:
        segment_duration = min(loop_duration, target_duration - elapsed)
        segment_source_timerange = source_timerange
        if loop_to_target or segment_duration != loop_duration:
            segment_source_timerange = draft.Timerange(source_start, segment_duration)
        audio_segment = draft.AudioSegment(
            material,
            draft.Timerange(target_start + elapsed, segment_duration),
            source_timerange=segment_source_timerange,
            volume=float(getattr(args, "audio_volume", 1.0)),
        )
        if elapsed == 0 and requested_fade_in_us > 0 and hasattr(audio_segment, "add_fade"):
            audio_segment.add_fade(
                min(requested_fade_in_us, segment_duration),
                0,
            )
        script.add_segment(audio_segment, track_name)
        segment_count += 1
        elapsed += segment_duration
        if not loop_to_target:
            break
    log(
        "已新增音乐轨道和音频片段: "
        f"track_name={track_name!r}, path={media_path}, "
        f"target_start={target_start}, target_duration={target_duration}, "
        f"loop_to_target={loop_to_target}, segment_count={segment_count}"
    )
    return True


def add_text_track_segment(draft: Any, script: Any, args: argparse.Namespace) -> bool:
    if not args.add_text:
        return False
    if args.text_duration_us <= 0:
        raise ValueError(f"新增文字 duration 必须大于 0: {args.text_duration_us}")
    if args.text_start_us < 0:
        raise ValueError(f"新增文字 start 不能小于 0: {args.text_start_us}")

    track_name = args.text_track_name.strip()
    if not track_name:
        track_name = f"probe_text_{uuid.uuid4().hex[:8]}"

    append_track_compat(
        draft,
        script,
        draft.TrackType.text,
        track_name,
        relative_index=args.text_relative_index,
    )
    text_segment = draft.TextSegment(
        args.add_text,
        draft.Timerange(args.text_start_us, args.text_duration_us),
        style=draft.TextStyle(
            size=args.text_size,
            align=args.text_align,
            auto_wrapping=args.text_auto_wrapping,
            max_line_width=float(getattr(args, "text_line_max_width", None) or 0.82),
        ),
        clip_settings=draft.ClipSettings(
            scale_x=float(getattr(args, "text_scale", 1.0)),
            scale_y=float(getattr(args, "text_scale", 1.0)),
            transform_x=args.text_transform_x,
            transform_y=args.text_transform_y,
        ),
    )
    script.add_segment(text_segment, track_name)
    log(
        "已新增文字轨道和文字片段: "
        f"track_name={track_name!r}, text={args.add_text!r}, "
        f"start={args.text_start_us}, duration={args.text_duration_us}"
    )
    return True


def find_nested_video_segment_ref(
    data: dict[str, Any],
    nested_draft_index: int,
    video_track_index: int,
    segment_index: int,
) -> dict[str, Any]:
    refs = nested_draft_refs(data)
    if not refs:
        raise RuntimeError("当前草稿没有可解析的嵌套模板 materials.drafts[*].draft")
    if not 0 <= nested_draft_index < len(refs):
        raise IndexError(f"嵌套草稿下标越界: {nested_draft_index}，可用范围 [0, {len(refs)})")

    raw_draft_index, draft_material, nested = refs[nested_draft_index]
    tracks = video_tracks(nested)
    if not tracks:
        raise RuntimeError(f"嵌套草稿 {nested_draft_index} 里没有视频轨道")
    if not 0 <= video_track_index < len(tracks):
        raise IndexError(f"嵌套视频轨道下标越界: {video_track_index}，可用范围 [0, {len(tracks)})")

    raw_track_index, track = tracks[video_track_index]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"嵌套视频轨道 {video_track_index} 的 segments 不是列表")
    if not 0 <= segment_index < len(segments):
        raise IndexError(f"嵌套视频片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError(f"嵌套视频轨道 {video_track_index} 的片段 {segment_index} 不是对象")

    materials = nested.get("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError(f"嵌套草稿 {nested_draft_index} 的 materials 不是对象")
    videos = materials.get("videos", [])
    if not isinstance(videos, list):
        raise RuntimeError(f"嵌套草稿 {nested_draft_index} 的 materials.videos 不是列表")

    material_id = str(segment.get("material_id", ""))
    target_material = material_by_id(videos).get(material_id)
    if target_material is None:
        raise RuntimeError(f"嵌套视频片段引用的素材不存在: material_id={material_id!r}")

    return {
        "raw_draft_index": raw_draft_index,
        "draft_material": draft_material,
        "nested": nested,
        "raw_track_index": raw_track_index,
        "track": track,
        "segment": segment,
        "material": target_material,
    }


def update_nested_video_material_fields(
    draft: Any,
    target_material: dict[str, Any],
    media_path: Path,
) -> dict[str, Any]:
    old_material = deepcopy(target_material)
    new_material = draft.VideoMaterial(str(media_path)).export_json()

    target_material.update(
        {
            "material_name": new_material["material_name"],
            "path": new_material["path"],
            "media_path": new_material.get("media_path", ""),
            "duration": new_material["duration"],
            "width": new_material["width"],
            "height": new_material["height"],
            "type": new_material["type"],
            "category_id": "",
            "category_name": "local",
            "material_url": "",
            "request_id": "",
            "source": 0,
            "source_platform": 0,
            "is_copyright": False,
        }
    )
    if "has_audio" in target_material and new_material["type"] == "photo":
        target_material["has_audio"] = False

    return old_material


def validate_nested_source_timerange(segment: dict[str, Any], material: dict[str, Any]) -> None:
    if material.get("type") == "photo":
        return
    source_timerange = segment.get("source_timerange")
    if not isinstance(source_timerange, dict):
        return
    material_duration = material.get("duration")
    if not isinstance(material_duration, int):
        return
    start = source_timerange.get("start", 0)
    duration = source_timerange.get("duration", 0)
    if not isinstance(start, int) or not isinstance(duration, int):
        return
    if start + duration > material_duration:
        raise RuntimeError(
            "新的嵌套视频素材时长不足，无法覆盖原片段 source_timerange: "
            f"source_start={start}, source_duration={duration}, material_duration={material_duration}。"
            "请换更长的视频，或设置 --nested-video-source-duration-us。"
        )


def replace_nested_video_segment_by_index(draft: Any, data: dict[str, Any], args: argparse.Namespace) -> bool:
    if not args.replace_nested_video_segment_path:
        return False

    media_path = Path(args.replace_nested_video_segment_path).resolve()
    if not media_path.exists():
        raise FileNotFoundError(f"嵌套模板替换素材不存在: {media_path}")

    ref = find_nested_video_segment_ref(
        data,
        args.target_nested_draft_index,
        args.target_nested_video_track_index,
        args.target_nested_video_segment_index,
    )
    segment = ref["segment"]
    target_material = ref["material"]
    old_material = update_nested_video_material_fields(draft, target_material, media_path)

    source_timerange = segment.setdefault("source_timerange", {"start": 0, "duration": 0})
    if not isinstance(source_timerange, dict):
        raise RuntimeError("目标嵌套视频片段的 source_timerange 不是对象")
    apply_json_timerange_override(
        source_timerange,
        args.nested_video_source_start_us,
        args.nested_video_source_duration_us,
        "嵌套视频片段 source_timerange",
    )

    target_timerange = segment.get("target_timerange")
    if not isinstance(target_timerange, dict):
        raise RuntimeError("目标嵌套视频片段缺少 target_timerange")
    target_changed = apply_json_timerange_override(
        target_timerange,
        args.nested_video_target_start_us,
        args.nested_video_target_duration_us,
        "嵌套视频片段 target_timerange",
    )
    if target_changed and target_material.get("type") == "photo" and args.nested_video_source_duration_us <= 0:
        source_timerange["duration"] = target_timerange.get("duration", source_timerange.get("duration", 0))

    validate_nested_source_timerange(segment, target_material)

    log(
        "已替换嵌套模板视频/图片素材: "
        f"nested_draft_index={args.target_nested_draft_index}, "
        f"video_track_index={args.target_nested_video_track_index}, "
        f"raw_track_index={ref['raw_track_index']}, "
        f"video_segment_index={args.target_nested_video_segment_index}, "
        f"old=({video_material_label(old_material)}), "
        f"new=({video_material_label(target_material)})"
    )
    return True


def apply_nested_json_changes(draft: Any, data: dict[str, Any], args: argparse.Namespace) -> int:
    changed = 0
    changed += int(replace_nested_video_segment_by_index(draft, data, args))
    if changed:
        log(f"本次共执行 {changed} 项嵌套模板 JSON 修改")
    return changed


def set_first_video_duration(draft: Any, script: Any, duration_us: int) -> bool:
    if duration_us <= 0:
        return False

    try:
        video_track = script.get_imported_track(draft.TrackType.video, index=0)
    except Exception as exc:
        log(f"未找到可修改的视频轨道，跳过视频片段时长修改: {exc}", "WARN")
        return False

    if len(video_track) == 0:
        log("视频轨道存在但没有片段，跳过视频片段时长修改", "WARN")
        return False

    first_segment = video_track.segments[0]
    if len(video_track) > 1:
        next_start = video_track.segments[1].start
        if first_segment.start + duration_us > next_start:
            raise RuntimeError(
                "不能设置第 1 个视频片段时长，设置后会和下一个片段重叠: "
                f"start={first_segment.start}, duration={duration_us}, next_start={next_start}"
            )

    old_duration = first_segment.duration
    first_segment.duration = duration_us
    log(f"已修改第 1 个视频片段目标时长: {old_duration} -> {duration_us} 微秒")
    return True


def apply_probe_changes(draft: Any, script: Any, args: argparse.Namespace, summary: dict[str, Any]) -> int:
    changed = 0
    materials = summary["materials"]

    if args.replace_first_text:
        changed += int(replace_first_text(draft, script, args.replace_first_text))
    if args.replace_text or args.text_start_us >= 0 or args.text_duration_us > 0:
        changed += int(replace_text_by_index(draft, script, args))

    if args.replace_video_path:
        video_name = args.replace_video_material_name or first_material_name(materials, "video")
        if not video_name:
            raise RuntimeError("没有找到可替换的视频素材名，请显式传入 --replace-video-material-name")
        changed += int(replace_video_by_name(draft, script, video_name, Path(args.replace_video_path).resolve()))
    changed += int(replace_video_segment_by_index(draft, script, args))

    if args.replace_audio_path:
        audio_name = args.replace_audio_material_name or first_material_name(materials, "audio")
        if not audio_name:
            raise RuntimeError("没有找到可替换的音频素材名，请显式传入 --replace-audio-material-name")
        changed += int(replace_audio_by_name(draft, script, audio_name, Path(args.replace_audio_path).resolve()))
    changed += int(replace_audio_segment_by_index(draft, script, args))
    changed += int(add_audio_track_segment(draft, script, args))

    if args.first_video_target_duration_us:
        changed += int(set_first_video_duration(draft, script, args.first_video_target_duration_us))

    if changed == 0 and args.replace_nested_video_segment_path:
        log("顶层轨道没有修改，后续将执行嵌套模板 JSON 修改")
    elif changed == 0:
        log("没有传入替换参数，本次只验证复制、读取、统计、保存流程", "WARN")
    else:
        log(f"本次共执行 {changed} 项修改")

    return changed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证 pyJianYingDraft 是否能读取、修改并另存剪映 5.9 明文 JSON 草稿。"
    )
    parser.add_argument("--template-draft-dir", required=True, help="原始模板草稿目录，目录内必须有 draft_content.json")
    parser.add_argument("--output-root", default="", help="新草稿输出父目录；推荐填剪映的 JianyingPro Drafts 目录")
    parser.add_argument("--output-name", default="", help="新草稿文件夹名称；不填则自动追加时间戳")
    parser.add_argument("--replace-first-text", default="", help="替换第 1 条文本轨道的第 1 个文本片段")
    parser.add_argument("--replace-text", default="", help="替换指定文本轨道/片段的内容")
    parser.add_argument("--target-text-track-index", type=int, default=0, help="目标文本轨道下标，按同类型文本轨道从 0 开始")
    parser.add_argument("--target-text-segment-index", type=int, default=0, help="目标文本片段下标，按目标文本轨道内从 0 开始")
    parser.add_argument("--text-start-us", type=int, default=-1, help="指定文本片段新的开始时间，微秒；-1 表示不改")
    parser.add_argument("--text-duration-us", type=int, default=0, help="指定文本片段新的持续时间，微秒；0 表示不改")
    parser.add_argument("--replace-video-path", default="", help="新视频/图片素材路径；默认替换第 1 个视频素材名")
    parser.add_argument("--replace-video-material-name", default="", help="要替换的原视频素材名，不填则自动取第 1 个")
    parser.add_argument("--replace-video-segment-path", default="", help="只替换指定视频轨道/片段的素材路径")
    parser.add_argument("--video-source-start-us", type=int, default=-1, help="新视频素材截取开始时间，微秒；-1 表示默认")
    parser.add_argument("--video-source-duration-us", type=int, default=0, help="新视频素材截取持续时间，微秒；0 表示默认")
    parser.add_argument("--video-target-start-us", type=int, default=-1, help="目标视频片段新的开始时间，微秒；-1 表示不改")
    parser.add_argument("--video-target-duration-us", type=int, default=0, help="目标视频片段新的持续时间，微秒；0 表示不改")
    parser.add_argument("--replace-audio-path", default="", help="新音频素材路径；默认替换第 1 个音频素材名")
    parser.add_argument("--replace-audio-material-name", default="", help="要替换的原音频素材名，不填则自动取第 1 个")
    parser.add_argument("--replace-audio-segment-path", default="", help="只替换指定音频轨道/片段的素材路径")
    parser.add_argument("--add-audio-path", default="", help="新增一条音乐轨道并添加该音频素材")
    parser.add_argument("--target-audio-track-index", type=int, default=0, help="目标音频轨道下标，按同类型音频轨道从 0 开始")
    parser.add_argument("--target-audio-segment-index", type=int, default=0, help="目标音频片段下标，按目标音频轨道内从 0 开始")
    parser.add_argument("--audio-source-start-us", type=int, default=-1, help="新音频素材截取开始时间，微秒；-1 表示默认")
    parser.add_argument("--audio-source-duration-us", type=int, default=0, help="新音频素材截取持续时间，微秒；0 表示默认")
    parser.add_argument("--audio-target-start-us", type=int, default=-1, help="目标音频片段新的开始时间，微秒；-1 表示不改；新增音乐时 -1 表示 0")
    parser.add_argument("--audio-target-duration-us", type=int, default=0, help="目标音频片段新的持续时间，微秒；0 表示不改或使用素材时长")
    parser.add_argument("--audio-loop-to-target", action="store_true", help="新增音乐短于目标时长时循环，并裁切最后一次循环到目标结尾")
    parser.add_argument("--dump-effects", action="store_true", help="打印特效轨道和 materials.video_effects 的详细关联")
    parser.add_argument("--dump-nested-drafts", action="store_true", help="打印 materials.drafts[*].draft 里的内部轨道和可替换视频/图片素材")
    parser.add_argument("--replace-nested-video-segment-path", default="", help="替换嵌套模板里指定内部视频轨道/片段引用的视频或图片素材")
    parser.add_argument("--target-nested-draft-index", type=int, default=0, help="目标嵌套草稿下标，按 materials.drafts[*].draft 从 0 开始")
    parser.add_argument("--target-nested-video-track-index", type=int, default=0, help="目标嵌套视频轨道下标，按嵌套草稿内同类型视频轨道从 0 开始")
    parser.add_argument("--target-nested-video-segment-index", type=int, default=0, help="目标嵌套视频片段下标，按目标嵌套视频轨道内从 0 开始")
    parser.add_argument("--nested-video-source-start-us", type=int, default=-1, help="嵌套替换素材截取开始时间，微秒；-1 表示不改")
    parser.add_argument("--nested-video-source-duration-us", type=int, default=0, help="嵌套替换素材截取持续时间，微秒；0 表示不改")
    parser.add_argument("--nested-video-target-start-us", type=int, default=-1, help="嵌套视频片段新的时间线开始时间，微秒；-1 表示不改")
    parser.add_argument("--nested-video-target-duration-us", type=int, default=0, help="嵌套视频片段新的时间线持续时间，微秒；0 表示不改")
    parser.add_argument("--export-first-effect-json", default="", help="把模板草稿里的第一个视频特效导出为可复用 JSON")
    parser.add_argument("--effect-json-path", default="", help="已导出的特效 JSON 文件路径")
    parser.add_argument("--add-effect-json-to-video", action="store_true", help="把特效 JSON 添加到指定视频片段上")
    parser.add_argument("--effect-source-draft-dir", default="", help="特效来源草稿目录，目录内必须有 draft_content.json")
    parser.add_argument(
        "--replace-first-effect-from-source",
        action="store_true",
        help="把来源草稿第一个视频特效替换到目标草稿第一个特效占位上",
    )
    parser.add_argument("--target-video-track-index", type=int, default=0, help="目标视频轨道下标，按同类型视频轨道从 0 开始")
    parser.add_argument("--target-video-segment-index", type=int, default=0, help="目标视频片段下标，按目标视频轨道内从 0 开始")
    parser.add_argument("--effect-start-us", type=int, default=-1, help="新增特效片段的开始时间，微秒；-1 表示跟随目标视频片段")
    parser.add_argument("--effect-duration-us", type=int, default=0, help="新增特效片段的持续时间，微秒；0 表示跟随目标视频片段")
    parser.add_argument(
        "--first-video-target-duration-us",
        type=int,
        default=0,
        help="把第 1 条视频轨道第 1 个片段的目标时长改为指定微秒数；0 表示不修改",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        draft = import_pyjianyingdraft()

        template_dir = Path(args.template_draft_dir).resolve()
        output_root = Path(args.output_root).resolve()
        if not template_dir.exists():
            return fail(f"模板草稿目录不存在: {template_dir}")
        if not template_dir.is_dir():
            return fail(f"模板草稿路径不是目录: {template_dir}")

        log(f"模板草稿目录: {template_dir}")
        if args.output_root:
            log(f"输出父目录: {output_root}")

        original_data = load_plain_draft_json(template_dir)
        original_summary = summarize_draft_json(original_data, "原模板")
        if args.dump_effects:
            log_effect_details(original_data, "原模板")
        if args.dump_nested_drafts:
            log_nested_draft_details(original_data, "原模板")
        validate_template_with_pyjyd(draft, template_dir)

        if args.export_first_effect_json:
            export_first_effect_json(original_data, Path(args.export_first_effect_json).resolve())
            if not args.output_root:
                return 0

        source_effect_data: dict[str, Any] | None = None
        if args.effect_source_draft_dir:
            source_effect_dir = Path(args.effect_source_draft_dir).resolve()
            if not source_effect_dir.exists():
                return fail(f"特效来源草稿目录不存在: {source_effect_dir}")
            source_effect_data = load_plain_draft_json(source_effect_dir)
            log_effect_details(source_effect_data, "特效来源草稿")
        if args.replace_first_effect_from_source and source_effect_data is None:
            return fail("使用 --replace-first-effect-from-source 时必须传入 --effect-source-draft-dir")
        effect_json_data: dict[str, Any] | None = None
        if args.effect_json_path:
            effect_json_data = load_effect_json(Path(args.effect_json_path).resolve())
        if args.add_effect_json_to_video and effect_json_data is None:
            return fail("使用 --add-effect-json-to-video 时必须传入 --effect-json-path")
        if not args.output_root:
            if (args.dump_effects or args.dump_nested_drafts) and not (
                args.replace_first_text
                or args.replace_text
                or args.replace_video_path
                or args.replace_video_segment_path
                or args.replace_audio_path
                or args.replace_audio_segment_path
                or args.add_audio_path
                or args.replace_first_effect_from_source
                or args.add_effect_json_to_video
                or args.replace_nested_video_segment_path
                or args.first_video_target_duration_us
            ):
                return 0
            return fail("除导出特效 JSON 外，其它操作必须传入 --output-root")

        output_name = args.output_name.strip()
        if not output_name:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_name = f"{template_dir.name}_probe_{stamp}"
        log(f"输出草稿名称: {output_name}")

        output_dir = copy_template_draft(template_dir, output_root, output_name)
        copied_data = load_plain_draft_json(output_dir)
        copied_summary = summarize_draft_json(copied_data, "输出副本-修改前")
        if args.dump_effects:
            log_effect_details(copied_data, "输出副本-修改前")
        if args.dump_nested_drafts:
            log_nested_draft_details(copied_data, "输出副本-修改前")

        script = load_output_script(draft, output_root, output_name)
        apply_probe_changes(draft, script, args, copied_summary)

        script.save()
        log(f"pyJianYingDraft 保存成功: {output_dir / 'draft_content.json'}")

        saved_data = load_plain_draft_json(output_dir)
        if args.replace_first_effect_from_source:
            assert source_effect_data is not None
            replace_first_effect_from_source(saved_data, source_effect_data)
            save_plain_draft_json(output_dir, saved_data)
            saved_data = load_plain_draft_json(output_dir)
        if args.add_effect_json_to_video:
            assert effect_json_data is not None
            add_effect_json_to_video(
                saved_data,
                effect_json_data,
                args.target_video_track_index,
                args.target_video_segment_index,
                args.effect_start_us,
                args.effect_duration_us,
            )
            save_plain_draft_json(output_dir, saved_data)
            saved_data = load_plain_draft_json(output_dir)
        if args.replace_nested_video_segment_path:
            apply_nested_json_changes(draft, saved_data, args)
            save_plain_draft_json(output_dir, saved_data)
            saved_data = load_plain_draft_json(output_dir)

        summarize_draft_json(saved_data, "输出副本-保存后")
        if args.dump_effects or args.replace_first_effect_from_source or args.add_effect_json_to_video:
            log_effect_details(saved_data, "输出副本-保存后")
        if args.dump_nested_drafts or args.replace_nested_video_segment_path:
            log_nested_draft_details(saved_data, "输出副本-保存后")
        load_output_script(draft, output_root, output_name)
        log("保存后草稿可被 pyJianYingDraft 再次加载，基础读写链路验证通过")

        log(f"新草稿完整目录: {output_dir}")
        log("最后一步请在剪映中打开这个新草稿目录对应的草稿，确认时间线和素材正常")
        return 0
    except Exception as exc:
        log(str(exc), "ERROR")
        return 1
