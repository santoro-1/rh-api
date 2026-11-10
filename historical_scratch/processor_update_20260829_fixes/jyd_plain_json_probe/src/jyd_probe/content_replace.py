from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import argparse
import json
import math
from typing import Any

from .cli import (
    add_audio_track_segment,
    add_text_track_segment,
    add_effect_json_to_video,
    copy_template_draft,
    first_material_name,
    import_pyjianyingdraft,
    load_effect_json,
    load_output_script,
    load_plain_draft_json,
    log,
    log_effect_details,
    log_nested_draft_details,
    max_segment_render_index,
    new_json_id,
    replace_audio_by_name,
    replace_audio_segment_by_index,
    replace_first_text,
    replace_nested_video_segment_by_index,
    replace_text_by_index,
    replace_video_by_name,
    replace_video_segment_by_index,
    save_plain_draft_json,
    set_first_video_duration,
    summarize_draft_json,
    nested_draft_refs,
    text_tracks,
    validate_template_with_pyjyd,
)
from .text_asset_apply import apply_text_effect_to_track, add_text_template_to_data
from .sticker_apply import add_fullscreen_sticker_to_data
from .image_apply import add_image_overlay_to_data
from .video_overlay_apply import add_video_overlay_to_data
from .draft_compat import normalize_draft_for_legacy_editor
from .visual_variant import VisualVariant, apply_visual_variant_to_data
from .cover_apply import (
    COVER_TRACK_PREFIX,
    CoverConfig,
    PreparedCover,
    add_cover_tracks,
    apply_cover_timeline_offset,
    prepare_cover_assets,
    rebase_cover_material_paths,
)


PathLike = str | Path
TEXT_STYLE_SCHEMA = "jyd_probe.text_style.v1"
TIMELINE_TAIL_FOLLOW_MIN_DURATION_US = 5_000_000
TEXT_MATERIAL_STYLE_KEYS = {
    "typesetting",
    "alignment",
    "letter_spacing",
    "line_spacing",
    "line_feed",
    "line_max_width",
    "force_apply_line_max_width",
    "check_flag",
    "type",
    "global_alpha",
    "background_style",
    "background_color",
    "background_alpha",
    "background_round_radius",
    "background_height",
    "background_width",
    "background_horizontal_offset",
    "background_vertical_offset",
}
TEXT_SEGMENT_STYLE_KEYS = {"clip", "uniform_scale"}


@dataclass(frozen=True)
class NamedVideoReplacement:
    """按素材名替换顶层 materials.videos 里的视频/图片。

    media_path: 新视频或图片路径。
    material_name: 原素材名；留空时使用顶层第一个视频/图片素材名。
    """

    media_path: PathLike
    material_name: str = ""


@dataclass(frozen=True)
class VideoSegmentReplacement:
    """替换普通顶层视频轨道里的指定片段。

    media_path: 新视频或图片路径。
    track_index: 顶层视频轨道下标，只统计 type="video" 的轨道，从 0 开始。
    segment_index: 该视频轨道内的片段下标，从 0 开始。
    source_start_us: 从新素材的第几微秒开始截取；-1 表示默认从 0 开始。
    source_duration_us: 从新素材截取多长；0 表示使用默认时长。
    target_start_us: 片段放在时间线的开始时间；-1 表示不改原开始时间。
    target_duration_us: 片段在时间线持续多久；0 表示不改原持续时间。
    """

    media_path: PathLike
    track_index: int = 0
    segment_index: int = 0
    source_start_us: int = -1
    source_duration_us: int = 0
    target_start_us: int = -1
    target_duration_us: int = 0


@dataclass(frozen=True)
class NestedVideoReplacement:
    """替换复合模板内部的图片/视频。

    media_path: 新视频或图片路径。
    nested_draft_index: 嵌套草稿下标，对应日志里的 materials.drafts[*].draft，从 0 开始。
    video_track_index: 嵌套草稿内部的视频轨道下标，对应日志里的 nested video track[...]。
    segment_index: 该内部视频轨道的片段下标，对应日志里的 nested video segment[... segment=N]。
    source_start_us: 从新视频的第几微秒开始截取；图片通常保持 -1。
    source_duration_us: 从新视频截取多长；图片通常保持 0。替换视频时可显式写原片段时长。
    target_start_us: 内部片段在模板时间线的开始时间；通常保持 -1，不改模板节奏。
    target_duration_us: 内部片段在模板时间线持续多久；通常保持 0，不改模板节奏。
    """

    media_path: PathLike
    nested_draft_index: int = 0
    video_track_index: int = 0
    segment_index: int = 0
    source_start_us: int = -1
    source_duration_us: int = 0
    target_start_us: int = -1
    target_duration_us: int = 0


@dataclass(frozen=True)
class TextReplacement:
    """替换顶层文本轨道里的指定文本片段。

    text: 新文本内容。
    track_index: 顶层文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    segment_index: 该文本轨道内的片段下标，从 0 开始。
    start_us: 文本片段出现时间；-1 表示不改原开始时间。
    duration_us: 文本片段持续时间；0 表示不改原持续时间。
    """

    text: str = ""
    track_index: int = 0
    segment_index: int = 0
    start_us: int = -1
    duration_us: int = 0


@dataclass(frozen=True)
class TextAddition:
    """新增一个顶层文字轨道和文字片段。

    text: 新文字内容。
    start_us/duration_us: 文字在时间线上的开始和持续时间，单位微秒。
    track_name: 新文字轨道名称；留空时按列表下标自动生成稳定名称。
    style_json_path: 可选；用 export_text_style_preset 导出的样式 JSON 套用到新增文字。
    apply_clip: 套用样式 JSON 时是否复制位置、缩放、旋转等 clip 设置。
    relative_index: 新文字轨道相对同类型轨道的层级，越大越靠前。
    transform_x/transform_y: 未使用样式 JSON 时的默认位置，单位为半个画布宽/高。
    """

    text: str
    start_us: int = 0
    duration_us: int = 5_000_000
    track_name: str = ""
    style_json_path: PathLike = ""
    text_effect_json_path: PathLike = ""
    apply_clip: bool = True
    relative_index: int = 999
    transform_x: float = 0.0
    transform_y: float = 0.0
    scale: float = 1.0
    size: float = 8.0
    align: int = 1
    auto_wrapping: bool = False
    line_max_width: float | None = None
    color: str = ""
    stroke_color: str = ""
    stroke_width: float | None = None
    opacity: float = 1.0
    letter_spacing: float | None = None
    shadow_color: str = ""
    shadow_alpha: float | None = None
    shadow_distance: float | None = None
    shadow_angle: float | None = None
    shadow_smoothing: float | None = None
    font_id: str = ""
    font_path: str = ""
    font_title: str = ""


@dataclass(frozen=True)
class TextTemplateAddition:
    """Add one collected composite text template as a new top-level text track."""

    template_json_path: PathLike
    texts: list[str] = field(default_factory=list)
    start_us: int = 0
    duration_us: int = 0
    track_name: str = ""


@dataclass(frozen=True)
class TextFontReplacement:
    """替换顶层已有文本片段的字体。

    font_name: pyJianYingDraft 字体名，例如 "文轩体" 或 "HarmonyOS_Sans_SC_Regular"。
    font_id: 剪映字体资源 id；如果提供 font_id，会优先使用它。
    font_path: 写入 JSON 的字体 path 字段，普通剪映字体保持默认 "D:" 即可。
    track_index: 顶层文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    segment_index: 该文本轨道内的片段下标，从 0 开始。
    """

    font_name: str = ""
    font_id: str = ""
    font_path: str = "D:"
    font_title: str = ""
    track_index: int = 0
    segment_index: int = 0


@dataclass(frozen=True)
class TextStylePresetReplacement:
    """把已导出的文本样式 JSON 应用到顶层已有文本片段。

    style_json_path: 由 export_text_style_preset 导出的样式 JSON。
    text: 可选的新文本内容；留空表示只换样式，不改文字。
    apply_clip: 是否复制位置/缩放/旋转等 clip 设置。
    track_index: 顶层文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    segment_index: 该文本轨道内的文本片段下标，从 0 开始。
    """

    style_json_path: PathLike
    text: str = ""
    apply_clip: bool = True
    track_index: int = 0
    segment_index: int = 0


@dataclass(frozen=True)
class SubtitleLine:
    """一条准备写入时间线的字幕。

    start_us: 相对 SubtitleRangeReplacement.start_us 的开始时间，单位微秒。
    duration_us: 字幕持续时间，单位微秒。
    text: 字幕文字。
    """

    start_us: int
    duration_us: int
    text: str


@dataclass(frozen=True)
class SubtitleRangeReplacement:
    """删除某个时间段内的旧字幕，并写入一组新字幕。

    start_us/end_us: 主时间线上的替换范围，采用左闭右开区间 [start_us, end_us)。
    subtitles: 新字幕列表；每条 SubtitleLine.start_us 都是相对 start_us 的时间。
    track_index: 顶层文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    base_segment_index: 用哪条原字幕片段作为新增字幕的结构模板。
    style_json_path: 可选；传入 export_text_style.py 导出的样式 JSON 后，新字幕会套用该样式。
    apply_clip: style_json_path 不为空时，是否同时套用位置/缩放/旋转等 clip 设置。
    """

    start_us: int
    end_us: int
    subtitles: list[SubtitleLine] = field(default_factory=list)
    track_index: int = 0
    base_segment_index: int = 0
    style_json_path: PathLike = ""
    apply_clip: bool = True


