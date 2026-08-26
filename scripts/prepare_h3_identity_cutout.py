from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def build_cutout(source: Path, target: Path) -> None:
    image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图片：{source}")
    height, width = image.shape[:2]
    if width < 256 or height < 256:
        raise RuntimeError("身份图分辨率过低")

    mask = np.full((height, width), cv2.GC_BGD, dtype=np.uint8)
    center_x = width // 2
    probable = np.zeros_like(mask)
    cv2.ellipse(
        probable,
        (center_x, int(height * 0.38)),
        (int(width * 0.39), int(height * 0.37)),
        0,
        0,
        360,
        255,
        -1,
    )
    neck = np.array(
        [
            [int(width * 0.37), int(height * 0.58)],
            [int(width * 0.65), int(height * 0.58)],
            [int(width * 0.67), int(height * 0.80)],
            [int(width * 0.34), int(height * 0.80)],
        ],
        dtype=np.int32,
    )
    cv2.fillConvexPoly(probable, neck, 255)
    mask[probable > 0] = cv2.GC_PR_FGD

    certain_face = np.zeros_like(mask)
    cv2.ellipse(
        certain_face,
        (center_x, int(height * 0.44)),
        (int(width * 0.21), int(height * 0.25)),
        0,
        0,
        360,
        255,
        -1,
    )
    mask[certain_face > 0] = cv2.GC_FGD
    mask[: max(1, int(height * 0.01)), :] = cv2.GC_BGD
    mask[int(height * 0.81) :, :] = cv2.GC_BGD

    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)
    cv2.grabCut(
        image,
        mask,
        None,
        background_model,
        foreground_model,
        8,
        cv2.GC_INIT_WITH_MASK,
    )
    alpha = np.where(
        (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0
    ).astype(np.uint8)
    alpha = cv2.bitwise_and(alpha, probable)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, kernel, iterations=1)
    alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=0.7, sigmaY=0.7)

    points = cv2.findNonZero((alpha > 8).astype(np.uint8))
    if points is None:
        raise RuntimeError("抠图结果为空")
    x, y, crop_width, crop_height = cv2.boundingRect(points)
    padding = max(24, int(max(crop_width, crop_height) * 0.06))
    x0 = max(0, x - padding)
    y0 = max(0, y - padding)
    x1 = min(width, x + crop_width + padding)
    y1 = min(height, y + crop_height + padding)

    rgba = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha
    rgba[alpha == 0, :3] = 0
    crop = rgba[y0:y1, x0:x1]
    side = max(crop.shape[:2])
    canvas = np.zeros((side, side, 4), dtype=np.uint8)
    offset_y = (side - crop.shape[0]) // 2
    offset_x = (side - crop.shape[1]) // 2
    canvas[
        offset_y : offset_y + crop.shape[0], offset_x : offset_x + crop.shape[1]
    ] = crop
    output = cv2.resize(canvas, (1024, 1024), interpolation=cv2.INTER_LANCZOS4)

    target.parent.mkdir(parents=True, exist_ok=True)
    encoded, buffer = cv2.imencode(target.suffix or ".png", output)
    if not encoded:
        raise RuntimeError(f"无法编码图片：{target}")
    buffer.tofile(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    arguments = parser.parse_args()
    build_cutout(arguments.source.resolve(), arguments.target.resolve())
    print(arguments.target.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
