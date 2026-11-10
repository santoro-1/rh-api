import json
import re
from pathlib import Path

from rapidocr_onnxruntime import RapidOCR


MEDIA_DIR = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\music_library_compare_20260811\xlsx_unpacked\xl\media")
OUTPUT_PATH = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\music_library_compare_20260811\ocr_results.json")


def image_number(path: Path) -> int:
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 10**9


engine = RapidOCR()
records = []
for path in [
    value
    for value in sorted(MEDIA_DIR.glob("image*.*"), key=image_number)
    if image_number(value) >= 37
]:
    result, _ = engine(str(path))
    lines = []
    for item in result or []:
        box, text, score = item
        ys = [point[1] for point in box]
        lines.append(
            {
                "text": text,
                "score": round(float(score), 4),
                "top": round(float(min(ys)), 1),
                "bottom": round(float(max(ys)), 1),
            }
        )
    records.append({"file": path.name, "lines": lines})
    print(path.name, "|", " || ".join(line["text"] for line in lines))

OUTPUT_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
