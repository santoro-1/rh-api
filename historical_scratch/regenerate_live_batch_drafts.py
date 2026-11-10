from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path


PROJECT_ID = "0f0f6e40a6284ee587897daef54fac7d"
REPOSITORY = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
RELEASE = Path(r"D:\JYD\JianyingRenderServer-windows-x64")
DATA = RELEASE / "data"
STORAGE = DATA / "web_storage"
DATABASE = STORAGE / "control.db"
DRAFT_ROOT = Path(r"D:\剪映草稿\JianyingPro Drafts")


def configure_environment() -> None:
    libraries = DATA / "libraries"
    values = {
        "JYD_WEB_STORAGE_ROOT": STORAGE,
        "JYD_DATABASE_PATH": DATABASE,
        "JYD_TEMPLATE_LIBRARY_ROOT": DATA / "template_library",
        "JYD_AUDIO_LIBRARY_ROOT": libraries / "audio_library",
        "JYD_EFFECT_LIBRARY_ROOT": libraries / "effect_library",
        "JYD_FONT_LIBRARY_ROOT": libraries / "font_library",
        "JYD_STICKER_LIBRARY_ROOT": libraries / "sticker_library",
        "JYD_CORNER_STICKER_LIBRARY_ROOT": libraries / "corner_sticker_library",
        "JYD_SEMANTIC_VISUAL_LIBRARY_ROOT": libraries / "semantic_visual_library",
        "JYD_TEXT_EFFECT_LIBRARY_ROOT": libraries / "text_effect_library",
        "JYD_TEXT_STYLE_LIBRARY_ROOT": libraries / "text_style_library",
        "JYD_TEXT_TEMPLATE_LIBRARY_ROOT": DATA / "l" / "t",
        "JYD_PERSONAL_LIBRARY_ROOT": DATA / "personal_libraries",
        "JYD_DECRYPT_WORK_ROOT": DATA / "decrypted",
        "JYD_DRAFTC_EXE": RELEASE / "tools" / "jy-draftc.exe",
        "JYD_WEB_DRAFT_ROOT": DRAFT_ROOT,
    }
    for name, value in values.items():
        os.environ[name] = str(Path(value).resolve())
    os.environ["JYD_EXECUTION_MODE"] = "embedded"
    os.environ["JYD_AUTH_SERVER_URL"] = "https://video.lanyingjk01.com"
    os.environ["JYD_AUTH_AUTHORITY"] = "false"
    os.environ["JYD_ASR_BASE_URL"] = "http://127.0.0.1:18084"
    os.environ["JYD_ASR_REQUIRED"] = "true"


def project_owner() -> str:
    with sqlite3.connect(DATABASE) as connection:
        row = connection.execute(
            "SELECT owner_user_id FROM projects WHERE project_id=?", (PROJECT_ID,)
        ).fetchone()
    if row is None:
        raise RuntimeError("目标批次不存在")
    return str(row[0])


def postprocess_settings(item: dict) -> dict:
    saved = dict(item.get("settings", {}).get("postprocess") or {})
    mode = str(saved.get("bgm_selection_mode") or "manual").strip().lower()
    return {
        "item_id": str(item["item_id"]),
        "font_identity": str(saved.get("font_identity") or ""),
        "bgm_identity": "" if mode == "auto" else str(saved.get("bgm_identity") or ""),
        "bgm_selection_mode": mode,
        "text_color": str(saved.get("text_color") or "#FFFFFF"),
        "layout_profile": str(saved.get("layout_profile") or "standing"),
    }


def coordinator_factory(app):
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", "")
        == "/api/new/projects/{project_id}/postprocess/generate"
        and "POST" in getattr(route, "methods", set())
    )
    closure = inspect.getclosurevars(route.endpoint)
    factory = closure.nonlocals.get("project_postprocess_coordinator")
    if factory is None:
        factory = closure.globals.get("project_postprocess_coordinator")
    if factory is None:
        raise RuntimeError("无法取得工作台草稿协调器")
    return factory


def latest_generate_operations(project: dict) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for operation in project.get("operations", []):
        if operation.get("operation_type") == "POSTPROCESS_GENERATE" and operation.get(
            "item_id"
        ):
            latest[str(operation["item_id"])] = operation
    return latest