@dataclass(frozen=True)
class NestedTextFontReplacement:
    """替换复合模板内部已有文本片段的字体。

    font_name: pyJianYingDraft 字体名，例如 "文轩体" 或 "HarmonyOS_Sans_SC_Regular"。
    font_id: 剪映字体资源 id；如果提供 font_id，会优先使用它。
    font_path: 写入 JSON 的字体 path 字段，普通剪映字体保持默认 "D:" 即可。
    nested_draft_index: 嵌套草稿下标，对应日志里的 materials.drafts[*].draft，从 0 开始。
    text_track_index: 嵌套草稿内部文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    segment_index: 该内部文本轨道的文本片段下标，从 0 开始。
    """

    font_name: str = ""
    font_id: str = ""
    font_path: str = "D:"
    font_title: str = ""
    nested_draft_index: int = 0
    text_track_index: int = 0
    segment_index: int = 0


@dataclass(frozen=True)
class NestedTextStylePresetReplacement:
    """把已导出的文本样式 JSON 应用到复合模板内部已有文本片段。

    style_json_path: 由 export_text_style_preset 导出的样式 JSON。
    text: 可选的新文本内容；留空表示只换样式，不改文字。
    apply_clip: 是否复制位置/缩放/旋转等 clip 设置。
    nested_draft_index: 嵌套草稿下标，对应日志里的 materials.drafts[*].draft，从 0 开始。
    text_track_index: 嵌套草稿内部文本轨道下标，只统计 type="text" 的轨道，从 0 开始。
    segment_index: 该内部文本轨道的文本片段下标，从 0 开始。
    """

    style_json_path: PathLike
    text: str = ""
    apply_clip: bool = True
    nested_draft_index: int = 0
    text_track_index: int = 0
    segment_index: int = 0


@dataclass(frozen=True)
class NamedAudioReplacement:
    """按素材名替换顶层 materials.audios 里的音频素材。

    media_path: 新音频路径。
    material_name: 原音频素材名；留空时使用顶层第一个音频素材名。
    """

    media_path: PathLike
    material_name: str = ""


@dataclass(frozen=True)
class AudioSegmentReplacement:
    """替换普通顶层音频轨道里的指定片段。

    media_path: 新音频路径。
    track_index: 顶层音频轨道下标，只统计 type="audio" 的轨道，从 0 开始。
    segment_index: 该音频轨道内的片段下标，从 0 开始。
    source_start_us: 从新音频第几微秒开始截取；-1 表示默认从 0 开始。
    source_duration_us: 从新音频截取多长；0 表示默认。
    target_start_us: 音频片段放在时间线的开始时间；-1 表示不改原开始时间。
    target_duration_us: 音频片段在时间线持续多久；0 表示不改原持续时间。
    """

    media_path: PathLike
    track_index: int = 0
    segment_index: int = 0
    source_start_us: int = -1
    source_duration_us: int = 0
    target_start_us: int = -1
    target_duration_us: int = 0


@dataclass(frozen=True)
class AudioAddition:
    """新增一条顶层音乐轨道。

    media_path: 要新增的音频路径。
    source_start_us: 从音频第几微秒开始截取；-1 表示默认从 0 开始。
    source_duration_us: 从音频截取多长；0 表示默认使用目标时长或音频时长。
    target_start_us: 新音乐在时间线的开始时间，默认 0。
    target_duration_us: 新音乐在时间线持续多久；0 表示使用素材时长。
    loop_to_target: 音乐短于目标时长时是否循环，并裁切最后一次循环到目标结尾。
    fade_in_us: 整条新增音乐从时间线起点渐起的时长，单位微秒；0 表示关闭。
    """

    media_path: PathLike
    source_start_us: int = -1
    source_duration_us: int = 0
    target_start_us: int = 0
    target_duration_us: int = 0
    volume: float = 1.0
    loop_to_target: bool = False
    align_to_end: bool = False
    crossfade_us: int = 0
    fade_in_us: int = 0


@dataclass(frozen=True)
class EffectAddition:
    """把已导出的特效 JSON 添加到顶层目标视频片段上。

    effect_json_path: 之前导出的特效 JSON 文件路径。
    target_video_track_index: 顶层目标视频轨道下标，从 0 开始。
    target_video_segment_index: 顶层目标视频片段下标，从 0 开始。
    start_us: 特效开始时间；-1 表示跟随目标视频片段开始时间。
    duration_us: 特效持续时间；网页生成任务会解析为整个草稿剩余时长。
    """

    effect_json_path: PathLike
    target_video_track_index: int = 0
    target_video_segment_index: int = 0
    start_us: int = -1
    duration_us: int = 0


@dataclass(frozen=True)
class StickerAddition:
    """Add one collected fullscreen sticker as a top-level sticker track."""

    sticker_json_path: PathLike
    start_us: int = 0
    duration_us: int = 0
    corner: str = ""
    visible_ratio: float = 0.05
    scale: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0
    track_name: str = ""
    transform_x: float | None = None
    transform_y: float | None = None
    optional: bool = False
    inside_canvas: bool = False
    render_below_text: bool = False


@dataclass(frozen=True)
class ImageAddition:
    """Add a PNG/JPG as a real photo material on an independent video track."""

    image_path: PathLike
    start_us: int = 0
    duration_us: int = 0
    corner: str = "center"
    scale: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0
    track_name: str = "图片贴图"
    optional: bool = False
    render_below_text: bool = True
    layer_order: int = 0
    transform_x: float | None = None
    transform_y: float | None = None


@dataclass(frozen=True)
class VideoOverlayAddition:
    """Add a semantic video on an independent native video track."""

    video_path: PathLike
    start_us: int
    duration_us: int
    source_start_us: int = 0
    source_duration_us: int = 0
    loop_to_target: bool = False
    mute: bool = True
    fit: str = "cover"
    corner: str = "center"
    scale: float = 1.0
    opacity: float = 1.0
    track_name: str = "语义前景视频"
    optional: bool = False
    render_below_text: bool = True
    layer_order: int = 0


@dataclass(frozen=True)
class ContentReplaceJob:
    """一次草稿替换任务。

    template_draft_dir: 原模板草稿目录，目录内必须有明文 draft_content.json。
    output_root: 新草稿输出父目录，通常是 JianyingPro Drafts。
    output_name: 新草稿文件夹名；留空时自动生成，不覆盖原草稿。
    dump_effects: 是否打印顶层特效结构。
    dump_nested_drafts: 是否打印复合模板内部结构。
    replace_first_text: 快速替换第一条文本轨道的第一个文本片段。
    first_video_target_duration_us: 快速修改第一条视频轨道第一个片段时长；0 表示不改。
    *_replacements / *_additions: 要执行的替换或新增操作列表，可一次传多个。
    """

    template_draft_dir: PathLike
    output_root: PathLike
    output_name: str = ""

    dump_effects: bool = False
    dump_nested_drafts: bool = True

    replace_first_text: str = ""
    first_video_target_duration_us: int = 0
    timeline_duration_us: int = 0
    template_timeline_duration_us: int = 0

    named_video_replacements: list[NamedVideoReplacement] = field(default_factory=list)
    video_segment_replacements: list[VideoSegmentReplacement] = field(default_factory=list)
    nested_video_replacements: list[NestedVideoReplacement] = field(default_factory=list)
    text_replacements: list[TextReplacement] = field(default_factory=list)
    text_additions: list[TextAddition] = field(default_factory=list)
    text_template_additions: list[TextTemplateAddition] = field(default_factory=list)
    text_font_replacements: list[TextFontReplacement] = field(default_factory=list)
    text_style_preset_replacements: list[TextStylePresetReplacement] = field(default_factory=list)
    subtitle_range_replacements: list[SubtitleRangeReplacement] = field(default_factory=list)
    nested_text_font_replacements: list[NestedTextFontReplacement] = field(default_factory=list)
    nested_text_style_preset_replacements: list[NestedTextStylePresetReplacement] = field(default_factory=list)
    named_audio_replacements: list[NamedAudioReplacement] = field(default_factory=list)
    audio_segment_replacements: list[AudioSegmentReplacement] = field(default_factory=list)
    audio_additions: list[AudioAddition] = field(default_factory=list)
    effect_additions: list[EffectAddition] = field(default_factory=list)
    sticker_additions: list[StickerAddition] = field(default_factory=list)
    image_additions: list[ImageAddition] = field(default_factory=list)
    video_overlay_additions: list[VideoOverlayAddition] = field(default_factory=list)
    visual_variant: VisualVariant | None = None
    cover: CoverConfig | None = None
    original_video_volume: float | None = None
    remove_existing_audio: bool = False
    remove_existing_effects: bool = False


@dataclass(frozen=True)
class ContentReplaceResult:
    """替换任务执行结果。"""

    output_dir: Path
    output_name: str
    top_level_changes: int
    json_changes: int


def _namespace(**values: Any) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _has_any_change(job: ContentReplaceJob) -> bool:
    return bool(
        job.replace_first_text
        or job.first_video_target_duration_us
        or job.timeline_duration_us
        or job.named_video_replacements
        or job.video_segment_replacements
        or job.nested_video_replacements
        or job.text_replacements
        or job.text_additions
        or job.text_template_additions
        or job.text_font_replacements
        or job.text_style_preset_replacements
        or job.subtitle_range_replacements
        or job.nested_text_font_replacements
        or job.nested_text_style_preset_replacements
        or job.named_audio_replacements
        or job.audio_segment_replacements
        or job.audio_additions
        or job.effect_additions
        or job.sticker_additions
        or job.image_additions
        or job.video_overlay_additions
        or job.visual_variant
        or job.cover
        or job.original_video_volume is not None
        or job.remove_existing_audio
        or job.remove_existing_effects
    )


