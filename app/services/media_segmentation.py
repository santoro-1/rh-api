from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.services.audio import AudioInspectionError, inspect_audio_duration
from app.services.processes import hidden_creation_flags
from app.services.speech.async_outputs import SubtitleCue


logger = logging.getLogger(__name__)

TARGET_SEGMENT_SECONDS = 30.0
MAX_SEGMENT_SECONDS = 45.0
_MIN_USEFUL_SEGMENT_SECONDS = 12.0
_STRONG_BREAK_RE = re.compile(r".+?(?:[。！？!?；;]+|\n+)|.+$", re.DOTALL)
_WEAK_BREAK_RE = re.compile(r".+?(?:[，,、：:]+)|.+$", re.DOTALL)
_SILENCE_RE = re.compile(r"silence_(start|end):\s*([0-9.]+)")


class MediaSegmentationError(ValueError):
    """A generated audio or source video cannot be segmented safely."""


@dataclass(frozen=True)
class SegmentPlan:
    index: int
    script_text: str
    start_seconds: float
    end_seconds: float
    alignment_method: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


def inspect_media_duration(path: Path) -> float:
    """Read a video duration without pretending it is an audio-only file."""

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
            timeout=30,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise MediaSegmentationError("服务器未安装 ffprobe，无法读取视频时长") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaSegmentationError("读取视频时长超时") from exc
    if completed.returncode != 0:
        raise MediaSegmentationError("源视频无法解析，请转存为标准 MP4 后重试")
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise MediaSegmentationError("源视频没有可读取的时长") from exc
    if duration <= 0:
        raise MediaSegmentationError("源视频时长必须大于 0")
    return duration


def _speech_weight(text: str) -> int:
    # Whitespace and punctuation contribute pauses but much less than spoken
    # characters. This is only a boundary estimate; real silence wins below.
    spoken = sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    punctuation = sum(1 for char in text if char in "，,、：:；;。！？!?")
    return max(spoken + round(punctuation * 0.35), 1)


def _split_text_units(script: str, duration_seconds: float) -> list[str]:
    strong_units = [
        match.group(0)
        for match in _STRONG_BREAK_RE.finditer(script.strip())
        if match.group(0)
    ]
    if not strong_units:
        raise MediaSegmentationError("口播脚本不能为空")

    # A single unpunctuated clause may itself exceed the hard limit. Split it
    # at weak punctuation, then at conservative character chunks as a fallback.
    total_weight = sum(_speech_weight(unit) for unit in strong_units)
    seconds_per_weight = duration_seconds / max(total_weight, 1)
    result: list[str] = []
    for unit in strong_units:
        if _speech_weight(unit) * seconds_per_weight <= MAX_SEGMENT_SECONDS:
            result.append(unit)
            continue
        weak_units = [
            match.group(0)
            for match in _WEAK_BREAK_RE.finditer(unit)
            if match.group(0)
        ]
        if len(weak_units) > 1:
            result.extend(weak_units)
            continue
        maximum_chars = max(int(MAX_SEGMENT_SECONDS / seconds_per_weight), 1)
        result.extend(
            unit[index : index + maximum_chars]
            for index in range(0, len(unit), maximum_chars)
        )
    return result


def detect_silence_midpoints(audio_path: Path) -> list[float]:
    """Return stable cut candidates without requiring a speech model."""

    null_target = "NUL" if os.name == "nt" else "-"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(audio_path),
        "-af",
        "silencedetect=noise=-35dB:d=0.18",
        "-f",
        "null",
        null_target,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("静音检测不可用，将使用标点和时长估算切分")
        return []

    starts: list[float] = []
    midpoints: list[float] = []
    for kind, raw_value in _SILENCE_RE.findall(completed.stderr or ""):
        value = float(raw_value)
        if kind == "start":
            starts.append(value)
        elif starts:
            start = starts.pop(0)
            midpoints.append((start + value) / 2)
    return midpoints


