from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3


INSTALL_ROOT = Path(r"D:\JYD\JianyingRenderServer-windows-x64")
DATABASE = INSTALL_ROOT / "data" / "web_storage" / "control.db"
PROJECT_ID = "bc133887b7894e4dabd5632b2b4422f3"
EXPECTED_PROJECT_NO = "DH-20260818-0002"
EXPECTED_OLD_VOICE_ID = "6ac81d9a-eab6-4eb1-9778-f484e5f38542"
RESTORE_POSITIONS = tuple(range(1, 22))


def decode_json(value: str | None) -> dict:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Expected a JSON object")
    return parsed


def main() -> None:
    if not DATABASE.is_file():
        raise RuntimeError(f"Database not found: {DATABASE}")

    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        project = connection.execute(
            "SELECT project_no FROM projects WHERE project_id=?", (PROJECT_ID,)
        ).fetchone()
        if project is None or project["project_no"] != EXPECTED_PROJECT_NO:
            raise RuntimeError("The expected 2026-08-18 project was not found")

        rows = connection.execute(
            """
            SELECT i.item_id, i.position, i.settings_json, i.subtitles_json,
                   a.asset_id, a.status AS asset_status, a.managed_path
            FROM project_items AS i
            JOIN project_assets AS a
              ON a.item_id=i.item_id AND a.asset_type='audio' AND a.version=1
            WHERE i.project_id=? AND i.position BETWEEN 1 AND 21
            ORDER BY i.position
            """,
            (PROJECT_ID,),
        ).fetchall()
        if tuple(row["position"] for row in rows) != RESTORE_POSITIONS:
            raise RuntimeError("The 21 expected project rows/audio v1 assets are incomplete")
        for row in rows:
            if row["asset_status"] != "READY":
                raise RuntimeError(f"Audio asset is not ready at position {row['position']}")
            if not Path(row["managed_path"]).is_file():
                raise RuntimeError(f"Audio file is missing at position {row['position']}")
            operation = connection.execute(
                """
                SELECT payload_json FROM project_operations
                WHERE project_id=? AND item_id=? AND operation_type='AUDIO_GENERATE'
                ORDER BY rowid DESC LIMIT 1
                """,
                (PROJECT_ID, row["item_id"]),
            ).fetchone()
            if operation is None:
                raise RuntimeError(f"Audio operation is missing at position {row['position']}")
            payload = decode_json(operation["payload_json"])
            if payload.get("voice_asset_id") != EXPECTED_OLD_VOICE_ID:
                raise RuntimeError(
                    f"Unexpected original voice at position {row['position']}: "
                    f"{payload.get('voice_asset_id')}"
                )

        backup_dir = DATABASE.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"control-pre-voice-scope-fix-{timestamp}.db"
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
        finally:
            backup.close()

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with connection:
            for row in rows:
                settings = decode_json(row["settings_json"])
                settings["voice_asset_id"] = EXPECTED_OLD_VOICE_ID
                subtitles = decode_json(row["subtitles_json"])
                subtitles.update(
                    {
                        "source": "minimax_timestamps",
                        "raw_cues": [],
                        "render_cues": [],
                        "bound_audio_asset_id": row["asset_id"],
                        "bound_video_asset_id": None,
                        "status": "PENDING_TIMESTAMPS",
                        "overflow_risk": False,
                    }
                )
                connection.execute(
                    """
                    UPDATE project_items
                    SET settings_json=?, subtitles_json=?, current_audio_asset_id=?,
                        current_base_video_asset_id=NULL, current_video_asset_id=NULL,
                        status='AUDIO_READY', updated_at=?
                    WHERE project_id=? AND item_id=?
                    """,
                    (
                        json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(subtitles, ensure_ascii=False, separators=(",", ":")),
                        row["asset_id"],
                        now,
                        PROJECT_ID,
                        row["item_id"],
                    ),
                )
            connection.execute(
                """
                UPDATE projects
                SET status='AUDIO_READY', revision=revision+1, updated_at=?
                WHERE project_id=?
                """,
                (now, PROJECT_ID),
            )

        restored = connection.execute(
            """
            SELECT COUNT(*) FROM project_items
            WHERE project_id=? AND position BETWEEN 1 AND 21
              AND status='AUDIO_READY' AND current_audio_asset_id IS NOT NULL
              AND json_extract(settings_json, '$.voice_asset_id')=?
              AND json_extract(subtitles_json, '$.status')='PENDING_TIMESTAMPS'
            """,
            (PROJECT_ID, EXPECTED_OLD_VOICE_ID),
        ).fetchone()[0]
        preserved = connection.execute(
            """
            SELECT COUNT(*) FROM project_items
            WHERE project_id=? AND position BETWEEN 22 AND 30
              AND status='AUDIO_READY' AND current_audio_asset_id IS NOT NULL
            """,
            (PROJECT_ID,),
        ).fetchone()[0]
        if restored != 21 or preserved != 9:
            raise RuntimeError(
                f"Post-repair verification failed: restored={restored}, preserved={preserved}"
            )
        print(
            json.dumps(
                {
                    "backup": str(backup_path),
                    "restored_chicken_soup": restored,
                    "preserved_dry_goods": preserved,
                },
                ensure_ascii=False,
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
