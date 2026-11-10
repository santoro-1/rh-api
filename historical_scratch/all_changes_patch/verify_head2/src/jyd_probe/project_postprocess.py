from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable
import uuid

import jieba
import jieba.posseg as pseg
from fontTools.ttLib import TTFont

from .bgm_loudness import (
    BGM_FALLBACK_VOLUME,
    BGM_TARGET_GAP_DB,
    BGM_STRONG_VOCAL_EXTRA_GAP_DB,
    automatic_bgm_mix,
    fallback_bgm_volume,
)
from .caption_alignment import (
    CaptionAlignmentError,
    alignment_matches,
    retime_render_cues,
)
from .music_matching import MusicProfileMatcher
from .project_music import (
    ProjectMusicSelector,
    automatic_music_identity_counts,
    item_video_duration_us,
    manual_music_selection,
)
from .project_export_naming import (
    available_draft_name,
    composition_draft_name,
    composition_export_filename,
)
from .project_store import ProjectStore, subtitle_analysis_sha256
from .layout_profiles import (
    DEFAULT_LAYOUT_PROFILE,
    apply_layout_to_visual_overlays,
    layout_font,
    layout_profile,
    nameplate_texts,
    normalize_layout_profile,
)
from .project_video_source import build_project_speech_audio, build_project_video_source
from .semantic_subtitles import (
    SEMANTIC_MAPPING_SCHEMA,
    SemanticSubtitleMappingError,
    map_subtitle_units_to_raw_cues,
    semantic_break_groups,
)
from .semantic_visuals import (
    SemanticVisualCatalog,
    SemanticVisualCatalogError,
    fixed_nameplate_overlay,
    frozen_visual_overlays,
    load_semantic_visual_catalog,
)
from .subtitles import CaptionCue, caption_cues_from_payload
from .unified_visual_plan import remap_saved_visual_plan


CAPTION_MAX_WIDTH_RATIO = 0.8
POSTPROCESS_STALL_TIMEOUT_SECONDS = 30 * 60
POSTPROCESS_NO_AGENT_GRACE_SECONDS = 60
CAPTION_MAX_LINES = 1
CAPTION_TRANSFORM_Y = -850 / 1920
CAPTION_BOTTOM_OFFSET_RATIO = 0.5 + CAPTION_TRANSFORM_Y / 2
CAPTION_REFERENCE_FONT_SIZE = 14.0
# Jianying v8.9 width calibration: at size 15 and line_max_width=0.8, the
# manually aligned 17-digit probe occupies 9.69 em.  Keep the renderer ratio
# at 0.8 and convert that physical width to our size-14 reference space.
CAPTION_REFERENCE_MAX_EM = 9.69 * 15.0 / CAPTION_REFERENCE_FONT_SIZE
CAPTION_STROKE_COLOR = "#000000"
CAPTION_STROKE_WIDTH = 0.06
TEXT_SHADOW_COLOR = "#000000"
TEXT_SHADOW_DISTANCE = 5.0
TEXT_SHADOW_ANGLE = -45.0
TEXT_SHADOW_SMOOTHING = 0.45000001788139343
BGM_CROSSFADE_US = 200_000
BGM_FADE_IN_US = 5_000_000
VIDEO_FADE_OUT_US = 0
FIXED_VIDEO_TITLE_TEXT = "世界冠军带你自律"
FIXED_VIDEO_TITLE_TRANSFORM_Y = 1535 / 1920
ASR_RAW_CUE_PREVIEW_FALLBACK_CODES = frozenset(
    {"ASR_SCRIPT_MATCH_TOO_LOW", "ASR_RAW_CUE_MATCH_TOO_LOW"}
)


