from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


DRAFT_IMPORT_REPORT_SCHEMA = "jyd_probe.draft_import_report.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT / "data" / "libraries"
DEFAULT_HASH_LIMIT_BYTES = 32 * 1024 * 1024
IGNORED_DEPENDENCY_COLLECTIONS = {"transitions"}


def analyze_draft_import(
    data: dict[str, Any],
    *,
    source_draft_dir: str | Path,
    analyzed_draft_dir: str | Path | None = None,
    was_decrypted: bool = False,
    workspace_root: str | Path | None = None,
    hash_limit_bytes: int | None = DEFAULT_HASH_LIMIT_BYTES,
) -> dict[str, Any]:
    """Analyze editable slots and portable dependencies for one plaintext draft."""

    source_dir = Path(source_draft_dir).expanduser().resolve()
    analyzed_dir = (
        Path(analyzed_draft_dir).expanduser().resolve()
        if analyzed_draft_dir
        else source_dir
    )
    libraries_root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root
        else WORKSPACE_ROOT.resolve()
    )
    catalog = _build_central_catalog(libraries_root)

    contexts = list(_draft_contexts(data))
    slots = {
        "audio": [],
        "video_effects": [],
        "texts": [],
        "text_templates": [],
    }
    dependency_records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for scope, draft in contexts:
        _collect_slots(draft, scope, slots)
        _collect_dependencies(
            draft,
            scope=scope,
            draft_dir=analyzed_dir,
            catalog=catalog,
            hash_limit_bytes=hash_limit_bytes,
            records=dependency_records,
        )

    dependencies = sorted(
        dependency_records.values(),
        key=lambda item: (str(item.get("status", "")), str(item.get("kind", "")), str(item.get("path", ""))),
    )
    status_counts = Counter(str(item.get("status", "unknown")) for item in dependencies)
    kind_counts = Counter(str(item.get("kind", "unknown")) for item in dependencies)
    missing_required = [
        item
        for item in dependencies
        if item.get("status") == "missing" and not item.get("can_skip_if_replaced")
    ]
    if missing_required:
        warnings.append(f"有 {len(missing_required)} 个必须保留的本地资源缺失，当前草稿无法完整迁移")
    replaceable_missing = [
        item
        for item in dependencies
        if item.get("status") == "missing" and item.get("can_skip_if_replaced")
    ]
    if replaceable_missing:
        warnings.append(
            f"有 {len(replaceable_missing)} 个可替换资源缺失；确认对应槽位会被替换后可以继续"
        )

    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        tracks = []
    canvas = data.get("canvas_config", {})
    if not isinstance(canvas, dict):
        canvas = {}

    return {
        "schema": DRAFT_IMPORT_REPORT_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "draft": {
            "name": source_dir.name,
            "source_draft_dir": str(source_dir),
            "analyzed_draft_dir": str(analyzed_dir),
            "was_decrypted": bool(was_decrypted),
            "duration_us": int(data.get("duration", 0) or 0),
            "track_count": len(tracks),
            "nested_draft_count": max(0, len(contexts) - 1),
            "canvas": {
                "width": int(canvas.get("width", 0) or 0),
                "height": int(canvas.get("height", 0) or 0),
                "ratio": str(canvas.get("ratio", "")),
            },
            "version_fields": _draft_version_fields(data),
        },
        "editable_slots": slots,
        "dependencies": dependencies,
        "central_catalog": catalog["summary"],
        "summary": {
            "slot_counts": {key: len(value) for key, value in slots.items()},
            "dependency_count": len(dependencies),
            "dependency_status_counts": dict(sorted(status_counts.items())),
            "dependency_kind_counts": dict(sorted(kind_counts.items())),
            "upload_required_count": status_counts.get("upload_required", 0),
            "central_library_count": status_counts.get("central_library", 0),
            "missing_count": status_counts.get("missing", 0),
            "blocked_missing_count": len(missing_required),
            "ready_for_packaging": not missing_required,
        },
        "warnings": warnings,
    }


def _draft_contexts(
    data: dict[str, Any],
    scope: str = "top",
    *,
    depth: int = 0,
) -> Iterator[tuple[str, dict[str, Any]]]:
    yield scope, data
    if depth >= 8:
        return
    materials = data.get("materials", {})
    drafts = materials.get("drafts", []) if isinstance(materials, dict) else []
    if not isinstance(drafts, list):
        return
    for index, item in enumerate(drafts):
        if not isinstance(item, dict) or not isinstance(item.get("draft"), dict):
            continue
        nested_scope = f"{scope}.drafts[{index}]"
        yield from _draft_contexts(item["draft"], nested_scope, depth=depth + 1)


