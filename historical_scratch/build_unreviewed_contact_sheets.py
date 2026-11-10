from __future__ import annotations

import csv
from pathlib import Path
import textwrap

import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps


SOURCE_ROOT = Path(r"D:\工作内容\素材\_筛选结果")
OUTPUT_ROOT = Path(
    r"D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_full_review"
)
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
BUCKETS = [
    ("food", "01_食物候选_去重后.csv"),
    ("exercise", "02_运动候选_去重后.csv"),
    ("review", "03_待视觉判断_去重后.csv"),
    ("deferred", "04_本轮暂不处理_去重后.csv"),
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fit_frame(frame, size: tuple[int, int]) -> Image.Image:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image.thumbnail(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (11, 18, 30))
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def video_frames(path: Path, size: tuple[int, int]) -> list[Image.Image]:
    capture = cv2.VideoCapture(str(path))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    frames: list[Image.Image] = []
    for ratio in (0.15, 0.50, 0.85):
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, round((total - 1) * ratio)))
        ok, frame = capture.read()
        if not ok:
            frames.append(Image.new("RGB", size, (90, 20, 30)))
        else:
            frames.append(fit_frame(frame, size))
    capture.release()
    return frames


def image_frame(path: Path, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as source:
            return ImageOps.contain(source.convert("RGB"), size, Image.Resampling.LANCZOS)
    except Exception:
        return Image.new("RGB", size, (90, 20, 30))


def main() -> None:
    reviewed = {
        row["sha256"]
        for row in read_rows(
            SOURCE_ROOT / "首批200_人工打标结果" / "首批200条_人工打标结果.csv"
        )
    }
    rows: list[dict[str, str]] = []
    for bucket, filename in BUCKETS:
        for row in read_rows(SOURCE_ROOT / filename):
            if row["sha256"] in reviewed:
                continue
            row["candidate_bucket"] = bucket
            rows.append(row)
    rows.sort(
        key=lambda row: (
            0 if row["media_type"] == "video" else 1,
            {name: index for index, (name, _file) in enumerate(BUCKETS)}[
                row["candidate_bucket"]
            ],
            float(row.get("duration_seconds") or 0),
            row["relative_path"].casefold(),
        )
    )
    for index, row in enumerate(rows, start=1):
        row["review_id"] = f"U{index:04d}"

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    fieldnames = ["review_id", "candidate_bucket", *[key for key in rows[0] if key not in {"review_id", "candidate_bucket"}]]
    with (OUTPUT_ROOT / "unreviewed_manifest.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    video_rows = [row for row in rows if row["media_type"] == "video"]
    image_rows = [row for row in rows if row["media_type"] == "image"]
    font = ImageFont.truetype(str(FONT_PATH), 22)
    small = ImageFont.truetype(str(FONT_PATH), 17)

    video_dir = OUTPUT_ROOT / "video_sheets"
    video_dir.mkdir(exist_ok=True)
    frame_size = (300, 330)
    row_height = 410
    per_sheet = 6
    for sheet_index in range(0, len(video_rows), per_sheet):
        batch = video_rows[sheet_index : sheet_index + per_sheet]
        sheet = Image.new("RGB", (940, row_height * len(batch)), (7, 13, 23))
        draw = ImageDraw.Draw(sheet)
        for local_index, row in enumerate(batch):
            top = local_index * row_height
            path = Path(row["full_path"])
            for frame_index, frame in enumerate(video_frames(path, frame_size)):
                sheet.paste(frame, (10 + frame_index * 310, top + 66))
            label = f"{row['review_id']}  {row['filename']}  {row['duration_seconds']}s  [{row['candidate_bucket']}]"
            draw.text((12, top + 8), label, font=font, fill=(235, 243, 255))
            relative = " / ".join(textwrap.wrap(row["relative_path"], width=88))
            draw.text((12, top + 37), relative, font=small, fill=(139, 164, 198))
        first = batch[0]["review_id"]
        last = batch[-1]["review_id"]
        sheet.save(video_dir / f"videos_{first}_{last}.jpg", quality=91)

    image_dir = OUTPUT_ROOT / "image_sheets"
    image_dir.mkdir(exist_ok=True)
    thumb_size = (220, 250)
    columns = 5
    rows_per_sheet = 5
    per_image_sheet = columns * rows_per_sheet
    cell_w, cell_h = 240, 330
    for sheet_index in range(0, len(image_rows), per_image_sheet):
        batch = image_rows[sheet_index : sheet_index + per_image_sheet]
        sheet = Image.new("RGB", (columns * cell_w, rows_per_sheet * cell_h), (7, 13, 23))
        draw = ImageDraw.Draw(sheet)
        for local_index, row in enumerate(batch):
            x = (local_index % columns) * cell_w
            y = (local_index // columns) * cell_h
            frame = image_frame(Path(row["full_path"]), thumb_size)
            canvas = Image.new("RGB", thumb_size, (11, 18, 30))
            canvas.paste(frame, ((thumb_size[0] - frame.width) // 2, (thumb_size[1] - frame.height) // 2))
            sheet.paste(canvas, (x + 10, y + 56))
            draw.text((x + 10, y + 6), row["review_id"], font=font, fill=(82, 221, 255))
            filename = row["filename"]
            if len(filename) > 16:
                filename = filename[:15] + "…"
            draw.text((x + 10, y + 31), filename, font=small, fill=(235, 243, 255))
        first = batch[0]["review_id"]
        last = batch[-1]["review_id"]
        sheet.save(image_dir / f"images_{first}_{last}.jpg", quality=91)

    print(f"unreviewed={len(rows)} videos={len(video_rows)} images={len(image_rows)}")
    print(f"video_sheets={(len(video_rows) + per_sheet - 1) // per_sheet}")
    print(f"image_sheets={(len(image_rows) + per_image_sheet - 1) // per_image_sheet}")


if __name__ == "__main__":
    main()
