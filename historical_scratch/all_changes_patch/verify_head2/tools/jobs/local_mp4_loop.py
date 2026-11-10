from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.content_replace import (  # noqa: E402
    AudioAddition,
    AudioSegmentReplacement,
    ContentReplaceJob,
    EffectAddition,
    NamedAudioReplacement,
    NestedTextStylePresetReplacement,
    NestedVideoReplacement,
    TextAddition,
    TextReplacement,
    TextStylePresetReplacement,
    VideoSegmentReplacement,
    run_content_replace_job,
)
from jyd_probe.draft_crypto import prepare_plain_draft_dir  # noqa: E402
from jyd_probe.draft_factory import create_plain_draft_from_video  # noqa: E402


def _load_export_api() -> tuple[type, type, type]:
    try:
        from pyJianYingDraft import ExportFramerate, ExportResolution, JianyingController
    except Exception as exc:
        raise RuntimeError(
            "无法导入 pyJianYingDraft 导出控制器；请使用已安装依赖的 Python，"
            "例如 D:\\Myanaconda\\python.exe"
        ) from exc

    return JianyingController, ExportResolution, ExportFramerate


def _enum_by_value(enum_type: type, value: str, label: str):
    if not value:
        return None
    for item in enum_type:
        if item.value.lower() == value.lower():
            return item
    choices = ", ".join(item.value for item in enum_type)
    raise ValueError(f"不支持的{label}: {value!r}，可用值: {choices}")


