from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
from typing import Any

from fontTools.ttLib import TTFont

from .draft_import_analyzer import analyze_draft_import


FONT_MANIFEST_SCHEMA = "jyd_probe.font_library_manifest.v1"
FONT_METADATA_SCHEMA = "jyd_probe.font_asset.v1"


@dataclass(frozen=True)
class FontExportResult:
    output_dir: Path
    manifest_path: Path
    encountered_count: int
    copied_count: int
    existing_count: int
    missing_count: int
    fonts: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "encountered_count": self.encountered_count,
            "copied_count": self.copied_count,
            "existing_count": self.existing_count,
            "missing_count": self.missing_count,
            "fonts": self.fonts,
        }


def export_font_library(
    data: dict[str, Any],
    output_dir: str | Path,
    *,
    source_draft_dir: str | Path,
    analyzed_draft_dir: str | Path | None = None,
    source_label: str = "",
    replace: bool = False,
) -> FontExportResult:
    """Copy every existing font dependency in a draft into a reusable font library."""

    root = Path(output_dir).expanduser().resolve()
    files_dir = root / "files"
    metadata_dir = root / "metadata"
    manifest_dir = root / "manifest"
    for directory in (files_dir, metadata_dir, manifest_dir):
        directory.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_dir / "font_manifest.json"
    manifest = _load_manifest(manifest_path)
    stored_fonts = [item for item in manifest.get("fonts", []) if isinstance(item, dict)]
    by_identity = {
        str(item.get("identity", "")): item
        for item in stored_fonts
        if item.get("identity")
    }
    by_checksum = {
        str(item.get("checksum_sha256", "")): item
        for item in stored_fonts
        if item.get("checksum_sha256")
    }

    report = analyze_draft_import(
        data,
        source_draft_dir=source_draft_dir,
        analyzed_draft_dir=analyzed_draft_dir,
        workspace_root=root.parent,
        hash_limit_bytes=None,
    )
    dependencies = [
        item
        for item in report.get("dependencies", [])
        if isinstance(item, dict) and item.get("kind") == "font"
    ]

    copied = 0
    existing = 0
    missing = 0
    encountered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dependency in dependencies:
        source_path = Path(str(dependency.get("path", ""))).expanduser()
        if not source_path.is_file():
            missing += 1
            continue
        source_path = source_path.resolve()
        display_name = source_path.stem
        if _generic_font_name(display_name):
            display_name = _font_display_name(source_path) or display_name
        checksum = str(dependency.get("checksum_sha256", ""))
        identifiers = dependency.get("identifiers", {})
        if not isinstance(identifiers, dict):
            identifiers = {}
        identity = _font_identity(identifiers, checksum)
        checksum_match = by_checksum.get(checksum)
        if checksum_match is not None and checksum_match.get("identity"):
            identity = str(checksum_match["identity"])
        if identity in seen:
            continue
        seen.add(identity)

        stored = by_identity.get(identity)
        suffix = source_path.suffix.lower() or ".font"
        stem = f"{_safe_filename(source_path.stem)}_{_safe_filename(identity.split(':', 1)[-1][:24])}"
        if stored and stored.get("file"):
            destination = root / str(stored["file"])
            metadata_path = root / str(stored.get("metadata_file", ""))
        else:
            destination = files_dir / f"{stem}{suffix}"
            metadata_path = metadata_dir / f"{stem}.json"

        if replace or not destination.is_file():
            shutil.copy2(source_path, destination)
            copied += 1
            status = "copied"
        else:
            existing += 1
            status = "existing"

        source_record = {
            "label": source_label or str(source_draft_dir),
            "source_path": str(source_path),
            "references": dependency.get("references", []),
        }
        if stored is None:
            stored = {
                "identity": identity,
                "name": display_name,
                "resource_id": str(identifiers.get("resource_id", "")),
                "effect_id": str(identifiers.get("effect_id", "")),
                "checksum_sha256": checksum,
                "size_bytes": source_path.stat().st_size,
                "file": destination.relative_to(root).as_posix(),
                "absolute_path": str(destination),
                "metadata_file": metadata_path.relative_to(root).as_posix(),
                "sources": [],
            }
            stored_fonts.append(stored)
            by_identity[identity] = stored
            if checksum:
                by_checksum[checksum] = stored
        elif display_name and _generic_font_name(str(stored.get("name", ""))):
            stored["name"] = display_name
        stored["absolute_path"] = str(destination)
        sources = stored.setdefault("sources", [])
        if source_record not in sources:
            sources.append(source_record)

        metadata = {
            "schema": FONT_METADATA_SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            **stored,
        }
        _write_json(metadata_path, metadata)
        encountered.append({**stored, "status": status})

    manifest_payload = {
        "schema": FONT_MANIFEST_SCHEMA,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fonts": stored_fonts,
    }
    _write_json(manifest_path, manifest_payload)
    return FontExportResult(
        output_dir=root,
        manifest_path=manifest_path,
        encountered_count=len(dependencies),
        copied_count=copied,
        existing_count=existing,
        missing_count=missing,
        fonts=encountered,
    )


