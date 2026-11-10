from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .cli import audio_tracks


AUDIO_MANIFEST_SCHEMA = "jyd_probe.audio_library_manifest.v1"
AUDIO_METADATA_SCHEMA = "jyd_probe.audio_asset.v1"


@dataclass(frozen=True)
class AudioExportResult:
    output_dir: Path
    manifest_path: Path
    scanned_segment_count: int
    encountered_asset_count: int
    copied_count: int
    existing_count: int
    duplicate_segment_count: int
    missing_file_count: int
    assets: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "scanned_segment_count": self.scanned_segment_count,
            "encountered_asset_count": self.encountered_asset_count,
            "copied_count": self.copied_count,
            "existing_count": self.existing_count,
            "duplicate_segment_count": self.duplicate_segment_count,
            "missing_file_count": self.missing_file_count,
            "assets": self.assets,
        }


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_audio"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_materials_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        return {}
    audios = materials.get("audios", [])
    if not isinstance(audios, list):
        return {}
    return {
        str(item.get("id")): item
        for item in audios
        if isinstance(item, dict) and item.get("id")
    }


def _asset_identity(material: dict[str, Any], checksum: str) -> str:
    music_id = str(material.get("music_id", "")).strip()
    if music_id:
        return f"music_id:{music_id}"
    resource_id = str(material.get("resource_id", "")).strip()
    if resource_id:
        return f"resource_id:{resource_id}"
    return f"sha256:{checksum}"


def _asset_stem(material: dict[str, Any], identity: str) -> str:
    name = str(material.get("name", "")).strip() or str(material.get("material_name", "")).strip()
    name = name or "未命名音频"
    identity_value = identity.split(":", 1)[-1]
    return f"{_safe_filename(name)}_{_safe_filename(identity_value[:24])}"


