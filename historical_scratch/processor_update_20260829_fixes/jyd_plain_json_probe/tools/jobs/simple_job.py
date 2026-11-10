from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.content_replace import (  # noqa: E402
    AudioAddition,
    ContentReplaceJob,
    EffectAddition,
    NestedTextFontReplacement,
    NestedTextStylePresetReplacement,
    NestedVideoReplacement,
    SubtitleLine,
    SubtitleRangeReplacement,
    TextFontReplacement,
    TextReplacement,
    TextStylePresetReplacement,
    VideoSegmentReplacement,
    run_content_replace_job,
)


# 基础草稿路径：
# TEMPLATE_DRAFT_DIR: 原模板草稿目录，里面必须有明文 draft_content.json。
# OUTPUT_ROOT: 新草稿输出父目录，通常就是剪映的 JianyingPro Drafts 目录。
# OUTPUT_NAME: 新草稿文件夹名；留空会自动生成，不会覆盖原草稿。
TEMPLATE_DRAFT_DIR = r"D:\剪映草稿\JianyingPro Drafts\模板测试2"
OUTPUT_ROOT = r"D:\剪映草稿\JianyingPro Drafts"
OUTPUT_NAME = ""  # 留空则自动生成


# 日志开关：
# DUMP_NESTED_DRAFTS: 打印复合模板内部轨道和片段下标。第一次分析模板时建议 True。
# DUMP_EFFECTS: 打印顶层特效轨道和 materials.video_effects。需要调特效时再打开。
DUMP_NESTED_DRAFTS = True
DUMP_EFFECTS = False


# 普通顶层视频片段替换：
# 适用于普通草稿里 tracks[type="video"] 直接有多个片段的情况。
# 复合模板通常不要用这里，而是用下面的 NESTED_VIDEO_REPLACEMENTS。
#
# media_path: 新视频或图片路径。
# track_index: 顶层视频轨道下标，只统计 type="video" 的轨道，从 0 开始。
# segment_index: 该视频轨道内的片段下标，从 0 开始。
# source_start_us: 从新素材第几微秒开始截取；-1 表示默认从 0 开始。
# source_duration_us: 从新素材截取多长；0 表示默认。
# target_start_us: 片段在时间线上的开始时间；-1 表示不改。
# target_duration_us: 片段在时间线上的持续时间；0 表示不改。
VIDEO_SEGMENT_REPLACEMENTS = [
    # VideoSegmentReplacement(
    #     media_path=r"D:\素材\new_video.mp4",
    #     track_index=0,
    #     segment_index=0,
    #     source_start_us=-1,
    #     source_duration_us=0,
    #     target_start_us=-1,
    #     target_duration_us=0,
    # ),
]


# 嵌套模板内部图片/视频替换：
# 适用于剪映模板/复合片段。先运行一次 DUMP_NESTED_DRAFTS=True，看日志里的下标。
# 模板测试2 的日志显示：nested_draft_index=0, video_track_index=0, segment_index=0..10。
#
# media_path: 新图片或视频路径，图片和视频都写这里。
# nested_draft_index: 嵌套草稿下标，对应日志 materials.drafts[*].draft，一般是 0。
# video_track_index: 内部视频轨道下标，对应日志 nested video track[...]。
# segment_index: 内部视频片段下标，对应日志 nested video segment[track=..., segment=...]。
# source_start_us: 替换成视频时，从新视频第几微秒开始截取；图片通常不用写。
# source_duration_us: 替换成视频时，从新视频截取多长；图片通常不用写。
# target_start_us: 这个模板片段在内部时间线上的开始时间；一般不改，保持 -1。
# target_duration_us: 这个模板片段在内部时间线上的持续时间；一般不改，保持 0。
#
# 时间单位都是微秒：1 秒 = 1_000_000，2.2 秒 = 2_200_000。
NESTED_VIDEO_REPLACEMENTS = [
    # 替换第 1 张图
    # NestedVideoReplacement(
    #     media_path=r"D:\素材\new_image_01.png",
    #     nested_draft_index=0,
    #     video_track_index=0,
    #     segment_index=0,
    # ),
    # 替换第 2 张图
    # NestedVideoReplacement(
    #     media_path=r"D:\素材\new_image_02.png",
    #     nested_draft_index=0,
    #     video_track_index=0,
    #     segment_index=1,
    # ),
    # 替换成视频，并只截取前 2.2 秒
    # NestedVideoReplacement(
    #     media_path=r"D:\素材\new_video_01.mp4",
    #     nested_draft_index=0,
    #     video_track_index=0,
    #     segment_index=0,
    #     source_start_us=0,
    #     source_duration_us=2_200_000,
    # ),
]


# 文本替换：
# text: 新文本内容。
# track_index: 顶层文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
# segment_index: 该文本轨道里的片段下标，从 0 开始。
# start_us: 文本出现时间；-1 表示不改。
# duration_us: 文本持续时间；0 表示不改。
# 没有文本时保持空列表。
TEXT_REPLACEMENTS = [
    # TextReplacement(
    #     text="新的字幕内容",
    #     track_index=0,
    #     segment_index=0,
    #     start_us=-1,
    #     duration_us=0,
    # ),
]


# 顶层已有文本字体替换：
# font_name: pyJianYingDraft.FontType 里的字体名，比如 "文轩体"、"HarmonyOS_Sans_SC_Regular"。
# font_id: 如果你从模板库里记录到了剪映字体资源 id，可以不用 font_name，直接填 font_id。
# font_path: 普通剪映字体保持默认 "D:" 即可。
# track_index/segment_index: 顶层文本轨道和文本片段下标，从 DUMP 日志里看。
TEXT_FONT_REPLACEMENTS = [
    # TextFontReplacement(
    #     font_name="文轩体",
    #     track_index=0,
    #     segment_index=0,
    # ),
]


