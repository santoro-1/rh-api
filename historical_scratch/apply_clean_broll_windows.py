from __future__ import annotations

import argparse
import json
from pathlib import Path


CATALOG = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\catalog.json"
)
SAFE_STARTS = {
    "review.u0042.video.93e2eea161": 600_000,
    "review.u0111.video.fb02d5d0c2": 0,
    "review.u0140.video.15b5a0e7df": 0,
    "review.u0144.video.64dc59739c": 800_000,
    "review.u0280.video.946f759f1a": 800_000,
    "review.u0317.video.e79db70509": 700_000,
    "review.u0453.video.b4c6a6dcb6": 0,
}
TAG = "自动空镜仅使用干净源窗口"
NOTE = "自动空镜排除源视频中的暂停、加载或鼠标点击/回放动画，仅使用已复核干净窗口"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    assets = {asset["asset_id"]: asset for asset in payload["assets"]}
    changes = []
    metadata_updates: list[tuple[Path, dict]] = []
    for asset_id, safe_start_us in SAFE_STARTS.items():
        asset = assets[asset_id]
        defaults = asset["defaults"]
        clean_duration_us = min(
            int(defaults["duration_us"]),
            int(asset["resource"]["duration_us"]) - safe_start_us,
        )
        if clean_duration_us <= 0:
            raise RuntimeError(f"invalid clean source window: {asset_id}")
        defaults["source_start_us"] = safe_start_us
        defaults["duration_us"] = clean_duration_us
        asset["platform_ui_status"] = "embedded"
        asset["tags"] = list(dict.fromkeys([*asset.get("tags", []), TAG]))

        metadata_rel = asset["resource"].get("metadata")
        if metadata_rel:
            metadata_path = CATALOG.parent / metadata_rel
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["review_decision"] = "APPROVE_CLIPPED"
            metadata["approved_source_start_us"] = safe_start_us
            metadata["approved_source_end_us"] = safe_start_us + clean_duration_us
            previous = str(metadata.get("review_notes") or "").strip()
            if NOTE not in previous:
                metadata["review_notes"] = f"{previous}；{NOTE}" if previous else NOTE
            metadata_updates.append((metadata_path, metadata))
        changes.append(
            {
                "asset_id": asset_id,
                "name": asset["name"],
                "source_start_us": safe_start_us,
                "source_duration_us": clean_duration_us,
            }
        )

    print(json.dumps(changes, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    CATALOG.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for path, metadata in metadata_updates:
        path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
