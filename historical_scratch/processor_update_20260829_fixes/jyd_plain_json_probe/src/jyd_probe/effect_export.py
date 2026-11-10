from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from .cli import effect_material_label, effect_tracks, material_index_by_id, video_effect_materials


EFFECT_SCHEMA = "jyd_probe.video_effect.v1"
EFFECT_MANIFEST_SCHEMA = "jyd_probe.effect_library_manifest.v1"


@dataclass(frozen=True)
class EffectExportResult:
    output_dir: Path
    manifest_path: Path
    scanned_segment_count: int
    exported_count: int
    existing_count: int
    duplicate_count: int
    missing_material_count: int
    effects: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "scanned_segment_count": self.scanned_segment_count,
            "exported_count": self.exported_count,
            "existing_count": self.existing_count,
            "duplicate_count": self.duplicate_count,
            "missing_material_count": self.missing_material_count,
            "effects": self.effects,
        }


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_effect"


def _effect_identity(material: dict[str, Any]) -> str:
    for key in ("resource_id", "effect_id", "id"):
        value = str(material.get(key, "")).strip()
        if value:
            return f"{key}:{value}"
    return ""


def _effect_filename(material: dict[str, Any]) -> str:
    name = str(material.get("name", "")).strip() or "未命名特效"
    identity = (
        str(material.get("effect_id", "")).strip()
        or str(material.get("resource_id", "")).strip()
        or str(material.get("id", "")).strip()
    )
    identity_suffix = f"_{_safe_filename(identity[-24:])}" if identity else ""
    return f"{_safe_filename(name)}{identity_suffix}.json"


def export_effect_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_label: str = "",
    replace: bool = False,
) -> EffectExportResult:
    """Export every referenced top-level video effect into reusable JSON files."""

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    materials = video_effect_materials(data)
    material_indexes = material_index_by_id(materials)
    existing_by_identity: dict[str, Path] = {}
    for existing_path in root.glob("*.json"):
        try:
            existing_data = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(existing_data, dict) or existing_data.get("schema") != EFFECT_SCHEMA:
            continue
        existing_material = existing_data.get("material")
        if not isinstance(existing_material, dict):
            continue
        existing_identity = _effect_identity(existing_material)
        if existing_identity:
            existing_by_identity.setdefault(existing_identity, existing_path)

    scanned = 0
    duplicates = 0
    missing = 0
    exported = 0
    existing = 0
    seen: set[str] = set()
    records: list[dict[str, Any]] = []

    for effect_track_index, (raw_track_index, track) in enumerate(effect_tracks(data)):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            scanned += 1
            material_id = str(segment.get("material_id", ""))
            material_index = material_indexes.get(material_id)
            if material_index is None:
                missing += 1
                continue
            material = materials[material_index]
            identity = _effect_identity(material) or f"material_id:{material_id}"
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)

            output_path = existing_by_identity.get(identity, root / _effect_filename(material))
            payload = {
                "schema": EFFECT_SCHEMA,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "effect_label": effect_material_label(material),
                "source": {
                    "label": source_label,
                    "effect_track_index": effect_track_index,
                    "raw_track_index": raw_track_index,
                    "segment_index": segment_index,
                    "material_id": material_id,
                    "target_timerange": deepcopy(segment.get("target_timerange")),
                },
                "material": deepcopy(material),
                "segment_template": deepcopy(segment),
            }

            status = "existing"
            if replace or not output_path.exists():
                output_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=4) + "\n",
                    encoding="utf-8",
                )
                exported += 1
                status = "exported"
                existing_by_identity[identity] = output_path
            else:
                existing += 1

            records.append(
                {
                    "name": str(material.get("name", "")),
                    "effect_id": str(material.get("effect_id", "")),
                    "resource_id": str(material.get("resource_id", "")),
                    "identity": identity,
                    "file": output_path.name,
                    "status": status,
                    "source": payload["source"],
                }
            )

    if not records and scanned == 0:
        raise RuntimeError("草稿中没有找到任何顶层 effect 轨道片段")
    if not records and missing:
        raise RuntimeError("找到了特效片段，但都无法关联到 materials.video_effects 素材")

    manifest_dir = root / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "effect_manifest.json"
    manifest = {
        "schema": EFFECT_MANIFEST_SCHEMA,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source": source_label,
        "scanned_segment_count": scanned,
        "unique_effect_count": len(records),
        "duplicate_count": duplicates,
        "missing_material_count": missing,
        "effects": records,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return EffectExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scanned_segment_count=scanned,
        exported_count=exported,
        existing_count=existing,
        duplicate_count=duplicates,
        missing_material_count=missing,
        effects=records,
    )