def _positive_path(path_text: str, label: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{label}不存在: {path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "本地闭环：输入视频 -> 替换模板草稿 -> 调用剪映导出 MP4。"
            "剪映需要已经打开并停在草稿首页。"
        )
    )
    parser.add_argument("--job", default="", help="从 JSON 任务文件读取配置，适合后端/worker 调用")
    parser.add_argument(
        "--source-kind",
        choices=("auto", "template", "video"),
        default="auto",
        help="输入来源：template 使用剪映草稿模板，video 从 input-video 自动创建基础草稿，auto 自动判断",
    )
    parser.add_argument("--template-draft-dir", default="", help="模板草稿目录，必须能被 pyJianYingDraft 加载")
    parser.add_argument(
        "--draft-root",
        default="",
        help="新草稿输出父目录；不填时使用模板草稿的父目录，通常是 JianyingPro Drafts",
    )
    parser.add_argument("--draft-name", default="", help="新草稿名称；不填时自动生成")
    parser.add_argument("--input-video", default="", help="可选：要放进模板的原始视频/图片路径")
    parser.add_argument("--base-draft-work-root", default="", help="source-kind=video 时，基础草稿的临时工作目录")
    parser.add_argument("--base-draft-name", default="", help="source-kind=video 时，基础草稿名称；不填自动生成")
    parser.add_argument("--canvas-width", type=int, default=0, help="source-kind=video 时可强制设置画布宽度")
    parser.add_argument("--canvas-height", type=int, default=0, help="source-kind=video 时可强制设置画布高度")
    parser.add_argument("--canvas-fps", type=int, default=30, help="source-kind=video 时基础草稿帧率")
    parser.add_argument("--output-mp4", default="", help="最终导出的 MP4 路径")
    parser.add_argument(
        "--target-kind",
        choices=("none", "video-segment", "nested-video"),
        default="none",
        help="替换普通顶层视频片段，还是替换复合模板内部视频/图片片段",
    )
    parser.add_argument("--video-track-index", type=int, default=0)
    parser.add_argument("--video-segment-index", type=int, default=0)
    parser.add_argument("--nested-draft-index", type=int, default=0)
    parser.add_argument("--nested-video-track-index", type=int, default=0)
    parser.add_argument("--nested-video-segment-index", type=int, default=0)
    parser.add_argument("--source-start-us", type=int, default=-1, help="从输入视频第几微秒开始截取")
    parser.add_argument("--source-duration-us", type=int, default=0, help="从输入视频截取多长；0 表示默认")
    parser.add_argument("--target-start-us", type=int, default=-1, help="目标片段在时间线上的开始时间")
    parser.add_argument("--target-duration-us", type=int, default=0, help="目标片段在时间线上的持续时间")
    parser.add_argument("--text", default="", help="可选：替换或新增一个文字片段")
    parser.add_argument("--text-mode", choices=("replace", "add"), default="replace", help="文字处理方式：替换已有或新增")
    parser.add_argument("--text-scope", choices=("top", "nested"), default="top", help="文字在顶层还是嵌套模板内")
    parser.add_argument("--text-track-index", type=int, default=0)
    parser.add_argument("--text-segment-index", type=int, default=0)
    parser.add_argument("--text-track-name", default="", help="text-mode=add 时的新文字轨道名称")
    parser.add_argument("--text-start-us", type=int, default=0)
    parser.add_argument("--text-duration-us", type=int, default=5_000_000)
    parser.add_argument("--text-transform-x", type=float, default=0.0)
    parser.add_argument("--text-transform-y", type=float, default=0.0)
    parser.add_argument("--text-size", type=float, default=8.0)
    parser.add_argument("--nested-text-draft-index", type=int, default=0)
    parser.add_argument("--nested-text-track-index", type=int, default=0)
    parser.add_argument("--nested-text-segment-index", type=int, default=0)
    parser.add_argument("--text-style-json", default="", help="可选：套用 text_style_library 中导出的样式 JSON")
    parser.add_argument(
        "--audio-mode",
        choices=("add", "replace-segment", "replace-named"),
        default="add",
        help="音乐处理方式：新增、按片段替换、按素材名替换",
    )
    parser.add_argument("--audio-path", default="", help="可选：新增一条背景音乐")
    parser.add_argument("--audio-material-name", default="", help="audio-mode=replace-named 时的原音频素材名，留空取第一个")
    parser.add_argument("--audio-track-index", type=int, default=0)
    parser.add_argument("--audio-segment-index", type=int, default=0)
    parser.add_argument("--audio-source-start-us", type=int, default=-1)
    parser.add_argument("--audio-source-duration-us", type=int, default=0)
    parser.add_argument("--audio-start-us", type=int, default=0)
    parser.add_argument("--audio-duration-us", type=int, default=0)
    parser.add_argument("--effect-json", default="", help="可选：添加 effect_library 中导出的特效 JSON")
    parser.add_argument("--effect-start-us", type=int, default=-1)
    parser.add_argument("--effect-duration-us", type=int, default=0)
    parser.add_argument("--resolution", default="", help="可选：480P/720P/1080P/2K/4K/8K")
    parser.add_argument("--framerate", default="", help="可选：24fps/25fps/30fps/50fps/60fps")
    parser.add_argument("--export-timeout", type=float, default=1200, help="导出超时时间，单位秒")
    parser.add_argument("--skip-export", action="store_true", help="只生成草稿，不调用剪映导出 MP4")
    parser.add_argument("--no-auto-decrypt", action="store_true", help="关闭自动解密；默认会自动检测加密草稿")
    parser.add_argument("--force-decrypt", action="store_true", help="强制把模板复制到工作目录并调用 jy-draftc 解密")
    parser.add_argument("--decrypt-work-root", default="", help="自动解密工作目录；不填时使用项目内 _decrypted_work")
    parser.add_argument("--jy-draftc-exe", default="", help="jy-draftc.exe 路径；不填时使用同级 jy-draftc 项目")
    parser.add_argument("--jy-install-dir", default="", help="包含 videoeditor.dll 的剪映安装目录；不填时使用 jy-draftc/.env")
    parser.add_argument("--jy-draftc-debug", action="store_true", help="给 jy-draftc.exe 传 --debug")
    parser.add_argument("--dump-nested-drafts", action="store_true", help="打印嵌套模板结构，第一次找下标时使用")
    parser.add_argument("--dump-effects", action="store_true", help="打印特效结构")
    return parser


def _load_job_file(job_path: str) -> dict:
    path = _positive_path(job_path, "任务 JSON")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"任务 JSON 顶层必须是对象: {path}")
    return data


