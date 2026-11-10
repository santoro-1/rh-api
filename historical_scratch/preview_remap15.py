from __future__ import annotations

import json
import sqlite3
import sys

from jyd_probe.semantic_visuals import map_visual_candidates_to_raw_cues


connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
row = connection.execute(
    """
    SELECT i.script_text, i.visual_analysis_json, i.subtitles_json
    FROM project_items AS i
    JOIN projects AS p ON p.project_id = i.project_id
    WHERE p.project_no = 'DH-20260808-0002' AND i.position = 15
    """
).fetchone()
visual = json.loads(row["visual_analysis_json"])
subtitles = json.loads(row["subtitles_json"])
plan = visual.get("visual_plan", [])
selected_anchors = {item["anchor_id"] for item in plan if int(item.get("priority") or 0) > 0}
candidates = [
    item
    for item in visual["candidate_request"]["candidates"]
    if ("START" if int(item["char_start"]) == 0 else f"B{int(item['char_start'])}")
    in selected_anchors
]
mapped = map_visual_candidates_to_raw_cues(
    row["script_text"],
    candidates,
    subtitles["raw_cues"],
    asr_alignment=subtitles.get("asr_alignment"),
)
print(
    json.dumps(
        [
            {
                "keyword": item["text"],
                "phrase": item["phrase_text"],
                "start_seconds": round(item["start_us"] / 1_000_000, 2),
                "end_seconds": round(
                    (item["start_us"] + item["duration_us"]) / 1_000_000, 2
                ),
                "timing_source": item["timing_source"],
            }
            for item in mapped
        ],
        ensure_ascii=False,
        indent=2,
    )
)
