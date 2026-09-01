from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing

from tests.test_migrations import PROJECT_ROOT, _run_alembic


def test_device_tables_preserve_existing_accounts_paid_tasks_and_repeat_upgrade():
    database = (
        PROJECT_ROOT / "tests" / ".runtime" / f"migration-device-{uuid.uuid4().hex}.db"
    )
    try:
        _run_alembic(database, "0048_legacy_task_runninghub_pool")
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id,username,password_hash,is_admin,is_active,created_at,updated_at) VALUES (1,'kept-user','kept-hash',0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO generation_tasks (id,user_id,workflow_type,image_path,audio_path,image_original_name,audio_original_name,audio_duration_seconds,start_seconds,end_seconds,prompt,status,runninghub_auto_retry_count,runninghub_task_id,created_at,updated_at) VALUES ('paid-task',1,'minimax_h3_ref2va','image.png','audio.mp3','image.png','audio.mp3',10,0,10,'test','RUNNING',0,'remote-paid-id',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
            connection.commit()
        _run_alembic(database, "head")
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT password_hash FROM users WHERE id=1"
            ).fetchone() == ("kept-hash",)
            assert connection.execute(
                "SELECT status,runninghub_task_id FROM generation_tasks WHERE id='paid-task'"
            ).fetchone() == ("RUNNING", "remote-paid-id")
            assert connection.execute(
                "SELECT mode FROM workbench_device_control WHERE id=1"
            ).fetchone() == ("OFF",)
            connection.execute(
                "INSERT INTO workbench_devices (id,thumbprint,public_jwk_json,status,protection_report,protection_verified,created_at,last_seen_at) VALUES ('device','thumbprint','{}','ACTIVE','tpm',0,1,1)"
            )
            connection.execute(
                "INSERT INTO workbench_device_grants (id,user_id,device_id,status,label,client_version,scopes_json,revision,created_at,updated_at) VALUES ('grant',1,'device','ACTIVE','kept','v1','[]',2,1,1)"
            )
            connection.commit()
        _run_alembic(database, "head")
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute(
                "SELECT status,revision FROM workbench_device_grants WHERE id='grant'"
            ).fetchone() == ("ACTIVE", 2)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.unlink(missing_ok=True)


def test_work_admission_upgrade_keeps_grants_and_does_not_rebuild_parent_tables():
    database = (
        PROJECT_ROOT
        / "tests"
        / ".runtime"
        / f"migration-admission-{uuid.uuid4().hex}.db"
    )
    try:
        _run_alembic(database, "0049_workbench_devices")
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO users (id,username,password_hash,is_admin,is_active,created_at,updated_at) VALUES (1,'licensed-user','kept-hash',0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO workbench_devices (id,thumbprint,public_jwk_json,status,protection_report,protection_verified,created_at,last_seen_at) VALUES ('device','thumbprint','{}','ACTIVE','tpm',0,1,1)"
            )
            connection.execute(
                "INSERT INTO workbench_device_grants (id,user_id,device_id,status,label,client_version,scopes_json,revision,created_at,updated_at) VALUES ('grant',1,'device','ACTIVE','kept','v1','[]',2,1,1)"
            )
            connection.execute(
                "UPDATE workbench_device_control SET mode='OBSERVE', revision=3 WHERE id=1"
            )
            before = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('users','generation_tasks','generation_segments','workbench_device_grants') ORDER BY name"
            ).fetchall()
            connection.commit()
        _run_alembic(database, "head")
        _run_alembic(database, "head")
        with closing(sqlite3.connect(database)) as connection:
            assert (
                connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ('users','generation_tasks','generation_segments','workbench_device_grants') ORDER BY name"
                ).fetchall()
                == before
            )
            assert connection.execute(
                "SELECT status,revision FROM workbench_device_grants WHERE id='grant'"
            ).fetchone() == ("ACTIVE", 2)
            assert connection.execute(
                "SELECT mode,revision FROM workbench_device_control WHERE id=1"
            ).fetchone() == ("OBSERVE", 3)
            assert connection.execute(
                "SELECT count(*) FROM workbench_device_operations"
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM workbench_device_work_bindings"
            ).fetchone() == (0,)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.unlink(missing_ok=True)
