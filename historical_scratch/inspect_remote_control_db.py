from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def decoded(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        print("integrity_check=", connection.execute("PRAGMA integrity_check").fetchone()[0])
        print("journal_mode=", connection.execute("PRAGMA journal_mode").fetchone()[0])
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        print("tables=", ",".join(sorted(tables)))

        projects = connection.execute(
            "SELECT project_id, owner_user_id, name, created_at, updated_at "
            "FROM projects ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
        print("\nRECENT_PROJECTS")
        for row in projects:
            print(json.dumps(dict(row), ensure_ascii=False, default=str))

        if not projects:
            return
        recent_ids = [row["project_id"] for row in projects[:5]]
        placeholders = ",".join("?" for _ in recent_ids)
        item_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(project_items)")
        }
        selected = [
            name
            for name in (
                "item_id",
                "project_id",
                "row_key",
                "status",
                "script_text",
                "content_analysis_json",
                "visual_analysis_json",
                "updated_at",
            )
            if name in item_columns
        ]
        items = connection.execute(
            f"SELECT {','.join(selected)} FROM project_items "
            f"WHERE project_id IN ({placeholders}) ORDER BY project_id,row_key",
            recent_ids,
        ).fetchall()
        print("\nRECENT_ITEMS")
        for row in items:
            value = dict(row)
            for key in ("content_analysis_json", "visual_analysis_json"):
                if key in value:
                    value[key] = decoded(value[key])
            print(json.dumps(value, ensure_ascii=False, default=str))

        if "project_operations" in tables:
            op_table = "project_operations"
        elif "operations" in tables:
            op_table = "operations"
        else:
            op_table = None
        if op_table:
            op_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({op_table})")
            }
            selected_ops = [
                name
                for name in (
                    "operation_id",
                    "project_id",
                    "item_id",
                    "operation_type",
                    "status",
                    "error_code",
                    "error_message",
                    "created_at",
                    "updated_at",
                    "result_json",
                    "payload_json",
                )
                if name in op_columns
            ]
            operations = connection.execute(
                f"SELECT {','.join(selected_ops)} FROM {op_table} "
                "ORDER BY updated_at DESC LIMIT 40"
            ).fetchall()
            print("\nRECENT_OPERATIONS")
            for row in operations:
                value = dict(row)
                for key in ("result_json", "payload_json"):
                    if key in value:
                        value[key] = decoded(value[key])
                print(json.dumps(value, ensure_ascii=False, default=str))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
