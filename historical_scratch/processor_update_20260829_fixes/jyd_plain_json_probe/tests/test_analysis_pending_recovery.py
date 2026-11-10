from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from jyd_probe.project_store import ProjectStore


def test_restart_recovers_stale_content_and_visual_pending(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    store = ProjectStore(database)
    project = store.create_project(
        owner_user_id="user-1",
        owner_username="tester",
        name="分析中断恢复",
        items=[{"row_key": "1", "script_text": "每天吃一个鸡蛋"}],
    )
    item = project["items"][0]
    script_hash = hashlib.sha256(item["script_text"].encode("utf-8")).hexdigest()
    assert store.mark_item_content_analysis_pending(
        "user-1",
        project["project_id"],
        item["item_id"],
        expected_script_sha256=script_hash,
    )
    assert store.mark_item_visual_analysis_pending(
        "user-1",
        project["project_id"],
        item["item_id"],
        expected_script_sha256=script_hash,
        candidate_request={
            "catalog_version": "sha256:test",
            "candidates": [],
        },
    )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT content_analysis_json, visual_analysis_json
            FROM project_items WHERE item_id=?
            """,
            (item["item_id"],),
        ).fetchone()
        content = json.loads(row[0])
        visual = json.loads(row[1])
        content["requested_at"] = "2026-08-20T00:00:00+08:00"
        visual["requested_at"] = "2026-08-20T00:00:00+08:00"
        connection.execute(
            """
            UPDATE project_items
            SET content_analysis_json=?, visual_analysis_json=?
            WHERE item_id=?
            """,
            (
                json.dumps(content, ensure_ascii=False),
                json.dumps(visual, ensure_ascii=False),
                item["item_id"],
            ),
        )

    restarted = ProjectStore(database)
    recovered = restarted.get_project("user-1", project["project_id"])["items"][0]

    assert restarted.startup_recovered_analysis_count == 1
    assert recovered["content_analysis"]["overall_status"] == "FAILED"
    assert recovered["content_analysis"]["errors"]["request"]["code"] == (
        "ANALYSIS_INTERRUPTED"
    )
    assert recovered["visual_analysis"]["analysis_status"] == "FAILED"
    assert recovered["visual_analysis"]["error"]["code"] == "ANALYSIS_INTERRUPTED"
    assert recovered["allowed_actions"]["analyze_content"] is True
    assert recovered["allowed_actions"]["analyze_visuals"] is True