def _collect_slots(
    data: dict[str, Any],
    scope: str,
    result: dict[str, list[dict[str, Any]]],
) -> None:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        tracks = []

    audios = _material_map(materials.get("audios"))
    video_effects = _material_map(materials.get("video_effects"))
    texts = _material_map(materials.get("texts"))
    text_templates = _material_map(materials.get("text_templates"))

    audio_track_number = 0
    effect_track_number = 0
    text_track_number = 0
    for raw_track_index, track in enumerate(tracks):
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type", ""))
        track_name = str(track.get("name", ""))
        track_id = str(track.get("id", ""))
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            segments = []

        if track_type == "audio":
            for segment_index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    continue
                material = audios.get(str(segment.get("material_id", "")), {})
                audio_type = str(material.get("type", "")).lower()
                suggested_role = _suggest_audio_role(track_name, material, segment)
                result["audio"].append(
                    _slot_record(
                        scope=scope,
                        kind="audio",
                        role=suggested_role,
                        replace_mode="rebuild_segment",
                        raw_track_index=raw_track_index,
                        typed_track_index=audio_track_number,
                        segment_index=segment_index,
                        track=track,
                        segment=segment,
                        material=material,
                        details={
                            "name": str(material.get("name") or material.get("material_name") or ""),
                            "path": str(material.get("path", "")),
                            "audio_type": audio_type,
                            "volume": segment.get("volume", 1.0),
                        },
                    )
                )
            audio_track_number += 1
            continue

        if track_type == "effect":
            for segment_index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    continue
                material = video_effects.get(str(segment.get("material_id", "")), {})
                result["video_effects"].append(
                    _slot_record(
                        scope=scope,
                        kind="video_effect",
                        role="video_effect",
                        replace_mode="rebuild_segment",
                        raw_track_index=raw_track_index,
                        typed_track_index=effect_track_number,
                        segment_index=segment_index,
                        track=track,
                        segment=segment,
                        material=material,
                        details={
                            "name": str(material.get("name", "")),
                            "effect_type": str(material.get("type", "")),
                        },
                    )
                )
            effect_track_number += 1
            continue

        if track_type != "text":
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            material_id = str(segment.get("material_id", ""))
            if material_id in text_templates:
                material = text_templates[material_id]
                template_texts = _template_texts(material, texts)
                result["text_templates"].append(
                    _slot_record(
                        scope=scope,
                        kind="text_template",
                        role="composite_text_template",
                        replace_mode="update_template_slots_or_replace_segment",
                        raw_track_index=raw_track_index,
                        typed_track_index=text_track_number,
                        segment_index=segment_index,
                        track=track,
                        segment=segment,
                        material=material,
                        details={
                            "name": str(material.get("name", "")),
                            "texts": template_texts,
                            "text_slot_count": len(template_texts),
                        },
                    )
                )
            else:
                material = texts.get(material_id, {})
                parsed = _parse_content(material.get("content"))
                styles = parsed.get("styles", []) if isinstance(parsed, dict) else []
                has_flower = any(
                    isinstance(style, dict) and isinstance(style.get("effectStyle"), dict)
                    for style in styles
                ) if isinstance(styles, list) else False
                result["texts"].append(
                    _slot_record(
                        scope=scope,
                        kind="text",
                        role="text_style",
                        replace_mode="update_text_material",
                        raw_track_index=raw_track_index,
                        typed_track_index=text_track_number,
                        segment_index=segment_index,
                        track=track,
                        segment=segment,
                        material=material,
                        details={
                            "text": str(parsed.get("text", "")) if isinstance(parsed, dict) else "",
                            "has_flower_text": has_flower,
                            "editable_fields": ["text", "font", "size", "color", "position", "flower_text"],
                        },
                    )
                )
        text_track_number += 1


def _slot_record(
    *,
    scope: str,
    kind: str,
    role: str,
    replace_mode: str,
    raw_track_index: int,
    typed_track_index: int,
    segment_index: int,
    track: dict[str, Any],
    segment: dict[str, Any],
    material: dict[str, Any],
    details: dict[str, Any],
) -> dict[str, Any]:
    segment_id = str(segment.get("id", ""))
    material_id = str(segment.get("material_id", ""))
    slot_suffix = segment_id or f"{raw_track_index}_{segment_index}_{material_id}"
    return {
        "slot_id": f"{scope}:{kind}:{slot_suffix}",
        "kind": kind,
        "suggested_role": role,
        "replace_mode": replace_mode,
        "selected_for_replacement": False,
        "selector": {
            "scope": scope,
            "track_id": str(track.get("id", "")),
            "track_name": str(track.get("name", "")),
            "raw_track_index": raw_track_index,
            "typed_track_index": typed_track_index,
            "segment_id": segment_id,
            "segment_index": segment_index,
            "material_id": material_id,
        },
        "target_timerange": _timerange(segment.get("target_timerange")),
        "source_timerange": _timerange(segment.get("source_timerange")),
        "identifiers": _identifiers(material),
        **details,
    }


