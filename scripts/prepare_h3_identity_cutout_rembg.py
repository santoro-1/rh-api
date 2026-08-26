from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from rembg import new_session, remove


def build_cutout(source: Path, target: Path, model: str) -> None:
    original = Image.open(source).convert("RGBA")
    isolated = remove(original, session=new_session(model)).convert("RGBA")
    rgba = np.asarray(isolated).copy()
    height, width = rgba.shape[:2]

    allowed = np.zeros((height, width), dtype=np.uint8)
    cv2.ellipse(
        allowed,
        (width // 2, int(height * 0.36)),
        (int(width * 0.39), int(height * 0.34)),
        0,
        0,
        360,
        255,
        -1,
    )
    allowed[int(height * 0.58) :, :] = 0
    lower_face_and_neck = np.array(
        [
            [int(width * 0.27), int(height * 0.52)],
            [int(width * 0.75), int(height * 0.52)],
            [int(width * 0.70), int(height * 0.59)],
            [int(width * 0.64), int(height * 0.64)],
            [int(width * 0.55), int(height * 0.68)],
            [int(width * 0.48), int(height * 0.68)],
            [int(width * 0.39), int(height * 0.64)],
            [int(width * 0.32), int(height * 0.59)],
        ],
        dtype=np.int32,
    )
    cv2.fillConvexPoly(allowed, lower_face_and_neck, 255)
    allowed = cv2.GaussianBlur(allowed, (0, 0), sigmaX=1.2, sigmaY=1.2)
    rgba[:, :, 3] = (
        rgba[:, :, 3].astype(np.float32) * (allowed.astype(np.float32) / 255.0)
    ).astype(np.uint8)
    rgba[rgba[:, :, 3] == 0, :3] = 0

    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(target, format="PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--model", default="silueta")
    arguments = parser.parse_args()
    build_cutout(
        arguments.source.resolve(), arguments.target.resolve(), arguments.model
    )
    print(arguments.target.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
