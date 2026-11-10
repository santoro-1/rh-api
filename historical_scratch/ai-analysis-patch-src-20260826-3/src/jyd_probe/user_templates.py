from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any
import uuid

from .draft_crypto import prepare_plain_draft_dir
from .draft_import_analyzer import analyze_draft_import
from .template_library import load_plain_draft_json


USER_TEMPLATE_SCHEMA = "jyd.user-template.v1"
KNOWN_CAPTION_TRACK_NAMES = {
    "网页自动字幕",
    "minimax 单行字幕",
    "minimax 精确字幕",
    "自动字幕",
}
IGNORED_REPLACED_DEPENDENCY_KINDS = {"audio"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _path_key(value: str) -> str:
    return value.strip().strip('"').replace("/", "\\").casefold()


def _safe_relative_path(value: str) -> Path:
    text = str(value or "").strip().replace("\\", "/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("上传文件路径不合法")
    if ":" in pure.parts[0]:
        raise ValueError("上传文件路径不能包含磁盘路径")
    return Path(*pure.parts)


def _owner_key(owner_user_id: str) -> str:
    owner = str(owner_user_id or "").strip()
    if not owner:
        raise ValueError("用户编号不能为空")
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


def _clean_name(value: str) -> str:
    name = " ".join(str(value or "").split()).strip()
    if not name:
        raise ValueError("模板名称不能为空")
    if len(name) > 80:
        raise ValueError("模板名称不能超过 80 个字符")
    return name


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON 顶层必须是对象: {path}")
    return value


def _text_materials(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    materials = data.get("materials", {})
    values = materials.get("texts", []) if isinstance(materials, dict) else []
    return {
        str(item.get("id")): item
        for item in values if isinstance(item, dict) and item.get("id")
    }


def _segment_text_material(
    data: dict[str, Any], texts: dict[str, dict[str, Any]], material_id: str
) -> dict[str, Any] | None:
    direct = texts.get(material_id)
    if direct is not None:
        return direct
    materials = data.get("materials", {})
    templates = materials.get("text_templates", []) if isinstance(materials, dict) else []
    for template in templates if isinstance(templates, list) else []:
        if not isinstance(template, dict) or str(template.get("id") or "") != material_id:
            continue
        for resource in template.get("text_info_resources", []):
            if not isinstance(resource, dict):
                continue
            nested = texts.get(str(resource.get("text_material_id") or ""))
            if nested is not None:
                return nested
    return None


def _text_value(material: dict[str, Any]) -> str:
    content = material.get("content", "")
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if not isinstance(content, str):
        return ""
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return ""
    return str(value.get("text") or "") if isinstance(value, dict) else ""


def _timerange(segment: dict[str, Any]) -> tuple[int, int]:
    value = segment.get("target_timerange", {})
    if not isinstance(value, dict):
        return 0, 0
    start = max(0, int(value.get("start", 0) or 0))
    duration = max(0, int(value.get("duration", 0) or 0))
    return start, duration


def detect_caption_track(data: dict[str, Any]) -> dict[str, Any]:
    texts = _text_materials(data)
    duration = max(1, int(data.get("duration", 0) or 0))
    candidates: list[dict[str, Any]] = []
    typed_index = 0
    for raw_index, track in enumerate(data.get("tracks", [])):
        if not isinstance(track, dict) or track.get("type") != "text":
            continue
        ordinary: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for segment_index, segment in enumerate(track.get("segments", [])):
            if not isinstance(segment, dict):
                continue
            material = _segment_text_material(
                data, texts, str(segment.get("material_id") or "")
            )
            if material is not None:
                ordinary.append((segment_index, segment, material))
        if ordinary:
            name = str(track.get("name") or "").strip()
            lowered = name.casefold()
            ranges = sorted((_timerange(segment) for _, segment, _ in ordinary))
            start = min(value[0] for value in ranges)
            end = max(value[0] + value[1] for value in ranges)
            overlap = any(
                ranges[index][0] < ranges[index - 1][0] + ranges[index - 1][1]
                for index in range(1, len(ranges))
            )
            values = [_text_value(material) for _, _, material in ordinary]
            short_ratio = sum(1 for value in values if 0 < len(value) <= 40) / len(values)
            score = min(len(ordinary), 20) * 2
            if lowered in KNOWN_CAPTION_TRACK_NAMES:
                score += 120
            elif any(token in lowered for token in ("字幕", "caption", "subtitle", "歌词")):
                score += 45
            if len(ordinary) >= 2:
                score += 20
            if not overlap:
                score += 15
            if (end - start) / duration >= 0.5:
                score += 20
            score += int(short_ratio * 20)
            candidates.append(
                {
                    "score": score,
                    "track_id": str(track.get("id") or ""),
                    "track_name": name,
                    "raw_track_index": raw_index,
                    "typed_track_index": typed_index,
                    "base_segment_index": ordinary[0][0],
                    "base_segment_id": str(ordinary[0][1].get("id") or ""),
                    "base_material_id": str(ordinary[0][1].get("material_id") or ""),
                    "segment_count": len(ordinary),
                    "coverage_ratio": round((end - start) / duration, 4),
                    "sample_texts": [value for value in values[:3] if value],
                }
            )
        typed_index += 1
    if not candidates:
        raise ValueError("模板中没有可用的普通文字字幕轨")
    candidates.sort(key=lambda item: (item["score"], item["segment_count"]), reverse=True)
    best = candidates[0]
    if best["score"] < 60:
        raise ValueError("无法自动确认字幕轨，请在剪映中保留一条连续字幕轨后重新上传")
    if len(candidates) > 1 and candidates[1]["score"] >= best["score"] - 8:
        raise ValueError("模板中存在多条相似字幕轨，请只保留一条语音字幕轨后重新上传")
    return {key: value for key, value in best.items() if key != "score"}


def detect_main_video(data: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    typed_index = 0
    for raw_index, track in enumerate(data.get("tracks", [])):
        if not isinstance(track, dict) or track.get("type") != "video":
            continue
        for segment_index, segment in enumerate(track.get("segments", [])):
            if not isinstance(segment, dict):
                continue
            start, duration = _timerange(segment)
            candidates.append(
                {
                    "track_id": str(track.get("id") or ""),
                    "raw_track_index": raw_index,
                    "typed_track_index": typed_index,
                    "segment_index": segment_index,
                    "segment_id": str(segment.get("id") or ""),
                    "material_id": str(segment.get("material_id") or ""),
                    "start_us": start,
                    "duration_us": duration,
                }
            )
        typed_index += 1
    if not candidates:
        raise ValueError("模板中没有可替换的主视频轨")
    return max(
        candidates,
        key=lambda item: (item["start_us"] == 0, item["duration_us"]),
    )


def _rewrite_paths(value: Any, path_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, path_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(item, path_map) for item in value]
    if not isinstance(value, str):
        return value
    direct = path_map.get(_path_key(value))
    if direct:
        return direct
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return value
        rewritten = _rewrite_paths(nested, path_map)
        if rewritten != nested:
            return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":"))
    return value


class UserTemplateStore:
    def __init__(
        self,
        root: str | Path,
        *,
        libraries_root: str | Path,
        max_template_bytes: int = 5 * 1024 * 1024 * 1024,
    ):
        self.root = Path(root).expanduser().resolve()
        self.libraries_root = Path(libraries_root).expanduser().resolve()
        self.max_template_bytes = max(1, int(max_template_bytes))
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, owner_user_id: str, name: str) -> dict[str, Any]:
        clean_name = _clean_name(name)
        if any(item["name"].casefold() == clean_name.casefold() for item in self.list(owner_user_id)):
            raise ValueError("当前账号已经有同名模板")
        template_id = uuid.uuid4().hex
        root = self._root(owner_user_id, template_id)
        root.mkdir(parents=True, exist_ok=False)
        now = _now()
        meta = {
            "schema": USER_TEMPLATE_SCHEMA,
            "template_id": template_id,
            "owner_user_id": str(owner_user_id),
            "name": clean_name,
            "status": "UPLOADING",
            "created_at": now,
            "updated_at": now,
            "profile": {},
            "missing_resources": [],
            "path_map": {},
        }
        self._save(root, meta)
        return self._public(meta)

    def list(self, owner_user_id: str) -> list[dict[str, Any]]:
        owner_root = self.root / _owner_key(owner_user_id)
        if not owner_root.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in owner_root.iterdir():
            if not path.is_dir():
                continue
            try:
                result.append(self._public(self._load(path, owner_user_id)))
            except Exception:
                continue
        return sorted(result, key=lambda item: item.get("updated_at", ""), reverse=True)

    def get(self, owner_user_id: str, template_id: str) -> dict[str, Any]:
        return self._public(self._load(self._root(owner_user_id, template_id), owner_user_id))

    def render_binding(self, owner_user_id: str, template_id: str) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        if meta.get("status") != "READY":
            raise ValueError("剪映模板尚未准备完成")
        draft_dir = root / "draft"
        if not (draft_dir / "draft_content.json").is_file():
            raise FileNotFoundError("剪映模板草稿不存在")
        return {
            "template_id": meta["template_id"],
            "name": meta["name"],
            "draft_dir": str(draft_dir),
            "profile": dict(meta.get("profile") or {}),
            "content_hash": str(meta.get("content_hash") or ""),
        }

    def upload_draft_file(
        self, owner_user_id: str, template_id: str, relative_path: str, content: bytes
    ) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        if meta.get("status") not in {"UPLOADING", "INVALID"}:
            raise ValueError("当前模板不能继续上传草稿文件")
        relative = _safe_relative_path(relative_path)
        target = (root / "upload" / relative).resolve()
        target.relative_to((root / "upload").resolve())
        self._check_quota(root, target, len(content))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        meta["status"] = "UPLOADING"
        meta["updated_at"] = _now()
        self._save(root, meta)
        return {"ok": True, "path": relative.as_posix(), "size_bytes": len(content)}

    def analyze(self, owner_user_id: str, template_id: str) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        upload_root = root / "upload"
        content_files = list(upload_root.rglob("draft_content.json")) if upload_root.is_dir() else []
        if len(content_files) != 1:
            raise ValueError("请选择只包含一个 draft_content.json 的具体剪映草稿文件夹")
        source_dir = content_files[0].parent
        prepared = prepare_plain_draft_dir(source_dir, work_root=root / "decrypt")
        draft_dir = root / "draft"
        if draft_dir.exists():
            shutil.rmtree(draft_dir)
        shutil.copytree(prepared.draft_dir, draft_dir)
        data = load_plain_draft_json(draft_dir)
        profile = {
            "draft_duration_us": int(data.get("duration", 0) or 0),
            "caption_track": detect_caption_track(data),
            "main_video": detect_main_video(data),
        }
        report = analyze_draft_import(
            data,
            source_draft_dir=source_dir,
            analyzed_draft_dir=draft_dir,
            was_decrypted=prepared.was_decrypted,
            workspace_root=self.libraries_root,
        )
        path_map: dict[str, str] = {}
        missing: list[dict[str, Any]] = []
        main_video_material_id = str(profile["main_video"].get("material_id") or "")
        for dependency in report.get("dependencies", []):
            if not isinstance(dependency, dict):
                continue
            kind = str(dependency.get("kind") or "resource")
            references = dependency.get("references", [])
            referenced_material_ids = {
                str(item.get("material_id") or "")
                for item in references if isinstance(item, dict)
            }
            if kind in IGNORED_REPLACED_DEPENDENCY_KINDS:
                continue
            if kind == "video" and main_video_material_id in referenced_material_ids:
                continue
            original = str(dependency.get("original_path") or dependency.get("path") or "")
            if not original:
                continue
            resource_key = hashlib.sha256(original.encode("utf-8")).hexdigest()[:20]
            source = self._dependency_source(dependency)
            if source is not None:
                target = self._copy_resource(root, resource_key, source)
                path_map[_path_key(original)] = str(target)
                continue
            identifiers = {
                str(key): str(value)
                for key, value in dict(dependency.get("identifiers") or {}).items()
                if value
            }
            missing.append(
                {
                    "resource_key": resource_key,
                    "kind": kind,
                    "original_path": original,
                    "identifiers": identifiers,
                    "candidate_cache_paths": self._cache_candidates(kind, identifiers),
                }
            )
        self._apply_path_map(draft_dir, path_map)
        content_hash = hashlib.sha256(
            (draft_dir / "draft_content.json").read_bytes()
        ).hexdigest()
        meta.update(
            {
                "status": "NEEDS_RESOURCES" if missing else "READY",
                "updated_at": _now(),
                "was_decrypted": prepared.was_decrypted,
                "profile": profile,
                "missing_resources": missing,
                "path_map": path_map,
                "content_hash": content_hash,
            }
        )
        self._save(root, meta)
        return self._public(meta)

    def upload_resource_file(
        self,
        owner_user_id: str,
        template_id: str,
        resource_key: str,
        relative_path: str,
        content: bytes,
    ) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        missing = {str(item.get("resource_key")): item for item in meta.get("missing_resources", [])}
        if resource_key not in missing:
            raise ValueError("模板没有请求这个素材")
        relative = _safe_relative_path(relative_path)
        target_root = (root / "assets" / resource_key / "payload").resolve()
        target = (target_root / relative).resolve()
        target.relative_to(target_root)
        self._check_quota(root, target, len(content))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {"ok": True, "path": relative.as_posix(), "size_bytes": len(content)}

    def complete_resources(self, owner_user_id: str, template_id: str) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        path_map = dict(meta.get("path_map") or {})
        unresolved: list[dict[str, Any]] = []
        for item in meta.get("missing_resources", []):
            if not isinstance(item, dict):
                continue
            payload = root / "assets" / str(item.get("resource_key")) / "payload"
            files = [path for path in payload.rglob("*") if path.is_file()] if payload.is_dir() else []
            if not files:
                unresolved.append(item)
                continue
            original = str(item.get("original_path") or "")
            if original:
                replacement = self._uploaded_resource_target(
                    payload,
                    files,
                    original,
                    dict(item.get("identifiers") or {}),
                )
                path_map[_path_key(original)] = str(replacement)
        self._apply_path_map(root / "draft", path_map)
        meta["path_map"] = path_map
        meta["missing_resources"] = unresolved
        meta["status"] = "READY" if not unresolved else "NEEDS_RESOURCES"
        meta["updated_at"] = _now()
        self._save(root, meta)
        return self._public(meta)

    def rename(self, owner_user_id: str, template_id: str, name: str) -> dict[str, Any]:
        root = self._root(owner_user_id, template_id)
        meta = self._load(root, owner_user_id)
        clean_name = _clean_name(name)
        if any(
            item["template_id"] != template_id and item["name"].casefold() == clean_name.casefold()
            for item in self.list(owner_user_id)
        ):
            raise ValueError("当前账号已经有同名模板")
        meta["name"] = clean_name
        meta["updated_at"] = _now()
        self._save(root, meta)
        return self._public(meta)

    def delete(self, owner_user_id: str, template_id: str) -> None:
        root = self._root(owner_user_id, template_id)
        self._load(root, owner_user_id)
        shutil.rmtree(root)

    def _root(self, owner_user_id: str, template_id: str) -> Path:
        clean_id = str(template_id or "").strip()
        if not clean_id or not clean_id.isalnum():
            raise ValueError("模板编号不合法")
        owner_root = (self.root / _owner_key(owner_user_id)).resolve()
        target = (owner_root / clean_id).resolve()
        target.relative_to(owner_root)
        return target

    def _load(self, root: Path, owner_user_id: str) -> dict[str, Any]:
        path = root / "template.json"
        if not path.is_file():
            raise FileNotFoundError("剪映模板不存在")
        meta = _read_json(path)
        if meta.get("schema") != USER_TEMPLATE_SCHEMA:
            raise RuntimeError("剪映模板记录格式不支持")
        if str(meta.get("owner_user_id") or "") != str(owner_user_id):
            raise FileNotFoundError("剪映模板不存在")
        return meta

    @staticmethod
    def _save(root: Path, meta: dict[str, Any]) -> None:
        _write_json(root / "template.json", meta)

    @staticmethod
    def _public(meta: dict[str, Any]) -> dict[str, Any]:
        missing_resources = []
        for item in meta.get("missing_resources", []):
            if not isinstance(item, dict):
                continue
            missing_resources.append(
                {
                    "resource_key": item.get("resource_key"),
                    "kind": item.get("kind"),
                    "identifiers": dict(item.get("identifiers") or {}),
                    "candidate_cache_paths": list(item.get("candidate_cache_paths") or []),
                }
            )
        return {
            "schema": USER_TEMPLATE_SCHEMA,
            "template_id": meta.get("template_id"),
            "name": meta.get("name"),
            "status": meta.get("status"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "profile": dict(meta.get("profile") or {}),
            "missing_resources": missing_resources,
        }

    def _dependency_source(self, dependency: dict[str, Any]) -> Path | None:
        match = dependency.get("central_match")
        if not isinstance(match, dict):
            return None
        kind = str(match.get("kind") or "")
        roots = {
            "audio": self.libraries_root / "audio_library",
            "text_effect": self.libraries_root / "text_effect_library",
            "text_template": self.libraries_root / "text_template_library",
            "sticker": self.libraries_root / "sticker_library",
            "font": self.libraries_root / "font_library",
            "video_effect": self.libraries_root / "effect_library",
        }
        library_root = roots.get(kind)
        if library_root is None:
            return None
        library_file = str(match.get("library_file") or "")
        if library_file:
            candidate = (library_root / library_file).resolve()
            if candidate.exists():
                return candidate
        metadata_file = str(match.get("metadata_file") or "")
        if not metadata_file:
            return None
        metadata_path = Path(metadata_file)
        if not metadata_path.is_absolute():
            metadata_path = (library_root / metadata_path).resolve()
        if not metadata_path.is_file():
            return None
        payload = _read_json(metadata_path)
        resources: list[dict[str, Any]] = []
        if isinstance(payload.get("resource"), dict):
            resources.append(payload["resource"])
        resources.extend(item for item in payload.get("resources", []) if isinstance(item, dict))
        original_key = _path_key(str(dependency.get("original_path") or dependency.get("path") or ""))
        ordered = sorted(resources, key=lambda item: _path_key(str(item.get("original_path") or "")) != original_key)
        for item in ordered:
            relative = str(item.get("library_path") or "")
            if not relative:
                continue
            candidate = (metadata_path.parent / relative).resolve()
            if candidate.exists():
                return candidate
        return None

    def _check_quota(self, root: Path, target: Path, incoming_bytes: int) -> None:
        existing_bytes = target.stat().st_size if target.is_file() else 0
        stored_bytes = sum(
            path.stat().st_size for path in root.rglob("*") if path.is_file()
        )
        if stored_bytes - existing_bytes + incoming_bytes > self.max_template_bytes:
            raise ValueError("模板文件总大小超过服务器限制")

    @staticmethod
    def _uploaded_resource_target(
        payload: Path,
        files: list[Path],
        original: str,
        identifiers: dict[str, Any],
    ) -> Path:
        normalized = PurePosixPath(original.replace("\\", "/"))
        parts = list(normalized.parts)
        payload_root = payload.resolve()
        for token in {str(value) for value in identifiers.values() if value}:
            matching_indexes = [
                index for index, part in enumerate(parts) if part.casefold() == token.casefold()
            ]
            if not matching_indexes:
                continue
            tail = parts[matching_indexes[-1] + 1 :]
            if any(part in {"", ".", ".."} for part in tail):
                continue
            candidate = payload_root.joinpath(*tail).resolve()
            try:
                candidate.relative_to(payload_root)
            except ValueError:
                continue
            if candidate.exists():
                return candidate
        original_name = normalized.name
        if Path(original_name).suffix:
            named = [path for path in files if path.name.casefold() == original_name.casefold()]
            suffix = Path(original_name).suffix.casefold()
            candidates = named or [path for path in files if path.suffix.casefold() == suffix]
            if len(candidates) == 1:
                candidate = candidates[0].resolve()
                candidate.relative_to(payload_root)
                return candidate
        return payload_root

    @staticmethod
    def _copy_resource(root: Path, resource_key: str, source: Path) -> Path:
        payload = root / "assets" / resource_key / "payload"
        if payload.exists():
            if payload.is_dir():
                shutil.rmtree(payload)
            else:
                payload.unlink()
        payload.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, payload)
            return payload
        payload.mkdir(parents=True, exist_ok=True)
        target = payload / source.name
        shutil.copy2(source, target)
        return target

    @staticmethod
    def _apply_path_map(draft_dir: Path, path_map: dict[str, str]) -> None:
        if not path_map:
            return
        for name in ("draft_content.json", "draft_meta_info.json"):
            path = draft_dir / name
            if not path.is_file():
                continue
            data = _read_json(path)
            rewritten = _rewrite_paths(data, path_map)
            _write_json(path, rewritten)

    @staticmethod
    def _cache_candidates(kind: str, identifiers: dict[str, str]) -> list[str]:
        tokens = []
        for key in ("resource_id", "effect_id", "third_resource_id"):
            value = str(identifiers.get(key) or "").strip()
            if value and value not in tokens:
                tokens.append(value)
        roots = {
            "text_effect": ("artistEffect", "effect"),
            "video_effect": ("effect",),
            "text_template_resource": ("textTemplate", "effect"),
            "sticker": ("effect", "sticker"),
            "font": ("font", "effect"),
        }.get(kind, ("effect",))
        return [f"{root}/{token}" for token in tokens for root in roots]
