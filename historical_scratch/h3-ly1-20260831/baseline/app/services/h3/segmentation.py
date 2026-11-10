from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from app.services.h3.duration import plan_h3_duration
from app.services.speech.async_outputs import SubtitleCue


H3_SEGMENTER_VERSION = "h3.segment.minimax-cues+funasr.v2"
_STRONG_END = re.compile(r"[。！？!?][”’\"']?$")
_MEDIUM_END = re.compile(r"[；;：:][”’\"']?$")
_WEAK_END = re.compile(r"[，,、][”’\"']?$")


@dataclass(frozen=True)
class H3TimestampedSegment:
    index: int
    script_text: str
    start_seconds: float
    end_seconds: float
    boundary_strength: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class _AlignedBoundary:
    script_offset: int
    time_seconds: float
    penalty: float
    strength: str


def _normalize(value: str) -> str:
    return "".join(str(value or "").split())


def _boundary_score(text: str, *, final: bool) -> tuple[float, str]:
    if final or _STRONG_END.search(text):
        return 0.0, "strong"
    if _MEDIUM_END.search(text):
        return 2.0, "medium"
    if _WEAK_END.search(text):
        return 5.0, "weak"
    return 9.0, "fallback"


def plan_h3_timestamped_segments(
    script_text: str,
    cues: list[SubtitleCue],
    audio_duration_seconds: float,
    *,
    generation_tail_seconds: float = 0.1,
) -> list[H3TimestampedSegment]:
    """Group authoritative MiniMax cues into the H3 4–15 second request grid."""

    if not math.isfinite(audio_duration_seconds) or audio_duration_seconds <= 0:
        raise ValueError("H3 完整音频时长不合法")
    if not cues:
        raise ValueError("H3 MiniMax 音频缺少 raw cues")
    ordered = sorted(cues, key=lambda cue: (cue.start_seconds, cue.end_seconds))
    for index, cue in enumerate(ordered):
        if (
            not cue.text.strip()
            or not math.isfinite(cue.start_seconds)
            or not math.isfinite(cue.end_seconds)
            or cue.start_seconds < -0.05
            or cue.end_seconds <= cue.start_seconds
            or cue.end_seconds > audio_duration_seconds + 0.5
        ):
            raise ValueError(f"H3 raw cue {index + 1} 不合法")
    if any(
        current.start_seconds + 0.25 < previous.end_seconds
        for previous, current in zip(ordered, ordered[1:])
    ):
        raise ValueError("H3 raw cues 时间存在重叠")
    if _normalize(script_text) != _normalize("".join(cue.text for cue in ordered)):
        raise ValueError("H3 raw cues 与冻结原稿不一致")

    boundaries = [0.0]
    for current, following in zip(ordered, ordered[1:]):
        if following.start_seconds >= current.end_seconds:
            boundary = (current.end_seconds + following.start_seconds) / 2
        else:
            boundary = current.end_seconds
        boundaries.append(
            min(max(boundary, boundaries[-1] + 0.001), audio_duration_seconds)
        )
    boundaries.append(audio_duration_seconds)

    count = len(ordered)
    costs = [float("inf")] * (count + 1)
    previous: list[int | None] = [None] * (count + 1)
    strengths: dict[tuple[int, int], str] = {}
    costs[0] = 0.0
    for end in range(1, count + 1):
        for start in range(end - 1, -1, -1):
            duration = boundaries[end] - boundaries[start]
            try:
                plan_h3_duration(duration, generation_tail_seconds)
            except ValueError:
                continue
            boundary_penalty, strength = _boundary_score(
                ordered[end - 1].text,
                final=end == count,
            )
            if 8.0 <= duration <= 12.0:
                window_penalty = 0.0
            else:
                distance = min(abs(duration - 8.0), abs(duration - 12.0))
                window_penalty = 40.0 + 12.0 * distance
            score = costs[start] + window_penalty + boundary_penalty + abs(duration - 10.0)
            if score < costs[end]:
                costs[end] = score
                previous[end] = start
                strengths[(start, end)] = strength
    if previous[count] is None:
        raise ValueError("H3 raw cues 无法在 4～15 秒请求窗口内安全分段")

    ranges = []
    cursor = count
    while cursor:
        start = previous[cursor]
        if start is None:
            raise ValueError("H3 raw cues 无法形成完整分段")
        ranges.append((start, cursor))
        cursor = start
    ranges.reverse()
    return [
        H3TimestampedSegment(
            index=index,
            script_text="".join(cue.text for cue in ordered[start:end]),
            start_seconds=boundaries[start],
            end_seconds=boundaries[end],
            boundary_strength=strengths[(start, end)],
        )
        for index, (start, end) in enumerate(ranges)
    ]


