from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import shutil
import subprocess
import threading

from .bgm_loudness import _ffmpeg_path


LOGGER = logging.getLogger(__name__)

_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


class BrowserPreviewError(RuntimeError):
    """Raised when an incompatible source cannot be converted for browsers."""


def _ffprobe_path() -> str | None:
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


def _browser_compatibility(path: Path) -> bool | None:
    """Return whether an MP4 has the conservative Chrome/Edge playback profile.

    ``None`` means the local installation cannot inspect this file. In that case
    callers preserve the old direct-stream behaviour instead of breaking a
    previously playable asset.
    """

    ffprobe = _ffprobe_path()
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        LOGGER.warning("浏览器预览视频探测失败，将直接返回原文件: path=%s", path)
        return None
    try:
        streams = json.loads(completed.stdout or "{}").get("streams", [])
    except (TypeError, ValueError):
        return None
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    if not isinstance(video, dict):
        return None
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"), None
    )
    return (
        str(video.get("codec_name") or "").lower() == "h264"
        and str(video.get("pix_fmt") or "").lower() == "yuv420p"
        and (
            not isinstance(audio, dict)
            or str(audio.get("codec_name") or "").lower() == "aac"
        )
    )


def _cache_lock(path: Path) -> threading.Lock:
    key = str(path).lower()
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def browser_preview_path(source: str | Path, cache_root: str | Path) -> Path:
    """Return a cached H.264/yuv420p proxy, or the original compatible MP4."""

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"基础视频文件不存在: {source_path}")
    compatibility = _browser_compatibility(source_path)
    if compatibility is not False:
        return source_path

    stat = source_path.stat()
    cache_key = hashlib.sha256(
        f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:20]
    target_root = Path(cache_root).expanduser().resolve()
    target = target_root / f"{cache_key}.mp4"
    with _cache_lock(target):
        if target.is_file() and target.stat().st_size > 0:
            return target
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            raise BrowserPreviewError("未找到 FFmpeg，无法转换浏览器不兼容的视频")
        target_root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.stem}.{threading.get_ident()}.tmp.mp4")
        temporary.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-vf",
                    "scale=720:-2:force_original_aspect_ratio=decrease,format=yuv420p",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    str(temporary),
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=1200,
            )
            if completed.returncode != 0 or not temporary.is_file():
                detail = (completed.stderr or completed.stdout or "未知错误").strip()
                raise BrowserPreviewError(f"浏览器预览视频转换失败: {detail[-500:]}")
            temporary.replace(target)
        except subprocess.TimeoutExpired as exc:
            raise BrowserPreviewError("浏览器预览视频转换超时") from exc
        finally:
            temporary.unlink(missing_ok=True)
    return target
