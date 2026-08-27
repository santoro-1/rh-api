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


def test_h3_remote_asr_migration_preserves_existing_head_trim_job() -> None:
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-h3-remote-asr-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0045_h3_remote_head_trim_jobs")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                "VALUES (1, 'h3-asr-user', 'hash', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO generation_tasks "
                "(id, user_id, workflow_type, image_path, audio_path, image_original_name, "
                "audio_original_name, audio_duration_seconds, start_seconds, end_seconds, "
                "prompt, status, runninghub_auto_retry_count, created_at, updated_at) "
                "VALUES ('h3-task', 1, 'minimax_h3_ref2va', 'image.png', 'audio.mp3', "
                "'image.png', 'audio.mp3', 10, 0, 10, 'test', 'RUNNING', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO h3_head_trim_jobs "
                "(id, generation_task_id, source_video_path, source_video_name, "
                "script_text, status, decision_json, created_at, updated_at) "
                "VALUES ('head-job', 'h3-task', 'outputs/raw.mp4', 'raw.mp4', "
                "'你好世界', 'SUCCESS', '{\"trimSeconds\":0.2}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            migrated = connection.execute(
                "SELECT user_id, generation_task_id, action, source_path, "
                "source_name, script_sha256, result_json "
                "FROM h3_remote_asr_jobs WHERE id = 'head-job'"
            ).fetchone()
            old_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='h3_head_trim_jobs'"
            ).fetchone()
        connection.close()
        assert revision == "0047_h3_prompt_override"
        assert migrated[:5] == (
            1,
            "h3-task",
            "h3_head_trim",
            "outputs/raw.mp4",
            "raw.mp4",
        )
        assert len(migrated[5]) == 64
        assert '"trimSeconds":0.2' in migrated[6]
        assert old_table is None

        _run_alembic(
            database,
            "0045_h3_remote_head_trim_jobs",
            command="downgrade",
        )
        with sqlite3.connect(database) as connection:
            restored = connection.execute(
                "SELECT generation_task_id, source_video_path, decision_json "
                "FROM h3_head_trim_jobs WHERE id = 'head-job'"
            ).fetchone()
        connection.close()
        assert restored == (
            "h3-task",
            "outputs/raw.mp4",
            '{"trimSeconds":0.2}',
        )
    finally:
        database.unlink(missing_ok=True)


def test_h3_manual_prompt_override_migration_adds_nullable_frozen_field() -> None:
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-h3-prompt-override-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0046_h3_remote_asr_jobs")
        _run_alembic(database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            columns = {
                row[1]: row[3]
                for row in connection.execute(
                    "PRAGMA table_info('h3_batch_configs')"
                )
            }
        connection.close()

        assert revision == "0047_h3_prompt_override"
        assert columns["prompt_override"] == 0
    finally:
        database.unlink(missing_ok=True)


def test_h3_motion_reference_pool_migration_is_reversible() -> None:
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-h3-motion-pool-{uuid.uuid4().hex}.db"
    try:
        _run_alembic(database, "0042_h3_loop_anchor_mode")
        _run_alembic(database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('h3_segment_configs')"
                )
            }
        connection.close()
        assert revision == "0047_h3_prompt_override"
        assert {
            "motion_reference_index",
            "motion_reference_path",
            "motion_reference_sha256",
        } <= columns

        _run_alembic(
            database,
            "0042_h3_loop_anchor_mode",
            command="downgrade",
        )
        with sqlite3.connect(database) as connection:
            columns_after_downgrade = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('h3_segment_configs')"
                )
            }
        connection.close()
        assert not {
            "motion_reference_index",
            "motion_reference_path",
            "motion_reference_sha256",
        } & columns_after_downgrade
    finally:
        database.unlink(missing_ok=True)


