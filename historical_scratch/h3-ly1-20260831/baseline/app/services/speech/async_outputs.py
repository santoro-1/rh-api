from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable


MAX_RESULT_BYTES = 256 * 1024 * 1024
_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac"}
_TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


class AsyncSpeechOutputError(ValueError):
    """The downloaded MiniMax result does not contain usable audio/timestamps."""


@dataclass(frozen=True)
class SubtitleCue:
    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class AsyncSpeechOutput:
    audio_bytes: bytes
    audio_suffix: str
    cues: list[SubtitleCue]


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _timecode_seconds(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) != 3:
        raise ValueError("invalid subtitle timecode")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_srt_or_vtt(text: str) -> list[SubtitleCue]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cues: list[SubtitleCue] = []
    index = 0
    while index < len(lines):
        match = _TIME_RANGE_RE.search(lines[index])
        if match is None:
            index += 1
            continue
        start = _timecode_seconds(match.group("start"))
        end = _timecode_seconds(match.group("end"))
        index += 1
        content: list[str] = []
        while index < len(lines) and lines[index].strip():
            content.append(lines[index].strip())
            index += 1
        cue_text = " ".join(content).strip()
        if cue_text and end > start:
            cues.append(SubtitleCue(cue_text, start, end))
    return cues


_TEXT_KEYS = ("text", "sentence", "content", "subtitle", "caption")
_START_KEYS = (
    "time_begin",
    "start_time",
    "startTime",
    "begin_time",
    "beginTime",
    "start_ms",
    "start",
    "begin",
    "from",
)
_END_KEYS = (
    "time_end",
    "end_time",
    "endTime",
    "finish_time",
    "finishTime",
    "end_ms",
    "end",
    "finish",
    "to",
)


def _find_value(item: dict[str, Any], keys: Iterable[str]) -> tuple[str, Any] | None:
    for key in keys:
        if key in item and item[key] is not None:
            return key, item[key]
    return None


