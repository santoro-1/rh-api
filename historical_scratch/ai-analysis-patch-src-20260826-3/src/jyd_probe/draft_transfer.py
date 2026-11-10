from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any
import uuid
import zipfile

from .template_library import TemplateLibrary, normalize_template_id


TRANSFER_SCHEMA = "jyd_probe.draft_transfer.v1"
MAX_ARCHIVE_FILES = 20_000
MAX_EXTRACTED_BYTES = 50 * 1024**3


def build_transfer_package(plan: dict[str, Any], output_path: str | Path) -> dict[str, Any]:
    """Build a portable ZIP from a persisted upload plan."""

    summary = plan.get("summary", {})
    if not isinstance(summary, dict) or not summary.get("ready_for_upload"):
        raise ValueError("上传清单仍有阻塞问题，不能生成迁移包")

    draft = plan.get("draft", {})
    if not isinstance(draft, dict):
        raise ValueError("上传清单缺少草稿信息")
    draft_dir = Path(str(draft.get("analyzed_draft_dir", ""))).expanduser().resolve()
    if not (draft_dir / "draft_content.json").is_file():
        raise FileNotFoundError(f"解密后的草稿不存在: {draft_dir}")

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    assets: list[dict[str, Any]] = []
    library_references: list[dict[str, Any]] = []

    with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
        _write_directory(archive, draft_dir, "draft")
        for index, dependency in enumerate(plan.get("dependencies", [])):
            if not isinstance(dependency, dict) or dependency.get("decision") != "upload":
                continue
            source = Path(str(dependency.get("path", ""))).expanduser().resolve()
            if not source.exists():
                raise FileNotFoundError(f"待上传素材已不存在: {source}")
            archive_path = f"assets/{index:04d}_{_safe_archive_name(source.name)}"
            if source.is_dir():
                _write_directory(archive, source, archive_path)
            else:
                archive.write(source, archive_path, compress_type=_compression_for(source))
            assets.append(
                {
                    "kind": str(dependency.get("kind", "resource")),
                    "source_path": str(source),
                    "original_path": str(dependency.get("original_path", "")),
                    "archive_path": archive_path,
                    "is_directory": source.is_dir(),
                    "size_bytes": int(dependency.get("size_bytes", 0) or 0),
                    "checksum_sha256": str(dependency.get("checksum_sha256", "")),
                    "references": dependency.get("references", []),
                }
            )

        for dependency in plan.get("dependencies", []):
            if not isinstance(dependency, dict) or dependency.get("decision") != "reuse_library":
                continue
            central_match = dependency.get("central_match", {})
            if not isinstance(central_match, dict):
                continue
            kind = str(dependency.get("kind", "resource"))
            library_file = str(central_match.get("library_file", "")).strip()
            original_path = str(dependency.get("original_path", "")).strip()
            if kind != "font" or not library_file or not original_path:
                continue
            library_references.append(
                {
                    "kind": kind,
                    "original_path": original_path,
                    "identity": str(central_match.get("identity", "")),
                    "library_file": library_file,
                    "checksum_sha256": str(central_match.get("checksum_sha256", "")),
                    "references": dependency.get("references", []),
                }
            )

        manifest = {
            "schema": TRANSFER_SCHEMA,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "plan_id": str(plan.get("plan_id", "")),
            "report_id": str(plan.get("report_id", "")),
            "draft": draft,
            "policies": plan.get("policies", {}),
            "summary": summary,
            "assets": assets,
            "library_references": library_references,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            compress_type=zipfile.ZIP_DEFLATED,
        )

    temporary.replace(output)
    return {
        "path": str(output),
        "filename": output.name,
        "size_bytes": output.stat().st_size,
        "checksum_sha256": _file_sha256(output),
        "asset_count": len(assets),
        "manifest": manifest,
    }