def test_h3_access_password_migration_preserves_existing_capability() -> None:
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-h3-access-{uuid.uuid4().hex}.db"
    timestamp = "2026-08-22 18:00:00"
    try:
        _run_alembic(database, "0039_h3_workbench_snapshots")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO runninghub_execution_accounts "
                "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                "digital_human_ai_app_id, max_concurrent_tasks, is_enabled, health_status, "
                "created_at, updated_at) VALUES (1, 'H3', 'encrypted', ?, "
                "'https://example', 'digital', 5, 1, 'UNKNOWN', ?, ?)",
                ("a" * 64, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_h3_capabilities "
                "(execution_account_id, is_enabled, workflow_id, instance_type, "
                "max_concurrent_tasks, safe_note, created_at, updated_at) "
                "VALUES (1, 1, 'workflow', 'plus', 3, 'note', ?, ?)",
                (timestamp, timestamp),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('runninghub_h3_capabilities')"
                )
            }
            row = connection.execute(
                "SELECT workflow_id, access_password_encrypted "
                "FROM runninghub_h3_capabilities WHERE execution_account_id=1"
            ).fetchone()
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert revision == "0047_h3_prompt_override"
        assert "access_password_encrypted" in columns
        assert row == ("workflow", None)
        assert foreign_key_errors == []

        _run_alembic(
            database,
            "0039_h3_workbench_snapshots",
            command="downgrade",
        )
        with sqlite3.connect(database) as connection:
            revision_after_downgrade = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            columns_after_downgrade = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('runninghub_h3_capabilities')"
                )
            }
            preserved = connection.execute(
                "SELECT workflow_id FROM runninghub_h3_capabilities "
                "WHERE execution_account_id=1"
            ).fetchone()
        connection.close()

        assert revision_after_downgrade == "0039_h3_workbench_snapshots"
        assert "access_password_encrypted" not in columns_after_downgrade
        assert preserved == ("workflow",)
    finally:
        database.unlink(missing_ok=True)


