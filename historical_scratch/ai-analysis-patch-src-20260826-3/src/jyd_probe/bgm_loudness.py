from __future__ import annotations

from functools import lru_cache
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


LOGGER = logging.getLogger(__name__)

BGM_TARGET_GAP_DB = 11.0
BGM_MIN_VOLUME = 0.08
BGM_MAX_VOLUME = 0.25
BGM_FALLBACK_VOLUME = 0.18
BGM_STRONG_VOCAL_EXTRA_GAP_DB = 4.0
BGM_STRONG_VOCAL_MIN_VOLUME = 0.05
BGM_STRONG_VOCAL_MAX_VOLUME = 0.16
BGM_VOLUME_ALGORITHM = "speech-relative-lufs.v1"


def volume_from_loudness(
    voice_lufs: float,
    bgm_lufs: float,
    *,
    target_gap_db: float = BGM_TARGET_GAP_DB,
    minimum_volume: float = BGM_MIN_VOLUME,
    maximum_volume: float = BGM_MAX_VOLUME,
) -> float:
    """Return a bounded linear BGM gain relative to the narration loudness."""

    desired_bgm_lufs = float(voice_lufs) - float(target_gap_db)
    gain_db = desired_bgm_lufs - float(bgm_lufs)
    volume = math.pow(10.0, gain_db / 20.0)
    return round(max(float(minimum_volume), min(float(maximum_volume), volume)), 4)


def fallback_bgm_volume(*, strong_vocals: bool = False) -> float:
    if not strong_vocals:
        return BGM_FALLBACK_VOLUME
    reduced = BGM_FALLBACK_VOLUME * math.pow(
        10.0, -BGM_STRONG_VOCAL_EXTRA_GAP_DB / 20.0
    )
    return round(
        max(BGM_STRONG_VOCAL_MIN_VOLUME, min(BGM_STRONG_VOCAL_MAX_VOLUME, reduced)),
        4,
    )


def _ffmpeg_path() -> str | None:
    configured = str(os.environ.get("JYD_FFMPEG") or "").strip()
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser().resolve())
    discovered = shutil.which("ffmpeg")
    if discovered:
        return str(Path(discovered).resolve())
    executable_root = Path(sys.executable).resolve().parent
    project_root = Path(__file__).resolve().parents[2]
    candidates = (
        executable_root / "ffmpeg" / "bin" / "ffmpeg.exe",
        executable_root.parent / "ffmpeg" / "bin" / "ffmpeg.exe",
        project_root / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path.cwd() / "ffmpeg" / "bin" / "ffmpeg.exe",
    )
    return next((str(path.resolve()) for path in candidates if path.is_file()), None)


@lru_cache(maxsize=512)
def _measure_integrated_lufs_cached(
    resolved_path: str,
    size_bytes: int,
    modified_ns: int,
    ffmpeg_path: str,
) -> float:
    _ = (size_bytes, modified_ns)
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            resolved_path,
            "-vn",
            "-sn",
            "-dn",
            "-af",
            "loudnorm=I=-24:TP=-2:LRA=11:print_format=json",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    matches = re.findall(
        r"\{\s*\"input_i\".*?\}", completed.stderr or "", re.DOTALL
    )
    if not matches:
        raise ValueError("FFmpeg 未返回响度分析结果")
    payload = json.loads(matches[-1])
    value = float(payload["input_i"])
    if not math.isfinite(value):
        raise ValueError("音频响度不是有限数值")
    return value


def measure_integrated_lufs(path: str | Path) -> float:
    media_path = Path(path).expanduser().resolve()
    if not media_path.is_file():
        raise ValueError(f"音频文件不存在：{media_path}")
    ffmpeg_path = _ffmpeg_path()
    if not ffmpeg_path:
        raise ValueError("未找到 FFmpeg，无法分析音频响度")
    stat = media_path.stat()
    return _measure_integrated_lufs_cached(
        str(media_path), stat.st_size, stat.st_mtime_ns, ffmpeg_path
    )


def automatic_bgm_mix(
    voice_path: str | Path,
    bgm_path: str | Path,
    *,
    strong_vocals: bool = False,
) -> dict[str, Any]:
    """Measure narration/music and return one authoritative preview/export gain."""

    target_gap_db = BGM_TARGET_GAP_DB + (
        BGM_STRONG_VOCAL_EXTRA_GAP_DB if strong_vocals else 0.0
    )
    minimum_volume = BGM_STRONG_VOCAL_MIN_VOLUME if strong_vocals else BGM_MIN_VOLUME
    maximum_volume = BGM_STRONG_VOCAL_MAX_VOLUME if strong_vocals else BGM_MAX_VOLUME
    try:
        voice_lufs = measure_integrated_lufs(voice_path)
        bgm_lufs = measure_integrated_lufs(bgm_path)
        return {
            "algorithm": BGM_VOLUME_ALGORITHM,
            "volume": volume_from_loudness(
                voice_lufs,
                bgm_lufs,
                target_gap_db=target_gap_db,
                minimum_volume=minimum_volume,
                maximum_volume=maximum_volume,
            ),
            "voice_lufs": round(voice_lufs, 2),
            "bgm_lufs": round(bgm_lufs, 2),
            "target_gap_db": target_gap_db,
            "strong_vocals": strong_vocals,
            "fallback": False,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.warning("BGM 响度分析失败，使用保守自动音量：%s", exc)
        return {
            "algorithm": BGM_VOLUME_ALGORITHM,
            "volume": fallback_bgm_volume(strong_vocals=strong_vocals),
            "target_gap_db": target_gap_db,
            "strong_vocals": strong_vocals,
            "fallback": True,
            "reason": str(exc)[:300],
        }
