from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Iterable


class H3LocalPostprocessError(RuntimeError):
    pass


H3_JYD_HANDOFF_SCHEMA = "h3.jyd_handoff.v2"


@dataclass(frozen=True)
class VisibleTailPlan:
    trim_start_seconds: float
    trim_end_seconds: float
    anchor_frame_seconds: float


@dataclass(frozen=True)
class H3JYDAssets:
    master_av_path: Path
    silent_base_video_path: Path
    authoritative_audio_path: Path
    subtitle_cues_path: Path
    segment_durations_seconds: tuple[float, ...]


def visible_tail_plan(generated_duration_seconds: float, *, fps: int = 24) -> VisibleTailPlan:
    """Describe the complete generated result and its soft-chain anchor frame."""

    if generated_duration_seconds <= 0 or fps <= 0:
        raise ValueError("H3 生成结果时长或帧率不合法")
    return VisibleTailPlan(
        trim_start_seconds=0.0,
        trim_end_seconds=float(generated_duration_seconds),
        anchor_frame_seconds=max(0.0, float(generated_duration_seconds) - 1 / fps),
    )


def _compact_text(value: object) -> str:
    return "".join(str(value or "").split())


def _run(command: list[str], message: str) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except FileNotFoundError as exc:
        raise H3LocalPostprocessError("本机未安装 FFmpeg/FFprobe") from exc
    except subprocess.TimeoutExpired as exc:
        raise H3LocalPostprocessError(f"{message}：处理超时") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise H3LocalPostprocessError(f"{message}：{detail or '未知错误'}")


def _atomic_media(command: list[str], target: Path, message: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        _run([*command, str(temporary)], message)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _concat_manifest(segment_paths: Iterable[Path], target: Path) -> Path:
    paths = list(segment_paths)
    if not paths or any(not path.is_file() for path in paths):
        raise H3LocalPostprocessError("H3 音画分段不完整")
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'"
        for path in paths
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def merge_generated_av_segments(segment_paths: list[Path], target: Path) -> Path:
    """Join complete H3 AV and normalize both output tracks to timeline zero.

    The concat demuxer exposes AAC encoder priming as a negative timestamp even
    when every source MP4 reports both tracks at zero.  A plain stream-copy then
    makes the first video frame start about one AAC packet late.  The second
    stream-copy pass applies each track's measured start offset independently;
    this preserves H3's encoded audio/video samples and their in-segment sync.
    """

    concat_file = target.with_suffix(".segments.txt")
    concat_output = target.with_name(f"{target.stem}.concat{target.suffix}")
    _concat_manifest(segment_paths, concat_file)
    try:
        _atomic_media(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-map", "0:v:0", "-map", "0:a:0",
                "-c", "copy",
                "-movflags", "+faststart",
            ],
            concat_output,
            "合并 H3 原生音画失败",
        )
        video_start, audio_start = probe_av_stream_starts(concat_output)
        return _atomic_media(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-itsoffset", f"{-video_start:.9f}", "-i", str(concat_output),
                "-itsoffset", f"{-audio_start:.9f}", "-i", str(concat_output),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy", "-avoid_negative_ts", "disabled",
                "-movflags", "+faststart",
            ],
            target,
            "归零 H3 原生音画时间戳失败",
        )
    finally:
        concat_file.unlink(missing_ok=True)
        concat_output.unlink(missing_ok=True)


def probe_av_stream_starts(path: Path) -> tuple[float, float]:
    if not path.is_file():
        raise H3LocalPostprocessError("H3 合并音画不存在")
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,start_time",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except FileNotFoundError as exc:
        raise H3LocalPostprocessError("本机未安装 FFprobe") from exc
    except subprocess.TimeoutExpired as exc:
        raise H3LocalPostprocessError("探测 H3 音画时间戳超时") from exc
    try:
        payload = json.loads(completed.stdout)
        starts = {
            str(stream["codec_type"]): float(stream.get("start_time") or 0)
            for stream in payload["streams"]
        }
        video_start = starts["video"]
        audio_start = starts["audio"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise H3LocalPostprocessError("无法读取 H3 音画时间戳") from exc
    if completed.returncode != 0 or video_start < 0 or audio_start < 0:
        raise H3LocalPostprocessError("H3 音画时间戳不合法")
    return video_start, audio_start


def extract_authoritative_h3_audio(master_av: Path, target: Path) -> Path:
    if not master_av.is_file():
        raise H3LocalPostprocessError("H3 合并音画不存在")
    return _atomic_media(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(master_av),
            "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le",
        ],
        target,
        "抽取 H3 权威音频失败",
    )


def make_silent_base_video(master_av: Path, target: Path) -> Path:
    if not master_av.is_file():
        raise H3LocalPostprocessError("H3 合并音画不存在")
    return _atomic_media(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(master_av),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart",
        ],
        target,
        "拆分 JYD 静音基础视频失败",
    )


