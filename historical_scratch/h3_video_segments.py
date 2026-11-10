"""Immutable H3 picture segments shared by materialization and draft planning."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any
import uuid

from .h3_audio_cleanup import file_sha256


H3_VIDEO_SEQUENCE_VERSION = "jyd.h3-video-sequence.v1"


def freeze_segment(source: Path, root: Path) -> Path:
    """Never let a historical draft reference the replaceable current.mp4 cache."""
    digest = file_sha256(source)
    target = root / digest / "segment.mp4"
    if target.is_file() and file_sha256(target) == digest:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{uuid.uuid4().hex}.part.mp4")
    try:
        shutil.copyfile(source, temporary)
        if file_sha256(temporary) != digest:
            raise RuntimeError("H3 分段在保存期间发生变化，请重试本地处理")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def bound_h3_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve exact frozen asset IDs, never the newest clip with a similar index."""
    outputs = item.get("outputs") or {}
    base = outputs.get("base_video") or {}
    metadata = base.get("metadata") or {}
    if metadata.get("video_sequence_version") != H3_VIDEO_SEQUENCE_VERSION:
        raise ValueError("H3 独立片段尚未准备好，请先刷新片段状态")
    ids = metadata.get("source_segment_asset_ids") or []
    segment_ids = metadata.get("source_segment_ids") or []
    count = metadata.get("segment_count")
    if (type(count) is not int or count < 1 or len(ids) != count
            or len(segment_ids) != count or len(set(ids)) != count):
        raise ValueError("H3 原始分段清单不完整，请重试本地处理")
    by_id = {a.get("asset_id"): a for a in outputs.get("original_video_segments", [])}
    result = []
    for index, (asset_id, segment_id) in enumerate(zip(ids, segment_ids), start=1):
        asset = by_id.get(asset_id) or {}
        ref = asset.get("external_ref") or {}
        meta = asset.get("metadata") or {}
        path = Path(str(asset.get("managed_path") or ""))
        if (asset.get("source_type") != "h3" or asset.get("status") != "READY"
                or ref.get("video_index") != index
                or ref.get("segment_id") != segment_id
                or meta.get("h3_segment_signature") != metadata.get("h3_segment_signature")
                or ref.get("batch_id") != metadata.get("remote_batch_id")
                or ref.get("remote_item_id") != metadata.get("remote_item_id")
                or type(meta.get("actual_duration_us")) is not int
                or meta["actual_duration_us"] <= 0
                or not path.is_file() or path.stat().st_size == 0):
            raise ValueError(f"H3 第 {index} 段缺失或版本不匹配，请重试本地处理")
        result.append(asset)
    if abs(sum(a["metadata"]["actual_duration_us"] for a in result)
           - int(metadata.get("duration_us") or 0)) > count:
        raise ValueError("H3 分段总时长与权威音画不一致，请重试本地处理")
    return result


def h3_video_sequence_ready(item: dict[str, Any]) -> bool:
    try:
        return bool(bound_h3_segments(item))
    except (OSError, TypeError, ValueError):
        return False