def draft_recipe_sha256(
    job: dict[str, Any],
    *,
    subtitle_analysis_identity: str | None = None,
) -> str:
    """Hash only timeline-affecting render inputs, excluding run-specific output data."""

    recipe = {
        key: value
        for key, value in job.items()
        if key not in {"output", "observability", "batch"}
    }
    if subtitle_analysis_identity:
        recipe["__subtitle_analysis_sha256"] = subtitle_analysis_identity
    canonical = json.dumps(
        recipe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


FIXED_VIDEO_TITLE_FONT_SIZE = 19.0
FIXED_VIDEO_TITLE_COLOR = "#E53935"
FIXED_VIDEO_TITLE_STROKE_COLOR = "#FFFFFF"
FIXED_VIDEO_TITLE_STROKE_WIDTH = 0.06
TOP_TITLE_MAX_LABEL_CHARS = 5
TOP_TITLE_MAX_HEADLINE_CHARS = 14
COVER_TITLE_MAX_LINE_1_CHARS = 5
COVER_TITLE_MAX_LINE_2_CHARS = 14
GENERATED_TITLE_MAX_LINE_2_CHARS = 5
COVER_FONT_IDENTITY = "resource_id:6807742980271641102"
COVER_LINE_1_TRANSFORM_Y = -160 / 1920
COVER_LINE_2_TRANSFORM_Y = -655 / 1920
COVER_LINE_1_FONT_SIZE = 30.0
COVER_LINE_2_FONT_SIZE = 22.0
COVER_LINE_1_COLOR = "#FADF4A"
COVER_LINE_2_COLOR = "#F5F6F0"
CAPTION_MIN_SLICE_US = 80_000
_BREAK_CHARS = set("，,、：:。.！？!?；;")
_HIDDEN_CAPTION_PUNCTUATION = _BREAK_CHARS | set("…")
_HARD_SENTENCE_BREAKS = set("。.！？!?；;…\r\n")
_SOFT_SENTENCE_BREAKS = set("，,、：:")
_CLAUSE_BREAKS = set("，,：:。.！？!?；;…")
_ORPHAN_PARTICLES = tuple("的地得呢啊了吧吗")
_STRUCTURAL_PARTICLES = frozenset("的地得")
_BOUND_RELATIVE_SUFFIXES = ("类的", "中的", "里的", "内的", "上的", "下的")
_RIGHT_BINDING_CLAUSES = frozenset(
    {
        "第一",
        "第二",
        "第三",
        "第四",
        "第五",
        "首先",
        "其次",
        "再次",
        "最后",
    }
)
_PROTECTED_TERMS = (
    "表现",
    "问题",
    "世界冠军",
    "核心逻辑",
    "储存",
    "储存机制",
    "小时",
    "新中年女性",
    "女性",
    "健康体重管理",
    "胖肚子",
    "以及",
    "内脏脂肪",
    "呼吸",
    "形式",
    "脂肪",
    "糖原",
    "二氧化碳",
    "葡萄糖",
)

BOTTOM_DISCLAIMER_TEXT = (
    "非医疗保健科普：仅供参考，个人经验分享，不代表普遍性\n"
    "如有不适请线下就医"
)
BOTTOM_DISCLAIMER_FONT_SIZE = 6.0
BOTTOM_DISCLAIMER_TRANSFORM_Y = -1760 / 1920
BOTTOM_DISCLAIMER_COLOR = "#FFFFFF"
BOTTOM_DISCLAIMER_OPACITY = 0.5

_COVER_TITLE_SAFE_FALLBACK = {"line_1": "生活提醒", "line_2": "理性看待"}
_COVER_TITLE_HARD_RISK_FRAGMENTS = (
    # 法律、公共安全、歧视与低俗伤害。
    "颠覆政权",
    "国家秘密",
    "民族仇恨",
    "民族歧视",
    "邪教",
    "色情",
    "性暗示",
    "情趣用品",
    "赌博",
    "毒品",
    "暴力",
    "血腥",
    "惊悚",
    "残忍",
    "恐怖袭击",
    "教唆犯罪",
    "侮辱",
    "辱骂",
    "歧视",
    "造谣",
    "自杀",
    "自残",
    "凶杀",
    "枪击",
    "刺伤",
    "拷打",
    "尸体",
    "斗殴",
    "虐待",
    "体罚",
    "性侵",
    "校园欺凌",
    "家暴",
    "未成年抽烟",
    "未成年喝酒",
    "未成年吸毒",
    "婚外恋",
    "婚闹",
    "童养媳",
    # 伪科学、绝对化医疗、危险操作与药物营销。
    "祖传秘方",
    "包治百病",
    "神药",
    "根治",
    "治愈",
    "治好",
    "治疗",
    "治癌",
    "抗癌",
    "排毒",
    "偏方",
    "食物相克",
    "以形补形",
    "药",
    "医生",
    "诊断",
    "处方",
    "疗效",
    "穴位",
    "刮痧",
    "拔罐",
    "艾灸",
    "针灸",
    "放血",
    "正骨",
    "注射",
    "肿瘤",
    "癌症",
    "百分百",
    "100%",
    "绝对有效",
    "一定有效",
    "保证有效",
    "立刻见效",
    # 平台安全、隐私与私域引流。
    "钓鱼网站",
    "恶意程序",
    "病毒代码",
    "微信号",
    "加微信",
    "私信我",
    "身份证",
    "手机号",
    "住址",
    "联系电话",
    "联系方式",
    "公众号",
    "二维码",
    "私域",
    "扫码",
    "进群",
    "加群",
    # 虚构煽动和不切实际收益承诺。
    "卖惨",
    "风水",
    "运势",
    "暴富",
    "包赚",
    "稳赚",
)
_COVER_TITLE_NATURAL_REPLACEMENTS = (
    ("健康瘦久", "体重稳定"),
    ("瘦得更快", "体重变轻"),
    ("瘦得很快", "体重变轻"),
    ("瘦得快", "体重变轻"),
    ("瘦不下来", "体重难降"),
    ("瘦不了", "体重难降"),
    ("瘦下来", "变轻盈"),
    ("享瘦", "享轻盈"),
    ("变瘦", "变轻盈"),
    ("减肥", "控重"),
    ("瘦身", "塑形"),
    ("掉秤", "变轻"),
    ("脂肪", "体脂"),
    ("肚腩", "腰腹"),
    ("唯一", "关键"),
)
_COVER_TITLE_CONTACT_PATTERN = re.compile(
    r"(?:1[3-9][0-9]{9}|(?:微信|V信|v信|vx|VX|电话号码|电话号|手机号|联系我|联系我们|私信))"
)
_COVER_TITLE_WEIGHT_PATTERN = re.compile(
    r"[0-9０-９零〇一二两三四五六七八九十百千万几多]+斤"
)
_COVER_TITLE_SUPERLATIVE_PATTERN = re.compile(r"最(?!近|后|终|初)")
_COVER_TITLE_ABSOLUTE_FIRST_PATTERN = re.compile(
    r"第一(?!个|步|点|条|种|组)"
)


def _sanitize_cover_title_lines(line_1: str, line_2: str) -> dict[str, str]:
    """Return a naturally compliant cover title or a neutral safe fallback.

    This is an output safety gate, not a moderation-evasion substitution.  A
    hard-risk title is replaced as a whole; lower-risk weight-management words
    are rewritten into ordinary neutral language.
    """

    combined = f"{line_1}\n{line_2}"
    if _COVER_TITLE_CONTACT_PATTERN.search(combined) or any(
        fragment in combined for fragment in _COVER_TITLE_HARD_RISK_FRAGMENTS
    ):
        return dict(_COVER_TITLE_SAFE_FALLBACK)

    def rewrite(text: str) -> str:
        if _COVER_TITLE_WEIGHT_PATTERN.search(text):
            return "体重变化"
        for source, replacement in _COVER_TITLE_NATURAL_REPLACEMENTS:
            text = text.replace(source, replacement)
        text = _COVER_TITLE_SUPERLATIVE_PATTERN.sub("更", text)
        text = _COVER_TITLE_ABSOLUTE_FIRST_PATTERN.sub("重要", text)
        return "体重变化" if "瘦" in text else text

    safe = {"line_1": rewrite(line_1), "line_2": rewrite(line_2)}
    if (
        not safe["line_1"]
        or not safe["line_2"]
        or safe["line_1"] == safe["line_2"]
        or len(safe["line_1"]) > COVER_TITLE_MAX_LINE_1_CHARS
        or len(safe["line_2"]) > COVER_TITLE_MAX_LINE_2_CHARS
    ):
        return dict(_COVER_TITLE_SAFE_FALLBACK)
    return safe

def normalize_top_title(value: Any) -> dict[str, str]:
    """Normalize the optional two-line fixed top title contract."""

    if value is None:
        return {"label": "", "headline": ""}
    if not isinstance(value, dict):
        raise ValueError("顶部固定标题必须是对象")

    def clean(*keys: str) -> str:
        raw = next((value.get(key) for key in keys if value.get(key) is not None), "")
        return re.sub(r"\s+", " ", str(raw or "")).strip()

    label = clean("label", "topic", "line_1")
    headline = clean("headline", "title", "line_2")
    if len(label) > TOP_TITLE_MAX_LABEL_CHARS:
        raise ValueError(f"顶部黄色小标题最多 {TOP_TITLE_MAX_LABEL_CHARS} 个字符")
    if len(headline) > TOP_TITLE_MAX_HEADLINE_CHARS:
        raise ValueError(f"顶部白色主标题最多 {TOP_TITLE_MAX_HEADLINE_CHARS} 个字符")
    return {"label": label, "headline": headline}


def normalize_cover_title(value: Any) -> dict[str, str]:
    """Normalize the model-owned two-line cover title contract."""

    if value is None:
        return {"line_1": "", "line_2": ""}
    if not isinstance(value, dict):
        raise ValueError("封面标题必须是对象")

    def clean(*keys: str) -> str:
        raw = next((value.get(key) for key in keys if value.get(key) is not None), "")
        text = str(raw or "").strip()
        if any(character.isspace() for character in text):
            raise ValueError("封面标题每行不能包含空格或换行")
        return text

    line_1 = clean("line_1", "topic", "label")
    line_2 = clean("line_2", "hook", "headline", "title")
    if bool(line_1) != bool(line_2):
        raise ValueError("封面标题必须同时提供两行")

    def validate_lengths(first: str, second: str) -> None:
        for index, (text, limit) in enumerate(
            (
                (first, COVER_TITLE_MAX_LINE_1_CHARS),
                (second, COVER_TITLE_MAX_LINE_2_CHARS),
            ),
            start=1,
        ):
            if len(text) > limit:
                raise ValueError(
                    f"封面第 {index} 行最多 {limit} 个字符"
                )

    # Preserve the existing public contract: malformed/overlong input remains
    # an explicit validation error. Safety rewriting applies only after the
    # caller has supplied a structurally valid title.
    validate_lengths(line_1, line_2)
    if line_1 and line_2:
        safe_title = _sanitize_cover_title_lines(line_1, line_2)
        line_1 = safe_title["line_1"]
        line_2 = safe_title["line_2"]
        validate_lengths(line_1, line_2)
    return {"line_1": line_1, "line_2": line_2}


def build_project_cover(
    item: dict[str, Any],
    *,
    fonts: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Build the fixed project cover recipe from saved titles and route media."""

    postprocess = dict(item.get("settings", {}).get("postprocess") or {})
    title = normalize_cover_title(
        postprocess.get("title") or postprocess.get("cover_title")
    )
    if not title["line_1"]:
        return None
    base_video = item.get("outputs", {}).get("base_video")
    use_video_first_frame = (
        isinstance(base_video, dict)
        and str(base_video.get("source_type") or "").strip().lower() == "ltx"
    )
    image_path: Path | None = None
    if not use_video_first_frame:
        image = resolve_project_cover_image(item)
        if not isinstance(image, dict) or not image.get("managed_path"):
            raise ValueError(f"任务 {item.get('row_key') or item.get('item_id')} 缺少封面原图")
        image_path = Path(str(image["managed_path"])).expanduser().resolve()
        if not image_path.is_file():
            raise ValueError(f"任务 {item.get('row_key') or item.get('item_id')} 的封面原图不存在")
    font = fonts.get(COVER_FONT_IDENTITY)
    if not isinstance(font, dict) or not font.get("path"):
        raise ValueError("固定封面字体“思源粗宋”不可用")
    profile = layout_profile(postprocess.get("layout_profile", DEFAULT_LAYOUT_PROFILE))
    cover_style = profile["cover"]
    caption_style = profile["caption"]
    overlay_y_ratio = float(cover_style["overlay_y_ratio"])
    overlay_height_ratio = float(cover_style["overlay_height_ratio"])
    overlay_top = overlay_y_ratio - overlay_height_ratio / 2
    overlay_bottom = overlay_y_ratio + overlay_height_ratio / 2
    return {
        "enabled": True,
        "frame_source": "timeline" if use_video_first_frame else "input_image",
        "image_path": str(image_path) if image_path is not None else "",
        "frame_time_seconds": 0,
        "frame_count": 3,
        "text_line_1": title["line_1"],
        "text_line_2": title["line_2"],
        "font": {
            "font_id": str(font.get("resource_id") or "6807742980271641102"),
            "font_path": str(Path(str(font["path"])).expanduser().resolve()),
            "font_title": str(font.get("name") or "SourceHanSerifCN-Heavy"),
        },
        "text_scale": float(cover_style["text_scale"]),
        "letter_spacing": 0,
        "line_spacing": 6,
        "auto_wrapping": bool(cover_style["auto_wrapping"]),
        "max_line_width": float(cover_style["max_line_width"]),
        "line_1_x": float(cover_style["line_1_x"]),
        "line_1_y": float(cover_style["line_1_y"]),
        "line_2_x": float(cover_style["line_2_x"]),
        "line_2_y": float(cover_style["line_2_y"]),
        "line_1_size": float(cover_style["line_1_size"]),
        "line_2_size": float(cover_style["line_2_size"]),
        "line_1_color": str(cover_style["line_1_color"]),
        "line_2_color": str(cover_style["line_2_color"]),
        "line_1_shadow_color": TEXT_SHADOW_COLOR,
        "line_1_shadow_alpha": float(caption_style["shadow_alpha"]),
        "line_1_shadow_smoothing": TEXT_SHADOW_SMOOTHING,
        "line_1_shadow_distance": TEXT_SHADOW_DISTANCE,
        "line_1_shadow_angle": TEXT_SHADOW_ANGLE,
        "line_2_shadow_color": TEXT_SHADOW_COLOR,
        "line_2_shadow_alpha": float(caption_style["shadow_alpha"]),
        "line_2_shadow_smoothing": TEXT_SHADOW_SMOOTHING,
        "line_2_shadow_distance": TEXT_SHADOW_DISTANCE,
        "line_2_shadow_angle": TEXT_SHADOW_ANGLE,
        "frame_scale": 1.0,
        "frame_offset_x": 0.0,
        "frame_offset_y": 0.0,
        "overlay_alpha": 0.5,
        "overlay_x_ratio": 0.5,
        "overlay_y_ratio": overlay_y_ratio,
        "overlay_width_ratio": 1.0,
        "overlay_height_ratio": overlay_height_ratio,
        "overlay_top_ratio": overlay_top,
        "overlay_bottom_ratio": overlay_bottom,
    }


def resolve_project_cover_image(item: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the portrait frozen into the current base video.

    Current selections can change after a paid composition has finished.  The
    base-video snapshot is authoritative for a cover; matching against asset
    history lets an existing video be re-exported without regenerating it.
    """

    current = item.get("inputs", {}).get("image")
    base_video = item.get("outputs", {}).get("base_video")
    frozen_sha256 = ""
    frozen_asset_id = ""
    if isinstance(base_video, dict):
        metadata = base_video.get("metadata")
        external_ref = base_video.get("external_ref")
        if isinstance(metadata, dict):
            frozen_sha256 = str(metadata.get("input_image_sha256") or "").strip().lower()
            frozen_asset_id = str(metadata.get("input_image_asset_id") or "").strip()
        if not frozen_sha256 and isinstance(external_ref, dict):
            frozen_sha256 = str(external_ref.get("image_sha256") or "").strip().lower()

    candidates: list[dict[str, Any]] = []
    if isinstance(current, dict):
        candidates.append(current)
    history = item.get("asset_history", {}).get("input_image", [])
    if isinstance(history, list):
        candidates.extend(candidate for candidate in history if isinstance(candidate, dict))

    def usable(candidate: dict[str, Any]) -> bool:
        path = str(candidate.get("managed_path") or "").strip()
        return bool(path and Path(path).expanduser().resolve().is_file())

    if frozen_sha256:
        for candidate in candidates:
            candidate_sha = str(
                (candidate.get("metadata") or {}).get("sha256")
                if isinstance(candidate.get("metadata"), dict)
                else ""
            ).strip().lower()
            if candidate_sha == frozen_sha256 and usable(candidate):
                return candidate
        row = item.get("row_key") or item.get("item_id")
        raise ValueError(
            f"任务 {row} 找不到基础视频生成时绑定的人物原图，已停止生成可能配错的封面"
        )
    if frozen_asset_id:
        for candidate in candidates:
            if str(candidate.get("asset_id") or "") == frozen_asset_id and usable(candidate):
                return candidate
    return current if isinstance(current, dict) else None


def build_top_title_texts(
    top_title: Any,
    *,
    font: dict[str, Any] | None = None,
    layout_profile_id: Any = DEFAULT_LAYOUT_PROFILE,
) -> list[dict[str, Any]]:
    """Build the fixed video banner and bottom disclaimer text layers."""

    # The model-owned two-line title is still saved for the project cover, but
    # the body video now uses one product-fixed memory hook.  Keep the argument
    # for render-job/API compatibility with already saved projects.
    _ = top_title
    profile = layout_profile(layout_profile_id)
    title_style = profile["title"]
    disclaimer_style = profile["disclaimer"]
    font_fields = {
        "font_id": str((font or {}).get("resource_id") or ""),
        "font_path": str((font or {}).get("path") or ""),
        "font_title": str((font or {}).get("name") or ""),
    }
    rows = [
        (
            FIXED_VIDEO_TITLE_TEXT,
            "顶部固定标题·世界冠军",
            title_style["transform_y"],
            title_style["font_size"],
            "#FFF589",
            "",
            0.0,
            1.0,
            title_style["clip_scale"],
            title_style["shadow_alpha"],
            950,
        ),
        (
            BOTTOM_DISCLAIMER_TEXT,
            "底部固定免责声明",
            disclaimer_style["transform_y"],
            disclaimer_style["font_size"],
            BOTTOM_DISCLAIMER_COLOR,
            "",
            0.0,
            disclaimer_style["opacity"],
            disclaimer_style["clip_scale"],
            disclaimer_style["shadow_alpha"],
            952,
        ),
    ]
    return [
        {
            "type": "add",
            "scope": "top",
            "text": text,
            "track_name": track_name,
            "start_us": 0,
            "duration_us": 0,
            "relative_index": relative_index,
            "transform_x": 0.0,
            "transform_y": transform_y,
            "scale": clip_scale,
            "size": size,
            "align": 1,
            "auto_wrapping": False,
            "line_max_width": 0.92,
            "color": color,
            "stroke_color": stroke_color,
            "stroke_width": stroke_width,
            "opacity": opacity,
            "shadow_color": "#000000" if shadow_alpha > 0 else "",
            "shadow_alpha": shadow_alpha,
            "shadow_distance": 5.0,
            "shadow_angle": -45.0,
            "shadow_smoothing": 0.45000001788139343,
            **font_fields,
        }
        for (
            text,
            track_name,
            transform_y,
            size,
            color,
            stroke_color,
            stroke_width,
            opacity,
            clip_scale,
            shadow_alpha,
            relative_index,
        ) in rows
        if text
    ]


def build_source_attribution_texts(
    overlays: Iterable[dict[str, Any]],
    *,
    font: dict[str, Any] | None = None,
    layout_profile_id: Any = DEFAULT_LAYOUT_PROFILE,
    video_duration_us: int | None = None,
) -> list[dict[str, Any]]:
    """Build timed top-right source labels for attributed semantic visuals."""

    intervals: list[tuple[int, int, str]] = []
    for overlay in overlays:
        if overlay.get("enabled") is False:
            continue
        text = str(overlay.get("attribution_text") or "").strip()
        start_us = int(overlay.get("start_us") or 0)
        duration_us = int(overlay.get("duration_us") or 0)
        if not text or start_us < 0 or duration_us <= 0:
            continue
        if video_duration_us is not None and start_us >= video_duration_us:
            continue
        end_us = start_us + duration_us
        if video_duration_us is not None:
            end_us = min(end_us, video_duration_us)
        if end_us > start_us:
            intervals.append((start_us, end_us, text))
    intervals.sort(key=lambda item: (item[0], item[1], item[2]))

    merged: list[list[Any]] = []
    for start_us, end_us, text in intervals:
        if merged and merged[-1][2] == text and start_us <= int(merged[-1][1]) + 100_000:
            merged[-1][1] = max(int(merged[-1][1]), end_us)
        else:
            merged.append([start_us, end_us, text])

    profile = layout_profile(layout_profile_id)
    style = profile["disclaimer"]
    font_fields = {
        "font_id": str((font or {}).get("resource_id") or ""),
        "font_path": str((font or {}).get("path") or ""),
        "font_title": str((font or {}).get("name") or ""),
    }
    return [
        {
            "type": "add",
            "scope": "top",
            "text": text,
            "track_name": f"右上素材来源标注·{index}",
            "start_us": int(start_us),
            "duration_us": int(end_us - start_us),
            "relative_index": 954,
            "transform_x": 0.72,
            "transform_y": 0.90,
            "scale": float(style["clip_scale"]),
            "size": float(style["font_size"]),
            "align": 2,
            "auto_wrapping": False,
            "line_max_width": 0.30,
            "color": BOTTOM_DISCLAIMER_COLOR,
            "stroke_color": "",
            "stroke_width": 0.0,
            "opacity": float(style["opacity"]),
            "shadow_color": "#000000" if float(style["shadow_alpha"]) > 0 else "",
            "shadow_alpha": float(style["shadow_alpha"]),
            "shadow_distance": 5.0,
            "shadow_angle": -45.0,
            "shadow_smoothing": 0.45000001788139343,
            **font_fields,
        }
        for index, (start_us, end_us, text) in enumerate(merged, start=1)
    ]


def bound_visual_overlays_to_video(
    overlays: Iterable[dict[str, Any]], video_duration_us: int
) -> list[dict[str, Any]]:
    """Drop fully out-of-range visuals and trim partial overlaps to the video."""

    bounded: list[dict[str, Any]] = []
    for raw in overlays:
        overlay = dict(raw)
        start_us = int(overlay.get("start_us") or 0)
        duration_us = int(overlay.get("duration_us") or 0)
        if start_us < 0 or duration_us <= 0:
            continue
        if video_duration_us > 0:
            if start_us >= video_duration_us:
                continue
            duration_us = min(duration_us, video_duration_us - start_us)
        if duration_us <= 0:
            continue
        overlay["duration_us"] = duration_us
        source_duration_us = overlay.get("source_duration_us")
        if isinstance(source_duration_us, int) and source_duration_us > duration_us:
            overlay["source_duration_us"] = duration_us
        bounded.append(overlay)
    return bounded


_PREFERRED_PHRASE_END_TERMS = (
    "世界冠军",
    "新中年女性",
    "健康体重管理",
    "核心逻辑",
    "胖肚子",
    "内脏脂肪",
    "形式",
)
_LEADING_CONNECTORS = (
    "那么",
    "但是",
    "所以",
    "然后",
    "不过",
    "因此",
    "同时",
    "另外",
    "而且",
    "其实",
    "如果",
    "因为",
    "虽然",
    "否则",
    "比如",
    "例如",
)
_NUMBER_EXPRESSION = re.compile(
    r"(?:百分之[0-9０-９零〇一二两三四五六七八九十百千万亿几多]+"
    r"|(?:大约|约|近|超过|至少|不到)?"
    r"[0-9０-９零〇一二两三四五六七八九十百千万亿几多]+"
    r"(?:[点.．][0-9０-９零〇一二两三四五六七八九十百千万亿几多]+)?"
    r"(?:(?:到|至|[-~～—–])"
    r"[0-9０-９零〇一二两三四五六七八九十百千万亿几多]+"
    r"(?:[点.．][0-9０-９零〇一二两三四五六七八九十百千万亿几多]+)?)?"
    r"(?:个)?(?:分钟|秒钟|小时|个月|公斤|千克|厘米|毫米|公里|年|月|天|日|周|岁|名|人|斤|元|次|倍|成|餐|%|％)"
    r"|[0-9０-９]+[.:：．][0-9０-９]+)"
)
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_QUANTITY_TAIL = re.compile(r"[0-9０-９零〇一二两三四五六七八九十百千万亿几多]+个$")
_COUNTING_TAIL = re.compile(
    r"[0-9０-９零〇一二两三四五六七八九十百千万亿几多]+(?:个|名|位|只|条|份|组|家|本|张|件|辆|台)$"
)
_ORDINAL_ITEM_PREFIX = re.compile(
    r"^第[0-9０-９零〇一二两三四五六七八九十百千万几多]+个"
)
_ANSWER_PROMPT_SUFFIXES = (
    "营养素",
    "保护神",
    "方式",
    "方法",
    "医生",
    "食物",
    "动作",
    "习惯",
    "运动",
    "方",
    "法",
    "菜",
)
_RELATIVE_PREFIXES = ("能", "能够", "可", "可以", "会", "要", "应该")
_BAD_LINE_ENDINGS = (
    "就",
    "都",
    "能",
    "会",
    "要",
    "把",
    "被",
    "从",
    "向",
    "往",
    "给",
    "对",
    "和",
    "与",
    "及",
    "或",
    "并",
)
_RESULT_DIRECTIONAL_COMPLEMENTS = frozenset(
    {
        "出",
        "出来",
        "出去",
        "到",
        "完",
        "好",
        "住",
        "掉",
        "成",
        "开",
        "进",
        "进去",
        "回",
        "回来",
        "上",
        "上来",
        "上去",
        "下",
        "下来",
        "下去",
        "过",
        "过来",
        "过去",
        "起",
        "起来",
        "来",
        "去",
        "懂",
        "见",
        "清",
        "对",
        "错",
        "走",
    }
)

jieba.setLogLevel(logging.ERROR)
_JIEBA_TOKENIZER = jieba.Tokenizer()
_JIEBA_POS_TOKENIZER = pseg.POSTokenizer(_JIEBA_TOKENIZER)


def _is_nominal_flag(flag: str) -> bool:
    """Treat Jieba abbreviations as nominal heads when scoring boundaries."""

    return str(flag or "").startswith("n") or str(flag or "") in {"vn", "j"}


def _discouraged_break_offsets(text: str) -> dict[int, float]:
    """Score grammatical token boundaries that are legal but usually unnatural."""

    tagged: list[tuple[str, str, int, int]] = []
    cursor = 0
    for token in _JIEBA_POS_TOKENIZER.cut(text, HMM=False):
        word = str(token.word)
        start = cursor
        end = start + len(word)
        tagged.append((word, str(token.flag), start, end))
        cursor = end
    penalties: dict[int, float] = {}
    for index, (left, right) in enumerate(zip(tagged, tagged[1:])):
        _left_word, left_flag, _left_start, boundary = left
        right_word, right_flag, _right_start, _right_end = right
        following_flag = tagged[index + 2][1] if index + 2 < len(tagged) else ""
        if _is_nominal_flag(left_flag) and _is_nominal_flag(right_flag):
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
        elif left_flag in {"a", "d"} and (
            right_flag.startswith("v") or right_flag.startswith("a")
        ):
            penalties[boundary] = max(penalties.get(boundary, 0.0), 2.0)
        elif (
            left_flag.startswith("v")
            and right_flag.startswith("a")
            and not following_flag.startswith("n")
        ):
            penalties[boundary] = max(penalties.get(boundary, 0.0), 2.0)
        elif left_flag.startswith("n") and right_flag.startswith("a"):
            penalties[boundary] = max(penalties.get(boundary, 0.0), 2.0)
        elif (
            left_flag.startswith("v")
            and right_word == "点"
            and right_flag.startswith("m")
        ):
            # Verb + quantity constructions such as `做点活动` should not leave
            # the quantifier at the start of the following subtitle.
            penalties[boundary] = max(penalties.get(boundary, 0.0), 12.0)
        elif left_flag.startswith("v") and (
            _is_nominal_flag(right_flag) or right_flag.startswith(("r", "m", "q"))
        ):
            # Prefer keeping a predicate with its object or complement. This
            # is a grammatical relationship, not a phrase dictionary:
            # 吃鸡蛋、增加五斤、带壳 all follow the same rule.
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
        elif left_flag.startswith("r") and right_flag.startswith("r"):
            # Pronoun/reflexive compounds: 你自己、我们大家。
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
        elif left_flag.startswith("p"):
            # A preposition normally belongs to the phrase it introduces.
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
        elif left_flag.startswith("c"):
            # A conjunction normally belongs to the following coordination.
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
        elif left_flag in {"d", "df", "zg"} and right_flag.startswith(
            ("a", "v", "d", "m", "q", "n", "p")
        ):
            penalties[boundary] = max(penalties.get(boundary, 0.0), 8.0)
    return penalties


def _dependency_break_offsets(text: str) -> set[int]:
    """Return boundaries inside a local POS dependency, without a term list."""

    tagged: list[tuple[str, str, int, int]] = []
    cursor = 0
    for token in _JIEBA_POS_TOKENIZER.cut(text, HMM=False):
        word = str(token.word)
        start = cursor
        end = start + len(word)
        tagged.append((word, str(token.flag), start, end))
        cursor = end

    dependencies: set[int] = set()
    for index, (left, right) in enumerate(zip(tagged, tagged[1:])):
        _left_word, left_flag, _left_start, boundary = left
        right_word, right_flag, _right_start, _right_end = right
        previous_flag = tagged[index - 1][1] if index > 0 else ""
        left_nominal = _is_nominal_flag(left_flag)
        right_nominal = _is_nominal_flag(right_flag)
        if left_flag.startswith("v") and (
            right_nominal or right_flag.startswith(("r", "m", "q"))
        ):
            dependencies.add(boundary)
        elif (
            left_flag.startswith("v")
            and right_flag.startswith("v")
            and previous_flag in {"d", "df", "zg"}
        ):
            # Degree/state predicate + complement: 很有|帮助.  A general v-v
            # boundary is not protected because serial predicates can be a
            # perfectly good split (点心|坚持).
            dependencies.add(boundary)
        elif left_nominal and right_nominal:
            dependencies.add(boundary)
        elif left_flag.startswith("r") and right_flag.startswith("r"):
            dependencies.add(boundary)
        elif _left_word in {"的", "地", "得"} and right_flag.startswith(
            ("a", "n", "r", "v")
        ):
            dependencies.add(boundary)
        elif left_flag.startswith("m") and (
            right_flag == "vn"
            or right_word in {"能", "会", "可", "可以", "能够"}
        ):
            dependencies.add(boundary)
        elif left_flag.startswith(("p", "c")):
            dependencies.add(boundary)
        elif left_flag in {"d", "df", "zg"} and right_flag.startswith(
            ("a", "v", "d", "m", "q", "n", "p")
        ):
            dependencies.add(boundary)
        elif left_flag.startswith("a") and right_nominal:
            dependencies.add(boundary)
        elif left_flag.startswith("m") and right_flag.startswith(("a", "n", "q")):
            dependencies.add(boundary)
    return dependencies


def _result_complement_break_offsets(text: str) -> set[int]:
    """Protect verb-result/directional complements as one spoken predicate.

    Jieba may tokenize ``排出`` as one word (already covered by lexical
    protection) or as ``排`` + ``出``.  The POS relationship is the stable
    signal: only a verb followed by a known result/directional complement is
    protected, so an unrelated sentence that happens to start with ``出`` is
    still free to break normally.
    """

    tagged: list[tuple[str, str, int]] = []
    cursor = 0
    for token in _JIEBA_POS_TOKENIZER.cut(text, HMM=False):
        word = str(token.word)
        cursor += len(word)
        tagged.append((word, str(token.flag), cursor))
    return {
        boundary
        for (left_word, left_flag, boundary), (right_word, _right_flag, _right_end) in zip(
            tagged, tagged[1:]
        )
        if left_word
        and left_flag.startswith("v")
        and right_word in _RESULT_DIRECTIONAL_COMPLEMENTS
    }


def _preferred_syntax_break_offsets(text: str) -> set[int]:
    """Return grammatically complete phrase boundaries worth preferring.

    These are structural break opportunities, not protected words.  In
    particular, a relative modifier followed by a numeric noun phrase should
    stay complete on the left: `蛋白质很高的|五种好食物`.
    """

    tagged: list[tuple[str, str, int, int]] = []
    cursor = 0
    for token in _JIEBA_POS_TOKENIZER.cut(text, HMM=False):
        word = str(token.word)
        start = cursor
        end = start + len(word)
        tagged.append((word, str(token.flag), start, end))
        cursor = end

    preferred: set[int] = set()
    for index, (left, right) in enumerate(zip(tagged, tagged[1:])):
        left_word, left_flag, _left_start, boundary = left
        right_word, right_flag, _right_start, right_end = right
        following_flag = tagged[index + 2][1] if index + 2 < len(tagged) else ""
        if left_word in {"的", "地", "得"} and (
            right_flag.startswith(("m", "q")) or right_flag == "j"
        ):
            preferred.add(boundary)
        elif left_word == "的" and right_flag in {"d", "df", "zg"}:
            # Completed nominalized phrase before a new adverbial predicate:
            # 你喜欢的|就OK了。
            preferred.add(boundary)
        elif left_flag.startswith(("m", "q")) and right_flag.startswith("v"):
            # Completed quantity/time phrase before a new predicate.
            preferred.add(boundary)
        elif (
            _is_nominal_flag(left_flag)
            and right_flag in {"d", "df", "zg"}
            and following_flag.startswith(("a", "n", "p", "v"))
        ):
            # Complete subject before an adverb-led predicate:
            # 八十几岁很多人|已经在坐轮椅。
            preferred.add(boundary)
        elif (
            left_flag.startswith("v")
            and right_flag.startswith("v")
            and right_end == len(text)
            and len(right_word) >= 2
        ):
            # A complete final predicate is a better short tail than splitting
            # the preceding predicate phrase.  This keeps `多上点心|坚持`
            # without depending on the semantic model to suggest that boundary.
            preferred.add(boundary)
    return preferred


def _strong_semantic_break_offsets(
    text: str,
    model_preferred_breaks: set[int],
) -> set[int]:
    """Return high-confidence prompt/answer or numbered-item boundaries.

    Ordinary model ``prefer`` boundaries remain layout hints: old analyses can
    contain useful but optional beats such as ``世界冠军|张雒``.  Only the
    structures below are strong enough to create an extra caption even when
    the complete text fits the width limit.
    """

    length = len(text)

    def usable(boundary: int) -> bool:
        return 2 <= boundary <= length - 2

    ordinal = _ORDINAL_ITEM_PREFIX.match(text)
    if ordinal is not None and length - ordinal.end() >= 5:
        return {ordinal.end()}

    # An explicit copula is the unambiguous end of the prompt.  Resolve it
    # before suffix matching so `最重要的方法是|睡眠` never becomes
    # `最重要的方法|是|睡眠`.
    for match in re.finditer("是", text):
        boundary = match.end()
        if text[:boundary].startswith("最") and usable(boundary):
            return {boundary}

    model_answers: set[int] = set()
    for boundary in model_preferred_breaks:
        left = text[:boundary]
        right = text[boundary:]
        if (
            (left.startswith("最") or "第一好" in left)
            and left.endswith(_ANSWER_PROMPT_SUFFIXES)
            and len(right) >= 2
        ):
            model_answers.add(boundary)
    if model_answers:
        return {min(model_answers)}

    # Local fallback for a missing model boundary.  This intentionally applies
    # only to superlative/list prompts and their generic category head; it does
    # not turn every noun-to-noun boundary into a forced split.
    if text.startswith("最") and "的" in text:
        local_answers: set[int] = set()
        for suffix in _ANSWER_PROMPT_SUFFIXES:
            cursor = text.find(suffix, text.find("的") + 1)
            while cursor >= 0:
                boundary = cursor + len(suffix)
                if usable(boundary):
                    local_answers.add(boundary)
                cursor = text.find(suffix, cursor + 1)
        if local_answers:
            return {min(local_answers)}

    return set()


class CaptionLayoutReviewRequired(ValueError):
    """The selected font cannot safely produce the requested one-line cues."""


@dataclass(frozen=True)
class FontMetrics:
    path: Path
    units_per_em: int
    cmap: dict[int, str]
    advances: dict[str, tuple[int, int]]

    @classmethod
    def load(cls, path: str | Path) -> "FontMetrics":
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise CaptionLayoutReviewRequired("所选字幕字体文件不存在")
        try:
            font = TTFont(str(resolved), lazy=True)
            cmap = font.getBestCmap() or {}
            advances = dict(font["hmtx"].metrics)
            units_per_em = int(font["head"].unitsPerEm)
            font.close()
        except Exception as exc:
            raise CaptionLayoutReviewRequired("无法读取所选字幕字体的真实字宽") from exc
        if not cmap or not advances or units_per_em <= 0:
            raise CaptionLayoutReviewRequired("所选字幕字体缺少可用字宽数据")
        return cls(resolved, units_per_em, cmap, advances)

    def text_width_em(self, text: str) -> float:
        width = 0.0
        missing: list[str] = []
        for character in text:
            glyph = self.cmap.get(ord(character))
            metric = self.advances.get(glyph or "")
            if metric is None:
                missing.append(character)
                continue
            width += float(metric[0]) / self.units_per_em
        if missing:
            preview = "".join(dict.fromkeys(missing))[:8]
            raise CaptionLayoutReviewRequired(
                f"所选字体缺少字幕字符：{preview}"
            )
        return width


def _normalized_caption_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _is_numeric_separator(text: str, index: int) -> bool:
    return (
        text[index] in {".", ",", ":"}
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    )


def _caption_display_text(value: str) -> tuple[str, set[int]]:
    """Remove display punctuation while retaining its preferred break positions."""

    normalized = _normalized_caption_text(value)
    display: list[str] = []
    preferred_breaks: set[int] = set()
    for index, character in enumerate(normalized):
        if (
            character in _HIDDEN_CAPTION_PUNCTUATION
            and not _is_numeric_separator(normalized, index)
        ):
            if display:
                preferred_breaks.add(len(display))
            continue
        display.append(character)
    cleaned = "".join(display).strip()
    if not cleaned:
        raise CaptionLayoutReviewRequired("字幕去除标点后内容为空")
    return cleaned, {position for position in preferred_breaks if 0 < position < len(cleaned)}


def _caption_script_offsets(value: str) -> tuple[str, list[int]]:
    """Return alignment text plus each visible character's source-script offset.

    Final captions intentionally omit source line breaks and surrounding spaces.
    Treat every whitespace character as layout-only here so a list written on
    separate source lines can still bind its visuals to the matching final cue.
    """

    source = str(value or "")
    normalized: list[str] = []
    normalized_offsets: list[int] = []
    for index, character in enumerate(source):
        if character.isspace():
            continue
        normalized.append(character)
        normalized_offsets.append(index)

    normalized_text = "".join(normalized)
    display: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(normalized_text):
        if (
            character in _HIDDEN_CAPTION_PUNCTUATION
            and not _is_numeric_separator(normalized_text, index)
        ):
            continue
        display.append(character)
        offsets.append(normalized_offsets[index])
    return "".join(display).strip(), offsets


def _uses_final_caption_anchor(overlay: dict[str, Any]) -> bool:
    candidate_id = str(overlay.get("candidate_id") or "")
    return (
        candidate_id.startswith("vc_")
        and overlay.get("manual") is not True
        and str(overlay.get("selection_mode") or "auto") != "manual"
        and str(overlay.get("timing_mode") or "sentence") == "sentence"
        and str(overlay.get("usage") or "explicit") != "seam_broll"
    )


def bind_semantic_overlays_to_render_cues(
    original_script: str,
    overlays: Iterable[dict[str, Any]],
    render_cues: Iterable[dict[str, Any]],
    candidate_request: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Start explicit automatic visuals on the final cue containing the keyword."""

    original = [dict(item) for item in overlays]
    candidates = {
        str(item.get("candidate_id") or ""): item
        for item in (candidate_request or {}).get("candidates", [])
        if isinstance(item, dict) and str(item.get("candidate_id") or "")
    }
    if not original or not candidates:
        return original

    script_display, script_offsets = _caption_script_offsets(original_script)
    if not script_display or len(script_display) != len(script_offsets):
        return original

    cue_ranges: list[tuple[int, int, int, int, str]] = []
    display_cursor = 0
    for raw in render_cues:
        if not isinstance(raw, dict):
            return original
        try:
            cue_anchor_text, _breaks = _caption_display_text(
                str(raw.get("text") or "")
            )
            cue_display = "".join(
                character
                for character in cue_anchor_text
                if not character.isspace()
            )
            cue_start_us = int(raw.get("start_us") or 0)
            cue_duration_us = int(raw.get("duration_us") or 0)
        except (CaptionLayoutReviewRequired, TypeError, ValueError):
            return original
        next_cursor = display_cursor + len(cue_display)
        if (
            cue_duration_us <= 0
            or next_cursor > len(script_display)
            or script_display[display_cursor:next_cursor] != cue_display
        ):
            return original
        cue_ranges.append(
            (
                script_offsets[display_cursor],
                script_offsets[next_cursor - 1] + 1,
                cue_start_us,
                cue_start_us + cue_duration_us,
                cue_anchor_text,
            )
        )
        display_cursor = next_cursor
    if display_cursor != len(script_display):
        return original

    bound: list[dict[str, Any]] = []
    for overlay in original:
        candidate_id = str(overlay.get("candidate_id") or "")
        if not _uses_final_caption_anchor(overlay):
            bound.append(overlay)
            continue
        candidate = candidates.get(candidate_id)
        if not isinstance(candidate, dict):
            bound.append(overlay)
            continue
        try:
            keyword_start = int(candidate.get("char_start"))
            keyword_end = int(candidate.get("char_end"))
            old_start_us = int(overlay.get("start_us") or 0)
            old_end_us = old_start_us + int(overlay.get("duration_us") or 0)
        except (TypeError, ValueError):
            bound.append(overlay)
            continue
        anchor = next(
            (
                cue
                for cue in cue_ranges
                if cue[0] < keyword_end and keyword_start < cue[1]
            ),
            None,
        )
        if anchor is None:
            bound.append(overlay)
            continue
        _cue_char_start, _cue_char_end, cue_start_us, cue_end_us, cue_text = anchor
        new_end_us = max(old_end_us, cue_end_us)
        new_duration_us = new_end_us - cue_start_us
        source_duration_us = overlay.get("source_duration_us")
        if isinstance(source_duration_us, int) and source_duration_us > 0:
            new_duration_us = min(new_duration_us, source_duration_us)
        if new_duration_us <= 0:
            bound.append(overlay)
            continue
        previous_timing_source = str(overlay.get("timing_source") or "")
        overlay.update(
            {
                "start_us": cue_start_us,
                "duration_us": new_duration_us,
                "timing_source": "final_caption_cue",
                "caption_anchor_text": cue_text,
            }
        )
        if previous_timing_source and previous_timing_source != "final_caption_cue":
            overlay["analysis_timing_source"] = previous_timing_source
        bound.append(overlay)
    return bound


def _trailing_sentence_break(text: str) -> str:
    normalized = str(text or "").rstrip()
    if not normalized:
        return ""
    if normalized[-1] in _HARD_SENTENCE_BREAKS:
        return "hard"
    if normalized[-1] in _SOFT_SENTENCE_BREAKS:
        return "soft"
    return ""


def _unbreakable_terms() -> tuple[str, ...]:
    return (*_PROTECTED_TERMS, *_LEADING_CONNECTORS)


def _unsafe_break_offsets(text: str) -> set[int]:
    """Return hard lexical boundaries that layout must never cross.

    Cross-token POS relationships are intentionally handled by
    ``_dependency_break_offsets`` as graded grammatical evidence.  Treating
    every adjacent noun token as one hard word made valid predicate/answer
    boundaries impossible (for example ``补钙方式|晒太阳``), after which the
    old all-or-nothing fallback discarded every grammatical safeguard.
    """

    unsafe_offsets: set[int] = set()
    for token, start, end in _JIEBA_TOKENIZER.tokenize(text, mode="default", HMM=False):
        if len(token) <= 1 or token.isspace():
            continue
        unsafe_offsets.update(range(int(start) + 1, int(end)))
    for term in _unbreakable_terms():
        cursor = 0
        while True:
            position = text.find(term, cursor)
            if position < 0:
                break
            unsafe_offsets.update(range(position + 1, position + len(term)))
            cursor = position + 1
    for match in _NUMBER_EXPRESSION.finditer(text):
        unsafe_offsets.update(range(match.start() + 1, match.end()))
    unsafe_offsets.update(_result_complement_break_offsets(text))
    for boundary in range(1, len(text)):
        # Structural particles must not start a rendered line. The boundary
        # value means "before text[boundary]"; protecting that exact offset
        # avoids the former off-by-one that allowed `管|得`.
        if text[boundary] in _STRUCTURAL_PARTICLES:
            unsafe_offsets.add(boundary)
        if any(text.startswith(suffix, boundary) for suffix in _BOUND_RELATIVE_SUFFIXES):
            # Category and locative relative suffixes belong with the phrase on
            # their left: 快餐|类的、疲惫生活|中的 are invalid caption starts.
            unsafe_offsets.add(boundary)
    return unsafe_offsets


def _merge_unbreakable_term_boundaries(
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Discard model boundaries that split a known lexical unit."""

    source_text = "".join(str(group.get("text") or "") for group in groups)
    unsafe_offsets = _unsafe_break_offsets(source_text)

    merged: list[dict[str, Any]] = []
    source_offset = 0
    for source in groups:
        group = dict(source)
        if merged and source_offset in unsafe_offsets:
            previous = merged.pop()
            group = {
                "text": str(previous.get("text") or "") + str(group.get("text") or ""),
                "start_us": int(previous.get("start_us") or 0),
                "end_us": int(group.get("end_us") or 0),
                "break_after": str(group.get("break_after") or "allow"),
                "hard_break_after": bool(group.get("hard_break_after")),
                "unit_count": int(previous.get("unit_count") or 1)
                + int(group.get("unit_count") or 1),
            }
        merged.append(group)
        source_offset += len(str(source.get("text") or ""))
    return merged


def _merge_orphan_sentence_tails(
    groups: list[dict[str, Any]],
    metrics: FontMetrics,
    *,
    maximum_width_em: float,
) -> list[dict[str, Any]]:
    """Repair one-character tails and very short comma-led prefixes."""

    merged: list[dict[str, Any]] = []
    for source in groups:
        group = dict(source)
        display, _preferred = _caption_display_text(str(group.get("text") or ""))
        if (
            merged
            and len(display) == 1
            and _trailing_sentence_break(str(group.get("text") or ""))
        ):
            previous = merged.pop()
            group = {
                "text": str(previous.get("text") or "") + str(group.get("text") or ""),
                "start_us": int(previous.get("start_us") or 0),
                "end_us": int(group.get("end_us") or 0),
                "break_after": str(group.get("break_after") or "allow"),
                "hard_break_after": bool(group.get("hard_break_after")),
                "unit_count": int(previous.get("unit_count") or 1)
                + int(group.get("unit_count") or 1),
            }
        merged.append(group)

    rebalanced: list[dict[str, Any]] = []
    index = 0
    while index < len(merged):
        group = merged[index]
        display, _preferred = _caption_display_text(str(group.get("text") or ""))
        if (
            len(display) < 4
            and _trailing_sentence_break(str(group.get("text") or "")) == "soft"
        ):
            previous = rebalanced[-1] if rebalanced else None
            if previous and _trailing_sentence_break(str(previous.get("text") or "")) != "hard":
                combined_display, _combined_preferred = _caption_display_text(
                    str(previous.get("text") or "") + str(group.get("text") or "")
                )
                if metrics.text_width_em(combined_display) <= maximum_width_em:
                    rebalanced.pop()
                    rebalanced.append(
                        {
                            "text": str(previous.get("text") or "")
                            + str(group.get("text") or ""),
                            "start_us": int(previous.get("start_us") or 0),
                            "end_us": int(group.get("end_us") or 0),
                            "break_after": str(group.get("break_after") or "allow"),
                            "hard_break_after": bool(group.get("hard_break_after")),
                            "unit_count": int(previous.get("unit_count") or 1)
                            + int(group.get("unit_count") or 1),
                        }
                    )
                    index += 1
                    continue
            if index + 1 >= len(merged):
                rebalanced.append(group)
                index += 1
                continue
            following = merged[index + 1]
            rebalanced.append(
                {
                    "text": str(group.get("text") or "")
                    + str(following.get("text") or ""),
                    "start_us": int(group.get("start_us") or 0),
                    "end_us": int(following.get("end_us") or 0),
                    "break_after": str(following.get("break_after") or "allow"),
                    "hard_break_after": bool(following.get("hard_break_after")),
                    "unit_count": int(group.get("unit_count") or 1)
                    + int(following.get("unit_count") or 1),
                }
            )
            index += 2
            continue
        rebalanced.append(group)
        index += 1

    attached: list[dict[str, Any]] = []
    for group in rebalanced:
        display, _preferred = _caption_display_text(str(group.get("text") or ""))
        if (
            attached
            and display.startswith(_ORPHAN_PARTICLES)
            and _trailing_sentence_break(str(attached[-1].get("text") or ""))
            != "hard"
        ):
            previous = attached.pop()
            group = {
                "text": str(previous.get("text") or "") + str(group.get("text") or ""),
                "start_us": int(previous.get("start_us") or 0),
                "end_us": int(group.get("end_us") or 0),
                "break_after": str(group.get("break_after") or "allow"),
                "hard_break_after": bool(group.get("hard_break_after")),
                "unit_count": int(previous.get("unit_count") or 1)
                + int(group.get("unit_count") or 1),
            }
        attached.append(group)
    return attached


def _split_one_line(
    text: str,
    metrics: FontMetrics,
    *,
    maximum_width_em: float,
    preferred_offsets: set[int] | None = None,
    _strict_dependencies: bool = True,
) -> list[str]:
    normalized, punctuation_breaks = _caption_display_text(text)
    model_preferred_breaks = set(preferred_offsets or set())
    if not normalized:
        raise CaptionLayoutReviewRequired("字幕内容为空")
    full_width = metrics.text_width_em(normalized)

    connector_starts: set[int] = set()
    connector_ends: set[int] = set()
    for connector in _LEADING_CONNECTORS:
        cursor = 0
        while True:
            position = normalized.find(connector, cursor)
            if position < 0:
                break
            if position > 0:
                connector_starts.add(position)
                connector_ends.add(position + len(connector))
            cursor = position + len(connector)

    protected_breaks = _unsafe_break_offsets(normalized)
    dependency_breaks = _dependency_break_offsets(normalized)
    discouraged_breaks = _discouraged_break_offsets(normalized)
    preferred_syntax_breaks = _preferred_syntax_break_offsets(normalized)
    strong_semantic_breaks = (
        _strong_semantic_break_offsets(normalized, model_preferred_breaks)
        - protected_breaks
    )
    if strong_semantic_breaks:
        chunks: list[str] = []
        start = 0
        for boundary in sorted(strong_semantic_breaks):
            part = normalized[start:boundary]
            if part:
                chunks.extend(
                    _split_one_line(
                        part,
                        metrics,
                        maximum_width_em=maximum_width_em,
                    )
                )
            start = boundary
        tail = normalized[start:]
        if tail:
            chunks.extend(
                _split_one_line(
                    tail,
                    metrics,
                    maximum_width_em=maximum_width_em,
                )
            )
        return chunks
    if full_width <= maximum_width_em:
        return [normalized]

    preferred_term_ends: set[int] = set()
    for term in _PREFERRED_PHRASE_END_TERMS:
        cursor = 0
        while True:
            position = normalized.find(term, cursor)
            if position < 0:
                break
            term_end = position + len(term)
            if term_end < len(normalized):
                preferred_term_ends.add(term_end)
            cursor = position + 1

    # Use a whole-sentence optimum so punctuation and grammar can be considered
    # together. Width is only a hard ceiling: it must never reward filling or
    # balancing lines, because that can create unnatural breaks such as
    # `很|高` merely to make two captions similar in length.
    length = len(normalized)
    widths = [0.0]
    for character in normalized:
        widths.append(widths[-1] + metrics.text_width_em(character))

    caption_counts = [length + 1] * (length + 1)
    quality_scores = [float("inf")] * (length + 1)
    previous: list[int | None] = [None] * (length + 1)
    caption_counts[0] = 0
    quality_scores[0] = 0.0
    for end in range(1, length + 1):
        for start in range(end - 1, -1, -1):
            width = widths[end] - widths[start]
            if width > maximum_width_em:
                break
            if quality_scores[start] == float("inf"):
                continue
            chunk = normalized[start:end].strip()
            if not chunk:
                continue
            # The tokenizer only sees punctuation-free display text.  It can
            # therefore mistake two words separated by an original comma or
            # enumeration comma for one protected phrase (for example
            # ``嘴馋、减不动`` -> ``嘴馋减不动``).  An original punctuation or
            # punctuation boundary is stronger evidence and must remain a
            # legal line break. A model suggestion is only a soft preference;
            # it must never override a local lexical/grammatical dependency.
            if (
                end < length
                and end in protected_breaks
                and end not in punctuation_breaks
            ):
                continue
            dependency_violation = bool(
                end < length
                and end in dependency_breaks
                and end not in punctuation_breaks
            )
            if (
                _strict_dependencies
                and dependency_violation
            ):
                continue
            core_length = len(chunk)
            if length > 1 and core_length == 1:
                # A one-character fragment is never a useful reflow result.
                # Independent one-character source clauses bypass this split
                # path because their whole text already fits above.
                continue
            # Minimize the necessary caption count first. For plans with that
            # same count, compare only semantic and grammatical break quality;
            # unused width is deliberately absent from both values.
            candidate_count = caption_counts[start] + 1
            score = quality_scores[start]
            if core_length == 2:
                score += 1.0
            elif core_length == 3:
                score += 0.25
            if end < length:
                score += 0.25
                if dependency_violation:
                    # If every strict grammatical plan is over-wide, relax only
                    # the necessary relationship and retain its full cost.  The
                    # former retry discarded all dependency information at once.
                    score += 8.0
                if end in model_preferred_breaks:
                    score -= 2.5
                elif end in punctuation_breaks:
                    score -= 2.5
                if end in preferred_syntax_breaks:
                    score -= 2.0
                score += discouraged_breaks.get(end, 0.0)
                if end in connector_starts:
                    score -= 0.55
                if end in preferred_term_ends:
                    score -= 1.25
                if end in connector_ends or chunk.endswith(_LEADING_CONNECTORS):
                    score += 4.0
                if chunk.endswith(_ORPHAN_PARTICLES):
                    score += 1.5
                if chunk.endswith(_BAD_LINE_ENDINGS):
                    score += 12.0
                next_text = normalized[end:]
                if (
                    _QUANTITY_TAIL.search(chunk)
                    and next_text.startswith(_RELATIVE_PREFIXES)
                ):
                    score += 8.0
                if _COUNTING_TAIL.search(chunk) and next_text:
                    score += 4.0
            if start > 0 and chunk.startswith(_ORPHAN_PARTICLES):
                score += 10.0
            # Once hard semantic beats have been handled above, keep the
            # necessary caption count minimal and use grammar only to choose
            # among plans with that count.  This prevents optional model beats
            # from producing fragments such as `第一|脂肪...`.
            if (candidate_count, score) < (
                caption_counts[end],
                quality_scores[end],
            ):
                caption_counts[end] = candidate_count
                quality_scores[end] = score
                previous[end] = start

    if previous[length] is None:
        if _strict_dependencies:
            return _split_one_line(
                text,
                metrics,
                maximum_width_em=maximum_width_em,
                preferred_offsets=preferred_offsets,
                _strict_dependencies=False,
            )
        raise CaptionLayoutReviewRequired("字幕无法可靠拆成单行")
    chunks: list[str] = []
    cursor = length
    while cursor > 0:
        start = previous[cursor]
        if start is None:
            raise CaptionLayoutReviewRequired("字幕无法可靠拆成单行")
        chunks.append(normalized[start:cursor].strip())
        cursor = start
    chunks.reverse()
    return chunks


def _allocate_cue_chunks(
    cue: CaptionCue,
    chunks: list[str],
    metrics: FontMetrics,
) -> list[CaptionCue]:
    if len(chunks) == 1:
        return [CaptionCue(cue.start_us, cue.duration_us, chunks[0])]
    if cue.duration_us < len(chunks) * CAPTION_MIN_SLICE_US:
        raise CaptionLayoutReviewRequired("字幕时间过短，无法安全拆成多条单行字幕")
    weights = [max(metrics.text_width_em(chunk), 0.01) for chunk in chunks]
    total = sum(weights)
    result: list[CaptionCue] = []
    cursor = cue.start_us
    allocated = 0
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        if index == len(chunks) - 1:
            duration = cue.end_us - cursor
        else:
            next_allocated = round(cue.duration_us * sum(weights[: index + 1]) / total)
            duration = next_allocated - allocated
            allocated = next_allocated
        if duration < CAPTION_MIN_SLICE_US:
            raise CaptionLayoutReviewRequired("字幕拆分后的显示时间过短")
        result.append(CaptionCue(cursor, duration, chunk))
        cursor += duration
    return result


def layout_one_line_captions(
    raw_cues: Iterable[object],
    *,
    font_path: str | Path,
    font_size: float = CAPTION_REFERENCE_FONT_SIZE,
    max_width_ratio: float = CAPTION_MAX_WIDTH_RATIO,
) -> list[dict[str, int | str]]:
    """Derive one-line render cues while preserving every provider cue range."""

    safe_size = float(font_size)
    safe_ratio = float(max_width_ratio)
    if safe_size <= 0:
        raise CaptionLayoutReviewRequired("字幕字号必须大于 0")
    if not 0.2 <= safe_ratio <= CAPTION_MAX_WIDTH_RATIO:
        raise CaptionLayoutReviewRequired("字幕宽度必须在画面宽度 20%–80% 之间")
    metrics = FontMetrics.load(font_path)
    maximum_width_em = (
        CAPTION_REFERENCE_MAX_EM
        * (safe_ratio / CAPTION_MAX_WIDTH_RATIO)
        * (CAPTION_REFERENCE_FONT_SIZE / safe_size)
    )
    result: list[CaptionCue] = []
    for cue in caption_cues_from_payload(raw_cues):
        chunks = _split_one_line(
            cue.text,
            metrics,
            maximum_width_em=maximum_width_em,
        )
        result.extend(_allocate_cue_chunks(cue, chunks, metrics))
    if not result:
        raise CaptionLayoutReviewRequired("当前音频没有可用的 MiniMax 字幕时间轴")
    return [cue.as_dict() for cue in result]


def _layout_semantic_groups(
    groups: list[dict[str, Any]],
    metrics: FontMetrics,
    *,
    maximum_width_em: float,
    preserve_preferred_boundaries: bool = False,
) -> list[dict[str, int | str]]:
    # Model boundaries are preferences, not trusted indivisible chunks.  Build
    # punctuation/raw-cue-bounded clauses first, then lay each clause out over
    # tokenizer-approved character boundaries.  This prevents a local repair
    # from crossing `。` or a MiniMax pause and also lets us override a bad model
    # split such as `头晕眼|花`.
    # The model is allowed to suggest boundaries, but it cannot split a lexical
    # or numeric expression.  Repair those boundaries before punctuation is
    # interpreted: otherwise a model result such as `0.|5到1公斤` makes the
    # decimal point look like sentence punctuation and silently removes it.
    groups = _merge_unbreakable_term_boundaries(groups)
    clauses: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for source in groups:
        group = dict(source)
        if current and int(group.get("start_us") or 0) > int(
            current[-1].get("end_us") or 0
        ):
            clauses.append(current)
            current = []
        current.append(group)
        group_text = str(group.get("text") or "").rstrip()
        if group.get("hard_break_after") or (
            group_text and group_text[-1] in _CLAUSE_BREAKS
        ) or (
            preserve_preferred_boundaries
            and str(group.get("break_after") or "allow") == "prefer"
        ):
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)

    # Ordinal/discourse markers such as `第一，` bind to what follows.  Other
    # punctuation remains a hard layout boundary because punctuation is hidden
    # in rendered captions and merging it would create text such as `花掉的`.
    joined_clauses: list[list[dict[str, Any]]] = []
    if preserve_preferred_boundaries:
        joined_clauses = clauses
    else:
        index = 0
        while index < len(clauses):
            clause = clauses[index]
            clause_text = "".join(str(group.get("text") or "") for group in clause)
            display, _preferred = _caption_display_text(clause_text)
            if (
                display in _RIGHT_BINDING_CLAUSES
                and index + 1 < len(clauses)
                and int(clauses[index + 1][0].get("start_us") or 0)
                == int(clause[-1].get("end_us") or 0)
            ):
                joined_clauses.append(clause + clauses[index + 1])
                index += 2
                continue
            joined_clauses.append(clause)
            index += 1

    result: list[CaptionCue] = []
    for clause in joined_clauses:
        text = "".join(str(group.get("text") or "") for group in clause)
        display, _punctuation_breaks = _caption_display_text(text)
        preferred_offsets: set[int] = set()
        display_cursor = 0
        for group in clause[:-1]:
            part, _part_breaks = _caption_display_text(str(group.get("text") or ""))
            display_cursor += len(part)
            if (
                str(group.get("break_after") or "allow") == "prefer"
                and part not in _RIGHT_BINDING_CLAUSES
            ):
                preferred_offsets.add(display_cursor)
        start_us = int(clause[0].get("start_us") or 0)
        end_us = int(clause[-1].get("end_us") or 0)
        if end_us <= start_us:
            raise SemanticSubtitleMappingError(
                "SEMANTIC_TIME_INVALID", "语义字幕的派生时间范围无效"
            )
        try:
            chunks = _split_one_line(
                text,
                metrics,
                maximum_width_em=maximum_width_em,
                preferred_offsets=preferred_offsets,
            )
            result.extend(
                _allocate_cue_chunks(
                    CaptionCue(start_us, end_us - start_us, display),
                    chunks,
                    metrics,
                )
            )
        except CaptionLayoutReviewRequired as exc:
            raise SemanticSubtitleMappingError(
                "SEMANTIC_LAYOUT_FAILED",
                "语义字幕无法在当前真实字宽内排成单行",
            ) from exc
    return [cue.as_dict() for cue in result]


def _fallback_mapping(
    item: dict[str, Any],
    *,
    code: str,
    summary: str,
) -> dict[str, Any]:
    analysis = dict(item.get("content_analysis") or {})
    audio = item.get("outputs", {}).get("audio")
    return {
        "schema": SEMANTIC_MAPPING_SCHEMA,
        "status": "FALLBACK",
        "reason_code": str(code or "SEMANTIC_MAPPING_UNAVAILABLE")[:100],
        "reason_summary": str(summary or "使用 MiniMax 原始字幕排版")[:500],
        "script_sha256": hashlib.sha256(str(item.get("script_text") or "").encode("utf-8")).hexdigest(),
        "analysis_script_sha256": analysis.get("script_sha256"),
        "analysis_schema_version": analysis.get("schema_version"),
        "analysis_prompt_version": analysis.get("prompt_version"),
        "analysis_subtitle_prompt_version": analysis.get(
            "subtitle_prompt_version"
        ),
        "analysis_subtitle_sha256": subtitle_analysis_sha256(analysis),
        "audio_asset_id": audio.get("asset_id") if isinstance(audio, dict) else None,
        "audio_version": audio.get("version") if isinstance(audio, dict) else None,
        "mapped_unit_count": 0,
    }


def _uses_validated_subtitle_boundaries(analysis: dict[str, Any]) -> bool:
    """Only v20+ subtitle contracts may turn provider preferences into hard beats."""

    subtitle_version = str(analysis.get("subtitle_prompt_version") or "")
    subtitle_match = re.fullmatch(
        r"jyd\.subtitle-analysis\.prompt\.v(\d+)", subtitle_version
    )
    if subtitle_match:
        return int(subtitle_match.group(1)) >= 20
    prompt_version = str(analysis.get("prompt_version") or "")
    match = re.fullmatch(r"jyd\.content-analysis\.prompt\.v(\d+)", prompt_version)
    return bool(match and int(match.group(1)) >= 20)


def derive_project_render_cues(
    item: dict[str, Any],
    *,
    font_path: str | Path,
    font_size: float = CAPTION_REFERENCE_FONT_SIZE,
    max_width_ratio: float = CAPTION_MAX_WIDTH_RATIO,
    asr_alignment: dict[str, Any] | None = None,
    require_precise_alignment: bool = False,
) -> tuple[list[dict[str, int | str]], dict[str, Any]]:
    """Use semantic units only when script, analysis and current audio versions agree."""

    raw_cues = item.get("subtitles", {}).get("raw_cues", [])
    script = str(item.get("script_text") or "")
    script_hash = hashlib.sha256(script.encode("utf-8")).hexdigest()
    analysis = dict(item.get("content_analysis") or {})
    audio = item.get("outputs", {}).get("audio")
    subtitles = dict(item.get("subtitles") or {})

    fallback = _fallback_mapping(
        item,
        code="SUBTITLE_ANALYSIS_UNAVAILABLE",
        summary="字幕分析未成功，使用 MiniMax raw_cues 和现有排版",
    )
    semantic_units: list[object] | None = None
    if analysis.get("subtitle_analysis_status") == "SUCCESS":
        if analysis.get("script_sha256") != script_hash:
            fallback = _fallback_mapping(
                item,
                code="ANALYSIS_SCRIPT_VERSION_MISMATCH",
                summary="字幕分析结果不属于当前脚本版本",
            )
        elif not isinstance(audio, dict):
            fallback = _fallback_mapping(
                item,
                code="CURRENT_AUDIO_MISSING",
                summary="当前脚本没有可验证的音频版本",
            )
        elif subtitles.get("bound_audio_asset_id") != audio.get("asset_id"):
            fallback = _fallback_mapping(
                item,
                code="RAW_CUES_AUDIO_VERSION_MISMATCH",
                summary="MiniMax raw_cues 没有绑定当前音频版本",
            )
        elif audio.get("metadata", {}).get("script_sha256") != script_hash:
            fallback = _fallback_mapping(
                item,
                code="AUDIO_SCRIPT_VERSION_MISMATCH",
                summary="当前音频没有绑定当前脚本版本",
            )
        elif not isinstance(analysis.get("subtitle_units"), list):
            fallback = _fallback_mapping(
                item,
                code="SUBTITLE_UNITS_MISSING",
                summary="字幕分析成功状态缺少 subtitle_units",
            )
        else:
            semantic_units = list(analysis["subtitle_units"])

    if semantic_units is not None:
        try:
            metrics = FontMetrics.load(font_path)
            maximum_width_em = (
                CAPTION_REFERENCE_MAX_EM
                * (float(max_width_ratio) / CAPTION_MAX_WIDTH_RATIO)
                * (CAPTION_REFERENCE_FONT_SIZE / float(font_size))
            )
            timed_units = map_subtitle_units_to_raw_cues(
                script,
                semantic_units,
                raw_cues,
            )
            groups = semantic_break_groups(timed_units)
            render_cues = _layout_semantic_groups(
                groups,
                metrics,
                maximum_width_em=maximum_width_em,
                preserve_preferred_boundaries=_uses_validated_subtitle_boundaries(
                    analysis
                ),
            )
            if asr_alignment is not None:
                render_cues = retime_render_cues(
                    script, raw_cues, render_cues, asr_alignment
                )
            elif require_precise_alignment:
                raise CaptionAlignmentError(
                    "ASR_ALIGNMENT_REQUIRED", "当前音频尚未完成 ASR 精确字幕校准"
                )
            return render_cues, {
                "schema": SEMANTIC_MAPPING_SCHEMA,
                "status": "SUCCESS",
                "reason_code": None,
                "reason_summary": None,
                "script_sha256": script_hash,
                "analysis_script_sha256": analysis.get("script_sha256"),
                "analysis_schema_version": analysis.get("schema_version"),
                "analysis_prompt_version": analysis.get("prompt_version"),
                "analysis_subtitle_prompt_version": analysis.get(
                    "subtitle_prompt_version"
                ),
                "analysis_subtitle_sha256": subtitle_analysis_sha256(analysis),
                "audio_asset_id": audio.get("asset_id"),
                "audio_version": audio.get("version"),
                "mapped_unit_count": len(timed_units),
                "timing_source": (
                    "funasr_word_timestamps"
                    if asr_alignment is not None
                    else "minimax_raw_cue_interpolation"
                ),
            }
        except (SemanticSubtitleMappingError, CaptionLayoutReviewRequired) as exc:
            fallback = _fallback_mapping(
                item,
                code=getattr(exc, "code", "SEMANTIC_LAYOUT_UNSAFE"),
                summary=str(exc),
            )

    render_cues = layout_one_line_captions(
        raw_cues,
        font_path=font_path,
        font_size=font_size,
        max_width_ratio=max_width_ratio,
    )
    if asr_alignment is not None:
        render_cues = retime_render_cues(script, raw_cues, render_cues, asr_alignment)
        fallback["timing_source"] = "funasr_word_timestamps"
    elif require_precise_alignment:
        raise CaptionAlignmentError(
            "ASR_ALIGNMENT_REQUIRED", "当前音频尚未完成 ASR 精确字幕校准"
        )
    else:
        fallback["timing_source"] = "minimax_raw_cue_interpolation"
    return render_cues, fallback


def _active_postprocess_operations(
    project: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    active: list[dict[str, Any]] = []
    latest_by_item: dict[str, str] = {}
    for operation in project.get("operations", []):
        if (
            operation.get("operation_type")
            in {"POSTPROCESS_GENERATE", "POSTPROCESS_EXPORT"}
            and operation.get("item_id")
        ):
            item_id = str(operation["item_id"])
            latest_by_item[item_id] = str(operation.get("operation_id") or "")
            if operation.get("status") in {"PENDING", "RUNNING"}:
                active.append(operation)
    return active, latest_by_item


def _postprocess_target_items(
    project: dict[str, Any], requested_item_ids: set[str]
) -> list[dict[str, Any]]:
    return [
        item
        for item in project.get("items", [])
        if item.get("outputs", {}).get("composition_video") is None
        and (
            not requested_item_ids
            or str(item.get("item_id") or "") in requested_item_ids
        )
    ]


class ProjectPostprocessCoordinator:
    """Build browser preview recipes and export them only on explicit request."""

    def __init__(
        self,
        store: ProjectStore,
        render_queue: Any,
        *,
        storage_root: Path,
        draft_root: Path,
        fonts: list[dict[str, Any]],
        bgm_assets: list[dict[str, Any]],
        music_matcher: MusicProfileMatcher,
        caption_aligner: Any | None = None,
        require_precise_alignment: bool = False,
        semantic_visual_library_root: Path | None = None,
        semantic_visual_catalog: SemanticVisualCatalog | None = None,
        stall_timeout_seconds: float = POSTPROCESS_STALL_TIMEOUT_SECONDS,
        no_agent_grace_seconds: float = POSTPROCESS_NO_AGENT_GRACE_SECONDS,
    ) -> None:
        self.store = store
        self.render_queue = render_queue
        self.storage_root = Path(storage_root).resolve()
        self.draft_root = Path(draft_root).resolve()
        self.fonts = {
            str(item.get("identity") or ""): item
            for item in fonts
            if item.get("identity") and item.get("available") and item.get("path")
        }
        self.bgm_assets = {
            str(item.get("identity") or ""): item
            for item in bgm_assets
            if item.get("identity") and item.get("available", True)
        }
        self.music_profiles = {
            str(profile.get("identity") or ""): profile
            for profile in music_matcher.snapshot().get("profiles", [])
            if profile.get("identity")
        }
        self.music_selector = ProjectMusicSelector(music_matcher, self.bgm_assets)
        self.caption_aligner = caption_aligner
        self.require_precise_alignment = bool(require_precise_alignment)
        self.stall_timeout_seconds = max(1.0, float(stall_timeout_seconds))
        self.no_agent_grace_seconds = max(0.0, float(no_agent_grace_seconds))
        self.semantic_visual_library_root = Path(
            semantic_visual_library_root
            or (
                Path(__file__).resolve().parents[2]
                / "data"
                / "libraries"
                / "semantic_visual_library"
            )
        ).resolve()
        self.semantic_visual_catalog = semantic_visual_catalog
        if self.semantic_visual_catalog is None:
            try:
                self.semantic_visual_catalog = load_semantic_visual_catalog(
                    self.semantic_visual_library_root
                )
            except SemanticVisualCatalogError:
                self.semantic_visual_catalog = None

    @staticmethod
    def _timestamp_age_seconds(*values: object) -> float | None:
        now = datetime.now(timezone.utc)
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
        return None

    def _active_render_stall(
        self,
        operation: dict[str, Any],
        status: dict[str, Any],
    ) -> tuple[str, str] | None:
        age = self._timestamp_age_seconds(
            status.get("heartbeat_at"),
            status.get("started_at"),
            status.get("queued_at"),
            status.get("created_at"),
            operation.get("started_at"),
            operation.get("created_at"),
        )
        execution_mode = str(
            getattr(self.render_queue, "execution_mode", "") or ""
        ).lower()
        if execution_mode == "agent" and age is not None:
            list_agents = getattr(self.render_queue, "list_agents", None)
            try:
                agents = list_agents() if callable(list_agents) else []
            except Exception:
                agents = None
            online = (
                [value for value in agents if value.get("status") != "offline"]
                if agents is not None
                else None
            )
            if online == [] and age >= self.no_agent_grace_seconds:
                return (
                    "JY_RENDER_AGENT_OFFLINE",
                    "剪映处理机未连接，任务已结束等待；启动处理机后可点击重试失败阶段",
                )
        if age is not None and age >= self.stall_timeout_seconds:
            return (
                "JY_RENDER_TIMEOUT",
                "剪映后处理任务超过 30 分钟没有完成，已停止无限等待；可点击重试失败阶段",
            )
        return None

    def _automatic_bgm_mix(
        self,
        item: dict[str, Any],
        bgm_identity: str,
    ) -> dict[str, Any]:
        if not bgm_identity:
            return {}
        profile = self.music_profiles.get(bgm_identity) or {}
        strong_vocals = "strong_vocals" in set(profile.get("traits") or [])
        audio = item.get("outputs", {}).get("audio")
        bgm = self.bgm_assets.get(bgm_identity)
        voice_path = str(audio.get("managed_path") or "") if isinstance(audio, dict) else ""
        bgm_path = str(bgm.get("absolute_path") or "") if isinstance(bgm, dict) else ""
        if not voice_path or not bgm_path:
            return {
                "algorithm": "speech-relative-lufs.v1",
                "volume": fallback_bgm_volume(strong_vocals=strong_vocals),
                "target_gap_db": BGM_TARGET_GAP_DB
                + (BGM_STRONG_VOCAL_EXTRA_GAP_DB if strong_vocals else 0.0),
                "strong_vocals": strong_vocals,
                "fallback": True,
                "reason": "人声或 BGM 文件路径不可用",
            }
        return automatic_bgm_mix(
            voice_path,
            bgm_path,
            strong_vocals=strong_vocals,
        )

    def _build_draft_job(
        self,
        item: dict[str, Any],
        *,
        draft_name: str,
        output_mp4: Path | None = None,
        skip_export: bool,
    ) -> dict[str, Any]:
        subtitles = dict(item.get("subtitles") or {})
        if subtitles.get("status") != "PREVIEW_READY" or not subtitles.get(
            "render_cues"
        ):
            raise ValueError("请先生成浏览器字幕与 BGM 预览")
        style = dict(subtitles.get("style") or {})
        settings = dict(item.get("settings", {}).get("postprocess") or {})
        profile_id = normalize_layout_profile(
            settings.get("layout_profile") or DEFAULT_LAYOUT_PROFILE
        )
        profile = layout_profile(profile_id)
        caption_profile = profile["caption"]
        font = layout_font(
            self.fonts,
            str(style.get("font_id") or settings.get("font_identity") or ""),
        )
        bgm_identity = str(settings.get("bgm_identity") or "")
        if bgm_identity and bgm_identity not in self.bgm_assets:
            raise ValueError("浏览器预览绑定的 BGM 不可用")
        bgm_media_path = ""
        if bgm_identity:
            bgm_asset = self.bgm_assets[bgm_identity]
            bgm_media_path = str(
                bgm_asset.get("absolute_path") or bgm_asset.get("path") or ""
            ).strip()
            if not bgm_media_path or not Path(bgm_media_path).expanduser().is_file():
                raise ValueError("浏览器预览绑定的 BGM 文件不存在")
            bgm_media_path = str(Path(bgm_media_path).expanduser().resolve())
        output: dict[str, Any] = {
            "draft_root": str(self.draft_root),
            "draft_name": draft_name,
            "skip_export": skip_export,
        }
        if output_mp4 is not None:
            output["mp4_path"] = str(output_mp4)
        source = build_project_video_source(item)
        source["fade_out_us"] = VIDEO_FADE_OUT_US
        job: dict[str, Any] = {
            "schema": "jyd.render_job.v1",
            "source": source,
            "original_video_volume": 0.0,
            "output": output,
            "captions": {
                "cues": subtitles["render_cues"],
                "track_name": "MiniMax 单行字幕",
                "size": float(caption_profile["font_size"]),
                "clip_scale": float(caption_profile["clip_scale"]),
                "color": "#FFFFFF",
                "stroke_color": "",
                "stroke_width": 0.0,
                "shadow_color": TEXT_SHADOW_COLOR,
                "shadow_alpha": float(caption_profile["shadow_alpha"]),
                "shadow_distance": TEXT_SHADOW_DISTANCE,
                "shadow_angle": TEXT_SHADOW_ANGLE,
                "shadow_smoothing": TEXT_SHADOW_SMOOTHING,
                "transform_x": 0.0,
                "transform_y": float(caption_profile["transform_y"]),
                "line_max_width": float(caption_profile["max_width_ratio"]),
                "max_lines": 1,
                "single_line": True,
                "font_id": str(font.get("resource_id") or ""),
                "font_path": str(font["path"]),
                "font_title": str(font.get("name") or ""),
            },
            "audios": [
                build_project_speech_audio(item),
                *(
                    [
                        {
                            "type": "bgm",
                            "library_identity": bgm_identity,
                            "media_path": bgm_media_path,
                            "target_start_us": 0,
                            "target_duration_us": 0,
                            "fit_to_video": True,
                            "align_to_end": True,
                            "crossfade_us": BGM_CROSSFADE_US,
                            "fade_in_us": BGM_FADE_IN_US,
                            "volume": float(
                                settings.get("bgm_volume") or BGM_FALLBACK_VOLUME
                            ),
                        }
                    ]
                    if bgm_identity
                    else []
                ),
            ],
            "export": {"resolution": "1080P", "framerate": "30fps"},
        }
        video_duration_us = item_video_duration_us(item)
        template = settings.get("jianying_template")
        if isinstance(template, dict) and template.get("template_id"):
            if video_duration_us <= 0:
                raise ValueError("当前视频时长不可用，无法套用剪映模板")
            draft_dir = Path(str(template.get("draft_dir") or "")).expanduser()
            if not (draft_dir / "draft_content.json").is_file():
                raise ValueError("账号绑定的剪映模板草稿不存在，请重新上传")
            profile_data = dict(template.get("profile") or {})
            main_video = dict(profile_data.get("main_video") or {})
            caption_track = dict(profile_data.get("caption_track") or {})
            base_video = dict(item.get("outputs", {}).get("base_video") or {})
            base_video_path = str(base_video.get("managed_path") or "").strip()
            if not base_video_path or not Path(base_video_path).is_file():
                raise ValueError("当前合并视频不存在，无法套用剪映模板")
            job["source"] = {
                "type": "template",
                "template_draft_dir": str(draft_dir.resolve()),
            }
            job["remove_existing_audio"] = True
            job["timeline_duration_us"] = video_duration_us
            if main_video:
                job["video_replacements"] = [
                    {
                        "type": "video-segment",
                        "track_index": int(main_video.get("typed_track_index", 0)),
                        "segment_index": int(main_video.get("segment_index", 0)),
                        "media_path": str(Path(base_video_path).resolve()),
                        "source_start_us": 0,
                        "source_duration_us": video_duration_us,
                        "target_start_us": 0,
                        "target_duration_us": video_duration_us,
                    }
                ]
            else:
                # A user template is also valid when it only contains captions,
                # flower text, stickers or effects.  Add the current project
                # video as a required native video track instead of forcing the
                # uploaded draft to carry a disposable placeholder clip.
                job["visual_overlays"] = [
                    {
                        "enabled": True,
                        "media_type": "video",
                        "video_path": str(Path(base_video_path).resolve()),
                        "start_us": 0,
                        "duration_us": video_duration_us,
                        "source_start_us": 0,
                        "source_duration_us": video_duration_us,
                        "mute": True,
                        "fit": "cover",
                        "corner": "center",
                        "scale": 1.0,
                        "opacity": 1.0,
                        "track_name": "项目主视频",
                        "optional": False,
                        "render_below_text": True,
                        "layer_order": -100,
                    }
                ]
            replace_end_us = max(
                video_duration_us,
                int(profile_data.get("draft_duration_us", 0) or 0),
            )
            job["subtitle_range_replacements"] = [
                {
                    "track_index": int(caption_track.get("typed_track_index", 0)),
                    "base_segment_index": int(caption_track.get("base_segment_index", 0)),
                    "start_us": 0,
                    "end_us": replace_end_us,
                    "subtitles": [dict(cue) for cue in subtitles["render_cues"]],
                }
            ]
            job.pop("captions", None)
            return job
        visual_analysis = dict(item.get("visual_analysis") or {})
        visual_overlays = bound_visual_overlays_to_video(
            apply_layout_to_visual_overlays(
                bind_semantic_overlays_to_render_cues(
                    str(item.get("script_text") or ""),
                    frozen_visual_overlays(
                        item,
                        library_root=self.semantic_visual_library_root,
                        catalog=self.semantic_visual_catalog,
                    ),
                    subtitles["render_cues"],
                    (
                        dict(visual_analysis.get("candidate_request") or {})
                        if isinstance(visual_analysis.get("candidate_request"), dict)
                        else None
                    ),
                ),
                profile_id,
            ),
            video_duration_us,
        )
        job["visual_overlays"] = visual_overlays
        job["fixed_overlays"] = [
            fixed_nameplate_overlay(self.semantic_visual_library_root, profile_id)
        ]
        title_texts = [
            *build_top_title_texts(
                settings.get("top_title"), font=font, layout_profile_id=profile_id
            ),
            *nameplate_texts(profile_id, font=font),
            *build_source_attribution_texts(
                visual_overlays,
                font=font,
                layout_profile_id=profile_id,
                video_duration_us=video_duration_us or None,
            ),
        ]
        if title_texts:
            job["texts"] = title_texts
        cover = build_project_cover(item, fonts=self.fonts)
        if cover is not None:
            job["cover"] = cover
        return job

    def start(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        idempotency_key: str,
        item_settings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("字幕与背景音乐预览请求缺少幂等键")
        repeated_operations = [
            operation
            for operation in project.get("operations", [])
            if operation.get("operation_type") == "POSTPROCESS_GENERATE"
            and operation.get("idempotency_key") == clean_key
        ]
        if repeated_operations and all(
            operation.get("status") == "SUCCEEDED"
            or operation.get("result", {}).get("job_id")
            for operation in repeated_operations
        ):
            return self.sync(owner_user_id, project_id)
        if any(item["status"] == "POSTPROCESS_RUNNING" for item in project["items"]):
            raise ValueError("当前视频正在按需导出，请勿重复提交")
        supplied = {
            str(item.get("item_id") or ""): item
            for item in item_settings
            if isinstance(item, dict) and item.get("item_id")
        }
        if not supplied:
            raise ValueError("字幕与背景音乐预览至少需要指定一条脚本行")
        project_items = {str(item["item_id"]): item for item in project["items"]}
        if not set(supplied).issubset(project_items):
            raise KeyError("项目脚本行不存在")
        blocked = [
            project_items[item_id]
            for item_id in supplied
            if not (
                project_items[item_id].get("allowed_actions", {}).get("start_postprocess")
                or project_items[item_id].get("allowed_actions", {}).get("retry_postprocess")
            )
        ]
        if blocked:
            raise ValueError(f"任务 {blocked[0]['row_key']} 尚未准备好生成字幕与 BGM 成片")
        subtitle_updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        resolved_settings: dict[str, dict[str, Any]] = {}

        requested_item_ids = set(supplied)
        target_items = _postprocess_target_items(project, requested_item_ids)
        if not target_items:
            raise ValueError("当前项目没有需要生成的完整预览")

        automatic_target_ids = {
            str(item["item_id"])
            for item in target_items
            if str(supplied.get(str(item["item_id"]), {}).get("bgm_selection_mode") or "manual")
            .strip()
            .lower()
            == "auto"
        }
        recent_identity_counts = automatic_music_identity_counts(
            project, excluded_item_ids=automatic_target_ids
        )

        for item in target_items:
            config = supplied.get(str(item["item_id"]), {})
            base_video = item.get("outputs", {}).get("base_video")
            if not isinstance(base_video, dict) or not base_video.get("managed_path"):
                raise ValueError(f"任务 {item['row_key']} 缺少基础视频")
            font_identity = str(config.get("font_identity") or "").strip()
            font = self.fonts.get(font_identity)
            if font is None:
                raise ValueError(f"任务 {item['row_key']} 选择的字幕字体不可用")
            bgm_mode = str(config.get("bgm_selection_mode") or "manual").strip().lower()
            if bgm_mode not in {"auto", "manual"}:
                raise ValueError(f"任务 {item['row_key']} 的 BGM 选择模式不合法")
            if bgm_mode == "auto":
                bgm_identity, music_selection = self.music_selector.resolve_auto(
                    project,
                    item,
                    recent_identity_counts=recent_identity_counts,
                )
                if bgm_identity:
                    recent_identity_counts[bgm_identity] = (
                        recent_identity_counts.get(bgm_identity, 0) + 1
                    )
            else:
                bgm_identity = str(config.get("bgm_identity") or "").strip()
                music_selection = manual_music_selection(item, bgm_identity)
            if bgm_identity and bgm_identity not in self.bgm_assets:
                raise ValueError(f"任务 {item['row_key']} 选择的 BGM 不可用")
            bgm_mix = self._automatic_bgm_mix(item, bgm_identity)
            saved_postprocess = dict(item.get("settings", {}).get("postprocess") or {})
            profile_id = normalize_layout_profile(
                config.get("layout_profile")
                or saved_postprocess.get("layout_profile")
                or DEFAULT_LAYOUT_PROFILE
            )
            profile = layout_profile(profile_id)
            caption_style = profile["caption"]
            font = layout_font(self.fonts, font_identity)
            font_identity = str(font.get("identity") or font_identity)
            top_title = normalize_top_title(
                config["top_title"]
                if "top_title" in config
                else saved_postprocess.get("top_title")
            )
            cover_title = normalize_cover_title(
                config["cover_title"]
                if "cover_title" in config
                else saved_postprocess.get("cover_title")
            )
            resolved_settings[str(item["item_id"])] = {
                **config,
                "bgm_identity": bgm_identity,
                "bgm_selection_mode": bgm_mode,
                "music_selection": music_selection,
                "bgm_mix": bgm_mix,
                "top_title": top_title,
                "cover_title": cover_title,
                "layout_profile": profile_id,
            }
            color = "#FFFFFF"
            if _HEX_COLOR.fullmatch(color) is None:
                raise ValueError(f"任务 {item['row_key']} 的字幕颜色不合法")
            subtitles = dict(item.get("subtitles") or {})
            audio = item.get("outputs", {}).get("audio")
            asr_alignment = subtitles.get("asr_alignment")
            asr_preview_warning: str | None = None
            alignment_is_current = bool(
                isinstance(audio, dict)
                and alignment_matches(
                    asr_alignment,
                    script=str(item.get("script_text") or ""),
                    audio_asset_id=str(audio.get("asset_id") or ""),
                    audio_version=audio.get("version"),
                )
            )
            if not alignment_is_current and self.caption_aligner is not None:
                if not isinstance(audio, dict) or not audio.get("managed_path"):
                    raise ValueError(f"任务 {item['row_key']} 缺少可供 ASR 校准的音频")
                try:
                    asr_alignment = self.caption_aligner.align(
                        audio["managed_path"],
                        script=str(item.get("script_text") or ""),
                        raw_cues=subtitles.get("raw_cues", []),
                        audio_asset_id=str(audio.get("asset_id") or ""),
                        audio_version=audio.get("version"),
                    )
                    subtitles["asr_alignment"] = asr_alignment
                    alignment_is_current = True
                except CaptionAlignmentError as exc:
                    failed_alignment = {
                        "status": "FAILED",
                        "reason_code": exc.code,
                        "reason_summary": str(exc)[:500],
                    }
                    if exc.code in ASR_RAW_CUE_PREVIEW_FALLBACK_CODES:
                        asr_alignment = failed_alignment
                        subtitles["asr_alignment"] = failed_alignment
                        asr_preview_warning = (
                            f"{exc}；已改用 MiniMax 原始时间轴生成预览字幕"
                        )
                    else:
                        subtitles.update(
                            {
                                "render_cues": [],
                                "status": "REVIEW_REQUIRED",
                                "overflow_risk": False,
                                "review_reason": str(exc),
                                "asr_alignment": failed_alignment,
                            }
                        )
                        self.store.set_item_subtitles(
                            owner_user_id, project_id, item["item_id"], subtitles
                        )
                        continue
            if (
                self.require_precise_alignment
                and not alignment_is_current
                and asr_preview_warning is None
            ):
                reason = "当前音频尚未配置或启动本地 ASR 精确字幕服务"
                subtitles.update(
                    {
                        "render_cues": [],
                        "status": "REVIEW_REQUIRED",
                        "overflow_risk": False,
                        "review_reason": reason,
                        "asr_alignment": {
                            "status": "FAILED",
                            "reason_code": "ASR_ALIGNMENT_REQUIRED",
                            "reason_summary": reason,
                        },
                    }
                )
                self.store.set_item_subtitles(
                    owner_user_id, project_id, item["item_id"], subtitles
                )
                continue
            render_item = {**item, "subtitles": subtitles}
            try:
                render_cues, semantic_mapping = derive_project_render_cues(
                    render_item,
                    font_path=str(font["path"]),
                    font_size=float(caption_style["font_size"]) * float(caption_style["clip_scale"]),
                    max_width_ratio=float(caption_style["max_width_ratio"]),
                    asr_alignment=(asr_alignment if alignment_is_current else None),
                    require_precise_alignment=(
                        self.require_precise_alignment and asr_preview_warning is None
                    ),
                )
            except (CaptionLayoutReviewRequired, CaptionAlignmentError) as exc:
                subtitles.update(
                    {
                        "render_cues": [],
                        "status": "REVIEW_REQUIRED",
                        "overflow_risk": isinstance(exc, CaptionLayoutReviewRequired),
                        "review_reason": str(exc),
                        "semantic_mapping": _fallback_mapping(
                            item,
                            code=(
                                "RAW_CUE_LAYOUT_REVIEW_REQUIRED"
                                if isinstance(exc, CaptionLayoutReviewRequired)
                                else exc.code
                            ),
                            summary=str(exc),
                        ),
                    }
                )
                self.store.set_item_subtitles(
                    owner_user_id, project_id, item["item_id"], subtitles
                )
                continue

            subtitles.update(
                {
                    "render_cues": render_cues,
                    "status": "PREVIEW_READY",
                    "overflow_risk": False,
                    "review_reason": asr_preview_warning,
                    "semantic_mapping": semantic_mapping,
                    "bound_video_asset_id": base_video.get("asset_id"),
                    "style": {
                        "font_id": font_identity,
                        "font_name": str(font.get("name") or ""),
                        "font_size": float(caption_style["font_size"]),
                        "clip_scale": float(caption_style["clip_scale"]),
                        "text_color": color,
                        "stroke_color": "",
                        "stroke_width": 0.0,
                        "shadow_color": "#000000",
                        "shadow_alpha": float(caption_style["shadow_alpha"]),
                        "shadow_distance": 5.0,
                        "shadow_angle": -45.0,
                        "shadow_smoothing": 0.45000001788139343,
                        "max_width_ratio": float(caption_style["max_width_ratio"]),
                        "max_lines": CAPTION_MAX_LINES,
                        "bottom_offset_ratio": 0.5 + float(caption_style["transform_y"]) / 2,
                        "transform_y": float(caption_style["transform_y"]),
                        "layout_profile": profile_id,
                    },
                }
            )
            subtitle_updates.append((item, subtitles))
        draft_jobs: list[dict[str, Any]] = []
        draft_variants: list[dict[str, Any]] = []
        draft_operations: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item, subtitles in subtitle_updates:
            selected = resolved_settings[str(item["item_id"])]
            self.store.configure_item_postprocess(
                owner_user_id,
                project_id,
                item["item_id"],
                font_identity=str(subtitles["style"]["font_id"]),
                bgm_identity=str(selected.get("bgm_identity") or ""),
                text_color=str(subtitles["style"]["text_color"]),
                bgm_selection_mode=str(
                    selected.get("bgm_selection_mode") or "manual"
                ),
                music_selection=dict(selected.get("music_selection") or {}),
                top_title=dict(selected.get("top_title") or {}),
                cover_title=dict(selected.get("cover_title") or {}),
                layout_profile=str(selected.get("layout_profile") or DEFAULT_LAYOUT_PROFILE),
                automatic_bgm_volume=(
                    float(selected.get("bgm_mix", {}).get("volume"))
                    if selected.get("bgm_identity")
                    else None
                ),
                bgm_loudness=dict(selected.get("bgm_mix") or {}),
                jianying_template=dict(selected.get("jianying_template") or {}),
            )
            updated_project = self.store.set_item_subtitles(
                owner_user_id, project_id, item["item_id"], subtitles
            )
            current_audio = item.get("outputs", {}).get("audio") or {}
            visual_alignment_is_current = bool(
                isinstance(current_audio, dict)
                and alignment_matches(
                    subtitles.get("asr_alignment"),
                    script=str(item.get("script_text") or ""),
                    audio_asset_id=str(current_audio.get("asset_id") or ""),
                    audio_version=current_audio.get("version"),
                )
            )
            if self.semantic_visual_catalog is not None and visual_alignment_is_current:
                updated_item = next(
                    (
                        candidate
                        for candidate in updated_project.get("items", [])
                        if candidate.get("item_id") == item["item_id"]
                    ),
                    None,
                )
                if isinstance(updated_item, dict):
                    remap_saved_visual_plan(
                        self.store,
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        item=updated_item,
                        catalog=self.semantic_visual_catalog,
                    )
            latest_project = self.store.get_project(owner_user_id, project_id)
            latest_item = next(
                (
                    candidate
                    for candidate in latest_project.get("items", [])
                    if candidate.get("item_id") == item["item_id"]
                ),
                None,
            )
            if not isinstance(latest_item, dict):
                raise KeyError("项目脚本行不存在")
            draft_name = available_draft_name(
                self.draft_root, composition_draft_name(latest_item)
            )
            job = self._build_draft_job(
                latest_item,
                draft_name=draft_name,
                skip_export=True,
            )
            recipe_sha256 = draft_recipe_sha256(
                job,
                subtitle_analysis_identity=subtitle_analysis_sha256(
                    latest_item.get("content_analysis")
                ),
            )
            operation = self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                operation_type="POSTPROCESS_GENERATE",
                idempotency_key=clean_key,
                payload={
                    "base_video_asset_id": item.get("outputs", {})
                    .get("base_video", {})
                    .get("asset_id"),
                    "font_identity": subtitles["style"]["font_id"],
                    "bgm_identity": selected.get("bgm_identity") or None,
                    "bgm_selection_mode": selected.get("bgm_selection_mode"),
                    "music_selection": selected.get("music_selection"),
                    "draft_recipe_sha256": recipe_sha256,
                    "caption_max_width_ratio": CAPTION_MAX_WIDTH_RATIO,
                    "caption_max_lines": CAPTION_MAX_LINES,
                    "caption_bottom_offset_ratio": 0.5
                    + float(layout_profile(selected.get("layout_profile"))["caption"]["transform_y"])
                    / 2,
                },
            )
            job["observability"] = {
                "project_id": project_id,
                "item_id": item["item_id"],
                "operation_id": operation["operation_id"],
                "correlation_id": operation["correlation_id"],
            }
            draft_jobs.append(job)
            draft_variants.append(
                {
                    "project_id": project_id,
                    "item_id": item["item_id"],
                    "kind": "composition_draft",
                }
            )
            draft_operations.append((item, operation))
        if not draft_jobs:
            return self.sync(owner_user_id, project_id)
        try:
            submitted = self.render_queue.submit_batch(draft_jobs, draft_variants)
            batch_id = str(submitted.get("batch_id") or "")
            job_ids = [str(value) for value in submitted.get("job_ids", [])]
            if not batch_id or len(job_ids) != len(draft_operations):
                raise ValueError("剪映任务队列返回了无效的草稿生成结果")
            for (item, operation), job_id in zip(draft_operations, job_ids):
                self.store.add_link(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    item_id=item["item_id"],
                    system="jianying",
                    relation="postprocess_draft_job",
                    external_id=job_id,
                    metadata={"batch_id": batch_id, "reason": "postprocess_generate"},
                )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation["operation_id"],
                    operation_type="POSTPROCESS_GENERATE",
                    status="RUNNING",
                    item_status="POSTPROCESS_RUNNING",
                    result={
                        "batch_id": batch_id,
                        "job_id": job_id,
                        "operation_id": operation["operation_id"],
                        "preview_mode": "browser_with_frozen_draft",
                    },
                )
        except Exception as exc:
            for item, operation in draft_operations:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation["operation_id"],
                    operation_type="POSTPROCESS_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
            raise
        return self.sync(owner_user_id, project_id)

    def export_preview(
        self,
        owner_user_id: str,
        project_id: str,
        item_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Export one browser preview only when the user explicitly requests a file."""

        project = self.store.get_project(owner_user_id, project_id)
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            raise ValueError("按需导出请求缺少幂等键")
        item = next(
            (entry for entry in project["items"] if str(entry["item_id"]) == str(item_id)),
            None,
        )
        if item is None:
            raise KeyError("脚本行不存在")
        if item.get("outputs", {}).get("composition_video") is not None:
            return project
        if item.get("status") == "POSTPROCESS_RUNNING":
            return self.sync(owner_user_id, project_id)
        subtitles = dict(item.get("subtitles") or {})
        if subtitles.get("status") != "PREVIEW_READY" or not subtitles.get("render_cues"):
            raise ValueError("请先生成浏览器字幕与 BGM 预览")
        repeated = [
            operation
            for operation in project.get("operations", [])
            if operation.get("operation_type") == "POSTPROCESS_EXPORT"
            and operation.get("item_id") == item["item_id"]
            and operation.get("idempotency_key") == clean_key
        ]
        if repeated and any(operation.get("result", {}).get("job_id") for operation in repeated):
            return self.sync(owner_user_id, project_id)
        base_video = item.get("outputs", {}).get("base_video")
        if not isinstance(base_video, dict) or not base_video.get("managed_path"):
            raise ValueError("当前浏览器预览缺少画面源文件")
        output = (
            self.storage_root
            / "projects"
            / str(owner_user_id)
            / project_id
            / str(item["item_id"])
            / "composition"
            / f"composition-{uuid.uuid4().hex}.mp4"
        )
        recovery_job = self._build_draft_job(
            item,
            draft_name=available_draft_name(
                self.draft_root, composition_draft_name(item)
            ),
            skip_export=True,
        )
        recipe_sha256 = draft_recipe_sha256(
            recovery_job,
            subtitle_analysis_identity=subtitle_analysis_sha256(
                item.get("content_analysis")
            ),
        )
        latest_generate = next(
            (
                candidate
                for candidate in reversed(project.get("operations", []))
                if candidate.get("operation_type") == "POSTPROCESS_GENERATE"
                and candidate.get("item_id") == item["item_id"]
                and candidate.get("status") == "SUCCEEDED"
                and isinstance(candidate.get("payload"), dict)
                and candidate["payload"].get("draft_recipe_sha256")
                == recipe_sha256
            ),
            None,
        )
        frozen_draft: dict[str, Any] | None = None
        if latest_generate is not None:
            result = (
                latest_generate.get("result")
                if isinstance(latest_generate.get("result"), dict)
                else {}
            )
            draft_dir_text = str(result.get("output_draft_dir") or "")
            draft_name = str(result.get("output_draft_name") or "")
            if draft_dir_text and draft_name:
                draft_dir = Path(draft_dir_text).resolve()
                if draft_dir.is_dir() and (draft_dir / "draft_content.json").is_file():
                    frozen_draft = {
                        "draft_dir": str(draft_dir),
                        "draft_name": draft_name,
                    }

        if frozen_draft is None:
            # Old previews did not pre-generate a draft.  Prepare one as its own
            # queue step and let the caller invoke export again after it succeeds.
            # This keeps timeline construction out of the Jianying export step.
            prepare_key = (
                "export-prepare-"
                + hashlib.sha256(
                    f"{clean_key}:{recipe_sha256}".encode("utf-8")
                ).hexdigest()[:32]
            )
            prepare_operation = self.store.create_operation(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                operation_type="POSTPROCESS_GENERATE",
                idempotency_key=prepare_key,
                payload={
                    "reason": "prepare_frozen_draft_for_export",
                    "base_video_asset_id": base_video.get("asset_id"),
                    "draft_recipe_sha256": recipe_sha256,
                },
            )
            prepare_job = recovery_job
            prepare_job["observability"] = {
                "project_id": project_id,
                "item_id": item["item_id"],
                "operation_id": prepare_operation["operation_id"],
                "correlation_id": prepare_operation["correlation_id"],
            }
            try:
                submitted = self.render_queue.submit_batch(
                    [prepare_job],
                    [
                        {
                            "project_id": project_id,
                            "item_id": item["item_id"],
                            "kind": "composition_draft",
                        }
                    ],
                )
                batch_id = str(submitted.get("batch_id") or "")
                job_ids = [str(value) for value in submitted.get("job_ids", [])]
                if not batch_id or len(job_ids) != 1:
                    raise ValueError("剪映任务队列返回了无效的草稿准备结果")
                job_id = job_ids[0]
                self.store.add_link(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    item_id=item["item_id"],
                    system="jianying",
                    relation="postprocess_draft_job",
                    external_id=job_id,
                    metadata={"batch_id": batch_id, "reason": "export_prepare"},
                )
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=prepare_operation["operation_id"],
                    operation_type="POSTPROCESS_GENERATE",
                    status="RUNNING",
                    item_status="POSTPROCESS_RUNNING",
                    result={
                        "batch_id": batch_id,
                        "job_id": job_id,
                        "operation_id": prepare_operation["operation_id"],
                        "preview_mode": "browser_with_frozen_draft",
                        "reason": "export_prepare",
                    },
                )
            except Exception as exc:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=prepare_operation["operation_id"],
                    operation_type="POSTPROCESS_GENERATE",
                    status="FAILED",
                    item_status="COMPOSITION_FAILED",
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
            return self.sync(owner_user_id, project_id)

        job = {
            "schema": "jyd.render_job.v1",
            "source": {
                "type": "existing_draft",
                **frozen_draft,
                "recovery": {"rebuild_job": recovery_job},
            },
            "output": {"mp4_path": str(output)},
            "export": {"resolution": "1080P", "framerate": "30fps"},
        }
        export_source = "frozen_draft"
        operation = self.store.create_operation(
            owner_user_id=owner_user_id,
            project_id=project_id,
            item_id=item["item_id"],
            operation_type="POSTPROCESS_EXPORT",
            idempotency_key=clean_key,
            payload={
                "reason": "explicit_download",
                "base_video_asset_id": base_video.get("asset_id"),
                "export_source": export_source,
                "draft_recipe_sha256": recipe_sha256,
            },
        )
        job["observability"] = {
            "project_id": project_id,
            "item_id": item["item_id"],
            "operation_id": operation["operation_id"],
            "correlation_id": operation["correlation_id"],
        }
        recovery_job["observability"] = {
            **job["observability"],
            "recovery_reason": "draft_discovery_exhausted",
        }
        try:
            submitted = self.render_queue.submit_batch(
                [job],
                [{"project_id": project_id, "item_id": item["item_id"], "kind": "preview_export"}],
            )
            batch_id = str(submitted.get("batch_id") or "")
            job_ids = [str(value) for value in submitted.get("job_ids", [])]
            if not batch_id or len(job_ids) != 1:
                raise ValueError("剪映任务队列返回了无效的按需导出结果")
            job_id = job_ids[0]
            self.store.add_link(
                owner_user_id=owner_user_id,
                project_id=project_id,
                item_id=item["item_id"],
                system="jianying",
                relation="postprocess_export_job",
                external_id=job_id,
                metadata={"batch_id": batch_id, "reason": "explicit_download"},
            )
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item["item_id"],
                operation_id=operation["operation_id"],
                operation_type="POSTPROCESS_EXPORT",
                status="RUNNING",
                item_status="POSTPROCESS_RUNNING",
                result={
                    "batch_id": batch_id,
                    "job_id": job_id,
                    "operation_id": operation["operation_id"],
                },
            )
        except Exception as exc:
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item["item_id"],
                operation_id=operation["operation_id"],
                operation_type="POSTPROCESS_EXPORT",
                status="FAILED",
                item_status="COMPOSITION_FAILED",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        return self.sync(owner_user_id, project_id)

    def sync(self, owner_user_id: str, project_id: str) -> dict[str, Any]:
        queue_store = getattr(self.render_queue, "store", None)
        recover_expired = getattr(queue_store, "recover_expired_leases", None)
        if callable(recover_expired):
            try:
                recover_expired()
            except Exception:
                pass
        project = self.store.get_project(owner_user_id, project_id)
        operations, latest_by_item = _active_postprocess_operations(project)
        items = {str(item["item_id"]): item for item in project["items"]}
        for operation in operations:
            item_id = str(operation.get("item_id") or "")
            item = items.get(item_id)
            if item is None:
                continue
            operation_id = str(operation.get("operation_id") or "")
            is_latest = latest_by_item.get(item_id) == operation_id
            preserved_item_status = str(item.get("status") or "DRAFT")
            result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
            operation_type = str(operation.get("operation_type") or "POSTPROCESS_GENERATE")
            job_id = str(result.get("job_id") or "")
            if not job_id:
                continue
            try:
                status = self.render_queue.get_status(job_id)
            except Exception as exc:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="FAILED",
                    item_status=("COMPOSITION_FAILED" if is_latest else preserved_item_status),
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )
                continue
            remote_status = str(status.get("status") or "")
            if remote_status in {"pending", "running"}:
                stalled = self._active_render_stall(operation, status)
                if stalled is None:
                    continue
                error_code, error_message = stalled
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="FAILED",
                    item_status=("COMPOSITION_FAILED" if is_latest else preserved_item_status),
                    result={"job_id": job_id},
                    error_code=error_code,
                    error_message=error_message,
                )
                continue
            if remote_status != "completed":
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="FAILED",
                    item_status=("COMPOSITION_FAILED" if is_latest else preserved_item_status),
                    result={"job_id": job_id},
                    error_code="JY_RENDER_FAILED",
                    error_message=str(status.get("error") or "剪映后处理失败"),
                )
                continue
            render_result = status.get("result") if isinstance(status.get("result"), dict) else {}
            if operation_type == "POSTPROCESS_GENERATE":
                draft_dir = Path(
                    str(render_result.get("output_draft_dir") or "")
                ).resolve()
                draft_name = str(render_result.get("output_draft_name") or "").strip()
                if (
                    not draft_name
                    or not draft_dir.is_dir()
                    or not (draft_dir / "draft_content.json").is_file()
                ):
                    self.store.transition_operation(
                        owner_user_id,
                        project_id,
                        item["item_id"],
                        operation_id=operation_id,
                        operation_type=operation_type,
                        status="FAILED",
                        item_status=(
                            "COMPOSITION_FAILED" if is_latest else preserved_item_status
                        ),
                        result={"job_id": job_id},
                        error_code="DRAFT_OUTPUT_MISSING",
                        error_message="草稿生成任务完成但剪映草稿不存在或结构不完整",
                    )
                    continue
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="SUCCEEDED",
                    item_status=("COMPOSITION_READY" if is_latest else preserved_item_status),
                    result={
                        **result,
                        "job_id": job_id,
                        "output_draft_dir": str(draft_dir),
                        "output_draft_name": draft_name,
                        "preview_mode": "browser_with_frozen_draft",
                    },
                )
                continue
            output = Path(str(render_result.get("output_mp4") or "")).resolve()
            if not output.is_file():
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="FAILED",
                    item_status=("COMPOSITION_FAILED" if is_latest else preserved_item_status),
                    result={"job_id": job_id},
                    error_code="OUTPUT_MISSING",
                    error_message="剪映任务完成但成片文件不存在",
                )
                continue
            if not is_latest:
                self.store.transition_operation(
                    owner_user_id,
                    project_id,
                    item["item_id"],
                    operation_id=operation_id,
                    operation_type=operation_type,
                    status="SUCCEEDED",
                    item_status=preserved_item_status,
                    result={
                        "batch_id": result.get("batch_id"),
                        "job_id": job_id,
                        "output_mp4": str(output),
                        "superseded": True,
                    },
                )
                continue
            current = item.get("outputs", {}).get("composition_video")
            subtitles = dict(item.get("subtitles") or {})
            settings = dict(item.get("settings", {}).get("postprocess") or {})
            subtitles["status"] = "RENDERED"
            subtitles["overflow_risk"] = False
            self.store.set_item_subtitles(
                owner_user_id, project_id, item["item_id"], subtitles
            )
            if not (
                isinstance(current, dict)
                and current.get("external_ref", {}).get("render_job_id") == job_id
            ):
                self.store.add_asset(
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    item_id=item["item_id"],
                    asset_type="composition_video",
                    source_type="jianying_postprocess",
                    status="READY",
                    filename=composition_export_filename(item),
                    managed_path=str(output),
                    external_ref={"render_job_id": job_id},
                    metadata={
                        "base_video_asset_id": item.get("outputs", {})
                        .get("base_video", {})
                        .get("asset_id"),
                        "captions": "minimax_one_line",
                        "bgm_volume": float(
                            settings.get("bgm_volume") or BGM_FALLBACK_VOLUME
                        ),
                    },
                    make_current=True,
                )
            self.store.transition_operation(
                owner_user_id,
                project_id,
                item["item_id"],
                operation_id=operation_id,
                operation_type=operation_type,
                status="SUCCEEDED",
                item_status="COMPOSITION_READY",
                result={
                    "batch_id": result.get("batch_id"),
                    "job_id": job_id,
                    "output_mp4": str(output),
                },
            )
        return self.store.get_project(owner_user_id, project_id)
