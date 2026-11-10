from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps


WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
SOURCE_ROOT = WORKSPACE / ".codex_tmp" / "h3_xu_doctor_photo_test"
OUTPUT_ROOT = WORKSPACE / ".codex_tmp" / "h3_crop_ab"
SOURCE_NAMES = ("A.jpg", "B.jpg", "C.jpg")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_largest_face(path: Path) -> tuple[int, int, int, int]:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图片：{path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade = cv2.CascadeClassifier(
        str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    )
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.05,
        minNeighbors=5,
        minSize=(80, 80),
    )
    if len(faces) == 0:
        raise RuntimeError(f"没有检测到人脸：{path}")
    x, y, width, height = max(faces, key=lambda box: int(box[2]) * int(box[3]))
    return int(x), int(y), int(width), int(height)


def bounded_crop_box(
    *,
    image_width: int,
    image_height: int,
    center_x: float,
    center_y: float,
    crop_width: float,
    crop_height: float,
) -> tuple[int, int, int, int]:
    crop_width = min(float(image_width), crop_width)
    crop_height = min(float(image_height), crop_height)
    left = min(max(0.0, center_x - crop_width / 2), image_width - crop_width)
    top = min(max(0.0, center_y - crop_height / 2), image_height - crop_height)
    return (
        round(left),
        round(top),
        round(left + crop_width),
        round(top + crop_height),
    )


def make_crops(source: Path) -> dict[str, object]:
    face = detect_largest_face(source)
    x, y, face_width, face_height = face
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        width, height = image.size
        face_center_x = x + face_width / 2
        face_center_y = y + face_height / 2

        # Headshot: face, hair, ears, jaw and a small amount of neck/collar.
        head_side = max(face_width, face_height) * 2.15
        head_box = bounded_crop_box(
            image_width=width,
            image_height=height,
            center_x=face_center_x,
            center_y=face_center_y + face_height * 0.12,
            crop_width=head_side,
            crop_height=head_side,
        )

        # Head-and-shoulders: 3:4 portrait crop, retaining shoulders but excluding
        # most clothing, pose and environmental context from the original frame.
        shoulder_width = max(face_width * 3.35, face_height * 3.0)
        shoulder_height = shoulder_width * 4 / 3
        shoulder_box = bounded_crop_box(
            image_width=width,
            image_height=height,
            center_x=face_center_x,
            center_y=face_center_y + face_height * 0.82,
            crop_width=shoulder_width,
            crop_height=shoulder_height,
        )

        outputs: dict[str, dict[str, object]] = {}
        for variant, box, output_size in (
            ("headshot", head_box, (1024, 1024)),
            ("head_shoulders", shoulder_box, (960, 1280)),
        ):
            target_dir = OUTPUT_ROOT / variant
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            cropped = image.crop(box).resize(output_size, Image.Resampling.LANCZOS)
            cropped.save(target, format="JPEG", quality=95, optimize=True, subsampling=0)
            outputs[variant] = {
                "path": str(target),
                "crop_box": list(box),
                "output_size": list(output_size),
                "sha256": sha256(target),
                "size_bytes": target.stat().st_size,
            }

    return {
        "source": str(source),
        "source_size": [width, height],
        "source_sha256": sha256(source),
        "face_box": list(face),
        "outputs": outputs,
    }


def contact_sheet(variant: str, target: Path) -> None:
    paths = [OUTPUT_ROOT / variant / name for name in SOURCE_NAMES]
    thumbnails: list[Image.Image] = []
    for path in paths:
        with Image.open(path) as image:
            thumbnail = ImageOps.contain(image.convert("RGB"), (480, 640))
            thumbnails.append(thumbnail.copy())
    canvas = Image.new("RGB", (len(thumbnails) * 500, 700), "white")
    draw = ImageDraw.Draw(canvas)
    for index, (name, thumbnail) in enumerate(zip(SOURCE_NAMES, thumbnails, strict=True)):
        left = index * 500 + (500 - thumbnail.width) // 2
        canvas.paste(thumbnail, (left, 20))
        draw.text((index * 500 + 20, 665), f"{variant}: {name}", fill="black")
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, format="JPEG", quality=92, optimize=True)


def main() -> None:
    records = [make_crops(SOURCE_ROOT / name) for name in SOURCE_NAMES]
    contact_sheet("headshot", OUTPUT_ROOT / "headshot-contact-sheet.jpg")
    contact_sheet("head_shoulders", OUTPUT_ROOT / "head-shoulders-contact-sheet.jpg")
    manifest = {
        "schema": "h3.identity-crop-ab.v1",
        "method": "pixel-preserving crop and Lanczos resize; no generative editing",
        "source_names": list(SOURCE_NAMES),
        "records": records,
    }
    (OUTPUT_ROOT / "crop-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