def _apply_top_level_changes(
    draft: Any,
    script: Any,
    job: ContentReplaceJob,
    summary: dict[str, Any],
    prepared_cover: PreparedCover | None = None,
) -> int:
    changed = 0
    materials = summary["materials"]

    if job.replace_first_text:
        changed += int(replace_first_text(draft, script, job.replace_first_text))

    for item in job.text_replacements:
        changed += int(
            replace_text_by_index(
                draft,
                script,
                _namespace(
                    replace_text=item.text,
                    target_text_track_index=item.track_index,
                    target_text_segment_index=item.segment_index,
                    text_start_us=item.start_us,
                    text_duration_us=item.duration_us,
                ),
            )
        )

    for item_index, item in enumerate(job.text_additions):
        changed += int(
            add_text_track_segment(
                draft,
                script,
                _namespace(
                    add_text=item.text,
                    text_track_name=_text_addition_track_name(item, item_index),
                    text_start_us=item.start_us,
                    text_duration_us=item.duration_us,
                    text_relative_index=item.relative_index,
                    text_transform_x=item.transform_x,
                    text_transform_y=item.transform_y,
                    text_scale=item.scale,
                    text_size=item.size,
                    text_align=item.align,
                    text_auto_wrapping=item.auto_wrapping,
                    text_line_max_width=item.line_max_width,
                ),
            )
        )

    for item in job.named_video_replacements:
        material_name = item.material_name or first_material_name(materials, "video")
        if not material_name:
            raise RuntimeError("没有找到可替换的视频素材名，请显式设置 material_name")
        changed += int(replace_video_by_name(draft, script, material_name, Path(item.media_path).resolve()))

    for item in job.video_segment_replacements:
        changed += int(
            replace_video_segment_by_index(
                draft,
                script,
                _namespace(
                    replace_video_segment_path=str(item.media_path),
                    target_video_track_index=item.track_index,
                    target_video_segment_index=item.segment_index,
                    video_source_start_us=item.source_start_us,
                    video_source_duration_us=item.source_duration_us,
                    video_target_start_us=item.target_start_us,
                    video_target_duration_us=item.target_duration_us,
                ),
            )
        )

    for item in job.named_audio_replacements:
        material_name = item.material_name or first_material_name(materials, "audio")
        if not material_name:
            raise RuntimeError("没有找到可替换的音频素材名，请显式设置 material_name")
        changed += int(replace_audio_by_name(draft, script, material_name, Path(item.media_path).resolve()))

    for item in job.audio_segment_replacements:
        changed += int(
            replace_audio_segment_by_index(
                draft,
                script,
                _namespace(
                    replace_audio_segment_path=str(item.media_path),
                    target_audio_track_index=item.track_index,
                    target_audio_segment_index=item.segment_index,
                    audio_source_start_us=item.source_start_us,
                    audio_source_duration_us=item.source_duration_us,
                    audio_target_start_us=item.target_start_us,
                    audio_target_duration_us=item.target_duration_us,
                    audio_volume=item.volume,
                    audio_loop_to_target=item.loop_to_target,
                ),
            )
        )

    for item in job.audio_additions:
        changed += int(
            add_audio_track_segment(
                draft,
                script,
                _namespace(
                    add_audio_path=str(item.media_path),
                    audio_source_start_us=item.source_start_us,
                    audio_source_duration_us=item.source_duration_us,
                    audio_target_start_us=item.target_start_us,
                    audio_target_duration_us=item.target_duration_us,
                    audio_volume=item.volume,
                    audio_loop_to_target=item.loop_to_target,
                    audio_align_to_end=item.align_to_end,
                    audio_crossfade_us=item.crossfade_us,
                    audio_fade_in_us=item.fade_in_us,
                ),
            )
        )

    if job.first_video_target_duration_us:
        changed += int(set_first_video_duration(draft, script, job.first_video_target_duration_us))

    if job.cover is not None and prepared_cover is not None:
        changed += add_cover_tracks(draft, script, prepared_cover, job.cover)

    if changed:
        log(f"顶层轨道共执行 {changed} 项修改")
    elif (
        job.nested_video_replacements
        or job.text_additions
        or job.text_template_additions
        or job.text_font_replacements
        or job.text_style_preset_replacements
        or job.subtitle_range_replacements
        or job.nested_text_font_replacements
        or job.nested_text_style_preset_replacements
        or job.effect_additions
        or job.sticker_additions
        or job.image_additions
        or job.video_overlay_additions
        or job.visual_variant
        or job.cover
        or job.original_video_volume is not None
        or job.remove_existing_audio
        or job.remove_existing_effects
    ):
        log("顶层轨道没有修改，后续将执行 JSON 级修改")
    else:
        log("没有传入替换配置，本次只验证复制、读取、统计、保存流程", "WARN")

    return changed


def _materials_dict(data: dict[str, Any]) -> dict[str, Any]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("当前草稿 materials 不是对象")
    return materials


def _normalize_font_key(value: str) -> str:
    return (
        value.lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
        .replace("（", "")
        .replace("）", "")
        .replace("(", "")
        .replace(")", "")
    )


def _resolve_font_json(draft: Any, font_name: str, font_id: str, font_path: str) -> dict[str, str]:
    if font_id:
        return {"id": font_id, "path": font_path}
    if not font_name:
        raise RuntimeError("替换字体时必须提供 font_name 或 font_id")

    font_type = getattr(draft, "FontType", None)
    if font_type is None:
        raise RuntimeError("当前 pyJianYingDraft 没有暴露 FontType，无法按字体名解析")

    target_key = _normalize_font_key(font_name)
    for font in font_type:
        member_name = getattr(font, "name", "")
        meta = getattr(font, "value", None)
        meta_name = getattr(meta, "name", "")
        if target_key in {
            _normalize_font_key(str(member_name)),
            _normalize_font_key(str(meta_name)),
        }:
            resource_id = getattr(meta, "resource_id", "")
            if not resource_id:
                raise RuntimeError(f"字体 {font_name!r} 缺少 resource_id")
            return {"id": str(resource_id), "path": font_path}

    raise RuntimeError(f"没有在 pyJianYingDraft.FontType 中找到字体: {font_name!r}")


