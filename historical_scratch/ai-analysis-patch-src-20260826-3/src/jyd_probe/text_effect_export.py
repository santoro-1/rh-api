from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .cli import text_tracks


TEXT_EFFECT_SCHEMA = "jyd_probe.text_effect.v1"
TEXT_EFFECT_MANIFEST_SCHEMA = "jyd_probe.text_effect_library_manifest.v1"


@dataclass(frozen=True)
class TextEffectExportResult:
    output_dir: Path
    manifest_path: Path
    scanned_text_segment_count: int
    flower_text_segment_count: int
    encountered_effect_count: int
    exported_count: int
    existing_count: int
    duplicate_reference_count: int
    missing_effect_material_count: int
    missing_resource_count: int
    effects: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "scanned_text_segment_count": self.scanned_text_segment_count,
            "flower_text_segment_count": self.flower_text_segment_count,
            "encountered_effect_count": self.encountered_effect_count,
            "exported_count": self.exported_count,
            "existing_count": self.existing_count,
            "duplicate_reference_count": self.duplicate_reference_count,
            "missing_effect_material_count": self.missing_effect_material_count,
            "missing_resource_count": self.missing_resource_count,
            "effects": self.effects,
        }


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_text_effect"


def _materials(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return []
    values = materials.get(key, [])
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def _parse_text_content(material: dict[str, Any]) -> dict[str, Any]:
    content = material.get("content", "")
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_effect_identity(material: dict[str, Any]) -> str:
    for key in ("resource_id", "effect_id", "third_resource_id", "id"):
        value = str(material.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    path = str(material.get("path", "")).strip()
    return f"path:{Path(path).as_posix().lower()}" if path else ""


def _is_text_effect(material: dict[str, Any]) -> bool:
    return str(material.get("type", "")).strip().lower() == "text_effect"


def _effect_tokens(material: dict[str, Any]) -> set[str]:
    tokens = {
        str(material.get(key, "")).strip()
        for key in ("id", "resource_id", "effect_id", "third_resource_id")
    }
    path = str(material.get("path", "")).strip()
    if path:
        tokens.add(Path(path).as_posix().lower())
    return {token for token in tokens if token}


def _effect_style_tokens(effect_style: dict[str, Any]) -> set[str]:
    tokens = {
        str(effect_style.get(key, "")).strip()
        for key in ("id", "resource_id", "effect_id")
    }
    path = str(effect_style.get("path", "")).strip()
    if path:
        tokens.add(Path(path).as_posix().lower())
    return {token for token in tokens if token}


def _effect_styles(content: dict[str, Any]) -> list[dict[str, Any]]:
    styles = content.get("styles", [])
    if not isinstance(styles, list):
        return []
    result: list[dict[str, Any]] = []
    for style in styles:
        if not isinstance(style, dict):
            continue
        effect_style = style.get("effectStyle")
        if isinstance(effect_style, dict) and effect_style:
            result.append(effect_style)
    return result


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": TEXT_EFFECT_MANIFEST_SCHEMA, "effects": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": TEXT_EFFECT_MANIFEST_SCHEMA, "effects": []}
    if not isinstance(data, dict) or data.get("schema") != TEXT_EFFECT_MANIFEST_SCHEMA:
        return {"schema": TEXT_EFFECT_MANIFEST_SCHEMA, "effects": []}
    if not isinstance(data.get("effects"), list):
        data["effects"] = []
    return data


def _bundle_stem(material: dict[str, Any], identity: str) -> str:
    name = str(material.get("name", "")).strip() or "未命名花字"
    suffix = identity.split(":", 1)[-1]
    return f"{_safe_filename(name)}_{_safe_filename(suffix[-24:])}"


def _copy_effect_resource(
    source_value: str,
    bundle_dir: Path,
    *,
    replace: bool,
) -> tuple[dict[str, Any], bool]:
    source = Path(source_value).expanduser() if source_value else Path()
    resource = {
        "original_path": source_value,
        "library_path": "",
        "kind": "",
        "status": "missing",
    }
    if not source_value or not source.exists():
        return resource, False

    source = source.resolve()
    resources_dir = bundle_dir / "resources"
    resources_dir.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        destination = resources_dir / "effect"
        if replace and destination.exists():
            shutil.rmtree(destination)
        if destination.exists():
            resource_status = "existing"
        else:
            shutil.copytree(source, destination)
            resource_status = "copied"
        kind = "directory"
    elif source.is_file():
        destination = resources_dir / source.name
        if replace or not destination.exists():
            shutil.copy2(source, destination)
            resource_status = "copied"
        else:
            resource_status = "existing"
        kind = "file"
    else:
        return resource, False

    resource.update(
        {
            "library_path": destination.relative_to(bundle_dir).as_posix(),
            "kind": kind,
            "status": resource_status,
        }
    )
    return resource, True


def _source_key(source: dict[str, Any]) -> tuple[Any, ...]:
    return (
        source.get("label"),
        source.get("raw_track_index"),
        source.get("segment_index"),
        source.get("material_id"),
        source.get("effect_material_id"),
    )


def _append_unique_sources(
    existing: list[dict[str, Any]],
    additions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = [item for item in existing if isinstance(item, dict)]
    seen = {_source_key(item) for item in result}
    for item in additions:
        key = _source_key(item)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def export_text_effect_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_label: str = "",
    replace: bool = False,
) -> TextEffectExportResult:
    """Export standalone text effects (Jianying flower text) and their resource folders."""

    root = Path(output_dir).expanduser().resolve()
    bundles_dir = root / "bundles"
    manifest_dir = root / "manifest"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_dir / "text_effect_manifest.json"
    manifest = _load_manifest(manifest_path)
    stored_effects = [item for item in manifest.get("effects", []) if isinstance(item, dict)]
    stored_by_identity = {
        str(item.get("identity")): item
        for item in stored_effects
        if item.get("identity")
    }

    text_materials = _materials(data, "texts")
    text_by_id = {
        str(item.get("id")): item
        for item in text_materials
        if item.get("id")
    }
    effect_materials = [item for item in _materials(data, "effects") if _is_text_effect(item)]
    effects_by_material_id: dict[str, list[dict[str, Any]]] = {}
    effects_by_token: dict[str, list[dict[str, Any]]] = {}
    for effect in effect_materials:
        material_id = str(effect.get("id", "")).strip()
        if material_id:
            indexed = effects_by_material_id.setdefault(material_id, [])
            identity = _text_effect_identity(effect)
            if all(_text_effect_identity(item) != identity for item in indexed):
                indexed.append(effect)
        for token in _effect_tokens(effect):
            indexed = effects_by_token.setdefault(token, [])
            identity = _text_effect_identity(effect)
            if all(_text_effect_identity(item) != identity for item in indexed):
                indexed.append(effect)

    scanned = 0
    flower_segments = 0
    duplicates = 0
    missing_effect_materials = 0
    missing_resources = 0
    exported = 0
    existing = 0
    candidates: dict[str, dict[str, Any]] = {}

    for text_track_index, (raw_track_index, track) in enumerate(text_tracks(data)):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            scanned += 1
            material_id = str(segment.get("material_id", "")).strip()
            text_material = text_by_id.get(material_id)
            # Composite text templates have a text_templates material_id. They belong
            # to the template collector and must not leak into the standalone library.
            if text_material is None:
                continue

            content = _parse_text_content(text_material)
            effect_styles = _effect_styles(content)
            matched: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            seen_matches: set[str] = set()

            refs = segment.get("extra_material_refs", [])
            if isinstance(refs, list):
                for ref in refs:
                    for effect in effects_by_material_id.get(str(ref), []):
                        identity = _text_effect_identity(effect)
                        if identity and identity not in seen_matches:
                            matched.append((effect, None))
                            seen_matches.add(identity)
                        elif identity:
                            duplicates += 1

            for effect_style in effect_styles:
                style_matches: dict[str, dict[str, Any]] = {}
                for token in _effect_style_tokens(effect_style):
                    for effect in effects_by_token.get(token, []):
                        identity = _text_effect_identity(effect)
                        if identity:
                            style_matches.setdefault(identity, effect)
                if style_matches:
                    for identity, effect in style_matches.items():
                        identity = _text_effect_identity(effect)
                        if identity and identity not in seen_matches:
                            matched.append((effect, effect_style))
                            seen_matches.add(identity)
                else:
                    missing_effect_materials += 1
                    fallback_id = str(effect_style.get("id", "")).strip()
                    fallback_path = str(effect_style.get("path", "")).strip()
                    fallback = {
                        "id": "",
                        "resource_id": fallback_id,
                        "effect_id": fallback_id,
                        "name": "未命名花字",
                        "path": fallback_path,
                        "type": "text_effect",
                    }
                    identity = _text_effect_identity(fallback)
                    if identity and identity not in seen_matches:
                        matched.append((fallback, effect_style))
                        seen_matches.add(identity)

            if not matched:
                continue
            flower_segments += 1
            for effect, matched_style in matched:
                identity = _text_effect_identity(effect)
                if not identity:
                    continue
                source = {
                    "label": source_label,
                    "text_track_index": text_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": material_id,
                    "effect_material_id": str(effect.get("id", "")),
                    "text": str(content.get("text", "")),
                    "target_timerange": deepcopy(segment.get("target_timerange")),
                    "effect_style": deepcopy(matched_style or (effect_styles[0] if effect_styles else {})),
                }
                candidate = candidates.get(identity)
                if candidate is None:
                    candidates[identity] = {
                        "identity": identity,
                        "material": deepcopy(effect),
                        "sample_text_material": deepcopy(text_material),
                        "segment_template": deepcopy(segment),
                        "sources": [source],
                    }
                else:
                    duplicates += 1
                    candidate["sources"] = _append_unique_sources(candidate["sources"], [source])

    records: list[dict[str, Any]] = []
    for identity, candidate in candidates.items():
        material = candidate["material"]
        stored = stored_by_identity.get(identity)
        if stored is not None and stored.get("bundle"):
            bundle_dir = root / str(stored["bundle"])
        else:
            bundle_dir = bundles_dir / _bundle_stem(material, identity)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = bundle_dir / "text_effect.json"

        previous_payload: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("schema") == TEXT_EFFECT_SCHEMA:
                    previous_payload = loaded
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass

        sources = _append_unique_sources(
            previous_payload.get("sources", []) if isinstance(previous_payload.get("sources"), list) else [],
            candidate["sources"],
        )
        resource, resource_found = _copy_effect_resource(
            str(material.get("path", "")),
            bundle_dir,
            replace=replace,
        )
        if not resource_found:
            missing_resources += 1

        payload = {
            "schema": TEXT_EFFECT_SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "identity": identity,
            "name": str(material.get("name", "")) or "未命名花字",
            "material": material,
            "resource": resource,
            "sample_text_material": candidate["sample_text_material"],
            "segment_template": candidate["segment_template"],
            "sources": sources,
        }
        status = "existing"
        if replace or not metadata_path.exists():
            status = "exported"
            exported += 1
        else:
            existing += 1
        # Sources and resource status can change even when the effect itself existed.
        metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        record = {
            "identity": identity,
            "name": payload["name"],
            "effect_id": str(material.get("effect_id", "")),
            "resource_id": str(material.get("resource_id", "")),
            "third_resource_id": str(material.get("third_resource_id", "")),
            "bundle": bundle_dir.relative_to(root).as_posix(),
            "metadata_file": metadata_path.relative_to(root).as_posix(),
            "resource": resource,
            "source_count": len(sources),
            "status": status if resource_found else f"{status}_resource_missing",
        }
        records.append(record)
        if stored is None:
            stored_effects.append({key: value for key, value in record.items() if key != "status"})
        else:
            stored.clear()
            stored.update({key: value for key, value in record.items() if key != "status"})

    if scanned == 0:
        raise RuntimeError("草稿中没有找到顶层文字轨道片段")
    if not candidates:
        raise RuntimeError("没有找到普通文字片段直接使用的花字效果；复合文字模板不会由此工具采集")

    manifest.update(
        {
            "schema": TEXT_EFFECT_MANIFEST_SCHEMA,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "effects": stored_effects,
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return TextEffectExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scanned_text_segment_count=scanned,
        flower_text_segment_count=flower_segments,
        encountered_effect_count=len(candidates),
        exported_count=exported,
        existing_count=existing,
        duplicate_reference_count=duplicates,
        missing_effect_material_count=missing_effect_materials,
        missing_resource_count=missing_resources,
        effects=records,
    )
