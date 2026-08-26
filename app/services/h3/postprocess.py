from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from app.services.alignment.base import AlignmentResult, AudioAlignmentProvider
from app.services.alignment.script_timestamps import tokenize_script
from app.services.media_segmentation import inspect_media_duration
from app.services.processes import hidden_creation_flags


class H3PostprocessError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


H3_OUTPUT_CONTRACT_VERSION = "h3.output.generated-av-head-trim.v3"
H3_HEAD_TRIM_FALLBACK_SECONDS = 0.300
H3_HEAD_TRIM_PREROLL_SECONDS = 0.040
H3_HEAD_TRIM_PREFIX_TOKENS = 3


@dataclass(frozen=True)
class H3HeadTrimDecision:
    mode: str
    trim_seconds: float
    first_script_token_start_seconds: float | None
    alignment_provider: str | None
    alignment_match_ratio: float | None
    matched_prefix_tokens: int
    fallback_reason: str | None


@dataclass(frozen=True)
class H3NormalizedResult:
    video_path: Path
    video_sha256: str
    anchor_path: Path | None
    anchor_sha256: str | None
    head_trim: H3HeadTrimDecision
    normalized_duration_seconds: float


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise H3PostprocessError("服务器未安装 ffmpeg，无法规范化 H3 成片") from exc
    except subprocess.TimeoutExpired as exc:
        raise H3PostprocessError(f"{message}：处理超时") from exc
    if completed.returncode != 0:
        raise H3PostprocessError(message)


