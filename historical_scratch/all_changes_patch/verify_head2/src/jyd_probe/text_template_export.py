from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .cli import text_tracks


TEXT_TEMPLATE_SCHEMA = "jyd_probe.text_template.v1"
TEXT_TEMPLATE_MANIFEST_SCHEMA = "jyd_probe.text_template_library_manifest.v1"


@dataclass(frozen=True)
class TextTemplateExportResult:
    output_dir: Path
    manifest_path: Path
    scanned_text_segment_count: int
    encountered_template_count: int
    exported_count: int
    existing_count: int
    duplicate_segment_count: int
    text_slot_count: int
    missing_text_material_count: int
    unresolved_reference_count: int
    missing_resource_count: int
    templates: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "scanned_text_segment_count": self.scanned_text_segment_count,
            "encountered_template_count": self.encountered_template_count,
            "exported_count": self.exported_count,
            "existing_count": self.existing_count,
            "duplicate_segment_count": self.duplicate_segment_count,
            "text_slot_count": self.text_slot_count,
            "missing_text_material_count": self.missing_text_material_count,
            "unresolved_reference_count": self.unresolved_reference_count,
            "missing_resource_count": self.missing_resource_count,
            "templates": self.templates,
        }


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_text_template"


def _materials_dict(data: dict[str, Any]) -> dict[str, Any]:
    materials = data.get("materials", {})
    return materials if isinstance(materials, dict) else {}


def _material_list(materials: dict[str, Any], key: str) -> list[dict[str, Any]]:
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


def _template_identity(template: dict[str, Any]) -> str:
    for key in ("resource_id", "effect_id", "id"):
        value = str(template.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    return ""


def _bundle_stem(template: dict[str, Any], identity: str) -> str:
    name = str(template.get("name", "")).strip() or "未命名复合文字模板"
    suffix = identity.split(":", 1)[-1]
    return f"{_safe_filename(name)}_{_safe_filename(suffix[-24:])}"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": TEXT_TEMPLATE_MANIFEST_SCHEMA, "templates": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": TEXT_TEMPLATE_MANIFEST_SCHEMA, "templates": []}
    if not isinstance(data, dict) or data.get("schema") != TEXT_TEMPLATE_MANIFEST_SCHEMA:
        return {"schema": TEXT_TEMPLATE_MANIFEST_SCHEMA, "templates": []}
    if not isinstance(data.get("templates"), list):
        data["templates"] = []
    return data


def _collect_extra_material_refs(value: Any, result: set[str]) -> None:
    if isinstance(value, dict):
        refs = value.get("extra_material_refs", [])
        if isinstance(refs, list):
            result.update(str(item) for item in refs if item)
        for item in value.values():
            _collect_extra_material_refs(item, result)
    elif isinstance(value, list):
        for item in value:
            _collect_extra_material_refs(item, result)


def _path_key(value: str) -> str:
    return value.strip().replace("\\", "/").casefold()


