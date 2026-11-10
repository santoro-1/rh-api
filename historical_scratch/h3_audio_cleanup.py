"""Local, equal-length H3 head cleanup. Provider files are never modified."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any
import wave

import numpy as np

from .caption_alignment import RecognizedToken, _TOKEN_RE, _key
from .project_h3_media import H3MediaError, _run


H3_AUDIO_CLEANUP_VERSION = "jyd.h3-head-silence.v1"


@dataclass(frozen=True)
class HeadCleanupConfig:
    version: str = H3_AUDIO_CLEANUP_VERSION
    window_ms: int = 10
    hop_ms: int = 5
    quiet_ms: int = 20
    fade_ms: int = 10
    quiet_below_speech_db: float = 25.0
    max_head_seconds: float = 5.0


DEFAULT_CONFIG = HeadCleanupConfig()


@dataclass(frozen=True)
class HeadGate:
    anchor_sample: int
    mute_until_sample: int
    restore_at_sample: int
    reason: str


@dataclass(frozen=True)
class CleanedSegment:
    directory: Path
    key: str
    raw_sha256: str
    audio_path: Path
    preview_path: Path
    report: dict[str, Any]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cleanup_key(raw_sha256: str, script: str, config: HeadCleanupConfig = DEFAULT_CONFIG) -> str:
    data = {"raw_sha256": raw_sha256, "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(), "config": asdict(config)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def source_sha256(source: Path) -> str:
    # The download cache publishes current.json only after hash verification.
    try:
        value = json.loads(source.with_suffix(".json").read_text(encoding="utf-8"))
        digest = str(value.get("local_video_sha256") or "")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    except (OSError, ValueError, TypeError):
        pass
    return file_sha256(source)


def _cache_directory(source: Path, script: str, config: HeadCleanupConfig) -> tuple[Path, str, str]:
    digest = source_sha256(source)
    key = cleanup_key(digest, script, config)
    return source.parent / "head-cleanup" / key, key, digest


def read_cleanup(source: Path, script: str, config: HeadCleanupConfig = DEFAULT_CONFIG) -> CleanedSegment | None:
    directory, key, digest = _cache_directory(source, script, config)
    try:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        audio, preview = directory / "clean.wav", directory / "preview.mp4"
        if (report.get("key") == key and report.get("version") == config.version
                and audio.stat().st_size == report.get("audio_bytes")
                and preview.stat().st_size == report.get("preview_bytes")):
            return CleanedSegment(directory, key, digest, audio, preview, report)
    except (OSError, ValueError, TypeError):
        pass
    return None


def prefix_anchor(script: str, tokens: list[RecognizedToken]) -> tuple[int, int]:
    expected = [_key(match.group()) for match in _TOKEN_RE.finditer(script)][:3]
    if not expected:
        raise H3MediaError("H3 片头清理缺少有效分段台词")
    # A model noise may produce up to two spurious leading tokens. Never use a
    # later repeated sentence or any interpolated subtitle timestamp as anchor.
    for start in range(min(3, len(tokens))):
        matched = tokens[start:start + len(expected)]
        if [token.key for token in matched] == expected:
            return matched[0].start_us, matched[-1].end_us
    raise H3MediaError("H3 片头台词未与本地 ASR 精确匹配，保留原片，等待本地重试")


def plan_head_gate(pcm: np.ndarray, sample_rate: int, script: str,
                   tokens: list[RecognizedToken], config: HeadCleanupConfig = DEFAULT_CONFIG) -> HeadGate:
    if pcm.ndim != 2 or not len(pcm) or pcm.dtype != np.int16 or sample_rate <= 0:
        raise H3MediaError("H3 片头清理 PCM 格式无效")
    anchor_us, phrase_end_us = prefix_anchor(script, tokens)
    anchor = round(anchor_us * sample_rate / 1_000_000)
    phrase_end = min(len(pcm), round(phrase_end_us * sample_rate / 1_000_000))
    if anchor < 0 or anchor >= phrase_end or anchor_us > config.max_head_seconds * 1_000_000:
        raise H3MediaError("H3 片头 ASR 时间戳超出有效范围，未静音原片")
    window = max(1, round(sample_rate * config.window_ms / 1000))
    hop = max(1, round(sample_rate * config.hop_ms / 1000))
    fade = max(2, round(sample_rate * config.fade_ms / 1000))
    quiet_length = max(window, fade, round(sample_rate * config.quiet_ms / 1000))
    # Max channel RMS, not downmix: opposite-phase stereo impulses must count.
    signal = pcm[:phrase_end].astype(np.float64) / 32768.0
    def rms(start: int, end: int) -> float:
        return float(np.sqrt(np.mean(signal[start:end] ** 2, axis=0)).max())
    speech_levels = [rms(i, min(i + window, phrase_end)) for i in range(anchor, phrase_end, hop)]
    typical = float(np.percentile(speech_levels, 70))
    if typical <= 1 / 32768:
        raise H3MediaError("H3 ASR 开口位置没有有效语音能量，未静音原片")
    threshold = max(1 / 32768, min(0.01, typical * 10 ** (-config.quiet_below_speech_db / 20)))
    quiet_start: int | None = None
    restore = 0
    for start in range(0, max(0, anchor - window + 1), hop):
        if rms(start, start + window) <= threshold:
            if quiet_start is None:
                quiet_start = start
            end = start + window
            if end - quiet_start >= quiet_length:
                restore = end
        else:
            quiet_start = None
    if not restore:
        # Immediate speech / no preceding gap: no blind fixed-duration mute.
        return HeadGate(anchor, 0, 0, "NO_HEAD_GAP")
    return HeadGate(anchor, max(0, restore - fade), restore, "HEAD_GAP_FOUND")


def apply_head_gate(pcm: np.ndarray, gate: HeadGate) -> np.ndarray:
    result = pcm.copy()
    start, end = gate.mute_until_sample, gate.restore_at_sample
    if not (0 <= start <= end <= len(pcm)):
        raise H3MediaError("H3 静音区间超出 PCM 范围")
    result[:start] = 0
    if end > start:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, end - start))
        result[start:end] = np.rint(pcm[start:end].astype(np.float64) * ramp[:, None]).astype(np.int16)
    return result


def _read_pcm(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as stream:
        if stream.getsampwidth() != 2 or stream.getcomptype() != "NONE":
            raise H3MediaError("H3 音频不是 16-bit PCM")
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").reshape(-1, stream.getnchannels()).copy(), stream.getframerate()


def _write_pcm(path: Path, pcm: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(pcm.shape[1])
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.astype("<i2", copy=False).tobytes())


def clean_segment(source: Path, script: str, aligner: Any, *,
                  config: HeadCleanupConfig = DEFAULT_CONFIG) -> CleanedSegment:
    existing = read_cleanup(source, script, config)
    if existing is not None:
        return existing
    if aligner is None or not callable(getattr(aligner, "recognize_tokens", None)):
        raise H3MediaError("H3 片头清理需要本地 ASR 字词时间戳服务")
    directory, key, digest = _cache_directory(source, script, config)
    directory.mkdir(parents=True, exist_ok=True)
    # Take an immutable snapshot: a concurrent paid regeneration can replace
    # current.mp4 while this local worker is still running on the old version.
    with tempfile.TemporaryDirectory(prefix="build-", dir=directory) as temporary:
        work = Path(temporary)
        raw = work / "raw.mp4"
        shutil.copyfile(source, raw)
        if file_sha256(raw) != digest:
            raise H3MediaError("H3 原片版本已变化，等待下一次本地清理")
        native, analysis = work / "native.wav", work / "analysis.wav"
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(raw), "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(native)], "解码 H3 原生音频失败")
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(native), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(analysis)], "准备 H3 本地 ASR 音频失败")
        tokens = aligner.recognize_tokens(analysis)
        pcm, rate = _read_pcm(native)
        gate = plan_head_gate(pcm, rate, script, tokens, config)
        cleaned = apply_head_gate(pcm, gate)
        audio, preview = work / "clean.wav", work / "preview.mp4"
        _write_pcm(audio, cleaned, rate)
        # Audio timestamps relative to video are retained, not reset separately.
        from .project_h3_media import _probe_av_starts
        video_start, audio_start = _probe_av_starts(raw)
        offset = audio_start - video_start
        _run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(raw), "-itsoffset", f"{offset:.9f}", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(preview)], "生成 H3 清理版预览失败")
        report = {"key": key, "version": config.version, "config": asdict(config),
                  "raw_sha256": digest, "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                  "sample_rate": rate, "channels": pcm.shape[1], "sample_count": len(pcm),
                  "gate": asdict(gate), "audio_offset_seconds": offset,
                  "muted_until_seconds": gate.mute_until_sample / rate,
                  "restored_at_seconds": gate.restore_at_sample / rate,
                  "anchor_seconds": gate.anchor_sample / rate,
                  "changed_samples": int(np.count_nonzero(cleaned != pcm)),
                  "speech_pcm_unchanged": bool(np.array_equal(cleaned[gate.restore_at_sample:], pcm[gate.restore_at_sample:])),
                  "audio_bytes": audio.stat().st_size, "preview_bytes": preview.stat().st_size}
        report_path = work / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in ("clean.wav", "preview.mp4", "report.json"):
            os.replace(work / name, directory / name)
    result = read_cleanup(source, script, config)
    if result is None:
        raise H3MediaError("H3 清理缓存尚未就绪或原片版本已变化")
    return result


# Factories create coordinators per HTTP request. Keep a shared, bounded local
# queue; workers publish only derivative files, never cloud tasks or database rows.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="h3-head-cleanup")
_LOCK = threading.Lock()
_PENDING: set[str] = set()
_MAX_PENDING = 16


def request_cleanup(source: Path, script: str, aligner: Any, *, force_retry: bool = False) -> dict[str, Any]:
    directory, key, _ = _cache_directory(source, script, DEFAULT_CONFIG)
    ready = read_cleanup(source, script)
    state: dict[str, Any] = {"key": key, "version": H3_AUDIO_CLEANUP_VERSION}
    if ready is not None:
        return {**state, "status": "READY", "muted_until_seconds": ready.report["muted_until_seconds"], "restored_at_seconds": ready.report["restored_at_seconds"]}
    identity = str(directory.resolve())
    failure_path = directory / "failure.json"
    with _LOCK:
        if identity in _PENDING:
            return {**state, "status": "PROCESSING"}
        try:
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            failure = {}
        attempts = 0 if force_retry else int(failure.get("attempts") or 0)
        if not force_retry and attempts >= 3:
            return {**state, "status": "FAILED", "error": failure.get("error", "本地清理失败")}
        if not force_retry and time.time() < float(failure.get("retry_at") or 0):
            return {**state, "status": "RETRY_WAIT", "error": failure.get("error", "等待本地重试")}
        if len(_PENDING) >= _MAX_PENDING:
            return {**state, "status": "PENDING"}
        _PENDING.add(identity)
    def work() -> None:
        try:
            clean_segment(source, script, aligner)
            failure_path.unlink(missing_ok=True)
        except Exception as exc:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".json", delete=False) as stream:
                json.dump({"attempts": attempts + 1, "retry_at": time.time() + 60, "error": str(exc)[:500]}, stream, ensure_ascii=False)
                temporary_failure = Path(stream.name)
            os.replace(temporary_failure, failure_path)
        finally:
            with _LOCK:
                _PENDING.discard(identity)
    try:
        _EXECUTOR.submit(work)
    except RuntimeError:
        with _LOCK:
            _PENDING.discard(identity)
        return {**state, "status": "PENDING"}
    return {**state, "status": "PROCESSING"}
