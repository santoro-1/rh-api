import json
import sqlite3
from pathlib import Path


DB_PATH = Path(r"D:\JYD\JianyingRenderServer-windows-x64\data\web_storage\control.db")


def main() -> None:
    connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    print("TABLES")
    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        print(row["name"])
    print("PROJECT_COLUMNS")
    for row in connection.execute("PRAGMA table_info(projects)"):
        print(dict(row))
    print("ITEM_COLUMNS")
    for row in connection.execute("PRAGMA table_info(project_items)"):
        print(dict(row))
    print("LATEST_PROJECTS")
    for row in connection.execute(
        "SELECT project_id, project_no, name, status, revision, created_at, updated_at "
        "FROM projects ORDER BY updated_at DESC LIMIT 15"
    ):
        print(dict(row))
    project_id = "fb690817a6a04995a907d51b8f2ed8a7"
    print("PROJECT_STATUS_SUMMARY")
    rows = connection.execute(
        "SELECT row_key, item_id, status, current_audio_asset_id, current_base_video_asset_id, "
        "current_video_asset_id, content_analysis_json, visual_analysis_json, updated_at "
        "FROM project_items WHERE project_id = ? ORDER BY position",
        (project_id,),
    ).fetchall()
    for row in rows:
        content = json.loads(row["content_analysis_json"] or "{}")
        visual = json.loads(row["visual_analysis_json"] or "{}")
        print(json.dumps({
            "row": row["row_key"], "item_id": row["item_id"], "status": row["status"],
            "audio": bool(row["current_audio_asset_id"]), "base_video": bool(row["current_base_video_asset_id"]),
            "final_video": bool(row["current_video_asset_id"]), "updated_at": row["updated_at"],
            "content_status": content.get("overall_status"),
            "music_status": content.get("music_analysis_status"),
            "subtitle_status": content.get("subtitle_analysis_status"),
            "title_status": content.get("title_analysis_status"),
            "content_errors": content.get("errors"),
            "visual_status": visual.get("analysis_status"),
            "mapping_status": visual.get("mapping_status"),
            "visual_error": visual.get("error"),
        }, ensure_ascii=False))
    print("OPERATION_COLUMNS")
    for row in connection.execute("PRAGMA table_info(project_operations)"):
        print(dict(row))
    print("RECENT_OPERATIONS")
    operations = connection.execute(
        "SELECT * FROM project_operations WHERE project_id = ? ORDER BY created_at DESC LIMIT 100",
        (project_id,),
    ).fetchall()
    for row in operations:
        payload = dict(row)
        for key, value in list(payload.items()):
            if key.endswith("_json") and value:
                try: payload[key] = json.loads(value)
                except json.JSONDecodeError: pass
        print(json.dumps(payload, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