def _collect_paths(
    value: Any,
    location: str,
    result: dict[str, dict[str, Any]],
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            item_location = f"{location}.{key}" if location else key
            if (
                isinstance(item, str)
                and item.strip()
                and (key == "path" or key.endswith("_path"))
            ):
                normalized = _path_key(item)
                record = result.setdefault(
                    normalized,
                    {"original_path": item, "locations": []},
                )
                if item_location not in record["locations"]:
                    record["locations"].append(item_location)
            _collect_paths(item, item_location, result)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect_paths(item, f"{location}[{index}]", result)


def _resource_destination(source: Path, resources_dir: Path, original_path: str) -> Path:
    digest = hashlib.sha256(_path_key(original_path).encode("utf-8")).hexdigest()[:12]
    name = _safe_filename(source.name) or "resource"
    return resources_dir / f"{digest}_{name}"


def _copy_resource(
    record: dict[str, Any],
    resources_dir: Path,
) -> tuple[dict[str, Any], bool]:
    original_path = str(record.get("original_path", ""))
    source = Path(original_path).expanduser() if original_path else Path()
    output = {
        "original_path": original_path,
        "library_path": "",
        "kind": "",
        "status": "missing",
        "locations": list(record.get("locations", [])),
    }
    if not original_path or not source.exists():
        return output, False

    source = source.resolve()
    destination = _resource_destination(source, resources_dir, original_path)
    if source.is_dir():
        if destination.exists():
            status = "existing"
        else:
            shutil.copytree(source, destination)
            status = "copied"
        kind = "directory"
    elif source.is_file():
        if destination.exists():
            status = "existing"
        else:
            shutil.copy2(source, destination)
            status = "copied"
        kind = "file"
    else:
        return output, False

    output.update(
        {
            "library_path": destination.relative_to(resources_dir.parent).as_posix(),
            "kind": kind,
            "status": status,
        }
    )
    return output, True


def _source_key(source: dict[str, Any]) -> tuple[Any, ...]:
    return (
        source.get("label"),
        source.get("raw_track_index"),
        source.get("segment_index"),
        source.get("material_id"),
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


def _build_material_index(materials: dict[str, Any]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    result: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen: set[tuple[str, str]] = set()
    for category, values in materials.items():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            material_id = str(item["id"])
            key = (category, material_id)
            if key in seen:
                continue
            seen.add(key)
            result.setdefault(material_id, []).append((category, item))
    return result


def _build_template_graph(
    template: dict[str, Any],
    segment: dict[str, Any],
    text_by_id: dict[str, dict[str, Any]],
    material_index: dict[str, list[tuple[str, dict[str, Any]]]],
) -> tuple[dict[str, Any], int, int]:
    slots: list[dict[str, Any]] = []
    missing_text_materials = 0
    text_info_resources = template.get("text_info_resources", [])
    if not isinstance(text_info_resources, list):
        text_info_resources = []
    for slot_index, text_info in enumerate(text_info_resources):
        if not isinstance(text_info, dict):
            continue
        text_material_id = str(text_info.get("text_material_id", ""))
        text_material = text_by_id.get(text_material_id)
        if text_material is None:
            missing_text_materials += 1
            slots.append(
                {
                    "slot_index": slot_index,
                    "text_material_id": text_material_id,
                    "text": "",
                    "text_info_resource": deepcopy(text_info),
                    "text_material": None,
                    "text_content": {},
                }
            )
            continue
        content = _parse_text_content(text_material)
        slots.append(
            {
                "slot_index": slot_index,
                "text_material_id": text_material_id,
                "text": str(content.get("text", "")),
                "text_info_resource": deepcopy(text_info),
                "text_material": deepcopy(text_material),
                "text_content": content,
            }
        )

    reference_ids: set[str] = set()
    _collect_extra_material_refs(segment, reference_ids)
    _collect_extra_material_refs(template, reference_ids)
    referenced_materials: dict[str, list[dict[str, Any]]] = {}
    unresolved: list[str] = []
    for reference_id in sorted(reference_ids):
        matches = material_index.get(reference_id, [])
        if not matches:
            unresolved.append(reference_id)
            continue
        for category, material in matches:
            referenced_materials.setdefault(category, []).append(deepcopy(material))

    graph = {
        "template": deepcopy(template),
        "segment_template": deepcopy(segment),
        "text_slots": slots,
        "reference_ids": sorted(reference_ids),
        "referenced_materials": referenced_materials,
        "unresolved_reference_ids": unresolved,
    }
    return graph, missing_text_materials, len(unresolved)


def export_text_template_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_label: str = "",
    replace: bool = False,
) -> TextTemplateExportResult:
    """Export composite Jianying text templates and their complete local resources."""

    root = Path(output_dir).expanduser().resolve()
    bundles_dir = root / "bundles"
    manifest_dir = root / "manifest"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_dir / "text_template_manifest.json"
    manifest = _load_manifest(manifest_path)
    stored_templates = [item for item in manifest.get("templates", []) if isinstance(item, dict)]
    stored_by_identity = {
        str(item.get("identity")): item
        for item in stored_templates
        if item.get("identity")
    }

    materials = _materials_dict(data)
    templates = _material_list(materials, "text_templates")
    template_by_id = {
        str(item.get("id")): item
        for item in templates
        if item.get("id")
    }
    text_by_id = {
        str(item.get("id")): item
        for item in _material_list(materials, "texts")
        if item.get("id")
    }
    material_index = _build_material_index(materials)

    scanned = 0
    duplicates = 0
    total_slots = 0
    missing_text_materials = 0
    unresolved_references = 0
    exported = 0
    existing = 0
    missing_resources = 0
    candidates: dict[str, dict[str, Any]] = {}

    for text_track_index, (raw_track_index, track) in enumerate(text_tracks(data)):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            scanned += 1
            material_id = str(segment.get("material_id", ""))
            template = template_by_id.get(material_id)
            if template is None:
                continue
            identity = _template_identity(template)
            if not identity:
                continue
            source = {
                "label": source_label,
                "text_track_index": text_track_index,
                "raw_track_index": raw_track_index,
                "segment_index": segment_index,
                "material_id": material_id,
                "target_timerange": deepcopy(segment.get("target_timerange")),
            }
            candidate = candidates.get(identity)
            if candidate is not None:
                duplicates += 1
                candidate["sources"] = _append_unique_sources(candidate["sources"], [source])
                continue

            graph, missing_slots, unresolved = _build_template_graph(
                template,
                segment,
                text_by_id,
                material_index,
            )
            total_slots += len(graph["text_slots"])
            missing_text_materials += missing_slots
            unresolved_references += unresolved
            candidates[identity] = {
                "identity": identity,
                "graph": graph,
                "sources": [source],
            }

    records: list[dict[str, Any]] = []
    for identity, candidate in candidates.items():
        graph = candidate["graph"]
        template = graph["template"]
        stored = stored_by_identity.get(identity)
        if stored is not None and stored.get("bundle"):
            bundle_dir = root / str(stored["bundle"])
        else:
            bundle_dir = bundles_dir / _bundle_stem(template, identity)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = bundle_dir / "text_template.json"
        resources_dir = bundle_dir / "resources"
        if replace and resources_dir.exists():
            shutil.rmtree(resources_dir)
        resources_dir.mkdir(parents=True, exist_ok=True)

        previous_payload: dict[str, Any] = {}
        if metadata_path.exists():
            try:
                loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("schema") == TEXT_TEMPLATE_SCHEMA:
                    previous_payload = loaded
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        previous_sources = previous_payload.get("sources", [])
        sources = _append_unique_sources(
            previous_sources if isinstance(previous_sources, list) else [],
            candidate["sources"],
        )

        path_records: dict[str, dict[str, Any]] = {}
        _collect_paths(graph["template"], "template", path_records)
        _collect_paths(graph["text_slots"], "text_slots", path_records)
        _collect_paths(graph["referenced_materials"], "referenced_materials", path_records)
        resource_records: list[dict[str, Any]] = []
        template_missing_resources = 0
        for path_record in path_records.values():
            copied, found = _copy_resource(path_record, resources_dir)
            resource_records.append(copied)
            if not found:
                template_missing_resources += 1
        missing_resources += template_missing_resources

        payload = {
            "schema": TEXT_TEMPLATE_SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "identity": identity,
            "name": str(template.get("name", "")) or "未命名复合文字模板",
            **graph,
            "resources": resource_records,
            "sources": sources,
        }
        status = "existing"
        if replace or not metadata_path.exists():
            exported += 1
            status = "exported"
        else:
            existing += 1
        metadata_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        record = {
            "identity": identity,
            "name": payload["name"],
            "effect_id": str(template.get("effect_id", "")),
            "resource_id": str(template.get("resource_id", "")),
            "bundle": bundle_dir.relative_to(root).as_posix(),
            "metadata_file": metadata_path.relative_to(root).as_posix(),
            "text_slot_count": len(graph["text_slots"]),
            "reference_count": len(graph["reference_ids"]),
            "unresolved_reference_count": len(graph["unresolved_reference_ids"]),
            "resource_count": len(resource_records),
            "missing_resource_count": template_missing_resources,
            "source_count": len(sources),
            "status": status if not template_missing_resources else f"{status}_resource_missing",
        }
        records.append(record)
        stored_value = {key: value for key, value in record.items() if key != "status"}
        if stored is None:
            stored_templates.append(stored_value)
        else:
            stored.clear()
            stored.update(stored_value)

    if scanned == 0:
        raise RuntimeError("草稿中没有找到顶层文字轨道片段")
    if not candidates:
        raise RuntimeError("没有找到 materials.text_templates 对应的复合文字模板片段")

    manifest.update(
        {
            "schema": TEXT_TEMPLATE_MANIFEST_SCHEMA,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "templates": stored_templates,
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return TextTemplateExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scanned_text_segment_count=scanned,
        encountered_template_count=len(candidates),
        exported_count=exported,
        existing_count=existing,
        duplicate_segment_count=duplicates,
        text_slot_count=total_slots,
        missing_text_material_count=missing_text_materials,
        unresolved_reference_count=unresolved_references,
        missing_resource_count=missing_resources,
        templates=records,
    )
