from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
RH_ROOT = WORKSPACE / "runninghub_mvp"
RH_DB = RH_ROOT / "data" / "app.db"
RH_DATA = RH_ROOT / "data"
JYD_ROOT = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
SOURCE_DB = JYD_ROOT / "data" / "web_storage" / "control.db"
SOURCE_STORAGE = SOURCE_DB.parent
TEST_DB = JYD_ROOT / "runtime" / "test_environment" / "web_storage" / "control.db"
TEST_STORAGE = TEST_DB.parent
DOWNLOADS = Path(r"C:\Users\san\Downloads")
OWNER_ID = "1"
OWNER_USERNAME = "admin"

PROJECT_SPECS = (
    {
        "source_name": "徐博士8.29导入",
        "test_name": "[本地隔离测试] 徐博士8.29导入",
        "download_dir": DOWNLOADS / "徐博士8.29导入-声音",
        "request_key": "isolated-test-0829-xu-v1",
    },
    {
        "source_name": "张雒8.29导入",
        "test_name": "[本地隔离测试] 张雒8.29导入",
        "download_dir": DOWNLOADS / "张雒8.29导入-声音",
        "request_key": "isolated-test-0829-zhang-v1",
    },
)


def json_value(raw: str | None, default):
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return deepcopy(default)
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_storage(root: Path, stored_path: str) -> Path:
    value = str(stored_path or "").strip()
    if not value.startswith("storage://"):
        raise RuntimeError(f"不支持的素材路径: {value}")
    resolved = (root / value.removeprefix("storage://")).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def backup_database(path: Path, label: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.parent / "manual_backups" / f"{path.stem}.before_{label}_{timestamp}{path.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def source_projects() -> list[dict]:
    connection = sqlite3.connect(SOURCE_DB)
    connection.row_factory = sqlite3.Row
    projects: list[dict] = []
    try:
        for spec in PROJECT_SPECS:
            project_row = connection.execute(
                "SELECT * FROM projects WHERE name=? AND owner_user_id=?",
                (spec["source_name"], OWNER_ID),
            ).fetchone()
            if project_row is None:
                raise RuntimeError(f"找不到原项目: {spec['source_name']}")
            item_rows = connection.execute(
                "SELECT * FROM project_items WHERE project_id=? ORDER BY position",
                (project_row["project_id"],),
            ).fetchall()
            if len(item_rows) != 5:
                raise RuntimeError(f"{spec['source_name']} 应为 5 条，实际 {len(item_rows)} 条")
            input_rows = connection.execute(
                "SELECT * FROM project_input_images WHERE project_id=? ORDER BY position",
                (project_row["project_id"],),
            ).fetchall()
            assets = connection.execute(
                "SELECT * FROM project_assets WHERE project_id=? ORDER BY item_id, asset_type, version",
                (project_row["project_id"],),
            ).fetchall()
            assets_by_id = {row["asset_id"]: dict(row) for row in assets}
            items: list[dict] = []
            for item_row in item_rows:
                item = dict(item_row)
                audio = assets_by_id.get(str(item["current_audio_asset_id"] or ""))
                if audio is None:
                    raise RuntimeError(f"{spec['source_name']} 第 {item['position']} 条缺少当前声音")
                downloaded = spec["download_dir"] / str(audio["filename"])
                source_audio = resolve_storage(SOURCE_STORAGE, str(audio["managed_path"]))
                if not downloaded.is_file():
                    raise RuntimeError(f"下载声音不存在: {downloaded}")
                if sha256(downloaded) != sha256(source_audio):
                    raise RuntimeError(f"下载声音与原项目不一致: {downloaded.name}")
                item["audio_asset"] = audio
                item["downloaded_audio"] = downloaded
                item_settings = json_value(item["settings_json"], {})
                reference_id = str(item_settings.get("h3", {}).get("reference_video_asset_id") or "")
                item["reference_asset"] = assets_by_id.get(reference_id)
                image_id = str(item["current_image_asset_id"] or "")
                item["image_asset"] = assets_by_id.get(image_id)
                items.append(item)
            projects.append(
                {
                    "spec": spec,
                    "project": dict(project_row),
                    "items": items,
                    "input_images": [dict(row) for row in input_rows],
                }
            )
    finally:
        connection.close()
    return projects


def seed_runninghub(projects: list[dict]) -> dict[str, dict]:
    connection = sqlite3.connect(RH_DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    seeded: dict[str, dict] = {}
    try:
        user = connection.execute("SELECT id FROM users WHERE id=1").fetchone()
        if user is None:
            raise RuntimeError("本机 RunningHub 缺少 admin 用户")
        template = connection.execute(
            """
            SELECT t.* FROM audio_generation_tasks t
            JOIN minimax_voice_assets v ON v.id=t.voice_asset_id
            JOIN minimax_configs c ON c.id=t.config_id
            WHERE t.user_id=1 AND v.user_id=1 AND c.user_id=1
            ORDER BY t.created_at DESC LIMIT 1
            """
        ).fetchone()
        if template is None:
            raise RuntimeError("本机 RunningHub 缺少可复用的 MiniMax 账号/音色配置")
        now = datetime.now(timezone.utc).isoformat(sep=" ")
        with connection:
            for project in projects:
                spec = project["spec"]
                existing = connection.execute(
                    "SELECT * FROM generation_batches WHERE user_id=1 AND request_key=?",
                    (spec["request_key"],),
                ).fetchone()
                if existing is not None:
                    items = connection.execute(
                        "SELECT * FROM generation_batch_items WHERE batch_id=? ORDER BY row_number",
                        (existing["id"],),
                    ).fetchall()
                    if len(items) != len(project["items"]):
                        raise RuntimeError(f"已有测试批次不完整: {spec['test_name']}")
                    seeded[spec["source_name"]] = {
                        "batch_id": existing["id"],
                        "items": {int(row["row_number"]): row["id"] for row in items},
                    }
                    continue

                batch_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO generation_batches(
                        id,user_id,name,workflow_type,audio_mode,request_key,status,total_items,
                        created_at,updated_at,review_required,video_review_required,source_channel,
                        correlation_id,runninghub_execution_account_ids_json,
                        seedvr2_execution_account_ids_json,execution_mode
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        batch_id, 1, spec["test_name"] + " 声音批次", "digital_human", "minimax",
                        spec["request_key"], "ACTIVE", len(project["items"]), now, now, 1, 0,
                        "new_workbench", uuid.uuid4().hex, None, None, None,
                    ),
                )
                row_map: dict[int, str] = {}
                for item in project["items"]:
                    position = int(item["position"])
                    item_id = str(uuid.uuid4())
                    task_id = str(uuid.uuid4())
                    planned_id = str(uuid.uuid4())
                    attempt_id = str(uuid.uuid4())
                    metadata = json_value(item["audio_asset"]["metadata_json"], {})
                    speed = float(metadata.get("speed") or 1.0)
                    output_rel = f"uploads/1/{planned_id}/generated-audio-v001.mp3"
                    subtitle_rel = f"uploads/1/{planned_id}/generated-subtitles-v001.json"
                    output_path = RH_DATA / Path(output_rel)
                    subtitle_path = RH_DATA / Path(subtitle_rel)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item["downloaded_audio"], output_path)
                    cues = metadata.get("subtitle_cues") if isinstance(metadata.get("subtitle_cues"), list) else []
                    subtitle_payload = [
                        {
                            "text": str(cue.get("text") or ""),
                            "start_seconds": float(cue.get("start_us") or 0) / 1_000_000,
                            "end_seconds": float(cue.get("end_us") or 0) / 1_000_000,
                        }
                        for cue in cues
                    ]
                    subtitle_path.write_text(
                        json.dumps(subtitle_payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    script = str(item["script_text"])
                    manifest = {
                        "row_id": str(item["row_key"]),
                        "speech_script": script,
                        "prompt": "人物自然说话，固定镜头",
                        "local_test_import": spec["source_name"],
                    }
                    connection.execute(
                        """
                        INSERT INTO generation_batch_items(
                            id,batch_id,row_number,row_key,manifest_json,audio_status,status,
                            error_code,error_message,created_at,updated_at,merged_video_status,
                            merged_video_path,merged_video_error,merged_at,merged_reviewed_at,
                            runninghub_execution_account_ids_json,seedvr2_execution_account_ids_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            item_id,batch_id,position,str(item["row_key"]),
                            json.dumps(manifest,ensure_ascii=False),"AWAITING_REVIEW",
                            "AWAITING_AUDIO_REVIEW",None,None,now,now,"NOT_APPLICABLE",
                            None,None,None,None,None,None,
                        ),
                    )
                    provider_id = f"local-import-0829-{uuid.uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO audio_generation_tasks(
                            id,user_id,config_id,batch_item_id,voice_a_id,voice_b_id,
                            planned_generation_task_id,credential_fingerprint,primary_kind,
                            primary_path,primary_original_name,speech_script,video_parameters_json,
                            model,weight_a,weight_b,speed,volume,pitch,language_boost,output_format,
                            status,attempt_count,error_code,error_message,output_path,cost_confirmed_at,
                            created_at,updated_at,completed_at,voice_asset_id,account_binding_id,
                            alignment_method,subtitle_path,provider_task_id,provider_file_id,
                            provider_submitted_at,pronunciation_dict_json,generation_version,
                            reviewed_at,primary_sha256
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            task_id,1,template["config_id"],item_id,template["voice_asset_id"],
                            template["voice_asset_id"],planned_id,template["credential_fingerprint"],
                            None,None,None,script,template["video_parameters_json"],template["model"],
                            template["weight_a"],template["weight_b"],speed,template["volume"],
                            template["pitch"],template["language_boost"],"mp3","AWAITING_REVIEW",1,
                            None,None,output_rel,now,now,now,now,template["voice_asset_id"],
                            template["account_binding_id"],"minimax_raw_cues",subtitle_rel,
                            provider_id,provider_id,now,"[]",1,None,sha256(output_path),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO audio_generation_attempts(
                            id,audio_task_id,version,provider_task_id,provider_file_id,
                            output_path,subtitle_path,status,created_at,completed_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            attempt_id,task_id,1,provider_id,provider_id,output_rel,subtitle_rel,
                            "READY",now,now,
                        ),
                    )
                    row_map[position] = item_id
                seeded[spec["source_name"]] = {"batch_id": batch_id, "items": row_map}
    finally:
        connection.close()
    return seeded


def sanitized_project_settings(source: dict, source_name: str) -> dict:
    settings = deepcopy(source)
    settings.pop("default_voice_asset_id", None)
    mapping = settings.get("image_mapping")
    if isinstance(mapping, dict):
        mapping["image_ids"] = []
    h3 = settings.get("h3")
    if isinstance(h3, dict):
        h3["identity_image_ids"] = []
        h3["remote_batch_id"] = None
        h3["remote_status"] = None
        h3["fee_snapshot"] = None
        h3["prepare_key"] = None
    settings["local_test_import"] = {
        "source_project": source_name,
        "source_date": "2026-08-29",
        "environment": "isolated_test",
    }
    return settings


def sanitized_item_settings(source: dict) -> dict:
    settings = deepcopy(source)
    settings.pop("voice_asset_id", None)
    h3 = settings.get("h3")
    if isinstance(h3, dict):
        h3["reference_video_asset_id"] = None
        h3["remote_item_id"] = None
        h3["remote_status"] = None
        h3["invalidated_reason"] = "LOCAL_TEST_IMPORT"
    return settings


def clone_test_projects(projects: list[dict], seeded: dict[str, dict]) -> dict[str, str]:
    sys.path.insert(0, str(JYD_ROOT / "src"))
    from jyd_probe.project_store import ProjectStore

    store = ProjectStore(TEST_DB)
    result: dict[str, str] = {}
    source_connection = sqlite3.connect(SOURCE_DB)
    source_connection.row_factory = sqlite3.Row
    try:
        for project in projects:
            spec = project["spec"]
            existing = sqlite3.connect(TEST_DB)
            try:
                existing.row_factory = sqlite3.Row
                found = existing.execute(
                    "SELECT project_id FROM projects WHERE owner_user_id=? AND name=?",
                    (OWNER_ID, spec["test_name"]),
                ).fetchone()
            finally:
                existing.close()
            if found is not None:
                result[spec["source_name"]] = found["project_id"]
                continue

            source_settings = json_value(project["project"]["settings_json"], {})
            created = store.create_project(
                owner_user_id=OWNER_ID,
                owner_username=OWNER_USERNAME,
                name=spec["test_name"],
                items=[
                    {
                        "row_key": item["row_key"],
                        "script_text": item["script_text"],
                        "settings": sanitized_item_settings(json_value(item["settings_json"], {})),
                    }
                    for item in project["items"]
                ],
                settings=sanitized_project_settings(source_settings, spec["source_name"]),
            )
            project_id = created["project_id"]
            test_connection = sqlite3.connect(TEST_DB)
            test_connection.row_factory = sqlite3.Row
            try:
                new_items = test_connection.execute(
                    "SELECT * FROM project_items WHERE project_id=? ORDER BY position", (project_id,)
                ).fetchall()
            finally:
                test_connection.close()
            new_by_position = {int(row["position"]): dict(row) for row in new_items}

            input_id_map: dict[str, str] = {}
            input_path_map: dict[str, Path] = {}
            for input_row in project["input_images"]:
                source_path = resolve_storage(SOURCE_STORAGE, str(input_row["managed_path"]))
                target_path = (
                    TEST_STORAGE / "new_projects" / project_id / "input_images"
                    / f"{uuid.uuid4().hex}_{input_row['filename']}"
                )
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                registered = store.register_input_image(
                    owner_user_id=OWNER_ID,
                    project_id=project_id,
                    filename=str(input_row["filename"]),
                    content_type=str(input_row["content_type"]),
                    size_bytes=target_path.stat().st_size,
                    sha256=sha256(target_path),
                    managed_path=str(target_path),
                )
                input_id_map[str(input_row["image_id"])] = registered["image_id"]
                input_path_map[registered["image_id"]] = target_path

            asset_id_map: dict[str, str] = {}
            for source_item in project["items"]:
                position = int(source_item["position"])
                new_item = new_by_position[position]
                new_item_id = new_item["item_id"]

                image_asset = source_item["image_asset"]
                if image_asset is not None:
                    external = json_value(image_asset["external_ref_json"], {})
                    new_input_id = input_id_map[str(external["input_image_id"])]
                    added_image = store.add_asset(
                        owner_user_id=OWNER_ID,
                        project_id=project_id,
                        item_id=new_item_id,
                        asset_type="input_image",
                        source_type="project_upload",
                        status="READY",
                        filename=str(image_asset["filename"]),
                        managed_path=str(input_path_map[new_input_id]),
                        external_ref={"input_image_id": new_input_id},
                        metadata=json_value(image_asset["metadata_json"], {}),
                        make_current=False,
                    )
                    asset_id_map[str(image_asset["asset_id"])] = added_image["asset_id"]

                reference_asset = source_item["reference_asset"]
                if reference_asset is not None:
                    source_path = resolve_storage(SOURCE_STORAGE, str(reference_asset["managed_path"]))
                    target_path = (
                        TEST_STORAGE / "new_projects" / project_id / new_item_id / "h3_reference"
                        / f"{uuid.uuid4().hex}_{reference_asset['filename']}"
                    )
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target_path)
                    store.add_h3_reference_video(
                        owner_user_id=OWNER_ID,
                        project_id=project_id,
                        item_id=new_item_id,
                        filename=str(reference_asset["filename"]),
                        managed_path=str(target_path),
                        metadata=json_value(reference_asset["metadata_json"], {}),
                    )
                    with sqlite3.connect(TEST_DB) as reference_connection:
                        reference_row = reference_connection.execute(
                            "SELECT settings_json FROM project_items WHERE item_id=?",
                            (new_item_id,),
                        ).fetchone()
                    reference_settings = json_value(reference_row[0], {})
                    new_reference_id = str(
                        reference_settings.get("h3", {}).get("reference_video_asset_id") or ""
                    )
                    if not new_reference_id:
                        raise RuntimeError(f"第 {position} 条 H3 参考视频编号写入失败")
                    asset_id_map[str(reference_asset["asset_id"])] = new_reference_id

                audio_asset = source_item["audio_asset"]
                audio_target = (
                    TEST_STORAGE / "projects" / OWNER_ID / project_id / new_item_id / "audio" / "v1.mp3"
                )
                audio_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_item["downloaded_audio"], audio_target)
                audio_metadata = json_value(audio_asset["metadata_json"], {})
                audio_metadata["provider_status"] = "AWAITING_REVIEW"
                audio_metadata["local_test_import"] = spec["source_name"]
                remote = seeded[spec["source_name"]]
                added_audio = store.add_asset(
                    owner_user_id=OWNER_ID,
                    project_id=project_id,
                    item_id=new_item_id,
                    asset_type="audio",
                    source_type="minimax",
                    status="READY",
                    filename=str(audio_asset["filename"]),
                    managed_path=str(audio_target),
                    external_ref={
                        "batch_id": remote["batch_id"],
                        "remote_item_id": remote["items"][position],
                        "generation_version": 1,
                    },
                    metadata=audio_metadata,
                    make_current=True,
                )
                asset_id_map[str(audio_asset["asset_id"])] = added_audio["asset_id"]
                subtitles = json_value(source_item["subtitles_json"], {})
                subtitles["bound_audio_asset_id"] = added_audio["asset_id"]
                subtitles["bound_video_asset_id"] = None
                subtitles["status"] = "READY"
                store.set_item_subtitles(OWNER_ID, project_id, new_item_id, subtitles)

            final_project_settings = sanitized_project_settings(source_settings, spec["source_name"])
            mapping = final_project_settings.get("image_mapping")
            source_mapping = source_settings.get("image_mapping")
            if isinstance(mapping, dict) and isinstance(source_mapping, dict):
                mapping["image_ids"] = [
                    input_id_map[value]
                    for value in source_mapping.get("image_ids", [])
                    if value in input_id_map
                ]
            h3 = final_project_settings.get("h3")
            source_h3 = source_settings.get("h3")
            if isinstance(h3, dict) and isinstance(source_h3, dict):
                h3["identity_image_ids"] = [
                    input_id_map[value]
                    for value in source_h3.get("identity_image_ids", [])
                    if value in input_id_map
                ]

            with sqlite3.connect(TEST_DB) as connection:
                connection.execute(
                    "UPDATE projects SET settings_json=?, updated_at=? WHERE project_id=?",
                    (
                        json.dumps(final_project_settings, ensure_ascii=False, separators=(",", ":")),
                        datetime.now().astimezone().isoformat(timespec="seconds"), project_id,
                    ),
                )
                for source_item in project["items"]:
                    position = int(source_item["position"])
                    new_item_id = new_by_position[position]["item_id"]
                    settings = sanitized_item_settings(json_value(source_item["settings_json"], {}))
                    source_reference = source_item["reference_asset"]
                    if source_reference is not None:
                        settings.setdefault("h3", {})["reference_video_asset_id"] = asset_id_map[
                            str(source_reference["asset_id"])
                        ]
                        settings["h3"]["reference_video_version"] = 1
                    postprocess = settings.get("postprocess")
                    if isinstance(postprocess, dict):
                        music = postprocess.get("music_selection")
                        if isinstance(music, dict):
                            music["audio_asset_id"] = asset_id_map[str(source_item["audio_asset"]["asset_id"])]
                    visual = json_value(source_item["visual_analysis_json"], {})
                    if visual:
                        visual["bound_audio_asset_id"] = asset_id_map[
                            str(source_item["audio_asset"]["asset_id"])
                        ]
                    connection.execute(
                        """
                        UPDATE project_items
                        SET current_image_asset_id=?, settings_json=?, content_analysis_json=?,
                            visual_analysis_json=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (
                            (
                                asset_id_map[str(source_item["image_asset"]["asset_id"])]
                                if source_item["image_asset"] is not None
                                else None
                            ),
                            json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
                            source_item["content_analysis_json"],
                            json.dumps(visual, ensure_ascii=False, separators=(",", ":")),
                            datetime.now().astimezone().isoformat(timespec="seconds"), new_item_id,
                        ),
                    )
            result[spec["source_name"]] = project_id
    finally:
        source_connection.close()
    return result


def verify(project_ids: dict[str, str], seeded: dict[str, dict]) -> None:
    rh = sqlite3.connect(RH_DB)
    jyd = sqlite3.connect(TEST_DB)
    try:
        for spec in PROJECT_SPECS:
            source_name = spec["source_name"]
            batch_id = seeded[source_name]["batch_id"]
            task_count = rh.execute(
                """
                SELECT count(*) FROM audio_generation_tasks t
                JOIN generation_batch_items i ON i.id=t.batch_item_id
                WHERE i.batch_id=? AND t.status='AWAITING_REVIEW'
                  AND t.output_path IS NOT NULL AND t.subtitle_path IS NOT NULL
                """,
                (batch_id,),
            ).fetchone()[0]
            project_id = project_ids[source_name]
            item_count = jyd.execute(
                "SELECT count(*) FROM project_items WHERE project_id=? AND status='AUDIO_READY'",
                (project_id,),
            ).fetchone()[0]
            asset_count = jyd.execute(
                "SELECT count(*) FROM project_assets WHERE project_id=? AND asset_type='audio' AND status='READY'",
                (project_id,),
            ).fetchone()[0]
            if (task_count, item_count, asset_count) != (5, 5, 5):
                raise RuntimeError(
                    f"验证失败 {source_name}: tasks={task_count}, items={item_count}, assets={asset_count}"
                )
            print(
                json.dumps(
                    {
                        "source": source_name,
                        "test_project_id": project_id,
                        "local_audio_batch_id": batch_id,
                        "rows": 5,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        rh.close()
        jyd.close()


def main() -> None:
    if not RH_DB.is_file() or not SOURCE_DB.is_file() or not TEST_DB.is_file():
        raise RuntimeError("本地数据库不完整，停止导入")
    projects = source_projects()
    rh_backup = backup_database(RH_DB, "0829_isolated_import")
    jyd_backup = backup_database(TEST_DB, "0829_isolated_import")
    print(f"RH_BACKUP={rh_backup}")
    print(f"JYD_BACKUP={jyd_backup}")
    seeded = seed_runninghub(projects)
    project_ids = clone_test_projects(projects, seeded)
    verify(project_ids, seeded)


if __name__ == "__main__":
    main()
