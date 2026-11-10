from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.services.media_segmentation import (
    MAX_SEGMENT_SECONDS,
    TARGET_SEGMENT_SECONDS,
    MediaSegmentationError,
    SegmentPlan,
)


_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff]|"
    r"[A-Za-z]+(?:'[A-Za-z]+)?|"
    r"\d+(?:\.\d+)?"
)
_STRONG_BREAKS = frozenset("。！？!?；;\n")
_WEAK_BREAKS = frozenset("，,、：:")
_MIN_SEGMENT_SECONDS = 12.0
_MIN_ALIGNMENT_RATIO = 0.45


@dataclass(frozen=True)
class RecognizedToken:
    text: str
    start_seconds: float
    end_seconds: float
    confidence: float | None = None


@dataclass(frozen=True)
class AlignedScriptToken:
    text: str
    script_start: int
    script_end: int
    start_seconds: float
    end_seconds: float
    confidence: float | None = None


@dataclass(frozen=True)
class ScriptAlignment:
    plans: tuple[SegmentPlan, ...]
    tokens: tuple[AlignedScriptToken, ...]
    match_ratio: float


@dataclass(frozen=True)
class _ScriptToken:
    key: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class _Boundary:
    script_offset: int
    time_seconds: float
    penalty: float
    method: str


def _token_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def tokenize_script(script: str) -> list[_ScriptToken]:
    return [
        _ScriptToken(
            key=_token_key(match.group(0)),
            start_offset=match.start(),
            end_offset=match.end(),
        )
        for match in _TOKEN_RE.finditer(script)
    ]


def _validate_recognized_tokens(
    tokens: list[RecognizedToken] | tuple[RecognizedToken, ...],
    duration_seconds: float,
) -> list[RecognizedToken]:
    if not tokens:
        raise MediaSegmentationError("ASR 没有返回字词时间戳")
    ordered = sorted(tokens, key=lambda token: (token.start_seconds, token.end_seconds))
    for position, token in enumerate(ordered, start=1):
        if (
            not _token_key(token.text)
            or token.start_seconds < -0.1
            or token.end_seconds <= token.start_seconds
            or token.end_seconds > duration_seconds + 1.0
            or not math.isfinite(token.start_seconds)
            or not math.isfinite(token.end_seconds)
        ):
            raise MediaSegmentationError(f"ASR 第 {position} 个字词时间戳无效")
    return ordered


def _script_to_asr_mapping(
    script_tokens: list[_ScriptToken],
    recognized_tokens: list[RecognizedToken],
) -> tuple[dict[int, int], float]:
    matcher = SequenceMatcher(
        None,
        [token.key for token in script_tokens],
        [_token_key(token.text) for token in recognized_tokens],
        autojunk=False,
    )
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    ratio = len(mapping) / max(len(script_tokens), 1)
    if ratio < _MIN_ALIGNMENT_RATIO:
        raise MediaSegmentationError(
            "ASR 识别结果与原脚本差异过大"
            f"（仅对齐 {ratio:.0%}），请确认音频和脚本内容一致"
        )
    return mapping, ratio


def _boundary_time(
    token_count: int,
    script_token_count: int,
    recognized_tokens: list[RecognizedToken],
    mapping: dict[int, int],
    duration_seconds: float,
) -> tuple[float, str] | None:
    if token_count <= 0:
        return 0.0, "asr_timestamp"
    if token_count >= script_token_count:
        return duration_seconds, "asr_timestamp"

    left_script_index = next(
        (
            index
            for index in range(token_count - 1, -1, -1)
            if index in mapping
        ),
        None,
    )
    right_script_index = next(
        (
            index
            for index in range(token_count, script_token_count)
            if index in mapping
        ),
        None,
    )
    if left_script_index is None and right_script_index is None:
        return None

    exact = (
        left_script_index == token_count - 1
        and right_script_index == token_count
    )
    if left_script_index is not None and right_script_index is not None:
        left = recognized_tokens[mapping[left_script_index]]
        right = recognized_tokens[mapping[right_script_index]]
        if right.start_seconds >= left.end_seconds:
            timestamp = (left.end_seconds + right.start_seconds) / 2
        else:
            timestamp = (left.end_seconds + right.start_seconds) / 2
    elif left_script_index is not None:
        timestamp = recognized_tokens[mapping[left_script_index]].end_seconds
    else:
        timestamp = recognized_tokens[mapping[right_script_index]].start_seconds

    return (
        min(max(timestamp, 0.0), duration_seconds),
        "asr_timestamp" if exact else "asr_interpolated",
    )


def _candidate_offsets(
    script: str,
    script_tokens: list[_ScriptToken],
) -> dict[int, tuple[int, float]]:
    """Map spoken-token counts to the best original-script boundary."""

    candidates: dict[int, tuple[int, float]] = {}
    token_cursor = 0
    for offset, char in enumerate(script, start=1):
        while (
            token_cursor < len(script_tokens)
            and script_tokens[token_cursor].end_offset <= offset
        ):
            token_cursor += 1
        if char in _STRONG_BREAKS:
            candidates[token_cursor] = (offset, 0.0)
        elif char in _WEAK_BREAKS:
            current = candidates.get(token_cursor)
            if current is None or current[1] > 4.0:
                candidates[token_cursor] = (offset, 4.0)

    # Every recognized token boundary remains a safe acoustic fallback. Its
    # large penalty means punctuation wins whenever both produce valid lengths.
    for token_count, token in enumerate(script_tokens[:-1], start=1):
        candidates.setdefault(token_count, (token.end_offset, 100.0))
    return candidates