def import_transfer_package(
    package_path: str | Path,
    *,
    imports_root: str | Path,
    template_library_root: str | Path,
    template_name: str = "",
    font_library_root: str | Path | None = None,
    expires_at: str = "",
    lifecycle: str = "",
) -> dict[str, Any]:
    """Validate and import a transfer ZIP into the server template library."""

    package = Path(package_path).expanduser().resolve()
    if not package.is_file():
        raise FileNotFoundError(f"迁移包不存在: {package}")

    import_id = uuid.uuid4().hex
    import_root = Path(imports_root).expanduser().resolve() / import_id
    extracted_root = import_root / "extracted"
    import_root.mkdir(parents=True, exist_ok=False)
    try:
        _safe_extract(package, extracted_root)
        manifest_path = extracted_root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("迁移包缺少 manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != TRANSFER_SCHEMA:
            raise ValueError("迁移包格式不受支持")

        source_draft = extracted_root / "draft"
        content_path = source_draft / "draft_content.json"
        if not content_path.is_file():
            raise ValueError("迁移包缺少 draft/draft_content.json")
        data = json.loads(content_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("draft_content.json 顶层必须是 JSON 对象")

        draft_info = manifest.get("draft", {})
        draft_name = str(draft_info.get("name", "本地导入母版")) if isinstance(draft_info, dict) else "本地导入母版"
        display_name = template_name.strip() or draft_name
        template_id = normalize_template_id(f"imported_{draft_name}_{import_id[:8]}")
        import_info = {
            "source": "local_collector",
            "import_id": import_id,
            "lifecycle": lifecycle.strip(),
            "incoming_package_path": str(package),
            "plan_id": str(manifest.get("plan_id", "")),
            "report_id": str(manifest.get("report_id", "")),
            "policies": manifest.get("policies", {}),
            "summary": manifest.get("summary", {}),
        }
        library = TemplateLibrary(template_library_root)
        record = library.import_template(
            source_draft,
            template_id=template_id,
            name=display_name,
            auto_decrypt=False,
            import_info=import_info,
            expires_at=expires_at,
        )

        asset_root = record.root_dir / "assets"
        path_map: dict[str, str] = {}
        assets = manifest.get("assets", [])
        if not isinstance(assets, list):
            raise ValueError("迁移包 assets 字段格式错误")
        for item in assets:
            if not isinstance(item, dict):
                continue
            archive_path = _validated_member_name(str(item.get("archive_path", "")))
            extracted_asset = extracted_root.joinpath(*archive_path.parts)
            if not extracted_asset.exists():
                raise FileNotFoundError(f"迁移包素材缺失: {archive_path.as_posix()}")
            target = asset_root.joinpath(*archive_path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if extracted_asset.is_dir():
                shutil.copytree(extracted_asset, target)
            else:
                shutil.copy2(extracted_asset, target)
            for source_key in (item.get("source_path"), item.get("original_path")):
                normalized = _normalize_local_path(str(source_key or ""))
                if normalized:
                    path_map[normalized] = str(target.resolve())

        library_references = manifest.get("library_references", [])
        if not isinstance(library_references, list):
            raise ValueError("迁移包 library_references 字段格式错误")
        for item in library_references:
            if not isinstance(item, dict) or str(item.get("kind", "")) != "font":
                continue
            if font_library_root is None:
                raise ValueError("母版需要复用字体库文件，但服务器未配置字体库目录")
            root = Path(font_library_root).expanduser().resolve()
            relative = _validated_member_name(str(item.get("library_file", "")))
            target = root.joinpath(*relative.parts).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"字体库引用越界: {relative.as_posix()}") from exc
            if not target.is_file():
                identity = str(item.get("identity", "")) or relative.as_posix()
                raise FileNotFoundError(f"服务器字体库缺少保留原样所需字体: {identity}")
            expected_checksum = str(item.get("checksum_sha256", "")).strip().lower()
            if expected_checksum and _file_sha256(target) != expected_checksum:
                identity = str(item.get("identity", "")) or relative.as_posix()
                raise ValueError(f"服务器字体库中的字体文件校验不一致: {identity}")
            normalized = _normalize_local_path(str(item.get("original_path", "")))
            if normalized:
                path_map[normalized] = str(target)

        rewrite_count = 0
        for filename in ("draft_content.json", "draft_meta_info.json"):
            imported_json_path = record.draft_dir / filename
            if not imported_json_path.is_file():
                continue
            imported_data = json.loads(imported_json_path.read_text(encoding="utf-8"))
            rewritten_data, file_rewrite_count = _rewrite_paths(imported_data, path_map)
            imported_json_path.write_text(
                json.dumps(rewritten_data, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            rewrite_count += file_rewrite_count
        (record.root_dir / "transfer_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = {
            "import_id": import_id,
            "status": "completed",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "package_path": str(package),
            "template": library.get(template_id).as_dict(),
            "asset_count": len(assets),
            "library_reference_count": len(library_references),
            "rewritten_path_count": rewrite_count,
        }
        (import_root / "import_result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result
    except Exception:
        shutil.rmtree(import_root, ignore_errors=True)
        raise


def _write_directory(archive: zipfile.ZipFile, source: Path, archive_root: str) -> None:
    wrote_file = False
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        wrote_file = True
        relative = path.relative_to(source).as_posix()
        archive_name = f"{archive_root.rstrip('/')}/{relative}"
        archive.write(path, archive_name, compress_type=_compression_for(path))
    if not wrote_file:
        archive.writestr(f"{archive_root.rstrip('/')}/", b"")


def _compression_for(path: Path) -> int:
    if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".mp3", ".m4a", ".aac", ".zip"}:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _safe_extract(package: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    total_size = 0
    with zipfile.ZipFile(package, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise ValueError(f"迁移包文件数量超过限制: {len(members)}")
        for member in members:
            name = _validated_member_name(member.filename)
            if member.is_dir():
                destination.joinpath(*name.parts).mkdir(parents=True, exist_ok=True)
                continue
            total_size += int(member.file_size)
            if total_size > MAX_EXTRACTED_BYTES:
                raise ValueError("迁移包解压后大小超过限制")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _validated_member_name(value: str) -> PurePosixPath:
    name = PurePosixPath(value.replace("\\", "/"))
    if not value or name.is_absolute() or ".." in name.parts or not name.parts:
        raise ValueError(f"迁移包包含不安全路径: {value}")
    if ":" in name.parts[0]:
        raise ValueError(f"迁移包包含不安全路径: {value}")
    return name


def _rewrite_paths(value: Any, path_map: dict[str, str]) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            rewritten, item_count = _rewrite_paths(item, path_map)
            result[key] = rewritten
            count += item_count
        return result, count
    if isinstance(value, list):
        result_list: list[Any] = []
        count = 0
        for item in value:
            rewritten, item_count = _rewrite_paths(item, path_map)
            result_list.append(rewritten)
            count += item_count
        return result_list, count
    if not isinstance(value, str):
        return value, 0

    replacement = path_map.get(_normalize_local_path(value))
    if replacement:
        return replacement, 1

    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return value, 0
        rewritten, count = _rewrite_paths(nested, path_map)
        if count:
            return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")), count
    return value, 0


def _normalize_local_path(value: str) -> str:
    return value.strip().replace("/", "\\").rstrip("\\").casefold()


def _safe_archive_name(value: str) -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip(" .")
    return cleaned or "asset"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
