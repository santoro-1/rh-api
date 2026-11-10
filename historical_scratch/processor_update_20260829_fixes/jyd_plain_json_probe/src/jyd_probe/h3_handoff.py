from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .project_store import ProjectStore


class H3HandoffError(ValueError):
    pass


def _path(value: object, field: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise H3HandoffError(f"H3 交接缺少 {field}")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise H3HandoffError(f"H3 交接文件不存在：{field}")
    return path


def _raw_cues(path: Path) -> list[dict[str, int | str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise H3HandoffError("H3 raw cues 文件损坏") from exc
    if isinstance(value, dict):
        value = value.get("cues") or value.get("captions") or value.get("sentences")
    if not isinstance(value, list) or not value:
        raise H3HandoffError("H3 raw cues 不能为空")
    result: list[dict[str, int | str]] = []
    previous_end = 0
    for index, cue in enumerate(value, start=1):
        if not isinstance(cue, dict):
            raise H3HandoffError(f"H3 raw cue {index} 格式错误")
        text = str(cue.get("text") or "").strip()
        start = cue.get("start_us")
        end = cue.get("end_us")
        if type(start) is not int or type(end) is not int:
            try:
                start = round(float(cue["start_seconds"]) * 1_000_000)
                end = round(float(cue["end_seconds"]) * 1_000_000)
            except (KeyError, TypeError, ValueError) as exc:
                raise H3HandoffError(f"H3 raw cue {index} 时间格式错误") from exc
        if not text or start < previous_end or end <= start:
            raise H3HandoffError(f"H3 raw cue {index} 文本或时间范围错误")
        result.append(
            {
                "text": text,
                "start_us": start,
                "duration_us": end - start,
                "end_us": end,
            }
        )
        previous_end = end
    return result


def _compact_text(value: object) -> str:
    return "".join(str(value or "").split())


def load_h3_handoff(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise H3HandoffError("H3 -> JYD 交接清单无法读取") from exc
    if not isinstance(value, dict) or value.get("schema_version") not in {
        "h3.jyd_handoff.v1",
        "h3.jyd_handoff.v2",
    }:
        raise H3HandoffError("H3 -> JYD 交接版本不受支持")
    schema_version = str(value["schema_version"])
    base = value.get("base_video")
    audio = value.get("authoritative_audio")
    subtitles = value.get("subtitles")
    source = value.get("source")
    if not all(isinstance(item, dict) for item in (base, audio, subtitles, source)):
        raise H3HandoffError("H3 -> JYD 交接结构不完整")
    if schema_version == "h3.jyd_handoff.v2":
        master = value.get("h3_master")
        if not isinstance(master, dict):
            raise H3HandoffError("H3 v2 交接缺少原生音画母版")
        if master.get("audio_video_pair") != "h3_generated":
            raise H3HandoffError("H3 v2 母版必须保留模型生成的完整音画")
        if base.get("audio_policy") != "separate_h3_generated_audio":
            raise H3HandoffError("H3 v2 base_video 必须使用拆分后的 H3 生成音轨")
        if audio.get("source") != "h3_generated_audio":
            raise H3HandoffError("H3 v2 权威音频必须来自 H3 生成音轨")
        if subtitles.get("timing_source") != "h3_segment_windows_then_funasr":
            raise H3HandoffError("H3 v2 字幕必须绑定 H3 音频并等待 FunASR 对齐")
    else:
        master = None
        if base.get("audio_policy") != "no_h3_audio":
            raise H3HandoffError("H3 v1 base_video 必须明确丢弃供应商音轨")
        if subtitles.get("timing_source") != "authoritative_full_audio":
            raise H3HandoffError("H3 v1 字幕必须绑定权威完整音频时间轴")
    if base.get("role") != "base_video":
        raise H3HandoffError("H3 交接基础视频角色不合法")
    if audio.get("timeline_start_seconds") != 0.0 or audio.get("reuse_once") is not True:
        raise H3HandoffError("H3 权威完整音频必须从 0 秒只铺入一次")
    script = str(source.get("script_text") or "").strip()
    if not script:
        raise H3HandoffError("H3 交接缺少冻结脚本")
    project_id = str(value.get("project_id") or "").strip()
    handoff_id = str(value.get("handoff_id") or "").strip()
    segment_ids = base.get("source_segment_ids")
    if (
        not project_id
        or not handoff_id
        or not isinstance(segment_ids, list)
        or not segment_ids
        or any(not str(segment_id or "").strip() for segment_id in segment_ids)
        or len({str(segment_id) for segment_id in segment_ids}) != len(segment_ids)
    ):
        raise H3HandoffError("H3 交接缺少唯一交接编号")
    identity = {"project_id": project_id, "segment_ids": segment_ids}
    if schema_version == "h3.jyd_handoff.v2":
        identity["schema_version"] = schema_version
    expected_handoff_id = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if handoff_id != expected_handoff_id:
        raise H3HandoffError("H3 交接编号与分段来源不一致")
    base_path = _path(base.get("path"), "base_video")
    audio_path = _path(audio.get("path"), "authoritative_audio")
    cues_path = _path(subtitles.get("raw_cues_asset"), "raw_cues")
    master_path = _path(master.get("path"), "h3_master") if master else None
    raw_cues = _raw_cues(cues_path)
    if _compact_text(script) != _compact_text(
        "".join(str(cue["text"]) for cue in raw_cues)
    ):
        raise H3HandoffError("H3 raw cues 与冻结脚本不一致")
    return {
        "manifest": value,
        "manifest_path": manifest_path,
        "schema_version": schema_version,
        "handoff_id": handoff_id,
        "row_key": str(source.get("row_key") or "H3-001").strip() or "H3-001",
        "script_text": script,
        "base_video_path": base_path,
        "audio_path": audio_path,
        "cues_path": cues_path,
        "raw_cues": raw_cues,
        "master_path": master_path,
    }


def import_h3_handoff(
    store: ProjectStore,
    *,
    owner_user_id: str,
    owner_username: str,
    project_name: str,
    manifest_path: str | Path,
) -> dict[str, Any]:
    handoff = load_h3_handoff(manifest_path)
    cues = handoff["raw_cues"]
    is_v2 = handoff["schema_version"] == "h3.jyd_handoff.v2"
    return store.import_h3_handoff_project(
        owner_user_id=owner_user_id,
        owner_username=owner_username,
        project_name=project_name,
        row_key=handoff["row_key"],
        script_text=handoff["script_text"],
        handoff_id=handoff["handoff_id"],
        audio_filename=handoff["audio_path"].name,
        audio_managed_path=str(handoff["audio_path"]),
        audio_metadata={
            "authoritative": True,
            "timeline_start_seconds": 0.0,
            "source": "h3_generated_audio" if is_v2 else "minimax_input_audio",
        },
        base_video_filename=handoff["base_video_path"].name,
        base_video_managed_path=str(handoff["base_video_path"]),
        base_video_metadata={
            "audio_policy": handoff["manifest"]["base_video"]["audio_policy"],
            "source_segment_ids": handoff["manifest"]["base_video"].get(
                "source_segment_ids"
            )
            or [],
            "h3_master_path": (
                str(handoff["master_path"]) if handoff["master_path"] else None
            ),
        },
        subtitles={
            "source": "h3_segment_windows" if is_v2 else "minimax_timestamps",
            "raw_cues": cues,
            "render_cues": cues,
            "bound_video_asset_id": None,
            "style": {},
            "status": "READY",
        },
        link_metadata={
            "manifest_path": str(handoff["manifest_path"]),
            "handoff_schema": handoff["schema_version"],
        },
    )