def _find_text_segment_ref(data: dict[str, Any], track_index: int, segment_index: int) -> dict[str, Any]:
    tracks = text_tracks(data)
    if not tracks:
        raise RuntimeError("当前草稿没有文本轨道")
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"文本轨道下标越界: {track_index}，可用范围 [0, {len(tracks)})")

    raw_track_index, track = tracks[track_index]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"文本轨道 {track_index} 的 segments 不是列表")
    if not 0 <= segment_index < len(segments):
        raise IndexError(f"文本片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError(f"文本轨道 {track_index} 的片段 {segment_index} 不是对象")
    material_id = segment.get("material_id")
    if not material_id:
        raise RuntimeError(f"文本轨道 {track_index} 的片段 {segment_index} 缺少 material_id")
    log(
        "已定位文本片段: "
        f"text_track_index={track_index}, raw_track_index={raw_track_index}, "
        f"text_segment_index={segment_index}, material_id={material_id!r}"
    )
    return {
        "raw_track_index": raw_track_index,
        "track": track,
        "segment": segment,
        "material_id": str(material_id),
    }


def _text_addition_track_name(item: TextAddition, item_index: int) -> str:
    return item.track_name.strip() or f"jyd_added_text_{item_index}"


def _find_text_segment_ref_by_track_name(
    data: dict[str, Any],
    track_name: str,
    segment_index: int = 0,
) -> dict[str, Any]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        raise RuntimeError("当前草稿 tracks 不是列表")

    matches = [
        (raw_index, track)
        for raw_index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == "text" and track.get("name") == track_name
    ]
    if not matches:
        raise RuntimeError(f"没有找到新增文字轨道: track_name={track_name!r}")
    if len(matches) > 1:
        raise RuntimeError(f"找到多个同名文字轨道: track_name={track_name!r}")

    raw_track_index, track = matches[0]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"文字轨道 segments 不是列表: track_name={track_name!r}")
    if not 0 <= segment_index < len(segments):
        raise IndexError(f"文字片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError(f"文字片段不是对象: track_name={track_name!r}, segment_index={segment_index}")
    material_id = segment.get("material_id")
    if not material_id:
        raise RuntimeError(f"文字片段缺少 material_id: track_name={track_name!r}, segment_index={segment_index}")

    return {
        "raw_track_index": raw_track_index,
        "track": track,
        "segment": segment,
        "material_id": str(material_id),
    }


def _find_text_segment_material_id(data: dict[str, Any], track_index: int, segment_index: int) -> str:
    return str(_find_text_segment_ref(data, track_index, segment_index)["material_id"])


def _text_materials_for_id(materials: dict[str, Any], material_id: str) -> list[dict[str, Any]]:
    texts = materials.get("texts", [])
    if not isinstance(texts, list):
        texts = []

    direct = [material for material in texts if isinstance(material, dict) and material.get("id") == material_id]
    if direct:
        return direct

    text_templates = materials.get("text_templates", [])
    if not isinstance(text_templates, list):
        text_templates = []

    sub_text_ids: list[str] = []
    for template in text_templates:
        if not isinstance(template, dict) or template.get("id") != material_id:
            continue
        resources = template.get("text_info_resources", [])
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            sub_material_id = resource.get("text_material_id")
            if sub_material_id:
                sub_text_ids.append(str(sub_material_id))

    return [
        material
        for material in texts
        if isinstance(material, dict) and str(material.get("id", "")) in sub_text_ids
    ]


def _parse_text_material_content(material: dict[str, Any]) -> dict[str, Any]:
    content = material.get("content", "")
    if not isinstance(content, str):
        raise RuntimeError(f"文本素材 content 不是字符串: id={material.get('id')!r}")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"文本素材 content 不是合法 JSON: id={material.get('id')!r}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"文本素材 content 顶层不是对象: id={material.get('id')!r}")
    return parsed


def _apply_font_to_text_material(
    material: dict[str, Any],
    font_json: dict[str, str],
    font_title: str = "",
) -> int:
    parsed = _parse_text_material_content(material)

    styles = parsed.get("styles")
    if not isinstance(styles, list) or not styles:
        styles = [{"range": [0, len(str(parsed.get("text", "")))]}]
        parsed["styles"] = styles

    changed = 0
    for style in styles:
        if not isinstance(style, dict):
            continue
        old_font = style.get("font")
        style["font"] = dict(font_json)
        changed += int(old_font != style["font"])

    material["content"] = json.dumps(parsed, ensure_ascii=False)
    font_id = str(font_json.get("id", ""))
    font_path = str(font_json.get("path", ""))
    for key, value in (
        ("font_path", font_path),
        ("font_resource_id", font_id),
        ("font_source_platform", 1),
    ):
        changed += int(material.get(key) != value)
        material[key] = value

    fonts = material.get("fonts")
    font_entry = deepcopy(fonts[0]) if isinstance(fonts, list) and fonts and isinstance(fonts[0], dict) else {}
    if not font_entry.get("id"):
        font_entry["id"] = new_json_id()
    font_entry.update(
        {
            "effect_id": font_id,
            "resource_id": font_id,
            "path": font_path,
            "source_platform": 1,
            "title": font_title or str(font_entry.get("title", "")),
        }
    )
    new_fonts = [font_entry]
    changed += int(fonts != new_fonts)
    material["fonts"] = new_fonts
    return changed


def _replace_text_font_for_material_id(
    materials: dict[str, Any],
    material_id: str,
    font_json: dict[str, str],
    font_title: str = "",
) -> int:
    text_materials = _text_materials_for_id(materials, material_id)
    if not text_materials:
        raise RuntimeError(f"没有找到文本片段引用的文本素材: material_id={material_id!r}")
    changed = 0
    for material in text_materials:
        changed += _apply_font_to_text_material(material, font_json, font_title)
    return changed


def _replace_text_font_in_data(
    draft: Any,
    data: dict[str, Any],
    item: TextFontReplacement,
) -> int:
    font_json = _resolve_font_json(draft, item.font_name, item.font_id, item.font_path)
    material_id = _find_text_segment_material_id(data, item.track_index, item.segment_index)
    materials = _materials_dict(data)
    changed = _replace_text_font_for_material_id(materials, material_id, font_json, item.font_title)
    log(
        "已替换顶层文本字体: "
        f"text_track_index={item.track_index}, text_segment_index={item.segment_index}, "
        f"font={font_json}"
    )
    return changed


def _replace_cover_text_fonts_in_data(
    draft: Any,
    data: dict[str, Any],
    config: CoverConfig,
) -> int:
    font_json = (
        _resolve_font_json(draft, "", config.font_id, config.font_path)
        if config.font_id or config.font_path
        else None
    )
    materials = _materials_dict(data)
    changed = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        name = str(track.get("name", ""))
        if track.get("type") != "text" or not name.startswith(f"{COVER_TRACK_PREFIX}text_"):
            continue
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict) or not segment.get("material_id"):
                continue
            material_id = str(segment["material_id"])
            if font_json is not None:
                changed += _replace_text_font_for_material_id(
                    materials,
                    material_id,
                    font_json,
                    config.font_title,
                )
            line_index = 1 if name.endswith("text_1") else 2
            color = config.line_1_color if line_index == 1 else config.line_2_color
            size = config.line_1_size if line_index == 1 else config.line_2_size
            shadow_color = (
                config.line_1_shadow_color
                if line_index == 1
                else config.line_2_shadow_color
            )
            shadow_alpha = (
                config.line_1_shadow_alpha
                if line_index == 1
                else config.line_2_shadow_alpha
            )
            shadow_smoothing = (
                config.line_1_shadow_smoothing
                if line_index == 1
                else config.line_2_shadow_smoothing
            )
            shadow_distance = (
                config.line_1_shadow_distance
                if line_index == 1
                else config.line_2_shadow_distance
            )
            shadow_angle = (
                config.line_1_shadow_angle
                if line_index == 1
                else config.line_2_shadow_angle
            )
            shadow_magnitude = float(shadow_distance) * 0.18
            shadow_radians = math.radians(float(shadow_angle))
            for material in _text_materials_for_id(materials, material_id):
                _apply_text_material_overrides(
                    material,
                    size=size,
                    color=color,
                    stroke_color="",
                    stroke_width=None,
                    line_max_width=0.86,
                    shadow_color=shadow_color,
                    shadow_alpha=shadow_alpha,
                    shadow_distance=shadow_distance,
                    shadow_angle=shadow_angle,
                    shadow_smoothing=shadow_smoothing,
                )
                material.update(
                    {
                        "alignment": 1,
                        "letter_spacing": float(config.letter_spacing) / 100.0,
                        "line_spacing": float(config.line_spacing) / 100.0,
                        "has_shadow": True,
                        "shadow_color": shadow_color,
                        "shadow_alpha": float(shadow_alpha),
                        "shadow_smoothing": float(shadow_smoothing),
                        "shadow_distance": float(shadow_distance),
                        "shadow_angle": float(shadow_angle),
                        "shadow_point": {
                            "x": shadow_magnitude * math.cos(shadow_radians),
                            "y": shadow_magnitude * math.sin(shadow_radians),
                        },
                    }
                )
                changed += 1
    if changed:
        log(f"已应用封面文字样式: font={font_json or 'default'}")
    return changed


def _load_text_style_preset(style_json_path: Path) -> dict[str, Any]:
    if not style_json_path.exists():
        raise FileNotFoundError(f"文本样式 JSON 不存在: {style_json_path}")
    with style_json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"文本样式 JSON 顶层必须是对象: {style_json_path}")
    if data.get("schema") != TEXT_STYLE_SCHEMA:
        raise RuntimeError(f"不支持的文本样式 JSON schema: {data.get('schema')!r}")
    if not isinstance(data.get("content"), dict):
        raise RuntimeError("文本样式 JSON 缺少 content 对象")
    if not isinstance(data.get("material_fields"), dict):
        raise RuntimeError("文本样式 JSON 缺少 material_fields 对象")
    if not isinstance(data.get("segment_fields"), dict):
        raise RuntimeError("文本样式 JSON 缺少 segment_fields 对象")
    log(f"已读取文本样式 JSON: {style_json_path}")
    return data


def _scaled_style_range(old_range: Any, source_len: int, target_len: int) -> list[int]:
    if not isinstance(old_range, list) or len(old_range) != 2:
        return [0, target_len]
    try:
        start = int(old_range[0])
        end = int(old_range[1])
    except (TypeError, ValueError):
        return [0, target_len]
    if source_len <= 0:
        return [0, target_len]
    new_start = round(start / source_len * target_len)
    new_end = round(end / source_len * target_len)
    new_start = max(0, min(target_len, new_start))
    new_end = max(new_start, min(target_len, new_end))
    return [new_start, new_end]


def _apply_text_style_preset_to_material(
    material: dict[str, Any],
    preset: dict[str, Any],
    new_text: str,
) -> None:
    current_content = _parse_text_material_content(material)
    target_text = new_text if new_text else str(current_content.get("text", ""))

    preset_content = deepcopy(preset["content"])
    source_text = str(preset_content.get("text", ""))
    source_len = len(source_text)
    target_len = len(target_text)

    styles = preset_content.get("styles")
    if not isinstance(styles, list) or not styles:
        styles = [{"range": [0, target_len]}]
        preset_content["styles"] = styles

    for style in styles:
        if isinstance(style, dict):
            style["range"] = _scaled_style_range(style.get("range"), source_len, target_len)

    preset_content["text"] = target_text
    material["content"] = json.dumps(preset_content, ensure_ascii=False)

    for key, value in preset["material_fields"].items():
        material[key] = deepcopy(value)


def _apply_text_style_preset_for_material_id(
    materials: dict[str, Any],
    material_id: str,
    preset: dict[str, Any],
    new_text: str,
) -> int:
    text_materials = _text_materials_for_id(materials, material_id)
    if not text_materials:
        raise RuntimeError(f"没有找到文本片段引用的文本素材: material_id={material_id!r}")
    for material in text_materials:
        _apply_text_style_preset_to_material(material, preset, new_text)
    return len(text_materials)


def _apply_segment_style_fields(segment: dict[str, Any], preset: dict[str, Any], apply_clip: bool) -> int:
    if not apply_clip:
        return 0
    changed = 0
    for key, value in preset["segment_fields"].items():
        old_value = segment.get(key)
        segment[key] = deepcopy(value)
        changed += int(old_value != segment[key])
    return changed


def _parse_hex_color(value: str) -> list[float]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6 or any(char not in "0123456789abcdefABCDEF" for char in text):
        raise ValueError(f"字幕颜色必须是 #RRGGBB 格式: {value!r}")
    return [int(text[index : index + 2], 16) / 255 for index in (0, 2, 4)]


