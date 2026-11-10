from __future__ import annotations

import io
import zipfile


def make_async_speech_bundle(
    audio_bytes: bytes,
    cues: list[tuple[float, float, str]],
) -> bytes:
    subtitle_blocks = []
    for index, (start, end, text) in enumerate(cues, start=1):
        subtitle_blocks.append(
            f"{index}\n{_timecode(start)} --> {_timecode(end)}\n{text}\n"
        )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("result/audio.mp3", audio_bytes)
        archive.writestr(
            "result/subtitle.srt",
            "\n".join(subtitle_blocks).encode("utf-8"),
        )
        archive.writestr(
            "result/extra_info.json",
            '{"audio_length": 55000}',
        )
    return target.getvalue()


def _timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},"
        f"{milliseconds:03d}"
    )
