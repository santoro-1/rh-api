from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time
from typing import Any, Mapping

from .content_replace import (
    AudioAddition,
    AudioSegmentReplacement,
    ContentReplaceJob,
    EffectAddition,
    ImageAddition,
    NamedAudioReplacement,
    NestedTextStylePresetReplacement,
    NestedVideoReplacement,
    StickerAddition,
    SubtitleLine,
    SubtitleRangeReplacement,
    TextAddition,
    TextFontReplacement,
    TextReplacement,
    TextStylePresetReplacement,
    TextTemplateAddition,
    VideoSegmentReplacement,
    VideoOverlayAddition,
    run_content_replace_job,
)
from .cli import load_plain_draft_json
from .draft_crypto import prepare_plain_draft_dir
from .draft_factory import (
    VideoSequenceItem,
    create_plain_draft_from_video,
    create_plain_draft_from_videos,
)
from .subtitles import (
    add_captions_to_draft,
    build_caption_cues,
    caption_cues_from_payload,
    parse_srt_cues,
    validate_caption_cues,
)
from .template_library import TemplateLibrary
from .visual_variant import VisualVariant
from .cover_apply import CoverConfig


TEXT_END_ROUNDING_TOLERANCE_US = 33_334
DRAFT_DISCOVERY_ATTEMPTS = 5
DRAFT_DISCOVERY_RETRY_DELAY_SECONDS = 2


RENDER_JOB_SCHEMA = "jyd.render_job.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RenderJobResult:
    source_kind: str
    source_draft_dir: Path | None
    working_template_dir: Path
    output_draft_dir: Path
    output_draft_name: str
    output_mp4: Path | None
    exported: bool
    top_level_changes: int
    json_changes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_draft_dir": str(self.source_draft_dir) if self.source_draft_dir else "",
            "working_template_dir": str(self.working_template_dir),
            "output_draft_dir": str(self.output_draft_dir),
            "output_draft_name": self.output_draft_name,
            "output_mp4": str(self.output_mp4) if self.output_mp4 else "",
            "exported": self.exported,
            "top_level_changes": self.top_level_changes,
            "json_changes": self.json_changes,
        }


def load_render_job_config(job_path: str | Path) -> dict[str, Any]:
    path = _positive_path(job_path, "任务 JSON")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"任务 JSON 顶层必须是对象: {path}")
    return data


def run_render_job_file(job_path: str | Path) -> RenderJobResult:
    return run_render_job(load_render_job_config(job_path))


def run_render_job(data: Mapping[str, Any]) -> RenderJobResult:
    config = dict(data)
    source = _dict_value(config.get("source"))
    output = _dict_value(config.get("output"))

    source_kind = str(
        _value(source, "type", "kind", default=_value(config, "source_kind", default="auto"))
    ).replace("_", "-").lower()
    if source_kind == "auto":
        source_kind = "template" if _template_dir_from_config(config, source) else "video"

    if source_kind == "existing-draft":
        return _export_existing_draft(config, source, output)

    if source_kind == "video":
        source_draft_dir, template_dir, output_name_source = _prepare_video_source(config, source)
        default_output_root = PROJECT_ROOT / "_local_loop_test"
    elif source_kind == "video-sequence":
        source_draft_dir, template_dir, output_name_source = _prepare_video_sequence_source(config, source)
        default_output_root = PROJECT_ROOT / "_local_loop_test"
    elif source_kind == "template":
        source_draft_dir, template_dir, output_name_source = _prepare_template_source(config, source)
        default_output_root = source_draft_dir.parent
    else:
        raise RuntimeError(f"不支持的 source.type: {source_kind!r}")

    output_root = Path(
        _value(output, "draft_root", "output_root", default=_value(config, "draft_root", "output_root", default=default_output_root))
    ).expanduser().resolve()
    output_name = str(_value(output, "draft_name", "output_name", default=_value(config, "draft_name", "output_name", default=""))).strip()
    if not output_name:
        from datetime import datetime

        output_name = f"{_safe_name(output_name_source)}_render_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    video_segment_replacements = _build_video_replacements(config)
    nested_video_replacements = _build_nested_video_replacements(config)
    source_data = load_plain_draft_json(template_dir)
    source_timeline_duration_us = _draft_duration_us(source_data)
    requested_timeline_duration_us = int(
        _value(config, "timeline_duration_us", default=0) or 0
    )
    effective_timeline_duration_us = (
        requested_timeline_duration_us or source_timeline_duration_us
    )
    text_replacements, text_additions, text_style_replacements, nested_text_style_replacements = _build_text_replacements(
        config,
        timeline_duration_us=effective_timeline_duration_us,
    )
    text_style_replacements.extend(
        _build_existing_text_style_replacements(config, source_data)
    )
    text_font_replacements = _build_existing_text_font_replacements(config, source_data)
    text_template_additions = _build_text_template_additions(
        config,
        timeline_duration_us=effective_timeline_duration_us,
    )
    named_audio_replacements, audio_segment_replacements, audio_additions = _build_audio_replacements(
        config,
        timeline_duration_us=effective_timeline_duration_us,
    )
    effect_additions = _build_effect_additions(
        config,
        timeline_duration_us=effective_timeline_duration_us,
    )
    sticker_additions = _build_sticker_additions(config)
    image_additions = _build_visual_overlay_additions(config)
    for fixed_overlay in _build_fixed_overlay_additions(
        config,
        timeline_duration_us=source_timeline_duration_us,
    ):
        if isinstance(fixed_overlay, StickerAddition):
            sticker_additions.append(fixed_overlay)
        else:
            image_additions.append(fixed_overlay)
    video_overlay_additions = _build_visual_video_additions(config)
    visual_variant = _build_visual_variant(config)
    cover = _build_cover(config, source_data)
    subtitle_range_replacements = _build_subtitle_range_replacements(config)

    content_job = ContentReplaceJob(
        template_draft_dir=template_dir,
        output_root=output_root,
        output_name=output_name,
        dump_effects=_as_bool(_value(config, "dump_effects", default=False)),
        dump_nested_drafts=_as_bool(_value(config, "dump_nested_drafts", default=False)),
        video_segment_replacements=video_segment_replacements,
        nested_video_replacements=nested_video_replacements,
        text_replacements=text_replacements,
        text_additions=text_additions,
        text_template_additions=text_template_additions,
        text_font_replacements=text_font_replacements,
        text_style_preset_replacements=text_style_replacements,
        subtitle_range_replacements=subtitle_range_replacements,
        nested_text_style_preset_replacements=nested_text_style_replacements,
        named_audio_replacements=named_audio_replacements,
        audio_segment_replacements=audio_segment_replacements,
        audio_additions=audio_additions,
        effect_additions=effect_additions,
        sticker_additions=sticker_additions,
        image_additions=image_additions,
        video_overlay_additions=video_overlay_additions,
        visual_variant=visual_variant,
        cover=cover,
        original_video_volume=_optional_volume(config),
        remove_existing_audio=_as_bool(_value(config, "remove_existing_audio", default=False)),
        remove_existing_effects=_as_bool(_value(config, "remove_existing_effects", default=False)),
        timeline_duration_us=requested_timeline_duration_us,
        template_timeline_duration_us=source_timeline_duration_us,
    )

    replace_result = run_content_replace_job(content_job)
    caption_changes = _apply_captions_to_output(
        config,
        replace_result.output_dir,
        timeline_offset_us=cover.duration_us if cover is not None else 0,
    )

    export_config = _dict_value(config.get("export"))
    skip_export = _as_bool(
        _value(
            output,
            "skip_export",
            default=_value(config, "skip_export", default=_value(export_config, "skip_export", default=False)),
        )
    )
    output_mp4_text = _value(
        output,
        "mp4_path",
        "output_mp4",
        "output_path",
        default=_value(config, "output_mp4", "output_path", default=""),
    )
    output_mp4 = Path(output_mp4_text).expanduser().resolve() if output_mp4_text else None
    exported = False
    if not skip_export:
        if output_mp4 is None:
            raise RuntimeError("导出 MP4 时必须提供 output.mp4_path 或 output_mp4")
        _export_mp4(
            replace_result.output_name,
            output_mp4,
            resolution=str(_value(export_config, "resolution", default=_value(output, "resolution", default=""))),
            framerate=str(_value(export_config, "framerate", default=_value(output, "framerate", default=""))),
            timeout=float(_value(export_config, "timeout", "export_timeout", default=_value(output, "timeout", default=1200))),
        )
        exported = True

    return RenderJobResult(
        source_kind=source_kind,
        source_draft_dir=source_draft_dir,
        working_template_dir=template_dir,
        output_draft_dir=replace_result.output_dir,
        output_draft_name=replace_result.output_name,
        output_mp4=output_mp4,
        exported=exported,
        top_level_changes=replace_result.top_level_changes,
        json_changes=replace_result.json_changes + caption_changes,
    )