def _apply_text_material_overrides(
    material: dict[str, Any],
    *,
    size: float | None,
    color: str,
    stroke_color: str,
    stroke_width: float | None,
    line_max_width: float | None,
    opacity: float | None = None,
    letter_spacing: float | None = None,
    shadow_color: str = "",
    shadow_alpha: float | None = None,
    shadow_distance: float | None = None,
    shadow_angle: float | None = None,
    shadow_smoothing: float | None = None,
) -> None:
    content = _parse_text_material_content(material)
    styles = content.get("styles")
    if not isinstance(styles, list) or not styles:
        styles = [{"range": [0, len(str(content.get("text", "")))]}]
        content["styles"] = styles

    rgb = _parse_hex_color(color) if color else None
    stroke_rgb = _parse_hex_color(stroke_color) if stroke_color else None
    shadow_rgb = _parse_hex_color(shadow_color) if shadow_color else None
    for style in styles:
        if not isinstance(style, dict):
            continue
        if size is not None:
            style["size"] = size
        if rgb is not None:
            fill = style.setdefault("fill", {})
            fill_content = fill.setdefault("content", {}) if isinstance(fill, dict) else {}
            solid = fill_content.setdefault("solid", {}) if isinstance(fill_content, dict) else {}
            if isinstance(solid, dict):
                solid["color"] = rgb
            style["useLetterColor"] = True
        if stroke_rgb is not None and stroke_width is not None and stroke_width > 0:
            style["strokes"] = [
                {
                    "content": {
                        "render_type": "solid",
                        "solid": {"color": stroke_rgb},
                    },
                    "width": float(stroke_width),
                }
            ]
        if shadow_rgb is not None and shadow_alpha is not None and shadow_alpha > 0:
            style["shadows"] = [
                {
                    "content": {
                        "render_type": "solid",
                        "solid": {"color": shadow_rgb},
                    },
                    "alpha": float(shadow_alpha),
                    "distance": float(shadow_distance if shadow_distance is not None else 5.0),
                    "angle": float(shadow_angle if shadow_angle is not None else -45.0),
                    "diffuse": 0.02500000037252903,
                }
            ]

    material["content"] = json.dumps(content, ensure_ascii=False)
    if opacity is not None:
        material["global_alpha"] = float(opacity)
    if line_max_width is not None:
        material["line_max_width"] = line_max_width
    if letter_spacing is not None:
        material["letter_spacing"] = float(letter_spacing)
    if shadow_rgb is not None and shadow_alpha is not None:
        enabled = shadow_alpha > 0
        material["has_shadow"] = enabled
        if enabled:
            material.update(
                {
                    "shadow_color": shadow_color.upper(),
                    "shadow_alpha": float(shadow_alpha),
                    "shadow_distance": float(shadow_distance if shadow_distance is not None else 5.0),
                    "shadow_angle": float(shadow_angle if shadow_angle is not None else -45.0),
                    "shadow_smoothing": float(shadow_smoothing if shadow_smoothing is not None else 0.45),
                }
            )


def apply_text_track_style(
    draft_dir: PathLike,
    *,
    track_index: int = 0,
    track_name: str = "",
    style_json_path: PathLike = "",
    size: float | None = None,
    color: str = "",
    stroke_color: str = "",
    stroke_width: float | None = None,
    transform_x: float | None = None,
    transform_y: float | None = None,
    line_max_width: float | None = None,
    font_id: str = "",
    font_path: str = "",
    font_title: str = "",
    shadow_color: str = "",
    shadow_alpha: float | None = None,
    shadow_distance: float | None = None,
    shadow_angle: float | None = None,
    shadow_smoothing: float | None = None,
) -> int:
    """Apply one preset and optional overrides to every segment on a text track."""

    draft_path = Path(draft_dir).resolve()
    data = load_plain_draft_json(draft_path)
    tracks = text_tracks(data)
    if track_name:
        matched = [item for item in tracks if str(item[1].get("name", "")) == track_name]
        if not matched:
            raise RuntimeError(f"没有找到指定名称的文本轨道: {track_name!r}")
        _raw_track_index, track = matched[0]
    else:
        if not 0 <= track_index < len(tracks):
            raise IndexError(f"文本轨道下标越界: {track_index}，可用范围 [0, {len(tracks)})")
        _raw_track_index, track = tracks[track_index]

    preset = _load_text_style_preset(Path(style_json_path).resolve()) if style_json_path else None
    materials = _materials_dict(data)
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("目标文本轨道 segments 不是列表")

    changed = 0
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        material_id = str(segment.get("material_id", ""))
        text_materials = _text_materials_for_id(materials, material_id)
        if not text_materials:
            continue

        for material in text_materials:
            current_text = str(_parse_text_material_content(material).get("text", ""))
            if preset is not None:
                _apply_text_style_preset_to_material(material, preset, current_text)
            _apply_text_material_overrides(
                material,
                size=size,
                color=color,
                stroke_color=stroke_color,
                stroke_width=stroke_width,
                line_max_width=line_max_width,
                shadow_color=shadow_color,
                shadow_alpha=shadow_alpha,
                shadow_distance=shadow_distance,
                shadow_angle=shadow_angle,
                shadow_smoothing=shadow_smoothing,
            )
            if font_id and font_path:
                _apply_font_to_text_material(
                    material,
                    {"id": font_id, "path": font_path},
                    font_title,
                )
            changed += 1

        if preset is not None:
            _apply_segment_style_fields(segment, preset, True)
        clip = segment.setdefault("clip", {})
        if isinstance(clip, dict):
            transform = clip.setdefault("transform", {})
            if isinstance(transform, dict):
                if transform_x is not None:
                    transform["x"] = transform_x
                if transform_y is not None:
                    transform["y"] = transform_y

    save_plain_draft_json(draft_path, data)
    log(
        "已给整条字幕轨道应用样式: "
        f"track_index={track_index}, track_name={track_name!r}, "
        f"segments={len(segments)}, style_json={style_json_path or 'default'}"
    )
    return changed


def _export_text_style_from_data(
    data: dict[str, Any],
    output_json_path: Path,
    *,
    track_index: int,
    segment_index: int,
    source_label: str,
) -> None:
    if output_json_path.exists():
        raise FileExistsError(f"文本样式 JSON 已存在，为避免覆盖已停止: {output_json_path}")

    ref = _find_text_segment_ref(data, track_index, segment_index)
    materials = _materials_dict(data)
    text_materials = _text_materials_for_id(materials, ref["material_id"])
    if not text_materials:
        raise RuntimeError(f"没有找到文本片段引用的文本素材: material_id={ref['material_id']!r}")

    material = text_materials[0]
    content = _parse_text_material_content(material)
    material_fields = {
        key: deepcopy(value)
        for key, value in material.items()
        if key in TEXT_MATERIAL_STYLE_KEYS
    }
    segment_fields = {
        key: deepcopy(value)
        for key, value in ref["segment"].items()
        if key in TEXT_SEGMENT_STYLE_KEYS
    }

    preset = {
        "schema": TEXT_STYLE_SCHEMA,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "label": source_label,
            "track_index": track_index,
            "segment_index": segment_index,
            "material_id": ref["material_id"],
        },
        "content": content,
        "material_fields": material_fields,
        "segment_fields": segment_fields,
    }

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    with output_json_path.open("w", encoding="utf-8") as f:
        json.dump(preset, f, ensure_ascii=False, indent=4)
    log(f"已导出文本样式 JSON: {output_json_path}")


def export_text_style_preset(
    draft_dir: PathLike,
    output_json_path: PathLike,
    *,
    track_index: int = 0,
    segment_index: int = 0,
    nested_draft_index: int | None = None,
    text_track_index: int = 0,
) -> None:
    """从草稿中的一个文本片段导出样式 JSON。

    nested_draft_index 为 None 时导出顶层文本；传入 0、1... 时导出嵌套草稿内部文本。
    """

    draft_path = Path(draft_dir).resolve()
    output_path = Path(output_json_path).resolve()
    data = load_plain_draft_json(draft_path)

    if nested_draft_index is None:
        _export_text_style_from_data(
            data,
            output_path,
            track_index=track_index,
            segment_index=segment_index,
            source_label=str(draft_path),
        )
        return

    refs = nested_draft_refs(data)
    if not refs:
        raise RuntimeError("当前草稿没有可解析的嵌套模板 materials.drafts[*].draft")
    if not 0 <= nested_draft_index < len(refs):
        raise IndexError(f"嵌套草稿下标越界: {nested_draft_index}，可用范围 [0, {len(refs)})")

    _raw_draft_index, _draft_material, nested = refs[nested_draft_index]
    _export_text_style_from_data(
        nested,
        output_path,
        track_index=text_track_index,
        segment_index=segment_index,
        source_label=f"{draft_path}::nested[{nested_draft_index}]",
    )


def _replace_text_style_preset_in_data(
    data: dict[str, Any],
    item: TextStylePresetReplacement,
) -> int:
    preset = _load_text_style_preset(Path(item.style_json_path).resolve())
    ref = _find_text_segment_ref(data, item.track_index, item.segment_index)
    materials = _materials_dict(data)
    changed = _apply_text_style_preset_for_material_id(materials, ref["material_id"], preset, item.text)
    changed += _apply_segment_style_fields(ref["segment"], preset, item.apply_clip)
    log(
        "已应用顶层文本样式: "
        f"text_track_index={item.track_index}, text_segment_index={item.segment_index}, "
        f"style_json={Path(item.style_json_path).resolve()}"
    )
    return changed


