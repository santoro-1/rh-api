from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "libraries"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.draft_crypto import prepare_plain_draft_dir  # noqa: E402


try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def draft_content_path(draft_dir: Path) -> Path:
    path = draft_dir / "draft_content.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到 draft_content.json: {path}")
    return path


def list_json_library(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        item: dict[str, Any] = {
            "name": path.stem,
            "path": str(path.resolve()),
        }
        try:
            data = read_json(path)
        except Exception as exc:
            item["error"] = str(exc)
            result.append(item)
            continue

        if isinstance(data, dict):
            item["schema"] = data.get("schema", "")
            item["label"] = data.get("effect_label", "") or data.get("source", {}).get("label", "")
            material = data.get("material")
            if isinstance(material, dict):
                item["effect_name"] = material.get("name", "")
                item["effect_id"] = material.get("effect_id", "")
                item["resource_id"] = material.get("resource_id", "")
        result.append(item)
    return result


def tracks_of_type(data: dict[str, Any], track_type: str) -> list[tuple[int, dict[str, Any]]]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        return []
    return [
        (index, track)
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("type") == track_type
    ]


def material_by_id(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            result[str(item["id"])] = item
    return result


def timerange(segment: dict[str, Any]) -> dict[str, int]:
    raw = segment.get("target_timerange")
    if not isinstance(raw, dict):
        return {"start": 0, "duration": 0}
    return {
        "start": int(raw.get("start", 0) or 0),
        "duration": int(raw.get("duration", 0) or 0),
    }


def text_materials_for_id(materials: dict[str, Any], material_id: str) -> list[dict[str, Any]]:
    texts = materials.get("texts", [])
    if not isinstance(texts, list):
        texts = []

    direct = [item for item in texts if isinstance(item, dict) and str(item.get("id", "")) == material_id]
    if direct:
        return direct

    text_templates = materials.get("text_templates", [])
    if not isinstance(text_templates, list):
        return []

    sub_ids: list[str] = []
    for template in text_templates:
        if not isinstance(template, dict) or str(template.get("id", "")) != material_id:
            continue
        resources = template.get("text_info_resources", [])
        if not isinstance(resources, list):
            continue
        for resource in resources:
            if isinstance(resource, dict) and resource.get("text_material_id"):
                sub_ids.append(str(resource["text_material_id"]))

    return [item for item in texts if isinstance(item, dict) and str(item.get("id", "")) in sub_ids]


def text_preview(materials: dict[str, Any], material_id: str) -> str:
    text_materials = text_materials_for_id(materials, material_id)
    if not text_materials:
        return ""
    content = text_materials[0].get("content", "")
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content[:80]
    if isinstance(parsed, dict):
        return str(parsed.get("text", ""))[:120]
    return ""


def collect_text_segments(
    data: dict[str, Any],
    *,
    scope: str,
    nested_draft_index: int | None = None,
) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}

    result: list[dict[str, Any]] = []
    for text_track_index, (raw_track_index, track) in enumerate(tracks_of_type(data, "text")):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material_id = str(segment.get("material_id", ""))
            item: dict[str, Any] = {
                "scope": scope,
                "track_index": text_track_index,
                "raw_track_index": raw_track_index,
                "segment_index": segment_index,
                "material_id": material_id,
                "text": text_preview(materials, material_id),
                "target_timerange": timerange(segment),
            }
            if nested_draft_index is not None:
                item["nested_draft_index"] = nested_draft_index
            result.append(item)
    return result


def collect_audio_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    audios_by_id = material_by_id(materials.get("audios", []))

    result: list[dict[str, Any]] = []
    for audio_track_index, (raw_track_index, track) in enumerate(tracks_of_type(data, "audio")):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material = audios_by_id.get(str(segment.get("material_id", "")), {})
            result.append(
                {
                    "track_index": audio_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": segment.get("material_id", ""),
                    "name": material.get("name", "") or material.get("material_name", ""),
                    "path": material.get("path", ""),
                    "target_timerange": timerange(segment),
                }
            )
    return result


def collect_video_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    videos_by_id = material_by_id(materials.get("videos", []))

    result: list[dict[str, Any]] = []
    for video_track_index, (raw_track_index, track) in enumerate(tracks_of_type(data, "video")):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material = videos_by_id.get(str(segment.get("material_id", "")), {})
            result.append(
                {
                    "track_index": video_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": segment.get("material_id", ""),
                    "name": material.get("material_name", "") or material.get("name", ""),
                    "path": material.get("path", ""),
                    "type": material.get("type", ""),
                    "target_timerange": timerange(segment),
                }
            )
    return result


def collect_effect_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    effects_by_id = material_by_id(materials.get("video_effects", []))

    result: list[dict[str, Any]] = []
    for effect_track_index, (raw_track_index, track) in enumerate(tracks_of_type(data, "effect")):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material = effects_by_id.get(str(segment.get("material_id", "")), {})
            result.append(
                {
                    "track_index": effect_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": segment.get("material_id", ""),
                    "name": material.get("name", ""),
                    "effect_id": material.get("effect_id", ""),
                    "resource_id": material.get("resource_id", ""),
                    "target_timerange": timerange(segment),
                }
            )
    return result


def nested_drafts(data: dict[str, Any]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return []
    drafts = materials.get("drafts", [])
    if not isinstance(drafts, list):
        return []

    result: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(drafts):
        if isinstance(item, dict) and isinstance(item.get("draft"), dict):
            result.append((index, item, item["draft"]))
    return result


def inspect_draft(
    draft_dir: Path,
    *,
    source_dir: Path | None = None,
    was_decrypted: bool = False,
) -> dict[str, Any]:
    data = read_json(draft_content_path(draft_dir))
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        tracks = []

    nested_text_segments: list[dict[str, Any]] = []
    nested_video_segments: list[dict[str, Any]] = []
    for nested_index, draft_material, nested in nested_drafts(data):
        nested_name = draft_material.get("name", "")
        for item in collect_text_segments(nested, scope="nested", nested_draft_index=nested_index):
            item["nested_name"] = nested_name
            nested_text_segments.append(item)
        for item in collect_video_segments(nested):
            item["scope"] = "nested"
            item["nested_draft_index"] = nested_index
            item["nested_name"] = nested_name
            nested_video_segments.append(item)

    return {
        "source_draft_dir": str((source_dir or draft_dir).resolve()),
        "draft_dir": str(draft_dir.resolve()),
        "draft_name": draft_dir.name,
        "was_decrypted": was_decrypted,
        "duration": data.get("duration", 0),
        "summary": {
            "track_count": len(tracks),
            "material_counts": {
                key: len(value)
                for key, value in sorted(materials.items())
                if isinstance(value, list)
                and key in {"videos", "audios", "texts", "text_templates", "drafts", "video_effects", "effects"}
            },
        },
        "targets": {
            "text_segments": collect_text_segments(data, scope="top"),
            "nested_text_segments": nested_text_segments,
            "audio_segments": collect_audio_segments(data),
            "effect_apply_video_segments": collect_video_segments(data),
            "nested_video_segments": nested_video_segments,
            "existing_effect_segments": collect_effect_segments(data),
        },
        "libraries": {
            "text_styles": list_json_library(WORKSPACE_ROOT / "text_style_library"),
            "effects": list_json_library(WORKSPACE_ROOT / "effect_library"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查剪映草稿里可替换的文字、音乐和特效目标。")
    parser.add_argument("--draft-dir", required=True, help="草稿目录，里面必须有 draft_content.json")
    parser.add_argument("--output", default="", help="可选：把检查结果写入 JSON 文件")
    parser.add_argument("--no-auto-decrypt", action="store_true", help="关闭自动调用 jy-draftc 解密")
    parser.add_argument("--force-decrypt", action="store_true", help="强制复制到工作目录并调用 jy-draftc 解密")
    parser.add_argument("--decrypt-work-root", default="", help="自动解密工作目录，不填则使用项目内 _decrypted_work")
    parser.add_argument("--jy-draftc-exe", default="", help="jy-draftc.exe 路径，不填则使用同级 jy-draftc 项目")
    parser.add_argument("--jy-install-dir", default="", help="包含 videoeditor.dll 的剪映安装目录，不填则使用 jy-draftc/.env")
    parser.add_argument("--jy-draftc-debug", action="store_true", help="给 jy-draftc.exe 传 --debug")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        draft_dir = Path(args.draft_dir).expanduser().resolve()
        prepared = prepare_plain_draft_dir(
            draft_dir,
            auto_decrypt=not args.no_auto_decrypt,
            force_decrypt=args.force_decrypt,
            work_root=Path(args.decrypt_work_root).expanduser().resolve() if args.decrypt_work_root else None,
            exe=Path(args.jy_draftc_exe).expanduser().resolve() if args.jy_draftc_exe else None,
            install_dir=Path(args.jy_install_dir).expanduser().resolve() if args.jy_install_dir else None,
            debug=args.jy_draftc_debug,
        )
        if prepared.was_decrypted:
            print(f"草稿已自动解密到工作目录: {prepared.draft_dir}", file=sys.stderr)
        result = inspect_draft(
            prepared.draft_dir,
            source_dir=prepared.source_dir,
            was_decrypted=prepared.was_decrypted,
        )
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text + "\n", encoding="utf-8")
            print(f"检查结果已写入: {output_path}")
        else:
            print(text)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