def refresh_font_library_metadata(output_dir: str | Path) -> dict[str, Any]:
    """Refresh display names from the font files already stored in a library."""

    root = Path(output_dir).expanduser().resolve()
    manifest_path = root / "manifest" / "font_manifest.json"
    manifest = _load_manifest(manifest_path)
    fonts = [item for item in manifest.get("fonts", []) if isinstance(item, dict)]
    updated = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for item in fonts:
        relative_file = str(item.get("file", "")).strip()
        path = (root / relative_file).resolve() if relative_file else Path()
        try:
            path.relative_to(root)
            if not path.is_file():
                raise FileNotFoundError(f"字体文件不存在: {path}")
            current_name = str(item.get("name", "")).strip()
            if not _generic_font_name(current_name):
                skipped += 1
                continue
            display_name = _font_display_name(path)
            if not display_name:
                skipped += 1
                continue
            if str(item.get("name", "")) != display_name:
                item["name"] = display_name
                updated += 1
            else:
                skipped += 1

            metadata_file = str(item.get("metadata_file", "")).strip()
            if metadata_file:
                metadata_path = (root / metadata_file).resolve()
                metadata_path.relative_to(root)
                metadata = _load_json_object(metadata_path)
                metadata["name"] = display_name
                _write_json(metadata_path, metadata)
        except Exception as exc:
            errors.append({"identity": str(item.get("identity", "")), "error": str(exc)})

    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["fonts"] = fonts
    _write_json(manifest_path, manifest)
    return {
        "manifest_path": str(manifest_path),
        "font_count": len(fonts),
        "updated_count": updated,
        "skipped_count": skipped,
        "error_count": len(errors),
        "errors": errors,
    }


def _font_identity(identifiers: dict[str, Any], checksum: str) -> str:
    for key in ("resource_id", "effect_id", "third_resource_id"):
        value = str(identifiers.get(key, "")).strip()
        if value and value != "0":
            return f"{key}:{value}"
    return f"sha256:{checksum}"


def _font_display_name(path: Path) -> str:
    try:
        font = TTFont(path, lazy=True)
    except Exception:
        return ""
    try:
        names = font.get("name")
        records = getattr(names, "names", []) if names is not None else []
        for name_id in (4, 1, 6):
            for record in records:
                if getattr(record, "nameID", None) != name_id:
                    continue
                try:
                    value = str(record.toUnicode()).strip()
                except Exception:
                    continue
                if _usable_font_name(value):
                    return value
    finally:
        font.close()
    return ""


def _usable_font_name(value: str) -> bool:
    normalized = value.strip().lower()
    return bool(
        normalized
        and normalized not in {"font", "regular", "unknown", "unnamed"}
        and "\ufffd" not in value
        and "\x00" not in value
    )


def _generic_font_name(value: str) -> bool:
    return value.strip().lower() in {"", "font", "regular", "unknown", "unnamed", "unnamed_font"}


def _safe_filename(value: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    safe = re.sub(r"\s+", "_", safe).strip(" ._")
    return safe[:100] or "unnamed_font"


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": FONT_MANIFEST_SCHEMA, "fonts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"schema": FONT_MANIFEST_SCHEMA, "fonts": []}
    if not isinstance(data, dict) or data.get("schema") != FONT_MANIFEST_SCHEMA:
        return {"schema": FONT_MANIFEST_SCHEMA, "fonts": []}
    if not isinstance(data.get("fonts"), list):
        data["fonts"] = []
    return data


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": FONT_METADATA_SCHEMA}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"schema": FONT_METADATA_SCHEMA}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