def _apply_text_style_preset_to_added_text(
    data: dict[str, Any],
    item: TextAddition,
    item_index: int,
) -> int:
    track_name = _text_addition_track_name(item, item_index)
    ref = _find_text_segment_ref_by_track_name(data, track_name)
    materials = _materials_dict(data)
    changed = 0
    if item.style_json_path:
        preset = _load_text_style_preset(Path(item.style_json_path).resolve())
        changed += _apply_text_style_preset_for_material_id(
            materials, ref["material_id"], preset, item.text
        )
        changed += _apply_segment_style_fields(ref["segment"], preset, item.apply_clip)

    text_materials = _text_materials_for_id(materials, ref["material_id"])
    for material in text_materials:
        _apply_text_material_overrides(
            material,
            size=item.size,
            color=item.color,
            stroke_color=item.stroke_color,
            stroke_width=item.stroke_width,
            line_max_width=item.line_max_width,
            opacity=item.opacity,
            letter_spacing=item.letter_spacing,
            shadow_color=item.shadow_color,
            shadow_alpha=item.shadow_alpha,
            shadow_distance=item.shadow_distance,
            shadow_angle=item.shadow_angle,
            shadow_smoothing=item.shadow_smoothing,
        )
        if item.font_id and item.font_path:
            _apply_font_to_text_material(
                material,
                {"id": item.font_id, "path": item.font_path},
                item.font_title,
            )
        changed += 1
    if item.style_json_path or text_materials:
        log(
            "已给新增文字应用固定样式: "
            f"track_name={track_name!r}, material_id={ref['material_id']!r}, "
            f"style_json={Path(item.style_json_path).resolve() if item.style_json_path else 'default'}"
        )
    return changed


def _segment_timerange_bounds(segment: dict[str, Any]) -> tuple[int, int] | None:
    target_timerange = segment.get("target_timerange")
    if not isinstance(target_timerange, dict):
        return None

    try:
        start = int(target_timerange.get("start", 0))
        duration = int(target_timerange.get("duration", 0))
    except (TypeError, ValueError):
        return None

    if duration <= 0:
        return None
    return start, start + duration


def _segment_sort_start(segment: Any) -> int:
    if not isinstance(segment, dict):
        return 0
    bounds = _segment_timerange_bounds(segment)
    return bounds[0] if bounds else 0


def _set_text_material_text(material: dict[str, Any], new_text: str) -> None:
    parsed = _parse_text_material_content(material)
    source_text = str(parsed.get("text", ""))
    source_len = len(source_text)
    target_len = len(new_text)

    styles = parsed.get("styles")
    if not isinstance(styles, list) or not styles:
        styles = [{"range": [0, target_len]}]
        parsed["styles"] = styles

    for style in styles:
        if isinstance(style, dict):
            style["range"] = _scaled_style_range(style.get("range"), source_len, target_len)

    parsed["text"] = new_text
    material["content"] = json.dumps(parsed, ensure_ascii=False)


