from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.cli import (  # noqa: E402
    add_effect_json_to_video,
    apply_json_timerange_override,
    effect_material_label,
    effect_tracks,
    import_pyjianyingdraft,
    load_effect_json,
    load_output_script,
    load_plain_draft_json,
    log,
    log_effect_details,
    save_plain_draft_json,
    summarize_draft_json,
    text_tracks,
    video_effect_materials,
    video_tracks,
)
from jyd_probe.content_replace import (  # noqa: E402
    AudioAddition,
    AudioSegmentReplacement,
    ContentReplaceJob,
    EffectAddition,
    NamedAudioReplacement,
    TextFontReplacement,
    TextStylePresetReplacement,
    run_content_replace_job,
)


# 基础草稿路径。
# TEMPLATE_DRAFT_DIR: 原模板草稿目录，里面必须有明文 draft_content.json。
# OUTPUT_ROOT: 新草稿输出父目录，通常就是剪映的 JianyingPro Drafts 目录。
# OUTPUT_NAME: 新草稿文件夹名；留空会自动生成，不会覆盖原草稿。
TEMPLATE_DRAFT_DIR = r"D:\剪映草稿\JianyingPro Drafts\测试1"
OUTPUT_ROOT = r"D:\剪映草稿\JianyingPro Drafts"
OUTPUT_NAME = ""


# 字体/字幕样式设置。
# 两种方式二选一即可：
# 1. FONT_ID 或 FONT_NAME: 只换字体。
# 2. TEXT_STYLE_JSON_PATH: 使用 export_text_style.py 导出的样式 JSON，可包含字体、颜色、描边、下划线等。
#
# APPLY_TO_TEXT_TRACK_INDEXES:
#   空列表 [] 表示处理所有顶层文本轨道。
#   [0] 表示只处理第 1 条顶层文本轨道。
FONT_NAME = ""
FONT_ID = ""
FONT_PATH = "D:"
TEXT_STYLE_JSON_PATH = ""
TEXT_STYLE_APPLY_CLIP = False
APPLY_TO_TEXT_TRACK_INDEXES: list[int] = []


# 背景音乐设置。
# 如果模板里已经有一条音乐，通常用 AUDIO_SEGMENT_REPLACEMENTS 替换。
# 如果模板里没有音乐，或者想额外添加音乐，用 AUDIO_ADDITIONS。
NAMED_AUDIO_REPLACEMENTS = [
    # NamedAudioReplacement(
    #     media_path=r"D:\素材\music.mp3",
    #     material_name="",  # 留空时默认替换第一个 audio material
    # ),
]

AUDIO_SEGMENT_REPLACEMENTS = [
    # AudioSegmentReplacement(
    #     media_path=r"D:\素材\music.mp3",
    #     track_index=0,
    #     segment_index=0,
    #     source_start_us=-1,
    #     source_duration_us=0,
    #     target_start_us=0,
    #     target_duration_us=0,
    # ),
]

AUDIO_ADDITIONS = [
    # AudioAddition(
    #     media_path=r"D:\素材\music.mp3",
    #     target_start_us=0,
    #     target_duration_us=0,
    # ),
]


@dataclass(frozen=True)
class VideoSubtitleSwap:
    """交换同一条视频轨道上的两个片段，并移动对应字幕。

    video_track_index: 顶层视频轨道下标，只统计 type="video" 的轨道，从 0 开始。
    first_segment_index: 要换到后面的片段下标，通常是 0。
    second_segment_index: 要换到前面的片段下标，通常是 1。
    text_track_indexes: 字幕轨道下标；空列表表示所有顶层文本轨道。
    """

    video_track_index: int = 0
    first_segment_index: int = 0
    second_segment_index: int = 1
    text_track_indexes: list[int] = field(default_factory=list)


# 视频片段和字幕一起互换。
VIDEO_SUBTITLE_SWAP = VideoSubtitleSwap(
    video_track_index=0,
    first_segment_index=0,
    second_segment_index=1,
    text_track_indexes=[],
)


