from __future__ import annotations

import json
import subprocess
from pathlib import Path


FFMPEG = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffprobe.exe")
VIDEOS = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\videos")
OUTPUT = Path(r"D:\工作内容\空镜处理\Codex已处理")


PLAN = [
    {"folder": "review_u0083_video_113d91a6c4", "start": 2.8, "end": 9.8},
    {"folder": "review_u0084_video_a48ccec361", "start": 0.0, "end": 7.8},
    {"folder": "review_u0089_video_53e6d68225", "start": 0.0, "end": 8.8},
    {"folder": "review_u0102_video_a64745b702", "start": 0.0, "end": 9.3},
    {"folder": "review_u0103_video_278ae8075a", "start": 1.0, "end": 8.8},
    {"folder": "review_u0104_video_48be7e8b16", "start": 0.0, "end": 10.0},
    {"folder": "review_u0111_video_fb02d5d0c2", "start": 0.0, "end": 9.6},
    {"folder": "review_u0113_video_0348aae870", "start": 3.0, "end": 11.8},
    {"folder": "review_u0118_video_2c328482b0", "start": 0.0, "end": 10.8},
    {"folder": "review_u0123_video_95528198ed", "start": 0.0, "end": 10.2},
    {"folder": "review_u0131_video_b36bf97ee8", "start": 0.8, "end": 9.8},
    {"folder": "review_u0140_video_15b5a0e7df", "start": 7.0, "end": 12.5},
    {"folder": "review_u0141_video_f5fa7e1647", "start": 3.5, "end": 11.5},
    {"folder": "review_u0144_video_64dc59739c", "start": 0.8, "end": 12.2},
    {"folder": "review_u0150_video_670ab9b41a", "start": 0.8, "end": 12.6},
    {"folder": "review_u0169_video_23fc32881d", "start": 0.0, "end": 12.6},
    {"folder": "review_u0171_video_cc36577888", "start": 6.0, "end": 12.5},
    {"folder": "review_u0174_video_d41ab7f8ab", "start": 0.0, "end": 13.5},
    {"folder": "review_u0187_video_7aaf47316e", "start": 4.5, "end": 17.5},
    {"folder": "review_u0224_video_a73208368a", "start": 15.0, "end": 19.3},
    {"folder": "review_u0235_video_feb63d391f", "start": 5.0, "end": 21.5},
    {"folder": "review_u0242_video_011aee5b2b", "start": 0.0, "end": 20.0},
    {"folder": "review_u0264_video_5d53fbb619", "start": 0.0, "end": 21.5},
    {"folder": "review_u0275_video_03be926bc5", "start": 7.5, "end": 28.0},
    {"folder": "review_u0280_video_946f759f1a", "start": 15.0, "end": 26.5},
    {"folder": "review_u0291_video_6ab39a99c4", "start": 8.0, "end": 32.0},
    {"folder": "review_u0310_video_63b1064065", "start": 0.0, "end": 24.0},
    {"folder": "review_u0325_video_a4ae2206b9", "start": 6.5, "end": 19.5},
    {"folder": "review_u0361_video_1e8db50869", "start": 0.0, "end": 53.0, "crop": "crop=iw:780:0:160"},
    {"folder": "review_u0410_video_1aa5ad676c", "start": 2.0, "end": 8.5},
    {"folder": "review_u0421_video_3017691222", "start": 5.5, "end": 13.5},
    {"folder": "review_u0422_video_506d3e8a60", "start": 8.0, "end": 14.6, "crop": "crop=iw:760:0:40"},
    {"folder": "review_u0427_video_84d8c4717f", "start": 5.0, "end": 16.5},
    {"folder": "review_u0453_video_b4c6a6dcb6", "start": 0.0, "end": 37.5},
]

REDO_ONLY = {
    "review_u0141_video_f5fa7e1647",
    "review_u0280_video_946f759f1a",
}


def probe(path: Path) -> dict:
    result = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


OUTPUT.mkdir(parents=True, exist_ok=True)
report: list[dict] = []
selected = [item for item in PLAN if item["folder"] in REDO_ONLY]
for index, item in enumerate(selected, start=1):
    source = VIDEOS / item["folder"] / "video.mp4"
    target = OUTPUT / f"{item['folder']}.mp4"
    filters = [item["crop"]] if item.get("crop") else []
    filters.append("scale=1080:-2:flags=lanczos")
    command = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(item["start"]), "-i", str(source),
        "-t", str(item["end"] - item["start"]),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(target),
    ]
    subprocess.run(command, check=True)
    info = probe(target)
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    report.append({
        **item,
        "output": str(target),
        "duration_sec": round(float(info["format"]["duration"]), 3),
        "width": video["width"],
        "height": video["height"],
        "has_audio": any(stream["codec_type"] == "audio" for stream in info["streams"]),
    })
    print(f"{index}/{len(selected)} {item['folder']} -> {report[-1]['duration_sec']}s", flush=True)

(OUTPUT / "processing_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