def _collect_dependencies(
    data: dict[str, Any],
    *,
    scope: str,
    draft_dir: Path,
    catalog: dict[str, Any],
    hash_limit_bytes: int | None,
    records: dict[str, dict[str, Any]],
) -> None:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return
    for collection, values in materials.items():
        if collection in IGNORED_DEPENDENCY_COLLECTIONS:
            continue
        if not isinstance(values, list):
            continue
        for material_index, material in enumerate(values):
            if not isinstance(material, dict):
                continue
            material_id = str(material.get("id", ""))
            material_identifiers = _identifiers(material)
            for path_value, pointer, nearby_identifiers in _walk_paths(material):
                dependency_kind = _dependency_kind(collection, pointer, path_value)
                identifiers = _merge_identifiers(material_identifiers, nearby_identifiers)
                resolved_path, external = _resolve_dependency_path(path_value, draft_dir)
                key = f"external:{path_value}" if external else _path_key(resolved_path)
                can_skip = dependency_kind in {
                    "audio",
                    "sound_effect",
                    "video_effect",
                    "text_effect",
                    "text_template_resource",
                    "font",
                }
                record = records.get(key)
                if record is None:
                    record = _dependency_record(
                        original_path=path_value,
                        resolved_path=resolved_path,
                        external=external,
                        kind=dependency_kind,
                        identifiers=identifiers,
                        catalog=catalog,
                        hash_limit_bytes=hash_limit_bytes,
                        can_skip_if_replaced=can_skip,
                    )
                    record["references"] = []
                    records[key] = record
                else:
                    record["can_skip_if_replaced"] = bool(
                        record.get("can_skip_if_replaced") and can_skip
                    )
                    record["identifiers"] = _merge_identifiers(
                        record.get("identifiers", {}), identifiers
                    )
                reference = {
                    "scope": scope,
                    "material_collection": str(collection),
                    "material_index": material_index,
                    "material_id": material_id,
                    "json_pointer": pointer,
                }
                if reference not in record["references"]:
                    record["references"].append(reference)


def _dependency_record(
    *,
    original_path: str,
    resolved_path: Path | None,
    external: bool,
    kind: str,
    identifiers: dict[str, str],
    catalog: dict[str, Any],
    hash_limit_bytes: int | None,
    can_skip_if_replaced: bool,
) -> dict[str, Any]:
    if external:
        return {
            "kind": kind,
            "path": original_path,
            "original_path": original_path,
            "status": "external",
            "exists": False,
            "is_directory": False,
            "size_bytes": 0,
            "checksum_sha256": "",
            "checksum_status": "not_local",
            "identifiers": identifiers,
            "central_match": None,
            "can_skip_if_replaced": can_skip_if_replaced,
        }

    assert resolved_path is not None
    exists = resolved_path.exists()
    is_directory = exists and resolved_path.is_dir()
    size_bytes = resolved_path.stat().st_size if exists and resolved_path.is_file() else 0
    checksum = ""
    checksum_status = "not_computed"
    if exists and resolved_path.is_file():
        if hash_limit_bytes is None or size_bytes <= hash_limit_bytes:
            checksum = _sha256_file(resolved_path)
            checksum_status = "computed"
        else:
            checksum_status = "skipped_size_limit"

    central_match = _match_central_library(
        kind=kind,
        path=resolved_path,
        identifiers=identifiers,
        checksum=checksum,
        catalog=catalog,
    )
    if central_match is not None:
        status = "central_library"
    elif exists:
        status = "upload_required"
    else:
        status = "missing"
    return {
        "kind": kind,
        "path": str(resolved_path),
        "original_path": original_path,
        "status": status,
        "exists": exists,
        "is_directory": is_directory,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
        "checksum_status": checksum_status,
        "identifiers": identifiers,
        "central_match": central_match,
        "can_skip_if_replaced": can_skip_if_replaced,
    }