def _value(data: dict, *keys: str, default=None):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def apply_job_config(args: argparse.Namespace) -> argparse.Namespace:
    if not args.job:
        args.job_video = None
        args.job_texts = None
        args.job_audios = None
        args.job_effects = None
        return args

    data = _load_job_file(args.job)
    args.source_kind = _value(data, "source_kind", default=args.source_kind)
    args.template_draft_dir = _value(data, "template_draft_dir", "template", default=args.template_draft_dir)
    args.draft_root = _value(data, "draft_root", "output_root", default=args.draft_root)
    args.draft_name = _value(data, "draft_name", "output_name", default=args.draft_name)
    args.output_mp4 = _value(data, "output_mp4", "output_path", default=args.output_mp4)
    args.skip_export = bool(_value(data, "skip_export", default=args.skip_export))
    args.dump_nested_drafts = bool(_value(data, "dump_nested_drafts", default=args.dump_nested_drafts))
    args.dump_effects = bool(_value(data, "dump_effects", default=args.dump_effects))

    export_config = data.get("export", {})
    if isinstance(export_config, dict):
        args.resolution = _value(export_config, "resolution", default=args.resolution)
        args.framerate = _value(export_config, "framerate", default=args.framerate)
        args.export_timeout = float(_value(export_config, "timeout", "export_timeout", default=args.export_timeout))

    source_config = data.get("source", {})
    if isinstance(source_config, dict):
        args.source_kind = str(_value(source_config, "type", "kind", default=args.source_kind)).replace("_", "-")
        if args.source_kind == "template":
            args.template_draft_dir = _value(
                source_config,
                "template_draft_dir",
                "template",
                "draft_dir",
                default=args.template_draft_dir,
            )
        elif args.source_kind == "video":
            args.input_video = _value(
                source_config,
                "media_path",
                "video_path",
                "input_video",
                default=args.input_video,
            )
        args.base_draft_work_root = _value(
            source_config,
            "base_draft_work_root",
            "work_root",
            default=args.base_draft_work_root,
        )
        args.base_draft_name = _value(source_config, "base_draft_name", "draft_name", default=args.base_draft_name)
        args.canvas_width = int(_value(source_config, "canvas_width", "width", default=args.canvas_width))
        args.canvas_height = int(_value(source_config, "canvas_height", "height", default=args.canvas_height))
        args.canvas_fps = int(_value(source_config, "canvas_fps", "fps", default=args.canvas_fps))

    decrypt_config = data.get("decrypt", {})
    if isinstance(decrypt_config, dict):
        enabled = _value(decrypt_config, "enabled", "auto_decrypt", default=None)
        if enabled is not None:
            args.no_auto_decrypt = not bool(enabled)
        args.force_decrypt = bool(_value(decrypt_config, "force", "force_decrypt", default=args.force_decrypt))
        args.decrypt_work_root = _value(decrypt_config, "work_root", "decrypt_work_root", default=args.decrypt_work_root)
        args.jy_draftc_exe = _value(decrypt_config, "exe", "jy_draftc_exe", default=args.jy_draftc_exe)
        args.jy_install_dir = _value(decrypt_config, "install_dir", "jy_install_dir", default=args.jy_install_dir)
        args.jy_draftc_debug = bool(_value(decrypt_config, "debug", default=args.jy_draftc_debug))

    args.job_video = data.get("video")
    args.job_texts = data.get("texts")
    args.job_audios = data.get("audios", data.get("audio"))
    args.job_effects = data.get("effects", data.get("effect"))
    return args


def _list_config(value, label: str) -> list[dict]:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise RuntimeError(f"{label} 配置必须是对象或对象列表")


def _build_video_replacements(
    args: argparse.Namespace,
) -> tuple[list[VideoSegmentReplacement], list[NestedVideoReplacement]]:
    video_replacements: list[VideoSegmentReplacement] = []
    nested_replacements: list[NestedVideoReplacement] = []

    if args.job_video is not None:
        video_config = args.job_video
        if not isinstance(video_config, dict):
            raise RuntimeError("video 配置必须是对象")
        kind = str(_value(video_config, "type", "target_kind", default="none")).replace("_", "-")
        media_path = _value(video_config, "media_path", "input_video", default="")
    else:
        kind = args.target_kind
        media_path = args.input_video
        if getattr(args, "_created_from_video_source", False) and kind == "none":
            return video_replacements, nested_replacements
        if media_path and kind == "none":
            kind = "video-segment"
        video_config = {}

    if kind in ("none", ""):
        return video_replacements, nested_replacements
    if not media_path:
        raise RuntimeError("配置了视频替换时必须提供 input_video 或 video.media_path")

    input_video = _positive_path(str(media_path), "输入视频/图片")
    source_start_us = int(_value(video_config, "source_start_us", default=args.source_start_us))
    source_duration_us = int(_value(video_config, "source_duration_us", default=args.source_duration_us))
    target_start_us = int(_value(video_config, "target_start_us", default=args.target_start_us))
    target_duration_us = int(_value(video_config, "target_duration_us", default=args.target_duration_us))

    if kind == "video-segment":
        video_replacements.append(
            VideoSegmentReplacement(
                media_path=input_video,
                track_index=int(_value(video_config, "track_index", "video_track_index", default=args.video_track_index)),
                segment_index=int(_value(video_config, "segment_index", "video_segment_index", default=args.video_segment_index)),
                source_start_us=source_start_us,
                source_duration_us=source_duration_us,
                target_start_us=target_start_us,
                target_duration_us=target_duration_us,
            )
        )
        return video_replacements, nested_replacements

    if kind == "nested-video":
        nested_replacements.append(
            NestedVideoReplacement(
                media_path=input_video,
                nested_draft_index=int(_value(video_config, "nested_draft_index", default=args.nested_draft_index)),
                video_track_index=int(
                    _value(video_config, "video_track_index", "nested_video_track_index", default=args.nested_video_track_index)
                ),
                segment_index=int(
                    _value(video_config, "segment_index", "nested_video_segment_index", default=args.nested_video_segment_index)
                ),
                source_start_us=source_start_us,
                source_duration_us=source_duration_us,
                target_start_us=target_start_us,
                target_duration_us=target_duration_us,
            )
        )
        return video_replacements, nested_replacements

    raise RuntimeError(f"不支持的视频替换类型: {kind!r}")