def test_h3_user_access_migration_backfills_existing_h3_members() -> None:
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-h3-user-access-{uuid.uuid4().hex}.db"
    timestamp = "2026-08-23 12:00:00"
    try:
        _run_alembic(database, "0040_h3_access_password")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                "VALUES (1, 'enabled-member', 'hash', 1, 1, ?, ?), "
                "(2, 'not-a-member', 'hash', 1, 1, ?, ?)",
                (timestamp, timestamp, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_execution_accounts "
                "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                "digital_human_ai_app_id, max_concurrent_tasks, is_enabled, health_status, "
                "created_at, updated_at) VALUES (1, 'H3', 'encrypted', ?, "
                "'https://example', 'digital', 5, 1, 'UNKNOWN', ?, ?)",
                ("b" * 64, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_h3_capabilities "
                "(execution_account_id, is_enabled, workflow_id, instance_type, "
                "max_concurrent_tasks, safe_note, access_password_encrypted, created_at, updated_at) "
                "VALUES (1, 1, 'workflow', 'plus', 3, 'note', NULL, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runninghub_pool_memberships "
                "(admin_user_id, execution_account_id, created_at) VALUES (1, 1, ?)",
                (timestamp,),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            grants = connection.execute(
                "SELECT id, h3_access_enabled FROM users ORDER BY id"
            ).fetchall()
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert revision == "0047_h3_prompt_override"
        assert grants == [(1, 1), (2, 0)]
        assert foreign_key_errors == []
    finally:
        database.unlink(missing_ok=True)


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
        dual_pool_grant_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('runninghub_dual_pool_grants')"
            )
        }
        seedvr2_account_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('seedvr2_execution_accounts')"
            )
        }
        enhancement_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('generation_task_enhancements')"
            )
        }
        enhancement_attempt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('generation_task_enhancement_attempts')"
            )
        }
        balance_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('runninghub_credential_balances')"
            )
        }
        ltx_preparation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('ltx_preparation_jobs')"
            )
        }
        multi_camera_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'multi_camera_%'"
            )
        }
        h3_capability_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('runninghub_h3_capabilities')"
            )
        }
        h3_workbench_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'h3_%_configs'"
            )
        }
        h3_remote_asr_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('h3_remote_asr_jobs')"
            )
        }
    assert revision == "0047_h3_prompt_override"
    assert "runninghub_failed_reason" in task_columns
    assert "runninghub_attempt_history" in task_columns
    assert "runninghub_auto_retry_count" in task_columns
    assert "seedvr2_enabled" in task_columns
    assert "runninghub_auto_retry_after" in task_columns
    assert "execution_account_id" in task_columns
    assert "batch_item_id" in long_audio_columns
    assert "video_review_required" in batch_columns
    assert "source_channel" in batch_columns
    assert "correlation_id" in batch_columns
    assert "runninghub_execution_account_ids_json" in batch_columns
    assert "seedvr2_execution_account_ids_json" in batch_columns
    assert "execution_mode" in batch_columns
    assert "runninghub_execution_account_ids_json" in item_columns
    assert "seedvr2_execution_account_ids_json" in item_columns
    assert {"user_id", "is_enabled", "allow_non_admin", "note"} <= dual_pool_grant_columns
    assert {
        "api_key_encrypted",
        "credential_fingerprint",
        "base_url",
        "seedvr2_ai_app_id",
        "max_concurrent_tasks",
    } <= seedvr2_account_columns
    assert "seedvr2_execution_account_id" in enhancement_columns
    assert "seedvr2_execution_account_id" in enhancement_attempt_columns
    assert {
        "credential_fingerprint",
        "balance_status",
        "remain_coins",
        "remain_money",
        "currency",
        "api_type",
        "remote_current_task_count",
        "checked_at",
    } <= balance_columns
    assert {
        "batch_item_id",
        "long_audio_project_id",
        "source_video_path",
        "source_audio_path",
        "script_text",
        "alignment_timeline_json",
        "segment_plan_json",
        "status",
    } <= ltx_preparation_columns
    assert {
        "generation_task_id",
        "staged_asset_id",
        "action",
        "source_path",
        "source_sha256",
        "script_text",
        "script_sha256",
        "status",
        "result_json",
        "remote_lease_id",
        "remote_lease_expires_at",
    } <= h3_remote_asr_columns
    assert {
        "multi_camera_user_access",
        "multi_camera_batch_configs",
        "multi_camera_image_groups",
        "multi_camera_image_group_assets",
        "multi_camera_item_bindings",
        "multi_camera_segment_bindings",
    } <= multi_camera_tables
    assert {
        "execution_account_id",
        "is_enabled",
        "workflow_id",
        "instance_type",
        "max_concurrent_tasks",
        "safe_note",
    } <= h3_capability_columns
    assert {
        "h3_batch_configs",
        "h3_item_configs",
        "h3_segment_configs",
    } <= h3_workbench_tables
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
        "title_analysis_status",
        "music_intent_json",
        "subtitle_units_json",
        "title_json",
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

        assert version == "0047_h3_prompt_override"
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

        assert revision == "0047_h3_prompt_override"
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

        assert revision == "0047_h3_prompt_override"
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