def plan_h3_aligned_segments(
    script_text: str,
    aligned_tokens: Sequence[object],
    audio_duration_seconds: float,
    *,
    generation_tail_seconds: float = 0.1,
) -> list[H3TimestampedSegment]:
    """Plan H3 cuts from FunASR tokens aligned to the frozen source script.

    MiniMax occasionally returns one raw cue for an audio file that is longer
    than H3's request window.  The raw cue remains authoritative input data,
    while these aligned token boundaries provide safe, word-level cut points
    inside that cue.  Original script slices are preserved verbatim.
    """

    clean_script = str(script_text or "").strip()
    if not clean_script:
        raise ValueError("H3 冻结原稿不能为空")
    if not math.isfinite(audio_duration_seconds) or audio_duration_seconds <= 0:
        raise ValueError("H3 完整音频时长不合法")
    ordered = sorted(
        aligned_tokens,
        key=lambda token: (
            int(getattr(token, "script_start")),
            float(getattr(token, "start_seconds")),
        ),
    )
    if len(ordered) < 2:
        raise ValueError("H3 FunASR 对齐结果缺少足够的字词切点")
    previous_script_end = -1
    previous_audio_end = -1.0
    for position, token in enumerate(ordered, start=1):
        try:
            script_start = int(getattr(token, "script_start"))
            script_end = int(getattr(token, "script_end"))
            start_seconds = float(getattr(token, "start_seconds"))
            end_seconds = float(getattr(token, "end_seconds"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"H3 FunASR 第 {position} 个字词时间戳不合法") from exc
        if (
            script_start < 0
            or script_end <= script_start
            or script_end > len(clean_script)
            or not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < -0.1
            or end_seconds <= start_seconds
            or end_seconds > audio_duration_seconds + 1.0
            or script_start < previous_script_end
            or start_seconds + 0.25 < previous_audio_end
        ):
            raise ValueError(f"H3 FunASR 第 {position} 个字词时间戳不合法")
        previous_script_end = script_end
        previous_audio_end = end_seconds

    candidates = [
        _AlignedBoundary(
            script_offset=0,
            time_seconds=0.0,
            penalty=0.0,
            strength="strong",
        )
    ]
    for left, right in zip(ordered, ordered[1:]):
        left_end = int(getattr(left, "script_end"))
        right_start = int(getattr(right, "script_start"))
        if right_start <= left_end or right_start >= len(clean_script):
            continue
        left_audio_end = float(getattr(left, "end_seconds"))
        right_audio_start = float(getattr(right, "start_seconds"))
        boundary_time = (left_audio_end + right_audio_start) / 2
        if not 0.05 < boundary_time < audio_duration_seconds - 0.05:
            continue
        boundary_penalty, strength = _boundary_score(
            clean_script[:right_start].rstrip(),
            final=False,
        )
        # A word boundary is still safer than a duration estimate, but it must
        # lose decisively to real punctuation whenever both are legal.
        if strength == "fallback":
            boundary_penalty = 100.0
        candidates.append(
            _AlignedBoundary(
                script_offset=right_start,
                time_seconds=boundary_time,
                penalty=boundary_penalty,
                strength=strength,
            )
        )
    candidates.append(
        _AlignedBoundary(
            script_offset=len(clean_script),
            time_seconds=audio_duration_seconds,
            penalty=0.0,
            strength="strong",
        )
    )
    candidates.sort(
        key=lambda boundary: (boundary.time_seconds, boundary.script_offset)
    )
    monotonic: list[_AlignedBoundary] = []
    for boundary in candidates:
        if monotonic and (
            boundary.script_offset <= monotonic[-1].script_offset
            or boundary.time_seconds <= monotonic[-1].time_seconds
        ):
            continue
        monotonic.append(boundary)
    candidates = monotonic

    costs = [float("inf")] * len(candidates)
    previous: list[int | None] = [None] * len(candidates)
    costs[0] = 0.0
    final_index = len(candidates) - 1
    for end in range(1, len(candidates)):
        for start in range(end - 1, -1, -1):
            if not math.isfinite(costs[start]):
                continue
            duration = candidates[end].time_seconds - candidates[start].time_seconds
            try:
                plan_h3_duration(duration, generation_tail_seconds)
            except ValueError:
                continue
            if 8.0 <= duration <= 12.0:
                window_penalty = 0.0
            else:
                distance = min(abs(duration - 8.0), abs(duration - 12.0))
                window_penalty = 40.0 + 12.0 * distance
            score = (
                costs[start]
                + window_penalty
                + candidates[end].penalty
                + abs(duration - 10.0)
            )
            if score < costs[end]:
                costs[end] = score
                previous[end] = start
    if previous[final_index] is None:
        raise ValueError("H3 FunASR 时间轴无法在 4～15 秒请求窗口内安全分段")

    ranges: list[tuple[int, int]] = []
    cursor = final_index
    while cursor:
        start = previous[cursor]
        if start is None:
            raise ValueError("H3 FunASR 时间轴无法形成完整分段")
        ranges.append((start, cursor))
        cursor = start
    ranges.reverse()
    segments = [
        H3TimestampedSegment(
            index=index,
            script_text=clean_script[
                candidates[start].script_offset : candidates[end].script_offset
            ],
            start_seconds=candidates[start].time_seconds,
            end_seconds=candidates[end].time_seconds,
            boundary_strength=candidates[end].strength,
        )
        for index, (start, end) in enumerate(ranges)
    ]
    if "".join(segment.script_text for segment in segments) != clean_script:
        raise ValueError("H3 FunASR 分段未完整覆盖冻结原稿")
    return segments
