from __future__ import annotations

import math
import subprocess
import re
import subprocess
import logging
from pathlib import Path
import wave

from mutagen import File as MutagenFile

from app.services.processes import hidden_creation_flags


logger = logging.getLogger(__name__)


class AudioInspectionError(ValueError):
    """The supplied media cannot be used as an audio input."""


_TIME_RE = re.compile(r"^(?P<minutes>\d+):(?P<seconds>[0-5]\d)$")


def parse_timecode(value: str) -> float:
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("时间格式必须为 M:SS，例如 1:05")
    return float(int(match.group("minutes")) * 60 + int(match.group("seconds")))


def format_timecode(seconds: float) -> str:
    if seconds < 0:
        raise ValueError("秒数不能小于 0")
    whole_seconds = int(seconds)
    return f"{whole_seconds // 60}:{whole_seconds % 60:02d}"


def format_duration_timecode(seconds: float) -> str:
    """Format a complete media duration without dropping a fractional tail."""
    if seconds < 0:
        raise ValueError("秒数不能小于 0")
    whole_seconds = math.ceil(seconds)
    return f"{whole_seconds // 60}:{whole_seconds % 60:02d}"


def validate_time_range(
    start_time: str, end_time: str, duration_seconds: float
) -> tuple[float, float]:
    start_seconds = parse_timecode(start_time)
    end_seconds = parse_timecode(end_time)
    if start_seconds < 0:
        raise ValueError("开始时间不能小于 0")
    if end_seconds <= start_seconds:
        raise ValueError("结束时间必须大于开始时间")
    # The public timecode has whole-second precision. Allow the exact ceiling
    # of the media duration so a fractional final second is not cut off.
    if end_seconds > math.ceil(duration_seconds):
        raise ValueError("结束时间不能超过音频实际时长")
    return start_seconds, end_seconds


def _duration_via_mutagen(path: Path) -> float | None:
    """Fallback for audio files when an ffprobe child process cannot run."""
    try:
        audio = MutagenFile(path)
        duration = float(audio.info.length) if audio and audio.info else 0.0
    except Exception:  # noqa: BLE001 - third-party codecs use varied exceptions
        duration = 0.0
    if duration > 0:
        return duration

    # Mutagen's generic detector does not always claim standard PCM WAV files.
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, OSError, ZeroDivisionError):
            return None
        return duration if duration > 0 else None
    return None


def inspect_audio_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        fallback_duration = _duration_via_mutagen(path)
        if fallback_duration:
            logger.warning("未找到 ffprobe，已使用 mutagen 读取上传音频时长")
            return fallback_duration
        raise AudioInspectionError("服务器未安装 ffprobe，且无法读取音频时长") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioInspectionError("读取音频时长超时") from exc

    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        # Do not expose the temporary server path to the browser. The log is
        # intentionally limited to codec/container diagnostics for support.
        logger.warning(
            "ffprobe 无法解析上传音频：returncode=%s，size=%s，diagnostic=%s",
            completed.returncode,
            path.stat().st_size,
            diagnostic[-500:] or "<empty>",
        )
        fallback_duration = _duration_via_mutagen(path)
        if fallback_duration:
            logger.warning("已使用 mutagen 回退读取上传音频时长")
            return fallback_duration
        raise AudioInspectionError(
            "音频无法解析：文件可能已损坏、下载不完整，或实际编码并非受支持的 "
            "MP3/WAV/M4A/AAC/FLAC。请先用播放器转存为标准 MP3 或 WAV 后重试。"
        )
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise AudioInspectionError("音频没有可读取的时长") from exc
    if duration <= 0:
        raise AudioInspectionError("音频时长必须大于 0")
    return duration


def add_silence_tail(
    source: Path,
    target: Path,
    *,
    padding_seconds: float,
) -> None:
    """Create a temporary provider input with a silent editing/generation tail."""

    if padding_seconds <= 0:
        raise ValueError("静音补偿时长必须大于 0")
    duration = inspect_audio_duration(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-af",
            f"apad=pad_dur={padding_seconds:.3f}",
            "-t",
            f"{duration + padding_seconds:.3f}",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
        creationflags=hidden_creation_flags(),
    )
    if completed.returncode != 0 or not target.is_file():
        target.unlink(missing_ok=True)
        raise AudioInspectionError("给数字人音频补尾部静音失败")
