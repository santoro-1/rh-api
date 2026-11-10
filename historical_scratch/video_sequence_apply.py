"""Replace a template's picture placeholder with real editable source clips."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
import uuid


def apply_main_video_sequence(draft: Any, data: dict[str, Any], config: dict[str, Any]) -> int:
    items = config.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("模板主视频分段不能为空")
    tracks = data["tracks"]
    materials = data["materials"]
    video_tracks = [t for t in tracks if t.get("type") == "video"]
    track_index = int(config.get("track_index", -1))
    segment_index = int(config.get("segment_index", 0))
    placeholder = None
    if track_index >= 0:
        track = video_tracks[track_index]
        placeholder = track["segments"][segment_index]
    else:
        track = {"id": uuid.uuid4().hex, "type": "video", "name": "项目主视频",
                 "is_default_name": False, "segments": []}
    obsolete_refs = {entry["id"] for key in ("speeds", "transitions")
                     for entry in materials.get(key, [])}
    new_segments = []
    new_materials: dict[str, list] = {"videos": [], "speeds": [], "transitions": []}
    cursor = 0
    for index, item in enumerate(items):
        path = Path(item["media_path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"模板主视频分段不存在：{path}")
        material = draft.VideoMaterial(str(path))
        duration = int(item["target_duration_us"])
        start = int(item.get("source_start_us", 0))
        if duration <= 0 or start < 0 or material.duration <= start:
            raise ValueError("模板主视频分段时长无效")
        segment = draft.VideoSegment(
            material, draft.Timerange(cursor, duration),
            source_timerange=draft.Timerange(start, min(duration, material.duration - start)),
            volume=float(item.get("volume", 0.0)),
        )
        transition = int(item.get("transition_after_us", 0))
        if transition > 0 and index + 1 < len(items):
            segment.add_transition(draft.TransitionType.叠化, duration=min(
                transition, duration // 2, int(items[index + 1]["target_duration_us"]) // 2))
            new_materials["transitions"].append(segment.transition.export_json())
        encoded = segment.export_json()
        if placeholder is not None:
            # Retain the template's framing, opacity, filters and native visual
            # attributes, but never reuse its material, clock or speed identity.
            retained = copy.deepcopy(placeholder)
            for key in ("id", "material_id", "target_timerange", "source_timerange", "speed", "volume"):
                retained[key] = encoded[key]
            retained["extra_material_refs"] = [ref for ref in placeholder.get("extra_material_refs", [])
                                               if ref not in obsolete_refs] + encoded.get("extra_material_refs", [])
            encoded = retained
        else:
            encoded["track_render_index"] = -100
        new_segments.append(encoded)
        new_materials["videos"].append(material.export_json())
        new_materials["speeds"].append(segment.speed.export_json())
        cursor += duration
    expected = int(data.get("duration") or 0)
    if abs(cursor - expected) > len(items):
        raise ValueError("模板主视频分段总时长与后期时间线不一致")
    if placeholder is not None:
        # Only the bound main-video slot belongs to the project; other template
        # tracks (and any non-overlapping clips) retain their native content.
        others = [s for i, s in enumerate(track["segments"]) if i != segment_index]
        if any(int(s.get("target_timerange", {}).get("start", 0)) < cursor for s in others):
            raise ValueError("模板主视频轨存在其他重叠片段，不能安全替换为独立分段")
        track["segments"][segment_index:segment_index + 1] = new_segments
    else:
        track["segments"] = new_segments
        tracks.insert(0, track)
    for key, entries in new_materials.items():
        materials.setdefault(key, []).extend(entries)
    if placeholder is not None:
        # Remove only the replaced placeholder's now-unused media; never purge
        # other template resources or touch the original template directory.
        old_id = placeholder.get("material_id")
        if not any(s.get("material_id") == old_id for t in tracks for s in t.get("segments", [])):
            materials["videos"] = [m for m in materials.get("videos", []) if m.get("id") != old_id]
    return len(new_segments)
