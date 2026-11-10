from __future__ import annotations

import json
from pathlib import Path


DRAFT_CONTENT = Path(
    r"D:\剪映草稿\JianyingPro Drafts\语义视觉-运动视频与Broll验收-20260810\draft_content.json"
)
OLD_IMAGE = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\bundles\egg_boiled\resources\sticker\singleImage.png"
)
NEW_IMAGE = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library\bundles\cucumber_salad_lower_fade_02\resources\sticker\singleImage.png"
)


def main() -> None:
    if not DRAFT_CONTENT.is_file():
        raise SystemExit(f"draft_content.json not found: {DRAFT_CONTENT}")
    if not NEW_IMAGE.is_file():
        raise SystemExit(f"cucumber image not found: {NEW_IMAGE}")

    payload = json.loads(DRAFT_CONTENT.read_text(encoding="utf-8"))
    track = next(
        (
            item
            for item in payload.get("tracks", [])
            if item.get("name") == "验收-下方食物图片"
        ),
        None,
    )
    if not isinstance(track, dict) or len(track.get("segments", [])) != 1:
        raise SystemExit("acceptance food track is missing or ambiguous")

    segment = track["segments"][0]
    material_id = segment.get("material_id")
    material = next(
        (
            item
            for item in payload.get("materials", {}).get("videos", [])
            if item.get("id") == material_id
        ),
        None,
    )
    if not isinstance(material, dict):
        raise SystemExit("acceptance food material is missing")
    if Path(str(material.get("path", ""))) not in {OLD_IMAGE, NEW_IMAGE}:
        raise SystemExit(f"unexpected acceptance food image: {material.get('path')}")

    # 1254x1254 image on the 736x1280 canvas.  A requested width ratio of 0.60
    # is capped by the production transform at 30% visible canvas height.
    resolved_scale = 0.30 / (736 / 1280)
    material["path"] = str(NEW_IMAGE)
    material["width"] = 1254
    material["height"] = 1254
    clip = segment["clip"]
    clip["scale"]["x"] = resolved_scale
    clip["scale"]["y"] = resolved_scale
    clip["transform"]["x"] = -1.0 + 0.08 + resolved_scale
    clip["transform"]["y"] = -1.0 + 0.08 + 0.30

    nameplate_track = next(
        (
            item
            for item in payload.get("tracks", [])
            if item.get("name") == "验收-固定人名牌"
        ),
        None,
    )
    if not isinstance(nameplate_track, dict) or len(
        nameplate_track.get("segments", [])
    ) != 1:
        raise SystemExit("acceptance nameplate track is missing or ambiguous")
    nameplate_clip = nameplate_track["segments"][0]["clip"]
    nameplate_clip["scale"]["x"] = 0.46
    nameplate_clip["scale"]["y"] = 0.46
    nameplate_clip["transform"]["x"] = -1.0 + 0.12 + 0.46
    nameplate_clip["transform"]["y"] = 0.0

    caption_texts = [
        "凉拌黄瓜热量比较低",
        "接下来做胯下击掌燃脂操",
        "保持核心发力，配合有氧动作",
        "动作做完再回到讲解画面",
    ]
    caption_track = next(
        (
            item
            for item in payload.get("tracks", [])
            if item.get("name") == "网页自动字幕"
        ),
        None,
    )
    if not isinstance(caption_track, dict) or len(
        caption_track.get("segments", [])
    ) != len(caption_texts):
        raise SystemExit("acceptance caption track is missing or unexpected")
    text_materials = {
        item.get("id"): item
        for item in payload.get("materials", {}).get("texts", [])
    }
    for segment, caption_text in zip(
        sorted(
            caption_track["segments"],
            key=lambda item: int(item["target_timerange"]["start"]),
        ),
        caption_texts,
        strict=True,
    ):
        text_material = text_materials.get(segment.get("material_id"))
        if not isinstance(text_material, dict):
            raise SystemExit("acceptance caption material is missing")
        content = json.loads(text_material["content"])
        content["text"] = caption_text
        for style in content.get("styles", []):
            style["range"] = [0, len(caption_text)]
        text_material["content"] = json.dumps(content, ensure_ascii=False)

    DRAFT_CONTENT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        "updated acceptance overlays: cucumber bottom-left at 30% height; "
        f"resolved_scale={resolved_scale:.9f}; nameplate width=46%; "
        "captions aligned to food/action/B-roll windows"
    )


if __name__ == "__main__":
    main()
