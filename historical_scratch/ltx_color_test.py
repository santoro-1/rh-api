from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def center_fit(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Match ComfyUI's center crop/resize geometry without stretching."""
    source_h, source_w = frame.shape[:2]
    source_ratio = source_w / source_h
    target_ratio = width / height
    if source_ratio > target_ratio:
        crop_w = max(1, int(round(source_h * target_ratio)))
        left = max(0, (source_w - crop_w) // 2)
        frame = frame[:, left : left + crop_w]
    elif source_ratio < target_ratio:
        crop_h = max(1, int(round(source_w / target_ratio)))
        top = max(0, (source_h - crop_h) // 2)
        frame = frame[top : top + crop_h]
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def read_frame(capture: cv2.VideoCapture, seconds: float) -> np.ndarray:
    capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
    ok, frame = capture.read()
    if not ok or frame is None:
        raise RuntimeError(f"cannot decode frame at {seconds:.3f}s")
    return frame


def robust_lab_stats(samples: list[np.ndarray]) -> dict[str, list[float]]:
    values = np.concatenate(samples, axis=0)
    q10 = np.quantile(values, 0.10, axis=0)
    q50 = np.quantile(values, 0.50, axis=0)
    q90 = np.quantile(values, 0.90, axis=0)
    return {
        "q10": q10.tolist(),
        "q50": q50.tolist(),
        "q90": q90.tolist(),
    }


def derive_mapping(source: Path, ltx: Path, sample_count: int) -> dict[str, object]:
    source_capture = cv2.VideoCapture(str(source))
    ltx_capture = cv2.VideoCapture(str(ltx))
    if not source_capture.isOpened() or not ltx_capture.isOpened():
        raise RuntimeError("cannot open source or LTX video")

    width = int(ltx_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(ltx_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(ltx_capture.get(cv2.CAP_PROP_FPS) or 24.0)
    frames = float(ltx_capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
    duration = frames / fps if frames > 0 and fps > 0 else 1.0
    start = min(0.75, duration * 0.05)
    end = max(start, duration - min(0.75, duration * 0.05))
    times = np.linspace(start, end, max(3, sample_count))

    source_samples: list[np.ndarray] = []
    ltx_samples: list[np.ndarray] = []
    for seconds in times:
        source_frame = center_fit(read_frame(source_capture, float(seconds)), width, height)
        ltx_frame = read_frame(ltx_capture, float(seconds))
        source_small = cv2.resize(source_frame, (264, 480), interpolation=cv2.INTER_AREA)
        ltx_small = cv2.resize(ltx_frame, (264, 480), interpolation=cv2.INTER_AREA)

        source_rgb = cv2.cvtColor(source_small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        ltx_rgb = cv2.cvtColor(ltx_small, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        source_lab = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2LAB)
        ltx_lab = cv2.cvtColor(ltx_rgb, cv2.COLOR_RGB2LAB)

        # Avoid crushed blacks/highlights; they are unstable and provide little chroma evidence.
        valid = (
            (source_lab[..., 0] > 4.0)
            & (source_lab[..., 0] < 96.0)
            & (ltx_lab[..., 0] > 4.0)
            & (ltx_lab[..., 0] < 96.0)
        )
        source_samples.append(source_lab[valid])
        ltx_samples.append(ltx_lab[valid])

    source_capture.release()
    ltx_capture.release()
    source_stats = robust_lab_stats(source_samples)
    ltx_stats = robust_lab_stats(ltx_samples)
    source_q10 = np.asarray(source_stats["q10"], dtype=np.float32)
    source_q50 = np.asarray(source_stats["q50"], dtype=np.float32)
    source_q90 = np.asarray(source_stats["q90"], dtype=np.float32)
    ltx_q10 = np.asarray(ltx_stats["q10"], dtype=np.float32)
    ltx_q50 = np.asarray(ltx_stats["q50"], dtype=np.float32)
    ltx_q90 = np.asarray(ltx_stats["q90"], dtype=np.float32)
    scale = (source_q90 - source_q10) / np.maximum(ltx_q90 - ltx_q10, 1e-4)
    # Keep automatic correction conservative. Chroma over-scaling is a common source of artifacts.
    scale = np.clip(scale, np.array([0.75, 0.80, 0.80]), np.array([1.25, 1.20, 1.20]))
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "times": times.tolist(),
        "source": source_stats,
        "ltx": ltx_stats,
        "center": source_q50.tolist(),
        "input_center": ltx_q50.tolist(),
        "scale": scale.tolist(),
    }


def write_cube(path: Path, mapping: dict[str, object], strength: float, size: int = 33) -> None:
    source_center = np.asarray(mapping["center"], dtype=np.float32)
    input_center = np.asarray(mapping["input_center"], dtype=np.float32)
    scale = np.asarray(mapping["scale"], dtype=np.float32)
    values = np.linspace(0.0, 1.0, size, dtype=np.float32)
    rows: list[str] = []
    # Iridas .cube convention: red changes fastest, then green, then blue.
    for blue in values:
        for green in values:
            rgb = np.stack(
                [values, np.full_like(values, green), np.full_like(values, blue)], axis=1
            ).reshape(1, size, 3)
            lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).reshape(size, 3)
            corrected_lab = (lab - input_center) * scale + source_center
            corrected_lab[:, 0] = np.clip(corrected_lab[:, 0], 0.0, 100.0)
            corrected_lab[:, 1:] = np.clip(corrected_lab[:, 1:], -127.0, 127.0)
            corrected = cv2.cvtColor(
                corrected_lab.reshape(1, size, 3).astype(np.float32), cv2.COLOR_LAB2RGB
            ).reshape(size, 3)
            blended = np.clip(rgb.reshape(size, 3) * (1.0 - strength) + corrected * strength, 0.0, 1.0)
            rows.extend(f"{r:.8f} {g:.8f} {b:.8f}" for r, g, b in blended)

    path.write_text(
        "\n".join(
            [
                f'TITLE "LTX color restore {strength:.2f}"',
                f"LUT_3D_SIZE {size}",
                "DOMAIN_MIN 0.0 0.0 0.0",
                "DOMAIN_MAX 1.0 1.0 1.0",
                *rows,
                "",
            ]
        ),
        encoding="ascii",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("ltx", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    mapping = derive_mapping(args.source, args.ltx, args.samples)
    (args.output / "mapping.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for strength in (0.60, 0.80, 1.00):
        write_cube(args.output / f"restore-{int(strength * 100):03d}.cube", mapping, strength)
    print(json.dumps(mapping, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