def _build_central_catalog(workspace_root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    manifest_specs = [
        (
            "audio",
            workspace_root / "audio_library" / "manifest" / "audio_manifest.json",
            "assets",
        ),
        (
            "text_effect",
            workspace_root / "text_effect_library" / "manifest" / "text_effect_manifest.json",
            "effects",
        ),
        (
            "text_template",
            workspace_root / "text_template_library" / "manifest" / "text_template_manifest.json",
            "templates",
        ),
        (
            "sticker",
            workspace_root / "sticker_library" / "manifest" / "sticker_manifest.json",
            "stickers",
        ),
        (
            "font",
            workspace_root / "font_library" / "manifest" / "font_manifest.json",
            "fonts",
        ),
    ]
    for kind, path, list_key in manifest_specs:
        data = _read_json_object(path)
        values = data.get(list_key, []) if data else []
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict):
                records.append(_catalog_record(kind, value, path.parent.parent))

    effect_root = workspace_root / "effect_library"
    if effect_root.exists():
        for path in sorted(effect_root.glob("*.json")):
            data = _read_json_object(path)
            material = data.get("material", {}) if data else {}
            if not isinstance(material, dict):
                continue
            value = {
                "identity": _first_identity(material),
                "name": material.get("name") or path.stem,
                "effect_id": material.get("effect_id", ""),
                "resource_id": material.get("resource_id", ""),
                "metadata_file": str(path),
            }
            records.append(_catalog_record("video_effect", value, effect_root))

    by_token: dict[str, list[dict[str, Any]]] = {}
    by_checksum: dict[str, list[dict[str, Any]]] = {}
    roots: set[str] = set()
    for record in records:
        for token in _identifier_tokens(record.get("identifiers", {})):
            by_token.setdefault(f"{record['kind']}|{token}", []).append(record)
        checksum = str(record.get("checksum_sha256", "")).lower()
        if checksum:
            by_checksum.setdefault(checksum, []).append(record)
        root = str(record.get("library_root", ""))
        if root:
            roots.add(root)
    kind_counts = Counter(str(record.get("kind", "unknown")) for record in records)
    return {
        "records": records,
        "by_token": by_token,
        "by_checksum": by_checksum,
        "roots": sorted(roots),
        "summary": {
            "workspace_root": str(workspace_root),
            "record_count": len(records),
            "kind_counts": dict(sorted(kind_counts.items())),
        },
    }


def _catalog_record(kind: str, value: dict[str, Any], library_root: Path) -> dict[str, Any]:
    identifiers = _identifiers(value)
    identity = str(value.get("identity", "")).strip()
    if identity and ":" in identity:
        key, token_value = identity.split(":", 1)
        identifiers.setdefault(key, token_value)
    library_file = ""
    if kind == "font":
        relative_file = str(value.get("file", "")).strip()
        if relative_file:
            candidate = (library_root / relative_file).resolve()
            try:
                candidate.relative_to(library_root.resolve())
            except ValueError:
                pass
            else:
                if candidate.is_file():
                    library_file = relative_file.replace("\\", "/")
    return {
        "kind": kind,
        "identity": identity or _first_identity(value),
        "name": str(value.get("name", "")),
        "checksum_sha256": str(value.get("checksum_sha256", "")),
        "metadata_file": str(value.get("metadata_file", "")),
        "library_file": library_file,
        "library_root": str(library_root.resolve()),
        "identifiers": identifiers,
    }


def _match_central_library(
    *,
    kind: str,
    path: Path,
    identifiers: dict[str, str],
    checksum: str,
    catalog: dict[str, Any],
) -> dict[str, Any] | None:
    resolved = path.resolve(strict=False)
    for root_value in catalog.get("roots", []):
        root = Path(root_value)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return {
            "match_type": "library_path",
            "kind": kind,
            "identity": "",
            "name": resolved.name,
        }

    preferred_kinds = _central_kinds_for_dependency(kind)
    for token in _identifier_tokens(identifiers):
        for catalog_kind in preferred_kinds:
            matches = catalog.get("by_token", {}).get(f"{catalog_kind}|{token}", [])
            if matches:
                return _central_match_payload(matches[0], "identifier")
    if checksum:
        matches = catalog.get("by_checksum", {}).get(checksum.lower(), [])
        if matches:
            return _central_match_payload(matches[0], "checksum")
    return None


def _central_match_payload(record: dict[str, Any], match_type: str) -> dict[str, Any]:
    return {
        "match_type": match_type,
        "kind": record.get("kind", ""),
        "identity": record.get("identity", ""),
        "name": record.get("name", ""),
        "metadata_file": record.get("metadata_file", ""),
        "library_file": record.get("library_file", ""),
        "checksum_sha256": record.get("checksum_sha256", ""),
    }


