from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from .bgm_loudness import _ffmpeg_path


class H3MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class H3MediaAssets:
    master_av_path: Path
    silent_base_video_path: Path
    authoritative_audio_path: Path
    raw_cues: tuple[dict[str, int | str], ...]
    segment_durations_seconds: tuple[float, ...]


def _run(command: list[str], message: str) -> None:
    resolved_command = list(command)
    if resolved_command and resolved_command[0].lower() == "ffmpeg":
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            raise H3MediaError("本机未安装 FFmpeg")
        resolved_command[0] = ffmpeg
    try:
        completed = subprocess.run(
            resolved_command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except FileNotFoundError as exc:
        raise H3MediaError("本机未安装 FFmpeg/FFprobe") from exc
    except subprocess.TimeoutExpired as exc:
        raise H3MediaError(f"{message}：处理超时") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise H3MediaError(f"{message}：{detail or '未知错误'}")


def _ffprobe_path() -> str | None:
    configured = str(os.environ.get("JYD_FFPROBE") or "").strip()
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser().resolve())
    discovered = shutil.which("ffprobe")
    if discovered:
        return str(Path(discovered).resolve())
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    candidate = Path(ffmpeg).with_name(
        "ffprobe.exe" if Path(ffmpeg).suffix.lower() == ".exe" else "ffprobe"
    )
    return str(candidate.resolve()) if candidate.is_file() else None


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
        raise H3MediaError("H3 音画分段不完整")
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "file '" + path.resolve().as_posix().replace("'", "'\\''") + "'"
        for path in paths
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _probe_av_starts(path: Path) -> tuple[float, float]:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise H3MediaError("本机未安装 FFprobe")
    command = [
        ffprobe, "-v", "error", "-show_entries", "stream=codec_type,start_time",
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
        payload = json.loads(completed.stdout)
        starts = {
            str(stream["codec_type"]): float(stream.get("start_time") or 0)
            for stream in payload["streams"]
        }
        video_start, audio_start = starts["video"], starts["audio"]
    except FileNotFoundError as exc:
        raise H3MediaError("本机未安装 FFprobe") from exc
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise H3MediaError("无法读取 H3 音画时间戳") from exc
    if completed.returncode != 0 or video_start < 0 or audio_start < 0:
        raise H3MediaError("H3 音画时间戳不合法")
    return video_start, audio_start


def _merge_segments(segment_paths: list[Path], target: Path) -> Path:
    manifest = target.with_suffix(".segments.txt")
    concat_output = target.with_name(f"{target.stem}.concat{target.suffix}")
    _concat_manifest(segment_paths, manifest)
    try:
        _atomic_media(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "concat",
                "-safe", "0", "-i", str(manifest), "-map", "0:v:0", "-map",
                "0:a:0", "-c", "copy", "-movflags", "+faststart",
            ],
            concat_output,
            "合并 H3 原生音画失败",
        )
        video_start, audio_start = _probe_av_starts(concat_output)
        return _atomic_media(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-itsoffset",
                f"{-video_start:.9f}", "-i", str(concat_output), "-itsoffset",
                f"{-audio_start:.9f}", "-i", str(concat_output), "-map", "0:v:0",
                "-map", "1:a:0", "-c", "copy", "-avoid_negative_ts", "disabled",
                "-movflags", "+faststart",
            ],
            target,
            "归零 H3 原生音画时间戳失败",
        )
    finally:
        manifest.unlink(missing_ok=True)
        concat_output.unlink(missing_ok=True)


def _probe_duration(path: Path) -> float:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise H3MediaError("本机未安装 FFprobe")
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=duration", "-of", "json", str(path),
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
        duration = float(json.loads(completed.stdout)["streams"][0]["duration"])
    except FileNotFoundError as exc:
        raise H3MediaError("本机未安装 FFprobe") from exc
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise H3MediaError("无法读取 H3 分段视频时长") from exc
    if completed.returncode != 0 or duration <= 0:
        raise H3MediaError("H3 分段视频时长不合法")
    return duration


def _compact_text(value: object) -> str:
    return "".join(str(value or "").split())


def build_segment_cues(
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
        raise H3MediaError("H3 字幕分段输入不完整")
    if _compact_text("".join(segment_texts)) != _compact_text(script_text):
        raise H3MediaError("H3 分段台词无法重建冻结原稿")
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


def prepare_h3_media(
    *,
    segment_paths: list[Path],
    segment_texts: list[str],
    script_text: str,
    target_dir: Path,
) -> H3MediaAssets:
    durations = [_probe_duration(path) for path in segment_paths]
    master = _merge_segments(segment_paths, target_dir / "h3-master-av.mp4")
    audio = _atomic_media(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(master),
            "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le",
        ],
        target_dir / "h3-authoritative-full.wav",
        "抽取 H3 权威音频失败",
    )
    base = _atomic_media(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(master),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart",
        ],
        target_dir / "h3-base-video-silent.mp4",
        "拆分 JYD 静音基础视频失败",
    )
    cues = build_segment_cues(segment_texts, durations, script_text=script_text)
    return H3MediaAssets(
        master_av_path=master,
        silent_base_video_path=base,
        authoritative_audio_path=audio,
        raw_cues=tuple(cues),
        segment_durations_seconds=tuple(durations),
    )
