from __future__ import annotations

import json
import subprocess
from pathlib import Path


FFMPEG = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffprobe.exe")
SOURCE = Path(r"D:\工作内容\空镜处理")
OUTPUT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\manual_video_qa")


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            str(FFPROBE), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


OUTPUT.mkdir(parents=True, exist_ok=True)
report: list[dict] = []
for path in sorted(SOURCE.glob("*.mp4")):
    info = probe(path)
    duration = float(info["format"]["duration"])
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    decode = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sheet = OUTPUT / f"{path.stem}.jpg"
    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-vf", f"fps={19 / max(duration, 0.1)},scale=240:-2,tile=5x4:padding=4:margin=4",
            "-frames:v", "1", str(sheet),
        ],
        check=True,
    )
    record = {
        "name": path.name,
        "duration_sec": round(duration, 3),
        "width": video["width"],
        "height": video["height"],
        "has_audio": any(stream["codec_type"] == "audio" for stream in info["streams"]),
        "decode_ok": decode.returncode == 0,
        "decode_error": decode.stderr.strip(),
        "sheet": str(sheet),
    }
    report.append(record)
    print(json.dumps(record, ensure_ascii=False), flush=True)

(OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