def plan_script_aligned_segments(
    script: str,
    duration_seconds: float,
    recognized_tokens: list[RecognizedToken] | tuple[RecognizedToken, ...],
) -> list[SegmentPlan]:
    """Align ASR timestamps to the original script and plan gap-free cuts."""

    return list(
        align_script_timeline(script, duration_seconds, recognized_tokens).plans
    )


def align_script_timeline(
    script: str,
    duration_seconds: float,
    recognized_tokens: list[RecognizedToken] | tuple[RecognizedToken, ...],
) -> ScriptAlignment:
    """Return segment plans plus ASR timestamps bound to original-script offsets."""

    clean_script = script.strip()
    if not clean_script:
        raise MediaSegmentationError("原脚本不能为空")
    if duration_seconds <= 0:
        raise MediaSegmentationError("音频时长必须大于 0")
    script_tokens = tokenize_script(clean_script)
    if not script_tokens:
        raise MediaSegmentationError("原脚本没有可对齐的文字")
    ordered_recognized = _validate_recognized_tokens(
        recognized_tokens,
        duration_seconds,
    )
    mapping, ratio = _script_to_asr_mapping(
        script_tokens,
        ordered_recognized,
    )

    boundaries = [
        _Boundary(
            script_offset=0,
            time_seconds=0.0,
            penalty=0.0,
            method="asr_timestamp",
        )
    ]
    for token_count, (script_offset, penalty) in _candidate_offsets(
        clean_script,
        script_tokens,
    ).items():
        resolved = _boundary_time(
            token_count,
            len(script_tokens),
            ordered_recognized,
            mapping,
            duration_seconds,
        )
        if resolved is None:
            continue
        timestamp, method = resolved
        if 0.05 < timestamp < duration_seconds - 0.05:
            boundaries.append(
                _Boundary(
                    script_offset=script_offset,
                    time_seconds=timestamp,
                    penalty=penalty,
                    method=method,
                )
            )
    boundaries.append(
        _Boundary(
            script_offset=len(clean_script),
            time_seconds=duration_seconds,
            penalty=0.0,
            method="asr_timestamp",
        )
    )
    boundaries.sort(key=lambda boundary: (boundary.time_seconds, boundary.script_offset))

    monotonic: list[_Boundary] = []
    for boundary in boundaries:
        if monotonic and boundary.script_offset <= monotonic[-1].script_offset:
            if (
                boundary.script_offset == monotonic[-1].script_offset
                and boundary.penalty < monotonic[-1].penalty
            ):
                monotonic[-1] = boundary
            continue
        monotonic.append(boundary)
    boundaries = monotonic

    costs = [float("inf")] * len(boundaries)
    previous: list[int | None] = [None] * len(boundaries)
    costs[0] = 0.0
    final_index = len(boundaries) - 1
    for end_index in range(1, len(boundaries)):
        for start_index in range(end_index - 1, -1, -1):
            segment_duration = (
                boundaries[end_index].time_seconds
                - boundaries[start_index].time_seconds
            )
            if segment_duration > MAX_SEGMENT_SECONDS + 0.01:
                break
            if segment_duration <= 0:
                continue
            if (
                segment_duration < _MIN_SEGMENT_SECONDS
                and end_index != final_index
                and start_index != 0
            ):
                continue
            score = (
                costs[start_index]
                + (segment_duration - TARGET_SEGMENT_SECONDS) ** 2
                + boundaries[end_index].penalty
            )
            if score < costs[end_index]:
                costs[end_index] = score
                previous[end_index] = start_index

    if previous[final_index] is None:
        raise MediaSegmentationError(
            f"ASR 时间轴中存在无法控制在 {MAX_SEGMENT_SECONDS:g} 秒内的连续内容"
        )

    groups: list[tuple[int, int]] = []
    cursor = final_index
    while cursor:
        start = previous[cursor]
        if start is None:
            raise MediaSegmentationError("无法根据 ASR 时间轴规划分段")
        groups.append((start, cursor))
        cursor = start
    groups.reverse()

    plans = [
        SegmentPlan(
            index=index,
            script_text=clean_script[
                boundaries[start].script_offset : boundaries[end].script_offset
            ],
            start_seconds=boundaries[start].time_seconds,
            end_seconds=boundaries[end].time_seconds,
            alignment_method=boundaries[end].method,
        )
        for index, (start, end) in enumerate(groups, start=1)
    ]
    aligned_tokens = tuple(
        AlignedScriptToken(
            text=clean_script[
                script_tokens[script_index].start_offset :
                script_tokens[script_index].end_offset
            ],
            script_start=script_tokens[script_index].start_offset,
            script_end=script_tokens[script_index].end_offset,
            start_seconds=ordered_recognized[recognized_index].start_seconds,
            end_seconds=ordered_recognized[recognized_index].end_seconds,
            confidence=ordered_recognized[recognized_index].confidence,
        )
        for script_index, recognized_index in sorted(mapping.items())
    )
    return ScriptAlignment(
        plans=tuple(plans),
        tokens=aligned_tokens,
        match_ratio=ratio,
    )
