from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
REVIEW = WORKSPACE / ".codex_tmp" / "semantic_full_review"
LIBRARY = WORKSPACE / ".codex_tmp" / "semantic_catalog_v3_expansion" / "library"
CANDIDATE = LIBRARY / "catalog.v3.candidate.json"
REPORT = WORKSPACE / ".codex_tmp" / "semantic_catalog_v3_expansion" / "full_validation_report.json"
WORKBENCH = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe")
sys.path.insert(0, str(WORKBENCH / "src"))

from jyd_probe.semantic_visuals import (  # noqa: E402
    _load_semantic_visual_catalog_v3,
    recall_semantic_visual_candidates,
)


def read_decisions() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for pattern in ("video_decisions_*.csv", "image_decisions_*.csv"):
        for path in sorted(REVIEW.glob(pattern)):
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows.extend(csv.DictReader(stream))
    return rows


payload = json.loads(CANDIDATE.read_text(encoding="utf-8"))
catalog = _load_semantic_visual_catalog_v3(LIBRARY, payload)
decisions = read_decisions()
selected = [row for row in decisions if row["decision"] != "REJECT"]

errors: list[str] = []
if len(decisions) != 2120 or len({row["review_id"] for row in decisions}) != 2120:
    errors.append("review coverage is not exactly 2120 unique IDs")
if len(selected) != 1169:
    errors.append(f"selected decision count mismatch: {len(selected)}")

review_assets = [asset for asset in payload["assets"] if asset["asset_id"].startswith("review.")]
if len(review_assets) != 1169:
    errors.append(f"review asset count mismatch: {len(review_assets)}")
if any(concept.startswith("candidate.") for asset in review_assets for concept in asset["concept_ids"]):
    errors.append("placeholder candidate concept remains")
if any(not asset["concept_ids"] for asset in review_assets):
    errors.append("selected asset without concept")
if any(asset["auto_eligible"] and set(asset["auto_trigger_concept_ids"]) != set(asset["concept_ids"]) for asset in review_assets):
    errors.append("auto trigger concepts diverge from reviewed concepts")

alias_owners: dict[str, set[str]] = defaultdict(set)
for concept in payload["concepts"]:
    for alias in concept["aliases"]:
        alias_owners[alias].add(concept["concept_id"])
duplicate_aliases = {alias: sorted(owners) for alias, owners in alias_owners.items() if len(owners) > 1}
if duplicate_aliases:
    errors.append(f"duplicate alias owners: {len(duplicate_aliases)}")

for asset in review_assets:
    resource = asset["resource"]
    paths = (
        [resource["video"], resource["preview"], resource["metadata"]]
        if asset["media_type"] == "video"
        else [str(Path(resource["bundle"]) / "sticker.json"), resource["preview"]]
    )
    for relative in paths:
        if not (LIBRARY / relative).is_file():
            errors.append(f"missing resource: {relative}")

scripts = {
    "food_list_quick_cut": "第一种鸡蛋，第二种玉米，第三种红薯。",
    "wrong_protein_regression": "补充蛋白质很重要，但不要把蛋白质直接等同于鱼。",
    "daily_light_activity": "吃完饭可以散步、逛超市，做点日常轻活动。",
    "new_foods": "今天吃西芹腰果虾仁、白灼虾和咸豆腐脑。",
    "activities": "周末可以骑自行车、打羽毛球或者去徒步。",
    "drinks": "早上喝豆浆，下午喝祁门红茶。",
}
recall = {}
for key, script in scripts.items():
    result = recall_semantic_visual_candidates(script, catalog)
    recall[key] = [
        {
            "phrase": item["text"],
            "concept_ids": [concept["concept_id"] for concept in item["allowed_concepts"]],
        }
        for item in result["candidates"]
    ]

protein = recall["wrong_protein_regression"]
if any("fish" in concept_id or "鱼" in str(item) for item in protein for concept_id in item["concept_ids"]):
    errors.append("protein regression recalled fish asset")
for required in ("activity.cycling", "activity.badminton", "activity.hiking"):
    if not any(required in item["concept_ids"] for item in recall["activities"]):
        errors.append(f"missing activity recall: {required}")
daily_activity_concepts = {
    concept_id
    for item in recall["daily_light_activity"]
    for concept_id in item["concept_ids"]
}
if not ({"activity.walking", "activity.light_daily"} <= daily_activity_concepts):
    errors.append("missing walking or daily light activity recall")

report = {
    "schema": "jyd.semantic-visual-full-review-validation.v1",
    "status": "passed" if not errors else "failed",
    "errors": errors,
    "review": {
        "decisions": len(decisions),
        "selected": len(selected),
        "decision_counts": dict(Counter(row["decision"] for row in decisions)),
    },
    "catalog": {
        "assets": len(payload["assets"]),
        "concepts": len(payload["concepts"]),
        "review_assets": len(review_assets),
        "review_videos": sum(asset["media_type"] == "video" for asset in review_assets),
        "review_images": sum(asset["media_type"] == "image" for asset in review_assets),
        "logical_clip_assets": sum(row["decision"] == "NEEDS_CLIP" for row in selected),
        "seam_broll_assets": sum("seam_broll" in asset["usage_modes"] for asset in review_assets),
        "duplicate_aliases": duplicate_aliases,
    },
    "recall": recall,
}
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if not errors else 1)