# 特效是单独添加的，不跟随视频片段互换。
# 这里的 target_video_segment_index 是互换保存后的最终视频片段下标。
EFFECT_ADDITIONS = [
    # EffectAddition(
    #     effect_json_path=r"D:\工作内容\轻盈健\公寓\effect_library\example_effect.json",
    #     target_video_track_index=0,
    #     target_video_segment_index=0,
    #     start_us=-1,
    #     duration_us=0,
    # ),
]


@dataclass(frozen=True)
class EffectReplacement:
    """替换草稿里已有的特效占位。

    effect_json_path: export/effect library 里保存的特效 JSON。
    target_effect_track_index: 目标 effect 轨道下标，只统计 type="effect" 的轨道，从 0 开始。
    target_effect_segment_index: 目标 effect 轨道内的特效片段下标，从 0 开始。
    start_us/duration_us: 可选；-1/0 表示保留原特效片段时间。
    """

    effect_json_path: str | Path
    target_effect_track_index: int = 0
    target_effect_segment_index: int = 0
    start_us: int = -1
    duration_us: int = 0


# 替换已有特效占位。
# 如果模板里本来已经有一个特效，只想把它换成特效库里的另一个效果，就填这里。
# target_effect_track_index / target_effect_segment_index 在 DumpEffects 日志里看。
EFFECT_REPLACEMENTS = [
    # EffectReplacement(
    #     effect_json_path=r"D:\工作内容\轻盈健\公寓\effect_library\example_effect.json",
    #     target_effect_track_index=0,
    #     target_effect_segment_index=0,
    #     start_us=-1,
    #     duration_us=0,
    # ),
]


@dataclass(frozen=True)
class EffectOperation:
    """Unified effect operation for add/replace and segment/full/custom scope.

    action:
        "add" adds a new effect track.
        "replace" replaces an existing effect segment placeholder.
    scope:
        "segment" follows target_video_track_index/target_video_segment_index.
        "full_video" covers the whole top-level video timeline.
        "custom" uses start_us/duration_us directly.
    """

    effect_json_path: str | Path
    action: str = "add"
    scope: str = "segment"
    target_video_track_index: int = 0
    target_video_segment_index: int = 0
    target_effect_track_index: int = 0
    target_effect_segment_index: int = 0
    start_us: int = -1
    duration_us: int = 0


# Recommended unified effect config.
# Examples:
#   Add effect to one video segment:
#       EffectOperation(path, action="add", scope="segment", target_video_segment_index=0)
#   Add effect to the whole video:
#       EffectOperation(path, action="add", scope="full_video")
#   Replace an existing effect placeholder and cover the whole video:
#       EffectOperation(path, action="replace", scope="full_video", target_effect_track_index=0)
#   Use a custom timeline range:
#       EffectOperation(path, action="add", scope="custom", start_us=0, duration_us=5_000_000)
EFFECT_OPERATIONS = [
    # EffectOperation(
    #     effect_json_path=r"D:\工作内容\轻盈健\公寓\effect_library\example_effect.json",
    #     action="add",  # "add" or "replace"
    #     scope="full_video",  # "segment", "full_video", or "custom"
    #     target_video_track_index=0,
    #     target_video_segment_index=0,
    #     target_effect_track_index=0,
    #     target_effect_segment_index=0,
    #     start_us=-1,
    #     duration_us=0,
    # ),
]


def _timerange(segment: dict[str, Any]) -> tuple[int, int]:
    target_timerange = segment.get("target_timerange")
    if not isinstance(target_timerange, dict):
        raise RuntimeError(f"片段缺少 target_timerange: id={segment.get('id')!r}")

    start = int(target_timerange.get("start", 0))
    duration = int(target_timerange.get("duration", 0))
    if duration <= 0:
        raise RuntimeError(f"片段 duration 必须大于 0: id={segment.get('id')!r}, duration={duration}")
    return start, duration


