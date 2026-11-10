from __future__ import annotations

from pathlib import Path

from jyd_probe.content_replace import (
    AudioAddition,
    ContentReplaceJob,
    ImageAddition,
    VideoOverlayAddition,
    run_content_replace_job,
)


WORKBENCH = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
LIBRARY = WORKBENCH / "data" / "libraries" / "semantic_visual_library"
TEMPLATE = (
    WORKBENCH
    / "runtime"
    / "test_environment"
    / "template_library"
    / "imported_d4ad83d477ae44ddb141e534793db162_YZ-__00001_p87-_1dc86009"
    / "draft"
)
OUTPUT_ROOT = Path(r"D:\剪映草稿\JianyingPro Drafts")


result = run_content_replace_job(
    ContentReplaceJob(
        template_draft_dir=TEMPLATE,
        output_root=OUTPUT_ROOT,
        output_name="语义视觉-运动视频与Broll验收-20260810",
        audio_additions=[
            AudioAddition(
                media_path=(
                    WORKBENCH
                    / "data"
                    / "libraries"
                    / "audio_library"
                    / "files"
                    / "Happy_People_6867663002858948609.mp3"
                ),
                target_start_us=0,
                target_duration_us=25_066_666,
                volume=0.18,
            )
        ],
        image_additions=[
            ImageAddition(
                image_path=(
                    LIBRARY
                    / "bundles"
                    / "cucumber_salad_lower_fade_02"
                    / "resources"
                    / "sticker"
                    / "singleImage.png"
                ),
                start_us=1_500_000,
                duration_us=1_800_000,
                corner="bottom_left",
                scale=0.60,
                track_name="验收-下方食物图片",
                optional=False,
                layer_order=10,
            ),
            ImageAddition(
                image_path=(
                    LIBRARY
                    / "fixed"
                    / "nameplate_zhangluo"
                    / "resources"
                    / "sticker"
                    / "singleImage.png"
                ),
                start_us=0,
                duration_us=25_066_666,
                corner="middle_left",
                scale=0.46,
                track_name="验收-固定人名牌",
                optional=False,
                layer_order=20,
            ),
        ],
        video_overlay_additions=[
            VideoOverlayAddition(
                video_path=(
                    LIBRARY
                    / "videos"
                    / "activity_aerobic_crotch_clap_01"
                    / "video.mp4"
                ),
                start_us=7_000_000,
                duration_us=4_000_000,
                source_start_us=0,
                mute=True,
                loop=False,
                fit="contain",
                corner="bottom_right",
                scale=0.42,
                track_name="验收-运动动作小窗",
                optional=False,
                layer_order=10,
            ),
            VideoOverlayAddition(
                video_path=(
                    LIBRARY
                    / "videos"
                    / "activity_aerobic_core_broll_01"
                    / "video.mp4"
                ),
                start_us=15_000_000,
                duration_us=5_000_000,
                source_start_us=12_000_000,
                mute=True,
                loop=False,
                fit="cover",
                corner="center",
                scale=1.0,
                track_name="验收-全屏B-roll",
                optional=False,
                layer_order=30,
            ),
        ],
    )
)

print(result.output_dir)
