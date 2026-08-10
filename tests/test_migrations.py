from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(database: Path, revision: str, command: str = "upgrade") -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
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
        task_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('generation_tasks')"
            )
        }
        long_audio_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('long_audio_projects')"
            )
        }
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('generation_batches')"
            )
        }
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('generation_batch_items')"
            )
        }
        audio_task_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info('audio_generation_tasks')"
            )
        }
        ark_config_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('ark_configs')"
            )
        }
        content_analysis_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('content_analysis_caches')"
            )
        }
        visual_analysis_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('visual_analysis_caches')"
            )
        }
    assert revision == "0029_seedvr2_video_enhancement"
    assert "runninghub_failed_reason" in task_columns
    assert "runninghub_attempt_history" in task_columns
    assert "runninghub_auto_retry_count" in task_columns
    assert "runninghub_auto_retry_after" in task_columns
    assert "execution_account_id" in task_columns
    assert "batch_item_id" in long_audio_columns
    assert "video_review_required" in batch_columns
    assert "source_channel" in batch_columns
    assert "correlation_id" in batch_columns
    assert "runninghub_execution_account_ids_json" in batch_columns
    assert {
        "merged_video_status",
        "merged_video_path",
        "merged_video_error",
        "merged_at",
        "merged_reviewed_at",
    } <= item_columns
    assert audio_task_columns["primary_kind"][3] == 0
    assert audio_task_columns["primary_path"][3] == 0
    assert audio_task_columns["primary_original_name"][3] == 0
    assert audio_task_columns["primary_sha256"][3] == 0
    assert {
        "user_id",
        "enabled",
        "api_key_encrypted",
        "base_url",
        "model",
        "timeout_seconds",
        "max_retries",
    } <= ark_config_columns
    assert {
        "user_id",
        "script_sha256",
        "schema_version",
        "prompt_version",
        "model",
        "overall_status",
        "music_analysis_status",
        "subtitle_analysis_status",
        "music_intent_json",
        "subtitle_units_json",
        "cacheable",
    } <= content_analysis_columns
    assert {
        "user_id",
        "script_sha256",
        "catalog_version",
        "candidate_set_sha256",
        "schema_version",
        "prompt_version",
        "model",
        "result_json",
    } <= visual_analysis_columns


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
            source_channel = connection.execute(
                "SELECT source_channel FROM generation_batches "
                "WHERE id = 'batch-before-0010'"
            ).fetchone()[0]
            correlation_id = connection.execute(
                "SELECT correlation_id FROM generation_batches "
                "WHERE id = 'batch-before-0010'"
            ).fetchone()[0]
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert item_count == 1
        assert review_required == 0
        assert source_channel == "legacy_web"
        assert correlation_id == "batch-before-0010"
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

        assert version == "0029_seedvr2_video_enhancement"
        assert category_columns == 1
        assert quick_check == "ok"
    finally:
        database.unlink(missing_ok=True)


