from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    defaults_json TEXT NOT NULL,
                    identity_images_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    selected_accounts_json TEXT NOT NULL,
                    remote_batch_id TEXT,
                    snapshot_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        return db

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "defaults": json.loads(row["defaults_json"]),
            "identity_images": json.loads(row["identity_images_json"]),
            "rows": json.loads(row["rows_json"]),
            "selected_account_ids": json.loads(row["selected_accounts_json"]),
            "remote_batch_id": row["remote_batch_id"],
            "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create(self, owner_key: str, name: str) -> dict[str, Any]:
        clean_name = name.strip() or "未命名 H3 项目"
        if len(clean_name) > 100:
            raise StoreError("项目名称不能超过 100 个字符")
        project_id = uuid.uuid4().hex
        now = _now()
        defaults = {
            "continuity_mode": "fast",
            "generation_tail_seconds": 0.5,
            "user_direction": "",
            "resolution": {
                "aspect_ratio": "9:16 (Portrait Widescreen)",
                "megapixels": 1.0,
                "multiple": 32,
            },
        }
        with self._connect() as db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    project_id,
                    owner_key,
                    clean_name,
                    json.dumps(defaults, ensure_ascii=False),
                    "[]",
                    "[]",
                    "[]",
                    now,
                    now,
                ),
            )
        return self.get(owner_key, project_id)

    def list(self, owner_key: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM projects WHERE owner_key=? ORDER BY updated_at DESC",
                (owner_key,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, owner_key: str, project_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE id=? AND owner_key=?",
                (project_id, owner_key),
            ).fetchone()
        if row is None:
            raise StoreError("项目不存在")
        return self._decode(row)

    def update(self, owner_key: str, project_id: str, **values: object) -> dict[str, Any]:
        self.get(owner_key, project_id)
        columns: list[str] = []
        arguments: list[object] = []
        json_fields = {
            "defaults": "defaults_json",
            "identity_images": "identity_images_json",
            "rows": "rows_json",
            "selected_account_ids": "selected_accounts_json",
            "snapshot": "snapshot_json",
        }
        for key, value in values.items():
            if key in json_fields:
                columns.append(f"{json_fields[key]}=?")
                arguments.append(json.dumps(value, ensure_ascii=False))
            elif key in {"name", "remote_batch_id"}:
                columns.append(f"{key}=?")
                arguments.append(value)
            else:
                raise StoreError("项目字段不受支持")
        if not columns:
            return self.get(owner_key, project_id)
        columns.append("updated_at=?")
        arguments.append(_now())
        arguments.extend([project_id, owner_key])
        with self._connect() as db:
            db.execute(
                f"UPDATE projects SET {', '.join(columns)} WHERE id=? AND owner_key=?",
                arguments,
            )
        return self.get(owner_key, project_id)
