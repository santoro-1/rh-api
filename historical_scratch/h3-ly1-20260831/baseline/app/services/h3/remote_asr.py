from __future__ import annotations

import math
from typing import Any

from app.services.alignment.script_timestamps import AlignedScriptToken


H3_SAFE_CUT_ALIGNMENT_SCHEMA = "jyd.h3-safe-cut-alignment.v1"
H3_AUDIO_ALIGNMENT_RESULT_SCHEMA = "runninghub.h3-audio-alignment-result.v1"
H3_ASR_ACTION_AUDIO_ALIGNMENT = "h3_audio_alignment"
H3_ASR_ACTION_HEAD_TRIM = "h3_head_trim"


def h3_audio_alignment_result_payload(alignment: Any) -> dict[str, object]:
    """Serialize one trusted provider result for the worker callback."""

    return {
        "schema": H3_AUDIO_ALIGNMENT_RESULT_SCHEMA,
        "provider": str(alignment.provider or "funasr_http"),
        "matchRatio": alignment.match_ratio,
        "tokens": [
            {
                "text": token.text,
                "scriptStart": token.script_start,
                "scriptEnd": token.script_end,
                "startSeconds": token.start_seconds,
                "endSeconds": token.end_seconds,
                "confidence": token.confidence,
            }
            for token in alignment.tokens
        ],
    }


def normalize_h3_audio_alignment_result(
    value: object,
    *,
    script_text: str,
    script_sha256: str,
    audio_sha256: str,
    audio_batch_id: str,
    audio_item_id: str,
    audio_generation_version: int,
) -> dict[str, object]:
    """Validate an untrusted worker result and bind it to immutable H3 input."""

    if not isinstance(value, dict):
        raise ValueError("H3 音频对齐结果不是有效对象")
    if str(value.get("schema") or "") != H3_AUDIO_ALIGNMENT_RESULT_SCHEMA:
        raise ValueError("H3 音频对齐结果版本不受支持")
    raw_tokens = value.get("tokens")
    if not isinstance(raw_tokens, list) or not 2 <= len(raw_tokens) <= 10_000:
        raise ValueError("H3 音频对齐结果的字词时间戳数量无效")

    ranges: list[dict[str, int]] = []
    previous_script_end = -1
    previous_audio_end = -1
    for position, raw in enumerate(raw_tokens, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"H3 音频对齐第 {position} 个时间戳无效")
        try:
            script_start = int(raw["scriptStart"])
            script_end = int(raw["scriptEnd"])
            start_seconds = float(raw["startSeconds"])
            end_seconds = float(raw["endSeconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"H3 音频对齐第 {position} 个时间戳无效") from exc
        start_us = round(start_seconds * 1_000_000)
        end_us = round(end_seconds * 1_000_000)
        if (
            not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or script_start < 0
            or script_end <= script_start
            or script_end > len(script_text)
            or not script_text[script_start:script_end].strip()
            or start_us < 0
            or end_us <= start_us
            or script_start < previous_script_end
            or start_us + 250_000 < previous_audio_end
        ):
            raise ValueError(f"H3 音频对齐第 {position} 个时间戳无效")
        ranges.append(
            {
                "script_start": script_start,
                "script_end": script_end,
                "start_us": start_us,
                "end_us": end_us,
            }
        )
        previous_script_end = script_end
        previous_audio_end = end_us

    match_ratio = value.get("matchRatio")
    if match_ratio is not None:
        try:
            match_ratio = float(match_ratio)
        except (TypeError, ValueError) as exc:
            raise ValueError("H3 音频对齐匹配率无效") from exc
        if not math.isfinite(match_ratio) or not 0 <= match_ratio <= 1:
            raise ValueError("H3 音频对齐匹配率无效")

    return {
        "schema": H3_SAFE_CUT_ALIGNMENT_SCHEMA,
        "source": "remote_media_node_funasr",
        "provider": str(value.get("provider") or "funasr_http")[:100],
        "match_ratio": match_ratio,
        "script_sha256": script_sha256,
        "audio_sha256": audio_sha256,
        "audio_batch_id": audio_batch_id,
        "audio_item_id": audio_item_id,
        "audio_generation_version": audio_generation_version,
        "ranges": ranges,
    }


def aligned_tokens_from_safe_cut(
    script_text: str, alignment: dict[str, object]
) -> list[AlignedScriptToken]:
    return [
        AlignedScriptToken(
            text=script_text[int(value["script_start"]) : int(value["script_end"])],
            script_start=int(value["script_start"]),
            script_end=int(value["script_end"]),
            start_seconds=int(value["start_us"]) / 1_000_000,
            end_seconds=int(value["end_us"]) / 1_000_000,
        )
        for value in alignment["ranges"]
    ]


__all__ = [
    "H3_ASR_ACTION_AUDIO_ALIGNMENT",
    "H3_ASR_ACTION_HEAD_TRIM",
    "H3_AUDIO_ALIGNMENT_RESULT_SCHEMA",
    "H3_SAFE_CUT_ALIGNMENT_SCHEMA",
    "aligned_tokens_from_safe_cut",
    "h3_audio_alignment_result_payload",
    "normalize_h3_audio_alignment_result",
]