def _numeric_time(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if ":" in raw:
        return _timecode_seconds(raw)
    return float(raw)


def _iter_dict_lists(value: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield value
        for item in value:
            yield from _iter_dict_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_dict_lists(item)


def _parse_json_timeline(text: str) -> list[SubtitleCue]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Some providers emit one JSON cue per line.
        rows: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                return []
        payload = rows

    for items in _iter_dict_lists(payload):
        raw_rows: list[tuple[str, str, float, str, float]] = []
        for item in items:
            text_value = _find_value(item, _TEXT_KEYS)
            start_value = _find_value(item, _START_KEYS)
            end_value = _find_value(item, _END_KEYS)
            if not text_value or not start_value or not end_value:
                raw_rows = []
                break
            cue_text = str(text_value[1]).strip()
            if not cue_text:
                raw_rows = []
                break
            try:
                raw_rows.append(
                    (
                        cue_text,
                        start_value[0],
                        _numeric_time(start_value[1]),
                        end_value[0],
                        _numeric_time(end_value[1]),
                    )
                )
            except (TypeError, ValueError):
                raw_rows = []
                break
        if not raw_rows:
            continue

        ambiguous_maximum = max(row[4] for row in raw_rows)
        cues: list[SubtitleCue] = []
        for cue_text, start_key, start, end_key, end in raw_rows:
            uses_milliseconds = (
                start_key.lower().endswith("ms")
                or end_key.lower().endswith("ms")
                or ambiguous_maximum > 1000
            )
            if uses_milliseconds:
                start /= 1000
                end /= 1000
            if end > start:
                cues.append(SubtitleCue(cue_text, start, end))
        if cues:
            return cues
    return []


def _parse_tabular_timeline(text: str) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for line in text.splitlines():
        parts = re.split(r"\t+|\s*\|\s*", line.strip(), maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            start = _numeric_time(parts[0])
            end = _numeric_time(parts[1])
        except ValueError:
            continue
        if end > 1000:
            start /= 1000
            end /= 1000
        if parts[2].strip() and end > start:
            cues.append(SubtitleCue(parts[2].strip(), start, end))
    return cues


def parse_subtitle_bytes(payload: bytes) -> list[SubtitleCue]:
    text = _decode_text(payload)
    return (
        _parse_srt_or_vtt(text)
        or _parse_json_timeline(text)
        or _parse_tabular_timeline(text)
    )


def _archive_members(payload: bytes) -> list[tuple[str, bytes]]:
    source = io.BytesIO(payload)
    if zipfile.is_zipfile(source):
        members: list[tuple[str, bytes]] = []
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MAX_RESULT_BYTES:
                    raise AsyncSpeechOutputError("MiniMax 结果包内文件过大")
                members.append((info.filename, archive.read(info)))
        return members

    source.seek(0)
    try:
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            members = []
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                if info.size > MAX_RESULT_BYTES:
                    raise AsyncSpeechOutputError("MiniMax 结果包内文件过大")
                file_object = archive.extractfile(info)
                if file_object is not None:
                    members.append((info.name, file_object.read()))
            if members:
                return members
    except tarfile.TarError:
        pass
    return []


def decode_async_speech_output(
    payload: bytes,
    *,
    expected_format: str,
) -> AsyncSpeechOutput:
    """Decode MiniMax's result bundle without extracting untrusted paths."""

    if not payload:
        raise AsyncSpeechOutputError("MiniMax 返回了空结果文件")
    if len(payload) > MAX_RESULT_BYTES:
        raise AsyncSpeechOutputError("MiniMax 结果文件过大")

    members = _archive_members(payload)
    if not members:
        raise AsyncSpeechOutputError(
            "MiniMax 异步结果不是包含音频和句级字幕的压缩包"
        )

    expected_suffix = f".{expected_format.lower()}"
    audio_candidates = [
        (name, content)
        for name, content in members
        if PurePosixPath(name.replace("\\", "/")).suffix.lower()
        in _AUDIO_SUFFIXES
    ]
    if not audio_candidates:
        raise AsyncSpeechOutputError("MiniMax 结果包中缺少音频文件")
    audio_name, audio_bytes = min(
        audio_candidates,
        key=lambda item: (
            PurePosixPath(item[0]).suffix.lower() != expected_suffix,
            len(PurePosixPath(item[0]).parts),
        ),
    )

    text_candidates = [
        (name, content)
        for name, content in members
        if PurePosixPath(name.replace("\\", "/")).suffix.lower()
        in {".srt", ".vtt", ".json", ".txt", ".subtitle", ".titles"}
    ]
    text_candidates.sort(
        key=lambda item: (
            not any(
                marker in item[0].lower()
                for marker in (
                    "subtitle",
                    "timestamp",
                    "caption",
                    ".srt",
                    ".vtt",
                    ".titles",
                )
            ),
            len(PurePosixPath(item[0]).parts),
        )
    )
    cues: list[SubtitleCue] = []
    for _name, content in text_candidates:
        cues = parse_subtitle_bytes(content)
        if cues:
            break
    cues.sort(key=lambda cue: (cue.start_seconds, cue.end_seconds))
    if not cues:
        raise AsyncSpeechOutputError("MiniMax 结果包中缺少可识别的句级时间戳")
    if any(
        cue.end_seconds <= cue.start_seconds
        or (
            index
            and cue.start_seconds + 0.25 < cues[index - 1].end_seconds
        )
        for index, cue in enumerate(cues)
    ):
        raise AsyncSpeechOutputError("MiniMax 句级时间戳顺序无效")

    return AsyncSpeechOutput(
        audio_bytes=audio_bytes,
        audio_suffix=PurePosixPath(audio_name).suffix.lower(),
        cues=cues,
    )


def dump_subtitle_cues(cues: list[SubtitleCue]) -> str:
    return json.dumps(
        [asdict(cue) for cue in cues],
        ensure_ascii=False,
        indent=2,
    )


def load_subtitle_cues(text: str) -> list[SubtitleCue]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AsyncSpeechOutputError("本地句级时间轴文件损坏") from exc
    if not isinstance(payload, list):
        raise AsyncSpeechOutputError("本地句级时间轴格式无效")
    try:
        cues = [
            SubtitleCue(
                text=str(item["text"]).strip(),
                start_seconds=float(item["start_seconds"]),
                end_seconds=float(item["end_seconds"]),
            )
            for item in payload
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise AsyncSpeechOutputError("本地句级时间轴格式无效") from exc
    if not cues or any(
        not cue.text or cue.end_seconds <= cue.start_seconds for cue in cues
    ):
        raise AsyncSpeechOutputError("本地句级时间轴没有有效句子")
    return cues
