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


def test_alembic_config_resolves_paths_outside_project_directory(tmp_path):
    database = tmp_path / "external-cwd.db"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(PROJECT_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
    assert revision == "0012_long_audio_projects"


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


def test_system_voice_category_migration_resumes_after_interrupted_add_column():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-resume-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0010_audio_review")
        with sqlite3.connect(database) as connection:
            # Reproduce SQLite's non-transactional DDL state: the column was
            # committed, but the launcher stopped before Alembic recorded 0011.
            connection.execute(
                "ALTER TABLE minimax_voice_assets "
                "ADD COLUMN category VARCHAR(50)"
            )
            connection.commit()
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "0010_audio_review"
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            version = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            category_columns = sum(
                row[1] == "category"
                for row in connection.execute(
                    "PRAGMA table_info('minimax_voice_assets')"
                )
            )
            quick_check = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
        connection.close()

        assert version == "0012_long_audio_projects"
        assert category_columns == 1
        assert quick_check == "ok"
    finally:
        database.unlink(missing_ok=True)
