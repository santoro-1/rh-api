from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import uuid
from typing import Any

from .draft_crypto import prepare_plain_draft_dir


TEMPLATE_META_SCHEMA = "jyd.template.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_LIBRARY_ROOT = PROJECT_ROOT / "data" / "template_library"


def load_plain_draft_json(draft_dir: Path) -> dict[str, Any]:
    draft_content_path = draft_dir / "draft_content.json"
    if not draft_content_path.is_file():
        raise FileNotFoundError(f"找不到 {draft_content_path}")
    try:
        with draft_content_path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{draft_content_path} 不是合法 UTF-8 明文 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{draft_content_path} 的顶层结构不是对象")
    return data


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    name: str
    root_dir: Path
    draft_dir: Path
    meta_path: Path
    source_dir: str
    created_at: str
    was_decrypted: bool
    duration_us: int
    track_count: int
    summary: dict[str, Any]
    import_info: dict[str, Any] = field(default_factory=dict)
    expires_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": TEMPLATE_META_SCHEMA,
            "template_id": self.template_id,
            "name": self.name,
            "root_dir": str(self.root_dir),
            "draft_dir": str(self.draft_dir),
            "meta_path": str(self.meta_path),
            "source_dir": self.source_dir,
            "created_at": self.created_at,
            "was_decrypted": self.was_decrypted,
            "duration_us": self.duration_us,
            "track_count": self.track_count,
            "summary": self.summary,
            "import_info": self.import_info,
            "expires_at": self.expires_at,
        }