def test_shared_minimax_voice_migration_backfills_same_key_accounts():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-shared-voice-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0023_batch_correlation_id")
        timestamp = "2026-08-07 00:00:00"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for user_id, username in ((1, "voice-share-a"), (2, "voice-share-b")):
                connection.execute(
                    "INSERT INTO users "
                    "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                    "VALUES (?, ?, 'hash', 0, 1, ?, ?)",
                    (user_id, username, timestamp, timestamp),
                )
                connection.execute(
                    "INSERT INTO minimax_configs "
                    "(id, user_id, api_key_encrypted, account_binding_id, account_label, "
                    "credential_fingerprint, base_url, requests_per_minute, created_at, updated_at) "
                    "VALUES (?, ?, 'encrypted', ?, '共享账号', 'same-fingerprint', "
                    "'https://api.minimax.io', 20, ?, ?)",
                    (user_id, user_id, f"binding-{user_id}", timestamp, timestamp),
                )
            connection.execute(
                "INSERT INTO minimax_voice_assets "
                "(id, user_id, config_id, name, voice_id, credential_fingerprint, status, "
                "source_relative_path, source_original_name, remote_file_id, activated_at, "
                "expires_at, created_at, updated_at, method, is_saved, preview_relative_path, "
                "account_binding_id, category) "
                "VALUES ('voice-a', 1, 1, '共享音色', 'provider-shared', 'rotated-old-fingerprint', "
                "'ACTIVE', NULL, NULL, NULL, ?, NULL, ?, ?, 'clone', 1, NULL, 'binding-1', NULL)",
                (timestamp, timestamp, timestamp),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            bindings = connection.execute(
                "SELECT account_binding_id FROM minimax_configs ORDER BY id"
            ).fetchall()
            voices = connection.execute(
                "SELECT user_id, config_id, voice_id, status, account_binding_id "
                "FROM minimax_voice_assets WHERE voice_id='provider-shared' ORDER BY config_id"
            ).fetchall()
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert revision == "0029_seedvr2_video_enhancement"
        assert bindings == [("binding-1",), ("binding-2",)]
        assert voices == [
            (1, 1, "provider-shared", "ACTIVE", "binding-1"),
            (2, 2, "provider-shared", "ACTIVE", "binding-2"),
        ]
        assert foreign_key_errors == []
    finally:
        database.unlink(missing_ok=True)


def test_runninghub_execution_pool_migration_preserves_existing_parent_child_rows():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-runninghub-pool-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0024_shared_minimax_voices")
        timestamp = "2026-08-07 12:00:00"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                "VALUES (1, 'pool-admin', 'hash', 1, 1, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_configs "
                "(id, user_id, api_key_encrypted, base_url, ai_app_id, instance_type, "
                "default_prompt, max_concurrent_tasks, created_at, updated_at) "
                "VALUES (1, 1, 'encrypted', 'https://www.runninghub.cn', 'app-1', "
                "'default', 'prompt', 5, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batches "
                "(id, user_id, name, workflow_type, source_channel, correlation_id, "
                "audio_mode, request_key, status, total_items, created_at, updated_at) "
                "VALUES ('pool-batch', 1, 'existing', 'digital_human', 'new_workbench', "
                "'pool-correlation', 'upload', 'pool-request', 'ACTIVE', 1, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batch_items "
                "(id, batch_id, row_number, row_key, manifest_json, audio_status, status, "
                "created_at, updated_at) VALUES "
                "('pool-item', 'pool-batch', 1, '001', '{}', 'SUCCESS', "
                "'VIDEO_PENDING', ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_tasks "
                "(id, user_id, batch_item_id, workflow_type, image_path, audio_path, "
                "image_original_name, audio_original_name, audio_duration_seconds, "
                "start_seconds, end_seconds, prompt, status, runninghub_auto_retry_count, "
                "created_at, updated_at) VALUES "
                "('pool-task', 1, 'pool-item', 'digital_human', 'image.png', 'audio.mp3', "
                "'image.png', 'audio.mp3', 10, 0, 10, 'prompt', 'PENDING', 0, ?, ?)",
                (timestamp, timestamp),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "users",
                    "runninghub_configs",
                    "generation_batches",
                    "generation_batch_items",
                    "generation_tasks",
                )
            }
            connection.execute(
                "INSERT INTO runninghub_execution_accounts "
                "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                "digital_human_ai_app_id, max_concurrent_tasks, is_enabled, health_status, "
                "created_at, updated_at) VALUES "
                "(10, 'RunningHub 一号', 'encrypted-pool', 'fingerprint-1', "
                "'https://www.runninghub.cn', 'pool-app', 5, 1, 'UNKNOWN', ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_pool_memberships "
                "(admin_user_id, execution_account_id, created_at) VALUES (1, 10, ?)",
                (timestamp,),
            )
            connection.execute(
                "UPDATE generation_batches SET runninghub_execution_account_ids_json='[10]' "
                "WHERE id='pool-batch'"
            )
            connection.execute(
                "UPDATE generation_tasks SET execution_account_id=10 WHERE id='pool-task'"
            )
            connection.execute(
                "INSERT INTO generation_task_attempts "
                "(id, generation_task_id, attempt_number, execution_account_id, status, "
                "created_at, updated_at) VALUES "
                "('pool-attempt', 'pool-task', 1, 10, 'RESERVED', ?, ?)",
                (timestamp, timestamp),
            )
            connection.commit()
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            task_binding = connection.execute(
                "SELECT execution_account_id FROM generation_tasks WHERE id='pool-task'"
            ).fetchone()[0]
            attempt_binding = connection.execute(
                "SELECT execution_account_id FROM generation_task_attempts "
                "WHERE id='pool-attempt'"
            ).fetchone()[0]

            duplicate_fingerprint_rejected = False
            try:
                connection.execute(
                    "INSERT INTO runninghub_execution_accounts "
                    "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                    "digital_human_ai_app_id, max_concurrent_tasks, is_enabled, "
                    "health_status, created_at, updated_at) VALUES "
                    "(11, '重复账号', 'encrypted-duplicate', 'fingerprint-1', "
                    "'https://www.runninghub.cn', 'pool-app', 5, 1, 'UNKNOWN', ?, ?)",
                    (timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                duplicate_fingerprint_rejected = True
            connection.rollback()

            invalid_concurrency_rejected = False
            try:
                connection.execute(
                    "INSERT INTO runninghub_execution_accounts "
                    "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                    "digital_human_ai_app_id, max_concurrent_tasks, is_enabled, "
                    "health_status, created_at, updated_at) VALUES "
                    "(12, '超额账号', 'encrypted-over-limit', 'fingerprint-2', "
                    "'https://www.runninghub.cn', 'pool-app', 6, 1, 'UNKNOWN', ?, ?)",
                    (timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                invalid_concurrency_rejected = True
            connection.rollback()

            duplicate_membership_rejected = False
            try:
                connection.execute(
                    "INSERT INTO runninghub_pool_memberships "
                    "(admin_user_id, execution_account_id, created_at) VALUES (1, 10, ?)",
                    (timestamp,),
                )
            except sqlite3.IntegrityError:
                duplicate_membership_rejected = True
            connection.rollback()

            duplicate_attempt_number_rejected = False
            try:
                connection.execute(
                    "INSERT INTO generation_task_attempts "
                    "(id, generation_task_id, attempt_number, execution_account_id, "
                    "status, created_at, updated_at) VALUES "
                    "('pool-attempt-duplicate', 'pool-task', 1, 10, 'RESERVED', ?, ?)",
                    (timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                duplicate_attempt_number_rejected = True
            connection.rollback()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.close()

        assert revision == "0029_seedvr2_video_enhancement"
        assert counts == {
            "users": 1,
            "runninghub_configs": 1,
            "generation_batches": 1,
            "generation_batch_items": 1,
            "generation_tasks": 1,
        }
        assert task_binding == 10
        assert attempt_binding == 10
        assert duplicate_fingerprint_rejected is True
        assert invalid_concurrency_rejected is True
        assert duplicate_membership_rejected is True
        assert duplicate_attempt_number_rejected is True
        assert foreign_key_errors == []

        _run_alembic(database, "0024_shared_minimax_voices", command="downgrade")
        with sqlite3.connect(database) as connection:
            downgraded_revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            downgraded_counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "users",
                    "runninghub_configs",
                    "generation_batches",
                    "generation_batch_items",
                    "generation_tasks",
                )
            }
            downgraded_foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert downgraded_revision == "0024_shared_minimax_voices"
        assert downgraded_counts == counts
        assert downgraded_foreign_key_errors == []
    finally:
        database.unlink(missing_ok=True)
