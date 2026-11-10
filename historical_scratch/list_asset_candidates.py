from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(sys.argv[1]).resolve()
    terms = [term for term in sys.argv[2:] if term]
    rows = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        rel = str(path.relative_to(root))
        if not any(term.lower() in rel.lower() for term in terms):
            continue
        try:
            with Image.open(path) as image:
                image.load()
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                transparent = has_alpha and image.convert("RGBA").getchannel("A").getextrema()[0] < 255
                rows.append(
                    {
                        "path": rel,
                        "format": str(image.format),
                        "mode": image.mode,
                        "width": image.width,
                        "height": image.height,
                        "transparent": transparent,
                        "bytes": path.stat().st_size,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            rows.append({"path": rel, "error": type(exc).__name__})
    rows.sort(key=lambda row: row["path"])
    print(json.dumps({"terms": terms, "count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