def _set_segment_start(segment: dict[str, Any], start_us: int) -> None:
    target_timerange = segment.setdefault("target_timerange", {})
    if not isinstance(target_timerange, dict):
        raise RuntimeError(f"片段 target_timerange 不是对象: id={segment.get('id')!r}")
    target_timerange["start"] = int(start_us)


def _segment_sort_start(segment: Any) -> int:
    if not isinstance(segment, dict):
        return 0
    target_timerange = segment.get("target_timerange")
    if not isinstance(target_timerange, dict):
        return 0
    return int(target_timerange.get("start", 0))


def _selected_text_tracks(data: dict[str, Any], indexes: list[int]) -> list[tuple[int, int, dict[str, Any]]]:
    tracks = text_tracks(data)
    if not tracks:
        log("当前草稿没有顶层文本轨道，视频互换时不会移动字幕", "WARN")
        return []

    if not indexes:
        return [(logical_index, raw_index, track) for logical_index, (raw_index, track) in enumerate(tracks)]

    selected: list[tuple[int, int, dict[str, Any]]] = []
    for logical_index in indexes:
        if not 0 <= logical_index < len(tracks):
            raise IndexError(f"文本轨道下标越界: {logical_index}，可用范围 [0, {len(tracks)})")
        raw_index, track = tracks[logical_index]
        selected.append((logical_index, raw_index, track))
    return selected


def _move_subtitles_for_swap(
    data: dict[str, Any],
    swap: VideoSubtitleSwap,
    first_old_start: int,
    first_duration: int,
    first_new_start: int,
    second_old_start: int,
    second_duration: int,
    second_new_start: int,
) -> int:
    first_old_end = first_old_start + first_duration
    second_old_end = second_old_start + second_duration
    moved = 0

    for logical_index, raw_index, text_track in _selected_text_tracks(data, swap.text_track_indexes):
        segments = text_track.get("segments", [])
        if not isinstance(segments, list):
            raise RuntimeError(f"文本轨道 segments 不是列表: text_track_index={logical_index}")

        for segment in segments:
            if not isinstance(segment, dict):
                continue

            try:
                subtitle_start, subtitle_duration = _timerange(segment)
            except RuntimeError:
                continue

            midpoint = subtitle_start + subtitle_duration // 2
            if first_old_start <= midpoint < first_old_end:
                new_start = first_new_start + (subtitle_start - first_old_start)
            elif second_old_start <= midpoint < second_old_end:
                new_start = second_new_start + (subtitle_start - second_old_start)
            else:
                continue

            _set_segment_start(segment, new_start)
            moved += 1

        segments.sort(key=_segment_sort_start)
        log(
            "已按视频互换移动字幕轨道: "
            f"text_track_index={logical_index}, raw_track_index={raw_index}, segments={len(segments)}"
        )

    return moved