# 顶层已有文本应用“样式 JSON”：
# style_json_path: 用 export_text_style.py 从参考文本导出的样式 JSON。
# text: 可选；留空表示只换样式，不改文字内容。
# apply_clip: 是否同时复制位置、缩放、旋转等 clip 设置。
# track_index/segment_index: 顶层文本轨道和文本片段下标，从 DUMP 日志里看。
TEXT_STYLE_PRESET_REPLACEMENTS = [
    # TextStylePresetReplacement(
    #     style_json_path=r"D:\工作内容\轻盈健\公寓\text_style_library\style_01.json",
    #     text="新的文字内容",
    #     apply_clip=True,
    #     track_index=0,
    #     segment_index=0,
    # ),
]


# 嵌套模板内部已有文本字体替换：
# 适用于文字藏在 materials.drafts[*].draft 里的模板。
# nested_draft_index: 嵌套草稿下标，一般是 0。
# text_track_index: 嵌套草稿内部文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
# segment_index: 该内部文本轨道的文本片段下标，从 0 开始。
NESTED_TEXT_FONT_REPLACEMENTS = [
    # NestedTextFontReplacement(
    #     font_name="文轩体",
    #     nested_draft_index=0,
    #     text_track_index=0,
    #     segment_index=0,
    # ),
]


# 嵌套模板内部已有文本应用“样式 JSON”：
# 适用于文字藏在 materials.drafts[*].draft 里的模板。
# text: 可选；留空表示只换样式，不改文字内容。
# apply_clip: 是否同时复制位置、缩放、旋转等 clip 设置。
NESTED_TEXT_STYLE_PRESET_REPLACEMENTS = [
    # NestedTextStylePresetReplacement(
    #     style_json_path=r"D:\工作内容\轻盈健\公寓\text_style_library\style_01.json",
    #     text="新的文字内容",
    #     apply_clip=True,
    #     nested_draft_index=0,
    #     text_track_index=0,
    #     segment_index=0,
    # ),
]


# 新增音乐：
# media_path: 音频路径。
# source_start_us/source_duration_us: 从音频里截取哪一段，单位微秒；默认不截。
# target_start_us: 音乐放到时间线第几微秒，默认 0。
# target_duration_us: 音乐持续多久；0 表示使用素材时长。
# 没有音乐时保持空列表。
SUBTITLE_RANGE_REPLACEMENTS = [
    # SubtitleRangeReplacement(
    #     start_us=10_000_000,
    #     end_us=20_000_000,
    #     track_index=0,
    #     base_segment_index=0,
    #     style_json_path=r"D:\工作内容\轻盈健\公寓\text_style_library\subtitle_style.json",
    #     apply_clip=True,
    #     subtitles=[
    #         SubtitleLine(start_us=0, duration_us=1_500_000, text="这是 B 视频第一句字幕"),
    #         SubtitleLine(start_us=1_500_000, duration_us=2_000_000, text="这是 B 视频第二句字幕"),
    #         SubtitleLine(start_us=3_500_000, duration_us=1_800_000, text="这是 B 视频第三句字幕"),
    #     ],
    # ),
]


AUDIO_ADDITIONS = [
    # AudioAddition(
    #     media_path=r"D:\素材\music.mp3",
    #     target_start_us=0,
    #     target_duration_us=5_000_000,
    # ),
]


# 添加已经导出的特效 JSON：
# effect_json_path: 之前从剪映草稿里导出的特效 JSON。
# target_video_track_index: 顶层目标视频轨道下标，从 0 开始。
# target_video_segment_index: 顶层目标视频片段下标，从 0 开始。
# start_us: 特效开始时间；-1 表示跟随目标视频片段。
# duration_us: 特效持续时间；0 表示跟随目标视频片段。
# 没有特效时保持空列表。
EFFECT_ADDITIONS = [
    # EffectAddition(
    #     effect_json_path=r"D:\工作内容\轻盈健\公寓\effect_library\example_effect.json",
    #     target_video_track_index=0,
    #     target_video_segment_index=0,
    #     start_us=-1,
    #     duration_us=0,
    # ),
]


def build_job() -> ContentReplaceJob:
    return ContentReplaceJob(
        template_draft_dir=TEMPLATE_DRAFT_DIR,
        output_root=OUTPUT_ROOT,
        output_name=OUTPUT_NAME,
        dump_nested_drafts=DUMP_NESTED_DRAFTS,
        dump_effects=DUMP_EFFECTS,
        video_segment_replacements=VIDEO_SEGMENT_REPLACEMENTS,
        nested_video_replacements=NESTED_VIDEO_REPLACEMENTS,
        text_replacements=TEXT_REPLACEMENTS,
        text_font_replacements=TEXT_FONT_REPLACEMENTS,
        text_style_preset_replacements=TEXT_STYLE_PRESET_REPLACEMENTS,
        subtitle_range_replacements=SUBTITLE_RANGE_REPLACEMENTS,
        nested_text_font_replacements=NESTED_TEXT_FONT_REPLACEMENTS,
        nested_text_style_preset_replacements=NESTED_TEXT_STYLE_PRESET_REPLACEMENTS,
        audio_additions=AUDIO_ADDITIONS,
        effect_additions=EFFECT_ADDITIONS,
    )


if __name__ == "__main__":
    result = run_content_replace_job(build_job())
    print(f"输出草稿: {result.output_dir}")