def _export_existing_draft(
    config: Mapping[str, Any],
    source: Mapping[str, Any],
    output: Mapping[str, Any],
) -> RenderJobResult:
    """Export a frozen draft, rebuilding once only after discovery is exhausted."""

    draft_dir = _positive_path(
        _value(source, "draft_dir", "template_dir", default=""),
        "已生成剪映草稿",
    )
    if not draft_dir.is_dir() or not (draft_dir / "draft_content.json").is_file():
        raise RuntimeError(f"已生成剪映草稿结构不完整: {draft_dir}")
    draft_name = str(
        _value(source, "draft_name", default=draft_dir.name)
    ).strip()
    if not draft_name:
        raise RuntimeError("已生成剪映草稿缺少名称")
    output_mp4_text = _value(
        output,
        "mp4_path",
        "output_mp4",
        "output_path",
        default=_value(config, "output_mp4", "output_path", default=""),
    )
    if not output_mp4_text:
        raise RuntimeError("导出 MP4 时必须提供 output.mp4_path 或 output_mp4")
    output_mp4 = Path(output_mp4_text).expanduser().resolve()
    export_config = _dict_value(config.get("export"))
    resolution = str(
        _value(
            export_config,
            "resolution",
            default=_value(output, "resolution", default=""),
        )
    )
    framerate = str(
        _value(
            export_config,
            "framerate",
            default=_value(output, "framerate", default=""),
        )
    )
    timeout = float(
        _value(
            export_config,
            "timeout",
            "export_timeout",
            default=_value(output, "timeout", default=1200),
        )
    )
    export_draft_dir = draft_dir
    export_draft_name = draft_name
    recovery_result: RenderJobResult | None = None
    try:
        _export_mp4(
            export_draft_name,
            output_mp4,
            resolution=resolution,
            framerate=framerate,
            timeout=timeout,
        )
    except Exception as exc:
        recovery = _dict_value(source.get("recovery"))
        rebuild_job = _dict_value(recovery.get("rebuild_job"))
        if not _is_draft_not_found(exc) or not rebuild_job:
            raise
        rebuild_output = _dict_value(rebuild_job.get("output"))
        if not _as_bool(_value(rebuild_output, "skip_export", default=False)):
            raise RuntimeError("草稿发现恢复任务必须设置 output.skip_export=true") from exc
        print(
            f"[export] 连续 {DRAFT_DISCOVERY_ATTEMPTS} 次未在剪映首页识别草稿 "
            f"draft={draft_name}，保留原草稿并创建一次恢复草稿",
            flush=True,
        )
        recovery_result = run_render_job(rebuild_job)
        export_draft_dir = recovery_result.output_draft_dir.resolve()
        export_draft_name = recovery_result.output_draft_name.strip()
        if (
            not export_draft_name
            or not export_draft_dir.is_dir()
            or not (export_draft_dir / "draft_content.json").is_file()
        ):
            raise RuntimeError("草稿发现恢复任务没有生成完整的新草稿") from exc
        if export_draft_dir == draft_dir or export_draft_name == draft_name:
            raise RuntimeError("草稿发现恢复任务必须使用新草稿名称，不能覆盖冻结草稿") from exc
        print(
            f"[export] 恢复草稿已创建 old={draft_name} new={export_draft_name}，"
            f"开始第二轮 {DRAFT_DISCOVERY_ATTEMPTS} 次识别",
            flush=True,
        )
        _export_mp4(
            export_draft_name,
            output_mp4,
            resolution=resolution,
            framerate=framerate,
            timeout=timeout,
        )
    return RenderJobResult(
        source_kind="existing-draft",
        source_draft_dir=draft_dir,
        working_template_dir=export_draft_dir,
        output_draft_dir=export_draft_dir,
        output_draft_name=export_draft_name,
        output_mp4=output_mp4,
        exported=True,
        top_level_changes=(recovery_result.top_level_changes if recovery_result else 0),
        json_changes=(recovery_result.json_changes if recovery_result else 0),
    )


