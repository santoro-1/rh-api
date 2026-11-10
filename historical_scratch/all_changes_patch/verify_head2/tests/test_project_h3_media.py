from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
import sys
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.project_h3_media import _ffprobe_path, _run  # noqa: E402


def test_h3_media_uses_bundled_ffmpeg_when_path_has_no_ffmpeg() -> None:
    completed = CompletedProcess(["unused"], 0, stdout="", stderr="")
    with patch(
        "jyd_probe.project_h3_media._ffmpeg_path",
        return_value=r"F:\PublicVideo-x64\digital-human\ffmpeg\bin\ffmpeg.exe",
    ), patch(
        "jyd_probe.project_h3_media.subprocess.run", return_value=completed
    ) as run:
        _run(["ffmpeg", "-version"], "FFmpeg 检查失败")

    assert run.call_args.args[0][0] == (
        r"F:\PublicVideo-x64\digital-human\ffmpeg\bin\ffmpeg.exe"
    )


def test_h3_media_finds_ffprobe_next_to_bundled_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    with patch("jyd_probe.project_h3_media.shutil.which", return_value=None), patch(
        "jyd_probe.project_h3_media._ffmpeg_path", return_value=str(ffmpeg)
    ):
        assert _ffprobe_path() == str(ffprobe.resolve())
