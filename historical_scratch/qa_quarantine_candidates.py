from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path


FFMPEG = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffprobe.exe")
VIDEOS = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\videos")
OUTPUT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\quarantine_candidate_qa")

CANDIDATES = [
    "review_u0317_video_e79db70509",
    "broll_20260814_001_video_0acd36aeb5",
    "broll_20260814_012_video_40af9611e5",
    "broll_20260814_017_video_ab8e7a8cd1",
    "broll_20260814_018_video_bd046fca01",
    "broll_20260814_021_video_fd64795cf2",
    "broll_20260814_026_video_e74d4bb115",
    "broll_20260814_029_video_68731f13cd",
    "broll_20260814_031_video_80eb4148e8",
]


def duration(path: Path) -> float:
    result = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(json.loads(result.stdout)["format"]["duration"])


OUTPUT.mkdir(parents=True, exist_ok=True)
report: list[dict] = []
for folder in CANDIDATES:
    source = VIDEOS / folder / "video.mp4"
    seconds = duration(source)
    sheets: list[str] = []
    for page, start in enumerate(range(0, math.ceil(seconds), 40), start=1):
        target = OUTPUT / f"{folder}__{page:02d}.jpg"
        subprocess.run(
            [
                str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(start), "-i", str(source), "-t", str(min(40, max(0.1, seconds - start))),
                "-vf", "fps=1,scale=150:-2,tile=8x5:padding=3:margin=3",
                "-frames:v", "1", str(target),
            ],
            check=True,
        )
        sheets.append(str(target))
    record = {"folder": folder, "duration_sec": round(seconds, 3), "sheets": sheets}
    report.append(record)
    print(json.dumps(record, ensure_ascii=False), flush=True)

(OUTPUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
