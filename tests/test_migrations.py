from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(database: Path, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_audio_review_migration_preserves_existing_batch_items():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0009_pronunciation_dict")

        timestamp = "2026-07-28 00:00:00"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "migration-user", "hash", 0, 1, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batches "
                "(id, user_id, name, workflow_type, audio_mode, request_key, "
                "status, total_items, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "batch-before-0010",
                    1,
                    "existing batch",
                    "digital_human",
                    "minimax",
                    "migration-preserve",
                    "ACTIVE",
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO generation_batch_items "
                "(id, batch_id, row_number, row_key, manifest_json, "
                "audio_status, status, error_code, error_message, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "item-before-0010",
                    "batch-before-0010",
                    1,
                    "SCRIPT-001",
                    '{"speech_script":"existing"}',
                    "SUCCESS",
                    "SEGMENTS_CREATED",
                    None,
                    None,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM generation_batch_items "
                "WHERE id = 'item-before-0010'"
            ).fetchone()[0]
            review_required = connection.execute(
                "SELECT review_required FROM generation_batches "
                "WHERE id = 'batch-before-0010'"
            ).fetchone()[0]
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert item_count == 1
        assert review_required == 0
        assert foreign_key_errors == []
    finally:
        database.unlink(missing_ok=True)