class TemplateLibrary:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else DEFAULT_TEMPLATE_LIBRARY_ROOT.resolve()

    def import_template(
        self,
        source_draft_dir: str | Path,
        *,
        template_id: str = "",
        name: str = "",
        replace: bool = False,
        auto_decrypt: bool = True,
        force_decrypt: bool = False,
        decrypt_work_root: str | Path | None = None,
        jy_draftc_exe: str | Path | None = None,
        jy_install_dir: str | Path | None = None,
        jy_draftc_debug: bool = False,
        import_info: dict[str, Any] | None = None,
        expires_at: str = "",
    ) -> TemplateRecord:
        source_dir = Path(source_draft_dir).expanduser().resolve()
        if not source_dir.is_dir():
            raise NotADirectoryError(f"模板草稿目录不存在或不是目录: {source_dir}")

        template_id = normalize_template_id(template_id or source_dir.name)
        if not template_id:
            template_id = f"template_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        display_name = name.strip() or source_dir.name

        record_root = self._record_root(template_id)
        draft_dir = record_root / "draft"
        meta_path = record_root / "template_meta.json"
        if record_root.exists():
            if not replace:
                raise FileExistsError(f"模板 id 已存在: {template_id} ({record_root})")
            shutil.rmtree(record_root)

        prepared = prepare_plain_draft_dir(
            source_dir,
            auto_decrypt=auto_decrypt,
            force_decrypt=force_decrypt,
            work_root=Path(decrypt_work_root).expanduser().resolve() if decrypt_work_root else None,
            exe=Path(jy_draftc_exe).expanduser().resolve() if jy_draftc_exe else None,
            install_dir=Path(jy_install_dir).expanduser().resolve() if jy_install_dir else None,
            debug=jy_draftc_debug,
        )

        record_root.mkdir(parents=True, exist_ok=False)
        shutil.copytree(prepared.draft_dir, draft_dir)

        data = load_plain_draft_json(draft_dir)
        tracks = data.get("tracks", [])
        if not isinstance(tracks, list):
            tracks = []

        meta = {
            "schema": TEMPLATE_META_SCHEMA,
            "template_id": template_id,
            "name": display_name,
            "source_dir": str(source_dir),
            "draft_dir": "draft",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "was_decrypted": prepared.was_decrypted,
            "duration_us": int(data.get("duration", 0) or 0),
            "track_count": len(tracks),
            "summary": summarize_draft_data(data),
            "import_info": dict(import_info or {}),
            "expires_at": expires_at.strip(),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.get(template_id)

    def get(self, template_id: str) -> TemplateRecord:
        normalized_id = normalize_template_id(template_id)
        if not normalized_id:
            raise ValueError("template_id 不能为空")

        record_root = self._record_root(normalized_id)
        meta_path = record_root / "template_meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"模板不存在: {normalized_id} ({meta_path})")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("schema") != TEMPLATE_META_SCHEMA:
            raise RuntimeError(f"模板元数据格式不支持: {meta_path}")

        draft_dir = record_root / str(meta.get("draft_dir", "draft"))
        if not (draft_dir / "draft_content.json").exists():
            raise FileNotFoundError(f"模板缺少 draft_content.json: {draft_dir}")

        summary = meta.get("summary")
        if not isinstance(summary, dict) or not summary:
            summary = summarize_draft_data(load_plain_draft_json(draft_dir))

        return TemplateRecord(
            template_id=normalized_id,
            name=str(meta.get("name", normalized_id)),
            root_dir=record_root,
            draft_dir=draft_dir,
            meta_path=meta_path,
            source_dir=str(meta.get("source_dir", "")),
            created_at=str(meta.get("created_at", "")),
            was_decrypted=bool(meta.get("was_decrypted", False)),
            duration_us=int(meta.get("duration_us", 0) or 0),
            track_count=int(meta.get("track_count", 0) or 0),
            summary=dict(summary),
            import_info=dict(meta.get("import_info", {})) if isinstance(meta.get("import_info"), dict) else {},
            expires_at=str(meta.get("expires_at", "")),
        )

    def list(self) -> list[TemplateRecord]:
        if not self.root.exists():
            return []
        records: list[TemplateRecord] = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            try:
                records.append(self.get(path.name))
            except Exception:
                continue
        return records

    def _record_root(self, template_id: str) -> Path:
        return self.root / normalize_template_id(template_id)


def rebase_template_library_paths(root: str | Path) -> dict[str, int]:
    """Relocate copied template assets whose JSON still contains old absolute paths."""

    library_root = Path(root).expanduser().resolve()
    stats = {"templates": 0, "files": 0, "paths": 0}
    if not library_root.is_dir():
        return stats

    for record_root in library_root.iterdir():
        asset_root = record_root / "assets"
        if not asset_root.is_dir():
            continue
        manifest_path_map = _transfer_manifest_path_map(record_root, asset_root)
        template_changed = False
        for filename in ("draft_content.json", "draft_meta_info.json"):
            draft_path = record_root / "draft" / filename
            if not draft_path.is_file():
                continue
            try:
                data = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            rewritten, count = _rebase_template_value(data, asset_root, manifest_path_map)
            if count <= 0:
                continue
            draft_path.write_text(
                json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            template_changed = True
            stats["files"] += 1
            stats["paths"] += count
        if template_changed:
            stats["templates"] += 1
    return stats


def _rebase_template_value(
    value: Any,
    asset_root: Path,
    manifest_path_map: dict[str, Path] | None = None,
) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            rewritten, item_count = _rebase_template_value(item, asset_root, manifest_path_map)
            result[key] = rewritten
            count += item_count
        return result, count
    if isinstance(value, list):
        result_list: list[Any] = []
        count = 0
        for item in value:
            rewritten, item_count = _rebase_template_value(item, asset_root, manifest_path_map)
            result_list.append(rewritten)
            count += item_count
        return result_list, count
    if not isinstance(value, str):
        return value, 0

    manifest_match = (manifest_path_map or {}).get(_normalize_template_path(value))
    if manifest_match is not None and manifest_match.exists():
        return str(manifest_match), 1

    rebased = _rebase_asset_path(value, asset_root)
    if rebased is not None:
        return str(rebased), 1

    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return value, 0
        rewritten, count = _rebase_template_value(nested, asset_root, manifest_path_map)
        if count:
            return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")), count
    return value, 0


def _transfer_manifest_path_map(record_root: Path, asset_root: Path) -> dict[str, Path]:
    manifest_path = record_root / "transfer_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    assets = manifest.get("assets", []) if isinstance(manifest, dict) else []
    if not isinstance(assets, list):
        return {}

    result: dict[str, Path] = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        archive_parts = [part for part in str(item.get("archive_path", "")).replace("\\", "/").split("/") if part]
        if not archive_parts or archive_parts[0].casefold() != "assets":
            continue
        target = asset_root.joinpath(*archive_parts[1:]).resolve()
        try:
            target.relative_to(asset_root.resolve())
        except ValueError:
            continue
        if not target.exists():
            continue
        for key in ("source_path", "original_path"):
            normalized = _normalize_template_path(str(item.get(key, "")))
            if normalized:
                result[normalized] = target
    return result


def _normalize_template_path(value: str) -> str:
    return value.strip().strip('"').replace("/", "\\").casefold()


def _rebase_asset_path(value: str, asset_root: Path) -> Path | None:
    normalized = value.strip().replace("/", "\\")
    if not normalized or "\\assets\\" not in normalized.casefold():
        return None
    marker_index = normalized.casefold().rfind("\\assets\\")
    relative_text = normalized[marker_index + len("\\assets\\") :]
    if not relative_text:
        return None
    candidate = asset_root.joinpath(*Path(relative_text).parts).resolve()
    try:
        candidate.relative_to(asset_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() else None


def normalize_template_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    normalized = normalized.strip(" ._")
    return normalized[:80]


def summarize_draft_data(data: dict[str, Any]) -> dict[str, Any]:
    tracks = data.get("tracks", [])
    if not isinstance(tracks, list):
        tracks = []

    track_type_counts: dict[str, int] = {}
    segment_count_by_type: dict[str, int] = {}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        track_type = str(track.get("type") or "unknown")
        track_type_counts[track_type] = track_type_counts.get(track_type, 0) + 1
        segments = track.get("segments", [])
        segment_count = len(segments) if isinstance(segments, list) else 0
        segment_count_by_type[track_type] = segment_count_by_type.get(track_type, 0) + segment_count

    materials = data.get("materials", {})
    if not isinstance(materials, dict):
        materials = {}
    material_counts = {
        str(key): len(value)
        for key, value in materials.items()
        if isinstance(value, list)
    }

    return {
        "track_type_counts": track_type_counts,
        "segment_count_by_type": segment_count_by_type,
        "material_counts": material_counts,
        "text_count": material_counts.get("texts", 0),
        "audio_count": material_counts.get("audios", 0),
        "video_count": material_counts.get("videos", 0),
        "effect_count": material_counts.get("effects", 0) + material_counts.get("video_effects", 0),
        "animation_count": material_counts.get("material_animations", 0),
        "nested_draft_count": material_counts.get("drafts", 0),
    }