def plan_audio_segments(
    script: str,
    duration_seconds: float,
    silence_midpoints: list[float] | None = None,
) -> list[SegmentPlan]:
    """Map original script units onto <=45-second audio intervals."""

    if duration_seconds <= 0:
        raise MediaSegmentationError("生成音频时长必须大于 0")
    clean_script = script.strip()
    if not clean_script:
        raise MediaSegmentationError("口播脚本不能为空")
    if duration_seconds <= MAX_SEGMENT_SECONDS + 0.01:
        return [
            SegmentPlan(
                index=1,
                script_text=clean_script,
                start_seconds=0.0,
                end_seconds=duration_seconds,
                alignment_method="single",
            )
        ]

    units = _split_text_units(clean_script, duration_seconds)
    weights = [_speech_weight(unit) for unit in units]
    total_weight = sum(weights)
    cumulative_weights: list[int] = []
    running = 0
    for weight in weights:
        running += weight
        cumulative_weights.append(running)

    silences = sorted(silence_midpoints or [])
    plans: list[SegmentPlan] = []
    unit_start = 0
    time_start = 0.0
    while unit_start < len(units):
        remaining = duration_seconds - time_start
        if remaining <= MAX_SEGMENT_SECONDS + 0.01:
            plans.append(
                SegmentPlan(
                    index=len(plans) + 1,
                    script_text="".join(units[unit_start:]),
                    start_seconds=time_start,
                    end_seconds=duration_seconds,
                    alignment_method=(
                        "punctuation_silence" if silences else "punctuation_estimate"
                    ),
                )
            )
            break

        candidates: list[tuple[float, int, float, bool]] = []
        for unit_end in range(unit_start, len(units) - 1):
            estimated_end = (
                duration_seconds * cumulative_weights[unit_end] / total_weight
            )
            relative = estimated_end - time_start
            if relative < _MIN_USEFUL_SEGMENT_SECONDS:
                continue
            if relative > MAX_SEGMENT_SECONDS:
                break
            nearby = [
                point
                for point in silences
                if time_start + _MIN_USEFUL_SEGMENT_SECONDS
                <= point
                <= time_start + MAX_SEGMENT_SECONDS
                and abs(point - estimated_end) <= 3.0
            ]
            actual_end = min(
                nearby,
                key=lambda point: abs(point - estimated_end),
                default=estimated_end,
            )
            used_silence = bool(nearby)
            score = abs((actual_end - time_start) - TARGET_SEGMENT_SECONDS)
            if not 25 <= actual_end - time_start <= 35:
                score += 4
            if not used_silence:
                score += 2
            candidates.append((score, unit_end, actual_end, used_silence))

        if candidates:
            _score, unit_end, time_end, used_silence = min(candidates)
        else:
            unit_end = unit_start
            time_end = min(time_start + MAX_SEGMENT_SECONDS, duration_seconds)
            used_silence = False
        time_end = min(max(time_end, time_start + 0.1), time_start + MAX_SEGMENT_SECONDS)
        plans.append(
            SegmentPlan(
                index=len(plans) + 1,
                script_text="".join(units[unit_start : unit_end + 1]),
                start_seconds=time_start,
                end_seconds=time_end,
                alignment_method=(
                    "punctuation_silence" if used_silence else "punctuation_estimate"
                ),
            )
        )
        unit_start = unit_end + 1
        time_start = time_end
    return plans


def _join_cue_text(cues: list[SubtitleCue]) -> str:
    parts: list[str] = []
    for cue in cues:
        text = cue.text.strip()
        if (
            parts
            and parts[-1][-1:].isascii()
            and parts[-1][-1:].isalnum()
            and text[:1].isascii()
            and text[:1].isalnum()
        ):
            parts.append(" ")
        parts.append(text)
    return "".join(parts)


