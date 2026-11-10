from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\videos")
OUT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\quarantine_timed")
OUT.mkdir(parents=True, exist_ok=True)

FOLDERS = [
    "review_u0140_video_15b5a0e7df",
    "review_u0171_video_cc36577888",
    "review_u0187_video_7aaf47316e",
    "review_u0224_video_a73208368a",
    "review_u0235_video_feb63d391f",
    "review_u0275_video_03be926bc5",
    "review_u0291_video_6ab39a99c4",
    "review_u0325_video_a4ae2206b9",
    "review_u0361_video_1e8db50869",
    "review_u0410_video_1aa5ad676c",
    "review_u0421_video_3017691222",
    "review_u0422_video_506d3e8a60",
    "review_u0427_video_84d8c4717f",
    "review_u0453_video_b4c6a6dcb6",
]


def fit(frame: np.ndarray, width: int = 180, height: int = 240) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = min(width / w, height / h)
    resized = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    y = (height - resized.shape[0]) // 2
    x = (width - resized.shape[1]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


for folder in FOLDERS:
    path = ROOT / folder / "video.mp4"
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps
    seconds = [x / 2 for x in range(math.ceil(duration * 2))]
    cells: list[np.ndarray] = []
    for sec in seconds:
        cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        cell = np.zeros((270, 180, 3), dtype=np.uint8)
        cell[:240] = fit(frame)
        cv2.putText(cell, f"{sec:05.1f}s", (8, 262), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        cells.append(cell)
    cap.release()
    cols = 5
    rows = math.ceil(len(cells) / cols)
    sheet = np.zeros((rows * 270, cols * 180, 3), dtype=np.uint8)
    for i, cell in enumerate(cells):
        y = (i // cols) * 270
        x = (i % cols) * 180
        sheet[y:y + 270, x:x + 180] = cell
    encoded = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])[1]
    encoded.tofile(str(OUT / f"{folder}.jpg"))
    print(folder, duration)
