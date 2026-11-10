"""Hash-guarded semantic visual catalog v3 apply and rollback utility."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .semantic_visuals import (
    CATALOG_SCHEMA_V2,
    CATALOG_SCHEMA_V3,
    SemanticVisualCatalogError,
    _load_semantic_visual_catalog_v2,
    _load_semantic_visual_catalog_v3,
)


MIGRATION_MANIFEST_SCHEMA = "jyd.semantic-visual-catalog-migration.v1"


class SemanticVisualMigrationError(RuntimeError):
    pass


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticVisualMigrationError(f"{kind} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise SemanticVisualMigrationError(f"{kind} must be a JSON object: {path}")
    return value


def _manifest_path(value: object, *, field: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not str(path):
        raise SemanticVisualMigrationError(f"migration manifest is missing {field}")
    return path.resolve()


def load_migration_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    manifest = _read_json(manifest_path, kind="migration manifest")
    required = {
        "schema",
        "source_catalog_path",
        "source_catalog_schema",
        "source_catalog_sha256",
        "source_backup_path",
        "source_backup_sha256",
        "candidate_path",
        "candidate_schema",
        "candidate_sha256",
        "asset_count",
        "approval",
        "rollback",
    }
    if not required.issubset(manifest) or manifest.get("schema") != MIGRATION_MANIFEST_SCHEMA:
        raise SemanticVisualMigrationError("invalid migration manifest")
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or approval.get("status") not in {
        "pending",
        "approved",
    }:
        raise SemanticVisualMigrationError("invalid migration approval")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _require_hash(path: Path, expected: object, *, kind: str) -> None:
    actual = file_sha256(path)
    if actual != str(expected or "").strip().lower():
        raise SemanticVisualMigrationError(
            f"{kind} hash mismatch: expected {expected}, got {actual}"
        )


def _validate_catalog_payload(
    catalog_root: Path, payload: Mapping[str, Any], *, expected_schema: str
) -> None:
    try:
        if expected_schema == CATALOG_SCHEMA_V2:
            _load_semantic_visual_catalog_v2(catalog_root, payload)
        elif expected_schema == CATALOG_SCHEMA_V3:
            _load_semantic_visual_catalog_v3(catalog_root, payload)
        else:
            raise SemanticVisualMigrationError(
                f"unsupported migration catalog schema: {expected_schema}"
            )
    except SemanticVisualCatalogError as exc:
        raise SemanticVisualMigrationError(
            f"catalog validation failed for {expected_schema}: {exc}"
        ) from exc


def validate_migration(path: str | Path) -> dict[str, Any]:
    manifest = load_migration_manifest(path)
    catalog_path = _manifest_path(manifest["source_catalog_path"], field="source_catalog_path")
    backup_path = _manifest_path(manifest["source_backup_path"], field="source_backup_path")
    candidate_path = _manifest_path(manifest["candidate_path"], field="candidate_path")
    for file_path, expected, kind in (
        (backup_path, manifest["source_backup_sha256"], "source backup"),
        (candidate_path, manifest["candidate_sha256"], "v3 candidate"),
    ):
        if not file_path.is_file():
            raise SemanticVisualMigrationError(f"{kind} is missing: {file_path}")
        _require_hash(file_path, expected, kind=kind)

    backup = _read_json(backup_path, kind="source backup")
    candidate = _read_json(candidate_path, kind="v3 candidate")
    if backup.get("schema") != manifest["source_catalog_schema"]:
        raise SemanticVisualMigrationError("source backup schema mismatch")
    if candidate.get("schema") != manifest["candidate_schema"]:
        raise SemanticVisualMigrationError("v3 candidate schema mismatch")
    if len(candidate.get("assets") or []) != int(manifest["asset_count"]):
        raise SemanticVisualMigrationError("v3 candidate asset count mismatch")
    _validate_catalog_payload(
        catalog_path.parent, backup, expected_schema=str(manifest["source_catalog_schema"])
    )
    _validate_catalog_payload(
        catalog_path.parent, candidate, expected_schema=str(manifest["candidate_schema"])
    )
    return manifest


def _atomic_replace_bytes(target: Path, payload: bytes) -> None:
    temporary = target.with_name(f".{target.name}.migration-{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def apply_migration(path: str | Path) -> dict[str, Any]:
    manifest = validate_migration(path)
    approval = manifest["approval"]
    if (
        approval.get("status") != "approved"
        or not str(approval.get("approved_by") or "").strip()
        or not str(approval.get("approved_at") or "").strip()
    ):
        raise SemanticVisualMigrationError(
            "migration requires explicit approved_by and approved_at"
        )
    catalog_path = _manifest_path(manifest["source_catalog_path"], field="source_catalog_path")
    candidate_path = _manifest_path(manifest["candidate_path"], field="candidate_path")
    if not catalog_path.is_file():
        raise SemanticVisualMigrationError(f"source catalog is missing: {catalog_path}")
    _require_hash(
        catalog_path, manifest["source_catalog_sha256"], kind="current source catalog"
    )
    _atomic_replace_bytes(catalog_path, candidate_path.read_bytes())
    _require_hash(catalog_path, manifest["candidate_sha256"], kind="applied v3 catalog")
    return manifest


def rollback_migration(path: str | Path) -> dict[str, Any]:
    manifest = validate_migration(path)
    catalog_path = _manifest_path(manifest["source_catalog_path"], field="source_catalog_path")
    backup_path = _manifest_path(manifest["source_backup_path"], field="source_backup_path")
    if not catalog_path.is_file():
        raise SemanticVisualMigrationError(f"current catalog is missing: {catalog_path}")
    rollback = manifest.get("rollback")
    if not isinstance(rollback, dict):
        raise SemanticVisualMigrationError("invalid rollback manifest")
    _require_hash(
        catalog_path,
        rollback.get("required_current_sha256"),
        kind="current v3 catalog",
    )
    _atomic_replace_bytes(catalog_path, backup_path.read_bytes())
    _require_hash(
        catalog_path, manifest["source_backup_sha256"], kind="restored v2 catalog"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate, apply, or roll back a hash-guarded semantic catalog migration."
    )
    parser.add_argument("action", choices=("validate", "apply", "rollback"))
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    operation = {
        "validate": validate_migration,
        "apply": apply_migration,
        "rollback": rollback_migration,
    }[args.action]
    manifest = operation(args.manifest)
    print(
        json.dumps(
            {
                "status": "ok",
                "action": args.action,
                "manifest": manifest["manifest_path"],
                "asset_count": int(manifest["asset_count"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