def plan_timestamped_segments(
    cues: list[SubtitleCue],
    duration_seconds: float,
) -> list[SegmentPlan]:
    """Group official sentence timestamps into roughly 30-second segments."""

    if duration_seconds <= 0:
        raise MediaSegmentationError("生成音频时长必须大于 0")
    if not cues:
        raise MediaSegmentationError("MiniMax 没有返回可用的句级时间戳")
    ordered = sorted(cues, key=lambda cue: (cue.start_seconds, cue.end_seconds))
    if any(
        not cue.text.strip()
        or cue.start_seconds < -0.05
        or cue.end_seconds <= cue.start_seconds
        for cue in ordered
    ):
        raise MediaSegmentationError("MiniMax 句级时间戳内容无效")
    if ordered[-1].end_seconds > duration_seconds + 1.0:
        raise MediaSegmentationError("MiniMax 句级时间戳超出音频时长")
    if any(
        current.start_seconds + 0.25 < previous.end_seconds
        for previous, current in zip(ordered, ordered[1:])
    ):
        raise MediaSegmentationError("MiniMax 句级时间戳存在重叠")

    # Cut in the middle of pauses between sentences. The first and last
    # boundaries keep the full audio, including any leading/trailing silence.
    boundaries = [0.0]
    for current, following in zip(ordered, ordered[1:]):
        if following.start_seconds >= current.end_seconds:
            boundary = (current.end_seconds + following.start_seconds) / 2
        else:
            boundary = current.end_seconds
        boundaries.append(min(max(boundary, boundaries[-1] + 0.001), duration_seconds))
    boundaries.append(duration_seconds)

    cue_count = len(ordered)
    costs = [float("inf")] * (cue_count + 1)
    previous: list[int | None] = [None] * (cue_count + 1)
    costs[0] = 0.0
    for end_index in range(1, cue_count + 1):
        for start_index in range(end_index - 1, -1, -1):
            segment_duration = boundaries[end_index] - boundaries[start_index]
            if segment_duration > MAX_SEGMENT_SECONDS + 0.01:
                continue
            if segment_duration <= 0:
                continue
            short_penalty = (
                225.0
                if segment_duration < _MIN_USEFUL_SEGMENT_SECONDS
                and not (start_index == 0 and end_index == cue_count)
                else 0.0
            )
            score = (
                costs[start_index]
                + (segment_duration - TARGET_SEGMENT_SECONDS) ** 2
                + short_penalty
            )
            if score < costs[end_index]:
                costs[end_index] = score
                previous[end_index] = start_index

    if previous[cue_count] is None:
        longest = max(
            cue.end_seconds - cue.start_seconds for cue in ordered
        )
        raise MediaSegmentationError(
            "句级时间戳中存在无法控制在 45 秒内的长句"
            f"（最长约 {longest:.1f} 秒），请在脚本中补充句号后重试"
        )

    groups: list[tuple[int, int]] = []
    cursor = cue_count
    while cursor:
        start = previous[cursor]
        if start is None:
            raise MediaSegmentationError("无法根据句级时间戳规划音频分段")
        groups.append((start, cursor))
        cursor = start
    groups.reverse()

    return [
        SegmentPlan(
            index=index,
            script_text=_join_cue_text(ordered[start:end]),
            start_seconds=boundaries[start],
            end_seconds=boundaries[end],
            alignment_method="minimax_sentence_timestamp",
        )
        for index, (start, end) in enumerate(groups, start=1)
    ]


def build_segment_plan(audio_path: Path, script: str) -> list[SegmentPlan]:
    try:
        duration = inspect_audio_duration(audio_path)
    except AudioInspectionError as exc:
        raise MediaSegmentationError(str(exc)) from exc
    return plan_audio_segments(
        script,
        duration,
        detect_silence_midpoints(audio_path),
    )


def cut_audio_segment(
    source: Path,
    target: Path,
    *,
    start_seconds: float,
    end_seconds: float,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part.mp3")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{end_seconds - start_seconds:.3f}",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(temporary),
    ]
    _run_ffmpeg(command, "切割生成音频失败")
    os.replace(temporary, target)


def cut_video_segment(
    source: Path,
    target: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part.mp4")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-t",
        f"{duration_seconds:.3f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    _run_ffmpeg(command, "切割源视频失败")
    os.replace(temporary, target)


def _run_ffmpeg(command: list[str], message: str) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            creationflags=hidden_creation_flags(),
        )
    except FileNotFoundError as exc:
        raise MediaSegmentationError("服务器未安装 ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaSegmentationError(f"{message}：处理超时") from exc
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout or "").strip()
        logger.warning("%s：%s", message, diagnostic[-1000:])
        raise MediaSegmentationError(message)