def _source_ref(
    *,
    source_label: str,
    audio_track_index: int,
    raw_track_index: int,
    segment_index: int,
    segment: dict[str, Any],
    material_id: str,
    source_path: Path,
) -> dict[str, Any]:
    return {
        "label": source_label,
        "audio_track_index": audio_track_index,
        "raw_track_index": raw_track_index,
        "segment_index": segment_index,
        "material_id": material_id,
        "source_path": str(source_path),
        "source_timerange": segment.get("source_timerange"),
        "target_timerange": segment.get("target_timerange"),
        "volume": segment.get("volume", 1.0),
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": AUDIO_MANIFEST_SCHEMA, "assets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": AUDIO_MANIFEST_SCHEMA, "assets": []}
    if not isinstance(data, dict) or data.get("schema") != AUDIO_MANIFEST_SCHEMA:
        return {"schema": AUDIO_MANIFEST_SCHEMA, "assets": []}
    if not isinstance(data.get("assets"), list):
        data["assets"] = []
    return data


def export_audio_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_label: str = "",
    replace: bool = False,
) -> AudioExportResult:
    """Copy all top-level timeline audio assets into a persistent local library."""

    root = Path(output_dir).expanduser().resolve()
    files_dir = root / "files"
    metadata_dir = root / "metadata"
    manifest_dir = root / "manifest"
    files_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_dir / "audio_manifest.json"
    manifest = _load_manifest(manifest_path)
    stored_assets = [item for item in manifest.get("assets", []) if isinstance(item, dict)]
    stored_by_identity = {
        str(item.get("identity")): item
        for item in stored_assets
        if item.get("identity")
    }
    stored_by_checksum = {
        str(item.get("checksum_sha256")): item
        for item in stored_assets
        if item.get("checksum_sha256")
    }

    materials_by_id = _audio_materials_by_id(data)
    scanned = 0
    copied = 0
    existing = 0
    duplicates = 0
    missing = 0
    encountered: list[dict[str, Any]] = []
    encountered_identities: set[str] = set()

    for audio_track_index, (raw_track_index, track) in enumerate(audio_tracks(data)):
        segments = track.get("segments", [])
        if not isinstance(segments, list):
            continue
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            scanned += 1
            material_id = str(segment.get("material_id", ""))
            material = materials_by_id.get(material_id)
            if material is None:
                missing += 1
                continue

            source_path = Path(str(material.get("path", ""))).expanduser()
            if not source_path.exists() or not source_path.is_file():
                missing += 1
                continue
            source_path = source_path.resolve()
            checksum = _sha256(source_path)
            identity = _asset_identity(material, checksum)
            checksum_match = stored_by_checksum.get(checksum)
            if checksum_match is not None and checksum_match.get("identity"):
                identity = str(checksum_match["identity"])
            source = _source_ref(
                source_label=source_label,
                audio_track_index=audio_track_index,
                raw_track_index=raw_track_index,
                segment_index=segment_index,
                segment=segment,
                material_id=material_id,
                source_path=source_path,
            )

            if identity in encountered_identities:
                duplicates += 1
                stored = stored_by_identity.get(identity)
                if stored is not None:
                    stored.setdefault("sources", []).append(source)
                    metadata_relative = str(stored.get("metadata_file", ""))
                    if metadata_relative:
                        duplicate_metadata_path = root / metadata_relative
                        if duplicate_metadata_path.exists():
                            try:
                                duplicate_metadata = json.loads(
                                    duplicate_metadata_path.read_text(encoding="utf-8")
                                )
                            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                                duplicate_metadata = {}
                            if isinstance(duplicate_metadata, dict):
                                duplicate_metadata["sources"] = stored["sources"]
                                duplicate_metadata_path.write_text(
                                    json.dumps(duplicate_metadata, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8",
                                )
                continue
            encountered_identities.add(identity)

            stored = stored_by_identity.get(identity)
            suffix = source_path.suffix.lower() or ".audio"
            if stored is not None and stored.get("file"):
                destination = root / str(stored["file"])
                metadata_path = root / str(stored.get("metadata_file", ""))
            else:
                stem = _asset_stem(material, identity)
                destination = files_dir / f"{stem}{suffix}"
                metadata_path = metadata_dir / f"{stem}.json"

            if replace or not destination.exists():
                shutil.copy2(source_path, destination)
                copied += 1
                status = "copied"
            else:
                existing += 1
                status = "existing"

            if stored is None:
                stored = {
                    "identity": identity,
                    "name": str(material.get("name", "") or material.get("material_name", "")),
                    "music_id": str(material.get("music_id", "")),
                    "resource_id": str(material.get("resource_id", "")),
                    "original_type": str(material.get("type", "")),
                    "duration_us": int(material.get("duration", 0) or 0),
                    "checksum_sha256": checksum,
                    "file": destination.relative_to(root).as_posix(),
                    "metadata_file": metadata_path.relative_to(root).as_posix(),
                    "sources": [],
                }
                stored_assets.append(stored)
                stored_by_identity[identity] = stored
                stored_by_checksum[checksum] = stored
            stored.setdefault("sources", []).append(source)

            metadata = {
                "schema": AUDIO_METADATA_SCHEMA,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "identity": identity,
                "checksum_sha256": checksum,
                "file": destination.relative_to(root).as_posix(),
                "material": material,
                "sources": stored["sources"],
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            encountered.append({**stored, "status": status})

    if scanned == 0:
        raise RuntimeError("草稿中没有找到任何顶层 audio 轨道片段")
    if not encountered and missing == scanned:
        raise RuntimeError("找到了音频片段，但素材文件路径都不存在或无法关联")

    manifest.update(
        {
            "schema": AUDIO_MANIFEST_SCHEMA,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "assets": stored_assets,
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return AudioExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        scanned_segment_count=scanned,
        encountered_asset_count=len(encountered_identities),
        copied_count=copied,
        existing_count=existing,
        duplicate_segment_count=duplicates,
        missing_file_count=missing,
        assets=encountered,
    )