def _prepare_video_source(config: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[Path, Path, str]:
    media_path = _value(source, "media_path", "video_path", "input_video", default=_value(config, "input_video", default=""))
    if not media_path:
        raise RuntimeError("source.type=video 时必须提供 source.media_path")
    media = _positive_path(media_path, "输入视频")

    canvas = _dict_value(source.get("canvas"))
    base_root = Path(
        _value(source, "work_root", "base_draft_work_root", default=_value(config, "base_draft_work_root", default=PROJECT_ROOT / "_generated_video_drafts"))
    ).expanduser().resolve()
    created = create_plain_draft_from_video(
        media,
        base_root,
        draft_name=str(_value(source, "base_draft_name", default="")),
        width=int(_value(canvas, "width", default=_value(source, "canvas_width", "width", default=0))),
        height=int(_value(canvas, "height", default=_value(source, "canvas_height", "height", default=0))),
        fps=int(_value(canvas, "fps", default=_value(source, "canvas_fps", "fps", default=30))),
        source_start_us=int(_value(source, "source_start_us", "start_us", default=0)),
        source_duration_us=int(_value(source, "source_duration_us", "duration_us", default=0)),
        fade_out_us=max(
            0,
            int(_value(source, "fade_out_us", "video_fade_out_us", default=0)),
        ),
    )

    return created.draft_dir, created.draft_dir, media.stem


def _prepare_video_sequence_source(
    config: Mapping[str, Any], source: Mapping[str, Any]
) -> tuple[Path, Path, str]:
    raw_items = _value(source, "items", "segments", "videos", default=None)
    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError("source.type=video_sequence 时必须提供 source.items")
    items: list[VideoSequenceItem] = []
    for index, raw in enumerate(raw_items, start=1):
        item = _dict_value(raw)
        media_path = _value(item, "media_path", "video_path", default="")
        if not media_path:
            raise RuntimeError(f"source.items[{index - 1}] 缺少 media_path")
        items.append(
            VideoSequenceItem(
                media_path=_positive_path(media_path, f"第 {index} 段输入视频"),
                target_duration_us=int(
                    _value(item, "target_duration_us", "duration_us", default=0)
                ),
                source_start_us=int(
                    _value(item, "source_start_us", "start_us", default=0)
                ),
                transition_after_us=int(
                    _value(item, "transition_after_us", default=0)
                ),
                volume=float(_value(item, "volume", default=1.0)),
            )
        )

    canvas = _dict_value(source.get("canvas"))
    base_root = Path(
        _value(
            source,
            "work_root",
            "base_draft_work_root",
            default=_value(
                config,
                "base_draft_work_root",
                default=PROJECT_ROOT / "_generated_video_drafts",
            ),
        )
    ).expanduser().resolve()
    created = create_plain_draft_from_videos(
        items,
        base_root,
        draft_name=str(_value(source, "base_draft_name", default="")),
        width=int(
            _value(canvas, "width", default=_value(source, "canvas_width", "width", default=0))
        ),
        height=int(
            _value(canvas, "height", default=_value(source, "canvas_height", "height", default=0))
        ),
        fps=int(_value(canvas, "fps", default=_value(source, "canvas_fps", "fps", default=30))),
        fade_out_us=max(
            0,
            int(_value(source, "fade_out_us", "video_fade_out_us", default=0)),
        ),
    )
    return created.draft_dir, created.draft_dir, items[0].media_path.stem


def _apply_captions_to_output(
    config: Mapping[str, Any],
    output_draft_dir: Path,
    *,
    timeline_offset_us: int = 0,
) -> int:
    captions = _dict_value(_value(config, "captions", "subtitles", default=None))
    caption_text = str(_value(captions, "text", "content", default="")).strip()
    raw_cues = _value(captions, "cues", default=None)
    srt_text = str(_value(captions, "srt_text", "srt", default="")).strip()
    if not caption_text and not raw_cues and not srt_text:
        return 0

    draft_data = load_plain_draft_json(output_draft_dir)
    draft_duration_us = _draft_duration_us(draft_data)
    caption_start_us = int(_value(captions, "start_us", default=0)) + timeline_offset_us
    if caption_start_us < 0 or caption_start_us >= draft_duration_us:
        raise RuntimeError(
            f"字幕开始时间超出草稿时长: start_us={caption_start_us}, draft_duration_us={draft_duration_us}"
        )

    caption_duration_us = int(_value(captions, "duration_us", default=0))
    available_duration_us = draft_duration_us - caption_start_us
    if caption_duration_us <= 0:
        caption_duration_us = available_duration_us
    elif caption_duration_us > available_duration_us:
        raise RuntimeError(
            "字幕范围超出草稿时长: "
            f"start_us={caption_start_us}, duration_us={caption_duration_us}, "
            f"draft_duration_us={draft_duration_us}"
        )

    style_json_path = _value(captions, "style_json_path", "style_json", default="")
    if style_json_path:
        _positive_path(style_json_path, "字幕样式 JSON")
    if raw_cues:
        if not isinstance(raw_cues, list):
            raise ValueError("captions.cues 必须是数组")
        cues = validate_caption_cues(
            caption_cues_from_payload(raw_cues),
            timeline_offset_us=timeline_offset_us,
            maximum_end_us=draft_duration_us,
        )
    elif srt_text:
        cues = validate_caption_cues(
            parse_srt_cues(srt_text),
            timeline_offset_us=timeline_offset_us,
            maximum_end_us=draft_duration_us,
        )
    else:
        cues = build_caption_cues(
            caption_text,
            start_us=caption_start_us,
            duration_us=caption_duration_us,
            max_chars=int(_value(captions, "max_chars", default=16)),
            min_duration_us=int(_value(captions, "min_duration_us", default=650_000)),
        )
    add_captions_to_draft(
        output_draft_dir,
        cues,
        style_json_path=style_json_path,
        size=_optional_float(captions, "size"),
        color=str(_value(captions, "color", default="")),
        stroke_color=str(_value(captions, "stroke_color", default="")),
        stroke_width=_optional_float(captions, "stroke_width"),
        transform_x=_optional_float(captions, "transform_x"),
        transform_y=_optional_float(captions, "transform_y"),
        clip_scale=_optional_float(captions, "clip_scale"),
        line_max_width=_optional_float(captions, "line_max_width"),
        single_line=_as_bool(
            _value(
                captions,
                "single_line",
                default=int(_value(captions, "max_lines", default=0)) == 1,
            )
        ),
        font_id=str(_value(captions, "font_id", default="")),
        font_path=str(_value(captions, "font_path", default="")),
        font_title=str(_value(captions, "font_title", "font_name", default="")),
        track_name=str(_value(captions, "track_name", default="网页自动字幕")),
        shadow_color=str(_value(captions, "shadow_color", default="")),
        shadow_alpha=_optional_float(captions, "shadow_alpha"),
        shadow_distance=_optional_float(captions, "shadow_distance"),
        shadow_angle=_optional_float(captions, "shadow_angle"),
        shadow_smoothing=_optional_float(captions, "shadow_smoothing"),
    )
    return len(cues)


def _draft_duration_us(data: Mapping[str, Any]) -> int:
    try:
        duration = int(data.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration > 0:
        return duration

    for track in data.get("tracks", []) if isinstance(data.get("tracks"), list) else []:
        if not isinstance(track, dict):
            continue
        for segment in track.get("segments", []) if isinstance(track.get("segments"), list) else []:
            if not isinstance(segment, dict):
                continue
            timerange = segment.get("target_timerange")
            if not isinstance(timerange, dict):
                continue
            try:
                duration = max(
                    duration,
                    int(timerange.get("start", 0) or 0) + int(timerange.get("duration", 0) or 0),
                )
            except (TypeError, ValueError):
                continue
    if duration <= 0:
        raise RuntimeError("无法从输出草稿确定字幕可用时长")
    return duration


def _prepare_template_source(config: Mapping[str, Any], source: Mapping[str, Any]) -> tuple[Path, Path, str]:
    template_id = _value(source, "template_id", default="")
    if template_id:
        library_root = _value(source, "library_root", default=_value(config, "template_library_root", default=""))
        record = TemplateLibrary(library_root or None).get(str(template_id))
        return record.draft_dir, record.draft_dir, record.template_id

    template_dir_text = _template_dir_from_config(config, source)
    if not template_dir_text:
        raise RuntimeError("source.type=template 时必须提供 source.template_draft_dir")
    source_template_dir = Path(template_dir_text).expanduser().resolve()
    if not source_template_dir.is_dir():
        raise NotADirectoryError(f"模板草稿目录不存在或不是目录: {source_template_dir}")

    decrypt = _dict_value(config.get("decrypt"))
    enabled = _value(decrypt, "enabled", "auto_decrypt", default=True)
    prepared = prepare_plain_draft_dir(
        source_template_dir,
        auto_decrypt=_as_bool(enabled),
        force_decrypt=_as_bool(_value(decrypt, "force", "force_decrypt", default=False)),
        work_root=Path(_value(decrypt, "work_root", "decrypt_work_root", default="")).expanduser().resolve()
        if _value(decrypt, "work_root", "decrypt_work_root", default="")
        else None,
        exe=Path(_value(decrypt, "exe", "jy_draftc_exe", default="")).expanduser().resolve()
        if _value(decrypt, "exe", "jy_draftc_exe", default="")
        else None,
        install_dir=Path(_value(decrypt, "install_dir", "jy_install_dir", default="")).expanduser().resolve()
        if _value(decrypt, "install_dir", "jy_install_dir", default="")
        else None,
        debug=_as_bool(_value(decrypt, "debug", default=False)),
    )
    return source_template_dir, prepared.draft_dir, source_template_dir.name


def _template_dir_from_config(config: Mapping[str, Any], source: Mapping[str, Any]) -> Any:
    return _value(
        source,
        "template_draft_dir",
        "template_dir",
        "draft_dir",
        "template",
        default=_value(config, "template_draft_dir", "template", default=""),
    )


def _build_video_replacements(config: Mapping[str, Any]) -> list[VideoSegmentReplacement]:
    replacements: list[VideoSegmentReplacement] = []
    for item in _list_config(_value(config, "video_replacements", "videos", "video", default=None), "video"):
        kind = str(_value(item, "type", "target_kind", default="video-segment")).replace("_", "-")
        if kind in ("none", "", "nested-video"):
            continue
        if kind != "video-segment":
            raise RuntimeError(f"不支持的视频替换类型: {kind!r}")
        media_path = _positive_path(_value(item, "media_path", "input_video", default=""), "输入视频/图片")
        replacements.append(
            VideoSegmentReplacement(
                media_path=media_path,
                track_index=int(_value(item, "track_index", "video_track_index", default=0)),
                segment_index=int(_value(item, "segment_index", "video_segment_index", default=0)),
                source_start_us=int(_value(item, "source_start_us", default=-1)),
                source_duration_us=int(_value(item, "source_duration_us", default=0)),
                target_start_us=int(_value(item, "target_start_us", default=-1)),
                target_duration_us=int(_value(item, "target_duration_us", default=0)),
            )
        )
    return replacements


def _build_subtitle_range_replacements(
    config: Mapping[str, Any],
) -> list[SubtitleRangeReplacement]:
    replacements: list[SubtitleRangeReplacement] = []
    for item in _list_config(
        _value(config, "subtitle_range_replacements", default=None),
        "subtitle_range_replacements",
    ):
        subtitles = []
        for line in _list_config(
            _value(item, "subtitles", "cues", default=None), "subtitles"
        ):
            subtitles.append(
                SubtitleLine(
                    start_us=int(_value(line, "start_us", default=0)),
                    duration_us=int(_value(line, "duration_us", default=0)),
                    text=str(_value(line, "text", default="")),
                )
            )
        replacements.append(
            SubtitleRangeReplacement(
                start_us=int(_value(item, "start_us", default=0)),
                end_us=int(_value(item, "end_us", default=0)),
                subtitles=subtitles,
                track_index=int(_value(item, "track_index", default=0)),
                base_segment_index=int(_value(item, "base_segment_index", default=0)),
            )
        )
    return replacements


def _build_nested_video_replacements(config: Mapping[str, Any]) -> list[NestedVideoReplacement]:
    replacements: list[NestedVideoReplacement] = []
    for item in _list_config(_value(config, "video_replacements", "videos", "video", default=None), "video"):
        kind = str(_value(item, "type", "target_kind", default="video-segment")).replace("_", "-")
        if kind != "nested-video":
            continue
        media_path = _positive_path(_value(item, "media_path", "input_video", default=""), "输入视频/图片")
        replacements.append(
            NestedVideoReplacement(
                media_path=media_path,
                nested_draft_index=int(_value(item, "nested_draft_index", default=0)),
                video_track_index=int(_value(item, "video_track_index", "nested_video_track_index", default=0)),
                segment_index=int(_value(item, "segment_index", "nested_video_segment_index", default=0)),
                source_start_us=int(_value(item, "source_start_us", default=-1)),
                source_duration_us=int(_value(item, "source_duration_us", default=0)),
                target_start_us=int(_value(item, "target_start_us", default=-1)),
                target_duration_us=int(_value(item, "target_duration_us", default=0)),
            )
        )
    return replacements


def _build_text_replacements(
    config: Mapping[str, Any],
    *,
    timeline_duration_us: int = 0,
) -> tuple[
    list[TextReplacement],
    list[TextAddition],
    list[TextStylePresetReplacement],
    list[NestedTextStylePresetReplacement],
]:
    replacements: list[TextReplacement] = []
    additions: list[TextAddition] = []
    style_replacements: list[TextStylePresetReplacement] = []
    nested_style_replacements: list[NestedTextStylePresetReplacement] = []

    for item in _list_config(_value(config, "texts", "text", default=None), "texts"):
        scope = str(_value(item, "scope", default="top")).lower()
        mode = str(_value(item, "type", "mode", default="replace")).replace("_", "-")
        text = str(_value(item, "text", default=""))
        style_json_path = _value(item, "style_json_path", "style_json", default="")
        apply_clip = _as_bool(_value(item, "apply_clip", default=True))

        if mode == "add":
            if scope != "top":
                raise RuntimeError("新增文字当前只支持 scope='top'")
            if not text:
                raise RuntimeError("新增文字必须提供 text")
            if style_json_path:
                _positive_path(style_json_path, "文本样式 JSON")
            text_effect_json_path = _value(item, "text_effect_json_path", "flower_text_json_path", default="")
            if text_effect_json_path:
                _positive_path(text_effect_json_path, "花字素材 JSON")
            start_us = int(_value(item, "start_us", default=0))
            # A source-attribution label can be inherited from a semantic
            # overlay whose rounded start lands after the actual merged video
            # has already ended.  Such text has no visible timeline range and
            # should not make an otherwise valid editable draft fail.
            if timeline_duration_us > 0 and start_us >= timeline_duration_us:
                continue
            duration_us = _resolve_timeline_duration(
                start_us,
                int(_value(item, "duration_us", default=5_000_000)),
                timeline_duration_us,
                "新增文字",
                end_tolerance_us=TEXT_END_ROUNDING_TOLERANCE_US,
            )
            opacity = float(_value(item, "opacity", "global_alpha", default=1.0))
            if not 0.0 <= opacity <= 1.0:
                raise ValueError("文字透明度必须在 0.0 到 1.0 之间")
            additions.append(
                TextAddition(
                    text=text,
                    start_us=start_us,
                    duration_us=duration_us,
                    track_name=str(_value(item, "track_name", default="")),
                    style_json_path=style_json_path,
                    text_effect_json_path=text_effect_json_path,
                    apply_clip=apply_clip,
                    relative_index=int(_value(item, "relative_index", default=999)),
                    transform_x=float(_value(item, "transform_x", default=0.0)),
                    transform_y=float(_value(item, "transform_y", default=0.0)),
                    scale=float(_value(item, "scale", "clip_scale", default=1.0)),
                    size=float(_value(item, "size", default=8.0)),
                    align=int(_value(item, "align", default=1)),
                    auto_wrapping=_as_bool(_value(item, "auto_wrapping", default=False)),
                    line_max_width=_optional_float(item, "line_max_width"),
                    color=str(_value(item, "color", default="")),
                    stroke_color=str(_value(item, "stroke_color", default="")),
                    stroke_width=_optional_float(item, "stroke_width"),
                    opacity=opacity,
                    letter_spacing=_optional_float(item, "letter_spacing"),
                    shadow_color=str(_value(item, "shadow_color", default="")),
                    shadow_alpha=_optional_float(item, "shadow_alpha"),
                    shadow_distance=_optional_float(item, "shadow_distance"),
                    shadow_angle=_optional_float(item, "shadow_angle"),
                    shadow_smoothing=_optional_float(item, "shadow_smoothing"),
                    font_id=str(_value(item, "font_id", default="")),
                    font_path=str(_value(item, "font_path", default="")),
                    font_title=str(_value(item, "font_title", default="")),
                )
            )
            continue

        if mode != "replace":
            raise RuntimeError(f"不支持的文字处理方式: {mode!r}")

        if scope == "top":
            track_index = int(_value(item, "track_index", "text_track_index", default=0))
            segment_index = int(_value(item, "segment_index", "text_segment_index", default=0))
            if style_json_path:
                _positive_path(style_json_path, "文本样式 JSON")
                style_replacements.append(
                    TextStylePresetReplacement(
                        style_json_path=style_json_path,
                        text=text,
                        apply_clip=apply_clip,
                        track_index=track_index,
                        segment_index=segment_index,
                    )
                )
            elif text:
                replacements.append(TextReplacement(text=text, track_index=track_index, segment_index=segment_index))
            continue

        if scope == "nested":
            if not style_json_path:
                raise RuntimeError("嵌套模板文字替换必须提供 style_json_path")
            _positive_path(style_json_path, "文本样式 JSON")
            nested_style_replacements.append(
                NestedTextStylePresetReplacement(
                    style_json_path=style_json_path,
                    text=text,
                    apply_clip=apply_clip,
                    nested_draft_index=int(_value(item, "nested_draft_index", default=0)),
                    text_track_index=int(_value(item, "text_track_index", "track_index", default=0)),
                    segment_index=int(_value(item, "segment_index", "text_segment_index", "nested_segment_index", default=0)),
                )
            )
            continue

        raise RuntimeError(f"不支持的文字 scope: {scope!r}")

    return replacements, additions, style_replacements, nested_style_replacements


def _build_existing_text_style_replacements(
    config: Mapping[str, Any],
    draft_data: Mapping[str, Any],
) -> list[TextStylePresetReplacement]:
    style = _dict_value(
        _value(config, "existing_text_style", "restyle_existing_text", default=None)
    )
    style_json_path = _value(style, "style_json_path", "style_json", default="")
    if not style_json_path:
        return []
    _positive_path(style_json_path, "已有字幕样式 JSON")

    materials = draft_data.get("materials", {})
    text_materials = materials.get("texts", []) if isinstance(materials, dict) else []
    text_material_ids = {
        str(item.get("id", ""))
        for item in text_materials
        if isinstance(item, dict) and item.get("id")
    }
    replacements: list[TextStylePresetReplacement] = []
    text_track_index = 0
    tracks = draft_data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict) or track.get("type") != "text":
            continue
        segments = track.get("segments", [])
        for segment_index, segment in enumerate(segments if isinstance(segments, list) else []):
            if not isinstance(segment, dict):
                continue
            if str(segment.get("material_id", "")) not in text_material_ids:
                continue
            replacements.append(
                TextStylePresetReplacement(
                    style_json_path=style_json_path,
                    text="",
                    apply_clip=_as_bool(_value(style, "apply_clip", default=True)),
                    track_index=text_track_index,
                    segment_index=segment_index,
                )
            )
        text_track_index += 1
    if not replacements:
        raise RuntimeError("当前母版没有可批量替换样式的普通文字片段")
    return replacements


def _build_existing_text_font_replacements(
    config: Mapping[str, Any],
    draft_data: Mapping[str, Any],
) -> list[TextFontReplacement]:
    font = _dict_value(
        _value(config, "existing_text_font", "replace_existing_text_font", default=None)
    )
    font_id = str(_value(font, "font_id", "resource_id", default="")).strip()
    font_path_value = _value(font, "font_path", "path", default="")
    if not font_id and not font_path_value:
        return []
    if not font_id or not font_path_value:
        raise RuntimeError("替换已有字幕字体必须同时提供 font_id 和 font_path")
    font_path = _positive_path(font_path_value, "已有字幕字体文件")
    font_title = str(_value(font, "font_title", "font_name", "name", default="")).strip()

    materials = draft_data.get("materials", {})
    text_materials = materials.get("texts", []) if isinstance(materials, dict) else []
    text_material_ids = {
        str(item.get("id", ""))
        for item in text_materials
        if isinstance(item, dict) and item.get("id")
    }
    replacements: list[TextFontReplacement] = []
    text_track_index = 0
    tracks = draft_data.get("tracks", [])
    for track in tracks if isinstance(tracks, list) else []:
        if not isinstance(track, dict) or track.get("type") != "text":
            continue
        segments = track.get("segments", [])
        for segment_index, segment in enumerate(segments if isinstance(segments, list) else []):
            if not isinstance(segment, dict):
                continue
            if str(segment.get("material_id", "")) not in text_material_ids:
                continue
            replacements.append(
                TextFontReplacement(
                    font_id=font_id,
                    font_path=str(font_path),
                    font_title=font_title,
                    track_index=text_track_index,
                    segment_index=segment_index,
                )
            )
        text_track_index += 1
    if not replacements:
        raise RuntimeError("当前母版没有可批量替换字体的普通文字片段")
    return replacements


def _resolve_timeline_duration(
    start_us: int,
    duration_us: int,
    timeline_duration_us: int,
    label: str,
    *,
    end_tolerance_us: int = 0,
) -> int:
    if start_us < 0:
        raise RuntimeError(f"{label}开始时间不能为负数")
    if timeline_duration_us <= 0 or start_us >= timeline_duration_us:
        raise RuntimeError(
            f"{label}开始时间超出视频时长: start_us={start_us}, video_duration_us={timeline_duration_us}"
        )
    resolved = duration_us if duration_us > 0 else timeline_duration_us - start_us
    if resolved <= 0:
        raise RuntimeError(
            f"{label}时间范围超出视频时长: start_us={start_us}, duration_us={resolved}, "
            f"video_duration_us={timeline_duration_us}"
        )
    end_overrun_us = start_us + resolved - timeline_duration_us
    if 0 < end_overrun_us <= max(0, int(end_tolerance_us)):
        return timeline_duration_us - start_us
    if end_overrun_us > 0:
        raise RuntimeError(
            f"{label}时间范围超出视频时长: start_us={start_us}, duration_us={resolved}, "
            f"video_duration_us={timeline_duration_us}"
        )
    return resolved


def _build_text_template_additions(
    config: Mapping[str, Any],
    *,
    timeline_duration_us: int,
) -> list[TextTemplateAddition]:
    additions: list[TextTemplateAddition] = []
    for item in _list_config(_value(config, "text_templates", "text_template", default=None), "text_templates"):
        template_json_path = _positive_path(
            _value(item, "template_json_path", "metadata_path", default=""),
            "复合文字模板 JSON",
        )
        texts_value = item.get("texts", [])
        if not isinstance(texts_value, list):
            raise RuntimeError("复合文字模板 texts 必须是数组")
        start_us = int(_value(item, "start_us", default=0))
        duration_us = _resolve_timeline_duration(
            start_us,
            int(_value(item, "duration_us", default=0)),
            timeline_duration_us,
            "复合文字模板",
        )
        additions.append(
            TextTemplateAddition(
                template_json_path=template_json_path,
                texts=[str(value) for value in texts_value],
                start_us=start_us,
                duration_us=duration_us,
                track_name=str(_value(item, "track_name", default="")),
            )
        )
    return additions


def _build_audio_replacements(
    config: Mapping[str, Any],
    *,
    timeline_duration_us: int = 0,
) -> tuple[list[NamedAudioReplacement], list[AudioSegmentReplacement], list[AudioAddition]]:
    named: list[NamedAudioReplacement] = []
    segments: list[AudioSegmentReplacement] = []
    additions: list[AudioAddition] = []

    for item in _list_config(_value(config, "audios", "audio", default=None), "audios"):
        mode = str(_value(item, "type", "mode", default="add")).replace("_", "-")
        media_path = _positive_file(
            _value(item, "media_path", "audio_path", default=""), "音频"
        )
        source_start_us = int(_value(item, "source_start_us", default=-1))
        source_duration_us = int(_value(item, "source_duration_us", default=0))
        target_start_us = int(_value(item, "target_start_us", "start_us", default=0))
        target_duration_us = int(_value(item, "target_duration_us", "duration_us", default=0))
        fit_to_video = _as_bool(
            _value(item, "fit_to_video", "fit_to_timeline", default=False)
        )
        if (
            mode in ("add", "bgm")
            and target_duration_us <= 0
            and fit_to_video
        ):
            target_duration_us = max(0, timeline_duration_us - max(0, target_start_us))
        source_start_us, source_duration_us = _normalise_audio_source_timerange(
            source_start_us,
            source_duration_us,
            target_duration_us,
        )
        volume = float(_value(item, "volume", default=1.0))
        if not 0.0 <= volume <= 2.0:
            raise ValueError(f"BGM volume 必须在 0.0 到 2.0 之间: {volume}")
        if mode in ("add", "bgm"):
            additions.append(
                AudioAddition(
                    media_path=media_path,
                    source_start_us=source_start_us,
                    source_duration_us=source_duration_us,
                    target_start_us=target_start_us,
                    target_duration_us=target_duration_us,
                    volume=volume,
                    loop_to_target=_as_bool(
                        _value(
                            item,
                            "loop_to_target",
                            "loop_to_video",
                            default=(mode == "bgm" and fit_to_video),
                        )
                    ),
                    align_to_end=_as_bool(
                        _value(item, "align_to_end", "align_end_to_video", default=False)
                    ),
                    crossfade_us=max(
                        0,
                        int(_value(item, "crossfade_us", default=0)),
                    ),
                    fade_in_us=max(
                        0,
                        int(_value(item, "fade_in_us", "fade_in_duration_us", default=0)),
                    ),
                )
            )
        elif mode == "replace-segment":
            segments.append(
                AudioSegmentReplacement(
                    media_path=media_path,
                    track_index=int(_value(item, "track_index", "audio_track_index", default=0)),
                    segment_index=int(_value(item, "segment_index", "audio_segment_index", default=0)),
                    source_start_us=source_start_us,
                    source_duration_us=source_duration_us,
                    target_start_us=target_start_us,
                    target_duration_us=target_duration_us,
                )
            )
        elif mode == "replace-named":
            named.append(
                NamedAudioReplacement(
                    media_path=media_path,
                    material_name=str(_value(item, "material_name", default="")),
                )
            )
        else:
            raise RuntimeError(f"不支持的音乐处理方式: {mode!r}")

    return named, segments, additions


def _normalise_audio_source_timerange(
    source_start_us: int,
    source_duration_us: int,
    target_duration_us: int,
) -> tuple[int, int]:
    if source_start_us >= 0 and source_duration_us <= 0:
        if target_duration_us > 0:
            source_duration_us = target_duration_us
        elif source_start_us == 0:
            source_start_us = -1
    return source_start_us, source_duration_us


def _build_effect_additions(
    config: Mapping[str, Any],
    *,
    timeline_duration_us: int,
) -> list[EffectAddition]:
    additions: list[EffectAddition] = []
    for item in _list_config(_value(config, "effects", "effect", default=None), "effects"):
        effect_json_path = _positive_path(_value(item, "effect_json_path", "effect_json", default=""), "特效 JSON")
        start_us = int(_value(item, "start_us", default=0))
        # Older web jobs used -1 to mean "follow the first video segment".
        # The product-level default is now the complete draft timeline.
        if start_us < 0:
            start_us = 0
        duration_us = _resolve_timeline_duration(
            start_us,
            int(_value(item, "duration_us", default=0)),
            timeline_duration_us,
            "视频特效",
        )
        additions.append(
            EffectAddition(
                effect_json_path=effect_json_path,
                target_video_track_index=int(_value(item, "target_video_track_index", "video_track_index", default=0)),
                target_video_segment_index=int(_value(item, "target_video_segment_index", "video_segment_index", default=0)),
                start_us=start_us,
                duration_us=duration_us,
            )
        )
    return additions


def _build_sticker_additions(config: Mapping[str, Any]) -> list[StickerAddition]:
    additions: list[StickerAddition] = []
    for item in _list_config(_value(config, "stickers", "sticker", default=None), "stickers"):
        sticker_json_path = _positive_path(
            _value(item, "sticker_json_path", "metadata_path", default=""),
            "全屏贴纸 JSON",
        )
        opacity = float(_value(item, "opacity", "alpha", default=1.0))
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("贴纸透明度必须在 0.0 到 1.0 之间")
        scale = float(_value(item, "scale", default=1.0))
        if scale <= 0.0 or scale > 10.0:
            raise ValueError("贴纸缩放必须大于 0 且不超过 10")
        additions.append(
            StickerAddition(
                sticker_json_path=sticker_json_path,
                start_us=int(_value(item, "start_us", default=0)),
                duration_us=int(_value(item, "duration_us", default=0)),
                corner=str(_value(item, "corner", default="")),
                visible_ratio=float(_value(item, "visible_ratio", default=0.05)),
                scale=scale,
                rotation=float(_value(item, "rotation", default=0.0)),
                opacity=opacity,
                track_name=str(_value(item, "track_name", default="")),
                transform_x=_optional_float(item, "transform_x"),
                transform_y=_optional_float(item, "transform_y"),
            )
        )
    return additions


def _build_visual_overlay_additions(
    config: Mapping[str, Any],
) -> list[ImageAddition]:
    additions: list[ImageAddition] = []
    for item in _list_config(
        _value(config, "visual_overlays", default=None), "visual_overlays"
    ):
        if not _as_bool(_value(item, "enabled", default=True)):
            continue
        if str(_value(item, "media_type", default="image")) == "video":
            continue
        bundle_text = str(_value(item, "bundle_path", default="")).strip()
        if not bundle_text:
            continue
        bundle_path = Path(bundle_text).expanduser().resolve()
        image_path = (
            bundle_path / "resources" / "sticker" / "singleImage.png"
            if bundle_path.is_dir()
            else bundle_path
        )
        opacity = float(_value(item, "opacity", default=1.0))
        scale = float(_value(item, "scale", default=1.0))
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("语义贴图透明度必须在 0.0 到 1.0 之间")
        if scale <= 0.0 or scale > 2.0:
            raise ValueError("语义贴图缩放必须大于 0 且不超过 2")
        corner = str(_value(item, "corner", default=""))
        additions.append(
            ImageAddition(
                image_path=image_path,
                start_us=int(_value(item, "start_us", default=0)),
                duration_us=int(_value(item, "duration_us", default=1_800_000)),
                corner="" if corner == "center" else corner,
                scale=scale,
                rotation=float(_value(item, "rotation", default=0.0)),
                opacity=opacity,
                track_name="语义前景图片",
                optional=True,
                render_below_text=True,
                layer_order=10,
                transform_x=_optional_float(item, "transform_x"),
                transform_y=_optional_float(item, "transform_y"),
            )
        )
    return additions


def _build_visual_video_additions(
    config: Mapping[str, Any],
) -> list[VideoOverlayAddition]:
    additions: list[VideoOverlayAddition] = []
    for item in _list_config(
        _value(config, "visual_overlays", default=None), "visual_overlays"
    ):
        if not _as_bool(_value(item, "enabled", default=True)):
            continue
        if str(_value(item, "media_type", default="image")) != "video":
            continue
        video_text = str(_value(item, "video_path", default="")).strip()
        if not video_text:
            continue
        corner = str(_value(item, "corner", default="center"))
        scale = float(_value(item, "scale", default=1.0))
        fullscreen = corner == "center" and scale >= 0.95
        additions.append(
            VideoOverlayAddition(
                video_path=Path(video_text).expanduser().resolve(),
                start_us=int(_value(item, "start_us", default=0)),
                duration_us=int(_value(item, "duration_us", default=1_800_000)),
                source_start_us=int(_value(item, "source_start_us", default=0)),
                source_duration_us=int(
                    _value(item, "source_duration_us", default=0)
                ),
                # Semantic videos and B-roll are one-shot even when an old
                # frozen recipe still carries loop_to_target=true.
                loop_to_target=False,
                mute=_as_bool(_value(item, "mute", default=True)),
                fit=str(_value(item, "fit", default="cover")),
                corner=corner,
                scale=scale,
                opacity=float(_value(item, "opacity", default=1.0)),
                track_name=str(
                    _value(
                        item,
                        "track_name",
                        default="全屏 B-roll" if fullscreen else "语义前景视频",
                    )
                ),
                optional=_as_bool(_value(item, "optional", default=True)),
                render_below_text=_as_bool(
                    _value(item, "render_below_text", default=True)
                ),
                layer_order=int(
                    _value(item, "layer_order", default=30 if fullscreen else 10)
                ),
            )
        )
    return additions


def _build_fixed_overlay_additions(
    config: Mapping[str, Any],
    *,
    timeline_duration_us: int = 0,
) -> list[ImageAddition | StickerAddition]:
    """Build branding first, directly above the base video and below every overlay."""

    additions: list[ImageAddition | StickerAddition] = []
    for item in _list_config(
        _value(config, "fixed_overlays", default=None), "fixed_overlays"
    ):
        if not _as_bool(_value(item, "enabled", default=True)):
            continue
        bundle_text = str(_value(item, "bundle_path", default="")).strip()
        if not bundle_text:
            continue
        bundle_path = Path(bundle_text).expanduser().resolve()
        image_path = (
            bundle_path / "resources" / "sticker" / "singleImage.png"
            if bundle_path.is_dir()
            else bundle_path
        )
        opacity = float(_value(item, "opacity", default=1.0))
        scale = float(_value(item, "scale", default=0.5))
        start_us = int(_value(item, "start_us", default=0))
        raw_duration_us = int(_value(item, "duration_us", default=0))
        duration_us = (
            _resolve_timeline_duration(
                start_us,
                raw_duration_us,
                timeline_duration_us,
                "固定前景",
            )
            if timeline_duration_us > 0
            else raw_duration_us
        )
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("固定前景透明度必须在 0.0 到 1.0 之间")
        if scale <= 0.0 or scale > 2.0:
            raise ValueError("固定前景缩放必须大于 0 且不超过 2")
        if str(_value(item, "renderer", default="photo")).strip().lower() == "sticker":
            additions.append(
                StickerAddition(
                    sticker_json_path=bundle_path / "sticker.json",
                    start_us=start_us,
                    duration_us=duration_us,
                    scale=scale,
                    rotation=float(_value(item, "rotation", default=0.0)),
                    opacity=opacity,
                    track_name=str(_value(item, "track_name", default="固定人名牌")),
                    transform_x=_optional_float(item, "transform_x"),
                    transform_y=_optional_float(item, "transform_y"),
                    optional=True,
                    render_below_text=True,
                )
            )
            continue
        additions.append(
            ImageAddition(
                image_path=image_path,
                start_us=start_us,
                duration_us=duration_us,
                corner=str(_value(item, "corner", default="middle_left")),
                scale=scale,
                rotation=float(_value(item, "rotation", default=0.0)),
                opacity=opacity,
                track_name=str(_value(item, "track_name", default="固定人名牌")),
                optional=True,
                render_below_text=True,
                layer_order=0,
                transform_x=_optional_float(item, "transform_x"),
                transform_y=_optional_float(item, "transform_y"),
            )
        )
    return additions


def _build_visual_variant(config: Mapping[str, Any]) -> VisualVariant | None:
    raw = config.get("visual_variant")
    if raw is None:
        return None
    value = _dict_value(raw)
    if not value or not _as_bool(_value(value, "enabled", default=True)):
        return None
    interval_seconds = float(_value(value, "mirror_interval_seconds", default=10.0))
    if interval_seconds <= 0 or interval_seconds > 3600:
        raise ValueError("镜像间隔必须大于 0 秒且不超过 3600 秒")
    sample_count = int(_value(value, "face_sample_count", default=3))
    if sample_count <= 0 or sample_count > 9:
        raise ValueError("人脸定位采样帧数必须是 1 到 9")
    crop_offset_x = float(_value(value, "crop_offset_x", default=0.0))
    crop_offset_y = float(_value(value, "crop_offset_y", default=0.0))
    crop_zoom = float(_value(value, "crop_zoom", default=1.0))
    if not -1.0 <= crop_offset_x <= 1.0 or not -1.0 <= crop_offset_y <= 1.0:
        raise ValueError("裁剪微调必须在 -1.0 到 1.0 之间")
    if not 1.0 <= crop_zoom <= 4.0:
        raise ValueError("裁剪缩放必须在 1.0 到 4.0 之间")
    return VisualVariant(
        mirror_interval_us=int(round(interval_seconds * 1_000_000)),
        crop_ratio=str(_value(value, "crop_ratio", default="1:1")),
        background_color=str(_value(value, "background_color", default="#000000")),
        face_centered=_as_bool(_value(value, "face_centered", default=True)),
        face_sample_count=sample_count,
        video_track_index=int(_value(value, "video_track_index", default=0)),
        crop_offset_x=crop_offset_x,
        crop_offset_y=crop_offset_y,
        crop_zoom=crop_zoom,
    )


def _build_cover(
    config: Mapping[str, Any],
    source_data: Mapping[str, Any],
) -> CoverConfig | None:
    raw = config.get("cover")
    if raw is None:
        return None
    value = _dict_value(raw)
    if not value or not _as_bool(_value(value, "enabled", default=True)):
        return None
    fps = float(_value(value, "fps", default=source_data.get("fps", 30) or 30))
    frame_time_seconds = float(
        _value(value, "frame_time_seconds", "time_seconds", default=0.0)
    )
    font = _dict_value(
        _value(
            value,
            "font",
            default=_value(config, "existing_text_font", default=_value(config, "captions", default=None)),
        )
    )
    overlay_top = float(_value(value, "overlay_top_ratio", default=0.429375))
    overlay_bottom = float(_value(value, "overlay_bottom_ratio", default=0.789375))
    return CoverConfig(
        frame_time_us=int(round(frame_time_seconds * 1_000_000)),
        frame_source=str(_value(value, "frame_source", default="preview_material")),
        image_path=str(_value(value, "image_path", default="")).strip(),
        fps=fps,
        frame_count=int(_value(value, "frame_count", default=3)),
        text_line_1=str(_value(value, "text_line_1", "text1", default="默认文本")),
        text_line_2=str(_value(value, "text_line_2", "text2", default="默认文本")),
        text_size=float(_value(value, "text_size", default=30.0)),
        text_scale=float(_value(value, "text_scale", default=1.0)),
        line_1_x=float(_value(value, "line_1_x", default=0.0)),
        line_1_y=float(_value(value, "line_1_y", default=-160.0 / 1920.0)),
        line_2_x=float(_value(value, "line_2_x", default=0.0)),
        line_2_y=float(_value(value, "line_2_y", default=-655.0 / 1920.0)),
        line_1_size=float(_value(value, "line_1_size", default=_value(value, "text_size", default=30.0))),
        line_2_size=float(_value(value, "line_2_size", default=22.0)),
        line_1_color=str(_value(value, "line_1_color", default="#FADF4A")),
        line_2_color=str(_value(value, "line_2_color", default="#F5F6F0")),
        frame_scale=float(_value(value, "frame_scale", default=1.0)),
        frame_offset_x=float(_value(value, "frame_offset_x", default=0.0)),
        frame_offset_y=float(_value(value, "frame_offset_y", default=0.0)),
        overlay_alpha=float(_value(value, "overlay_alpha", default=0.5)),
        overlay_x_ratio=float(_value(value, "overlay_x_ratio", default=0.5)),
        overlay_y_ratio=float(_value(value, "overlay_y_ratio", default=(overlay_top + overlay_bottom) / 2.0)),
        overlay_width_ratio=float(_value(value, "overlay_width_ratio", default=1.0)),
        overlay_height_ratio=float(_value(value, "overlay_height_ratio", default=overlay_bottom - overlay_top)),
        overlay_top_ratio=overlay_top,
        overlay_bottom_ratio=overlay_bottom,
        font_id=str(_value(font, "font_id", "resource_id", default="")).strip(),
        font_path=str(_value(font, "font_path", "path", default="")).strip(),
        font_title=str(_value(font, "font_title", "font_name", "name", default="")).strip(),
        letter_spacing=int(_value(value, "letter_spacing", default=0)),
        line_spacing=int(_value(value, "line_spacing", default=6)),
        auto_wrapping=_as_bool(_value(value, "auto_wrapping", default=False)),
        max_line_width=float(_value(value, "max_line_width", default=0.86)),
        line_1_shadow_color=str(_value(value, "line_1_shadow_color", default="#000000")),
        line_1_shadow_alpha=float(_value(value, "line_1_shadow_alpha", default=0.9)),
        line_1_shadow_smoothing=float(_value(value, "line_1_shadow_smoothing", default=0.15)),
        line_1_shadow_distance=float(_value(value, "line_1_shadow_distance", default=5.0)),
        line_1_shadow_angle=float(_value(value, "line_1_shadow_angle", default=-45.0)),
        line_2_shadow_color=str(_value(value, "line_2_shadow_color", default="#1F1A05")),
        line_2_shadow_alpha=float(_value(value, "line_2_shadow_alpha", default=0.5)),
        line_2_shadow_smoothing=float(_value(value, "line_2_shadow_smoothing", default=0.15)),
        line_2_shadow_distance=float(_value(value, "line_2_shadow_distance", default=5.0)),
        line_2_shadow_angle=float(_value(value, "line_2_shadow_angle", default=-45.0)),
    )


def _optional_volume(config: Mapping[str, Any]) -> float | None:
    raw = _value(config, "original_video_volume", "original_audio_volume", default=None)
    if raw is None or raw == "":
        return None
    value = float(raw)
    if not 0.0 <= value <= 2.0:
        raise ValueError("原视频音量必须在 0.0 到 2.0 之间")
    return value


def _export_mp4(
    draft_name: str,
    output_mp4: Path,
    *,
    resolution: str = "",
    framerate: str = "",
    timeout: float = 1200,
) -> None:
    print(
        f"[export] 开始调用剪映导出 draft={draft_name} output={output_mp4} "
        f"resolution={resolution or '默认'} framerate={framerate or '默认'} timeout={timeout}",
        flush=True,
    )
    controller_type, resolution_type, framerate_type = _load_export_api()
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    if output_mp4.exists():
        raise FileExistsError(f"输出 MP4 已存在，为避免覆盖已停止: {output_mp4}")
    export_kwargs = {
        "resolution": _enum_by_value(resolution_type, resolution, "分辨率"),
        "framerate": _enum_by_value(framerate_type, framerate, "帧率"),
        "timeout": timeout,
    }
    discovery_attempts = DRAFT_DISCOVERY_ATTEMPTS
    for attempt in range(1, discovery_attempts + 1):
        controller = controller_type()
        try:
            controller.export_draft(draft_name, str(output_mp4), **export_kwargs)
            break
        except Exception as exc:
            draft_not_found = _is_draft_not_found(exc)
            editor_not_open = "未在编辑窗口中找到导出按钮" in str(exc)
            transient_com_error = _is_transient_ui_automation_error(exc)
            retryable = draft_not_found or editor_not_open or transient_com_error
            if not retryable or attempt >= discovery_attempts:
                if editor_not_open:
                    raise RuntimeError(
                        f"剪映点击草稿后未进入编辑页，已重试 {attempt} 次: draft={draft_name}"
                    ) from exc
                if transient_com_error:
                    raise RuntimeError(
                        f"剪映 UI 自动化连接失效，已重试 {attempt} 次: draft={draft_name}"
                    ) from exc
                raise
            home_refreshed = (
                _refresh_jianying_draft_home(controller) if draft_not_found else False
            )
            if editor_not_open:
                print(
                    f"[export] 已找到草稿但点击后未进入编辑页 draft={draft_name}，"
                    f"重新聚焦剪映并点击（{attempt}/{discovery_attempts}）",
                    flush=True,
                )
            elif transient_com_error:
                print(
                    f"[export] 剪映 UI 自动化连接暂时失效 draft={draft_name}，"
                    f"2 秒后重建控制器（{attempt}/{discovery_attempts}）",
                    flush=True,
                )
            else:
                print(
                    f"[export] 剪映首页暂未识别草稿 draft={draft_name}，"
                    f"{'已主动刷新主页，' if home_refreshed else ''}"
                    f"2 秒后重试（{attempt}/{discovery_attempts}）",
                    flush=True,
                )
            time.sleep(DRAFT_DISCOVERY_RETRY_DELAY_SECONDS)
    print(f"[export] 剪映导出完成 output={output_mp4}", flush=True)


def _refresh_jianying_draft_home(controller: Any) -> bool:
    """Best-effort catalogue refresh after Jianying cannot see a new draft.

    Newly written draft folders can take several seconds to enter Jianying's
    virtualised home catalogue.  Recreating the automation controller alone
    does not refresh that catalogue, so request the standard F5 refresh before
    the next discovery attempt.  Any UI-version-specific failure is ignored and
    the existing retry/rebuild path remains available.
    """

    try:
        get_window = getattr(controller, "get_window", None)
        if callable(get_window):
            get_window()
        switch_to_home = getattr(controller, "switch_to_home", None)
        if callable(switch_to_home):
            switch_to_home()
        app = getattr(controller, "app", None)
        send_keys = getattr(app, "SendKeys", None)
        if not callable(send_keys):
            return False
        set_active = getattr(app, "SetActive", None)
        if callable(set_active):
            set_active()
        send_keys("{F5}", waitTime=0.2)
        return True
    except Exception as exc:
        print(f"[export] 剪映主页刷新失败，将继续按原策略重试: {exc}", flush=True)
        return False


def _is_draft_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ == "DraftNotFound"


def _is_transient_ui_automation_error(exc: BaseException) -> bool:
    if type(exc).__name__ != "COMError":
        return False
    message = str(exc)
    return "-2147220991" in message or "事件无法调用任何订户" in message


def _load_export_api() -> tuple[type, type, type]:
    try:
        from pyJianYingDraft import ExportFramerate, ExportResolution, JianyingController
    except Exception as exc:
        raise RuntimeError("无法导入 pyJianYingDraft 导出控制器；请使用已安装依赖的 Python") from exc
    return JianyingController, ExportResolution, ExportFramerate


def _enum_by_value(enum_type: type, value: str, label: str):
    if not value:
        return None
    for item in enum_type:
        if item.value.lower() == value.lower():
            return item
    choices = ", ".join(item.value for item in enum_type)
    raise ValueError(f"不支持的{label}: {value!r}，可用值: {choices}")


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_config(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise RuntimeError(f"{label} 配置必须是对象或对象列表")


def _value(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _optional_float(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    return float(value)


def _positive_path(path_text: Any, label: str) -> Path:
    raw_path = str(path_text or "").strip()
    if not raw_path:
        raise FileNotFoundError(f"{label}路径为空")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{label}不存在: {path}")
    return path


def _positive_file(path_text: Any, label: str) -> Path:
    path = _positive_path(path_text, label)
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在或不是文件: {path}")
    return path


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def _safe_name(value: str) -> str:
    name = "".join("_" if char in '<>:"/\\|?*' else char for char in value.strip())
    return name.strip(" ._") or "render_job"
