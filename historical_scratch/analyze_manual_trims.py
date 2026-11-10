from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


QUARANTINE = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\quarantine\platform_ui_pending.json")
VIDEOS = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\videos")
EDITED = Path(r"D:\工作内容\空镜处理")


def signature(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    mean = float(gray.mean())
    std = float(gray.std()) or 1.0
    return (gray - mean) / std


def source_frames(path: Path) -> tuple[list[float], np.ndarray, float]:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps
    step = max(1, round(fps / 20.0))
    times: list[float] = []
    sigs: list[np.ndarray] = []
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step == 0:
            times.append(index / fps)
            sigs.append(signature(frame))
        index += 1
    cap.release()
    return times, np.stack(sigs), duration


def frame_at(path: Path, sec: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, sec) * 1000.0)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"cannot read {path} at {sec}")
    return signature(frame)


def duration(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return total / fps


items = json.loads(QUARANTINE.read_text(encoding="utf-8"))["assets"]
for item in items:
    edited = EDITED / f"{item['name']}.mp4"
    if not edited.exists():
        continue
    source = VIDEOS / item["asset_id"].replace(".", "_") / "video.mp4"
    times, sigs, src_duration = source_frames(source)
    edited_duration = duration(edited)
    probes = sorted({min(edited_duration - 0.15, value) for value in (0.15, 0.5, 1.0, edited_duration / 2, edited_duration - 1.0, edited_duration - 0.5) if 0.0 < value < edited_duration})
    offsets: list[float] = []
    errors: list[float] = []
    for probe in probes:
        target = frame_at(edited, probe)
        mse = np.mean((sigs - target) ** 2, axis=(1, 2))
        best = int(np.argmin(mse))
        offsets.append(times[best] - probe)
        errors.append(float(mse[best]))
    offset = float(np.median(offsets))
    print(json.dumps({
        "asset_id": item["asset_id"],
        "name": item["name"],
        "source_sec": round(src_duration, 3),
        "edited_sec": round(edited_duration, 3),
        "estimated_start_sec": round(offset, 3),
        "estimated_end_sec": round(offset + edited_duration, 3),
        "estimated_tail_removed_sec": round(src_duration - offset - edited_duration, 3),
        "match_error": round(float(np.median(errors)), 5),
        "offset_spread": round(float(np.ptp(offsets)), 3),
    }, ensure_ascii=False))