def test_dual_pool_migration_preserves_seedvr2_rows_and_seeds_controlled_grant():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-dual-pool-{uuid.uuid4().hex}.db"
    timestamp = "2026-08-12 09:00:00"
    try:
        _run_alembic(database, "0030_content_analysis_title")
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                "VALUES (7, 'Cx_ceshi', 'hash', 0, 1, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batches "
                "(id, user_id, name, workflow_type, source_channel, audio_mode, "
                "review_required, video_review_required, request_key, status, total_items, "
                "created_at, updated_at) VALUES "
                "('dual-batch', 7, 'existing', 'digital_human', 'new_workbench', 'upload', "
                "0, 0, 'dual-request', 'ACTIVE', 1, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_tasks "
                "(id, user_id, workflow_type, image_path, audio_path, image_original_name, "
                "audio_original_name, audio_duration_seconds, start_seconds, end_seconds, "
                "prompt, status, runninghub_auto_retry_count, created_at, updated_at) VALUES "
                "('dual-task', 7, 'digital_human', 'image.png', 'audio.mp3', 'image.png', "
                "'audio.mp3', 10, 0, 10, 'prompt', 'COMPLETED', 0, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_task_enhancements "
                "(id, generation_task_id, provider, workflow_kind, status, source_result_path, "
                "auto_retry_count, created_at, updated_at) VALUES "
                "('dual-enhancement', 'dual-task', 'runninghub', 'seedvr2_upscale', "
                "'PENDING', 'source.mp4', 0, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_task_enhancement_attempts "
                "(id, enhancement_id, attempt_number, status, created_at, updated_at) VALUES "
                "('dual-enhancement-attempt', 'dual-enhancement', 1, 'RESERVED', ?, ?)",
                (timestamp, timestamp),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            grant = connection.execute(
                "SELECT user_id, is_enabled, allow_non_admin "
                "FROM runninghub_dual_pool_grants"
            ).fetchone()
            batch_snapshot = connection.execute(
                "SELECT execution_mode, seedvr2_execution_account_ids_json "
                "FROM generation_batches WHERE id='dual-batch'"
            ).fetchone()
            connection.execute(
                "INSERT INTO seedvr2_execution_accounts "
                "(id, label, api_key_encrypted, credential_fingerprint, base_url, "
                "seedvr2_ai_app_id, max_concurrent_tasks, is_enabled, health_status, "
                "created_at, updated_at) VALUES "
                "(21, 'SeedVR2 一号', 'encrypted', 'seed-fingerprint', "
                "'https://www.runninghub.cn', 'seed-app', 5, 1, 'UNKNOWN', ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO seedvr2_pool_memberships "
                "(user_id, execution_account_id, created_at) VALUES (7, 21, ?)",
                (timestamp,),
            )
            connection.execute(
                "UPDATE generation_task_enhancements "
                "SET seedvr2_execution_account_id=21 WHERE id='dual-enhancement'"
            )
            connection.execute(
                "UPDATE generation_task_enhancement_attempts "
                "SET seedvr2_execution_account_id=21 "
                "WHERE id='dual-enhancement-attempt'"
            )
            connection.commit()
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert revision == "0047_h3_prompt_override"
        assert grant == (7, 1, 1)
        assert batch_snapshot == (None, None)
        assert foreign_key_errors == []

        _run_alembic(database, "0030_content_analysis_title", command="downgrade")

        with sqlite3.connect(database) as connection:
            revision_after_downgrade = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            preserved = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM users WHERE id=7), "
                "(SELECT COUNT(*) FROM generation_batches WHERE id='dual-batch'), "
                "(SELECT COUNT(*) FROM generation_tasks WHERE id='dual-task'), "
                "(SELECT COUNT(*) FROM generation_task_enhancements "
                " WHERE id='dual-enhancement'), "
                "(SELECT COUNT(*) FROM generation_task_enhancement_attempts "
                " WHERE id='dual-enhancement-attempt')"
            ).fetchone()
            enhancement_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('generation_task_enhancements')"
                )
            }
            new_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='seedvr2_execution_accounts'"
            ).fetchone()
            foreign_key_errors_after_downgrade = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        connection.close()

        assert revision_after_downgrade == "0030_content_analysis_title"
        assert preserved == (1, 1, 1, 1, 1)
        assert "seedvr2_execution_account_id" not in enhancement_columns
        assert new_table is None
        assert foreign_key_errors_after_downgrade == []
    finally:
        database.unlink(missing_ok=True)


