from __future__ import annotations

import math
import re
from dataclasses import dataclass


STRONG_END = re.compile(r"[。！？!?][”’\"']?$")
MEDIUM_END = re.compile(r"[；;：:][”’\"']?$")
WEAK_END = re.compile(r"[，,、][”’\"']?$")


@dataclass(frozen=True)
class RawCue:
    text: str
    start_us: int
    end_us: int


@dataclass(frozen=True)
class SegmentPlan:
    segment_index: int
    cue_start_index: int
    cue_end_index: int
    start_us: int
    end_us: int
    audio_duration_seconds: float
    requested_generation_duration_seconds: float
    text: str
    boundary_strength: str


def _boundary_penalty(text: str, is_final: bool) -> tuple[float, str]:
    if is_final or STRONG_END.search(text):
        return 0.0, "strong"
    if MEDIUM_END.search(text):
        return 2.0, "medium"
    if WEAK_END.search(text):
        return 5.0, "weak"
    return 9.0, "fallback"


def _normalized_script(value: str) -> str:
    return "".join(str(value or "").split())


def _validated_cues(cues: list[RawCue], audio_duration_us: int) -> None:
    if not cues:
        raise ValueError("H3 分段缺少 raw cues")
    if isinstance(audio_duration_us, bool) or audio_duration_us <= 0:
        raise ValueError("H3 完整音频时长不合法")
    previous_start = -1
    previous_end = 0
    for index, cue in enumerate(cues):
        if not cue.text or cue.start_us < 0 or cue.end_us <= cue.start_us:
            raise ValueError(f"H3 raw cue {index} 不合法")
        if cue.start_us < previous_start or cue.end_us < previous_end:
            raise ValueError("H3 raw cues 时间必须单调递增")
        if cue.end_us > audio_duration_us:
            raise ValueError("H3 raw cue 超出完整音频时长")
        previous_start = cue.start_us
        previous_end = cue.end_us


def plan_segments(
    script_text: str,
    cues: list[RawCue],
    *,
    audio_duration_us: int,
    generation_tail_seconds: float = 0.5,
    target_min_seconds: float = 8.0,
    target_max_seconds: float = 12.0,
    request_min_seconds: float = 4.0,
    request_max_seconds: float = 15.0,
) -> list[SegmentPlan]:
    """Split on raw-cue boundaries using global cost instead of greedy tail repair."""

    _validated_cues(cues, audio_duration_us)
    if _normalized_script(script_text) != _normalized_script("".join(cue.text for cue in cues)):
        raise ValueError("H3 raw cues 与冻结原稿不一致")
    numbers = (
        generation_tail_seconds,
        target_min_seconds,
        target_max_seconds,
        request_min_seconds,
        request_max_seconds,
    )
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("H3 分段时长参数不合法")
    if not (
        0 <= generation_tail_seconds <= 1
        and 0 < request_min_seconds <= target_min_seconds
        <= target_max_seconds <= request_max_seconds
    ):
        raise ValueError("H3 分段时长窗口不合法")

    count = len(cues)
    # A boundary before cue j becomes the end of the previous audio segment.
    boundaries = [0] + [cues[index].start_us for index in range(1, count)] + [audio_duration_us]
    best: list[tuple[float, int] | None] = [None] * (count + 1)
    best[0] = (0.0, -1)
    boundary_kinds: dict[tuple[int, int], str] = {}
    for end in range(1, count + 1):
        for start in range(0, end):
            prior = best[start]
            if prior is None:
                continue
            duration = (boundaries[end] - boundaries[start]) / 1_000_000
            requested = duration + generation_tail_seconds
            if requested < request_min_seconds - 1e-9:
                continue
            if requested > request_max_seconds + 1e-9:
                continue
            boundary_penalty, boundary_kind = _boundary_penalty(
                cues[end - 1].text,
                end == count,
            )
            if target_min_seconds <= duration <= target_max_seconds:
                window_penalty = 0.0
            else:
                distance = min(
                    abs(duration - target_min_seconds),
                    abs(duration - target_max_seconds),
                )
                window_penalty = 40.0 + distance * 12.0
            duration_penalty = abs(duration - 10.0)
            cost = prior[0] + window_penalty + boundary_penalty + duration_penalty
            if best[end] is None or cost < best[end][0]:
                best[end] = (cost, start)
                boundary_kinds[(start, end)] = boundary_kind

    if best[count] is None:
        raise ValueError("H3 raw cues 无法在 4～15 秒请求窗口内安全分段")
    ranges = []
    cursor = count
    while cursor > 0:
        start = best[cursor][1]  # type: ignore[index]
        ranges.append((start, cursor))
        cursor = start
    ranges.reverse()

    result = []
    for segment_index, (start, end) in enumerate(ranges):
        start_us = boundaries[start]
        end_us = boundaries[end]
        duration = (end_us - start_us) / 1_000_000
        result.append(
            SegmentPlan(
                segment_index=segment_index,
                cue_start_index=start,
                cue_end_index=end,
                start_us=start_us,
                end_us=end_us,
                audio_duration_seconds=duration,
                requested_generation_duration_seconds=(
                    duration + generation_tail_seconds
                ),
                text="".join(cue.text for cue in cues[start:end]),
                boundary_strength=boundary_kinds[(start, end)],
            )
        )
    return result
