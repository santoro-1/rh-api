from __future__ import annotations

import json
from pathlib import Path


DRAFT_CONTENT = Path(
    r"D:\剪映草稿\JianyingPro Drafts\语义视觉-运动视频与Broll验收-20260810\draft_content.json"
)
OLD_BASES = {
    Path(
        r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\template_library\imported_d4ad83d477ae44ddb141e534793db162_YZ-__00001_p87-_1dc86009\assets\0003_d4ad83d477ae44ddb141e534793db162_YZ-数字人_00001_p87-audio_cveth_1783677159.mp4"
    ),
    Path(
        r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\runtime\test_environment\template_library\imported_d4ad83d477ae44ddb141e534793db162_YZ-__00001_p87-_1dc86009\assets\0003_d4ad83d477ae44ddb141e534793db162_YZ-数字人_00001_p87-audio_cveth_1783677159.mp4"
    ),
}
NEW_BASE = Path(
    r"D:\剪映草稿\JianyingPro Drafts\语义视觉-运动视频与Broll验收-20260810\Resources\semantic_acceptance_base.mp4"
)


def main() -> None:
    if not DRAFT_CONTENT.is_file():
        raise SystemExit(f"draft_content.json not found: {DRAFT_CONTENT}")
    if not NEW_BASE.is_file():
        raise SystemExit(f"replacement base video not found: {NEW_BASE}")

    payload = json.loads(DRAFT_CONTENT.read_text(encoding="utf-8"))
    replacements = 0
    for material in payload.get("materials", {}).get("videos", []):
        current_path = Path(str(material.get("path", "")))
        if current_path in OLD_BASES or current_path == NEW_BASE:
            material["path"] = str(NEW_BASE)
            material["width"] = 576
            material["height"] = 1024
            replacements += 1

    for track in payload.get("tracks", []):
        if track.get("name") != "video":
            continue
        for segment in track.get("segments", []):
            segment["volume"] = 0.0

    if replacements != 1:
        raise SystemExit(f"expected one base-video replacement, got {replacements}")

    DRAFT_CONTENT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"rebound {replacements} base-video path: {NEW_BASE}")


if __name__ == "__main__":
    main()