def main() -> None:
    configure_environment()
    sys.path.insert(0, str(REPOSITORY / "src"))
    from jyd_probe.web_api import create_app, default_settings

    app = create_app(default_settings())
    store = app.state.project_store
    owner = project_owner()
    project = store.get_project(owner, PROJECT_ID)
    project_items = list(project.get("items") or [])
    requested_row_keys = {
        value.strip()
        for value in os.environ.get("JYD_CODEX_ROW_KEYS", "").split(",")
        if value.strip()
    }
    items = [
        item
        for item in project_items
        if not requested_row_keys or str(item.get("row_key") or "") in requested_row_keys
    ]
    expected_total = len(requested_row_keys) if requested_row_keys else 30
    if len(items) != expected_total:
        raise RuntimeError(
            f"目标任务应有 {expected_total} 条，实际匹配 {len(items)} 条"
        )
    active = [item for item in items if item.get("status") == "POSTPROCESS_RUNNING"]
    if active:
        raise RuntimeError(f"仍有 {len(active)} 条草稿任务正在运行，拒绝重复提交")
    if any(not item.get("outputs", {}).get("base_video") for item in items):
        raise RuntimeError("批次中存在缺少数字人基础视频的任务")

    requests = [postprocess_settings(item) for item in items]
    missing_fonts = [item["item_id"] for item in requests if not item["font_identity"]]
    if missing_fonts:
        raise RuntimeError(f"存在未配置字幕字体的任务: {missing_fonts}")

    print(json.dumps({"phase": "invalidate", "total": len(items)}, ensure_ascii=False), flush=True)
    for item, request in zip(items, requests):
        store.configure_item_postprocess(
            owner,
            PROJECT_ID,
            str(item["item_id"]),
            font_identity=request["font_identity"],
            bgm_identity=request["bgm_identity"],
            bgm_selection_mode=request["bgm_selection_mode"],
            text_color=request["text_color"],
            layout_profile=request["layout_profile"],
            force_invalidate=True,
            preserve_auto_bgm=request["bgm_selection_mode"] == "auto",
        )

    coordinator = coordinator_factory(app)()
    idempotency_key = f"codex-draft-refresh-{PROJECT_ID}-{int(time.time() * 1000)}"
    print(
        json.dumps(
            {"phase": "submit", "total": len(requests), "idempotency_key": idempotency_key},
            ensure_ascii=False,
        ),
        flush=True,
    )
    coordinator.start(
        owner,
        PROJECT_ID,
        idempotency_key=idempotency_key,
        item_settings=requests,
    )

    deadline = time.monotonic() + 3600
    last_summary = None
    while True:
        project = coordinator.sync(owner, PROJECT_ID)
        latest = latest_generate_operations(project)
        statuses = Counter(
            str(latest.get(str(item["item_id"]), {}).get("status") or "MISSING")
            for item in items
        )
        item_statuses = Counter(str(item.get("status") or "") for item in project["items"])
        summary = {
            "phase": "running",
            "operations": dict(statuses),
            "items": dict(item_statuses),
        }
        if summary != last_summary:
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            last_summary = summary
        if not any(status in {"PENDING", "RUNNING"} for status in statuses):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("等待 30 条剪映草稿生成超时")
        time.sleep(2)

    latest = latest_generate_operations(project)
    target_item_ids = {str(item["item_id"]) for item in items}
    current_items = [
        item
        for item in project["items"]
        if str(item["item_id"]) in target_item_ids
    ]
    failed = [
        {
            "row_key": item["row_key"],
            "item_id": item["item_id"],
            "status": latest.get(str(item["item_id"]), {}).get("status"),
            "error": latest.get(str(item["item_id"]), {}).get("error_message"),
        }
        for item in current_items
        if latest.get(str(item["item_id"]), {}).get("status") != "SUCCEEDED"
    ]
    ready = 0
    missing_paths = []
    for item in current_items:
        operation = latest.get(str(item["item_id"]), {})
        result = dict(operation.get("result") or {})
        path_text = str(
            result.get("output_draft_dir")
            or result.get("draft_path")
            or result.get("output_draft")
            or ""
        )
        if path_text and Path(path_text).exists():
            ready += 1
        else:
            missing_paths.append({"row_key": item["row_key"], "result": result})

    print(
        json.dumps(
            {
                "phase": "complete" if not failed and ready == len(items) else "failed",
                "succeeded": len(items) - len(failed),
                "failed": failed,
                "verified_draft_paths": ready,
                "missing_draft_paths": missing_paths,
                "export_operations_created": 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if failed or ready != len(items):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