def probe_media_duration(path: Path) -> float:
    if not path.is_file():
        raise H3LocalPostprocessError("H3 音画分段不存在")
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration", "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except FileNotFoundError as exc:
        raise H3LocalPostprocessError("本机未安装 FFprobe") from exc
    except subprocess.TimeoutExpired as exc:
        raise H3LocalPostprocessError("探测 H3 分段时长超时") from exc
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload["streams"][0]["duration"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise H3LocalPostprocessError("无法读取 H3 分段视频时长") from exc
    if completed.returncode != 0 or duration <= 0:
        raise H3LocalPostprocessError("H3 分段视频时长不合法")
    return duration


def build_h3_segment_cues(
    segment_texts: list[str],
    segment_durations_seconds: list[float],
    *,
    script_text: str,
) -> list[dict[str, int | str]]:
    if (
        not segment_texts
        or len(segment_texts) != len(segment_durations_seconds)
        or any(not str(text or "").strip() for text in segment_texts)
        or any(duration <= 0 for duration in segment_durations_seconds)
    ):
        raise H3LocalPostprocessError("H3 字幕分段输入不完整")
    if _compact_text("".join(segment_texts)) != _compact_text(script_text):
        raise H3LocalPostprocessError("H3 分段台词无法重建冻结原稿")
    result: list[dict[str, int | str]] = []
    cursor_us = 0
    for index, (text, duration) in enumerate(
        zip(segment_texts, segment_durations_seconds, strict=True)
    ):
        duration_us = max(1, round(duration * 1_000_000))
        end_us = cursor_us + duration_us
        result.append(
            {
                "text": str(text).strip(),
                "start_us": cursor_us,
                "duration_us": duration_us,
                "end_us": end_us,
                "segment_index": index,
            }
        )
        cursor_us = end_us
    return result


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def prepare_h3_jyd_assets(
    *,
    segment_paths: list[Path],
    segment_texts: list[str],
    script_text: str,
    target_dir: Path,
) -> H3JYDAssets:
    durations = [probe_media_duration(path) for path in segment_paths]
    master = merge_generated_av_segments(segment_paths, target_dir / "h3-master-av.mp4")
    authoritative_audio = extract_authoritative_h3_audio(
        master, target_dir / "h3-authoritative-full.wav"
    )
    silent_base = make_silent_base_video(master, target_dir / "h3-base-video-silent.mp4")
    cues = build_h3_segment_cues(
        segment_texts,
        durations,
        script_text=script_text,
    )
    cues_path = write_json(target_dir / "h3-segment-cue-windows.json", cues)
    return H3JYDAssets(
        master_av_path=master,
        silent_base_video_path=silent_base,
        authoritative_audio_path=authoritative_audio,
        subtitle_cues_path=cues_path,
        segment_durations_seconds=tuple(durations),
    )


def build_jyd_handoff(
    *,
    project_id: str,
    silent_base_video: str,
    authoritative_h3_audio: str,
    h3_segment_cues_asset: str,
    h3_master_av: str,
    segment_ids: list[str],
    row_key: str = "",
    script_text: str = "",
    input_raw_cues_asset: str = "",
) -> dict[str, object]:
    if not all(
        str(value or "").strip()
        for value in (
            project_id,
            silent_base_video,
            authoritative_h3_audio,
            h3_segment_cues_asset,
            h3_master_av,
            row_key,
            script_text,
        )
    ):
        raise ValueError("H3 -> JYD 交接缺少必要资产")
    if (
        not segment_ids
        or any(not str(segment_id or "").strip() for segment_id in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
    ):
        raise ValueError("H3 -> JYD 分段来源不合法")
    handoff_key = json.dumps(
        {
            "schema_version": H3_JYD_HANDOFF_SCHEMA,
            "project_id": project_id,
            "segment_ids": segment_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": H3_JYD_HANDOFF_SCHEMA,
        "project_id": project_id,
        "handoff_id": hashlib.sha256(handoff_key).hexdigest(),
        "source": {
            "row_key": str(row_key).strip(),
            "script_text": str(script_text).strip(),
            "input_raw_cues_asset": str(input_raw_cues_asset or "").strip() or None,
        },
        "h3_master": {
            "path": h3_master_av,
            "audio_video_pair": "h3_generated",
        },
        "base_video": {
            "path": silent_base_video,
            "role": "base_video",
            "audio_policy": "separate_h3_generated_audio",
            "source_segment_ids": list(segment_ids),
        },
        "authoritative_audio": {
            "path": authoritative_h3_audio,
            "source": "h3_generated_audio",
            "timeline_start_seconds": 0.0,
            "reuse_once": True,
        },
        "subtitles": {
            "raw_cues_asset": h3_segment_cues_asset,
            "timing_source": "h3_segment_windows_then_funasr",
            "script_source": "frozen_input_script",
        },
    }


def write_jyd_handoff(path: Path, handoff: dict[str, object]) -> Path:
    return write_json(path, handoff)
