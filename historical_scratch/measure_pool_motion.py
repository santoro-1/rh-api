from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")
CATALOG = ROOT / "catalog.json"
POOLS = {
    "editorial.home_daily",
    "editorial.meal_daily",
    "editorial.leisure_daily",
    "editorial.family_life",
    "editorial.mood_atmosphere",
}


def motion_stats(path: Path) -> dict[str, float | int]:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    duration_s = frame_count / fps if frame_count > 0 else 0.0
    sample_step_s = 0.5
    previous = None
    diffs: list[float] = []
    static_runs: list[int] = []
    run = 0
    timestamp_s = 0.1
    while timestamp_s < duration_s:
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_s * 1000.0)
        ok, frame = cap.read()
        if not ok:
            break
        height, width = frame.shape[:2]
        scale = min(1.0, 240.0 / max(height, width))
        gray = cv2.cvtColor(
            cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale)))),
            cv2.COLOR_BGR2GRAY,
        )
        if previous is not None:
            diff = float(np.mean(cv2.absdiff(gray, previous)))
            diffs.append(diff)
            if diff < 0.35:
                run += 1
            else:
                if run:
                    static_runs.append(run)
                run = 0
        previous = gray
        timestamp_s += sample_step_s
    cap.release()
    if run:
        static_runs.append(run)
    if not diffs:
        return {"samples": 0, "mean_diff": 0.0, "p10_diff": 0.0, "static_run_s": 0.0}
    return {
        "samples": len(diffs) + 1,
        "mean_diff": round(float(np.mean(diffs)), 4),
        "p10_diff": round(float(np.percentile(diffs, 10)), 4),
        "static_run_s": round(max(static_runs, default=0) * sample_step_s, 3),
    }


payload = json.loads(CATALOG.read_text(encoding="utf-8"))
rows = []
for asset in payload["assets"]:
    if asset.get("media_type") != "video":
        continue
    fallback = set(asset.get("video_taxonomy", {}).get("fallback_concept_ids", []))
    if not fallback & POOLS:
        continue
    path = ROOT / asset["resource"]["video"]
    rows.append(
        {
            "asset_id": asset["asset_id"],
            "name": asset["name"],
            **motion_stats(path),
        }
    )

rows.sort(key=lambda row: (row["mean_diff"], -row["static_run_s"]))
print(json.dumps(rows, ensure_ascii=False, indent=2))
