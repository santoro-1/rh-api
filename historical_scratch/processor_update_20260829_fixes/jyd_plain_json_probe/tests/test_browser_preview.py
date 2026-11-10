from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from jyd_probe.browser_preview import browser_preview_path


def test_browser_preview_keeps_h264_yuv420p_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"compatible")
    probe = CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
        ),
        stderr="",
    )
    with patch("jyd_probe.browser_preview._ffprobe_path", return_value="ffprobe"), patch(
        "jyd_probe.browser_preview.subprocess.run", return_value=probe
    ) as run:
        assert browser_preview_path(source, tmp_path / "cache") == source.resolve()
    assert run.call_count == 1


def test_browser_preview_transcodes_yuv444p_once(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"incompatible")
    probe = CompletedProcess(
        [],
        0,
        stdout=json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv444p"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            }
        ),
        stderr="",
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> CompletedProcess[str]:
        calls.append(args)
        if args[0] == "ffprobe":
            return probe
        Path(args[-1]).write_bytes(b"browser-proxy")
        return CompletedProcess(args, 0, stdout="", stderr="")

    with patch("jyd_probe.browser_preview._ffprobe_path", return_value="ffprobe"), patch(
        "jyd_probe.browser_preview._ffmpeg_path", return_value="ffmpeg"
    ), patch("jyd_probe.browser_preview.subprocess.run", side_effect=fake_run):
        first = browser_preview_path(source, tmp_path / "cache")
        second = browser_preview_path(source, tmp_path / "cache")

    assert first == second
    assert first.read_bytes() == b"browser-proxy"
    transcodes = [args for args in calls if args[0] == "ffmpeg"]
    assert len(transcodes) == 1
    assert "yuv420p" in transcodes[0]
    assert "scale=720:-2:force_original_aspect_ratio=decrease,format=yuv420p" in transcodes[0]
    assert "+faststart" in transcodes[0]
