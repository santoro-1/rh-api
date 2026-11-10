from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
PACKAGE_ROOT = WORKSPACE / ".codex_tmp" / "semantic_catalog_v3_expansion"
STAGED_LIBRARY = PACKAGE_ROOT / "library"
CANDIDATE = STAGED_LIBRARY / "catalog.v3.candidate.json"
ACTIVE_ROOT = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library"
)
ACTIVE_CATALOG = ACTIVE_ROOT / "catalog.json"
BACKUP = PACKAGE_ROOT / "catalog.actual.pre-expansion.json"
APPLIED_MANIFEST = PACKAGE_ROOT / "migration.applied.local.json"
EXPECTED_ACTIVE_SHA256 = "d3d5c386fb2bdc8f1701b01bb53d8445e94f9d26aab07ae94dc3fd249b388d81"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_new_resource_dirs(candidate: dict) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    for asset in candidate["assets"]:
        if not asset["asset_id"].startswith("review."):
            continue
        if asset["media_type"] == "video":
            relative = Path(asset["resource"]["video"]).parent
        else:
            relative = Path(asset["resource"]["bundle"])
        if relative in seen:
            continue
        seen.add(relative)
        pairs.append((STAGED_LIBRARY / relative, ACTIVE_ROOT / relative))
    return pairs


def main() -> int:
    current_hash = sha256(ACTIVE_CATALOG)
    if current_hash != EXPECTED_ACTIVE_SHA256:
        raise SystemExit(
            "active catalog changed after review packaging; refusing to overwrite: "
            f"expected {EXPECTED_ACTIVE_SHA256}, got {current_hash}"
        )
    if APPLIED_MANIFEST.exists():
        previous = json.loads(APPLIED_MANIFEST.read_text(encoding="utf-8"))
        if previous.get("candidate_sha256") != current_hash:
            raise SystemExit("existing applied manifest does not describe current local catalog")
    candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    if candidate.get("schema") != "jyd.semantic-visual-catalog.v3":
        raise SystemExit("invalid staged candidate schema")
    if len(candidate.get("assets") or []) != 1378:
        raise SystemExit("unexpected staged candidate asset count")

    resource_pairs = iter_new_resource_dirs(candidate)
    if len(resource_pairs) != 1169:
        raise SystemExit(f"unexpected new resource directory count: {len(resource_pairs)}")
    missing = [str(source) for source, _target in resource_pairs if not source.is_dir()]
    if missing:
        raise SystemExit(f"staged resource directories are missing: {missing[:3]}")

    if not BACKUP.exists() or len(json.loads(BACKUP.read_text(encoding="utf-8")).get("assets") or []) != 209:
        raise SystemExit("original pre-expansion backup is missing or invalid")

    temporary_root = ACTIVE_ROOT / f".semantic-expansion-{os.getpid()}.tmp"
    temporary_root.mkdir()
    try:
        for index, (source, target) in enumerate(resource_pairs, start=1):
            if target.is_dir():
                continue
            relative = target.relative_to(ACTIVE_ROOT)
            temporary = temporary_root / relative
            shutil.copytree(source, temporary)
            if index % 50 == 0 or index == len(resource_pairs):
                print(f"copied {index}/{len(resource_pairs)} resources", flush=True)

        for source, target in resource_pairs:
            if target.is_dir():
                continue
            relative = target.relative_to(ACTIVE_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_root / relative, target)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)

    # Verify every candidate resource against the actual active library before the
    # catalog is made visible.
    for asset in candidate["assets"]:
        if asset["media_type"] == "video":
            relative_files = [asset["resource"]["video"], asset["resource"]["preview"]]
            if "metadata" in asset["resource"]:
                relative_files.append(asset["resource"]["metadata"])
        else:
            relative_files = [
                str(Path(asset["resource"]["bundle"]) / "sticker.json"),
                asset["resource"]["preview"],
            ]
        for relative in relative_files:
            if not (ACTIVE_ROOT / relative).is_file():
                raise SystemExit(f"active candidate resource missing: {relative}")

    candidate_hash = sha256(CANDIDATE)
    temporary_catalog = ACTIVE_ROOT / f".catalog.expansion-{os.getpid()}.tmp"
    shutil.copy2(CANDIDATE, temporary_catalog)
    os.replace(temporary_catalog, ACTIVE_CATALOG)
    if sha256(ACTIVE_CATALOG) != candidate_hash:
        raise SystemExit("applied catalog hash mismatch")

    applied_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    manifest = {
        "schema": "jyd.semantic-visual-catalog-migration.v1",
        "created_at": applied_at,
        "source_catalog_path": str(ACTIVE_CATALOG),
        "source_catalog_schema": "jyd.semantic-visual-catalog.v3",
        "source_catalog_sha256": current_hash,
        "source_backup_path": str(BACKUP),
        "source_backup_sha256": current_hash,
        "candidate_path": str(CANDIDATE),
        "candidate_schema": "jyd.semantic-visual-catalog.v3",
        "candidate_sha256": candidate_hash,
        "asset_count": len(candidate["assets"]),
        "approval": {
            "status": "approved",
            "approved_by": "Codex（用户明确委托逐条审核并要求直接进入下一步）",
            "approved_at": applied_at,
            "note": "视频U0001-U0555与图片U0556-U2120均由Codex逐条审核；仅应用本地工作台，不部署生产环境。",
        },
        "rollback": {
            "backup_path": str(BACKUP),
            "required_current_sha256": candidate_hash,
            "note": "Rollback must refuse if the active catalog changed after this local application.",
        },
        "resource_directories_added": len(resource_pairs),
        "production_deployed": False,
    }
    APPLIED_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "ok", "assets": 1378, "resources": 1169}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