def _build_text_replacements(args: argparse.Namespace) -> tuple[
    list[TextReplacement],
    list[TextAddition],
    list[TextStylePresetReplacement],
    list[NestedTextStylePresetReplacement],
]:
    text_replacements: list[TextReplacement] = []
    text_additions: list[TextAddition] = []
    text_style_replacements: list[TextStylePresetReplacement] = []
    nested_text_style_replacements: list[NestedTextStylePresetReplacement] = []

    if args.job_texts is not None:
        configs = _list_config(args.job_texts, "texts")
    elif args.text or args.text_style_json:
        configs = [
            {
                "scope": args.text_scope,
                "type": args.text_mode,
                "text": args.text,
                "style_json_path": args.text_style_json,
                "track_index": args.text_track_index,
                "segment_index": args.text_segment_index,
                "track_name": args.text_track_name,
                "start_us": args.text_start_us,
                "duration_us": args.text_duration_us,
                "transform_x": args.text_transform_x,
                "transform_y": args.text_transform_y,
                "size": args.text_size,
                "nested_draft_index": args.nested_text_draft_index,
                "text_track_index": args.nested_text_track_index,
                "nested_segment_index": args.nested_text_segment_index,
                "apply_clip": True,
            }
        ]
    else:
        configs = []

    for item in configs:
        scope = str(_value(item, "scope", default="top")).lower()
        mode = str(_value(item, "type", "mode", default="replace")).replace("_", "-")
        text = str(_value(item, "text", default=""))
        style_json_path = _value(item, "style_json_path", "style_json", default="")
        apply_clip = bool(_value(item, "apply_clip", default=True))

        if mode == "add":
            if scope != "top":
                raise RuntimeError("新增文字当前只支持顶层 scope='top'")
            if not text:
                raise RuntimeError("新增文字必须提供 text")
            if style_json_path:
                _positive_path(str(style_json_path), "文本样式 JSON")
            text_additions.append(
                TextAddition(
                    text=text,
                    start_us=int(_value(item, "start_us", default=0)),
                    duration_us=int(_value(item, "duration_us", default=5_000_000)),
                    track_name=str(_value(item, "track_name", default="")),
                    style_json_path=style_json_path,
                    apply_clip=apply_clip,
                    relative_index=int(_value(item, "relative_index", default=999)),
                    transform_x=float(_value(item, "transform_x", default=0.0)),
                    transform_y=float(_value(item, "transform_y", default=0.0)),
                    size=float(_value(item, "size", default=8.0)),
                    align=int(_value(item, "align", default=1)),
                    auto_wrapping=bool(_value(item, "auto_wrapping", default=False)),
                )
            )
            continue

        if mode != "replace":
            raise RuntimeError(f"不支持的文字处理方式: {mode!r}")

        if scope == "top":
            track_index = int(_value(item, "track_index", "text_track_index", default=0))
            segment_index = int(_value(item, "segment_index", "text_segment_index", default=0))
            if style_json_path:
                _positive_path(str(style_json_path), "文本样式 JSON")
                text_style_replacements.append(
                    TextStylePresetReplacement(
                        style_json_path=style_json_path,
                        text=text,
                        apply_clip=apply_clip,
                        track_index=track_index,
                        segment_index=segment_index,
                    )
                )
            elif text:
                text_replacements.append(
                    TextReplacement(
                        text=text,
                        track_index=track_index,
                        segment_index=segment_index,
                    )
                )
            continue

        if scope == "nested":
            if not style_json_path:
                raise RuntimeError("嵌套模板文字替换暂时必须提供 style_json_path，用样式预设同时写入文字")
            _positive_path(str(style_json_path), "文本样式 JSON")
            nested_text_style_replacements.append(
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

    return text_replacements, text_additions, text_style_replacements, nested_text_style_replacements


def _build_audio_replacements(args: argparse.Namespace) -> tuple[
    list[NamedAudioReplacement],
    list[AudioSegmentReplacement],
    list[AudioAddition],
]:
    named_audio_replacements: list[NamedAudioReplacement] = []
    audio_segment_replacements: list[AudioSegmentReplacement] = []
    audio_additions: list[AudioAddition] = []

    if args.job_audios is not None:
        configs = _list_config(args.job_audios, "audios")
    elif args.audio_path:
        configs = [
            {
                "type": args.audio_mode,
                "media_path": args.audio_path,
                "material_name": args.audio_material_name,
                "track_index": args.audio_track_index,
                "segment_index": args.audio_segment_index,
                "source_start_us": args.audio_source_start_us,
                "source_duration_us": args.audio_source_duration_us,
                "target_start_us": args.audio_start_us,
                "target_duration_us": args.audio_duration_us,
            }
        ]
    else:
        configs = []

    for item in configs:
        mode = str(_value(item, "type", "mode", default="add")).replace("_", "-")
        media_path = _positive_path(str(_value(item, "media_path", "audio_path", default="")), "音频")

        if mode == "add":
            audio_additions.append(
                AudioAddition(
                    media_path=media_path,
                    source_start_us=int(_value(item, "source_start_us", default=-1)),
                    source_duration_us=int(_value(item, "source_duration_us", default=0)),
                    target_start_us=int(_value(item, "target_start_us", "start_us", default=0)),
                    target_duration_us=int(_value(item, "target_duration_us", "duration_us", default=0)),
                )
            )
        elif mode == "replace-segment":
            audio_segment_replacements.append(
                AudioSegmentReplacement(
                    media_path=media_path,
                    track_index=int(_value(item, "track_index", "audio_track_index", default=0)),
                    segment_index=int(_value(item, "segment_index", "audio_segment_index", default=0)),
                    source_start_us=int(_value(item, "source_start_us", default=-1)),
                    source_duration_us=int(_value(item, "source_duration_us", default=0)),
                    target_start_us=int(_value(item, "target_start_us", "start_us", default=-1)),
                    target_duration_us=int(_value(item, "target_duration_us", "duration_us", default=0)),
                )
            )
        elif mode == "replace-named":
            named_audio_replacements.append(
                NamedAudioReplacement(
                    media_path=media_path,
                    material_name=str(_value(item, "material_name", default="")),
                )
            )
        else:
            raise RuntimeError(f"不支持的音乐处理方式: {mode!r}")

    return named_audio_replacements, audio_segment_replacements, audio_additions


def _build_effect_additions(args: argparse.Namespace) -> list[EffectAddition]:
    effect_additions: list[EffectAddition] = []
    if args.job_effects is not None:
        configs = _list_config(args.job_effects, "effects")
    elif args.effect_json:
        configs = [
            {
                "effect_json_path": args.effect_json,
                "target_video_track_index": args.video_track_index,
                "target_video_segment_index": args.video_segment_index,
                "start_us": args.effect_start_us,
                "duration_us": args.effect_duration_us,
            }
        ]
    else:
        configs = []

    for item in configs:
        effect_json_path = _positive_path(
            str(_value(item, "effect_json_path", "effect_json", default="")),
            "特效 JSON",
        )
        effect_additions.append(
            EffectAddition(
                effect_json_path=effect_json_path,
                target_video_track_index=int(_value(item, "target_video_track_index", "video_track_index", default=0)),
                target_video_segment_index=int(_value(item, "target_video_segment_index", "video_segment_index", default=0)),
                start_us=int(_value(item, "start_us", default=-1)),
                duration_us=int(_value(item, "duration_us", default=0)),
            )
        )
    return effect_additions


def build_job(args: argparse.Namespace) -> ContentReplaceJob:
    args = apply_job_config(args)
    source_kind = str(args.source_kind or "auto").lower()
    if source_kind == "auto":
        source_kind = "template" if args.template_draft_dir else "video"

    args._created_from_video_source = False
    output_name_source = ""

    if source_kind == "template":
        if not args.template_draft_dir:
            raise RuntimeError("source-kind=template 时需要提供 --template-draft-dir 或 job.template_draft_dir")
        source_template_dir = Path(args.template_draft_dir).expanduser().resolve()
        if not source_template_dir.is_dir():
            raise NotADirectoryError(f"模板草稿目录不存在或不是目录: {source_template_dir}")

        prepared = prepare_plain_draft_dir(
            source_template_dir,
            auto_decrypt=not args.no_auto_decrypt,
            force_decrypt=args.force_decrypt,
            work_root=Path(args.decrypt_work_root).expanduser().resolve() if args.decrypt_work_root else None,
            exe=Path(args.jy_draftc_exe).expanduser().resolve() if args.jy_draftc_exe else None,
            install_dir=Path(args.jy_install_dir).expanduser().resolve() if args.jy_install_dir else None,
            debug=args.jy_draftc_debug,
        )
        template_dir = prepared.draft_dir
        output_name_source = source_template_dir.name
        if prepared.was_decrypted:
            print(f"模板草稿已自动解密到工作目录: {template_dir}")
        draft_root = Path(args.draft_root).expanduser().resolve() if args.draft_root else source_template_dir.parent
    elif source_kind == "video":
        if not args.input_video:
            raise RuntimeError("source-kind=video 时需要提供 --input-video 或 job.source.media_path")
        input_video = _positive_path(str(args.input_video), "输入视频/图片")
        base_root = (
            Path(args.base_draft_work_root).expanduser().resolve()
            if args.base_draft_work_root
            else PROJECT_ROOT / "_generated_video_drafts"
        )
        created = create_plain_draft_from_video(
            input_video,
            base_root,
            draft_name=args.base_draft_name,
            width=args.canvas_width,
            height=args.canvas_height,
            fps=args.canvas_fps,
            source_start_us=max(0, args.source_start_us),
            source_duration_us=args.source_duration_us,
        )
        template_dir = created.draft_dir
        output_name_source = input_video.stem
        args._created_from_video_source = True
        draft_root = (
            Path(args.draft_root).expanduser().resolve()
            if args.draft_root
            else PROJECT_ROOT / "_local_loop_test"
        )
    else:
        raise RuntimeError(f"不支持的 source-kind: {source_kind!r}")

    output_name = args.draft_name.strip()
    if not output_name:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_name = f"{output_name_source}_mp4_loop_{stamp}"

    video_replacements, nested_replacements = _build_video_replacements(args)
    text_replacements, text_additions, text_style_replacements, nested_text_style_replacements = _build_text_replacements(args)
    named_audio_replacements, audio_segment_replacements, audio_additions = _build_audio_replacements(args)
    effect_additions = _build_effect_additions(args)

    return ContentReplaceJob(
        template_draft_dir=template_dir,
        output_root=draft_root,
        output_name=output_name,
        dump_effects=args.dump_effects,
        dump_nested_drafts=args.dump_nested_drafts,
        video_segment_replacements=video_replacements,
        nested_video_replacements=nested_replacements,
        text_replacements=text_replacements,
        text_additions=text_additions,
        text_style_preset_replacements=text_style_replacements,
        nested_text_style_preset_replacements=nested_text_style_replacements,
        named_audio_replacements=named_audio_replacements,
        audio_segment_replacements=audio_segment_replacements,
        audio_additions=audio_additions,
        effect_additions=effect_additions,
    )


def export_mp4(args: argparse.Namespace, draft_name: str) -> Path:
    args = apply_job_config(args)
    if not args.output_mp4:
        raise RuntimeError("需要提供 --output-mp4 或 job.output_mp4 才能导出")
    output_mp4 = Path(args.output_mp4).expanduser().resolve()
    if output_mp4.exists():
        raise FileExistsError(f"输出 MP4 已存在，为避免覆盖已停止: {output_mp4}")
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    controller_type, resolution_type, framerate_type = _load_export_api()
    resolution = _enum_by_value(resolution_type, args.resolution, "分辨率")
    framerate = _enum_by_value(framerate_type, args.framerate, "帧率")

    controller = controller_type()
    controller.export_draft(
        draft_name,
        str(output_mp4),
        resolution=resolution,
        framerate=framerate,
        timeout=args.export_timeout,
    )
    return output_mp4


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run_content_replace_job(build_job(args))
        print(f"草稿已生成: {result.output_dir}")

        if args.skip_export:
            print("已跳过 MP4 导出。")
            return 0

        output_mp4 = export_mp4(args, result.output_name)
        print(f"MP4 已导出: {output_mp4}")
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