def _walk_paths(value: Any, pointer: str = "") -> Iterator[tuple[str, str, dict[str, str]]]:
    if isinstance(value, dict):
        nearby = _identifiers(value)
        if "id" in value and str(value.get("id", "")).isdigit():
            nearby.setdefault("resource_id", str(value["id"]))
            nearby.setdefault("effect_id", str(value["id"]))
        for key, item in value.items():
            child_pointer = f"{pointer}/{key}"
            if isinstance(item, str) and (key == "path" or key.endswith("_path")) and item.strip():
                yield item.strip(), child_pointer, nearby
            elif key == "content" and isinstance(item, str):
                parsed = _parse_content(item)
                if parsed:
                    yield from _walk_paths(parsed, f"{child_pointer}$json")
            else:
                yield from _walk_paths(item, child_pointer)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_paths(item, f"{pointer}/{index}")


def _resolve_dependency_path(value: str, draft_dir: Path) -> tuple[Path | None, bool]:
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme.lower() not in {"file"} and len(parsed.scheme) > 1:
        return None, True
    path = Path(parsed.path if parsed.scheme.lower() == "file" else value).expanduser()
    if not path.is_absolute():
        path = draft_dir / path
    return path.resolve(strict=False), False


def _dependency_kind(collection: str, pointer: str, path_value: str) -> str:
    lower_pointer = pointer.lower()
    suffix = Path(path_value).suffix.lower()
    if collection == "videos":
        return "video"
    if collection == "audios":
        return "audio"
    if collection == "video_effects":
        return "video_effect"
    if collection == "effects":
        return "video_adjustment"
    if "effectstyle" in lower_pointer:
        return "text_effect"
    if collection == "text_templates":
        return "text_template_resource"
    if "font" in lower_pointer or suffix in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
        return "font"
    if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio"
    if suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
        return "video"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return "image"
    if collection == "stickers":
        return "sticker"
    return "resource"


def _central_kinds_for_dependency(kind: str) -> list[str]:
    return {
        "audio": ["audio"],
        "sound_effect": ["audio"],
        "video_effect": ["video_effect"],
        "text_effect": ["text_effect"],
        "text_template_resource": ["text_template"],
        "sticker": ["sticker"],
    }.get(kind, [kind])


def _suggest_audio_role(
    track_name: str,
    material: dict[str, Any],
    segment: dict[str, Any],
) -> str:
    combined = " ".join(
        [
            track_name,
            str(material.get("name", "")),
            str(material.get("material_name", "")),
            str(material.get("type", "")),
        ]
    ).lower()
    if any(token in combined for token in ("音效", "sound effect", "sfx")):
        return "sound_effect"
    timerange = _timerange(segment.get("target_timerange"))
    if timerange["duration"] and timerange["duration"] <= 10_000_000:
        return "bgm_or_sound_effect"
    return "bgm"


def _template_texts(
    template: dict[str, Any],
    texts: dict[str, dict[str, Any]],
) -> list[str]:
    result: list[str] = []
    resources = template.get("text_info_resources", [])
    if not isinstance(resources, list):
        return result
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        material = texts.get(str(resource.get("text_material_id", "")), {})
        content = _parse_content(material.get("content"))
        result.append(str(content.get("text", "")) if isinstance(content, dict) else "")
    return result


def _parse_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip().startswith(("{", "[")):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _material_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {
        str(item["id"]): item
        for item in value
        if isinstance(item, dict) and item.get("id")
    }


def _timerange(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"start": 0, "duration": 0}
    return {
        "start": int(value.get("start", 0) or 0),
        "duration": int(value.get("duration", 0) or 0),
    }


def _identifiers(value: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("music_id", "resource_id", "effect_id", "third_resource_id"):
        token = str(value.get(key, "")).strip()
        if token and token != "0":
            result[key] = token
    return result


def _merge_identifiers(*values: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        for key, item in value.items():
            token = str(item).strip()
            if token and token != "0":
                result.setdefault(str(key), token)
    return result


def _identifier_tokens(value: dict[str, str]) -> list[str]:
    return [f"{key}:{item}" for key, item in sorted(value.items()) if item]


def _first_identity(value: dict[str, Any]) -> str:
    identifiers = _identifiers(value)
    for key in ("music_id", "resource_id", "effect_id", "third_resource_id"):
        if key in identifiers:
            return f"{key}:{identifiers[key]}"
    return ""


def _draft_version_fields(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "version",
        "app_version",
        "draft_version",
        "new_version",
        "last_modified_platform",
        "platform",
    ):
        if key in data and data[key] not in (None, ""):
            result[key] = data[key]
    return result


def _path_key(path: Path | None) -> str:
    return str(path or "").replace("\\", "/").casefold()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