def normalize_h3_video(
    source: Path,
    target: Path,
    *,
    trim_start_seconds: float = 0.0,
) -> None:
    """Normalize one H3-generated audiovisual pair after a synchronous head cut."""

    if not source.is_file():
        raise H3PostprocessError("H3 原始成片不存在")
    if not math.isfinite(trim_start_seconds) or trim_start_seconds < 0:
        raise H3PostprocessError("H3 片头裁剪时间不合法")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(source)]
    if trim_start_seconds > 0:
        trim_text = f"{trim_start_seconds:.6f}"
        command.extend(
            [
                "-filter_complex",
                (
                    f"[0:v:0]trim=start={trim_text},setpts=PTS-STARTPTS[v];"
                    f"[0:a:0]atrim=start={trim_text},asetpts=PTS-STARTPTS[a]"
                ),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        )
    else:
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
            ]
        )
    command.extend(["-movflags", "+faststart", str(temporary)])
    try:
        _run(command, "保留 H3 原生音画并重封装失败")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def extract_h3_audio_for_alignment(source: Path, target: Path) -> None:
    """Decode the generated H3 audio to the format accepted by the shared ASR."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ],
            "提取 H3 音轨供 ASR 对齐失败",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def fallback_h3_head_trim(reason: str) -> H3HeadTrimDecision:
    return H3HeadTrimDecision(
        mode="fallback_300ms",
        trim_seconds=H3_HEAD_TRIM_FALLBACK_SECONDS,
        first_script_token_start_seconds=None,
        alignment_provider=None,
        alignment_match_ratio=None,
        matched_prefix_tokens=0,
        fallback_reason=reason,
    )


def decide_h3_head_trim(
    script_text: str,
    alignment: AlignmentResult,
) -> H3HeadTrimDecision:
    """Use consecutive exact script-prefix tokens to find the first real word."""

    clean_script = str(script_text or "").strip()
    expected = tokenize_script(clean_script)
    if not expected:
        return fallback_h3_head_trim("script_has_no_spoken_tokens")
    required = min(H3_HEAD_TRIM_PREFIX_TOKENS, len(expected))
    aligned_by_range = {
        (token.script_start, token.script_end): token for token in alignment.tokens
    }
    prefix = []
    for token in expected[:required]:
        aligned = aligned_by_range.get((token.start_offset, token.end_offset))
        if aligned is None:
            break
        prefix.append(aligned)
    if len(prefix) < required:
        return replace(
            fallback_h3_head_trim("script_prefix_not_matched"),
            alignment_provider=alignment.provider,
            alignment_match_ratio=alignment.match_ratio,
            matched_prefix_tokens=len(prefix),
        )
    first_start = float(prefix[0].start_seconds)
    if not math.isfinite(first_start) or first_start < 0:
        return replace(
            fallback_h3_head_trim("first_token_timestamp_invalid"),
            alignment_provider=alignment.provider,
            alignment_match_ratio=alignment.match_ratio,
            matched_prefix_tokens=len(prefix),
        )
    return H3HeadTrimDecision(
        mode="asr_adaptive",
        trim_seconds=round(max(0.0, first_start - H3_HEAD_TRIM_PREROLL_SECONDS), 6),
        first_script_token_start_seconds=first_start,
        alignment_provider=alignment.provider,
        alignment_match_ratio=alignment.match_ratio,
        matched_prefix_tokens=len(prefix),
        fallback_reason=None,
    )


def detect_h3_head_trim(
    source: Path,
    script_text: str,
    alignment_provider: AudioAlignmentProvider | None,
) -> H3HeadTrimDecision:
    """Run the existing ASR alignment, with a non-failing fixed-cut fallback."""

    if alignment_provider is None:
        return fallback_h3_head_trim("alignment_provider_unavailable")
    alignment_audio = source.with_name(f"{source.stem}.head-align.wav")
    try:
        extract_h3_audio_for_alignment(source, alignment_audio)
        alignment = alignment_provider.align(alignment_audio, script_text)
        return decide_h3_head_trim(script_text, alignment)
    except Exception as exc:  # noqa: BLE001 - every ASR failure deliberately degrades
        logger.warning(
            "H3 片头 ASR 对齐失败，固定裁剪 %.0fms：%s",
            H3_HEAD_TRIM_FALLBACK_SECONDS * 1000,
            exc,
        )
        return replace(
            fallback_h3_head_trim(type(exc).__name__),
            alignment_provider=(
                str(getattr(alignment_provider, "name", "") or "") or None
            ),
        )
    finally:
        alignment_audio.unlink(missing_ok=True)


def h3_head_trim_decision_payload(
    decision: H3HeadTrimDecision,
) -> dict[str, object]:
    return {
        "mode": decision.mode,
        "trimSeconds": decision.trim_seconds,
        "firstScriptTokenStartSeconds": decision.first_script_token_start_seconds,
        "alignmentProvider": decision.alignment_provider,
        "alignmentMatchRatio": decision.alignment_match_ratio,
        "matchedPrefixTokens": decision.matched_prefix_tokens,
        "fallbackReason": decision.fallback_reason,
    }


def parse_h3_head_trim_decision(value: object) -> H3HeadTrimDecision:
    """Strictly validate a trusted-node callback before it controls ffmpeg."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("H3 片头裁剪结果不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("H3 片头裁剪结果格式错误")
    mode = str(value.get("mode") or "")
    if mode not in {"asr_adaptive", "fallback_300ms"}:
        raise ValueError("H3 片头裁剪模式无效")
    try:
        trim_seconds = float(value["trimSeconds"])
        matched_prefix_tokens = int(value.get("matchedPrefixTokens") or 0)
        first_start = (
            float(value["firstScriptTokenStartSeconds"])
            if value.get("firstScriptTokenStartSeconds") is not None
            else None
        )
        match_ratio = (
            float(value["alignmentMatchRatio"])
            if value.get("alignmentMatchRatio") is not None
            else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("H3 片头裁剪数值格式错误") from exc
    if not math.isfinite(trim_seconds) or not 0 <= trim_seconds <= 10:
        raise ValueError("H3 片头裁剪时间超出范围")
    if first_start is not None and (
        not math.isfinite(first_start) or not 0 <= first_start <= 10
    ):
        raise ValueError("H3 首词时间超出范围")
    if match_ratio is not None and (
        not math.isfinite(match_ratio) or not 0 <= match_ratio <= 1
    ):
        raise ValueError("H3 ASR 匹配率超出范围")
    if not 0 <= matched_prefix_tokens <= H3_HEAD_TRIM_PREFIX_TOKENS:
        raise ValueError("H3 原稿前缀匹配数超出范围")
    provider = str(value.get("alignmentProvider") or "").strip() or None
    fallback_reason = str(value.get("fallbackReason") or "").strip() or None
    if mode == "fallback_300ms":
        if abs(trim_seconds - H3_HEAD_TRIM_FALLBACK_SECONDS) > 0.001:
            raise ValueError("H3 降级裁剪时间必须为 300ms")
    else:
        if (
            first_start is None
            or provider is None
            or matched_prefix_tokens < 1
            or fallback_reason is not None
        ):
            raise ValueError("H3 ASR 自适应裁剪结果字段不完整")
        expected_trim = max(0.0, first_start - H3_HEAD_TRIM_PREROLL_SECONDS)
        if abs(trim_seconds - expected_trim) > 0.001:
            raise ValueError("H3 ASR 自适应裁剪时间与首词时间不一致")
    return H3HeadTrimDecision(
        mode=mode,
        trim_seconds=round(trim_seconds, 6),
        first_script_token_start_seconds=first_start,
        alignment_provider=provider,
        alignment_match_ratio=match_ratio,
        matched_prefix_tokens=matched_prefix_tokens,
        fallback_reason=fallback_reason,
    )


def extract_last_visible_frame(source: Path, target: Path) -> None:
    """Extract a frame immediately before EOF from the complete H3 result."""

    if not source.is_file():
        raise H3PostprocessError("H3 规范化成片不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-sseof",
        "-0.050",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        str(temporary),
    ]
    try:
        _run(command, "抽取完整 H3 成片最后可见帧失败")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def extract_reference_frame(source: Path, target: Path) -> None:
    """Extract a deterministic early frame for the multi-image Picture 1 anchor."""

    if not source.is_file():
        raise H3PostprocessError("H3 参考视频不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.part{target.suffix}")
    try:
        for seek_seconds in ("0.500", "0"):
            temporary.unlink(missing_ok=True)
            try:
                _run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-nostdin",
                        "-y",
                        "-ss",
                        seek_seconds,
                        "-i",
                        str(source),
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        str(temporary),
                    ],
                    "抽取 H3 参考视频主画面失败",
                )
                os.replace(temporary, target)
                return
            except H3PostprocessError:
                if seek_seconds == "0":
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def postprocess_h3_result(
    source: Path,
    *,
    script_text: str,
    needs_continuity_anchor: bool,
    alignment_provider: AudioAlignmentProvider | None = None,
    head_trim_decision: H3HeadTrimDecision | None = None,
) -> H3NormalizedResult:
    normalized = source.with_name(f"{source.stem}.normalized.mp4")
    anchor = source.with_name(f"{source.stem}.last-visible.png")
    head_trim = head_trim_decision or detect_h3_head_trim(
        source, script_text, alignment_provider
    )
    normalize_h3_video(
        source,
        normalized,
        trim_start_seconds=head_trim.trim_seconds,
    )
    try:
        if needs_continuity_anchor:
            extract_last_visible_frame(normalized, anchor)
        else:
            anchor = None
        return H3NormalizedResult(
            video_path=normalized,
            video_sha256=_sha256_file(normalized),
            anchor_path=anchor,
            anchor_sha256=_sha256_file(anchor) if anchor else None,
            head_trim=head_trim,
            normalized_duration_seconds=inspect_media_duration(normalized),
        )
    except Exception:
        normalized.unlink(missing_ok=True)
        if anchor is not None:
            anchor.unlink(missing_ok=True)
        raise
