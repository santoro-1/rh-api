from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")

FFMPEG = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\ctool\ffmpeg\ffmpeg-8.1.1-full_build\bin\ffprobe.exe")
REPO_LIBRARY = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")
CATALOG_PATH = REPO_LIBRARY / "catalog.json"
QUARANTINE_PATH = REPO_LIBRARY / "quarantine" / "platform_ui_pending.json"
MANUAL_DIR = Path(r"D:\工作内容\空镜处理")
PROCESSED_DIR = MANUAL_DIR / "Codex已处理"
STAGE = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819")

MANUAL_FILES = {
    "manual.048.video.10095de4fb": MANUAL_DIR / "上下摆臂.mp4",
    "manual.056.video.609531a24e": MANUAL_DIR / "站姿扭腰动作.mp4",
    "review.u0020.video.507d6ee47f": MANUAL_DIR / "玉米鸡蛋蔬菜水煮餐.mp4",
    "review.u0034.video.0fa094a4f3": MANUAL_DIR / "倒温水.mp4",
    "review.u0042.video.93e2eea161": MANUAL_DIR / "秋日银杏街景.mp4",
    "review.u0046.video.b438b99f2f": MANUAL_DIR / "凉拌海带.mp4",
    "review.u0060.video.e329aacbfa": MANUAL_DIR / "枸杞.mp4",
    "review.u0061.video.40976d2d4c": PROCESSED_DIR / "manual_大蒜.mp4",
    "review.u0075.video.bdfa56baf2": MANUAL_DIR / "海带.mp4",
    "review.u0077.video.1eaa547446": MANUAL_DIR / "菠菜鸡蛋汤.mp4",
}

AUDITED_CLEAN_IDS = {
    "broll.20260814.001.video.0acd36aeb5",
    "broll.20260814.012.video.40af9611e5",
    "broll.20260814.017.video.ab8e7a8cd1",
    "broll.20260814.018.video.bd046fca01",
    "broll.20260814.021.video.fd64795cf2",
    "broll.20260814.026.video.e74d4bb115",
    "broll.20260814.029.video.68731f13cd",
    "broll.20260814.031.video.80eb4148e8",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            str(FFPROBE), "-v", "error", "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    video = next(stream for stream in payload["streams"] if stream["codec_type"] == "video")
    return {
        "duration_us": round(float(payload["format"]["duration"]) * 1_000_000),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": str(video.get("r_frame_rate") or ""),
        "has_audio": any(stream["codec_type"] == "audio" for stream in payload["streams"]),
    }


if STAGE.exists():
    raise SystemExit(f"staging directory already exists: {STAGE}")

catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
quarantine = json.loads(QUARANTINE_PATH.read_text(encoding="utf-8"))
assets_by_id = {asset["asset_id"]: asset for asset in catalog["assets"]}
quarantined = quarantine["assets"]
if len(quarantined) != 53:
    raise RuntimeError(f"expected 53 quarantined assets, got {len(quarantined)}")

processed_rows = quarantined[:45]
if {row["asset_id"] for row in quarantined[45:]} != AUDITED_CLEAN_IDS:
    raise RuntimeError("the final 8 quarantine entries no longer match the audited clean set")

stage_videos = STAGE / "videos"
stage_videos.mkdir(parents=True)
manifest: list[dict] = []
new_assets: list[dict] = []

for sequence, row in enumerate(processed_rows, start=1):
    source_id = row["asset_id"]
    source_asset = assets_by_id[source_id]
    if source_id in MANUAL_FILES:
        clean_source = MANUAL_FILES[source_id]
        method = "manual_trim_then_codex_qa"
    else:
        clean_source = PROCESSED_DIR / f"{source_id.replace('.', '_')}.mp4"
        method = "codex_trim_crop_then_qa"
    if not clean_source.is_file():
        raise FileNotFoundError(clean_source)

    clean_hash = sha256(clean_source)
    new_id = f"clean.20260819.{sequence:03d}.video.{clean_hash[:10]}"
    folder_name = new_id.replace(".", "_")
    destination = stage_videos / folder_name
    destination.mkdir()
    video_target = destination / "video.mp4"
    shutil.copy2(clean_source, video_target)

    media = probe(video_target)
    poster_target = destination / "poster.png"
    poster_time = max(0.0, min(media["duration_us"] / 2_000_000, 3.0))
    subprocess.run(
        [
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{poster_time:.3f}", "-i", str(video_target),
            "-frames:v", "1", str(poster_target),
        ],
        check=True,
    )

    metadata = {
        "schema": "jyd.semantic-video-metadata.v1",
        "review_id": f"CLEAN-20260819-{sequence:03d}",
        "original_name": clean_source.name,
        "source_sha256": clean_hash,
        "content_sha256": clean_hash,
        "fps": media["fps"],
        "duration_us": media["duration_us"],
        "width": media["width"],
        "height": media["height"],
        "has_audio": media["has_audio"],
        "review_decision": "APPROVED_CLEAN",
        "approved_source_start_us": 0,
        "approved_source_end_us": media["duration_us"],
        "review_notes": "已去除播放器按钮、加载图标或应用界面，并完成完整解码与抽帧复核",
        "lineage": {
            "source_asset_id": source_id,
            "cleaning_method": method,
            "review_date": "2026-08-19",
        },
    }
    (destination / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    cloned = copy.deepcopy(source_asset)
    cloned["asset_id"] = new_id
    cloned["description"] = f"{source_asset.get('description', '').rstrip('。')}；净化版已移除播放器或应用界面。"
    tags = list(cloned.get("tags") or [])
    if "播放器界面净化版" not in tags:
        tags.append("播放器界面净化版")
    cloned["tags"] = tags
    cloned["resource"] = {
        "video": f"videos/{folder_name}/video.mp4",
        "preview": f"videos/{folder_name}/poster.png",
        "metadata": f"videos/{folder_name}/metadata.json",
        "duration_us": media["duration_us"],
        "width": media["width"],
        "height": media["height"],
        "has_audio": media["has_audio"],
    }
    cloned.setdefault("defaults", {})["source_start_us"] = 0
    cloned["defaults"]["duration_us"] = min(
        int(cloned["defaults"].get("duration_us") or 5_000_000),
        media["duration_us"],
    )
    cloned["cleanliness_grade"] = "A"
    cloned["auto_eligible"] = True
    cloned["requires_clip"] = False
    cloned["platform_ui_status"] = "none"
    new_assets.append(cloned)
    manifest.append({
        "source_asset_id": source_id,
        "new_asset_id": new_id,
        "name": cloned["name"],
        "folder": folder_name,
        "source_file": str(clean_source),
        "sha256": clean_hash,
        **media,
    })
    print(f"{sequence:02d}/45 {source_id} -> {new_id}", flush=True)

for asset in catalog["assets"]:
    if asset["asset_id"] in AUDITED_CLEAN_IDS:
        asset["platform_ui_status"] = "none"
        tags = list(asset.get("tags") or [])
        if "逐秒抽帧复核无平台界面" not in tags:
            tags.append("逐秒抽帧复核无平台界面")
        asset["tags"] = tags

catalog["assets"].extend(new_assets)
quarantine["assets"] = [
    row for row in quarantine["assets"] if row["asset_id"] not in AUDITED_CLEAN_IDS
]

(STAGE / "catalog.json").write_text(
    json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
(STAGE / "platform_ui_pending.json").write_text(
    json.dumps(quarantine, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
(STAGE / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(f"stage={STAGE}")
print(f"catalog_assets={len(catalog['assets'])} quarantine_assets={len(quarantine['assets'])}")
