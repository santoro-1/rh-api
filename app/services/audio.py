from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import threading
import uuid
import wave
from pathlib import Path

from mutagen import File as MutagenFile

from app.services.processes import hidden_creation_flags


logger = logging.getLogger(__name__)


# Keep the provider/user voice setting intact, then apply a small, predictable
# mastering lift to the completed spoken track.  The limiter protects louder
# syllables without normalising every script to an unnaturally identical level.
GENERATED_SPEECH_GAIN_DB = 3.0
GENERATED_SPEECH_PEAK_DBFS = -1.0
GENERATED_SPEECH_MASTERING_VERSION = "speech-plus3db-peak-minus1-v1"
_GENERATED_SPEECH_LOCKS_GUARD = threading.Lock()
_GENERATED_SPEECH_LOCKS: dict[str, threading.Lock] = {}


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


def master_generated_speech(
    source: Path,
    target: Path,
    *,
    gain_db: float = GENERATED_SPEECH_GAIN_DB,
    peak_dbfs: float = GENERATED_SPEECH_PEAK_DBFS,
) -> None:
    """Lift one generated speech file and cap peaks without changing timing.

    The result is written atomically so an interrupted ffmpeg process cannot
    leave a partial file at the path later handed to RunningHub or the local
    workbench.
    """

    if not source.is_file() or source.stat().st_size <= 0:
        raise AudioInspectionError("待增强的口播音频不存在或为空")
    if gain_db < 0:
        raise ValueError("口播增益不能小于 0 dB")
    if not -20.0 <= peak_dbfs < 0.0:
        raise ValueError("口播峰值上限必须在 -20 dBFS 到 0 dBFS 之间")

    codec_args = {
        ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
        ".wav": ["-c:a", "pcm_s16le"],
        ".flac": ["-c:a", "flac"],
    }.get(target.suffix.lower())
    if codec_args is None:
        raise AudioInspectionError("口播增强只支持 MP3、WAV 或 FLAC")

    peak_linear = 10 ** (peak_dbfs / 20.0)
    audio_filter = (
        f"volume={gain_db:.3f}dB,"
        f"alimiter=limit={peak_linear:.9f}:attack=5:release=50:"
        "level=false:latency=true"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.stem}.mastering-{uuid.uuid4().hex}{target.suffix}"
    )
    try:
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(source),
                    "-map_metadata",
                    "-1",
                    "-vn",
                    "-af",
                    audio_filter,
                    *codec_args,
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
                creationflags=hidden_creation_flags(),
            )
        except FileNotFoundError as exc:
            raise AudioInspectionError(
                "服务器未安装 ffmpeg，无法增强口播音量"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioInspectionError("增强口播音量超时") from exc
        if (
            completed.returncode != 0
            or not temporary.is_file()
            or temporary.stat().st_size <= 0
        ):
            diagnostic = (completed.stderr or completed.stdout).strip().replace(
                "\n", " "
            )
            logger.warning(
                "ffmpeg 口播增强失败：returncode=%s，diagnostic=%s",
                completed.returncode,
                diagnostic[-500:] or "<empty>",
            )
            raise AudioInspectionError("提升口播音量失败")

        # Validate the temporary result before replacing a previous version.
        inspect_audio_duration(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _generated_speech_mastering_marker(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.{GENERATED_SPEECH_MASTERING_VERSION}.mastered"
    )


def mark_generated_speech_mastered(path: Path) -> None:
    """Record the exact mastered file so a later download never boosts it twice."""

    stat = path.stat()
    marker = _generated_speech_mastering_marker(path)
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            f"{GENERATED_SPEECH_MASTERING_VERSION}\n{stat.st_size}\n{stat.st_mtime_ns}\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def generated_speech_is_mastered(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    marker = _generated_speech_mastering_marker(path)
    try:
        version, raw_size, raw_mtime = marker.read_text(encoding="utf-8").splitlines()
        stat = path.stat()
        return (
            version == GENERATED_SPEECH_MASTERING_VERSION
            and int(raw_size) == stat.st_size
            and int(raw_mtime) == stat.st_mtime_ns
        )
    except (FileNotFoundError, OSError, ValueError):
        return False


def ensure_generated_speech_mastered(path: Path) -> bool:
    """Master one legacy generated file once; return whether it was changed."""

    lock_key = str(path.resolve())
    with _GENERATED_SPEECH_LOCKS_GUARD:
        lock = _GENERATED_SPEECH_LOCKS.setdefault(lock_key, threading.Lock())
    with lock:
        if generated_speech_is_mastered(path):
            return False
        master_generated_speech(path, path)
        mark_generated_speech_mastered(path)
        return True


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
