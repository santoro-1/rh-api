from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from .cli import import_pyjianyingdraft, log
from .content_replace import apply_text_track_style


@dataclass(frozen=True)
class CaptionCue:
    start_us: int
    duration_us: int
    text: str

    @property
    def end_us(self) -> int:
        return self.start_us + self.duration_us

    def as_dict(self) -> dict[str, int | str]:
        return {
            "start_us": self.start_us,
            "duration_us": self.duration_us,
            "end_us": self.end_us,
            "text": self.text,
        }


_SRT_TIME_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{3})"
)


def _parse_srt_timecode(value: str) -> int:
    hours, minutes, tail = value.replace(".", ",").split(":")
    seconds, milliseconds = tail.split(",")
    return (
        int(hours) * 3_600_000_000
        + int(minutes) * 60_000_000
        + int(seconds) * 1_000_000
        + int(milliseconds) * 1_000
    )


def parse_srt_cues(text: str) -> list[CaptionCue]:
    """Parse an inline SRT payload without changing its provider timestamps."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    cues: list[CaptionCue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        time_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            -1,
        )
        if time_index < 0:
            raise ValueError("SRT 字幕缺少时间范围")
        match = _SRT_TIME_RE.fullmatch(lines[time_index])
        if match is None:
            raise ValueError(f"SRT 字幕时间格式无效: {lines[time_index]!r}")
        start_us = _parse_srt_timecode(match.group("start"))
        end_us = _parse_srt_timecode(match.group("end"))
        cue_text = "\n".join(lines[time_index + 1 :]).strip()
        if not cue_text or end_us <= start_us:
            raise ValueError("SRT 字幕内容或时间范围无效")
        cues.append(
            CaptionCue(
                start_us=start_us,
                duration_us=end_us - start_us,
                text=cue_text,
            )
        )
    return validate_caption_cues(cues)


def caption_cues_from_payload(values: Iterable[object]) -> list[CaptionCue]:
    """Read precise JSON cues using start/end or start/duration microseconds."""

    cues: list[CaptionCue] = []
    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 条字幕必须是对象")
        cue_text = str(raw.get("text") or raw.get("content") or "").strip()
        try:
            start_us = int(raw.get("start_us", 0))
            if raw.get("end_us") is not None:
                end_us = int(raw["end_us"])
                duration_us = end_us - start_us
            else:
                duration_us = int(raw.get("duration_us", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 条字幕时间必须是整数微秒") from exc
        if not cue_text:
            raise ValueError(f"第 {index} 条字幕内容不能为空")
        cues.append(
            CaptionCue(
                start_us=start_us,
                duration_us=duration_us,
                text=cue_text,
            )
        )
    return validate_caption_cues(cues)


def validate_caption_cues(
    cues: Iterable[CaptionCue],
    *,
    timeline_offset_us: int = 0,
    maximum_end_us: int | None = None,
    end_tolerance_us: int = 1_000_000,
) -> list[CaptionCue]:
    """Validate, offset and minimally clip an exact provider timeline."""

    ordered = sorted(cues, key=lambda cue: (cue.start_us, cue.end_us))
    result: list[CaptionCue] = []
    previous_end = -1
    for index, cue in enumerate(ordered, start=1):
        start_us = cue.start_us + timeline_offset_us
        end_us = cue.end_us + timeline_offset_us
        if start_us < 0 or end_us <= start_us:
            raise ValueError(f"第 {index} 条字幕时间范围无效")
        if start_us < previous_end:
            raise ValueError(f"第 {index} 条字幕与上一条字幕重叠")
        if maximum_end_us is not None and end_us > maximum_end_us:
            overflow = end_us - maximum_end_us
            if overflow > end_tolerance_us or start_us >= maximum_end_us:
                raise ValueError(f"第 {index} 条字幕超出视频时长")
            end_us = maximum_end_us
        result.append(
            CaptionCue(
                start_us=start_us,
                duration_us=end_us - start_us,
                text=cue.text,
            )
        )
        previous_end = end_us
    return result


def _display_units(text: str) -> float:
    units = 0.0
    for char in text:
        code = ord(char)
        if char.isspace():
            units += 0.35
        elif char.isascii() and char.isalnum():
            units += 0.55
        elif char in "，。！？；：、,.!?;:()（）《》<>\"'“”‘’":
            units += 0.5
        elif 0x2E80 <= code <= 0x9FFF or 0xFF00 <= code <= 0xFFEF:
            units += 1.0
        else:
            units += 0.8
    return units


def _split_paragraph(paragraph: str, max_units: float) -> list[str]:
    chunks: list[str] = []
    break_chars = set("，,、：:。！？!?；;")
    cursor = 0
    while cursor < len(paragraph):
        width = 0.0
        end = cursor
        while end < len(paragraph):
            next_width = width + _display_units(paragraph[end])
            if end > cursor and next_width > max_units:
                break
            width = next_width
            end += 1

        if end >= len(paragraph):
            tail = paragraph[cursor:].strip()
            if tail:
                chunks.append(tail)
            break

        window = paragraph[cursor:end]
        preferred_breaks = [
            index + 1
            for index, char in enumerate(window)
            if char in break_chars and _display_units(window[: index + 1]) >= max_units * 0.45
        ]
        cut = cursor + (preferred_breaks[-1] if preferred_breaks else len(window))
        chunk = paragraph[cursor:cut].strip()
        if chunk:
            chunks.append(chunk)
        cursor = cut
        while cursor < len(paragraph) and paragraph[cursor].isspace():
            cursor += 1
    return chunks


def split_caption_text(text: str, max_chars: int = 16) -> list[str]:
    """Split long copy into readable caption chunks using punctuation and visual width."""

    if not text or not text.strip():
        return []
    if not 4 <= int(max_chars) <= 60:
        raise ValueError(f"每条字幕最大字数必须在 4 到 60 之间: {max_chars}")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [re.sub(r"[ \t]+", " ", item).strip() for item in normalized.split("\n")]
    result: list[str] = []

    for paragraph in paragraphs:
        if not paragraph:
            continue
        result.extend(_split_paragraph(paragraph, float(max_chars)))

    return result


def build_caption_cues(
    text: str,
    *,
    start_us: int = 0,
    duration_us: int,
    max_chars: int = 16,
    min_duration_us: int = 650_000,
) -> list[CaptionCue]:
    chunks = split_caption_text(text, max_chars=max_chars)
    if not chunks:
        return []
    if start_us < 0:
        raise ValueError(f"字幕开始时间不能小于 0: {start_us}")
    if duration_us <= 0:
        raise ValueError(f"字幕总时长必须大于 0: {duration_us}")
    if duration_us < len(chunks):
        raise ValueError("字幕总时长过短，无法为每条字幕分配有效时间")

    base_duration = min(max(0, int(min_duration_us)), duration_us // len(chunks))
    remaining = duration_us - base_duration * len(chunks)
    weights = [max(1.0, _display_units(chunk)) for chunk in chunks]
    total_weight = sum(weights)

    cues: list[CaptionCue] = []
    cursor = start_us
    allocated_extra = 0
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        if index == len(chunks) - 1:
            extra = remaining - allocated_extra
            cue_duration = start_us + duration_us - cursor
        else:
            next_allocated_extra = round(remaining * sum(weights[: index + 1]) / total_weight)
            extra = next_allocated_extra - allocated_extra
            allocated_extra = next_allocated_extra
            cue_duration = base_duration + extra
        cues.append(CaptionCue(start_us=cursor, duration_us=cue_duration, text=chunk))
        cursor += cue_duration
    return cues


def _srt_timestamp(value_us: int) -> str:
    milliseconds = max(0, value_us // 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def cues_to_srt(cues: Iterable[CaptionCue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start_us)} --> {_srt_timestamp(cue.end_us)}\n{cue.text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def add_captions_to_draft(
    draft_dir: str | Path,
    cues: list[CaptionCue],
    *,
    style_json_path: str | Path = "",
    size: float | None = None,
    color: str = "",
    stroke_color: str = "",
    stroke_width: float | None = None,
    transform_x: float | None = None,
    transform_y: float | None = None,
    clip_scale: float | None = None,
    line_max_width: float | None = None,
    single_line: bool = False,
    font_id: str = "",
    font_path: str = "",
    font_title: str = "",
    track_name: str = "自动字幕",
    shadow_color: str = "",
    shadow_alpha: float | None = None,
    shadow_distance: float | None = None,
    shadow_angle: float | None = None,
    shadow_smoothing: float | None = None,
) -> Path:
    if not cues:
        raise ValueError("没有可导入的字幕")

    draft_path = Path(draft_dir).resolve()
    srt_path = draft_path / "auto_captions.srt"
    srt_path.write_text(cues_to_srt(cues), encoding="utf-8-sig")

    draft = import_pyjianyingdraft()
    folder = draft.DraftFolder(str(draft_path.parent))
    script = folder.load_template(draft_path.name)
    actual_track_name = track_name or "自动字幕"
    suffix = 2
    while actual_track_name in script.tracks:
        actual_track_name = f"{track_name or '自动字幕'}_{suffix}"
        suffix += 1
    default_size = float(size) if size is not None and size > 0 else 8.0
    default_width = float(line_max_width) if line_max_width is not None else 0.82
    script.import_srt(
        str(srt_path),
        actual_track_name,
        text_style=draft.TextStyle(
            size=default_size,
            align=1,
            auto_wrapping=not single_line,
            max_line_width=default_width,
        ),
        clip_settings=draft.ClipSettings(
            scale_x=float(clip_scale if clip_scale is not None else 1.0),
            scale_y=float(clip_scale if clip_scale is not None else 1.0),
            transform_x=float(transform_x or 0.0),
            transform_y=float(transform_y if transform_y is not None else -0.8),
        ),
    )
    script.save()

    apply_text_track_style(
        draft_path,
        track_name=actual_track_name,
        style_json_path=style_json_path,
        size=size,
        color=color,
        stroke_color=stroke_color,
        stroke_width=stroke_width,
        transform_x=transform_x,
        transform_y=transform_y,
        line_max_width=line_max_width,
        font_id=font_id,
        font_path=font_path,
        font_title=font_title,
        shadow_color=shadow_color,
        shadow_alpha=shadow_alpha,
        shadow_distance=shadow_distance,
        shadow_angle=shadow_angle,
        shadow_smoothing=shadow_smoothing,
    )
    log(f"已导入自动字幕: track={actual_track_name!r}, cues={len(cues)}, srt={srt_path}")
    return srt_path
