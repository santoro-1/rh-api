"""Minimal pyJianYingDraft plain JSON draft probe."""

from .content_replace import (
    AudioAddition,
    AudioSegmentReplacement,
    ContentReplaceJob,
    ContentReplaceResult,
    EffectAddition,
    NamedAudioReplacement,
    NamedVideoReplacement,
    NestedTextFontReplacement,
    NestedTextStylePresetReplacement,
    NestedVideoReplacement,
    SubtitleLine,
    SubtitleRangeReplacement,
    StickerAddition,
    ImageAddition,
    TextFontReplacement,
    TextAddition,
    TextReplacement,
    TextStylePresetReplacement,
    TextTemplateAddition,
    VideoSegmentReplacement,
    VideoOverlayAddition,
    export_text_style_preset,
    run_content_replace_job,
)
from .audio_export import AudioExportResult, export_audio_library
from .audio_catalog import AudioCatalog
from .draft_factory import CreatedVideoDraft, create_plain_draft_from_video
from .draft_import_analyzer import DRAFT_IMPORT_REPORT_SCHEMA, analyze_draft_import
from .draft_upload_plan import DRAFT_UPLOAD_PLAN_SCHEMA, build_draft_upload_plan
from .local_collector import LocalCollectorService, LocalCollectorSettings
from .effect_export import EffectExportResult, export_effect_library
from .font_export import FontExportResult, export_font_library, refresh_font_library_metadata
from .text_effect_export import TextEffectExportResult, export_text_effect_library
from .text_template_export import TextTemplateExportResult, export_text_template_library
from .sticker_export import StickerExportResult, export_sticker_library
from .render_job import RENDER_JOB_SCHEMA, RenderJobResult, run_render_job, run_render_job_file
from .subtitles import (
    CaptionCue,
    add_captions_to_draft,
    build_caption_cues,
    caption_cues_from_payload,
    cues_to_srt,
    parse_srt_cues,
    split_caption_text,
    validate_caption_cues,
)
from .template_library import TemplateLibrary, TemplateRecord

__version__ = "0.1.0"

__all__ = [
    "AudioAddition",
    "AudioCatalog",
    "AudioExportResult",
    "AudioSegmentReplacement",
    "ContentReplaceJob",
    "ContentReplaceResult",
    "EffectAddition",
    "EffectExportResult",
    "FontExportResult",
    "TextEffectExportResult",
    "TextTemplateExportResult",
    "NamedAudioReplacement",
    "NamedVideoReplacement",
    "NestedTextFontReplacement",
    "NestedTextStylePresetReplacement",
    "NestedVideoReplacement",
    "SubtitleLine",
    "SubtitleRangeReplacement",
    "StickerAddition",
    "ImageAddition",
    "StickerExportResult",
    "TextFontReplacement",
    "TextAddition",
    "TextReplacement",
    "TextStylePresetReplacement",
    "TextTemplateAddition",
    "VideoSegmentReplacement",
    "VideoOverlayAddition",
    "CreatedVideoDraft",
    "DRAFT_IMPORT_REPORT_SCHEMA",
    "DRAFT_UPLOAD_PLAN_SCHEMA",
    "LocalCollectorService",
    "LocalCollectorSettings",
    "CaptionCue",
    "RENDER_JOB_SCHEMA",
    "RenderJobResult",
    "TemplateLibrary",
    "TemplateRecord",
    "create_plain_draft_from_video",
    "analyze_draft_import",
    "build_draft_upload_plan",
    "add_captions_to_draft",
    "build_caption_cues",
    "caption_cues_from_payload",
    "cues_to_srt",
    "parse_srt_cues",
    "export_text_style_preset",
    "export_effect_library",
    "export_font_library",
    "refresh_font_library_metadata",
    "export_text_effect_library",
    "export_text_template_library",
    "export_sticker_library",
    "export_audio_library",
    "run_render_job",
    "run_render_job_file",
    "run_content_replace_job",
    "split_caption_text",
    "validate_caption_cues",
]