def _sync_generated_subtitle_metadata(
    material: dict[str, Any],
    new_text: str,
    duration_us: int,
) -> None:
    """Detach a cloned auto-caption material from its source recognition text.

    Jianying auto-caption materials can store the rendered text in both
    ``content`` and ``base_content``.  They also carry recognition metadata.
    When a template caption is cloned, leaving those fields unchanged makes
    Jianying render the source template's first caption even though the text
    inspector shows the newly assigned ``content`` value.
    """

    base_source_len = 0
    base_content = material.get("base_content")
    if isinstance(base_content, str) and base_content:
        try:
            parsed = json.loads(base_content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            source_text = str(parsed.get("text", ""))
            source_len = len(source_text)
            base_source_len = source_len
            target_len = len(new_text)
            styles = parsed.get("styles")
            if not isinstance(styles, list) or not styles:
                styles = [{"range": [0, target_len]}]
                parsed["styles"] = styles
            for style in styles:
                if isinstance(style, dict):
                    style["range"] = _scaled_style_range(
                        style.get("range"), source_len, target_len
                    )
            parsed["text"] = new_text
            material["base_content"] = json.dumps(parsed, ensure_ascii=False)

    if "recognize_text" in material:
        material["recognize_text"] = new_text
    if "recognize_task_id" in material:
        material["recognize_task_id"] = ""
    if "current_words" in material:
        material["current_words"] = {}
    if "words" in material:
        duration_ms = max(1, (duration_us + 999) // 1000)
        material["words"] = {
            "start_time": [0],
            "end_time": [duration_ms],
            "text": [new_text],
        }

    keywords = material.get("subtitle_keywords")
    if isinstance(keywords, dict):
        ranges = keywords.get("range")
        if isinstance(ranges, list):
            target_len = len(new_text)
            scaled_ranges: list[dict[str, Any]] = []
            for item in ranges:
                if not isinstance(item, dict):
                    continue
                try:
                    old_start = int(item.get("location", 0))
                    old_length = int(item.get("length", base_source_len))
                except (TypeError, ValueError):
                    old_start = 0
                    old_length = base_source_len
                new_start, new_end = _scaled_style_range(
                    [old_start, old_start + old_length], base_source_len, target_len
                )
                scaled = deepcopy(item)
                if "location" in scaled or new_start:
                    scaled["location"] = new_start
                scaled["length"] = new_end - new_start
                scaled_ranges.append(scaled)
            keywords["range"] = scaled_ranges


def _text_materials_list(materials: dict[str, Any]) -> list[dict[str, Any]]:
    texts = materials.setdefault("texts", [])
    if not isinstance(texts, list):
        raise RuntimeError("materials.texts 不是列表，无法新增字幕素材")
    return texts


def _trim_text_track_range(
    track: dict[str, Any],
    start_us: int,
    end_us: int,
    *,
    text_material_ids: set[str] | None = None,
) -> int:
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("目标文本轨道 segments 不是列表，无法重建字幕")

    kept: list[Any] = []
    removed_or_trimmed = 0

    for segment in segments:
        if not isinstance(segment, dict):
            kept.append(segment)
            continue

        if (
            text_material_ids is not None
            and str(segment.get("material_id") or "") not in text_material_ids
        ):
            kept.append(segment)
            continue

        bounds = _segment_timerange_bounds(segment)
        if bounds is None:
            kept.append(segment)
            continue

        seg_start, seg_end = bounds
        if seg_end <= start_us or seg_start >= end_us:
            kept.append(segment)
            continue

        removed_or_trimmed += 1

        if seg_start < start_us:
            left = deepcopy(segment)
            left["target_timerange"] = {
                **deepcopy(segment.get("target_timerange", {})),
                "start": seg_start,
                "duration": start_us - seg_start,
            }
            kept.append(left)

        if seg_end > end_us:
            right = deepcopy(segment)
            right["id"] = new_json_id()
            right["target_timerange"] = {
                **deepcopy(segment.get("target_timerange", {})),
                "start": end_us,
                "duration": seg_end - end_us,
            }
            kept.append(right)

    track["segments"] = kept
    return removed_or_trimmed


def _make_subtitle_material(
    base_material: dict[str, Any],
    new_material_id: str,
    text: str,
    duration_us: int,
    preset: dict[str, Any] | None,
) -> dict[str, Any]:
    material = deepcopy(base_material)
    material["id"] = new_material_id
    if "material_id" in material:
        material["material_id"] = new_material_id

    if preset is not None:
        _apply_text_style_preset_to_material(material, preset, text)
    else:
        _set_text_material_text(material, text)
    _sync_generated_subtitle_metadata(material, text, duration_us)
    return material


def _make_subtitle_segment(
    base_segment: dict[str, Any],
    new_material_id: str,
    start_us: int,
    duration_us: int,
    preset: dict[str, Any] | None,
    apply_clip: bool,
) -> dict[str, Any]:
    segment = deepcopy(base_segment)
    segment["id"] = new_json_id()
    segment["material_id"] = new_material_id
    segment["target_timerange"] = {
        "start": start_us,
        "duration": duration_us,
    }

    if preset is not None:
        _apply_segment_style_fields(segment, preset, apply_clip)

    return segment


def _replace_subtitle_range_in_data(data: dict[str, Any], item: SubtitleRangeReplacement) -> int:
    if item.start_us < 0:
        raise ValueError(f"字幕替换范围 start_us 不能小于 0: {item.start_us}")
    if item.end_us <= item.start_us:
        raise ValueError(
            f"字幕替换范围 end_us 必须大于 start_us: start_us={item.start_us}, end_us={item.end_us}"
        )

    ref = _find_text_segment_ref(data, item.track_index, item.base_segment_index)
    track = ref["track"]
    base_segment = deepcopy(ref["segment"])

    materials = _materials_dict(data)
    base_materials = _text_materials_for_id(materials, ref["material_id"])
    if not base_materials:
        raise RuntimeError(f"没有找到可复制的字幕素材: material_id={ref['material_id']!r}")
    base_material = deepcopy(base_materials[0])

    preset = None
    style_path_label = ""
    if item.style_json_path:
        style_path = Path(item.style_json_path).resolve()
        preset = _load_text_style_preset(style_path)
        style_path_label = str(style_path)

    text_material_ids = {
        str(material.get("id") or "")
        for collection in ("texts", "text_templates")
        for material in (
            materials.get(collection, [])
            if isinstance(materials.get(collection), list)
            else []
        )
        if isinstance(material, dict) and material.get("id")
    }
    removed_or_trimmed = _trim_text_track_range(
        track,
        item.start_us,
        item.end_us,
        text_material_ids=text_material_ids,
    )
    texts = _text_materials_list(materials)
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError("目标文本轨道 segments 不是列表，无法新增字幕")

    for line_index, line in enumerate(item.subtitles):
        if line.start_us < 0:
            raise ValueError(f"字幕行 start_us 不能小于 0: line_index={line_index}, start_us={line.start_us}")
        if line.duration_us <= 0:
            raise ValueError(
                f"字幕行 duration_us 必须大于 0: line_index={line_index}, duration_us={line.duration_us}"
            )

        absolute_start = item.start_us + line.start_us
        absolute_end = absolute_start + line.duration_us
        if absolute_start < item.start_us or absolute_end > item.end_us:
            log(
                "新增字幕超出替换范围: "
                f"line_index={line_index}, subtitle_range=[{absolute_start}, {absolute_end}), "
                f"replace_range=[{item.start_us}, {item.end_us})",
                "WARN",
            )

        new_material_id = new_json_id()
        material = _make_subtitle_material(
            base_material,
            new_material_id,
            line.text,
            line.duration_us,
            preset,
        )
        segment = _make_subtitle_segment(
            base_segment,
            new_material_id,
            absolute_start,
            line.duration_us,
            preset,
            item.apply_clip,
        )
        render_index = segment.get("render_index")
        if render_index in (None, ""):
            render_index = max_segment_render_index(data, 15000) + 1
        segment["render_index"] = int(render_index)
        if "track_render_index" in segment:
            segment["track_render_index"] = int(render_index)

        texts.append(material)
        segments.append(segment)

    segments.sort(key=_segment_sort_start)
    log(
        "已重建字幕时间段: "
        f"text_track_index={item.track_index}, base_segment_index={item.base_segment_index}, "
        f"range=[{item.start_us}, {item.end_us}), removed_or_trimmed={removed_or_trimmed}, "
        f"added={len(item.subtitles)}, style_json={style_path_label!r}"
    )
    return removed_or_trimmed + len(item.subtitles)


def _replace_nested_text_font_in_data(
    draft: Any,
    data: dict[str, Any],
    item: NestedTextFontReplacement,
) -> int:
    refs = nested_draft_refs(data)
    if not refs:
        raise RuntimeError("当前草稿没有可解析的嵌套模板 materials.drafts[*].draft")
    if not 0 <= item.nested_draft_index < len(refs):
        raise IndexError(f"嵌套草稿下标越界: {item.nested_draft_index}，可用范围 [0, {len(refs)})")

    raw_draft_index, _draft_material, nested = refs[item.nested_draft_index]
    font_json = _resolve_font_json(draft, item.font_name, item.font_id, item.font_path)
    material_id = _find_text_segment_material_id(nested, item.text_track_index, item.segment_index)
    materials = _materials_dict(nested)
    changed = _replace_text_font_for_material_id(materials, material_id, font_json, item.font_title)
    log(
        "已替换嵌套文本字体: "
        f"nested_draft_index={item.nested_draft_index}, raw_draft_index={raw_draft_index}, "
        f"text_track_index={item.text_track_index}, text_segment_index={item.segment_index}, "
        f"font={font_json}"
    )
    return changed


def _replace_nested_text_style_preset_in_data(
    data: dict[str, Any],
    item: NestedTextStylePresetReplacement,
) -> int:
    refs = nested_draft_refs(data)
    if not refs:
        raise RuntimeError("当前草稿没有可解析的嵌套模板 materials.drafts[*].draft")
    if not 0 <= item.nested_draft_index < len(refs):
        raise IndexError(f"嵌套草稿下标越界: {item.nested_draft_index}，可用范围 [0, {len(refs)})")

    raw_draft_index, _draft_material, nested = refs[item.nested_draft_index]
    preset = _load_text_style_preset(Path(item.style_json_path).resolve())
    ref = _find_text_segment_ref(nested, item.text_track_index, item.segment_index)
    materials = _materials_dict(nested)
    changed = _apply_text_style_preset_for_material_id(materials, ref["material_id"], preset, item.text)
    changed += _apply_segment_style_fields(ref["segment"], preset, item.apply_clip)
    log(
        "已应用嵌套文本样式: "
        f"nested_draft_index={item.nested_draft_index}, raw_draft_index={raw_draft_index}, "
        f"text_track_index={item.text_track_index}, text_segment_index={item.segment_index}, "
        f"style_json={Path(item.style_json_path).resolve()}"
    )
    return changed


def _apply_json_changes(draft: Any, data: dict[str, Any], job: ContentReplaceJob) -> int:
    changed = 0

    if job.original_video_volume is not None:
        changed += _apply_original_video_volume(data, job.original_video_volume)

    if job.visual_variant is not None:
        changed += apply_visual_variant_to_data(
            data,
            job.visual_variant,
            warning=lambda message: log(f"画面变化提示: {message}"),
        )

    for item in job.nested_video_replacements:
        changed += int(
            replace_nested_video_segment_by_index(
                draft,
                data,
                _namespace(
                    replace_nested_video_segment_path=str(item.media_path),
                    target_nested_draft_index=item.nested_draft_index,
                    target_nested_video_track_index=item.video_track_index,
                    target_nested_video_segment_index=item.segment_index,
                    nested_video_source_start_us=item.source_start_us,
                    nested_video_source_duration_us=item.source_duration_us,
                    nested_video_target_start_us=item.target_start_us,
                    nested_video_target_duration_us=item.target_duration_us,
                ),
            )
        )

    for item in job.text_font_replacements:
        changed += _replace_text_font_in_data(draft, data, item)

    if job.cover is not None:
        changed += _replace_cover_text_fonts_in_data(draft, data, job.cover)

    for item in job.text_style_preset_replacements:
        changed += _replace_text_style_preset_in_data(data, item)

    for item_index, item in enumerate(job.text_additions):
        changed += _apply_text_style_preset_to_added_text(data, item, item_index)
        if item.text_effect_json_path:
            changed += apply_text_effect_to_track(
                data,
                item.text_effect_json_path,
                _text_addition_track_name(item, item_index),
            )

    for item_index, item in enumerate(job.text_template_additions):
        changed += add_text_template_to_data(
            data,
            item.template_json_path,
            item.texts,
            start_us=item.start_us,
            duration_us=item.duration_us,
            track_name=item.track_name or f"程序复合文字模板_{item_index}",
        )

    for item in job.subtitle_range_replacements:
        changed += _replace_subtitle_range_in_data(data, item)

    for item in job.nested_text_font_replacements:
        changed += _replace_nested_text_font_in_data(draft, data, item)

    for item in job.nested_text_style_preset_replacements:
        changed += _replace_nested_text_style_preset_in_data(data, item)

    for item in job.effect_additions:
        effect_json_data = load_effect_json(Path(item.effect_json_path).resolve())
        add_effect_json_to_video(
            data,
            effect_json_data,
            item.target_video_track_index,
            item.target_video_segment_index,
            item.start_us,
            item.duration_us,
        )
        changed += 1

    for item in job.sticker_additions:
        try:
            changed += add_fullscreen_sticker_to_data(
                data,
                item.sticker_json_path,
                start_us=item.start_us,
                duration_us=item.duration_us,
                corner=item.corner,
                visible_ratio=item.visible_ratio,
                scale=item.scale,
                rotation=item.rotation,
                opacity=item.opacity,
                track_name=item.track_name,
                transform_x=item.transform_x,
                transform_y=item.transform_y,
                inside_canvas=item.inside_canvas,
                render_below_text=item.render_below_text,
            )
        except Exception as exc:
            if not item.optional:
                raise
            log(f"可选前景贴图已跳过，不影响基础成片: {exc}")

    visual_additions = sorted(
        [*job.image_additions, *job.video_overlay_additions],
        key=lambda item: item.layer_order,
    )
    for item in visual_additions:
        try:
            if isinstance(item, ImageAddition):
                changed += add_image_overlay_to_data(
                    draft,
                    data,
                    item.image_path,
                    start_us=item.start_us,
                    duration_us=item.duration_us,
                    corner=item.corner,
                    scale=item.scale,
                    rotation=item.rotation,
                    opacity=item.opacity,
                    track_name=item.track_name,
                    render_below_text=item.render_below_text,
                    transform_x=item.transform_x,
                    transform_y=item.transform_y,
                )
            else:
                changed += add_video_overlay_to_data(
                    draft,
                    data,
                    item.video_path,
                    start_us=item.start_us,
                    duration_us=item.duration_us,
                    source_start_us=item.source_start_us,
                    source_duration_us=item.source_duration_us,
                    loop_to_target=item.loop_to_target,
                    mute=item.mute,
                    fit=item.fit,
                    corner=item.corner,
                    scale=item.scale,
                    opacity=item.opacity,
                    track_name=item.track_name,
                    render_below_text=item.render_below_text,
                )
        except Exception as exc:
            if not item.optional:
                raise
            log(f"可选语义视觉已跳过，不影响基础成片: {exc}")

    if job.cover is not None:
        changed += apply_cover_timeline_offset(data, job.cover)

    if job.timeline_duration_us:
        changed += _fit_timeline_duration(
            data,
            job.timeline_duration_us,
            template_duration_us=job.template_timeline_duration_us,
            protected_text_track_indexes={
                item.track_index for item in job.subtitle_range_replacements
            },
        )

    if changed:
        log(f"JSON 级共执行 {changed} 项修改")
    return changed


def _fit_timeline_duration(
    data: dict[str, Any],
    target_duration_us: int,
    *,
    template_duration_us: int = 0,
    protected_text_track_indexes: set[int] | None = None,
) -> int:
    if target_duration_us <= 0:
        raise ValueError("时间线时长必须大于 0")
    current_duration_us = max(0, int(data.get("duration", 0) or 0))
    original_duration_us = max(0, int(template_duration_us or current_duration_us))
    protected = protected_text_track_indexes or set()
    changed = int(current_duration_us != target_duration_us)
    text_track_index = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type") or "")
        protect_extension = track_type == "text" and text_track_index in protected
        if track_type == "text":
            text_track_index += 1
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        retained: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict):
                retained.append(segment)
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, dict):
                retained.append(segment)
                continue
            start_us = max(0, int(timerange.get("start", 0) or 0))
            duration_us = max(0, int(timerange.get("duration", 0) or 0))
            end_us = start_us + duration_us
            if start_us >= target_duration_us:
                changed += 1
                continue
            next_duration = duration_us
            reaches_original_end = (
                original_duration_us > 0
                and abs(end_us - original_duration_us) <= 33_334
            )
            if end_us > target_duration_us:
                next_duration = target_duration_us - start_us
            elif (
                reaches_original_end
                and duration_us > TIMELINE_TAIL_FOLLOW_MIN_DURATION_US
                and not protect_extension
            ):
                next_duration = target_duration_us - start_us
            if next_duration != duration_us:
                timerange["duration"] = max(1, next_duration)
                changed += 1
            retained.append(segment)
        if len(retained) != len(segments):
            track["segments"] = retained
    data["duration"] = target_duration_us
    if changed:
        log(
            "已按当前视频调整模板时间线: "
            f"original={original_duration_us}, target={target_duration_us}, changes={changed}"
        )
    return changed