def swap_video_segments_and_subtitles(data: dict[str, Any], swap: VideoSubtitleSwap) -> int:
    tracks = video_tracks(data)
    if not tracks:
        raise RuntimeError("当前草稿没有顶层视频轨道，无法交换视频片段")
    if not 0 <= swap.video_track_index < len(tracks):
        raise IndexError(f"视频轨道下标越界: {swap.video_track_index}，可用范围 [0, {len(tracks)})")

    raw_track_index, video_track = tracks[swap.video_track_index]
    segments = video_track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"视频轨道 segments 不是列表: video_track_index={swap.video_track_index}")

    for segment_index in [swap.first_segment_index, swap.second_segment_index]:
        if not 0 <= segment_index < len(segments):
            raise IndexError(f"视频片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    first_segment = segments[swap.first_segment_index]
    second_segment = segments[swap.second_segment_index]
    if not isinstance(first_segment, dict) or not isinstance(second_segment, dict):
        raise RuntimeError("要交换的视频片段不是 JSON 对象")

    first_old_start, first_duration = _timerange(first_segment)
    second_old_start, second_duration = _timerange(second_segment)

    if second_old_start < first_old_start:
        log(
            "第二个片段在时间线上早于第一个片段，仍按配置下标执行互换: "
            f"first_start={first_old_start}, second_start={second_old_start}",
            "WARN",
        )

    new_front_start = min(first_old_start, second_old_start)
    second_new_start = new_front_start
    first_new_start = new_front_start + second_duration

    _set_segment_start(second_segment, second_new_start)
    _set_segment_start(first_segment, first_new_start)
    segments.sort(key=_segment_sort_start)

    moved_subtitles = _move_subtitles_for_swap(
        data,
        swap,
        first_old_start,
        first_duration,
        first_new_start,
        second_old_start,
        second_duration,
        second_new_start,
    )

    log(
        "已交换视频片段并移动对应字幕: "
        f"video_track_index={swap.video_track_index}, raw_track_index={raw_track_index}, "
        f"first_segment_index={swap.first_segment_index}, second_segment_index={swap.second_segment_index}, "
        f"first_old=[{first_old_start}, {first_old_start + first_duration}), "
        f"second_old=[{second_old_start}, {second_old_start + second_duration}), "
        f"first_new_start={first_new_start}, second_new_start={second_new_start}, "
        f"moved_subtitles={moved_subtitles}"
    )
    return 2 + moved_subtitles


def _track_selected(track_index: int, selected_indexes: list[int]) -> bool:
    return not selected_indexes or track_index in selected_indexes


def build_text_font_replacements() -> list[TextFontReplacement]:
    if not FONT_NAME and not FONT_ID:
        return []

    data = load_plain_draft_json(Path(TEMPLATE_DRAFT_DIR).resolve())
    replacements: list[TextFontReplacement] = []
    for text_track_index, (_raw_track_index, track) in enumerate(text_tracks(data)):
        if not _track_selected(text_track_index, APPLY_TO_TEXT_TRACK_INDEXES):
            continue
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, _segment in enumerate(segments):
            replacements.append(
                TextFontReplacement(
                    font_name=FONT_NAME,
                    font_id=FONT_ID,
                    font_path=FONT_PATH,
                    track_index=text_track_index,
                    segment_index=segment_index,
                )
            )

    log(f"已生成字体替换配置: {len(replacements)} 条")
    return replacements


def build_text_style_replacements() -> list[TextStylePresetReplacement]:
    if not TEXT_STYLE_JSON_PATH:
        return []

    data = load_plain_draft_json(Path(TEMPLATE_DRAFT_DIR).resolve())
    replacements: list[TextStylePresetReplacement] = []
    for text_track_index, (_raw_track_index, track) in enumerate(text_tracks(data)):
        if not _track_selected(text_track_index, APPLY_TO_TEXT_TRACK_INDEXES):
            continue
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, _segment in enumerate(segments):
            replacements.append(
                TextStylePresetReplacement(
                    style_json_path=TEXT_STYLE_JSON_PATH,
                    text="",
                    apply_clip=TEXT_STYLE_APPLY_CLIP,
                    track_index=text_track_index,
                    segment_index=segment_index,
                )
            )

    log(f"已生成字幕样式替换配置: {len(replacements)} 条")
    return replacements


def build_base_job() -> ContentReplaceJob:
    return ContentReplaceJob(
        template_draft_dir=TEMPLATE_DRAFT_DIR,
        output_root=OUTPUT_ROOT,
        output_name=OUTPUT_NAME,
        dump_nested_drafts=False,
        dump_effects=False,
        text_font_replacements=build_text_font_replacements(),
        text_style_preset_replacements=build_text_style_replacements(),
        named_audio_replacements=NAMED_AUDIO_REPLACEMENTS,
        audio_segment_replacements=AUDIO_SEGMENT_REPLACEMENTS,
        audio_additions=AUDIO_ADDITIONS,
    )


def _video_segment_timerange(data: dict[str, Any], track_index: int, segment_index: int) -> tuple[int, int]:
    tracks = video_tracks(data)
    if not tracks:
        raise RuntimeError("当前草稿没有顶层视频轨道，无法按视频片段定位特效")
    if not 0 <= track_index < len(tracks):
        raise IndexError(f"视频轨道下标越界: {track_index}，可用范围 [0, {len(tracks)})")

    _raw_track_index, track = tracks[track_index]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"视频轨道 segments 不是列表: video_track_index={track_index}")
    if not 0 <= segment_index < len(segments):
        raise IndexError(f"视频片段下标越界: {segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError("目标视频片段不是 JSON 对象")
    return _timerange(segment)


def _full_video_timerange(data: dict[str, Any]) -> tuple[int, int]:
    max_end = 0
    for _raw_track_index, track in video_tracks(data):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                start, duration = _timerange(segment)
            except RuntimeError:
                continue
            max_end = max(max_end, start + duration)

    if max_end <= 0:
        raise RuntimeError("无法计算整个视频时长：没有找到有效的视频片段 target_timerange")
    return 0, max_end


def resolve_effect_operation_timerange(data: dict[str, Any], item: EffectOperation) -> tuple[int, int]:
    scope = item.scope.strip().lower()
    if scope == "segment":
        return _video_segment_timerange(
            data,
            item.target_video_track_index,
            item.target_video_segment_index,
        )
    if scope == "full_video":
        return _full_video_timerange(data)
    if scope == "custom":
        if item.start_us < 0 or item.duration_us <= 0:
            raise ValueError("scope='custom' 时必须设置 start_us >= 0 且 duration_us > 0")
        return item.start_us, item.duration_us

    raise ValueError(f"不支持的特效 scope: {item.scope!r}，可用值: 'segment', 'full_video', 'custom'")


def _effect_material_index_by_id(materials: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, material in enumerate(materials):
        material_id = material.get("id")
        if material_id:
            result[str(material_id)] = index
    return result


def find_effect_segment_ref(
    data: dict[str, Any],
    effect_track_index: int,
    effect_segment_index: int,
) -> dict[str, Any]:
    tracks = effect_tracks(data)
    if not tracks:
        raise RuntimeError("当前草稿没有 effect 轨道，无法替换已有特效；如需新增特效请使用 EFFECT_ADDITIONS")
    if not 0 <= effect_track_index < len(tracks):
        raise IndexError(f"effect 轨道下标越界: {effect_track_index}，可用范围 [0, {len(tracks)})")

    raw_track_index, track = tracks[effect_track_index]
    segments = track.get("segments", [])
    if not isinstance(segments, list):
        raise RuntimeError(f"effect 轨道 segments 不是列表: effect_track_index={effect_track_index}")
    if not 0 <= effect_segment_index < len(segments):
        raise IndexError(f"effect 片段下标越界: {effect_segment_index}，可用范围 [0, {len(segments)})")

    segment = segments[effect_segment_index]
    if not isinstance(segment, dict):
        raise RuntimeError("目标 effect 片段不是 JSON 对象")

    material_id = segment.get("material_id")
    if not material_id:
        raise RuntimeError("目标 effect 片段缺少 material_id，无法替换")

    effects = video_effect_materials(data)
    effects_by_id = _effect_material_index_by_id(effects)
    material_index = effects_by_id.get(str(material_id))

    return {
        "raw_track_index": raw_track_index,
        "track": track,
        "segment": segment,
        "material_id": str(material_id),
        "material_index": material_index,
        "material": effects[material_index] if material_index is not None else None,
    }


def replace_effect_json_at_segment(
    data: dict[str, Any],
    item: EffectReplacement | EffectOperation,
    *,
    start_us: int | None = None,
    duration_us: int | None = None,
) -> None:
    ref = find_effect_segment_ref(
        data,
        item.target_effect_track_index,
        item.target_effect_segment_index,
    )
    effect_json_data = load_effect_json(Path(item.effect_json_path).resolve())

    target_material_id = ref["material_id"]
    replacement = deepcopy(effect_json_data["material"])
    old_source_id = replacement.get("id")
    replacement["id"] = target_material_id
    if "material_id" in replacement:
        replacement["material_id"] = target_material_id

    materials = data.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise RuntimeError("目标草稿 materials 不是对象，无法替换特效")
    raw_effects = materials.setdefault("video_effects", [])
    if not isinstance(raw_effects, list):
        raise RuntimeError("目标草稿 materials.video_effects 不是列表，无法替换特效")

    material_index = ref["material_index"]
    if material_index is None:
        raw_effects.append(replacement)
    else:
        raw_effects[material_index] = replacement

    target_timerange = ref["segment"].get("target_timerange")
    if not isinstance(target_timerange, dict):
        raise RuntimeError("目标 effect 片段缺少 target_timerange，无法更新时间")
    final_start_us = item.start_us if start_us is None else start_us
    final_duration_us = item.duration_us if duration_us is None else duration_us
    apply_json_timerange_override(target_timerange, final_start_us, final_duration_us, "替换特效片段")

    log(
        "已替换已有特效占位: "
        f"effect_track_index={item.target_effect_track_index}, "
        f"effect_segment_index={item.target_effect_segment_index}, "
        f"raw_track_index={ref['raw_track_index']}, kept_material_id={target_material_id!r}, "
        f"source_id={old_source_id!r}, effect=({effect_material_label(replacement)})"
    )


def apply_effect_operations_after_swap(data: dict[str, Any]) -> int:
    changed = 0
    for item in EFFECT_OPERATIONS:
        start_us, duration_us = resolve_effect_operation_timerange(data, item)
        action = item.action.strip().lower()

        if action == "add":
            effect_json_data = load_effect_json(Path(item.effect_json_path).resolve())
            add_effect_json_to_video(
                data,
                effect_json_data,
                item.target_video_track_index,
                item.target_video_segment_index,
                start_us,
                duration_us,
            )
        elif action == "replace":
            replace_effect_json_at_segment(
                data,
                item,
                start_us=start_us,
                duration_us=duration_us,
            )
        else:
            raise ValueError(f"不支持的特效 action: {item.action!r}，可用值: 'add', 'replace'")

        log(
            "已执行统一特效操作: "
            f"action={action!r}, scope={item.scope!r}, start_us={start_us}, duration_us={duration_us}"
        )
        changed += 1
    return changed


def apply_effect_replacements_after_swap(data: dict[str, Any]) -> int:
    changed = 0
    for item in EFFECT_REPLACEMENTS:
        replace_effect_json_at_segment(data, item)
        changed += 1
    return changed


def apply_effect_additions_after_swap(data: dict[str, Any]) -> int:
    changed = 0
    for item in EFFECT_ADDITIONS:
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
    return changed


def main() -> None:
    result = run_content_replace_job(build_base_job())

    output_data = load_plain_draft_json(result.output_dir)
    json_changes = 0
    json_changes += swap_video_segments_and_subtitles(output_data, VIDEO_SUBTITLE_SWAP)
    json_changes += apply_effect_operations_after_swap(output_data)
    json_changes += apply_effect_replacements_after_swap(output_data)
    json_changes += apply_effect_additions_after_swap(output_data)

    if json_changes:
        save_plain_draft_json(result.output_dir, output_data)
        output_data = load_plain_draft_json(result.output_dir)

    summarize_draft_json(output_data, "片段字幕互换后")
    if EFFECT_OPERATIONS or EFFECT_REPLACEMENTS or EFFECT_ADDITIONS:
        log_effect_details(output_data, "片段字幕互换后")

    draft = import_pyjianyingdraft()
    load_output_script(draft, Path(OUTPUT_ROOT).resolve(), result.output_name)
    log(f"新草稿完整目录: {result.output_dir}")


if __name__ == "__main__":
    main()
