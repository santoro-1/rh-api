from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any


ASSET_ADMIN_SCHEMA = "jyd_probe.asset_admin.v1"
ASSET_KINDS = {
    "audio",
    "font",
    "effect",
    "sticker",
    "corner_sticker",
    "text_effect",
    "text_style",
    "text_template",
    "template",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AssetAdminCatalog:
    """Persistent operator metadata layered over immutable collected assets."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.Lock()

    def decorate(
        self,
        kind: str,
        items: list[dict[str, Any]],
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        asset_kind = self._kind(kind)
        with self._lock:
            data = self._load()
        overrides = data.get("assets", {})
        result: list[dict[str, Any]] = []
        for source in items:
            if not isinstance(source, dict):
                continue
            identity = str(source.get("identity", "")).strip()
            if not identity:
                continue
            item = deepcopy(source)
            override = overrides.get(self._key(asset_kind, identity), {})
            if not isinstance(override, dict):
                override = {}
            original_name = str(item.get("name", "")).strip() or identity
            item["kind"] = asset_kind
            item["original_name"] = original_name
            item["name"] = str(override.get("name", "")).strip() or original_name
            item["category"] = str(override.get("category", "")).strip()
            item["enabled"] = bool(override.get("enabled", True))
            item["deleted"] = bool(override.get("deleted", False))
            item["deleted_at"] = str(override.get("deleted_at", ""))
            item["admin_updated_at"] = str(override.get("updated_at", ""))
            if override.get("purged"):
                continue
            if item["deleted"] and not include_deleted:
                continue
            result.append(item)
        return result

    def update(
        self,
        kind: str,
        identity: str,
        *,
        name: str | None = None,
        category: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        asset_kind = self._kind(kind)
        asset_identity = self._identity(identity)
        with self._lock:
            data = self._load()
            assets = data.setdefault("assets", {})
            record = assets.setdefault(self._key(asset_kind, asset_identity), {})
            if name is not None:
                record["name"] = self._short_text(name, "素材名称", 120)
            if category is not None:
                record["category"] = self._short_text(category, "分类标签", 80)
            if enabled is not None:
                record["enabled"] = bool(enabled)
            record["updated_at"] = _now()
            data["updated_at"] = record["updated_at"]
            self._save(data)
            return deepcopy(record)

    def move_to_trash(self, kind: str, identity: str) -> dict[str, Any]:
        record = self.update(kind, identity, enabled=False)
        with self._lock:
            data = self._load()
            stored = data["assets"][self._key(self._kind(kind), self._identity(identity))]
            stored["deleted"] = True
            stored["deleted_at"] = _now()
            stored["updated_at"] = stored["deleted_at"]
            data["updated_at"] = stored["updated_at"]
            self._save(data)
            return deepcopy(stored)

    def restore(self, kind: str, identity: str) -> dict[str, Any]:
        asset_kind = self._kind(kind)
        asset_identity = self._identity(identity)
        with self._lock:
            data = self._load()
            assets = data.setdefault("assets", {})
            record = assets.setdefault(self._key(asset_kind, asset_identity), {})
            record["deleted"] = False
            record["enabled"] = True
            record.pop("deleted_at", None)
            record.pop("purged", None)
            record.pop("purged_at", None)
            record["updated_at"] = _now()
            data["updated_at"] = record["updated_at"]
            self._save(data)
            return deepcopy(record)

    def deleted_records(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load()
        result: list[dict[str, Any]] = []
        for key, raw in data.get("assets", {}).items():
            if not isinstance(raw, dict) or not raw.get("deleted") or raw.get("purged"):
                continue
            kind, separator, identity = str(key).partition("|")
            if not separator or kind not in ASSET_KINDS or not identity:
                continue
            result.append({"kind": kind, "identity": identity, **deepcopy(raw)})
        return result

    def mark_purged(self, kind: str, identity: str) -> dict[str, Any]:
        asset_kind = self._kind(kind)
        asset_identity = self._identity(identity)
        with self._lock:
            data = self._load()
            assets = data.setdefault("assets", {})
            record = assets.setdefault(self._key(asset_kind, asset_identity), {})
            record["enabled"] = False
            record["deleted"] = True
            record["purged"] = True
            record["purged_at"] = _now()
            record["updated_at"] = record["purged_at"]
            data["updated_at"] = record["updated_at"]
            self._save(data)
            return deepcopy(record)

    @staticmethod
    def _kind(value: str) -> str:
        kind = str(value).strip().lower()
        if kind not in ASSET_KINDS:
            raise ValueError(f"不支持的素材类型: {kind!r}")
        return kind

    @staticmethod
    def _identity(value: str) -> str:
        identity = str(value).strip()
        if not identity or len(identity) > 300 or any(char in identity for char in "\r\n\x00"):
            raise ValueError("素材 identity 无效")
        return identity

    @staticmethod
    def _key(kind: str, identity: str) -> str:
        return f"{kind}|{identity}"

    @staticmethod
    def _short_text(value: str, label: str, limit: int) -> str:
        text = str(value).strip()
        if len(text) > limit:
            raise ValueError(f"{label}不能超过 {limit} 个字符")
        return text

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema": ASSET_ADMIN_SCHEMA, "updated_at": "", "assets": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"schema": ASSET_ADMIN_SCHEMA, "updated_at": "", "assets": {}}
        if not isinstance(data, dict) or data.get("schema") != ASSET_ADMIN_SCHEMA:
            return {"schema": ASSET_ADMIN_SCHEMA, "updated_at": "", "assets": {}}
        if not isinstance(data.get("assets"), dict):
            data["assets"] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