def _apply_original_video_volume(data: dict[str, Any], multiplier: float) -> int:
    if not 0.0 <= multiplier <= 2.0:
        raise ValueError("Original video volume must be between 0 and 2")
    changed = 0
    tracks = data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        segments = track.get("segments", [])
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict):
                continue
            try:
                current = float(segment.get("volume", 1.0) or 0.0)
            except (TypeError, ValueError):
                current = 1.0
            updated = max(0.0, min(2.0, current * multiplier))
            if segment.get("volume") != updated:
                segment["volume"] = updated
                changed += 1
    if changed:
        log(f"已调整原视频声音: multiplier={multiplier:.2f}, segments={changed}")
    return changed


def _remove_replaced_materials(
    data: dict[str, Any],
    *,
    remove_audio: bool = False,
    remove_effects: bool = False,
) -> int:
    changed = 0
    tracks = data.get("tracks", [])
    if isinstance(tracks, list):
        removed_types = set()
        if remove_audio:
            removed_types.add("audio")
        if remove_effects:
            removed_types.add("effect")
        if removed_types:
            retained = [
                track
                for track in tracks
                if not isinstance(track, dict) or str(track.get("type", "")) not in removed_types
            ]
            changed += len(tracks) - len(retained)
            data["tracks"] = retained

    materials = data.get("materials", {})
    if isinstance(materials, dict):
        collections: list[str] = []
        if remove_audio:
            collections.extend(["audios", "audio_effects"])
        if remove_effects:
            collections.extend(["video_effects", "effects"])
        for collection in collections:
            values = materials.get(collection)
            if isinstance(values, list) and values:
                changed += len(values)
                materials[collection] = []
    return changed


def _video_material_ids_before_replacement(
    data: dict[str, Any], job: ContentReplaceJob
) -> set[str]:
    """Return only the top-level video materials targeted by this job.

    pyJianYingDraft adds the replacement material but intentionally leaves the
    old material object in ``materials.videos``.  Jianying then shows that
    unreferenced object as "Media Not Found" when the account template did not
    transfer its disposable placeholder clip.
    """

    targets = {
        (int(item.track_index), int(item.segment_index))
        for item in job.video_segment_replacements
    }
    if not targets:
        return set()
    result: set[str] = set()
    typed_track_index = 0
    for track in data.get("tracks", []):
        if not isinstance(track, dict) or str(track.get("type") or "") != "video":
            continue
        segments = track.get("segments", [])
        if isinstance(segments, list):
            for segment_index, segment in enumerate(segments):
                if (
                    (typed_track_index, segment_index) not in targets
                    or not isinstance(segment, dict)
                ):
                    continue
                material_id = str(segment.get("material_id") or "").strip()
                if material_id:
                    result.add(material_id)
        typed_track_index += 1
    return result


def _remove_unreferenced_replaced_video_materials(
    data: dict[str, Any], candidate_ids: set[str]
) -> int:
    """Remove replaced video records only after their last timeline reference is gone."""

    if not candidate_ids:
        return 0
    referenced_ids: set[str] = set()
    for track in data.get("tracks", []):
        if not isinstance(track, dict):
            continue
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        referenced_ids.update(
            str(segment.get("material_id") or "").strip()
            for segment in segments
            if isinstance(segment, dict) and segment.get("material_id")
        )
    removable_ids = candidate_ids - referenced_ids
    materials = data.get("materials")
    videos = materials.get("videos") if isinstance(materials, dict) else None
    if not removable_ids or not isinstance(videos, list):
        return 0
    retained = [
        material
        for material in videos
        if not isinstance(material, dict)
        or str(material.get("id") or material.get("material_id") or "").strip()
        not in removable_ids
    ]
    removed = len(videos) - len(retained)
    if removed:
        materials["videos"] = retained
    return removed


def run_content_replace_job(job: ContentReplaceJob) -> ContentReplaceResult:
    draft = import_pyjianyingdraft()

    template_dir = Path(job.template_draft_dir).resolve()
    output_root = Path(job.output_root).resolve()
    if not template_dir.exists():
        raise FileNotFoundError(f"模板草稿目录不存在: {template_dir}")
    if not template_dir.is_dir():
        raise NotADirectoryError(f"模板草稿路径不是目录: {template_dir}")

    log(f"模板草稿目录: {template_dir}")
    log(f"输出父目录: {output_root}")

    original_data = load_plain_draft_json(template_dir)
    summarize_draft_json(original_data, "原模板")
    if job.dump_effects:
        log_effect_details(original_data, "原模板")
    if job.dump_nested_drafts:
        log_nested_draft_details(original_data, "原模板")
    validate_template_with_pyjyd(draft, template_dir)

    output_name = job.output_name.strip()
    if not output_name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"{template_dir.name}_program_{stamp}"
    log(f"输出草稿名称: {output_name}")

    output_dir = copy_template_draft(template_dir, output_root, output_name)
    copied_data = load_plain_draft_json(output_dir)
    compatibility = normalize_draft_for_legacy_editor(copied_data)
    if compatibility.changed:
        save_plain_draft_json(output_dir, copied_data)
        copied_data = load_plain_draft_json(output_dir)
        source_versions = ", ".join(compatibility.source_platform_versions) or "unknown"
        log(
            "已将输出副本转换为剪映低版本兼容草稿: "
            f"source_platform={source_versions}, "
            f"target_platform={compatibility.target_app_version}, "
            f"contexts={compatibility.changed_contexts}, fields={compatibility.changed_fields}"
        )
    else:
        source_versions = ", ".join(compatibility.source_platform_versions) or "unknown"
        log(
            "输出副本无需降版本转换: "
            f"source_platform={source_versions}, target_platform={compatibility.target_app_version}"
        )
    replaced_video_material_ids = _video_material_ids_before_replacement(
        copied_data, job
    )
    cleanup_changes = _remove_replaced_materials(
        copied_data,
        remove_audio=job.remove_existing_audio,
        remove_effects=job.remove_existing_effects,
    )
    if cleanup_changes:
        save_plain_draft_json(output_dir, copied_data)
        copied_data = load_plain_draft_json(output_dir)
        log(f"输出副本已清除 {cleanup_changes} 项待替换旧素材")
    copied_summary = summarize_draft_json(copied_data, "输出副本-修改前")
    if job.dump_effects:
        log_effect_details(copied_data, "输出副本-修改前")
    if job.dump_nested_drafts:
        log_nested_draft_details(copied_data, "输出副本-修改前")

    prepared_cover = (
        prepare_cover_assets(copied_data, job.cover, output_dir)
        if job.cover is not None
        else None
    )
    script = load_output_script(draft, output_root, output_name)
    top_level_changes = _apply_top_level_changes(
        draft,
        script,
        job,
        copied_summary,
        prepared_cover,
    )
    script.save()
    log(f"pyJianYingDraft 保存成功: {output_dir / 'draft_content.json'}")

    saved_data = load_plain_draft_json(output_dir)
    orphan_video_changes = _remove_unreferenced_replaced_video_materials(
        saved_data, replaced_video_material_ids
    )
    if orphan_video_changes:
        log(
            f"输出副本已清除 {orphan_video_changes} 个已替换且无时间线引用的旧视频素材"
        )
    cover_path_changes = (
        rebase_cover_material_paths(saved_data, output_dir)
        if prepared_cover is not None
        else 0
    )
    json_changes = (
        cleanup_changes
        + orphan_video_changes
        + cover_path_changes
        + _apply_json_changes(draft, saved_data, job)
    )
    final_compatibility = normalize_draft_for_legacy_editor(saved_data)
    if final_compatibility.changed:
        log(
            "保存后再次修正草稿低版本兼容字段: "
            f"contexts={final_compatibility.changed_contexts}, fields={final_compatibility.changed_fields}"
        )
    if json_changes > cleanup_changes or final_compatibility.changed:
        save_plain_draft_json(output_dir, saved_data)
        saved_data = load_plain_draft_json(output_dir)

    summarize_draft_json(saved_data, "输出副本-保存后")
    if job.dump_effects or job.effect_additions:
        log_effect_details(saved_data, "输出副本-保存后")
    if job.dump_nested_drafts or job.nested_video_replacements:
        log_nested_draft_details(saved_data, "输出副本-保存后")

    load_output_script(draft, output_root, output_name)
    if _has_any_change(job):
        log("程序化替换流程完成，保存后草稿可被 pyJianYingDraft 再次加载")
    else:
        log("程序化读取/复制/保存流程完成，保存后草稿可被 pyJianYingDraft 再次加载")
    log(f"新草稿完整目录: {output_dir}")

    return ContentReplaceResult(
        output_dir=output_dir,
        output_name=output_name,
        top_level_changes=top_level_changes,
        json_changes=json_changes,
    )
