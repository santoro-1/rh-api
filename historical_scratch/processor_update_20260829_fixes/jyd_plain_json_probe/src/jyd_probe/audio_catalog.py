from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import threading
from typing import Any
import uuid


AUDIO_MANIFEST_SCHEMA = "jyd_probe.audio_library_manifest.v1"
AUDIO_CATALOG_SCHEMA = "jyd_probe.audio_catalog.v1"
UNCLASSIFIED_CATEGORY_ID = "unclassified"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_category_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return normalized[:64]


class AudioCatalog:
    """Persistent category assignments and round-robin cursors for audio assets."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.manifest_path = self.root / "manifest" / "audio_manifest.json"
        self.catalog_path = self.root / "catalog.json"
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            manifest = self._load_manifest()
            catalog = self._load_catalog()
            assets = self._assets(manifest, catalog)
            categories = self._categories(catalog, assets)
            return {
                "schema": AUDIO_CATALOG_SCHEMA,
                "root": str(self.root),
                "asset_count": len(assets),
                "categories": categories,
                "assets": assets,
            }

    def create_category(self, name: str, category_id: str = "") -> dict[str, Any]:
        display_name = name.strip()
        if not display_name:
            raise ValueError("音乐分类名称不能为空")
        normalized_id = _normalize_category_id(category_id)
        if not normalized_id:
            normalized_id = f"category_{uuid.uuid4().hex[:10]}"
        if normalized_id == UNCLASSIFIED_CATEGORY_ID:
            raise ValueError("unclassified 是系统保留分类")

        with self._lock:
            catalog = self._load_catalog()
            categories = catalog.setdefault("categories", [])
            if any(item.get("id") == normalized_id for item in categories if isinstance(item, dict)):
                raise ValueError(f"音乐分类 ID 已存在: {normalized_id}")
            record = {
                "id": normalized_id,
                "name": display_name,
                "created_at": _now(),
            }
            categories.append(record)
            self._save_catalog(catalog)
            return record

    def assign(self, identity: str, category_ids: list[str]) -> dict[str, Any]:
        asset_identity = identity.strip()
        if not asset_identity:
            raise ValueError("音乐 identity 不能为空")

        with self._lock:
            manifest = self._load_manifest()
            manifest_identities = {
                str(item.get("identity"))
                for item in manifest.get("assets", [])
                if isinstance(item, dict) and item.get("identity")
            }
            if asset_identity not in manifest_identities:
                raise KeyError(f"音乐素材不存在: {asset_identity}")

            catalog = self._load_catalog()
            valid_categories = {
                str(item.get("id"))
                for item in catalog.get("categories", [])
                if isinstance(item, dict) and item.get("id")
            }
            normalized_ids: list[str] = []
            for category_id in category_ids:
                value = _normalize_category_id(str(category_id))
                if not value or value == UNCLASSIFIED_CATEGORY_ID:
                    continue
                if value not in valid_categories:
                    raise KeyError(f"音乐分类不存在: {value}")
                if value not in normalized_ids:
                    normalized_ids.append(value)

            assignments = catalog.setdefault("assignments", {})
            assignments[asset_identity] = normalized_ids
            self._save_catalog(catalog)
            return {
                "identity": asset_identity,
                "category_ids": normalized_ids or [UNCLASSIFIED_CATEGORY_ID],
            }

    def assign_many_to_category(
        self,
        identities: list[str],
        category_name: str,
    ) -> dict[str, Any]:
        """Create/reuse one category and add it to every listed audio asset."""

        display_name = category_name.strip()
        if not display_name:
            raise ValueError("音乐分类名称不能为空")

        asset_identities: list[str] = []
        for identity in identities:
            value = str(identity).strip()
            if value and value not in asset_identities:
                asset_identities.append(value)
        if not asset_identities:
            raise ValueError("没有可归类的音乐素材")

        with self._lock:
            manifest = self._load_manifest()
            manifest_identities = {
                str(item.get("identity"))
                for item in manifest.get("assets", [])
                if isinstance(item, dict) and item.get("identity")
            }
            missing = [identity for identity in asset_identities if identity not in manifest_identities]
            if missing:
                raise KeyError(f"音乐素材不存在: {', '.join(missing)}")

            catalog = self._load_catalog()
            categories = catalog.setdefault("categories", [])
            category = next(
                (
                    item
                    for item in categories
                    if isinstance(item, dict)
                    and str(item.get("name", "")).strip().casefold() == display_name.casefold()
                    and item.get("id")
                ),
                None,
            )
            created = category is None
            if category is None:
                category = {
                    "id": f"category_{uuid.uuid4().hex[:10]}",
                    "name": display_name,
                    "created_at": _now(),
                }
                categories.append(category)

            category_id = str(category["id"])
            assignments = catalog.setdefault("assignments", {})
            changed_count = 0
            for identity in asset_identities:
                assigned = assignments.get(identity, [])
                assigned_ids = [str(item) for item in assigned] if isinstance(assigned, list) else []
                assigned_ids = [
                    value
                    for value in assigned_ids
                    if value and value != UNCLASSIFIED_CATEGORY_ID
                ]
                if category_id not in assigned_ids:
                    assigned_ids.append(category_id)
                    changed_count += 1
                assignments[identity] = assigned_ids

            self._save_catalog(catalog)
            return {
                "category": deepcopy(category),
                "created": created,
                "asset_count": len(asset_identities),
                "changed_count": changed_count,
                "identities": asset_identities,
            }

    def get_asset(self, identity: str) -> dict[str, Any]:
        with self._lock:
            manifest = self._load_manifest()
            catalog = self._load_catalog()
            for asset in self._assets(manifest, catalog):
                if asset["identity"] == identity:
                    return asset
        raise KeyError(f"音乐素材不存在: {identity}")

    def select_next(self, category_id: str) -> dict[str, Any]:
        selected_category = _normalize_category_id(category_id) or UNCLASSIFIED_CATEGORY_ID
        with self._lock:
            manifest = self._load_manifest()
            catalog = self._load_catalog()
            assets = [
                item
                for item in self._assets(manifest, catalog)
                if selected_category in item["category_ids"] and item["available"]
            ]
            if not assets:
                raise RuntimeError(f"音乐分类中没有可用文件: {selected_category}")

            cursors = catalog.setdefault("cursors", {})
            cursor = int(cursors.get(selected_category, 0) or 0)
            selected = assets[cursor % len(assets)]
            cursors[selected_category] = (cursor + 1) % len(assets)
            self._save_catalog(catalog)
            return {
                **selected,
                "selection_mode": "next",
                "selected_category_id": selected_category,
                "sequence_index": cursor % len(assets),
            }

    def file_path(self, identity: str) -> Path:
        asset = self.get_asset(identity)
        path = Path(asset["absolute_path"])
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"音乐文件不存在: {path}")
        return path

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {"schema": AUDIO_MANIFEST_SCHEMA, "assets": []}
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != AUDIO_MANIFEST_SCHEMA:
            raise RuntimeError(f"不支持的音频素材清单: {self.manifest_path}")
        if not isinstance(data.get("assets"), list):
            data["assets"] = []
        return data

    def _load_catalog(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            return {
                "schema": AUDIO_CATALOG_SCHEMA,
                "categories": [],
                "assignments": {},
                "cursors": {},
            }
        data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != AUDIO_CATALOG_SCHEMA:
            raise RuntimeError(f"不支持的音乐分类目录: {self.catalog_path}")
        data.setdefault("categories", [])
        data.setdefault("assignments", {})
        data.setdefault("cursors", {})
        return data

    def _save_catalog(self, catalog: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        catalog["schema"] = AUDIO_CATALOG_SCHEMA
        catalog["updated_at"] = _now()
        temporary_path = self.catalog_path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        temporary_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(self.catalog_path)

    def _assets(self, manifest: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
        assignments = catalog.get("assignments", {})
        if not isinstance(assignments, dict):
            assignments = {}
        result: list[dict[str, Any]] = []
        for order, raw in enumerate(manifest.get("assets", [])):
            if not isinstance(raw, dict) or not raw.get("identity") or not raw.get("file"):
                continue
            identity = str(raw["identity"])
            assigned = assignments.get(identity, [])
            category_ids = [str(item) for item in assigned] if isinstance(assigned, list) else []
            if not category_ids:
                category_ids = [UNCLASSIFIED_CATEGORY_ID]
            absolute_path = (self.root / str(raw["file"])).resolve()
            result.append(
                {
                    **deepcopy(raw),
                    "order": order,
                    "category_ids": category_ids,
                    "available": absolute_path.exists() and absolute_path.is_file(),
                    "absolute_path": str(absolute_path),
                }
            )
        return result

    def _categories(self, catalog: dict[str, Any], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        categories = [
            {"id": UNCLASSIFIED_CATEGORY_ID, "name": "未分类", "system": True}
        ]
        categories.extend(
            deepcopy(item)
            for item in catalog.get("categories", [])
            if isinstance(item, dict) and item.get("id") and item.get("name")
        )
        cursors = catalog.get("cursors", {}) if isinstance(catalog.get("cursors"), dict) else {}
        for category in categories:
            category_id = str(category["id"])
            matching = [asset for asset in assets if category_id in asset["category_ids"]]
            category["asset_count"] = len(matching)
            category["available_count"] = sum(1 for asset in matching if asset["available"])
            category["next_index"] = int(cursors.get(category_id, 0) or 0)
        return categories


class CombinedAudioCatalog:
    """Read several audio libraries as one catalog while keeping public admin writes primary."""

    def __init__(self, roots: list[str | Path]):
        unique: list[Path] = []
        for value in roots:
            path = Path(value).expanduser().resolve()
            if path not in unique:
                unique.append(path)
        if not unique:
            raise ValueError("至少需要一个音乐素材库")
        self.catalogs = [AudioCatalog(path) for path in unique]
        self.primary = self.catalogs[0]
        self.root = self.primary.root
        self._lock = threading.Lock()
        self._cursors: dict[str, int] = {}

    def snapshot(self) -> dict[str, Any]:
        assets: list[dict[str, Any]] = []
        categories_by_id: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for index, catalog in enumerate(self.catalogs):
            snapshot = catalog.snapshot()
            source = "public" if index == 0 else "personal"
            for raw in snapshot.get("assets", []):
                identity = str(raw.get("identity", ""))
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                assets.append({**raw, "library_scope": source})
            for raw in snapshot.get("categories", []):
                category_id = str(raw.get("id", ""))
                if category_id and category_id not in categories_by_id:
                    categories_by_id[category_id] = {
                        key: value
                        for key, value in raw.items()
                        if key not in {"asset_count", "available_count", "next_index"}
                    }
        categories = list(categories_by_id.values())
        for category in categories:
            category_id = str(category["id"])
            matching = [asset for asset in assets if category_id in asset.get("category_ids", [])]
            category["asset_count"] = len(matching)
            category["available_count"] = sum(1 for asset in matching if asset.get("available"))
            category["next_index"] = self._cursors.get(category_id, 0)
        return {
            "schema": AUDIO_CATALOG_SCHEMA,
            "root": str(self.root),
            "roots": [str(catalog.root) for catalog in self.catalogs],
            "asset_count": len(assets),
            "categories": categories,
            "assets": assets,
        }

    def get_asset(self, identity: str) -> dict[str, Any]:
        for catalog in self.catalogs:
            try:
                return catalog.get_asset(identity)
            except KeyError:
                continue
        raise KeyError(f"音乐素材不存在: {identity}")

    def select_next(self, category_id: str) -> dict[str, Any]:
        selected_category = _normalize_category_id(category_id) or UNCLASSIFIED_CATEGORY_ID
        with self._lock:
            assets = [
                item
                for item in self.snapshot().get("assets", [])
                if selected_category in item.get("category_ids", []) and item.get("available")
            ]
            if not assets:
                raise RuntimeError(f"音乐分类中没有可用文件: {selected_category}")
            cursor = self._cursors.get(selected_category, 0)
            selected = assets[cursor % len(assets)]
            self._cursors[selected_category] = (cursor + 1) % len(assets)
            return {
                **selected,
                "selection_mode": "next",
                "selected_category_id": selected_category,
                "sequence_index": cursor % len(assets),
            }

    def file_path(self, identity: str) -> Path:
        path = Path(str(self.get_asset(identity)["absolute_path"]))
        if not path.is_file():
            raise FileNotFoundError(f"音乐文件不存在: {path}")
        return path

    def create_category(self, name: str, category_id: str = "") -> dict[str, Any]:
        return self.primary.create_category(name, category_id)

    def assign(self, identity: str, category_ids: list[str]) -> dict[str, Any]:
        return self.primary.assign(identity, category_ids)

    def assign_many_to_category(self, identities: list[str], category_name: str) -> dict[str, Any]:
        return self.primary.assign_many_to_category(identities, category_name)
