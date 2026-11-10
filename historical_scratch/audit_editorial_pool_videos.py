from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


CATALOG_ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")
OUTPUT_ROOT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\editorial_pool_quality_20260814")
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
POOL_PREFIX = "editorial."


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size=size)


def frame_at(capture: cv2.VideoCapture, seconds: float) -> np.ndarray | None:
    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000.0)
    ok, frame = capture.read()
    return frame if ok else None


def fit_frame(frame: np.ndarray, width: int, height: int) -> Image.Image:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "black")
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas


def freeze_ranges(capture: cv2.VideoCapture, duration: float) -> list[dict[str, float]]:
    times = np.arange(0.0, max(0.0, duration - 0.05), 0.20)
    previous: np.ndarray | None = None
    run_start: float | None = None
    ranges: list[dict[str, float]] = []
    for current in times:
        frame = frame_at(capture, float(current))
        if frame is None:
            continue
        gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
        difference = float(np.mean(cv2.absdiff(previous, gray))) if previous is not None else 999.0
        is_frozen = difference < 0.35
        if is_frozen and run_start is None:
            run_start = max(0.0, float(current) - 0.20)
        if not is_frozen and run_start is not None:
            if float(current) - run_start >= 0.80:
                ranges.append({"start": round(run_start, 2), "end": round(float(current), 2)})
            run_start = None
        previous = gray
    if run_start is not None and duration - run_start >= 0.80:
        ranges.append({"start": round(run_start, 2), "end": round(duration, 2)})
    return ranges


def main() -> None:
    payload = json.loads((CATALOG_ROOT / "catalog.json").read_text(encoding="utf-8"))
    assets = [
        asset
        for asset in payload["assets"]
        if asset.get("media_type") == "video"
        and asset.get("auto_eligible")
        and any(
            str(concept_id).startswith(POOL_PREFIX)
            for concept_id in (asset.get("video_taxonomy") or {}).get("fallback_concept_ids", [])
        )
    ]
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    sheets_root = OUTPUT_ROOT / "sheets"
    sheets_root.mkdir(parents=True, exist_ok=True)
    records = []
    label_font = font(24)
    time_font = font(17)
    for sequence, asset in enumerate(assets, start=1):
        video_path = CATALOG_ROOT / asset["resource"]["video"]
        capture = cv2.VideoCapture(str(video_path))
        duration = float(asset["resource"]["duration_us"]) / 1_000_000
        times = np.linspace(min(0.15, duration / 10), max(0.15, duration - 0.15), 12)
        sheet_path = sheets_root / f"{sequence:03d}_{safe_name(asset['asset_id'])}.jpg"
        if not sheet_path.exists():
            sheet = Image.new("RGB", (960, 650), "#111111")
            draw = ImageDraw.Draw(sheet)
            draw.text((16, 10), f"{sequence:03d}  {asset['name']}  {duration:.2f}s", fill="white", font=label_font)
            draw.text((16, 44), asset["asset_id"], fill="#9fd3ff", font=time_font)
            for index, seconds in enumerate(times):
                frame = frame_at(capture, float(seconds))
                if frame is None:
                    continue
                thumb = fit_frame(frame, 230, 175)
                x = 5 + (index % 4) * 238
                y = 72 + (index // 4) * 190
                sheet.paste(thumb, (x, y))
                ImageDraw.Draw(sheet).text((x + 4, y + 3), f"{seconds:.1f}s", fill="white", stroke_width=2, stroke_fill="black", font=time_font)
            sheet.save(sheet_path, quality=88)
        capture.release()
        records.append({
            "sequence": sequence,
            "asset_id": asset["asset_id"],
            "name": asset["name"],
            "video": str(video_path),
            "duration_seconds": round(duration, 3),
            "pools": [value for value in asset["video_taxonomy"]["fallback_concept_ids"] if str(value).startswith(POOL_PREFIX)],
            "freeze_ranges": [],
            "sheet": str(sheet_path),
        })

    overview_font = font(30)
    for group_index in range(math.ceil(len(records) / 6)):
        group = records[group_index * 6 : group_index * 6 + 6]
        overview = Image.new("RGB", (1920, 1950), "#050505")
        draw = ImageDraw.Draw(overview)
        draw.text((20, 10), f"编辑型空镜池复核 {group_index + 1}/{math.ceil(len(records) / 6)}", fill="white", font=overview_font)
        for index, record in enumerate(group):
            sheet = Image.open(record["sheet"])
            x = (index % 2) * 960
            y = 55 + (index // 2) * 630
            overview.paste(sheet.resize((960, 650)), (x, y))
        overview.save(OUTPUT_ROOT / f"overview_{group_index + 1:02d}.jpg", quality=90)

    (OUTPUT_ROOT / "audit.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"assets": len(records), "freeze_flagged": sum(bool(item["freeze_ranges"]) for item in records), "overviews": math.ceil(len(records) / 6)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