def test_item_execution_pool_migration_copies_legacy_batch_snapshots():
    runtime = PROJECT_ROOT / "tests" / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    database = runtime / f"migration-item-pool-{uuid.uuid4().hex}.db"
    timestamp = "2026-08-15 09:00:00"
    try:
        _run_alembic(database, "0034_runninghub_credential_balance")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
                "VALUES (9, 'item-pool-user', 'hash', 1, 1, ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batches "
                "(id, user_id, name, workflow_type, source_channel, audio_mode, "
                "review_required, video_review_required, request_key, status, total_items, "
                "runninghub_execution_account_ids_json, seedvr2_execution_account_ids_json, "
                "created_at, updated_at) VALUES "
                "('item-pool-batch', 9, 'existing', 'digital_human', 'new_workbench', "
                "'upload', 0, 0, 'item-pool-request', 'ACTIVE', 1, '[3,5]', '[7]', ?, ?)",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO generation_batch_items "
                "(id, batch_id, row_number, row_key, manifest_json, audio_status, status, "
                "created_at, updated_at) VALUES "
                "('item-pool-row', 'item-pool-batch', 1, '001', '{}', 'SUCCESS', "
                "'VIDEO_PENDING', ?, ?)",
                (timestamp, timestamp),
            )
            connection.commit()
        connection.close()

        _run_alembic(database, "head")

        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
            snapshots = connection.execute(
                "SELECT runninghub_execution_account_ids_json, "
                "seedvr2_execution_account_ids_json FROM generation_batch_items "
                "WHERE id='item-pool-row'"
            ).fetchone()
        connection.close()

        assert revision == "0047_h3_prompt_override"
        assert snapshots == ("[3,5]", "[7]")
    finally:
        database.unlink(missing_ok=True)


def test_seedvr2_switch_migration_preserves_task_and_enhancement(tmp_path):
    database = tmp_path / "seedvr2-switch-parent-child.db"
    _run_alembic(database, "0032_runninghub_pool_runtime_control")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users "
            "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
            "VALUES (1, 'migration-user', 'hash', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO generation_tasks "
            "(id, user_id, workflow_type, image_path, audio_path, image_original_name, "
            "audio_original_name, audio_duration_seconds, start_seconds, end_seconds, "
            "prompt, status, runninghub_auto_retry_count, created_at, updated_at) "
            "VALUES ('task-1', 1, 'digital_human', 'image.png', 'audio.mp3', "
            "'image.png', 'audio.mp3', 10, 0, 10, 'test', 'RUNNING', 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO generation_task_enhancements "
            "(id, generation_task_id, provider, workflow_kind, status, "
            "source_result_path, auto_retry_count, created_at, updated_at) "
            "VALUES ('enhancement-1', 'task-1', 'runninghub', 'seedvr2_upscale', "
            "'PENDING', 'source.mp4', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        connection.commit()

    _run_alembic(database, "head")
    with sqlite3.connect(database) as connection:
        task = connection.execute(
            "SELECT id, seedvr2_enabled FROM generation_tasks"
        ).fetchone()
        enhancement = connection.execute(
            "SELECT id, generation_task_id FROM generation_task_enhancements"
        ).fetchone()
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

    assert task == ("task-1", 1)
    assert enhancement == ("enhancement-1", "task-1")
    assert foreign_key_errors == []


def test_multi_camera_migration_bootstraps_only_exact_active_accounts(tmp_path):
    database = tmp_path / "multi-camera-access.db"
    _run_alembic(database, "0036_ltx_workbench_preparation")
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO users "
            "(id, username, password_hash, is_admin, is_active, created_at, updated_at) "
            "VALUES (?, ?, 'hash', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            [
                (1, "admin", 1, 1),
                (2, "Cx_ceshi", 0, 1),
                (3, "other-admin", 1, 1),
                (4, "cx_ceshi", 0, 1),
            ],
        )
        connection.commit()

    _run_alembic(database, "head")
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        grants = connection.execute(
            "SELECT user_id, is_enabled FROM multi_camera_user_access ORDER BY user_id"
        ).fetchall()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert revision == "0047_h3_prompt_override"
    assert grants == [(1, 1), (2, 1)]
    assert foreign_key_errors == []
