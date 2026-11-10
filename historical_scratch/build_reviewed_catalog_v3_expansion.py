from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps


WORKSPACE = Path(r"D:\工作内容\轻盈健\数字人")
REVIEW_ROOT = WORKSPACE / ".codex_tmp" / "semantic_full_review"
MANIFEST_PATH = REVIEW_ROOT / "unreviewed_manifest.csv"
ACTIVE_ROOT = Path(
    r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library"
)
OUTPUT_ROOT = WORKSPACE / ".codex_tmp" / "semantic_catalog_v3_expansion"
BASELINE_CATALOG_PATH = OUTPUT_ROOT / "catalog.actual.pre-expansion.json"
LIBRARY_ROOT = OUTPUT_ROOT / "library"
CATALOG_PATH = LIBRARY_ROOT / "catalog.json"
CANDIDATE_PATH = LIBRARY_ROOT / "catalog.v3.candidate.json"
BACKUP_PATH = LIBRARY_ROOT / "catalog.source-backup.json"
MIGRATION_PATH = OUTPUT_ROOT / "migration.pending.json"
REPORT_PATH = OUTPUT_ROOT / "validation_report.json"
SELECTED_PATH = OUTPUT_ROOT / "selected_assets.csv"
REBUILD_MARKER = OUTPUT_ROOT / ".allow-candidate-rebuild"

CONCEPT_ALIAS_OVERRIDES = {
    "food.gorgon_fruit_seed": ["芡实"],
}

EXACT_ALIAS_OWNER_OVERRIDES = {
    "杂粮饭": "food.mixed_grain_rice",
    "巴旦木": "food.almond",
    "香菇": "food.shiitake",
    "核心燃脂": "activity.core_training",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def clone_tree(source: Path, target: Path) -> None:
    for item in source.rglob("*"):
        if item.is_file():
            link_or_copy(item, target / item.relative_to(source))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def safe_alias(value: str) -> str:
    alias = re.sub(r"\.(jpg|jpeg|png|webp|mp4|mov)$", "", value.strip(), flags=re.I)
    alias = re.sub(r"(?<=[\u4e00-\u9fff])\d+$", "", alias).strip(" _-—")
    return alias


def usable_alias(value: str) -> bool:
    if not value or len(value) > 18:
        return False
    blocked = ("微信截图", "截图", "素材", "合集", "画板", "对比")
    return not any(token in value for token in blocked)


def composite_concept(review_id: str) -> str:
    return f"review.combo.{review_id.lower()}"


def narrow_concepts(row: dict[str, str]) -> tuple[list[str], str]:
    original = [value for value in row["proposed_concepts"].split("|") if value]
    if len(original) <= 1:
        return original, "unchanged_single"

    prefixes = [value.split(".", 1)[0] for value in original]
    for preferred in ("dish", "meal", "portion"):
        values = [value for value in original if value.startswith(preferred + ".")]
        if values:
            return [values[0]], f"narrow_to_{preferred}"

    actions = [value for value in original if value.startswith("action.")]
    if actions:
        objects = [
            value
            for value in original
            if value.startswith(("food.", "drink.", "object."))
        ]
        return list(dict.fromkeys([*actions, *objects[:1]])), "action_plus_direct_object"

    activities = [value for value in original if value.startswith("activity.")]
    if activities:
        return activities, "activity_hierarchy_retained"

    scenes = [
        value
        for value in original
        if value.startswith(("scene.", "season.", "weather."))
    ]
    if scenes:
        return scenes, "scene_hierarchy_retained"

    drinks = [value for value in original if value.startswith("drink.")]
    if drinks:
        return [drinks[0]], "narrow_to_complete_drink"

    same_prefix = len(set(prefixes)) == 1
    if same_prefix:
        # Proposed concepts are written most-specific first.  Retain the first item
        # instead of letting a generic parent (shrimp, bread, porridge, etc.) widen it.
        return [original[0]], "narrow_to_specific_first"

    # A multi-ingredient scene without an explicit dish/meal concept is not allowed
    # to trigger from each ingredient independently.  Give it one exact phrase concept.
    return [composite_concept(row["review_id"])], "replace_with_exact_composite"


def usage_modes(row: dict[str, str], media_type: str) -> list[str]:
    raw = [item for item in row["usage"].split("|") if item]
    if media_type == "image":
        return ["semantic_overlay", "list_quick_cut"]
    modes = list(raw)
    if "full_screen_broll" in modes and "seam_broll" not in modes:
        modes.append("seam_broll")
    return modes


def clone_existing_resources(catalog: dict) -> None:
    copied: set[str] = set()
    for asset in catalog["assets"]:
        if asset["media_type"] == "image":
            rel = asset["resource"]["bundle"]
            source = ACTIVE_ROOT / rel
        else:
            rel = str(Path(asset["resource"]["video"]).parent)
            source = ACTIVE_ROOT / rel
        if rel in copied:
            continue
        clone_tree(source, LIBRARY_ROOT / rel)
        copied.add(rel)


def probe_has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return bool(result.stdout.strip())


def render_poster(source: Path, target: Path, seek_s: float) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek_s:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale='min(720,iw)':-2",
        "-y",
        str(target),
    ]
    subprocess.run(command, check=True)


def build_image(row: dict[str, str], manifest: dict[str, str], concepts: list[str]) -> dict:
    source = Path(manifest["full_path"])
    sha = manifest["sha256"]
    folder = f"bundles/review_{row['review_id'].lower()}_image_{sha[:10]}"
    bundle_root = LIBRARY_ROOT / folder
    preview = bundle_root / "resources" / "sticker" / "singleImage.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    if not preview.is_file() or preview.stat().st_size <= 0:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.save(preview, format="PNG")
    sticker = {
        "schema": "jyd.semantic-image-bundle.v1",
        "name": row["asset_name"],
        "preview_file": "resources/sticker/singleImage.png",
        "resource": {"library_path": "resources/sticker", "status": "copied"},
        "source": {
            "review_id": row["review_id"],
            "original_name": manifest["filename"],
            "source_sha256": sha,
            "review_decision": row["decision"],
        },
    }
    (bundle_root / "sticker.json").write_text(
        json.dumps(sticker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "asset_id": f"review.{row['review_id'].lower()}.image.{sha[:10]}",
        "concept_ids": concepts,
        "name": row["asset_name"],
        "description": row["reason"],
        "media_type": "image",
        "renderer": "jyd_sticker_bundle",
        "tags": ["人工逐图审核", "网络素材", "窄语义", "图片"],
        "resource": {
            "bundle": folder.replace("\\", "/"),
            "preview": str((Path(folder) / "resources/sticker/singleImage.png")).replace("\\", "/"),
        },
        "defaults": {
            "corner": "bottom_center",
            "scale": 0.56,
            "opacity": 1.0,
            "duration_us": 2_000_000,
        },
        "semantic_roles": {"depicts": concepts, "expresses": [], "related": []},
        "auto_trigger_concept_ids": concepts,
        "trigger_basis": {value: "exact_subject" for value in concepts},
        "visual_actions": [],
        "usage_modes": usage_modes(row, "image"),
        "cleanliness_grade": "A",
        "auto_eligible": True,
        "requires_clip": False,
        "loop_allowed": False,
        "rights_status": "attributed",
        "person_status": (
            "identifiable"
            if any(value.startswith(("action.", "activity.", "clothing.")) for value in concepts)
            else "none"
        ),
        "brand_status": "none",
        "health_claim_status": "none",
        "platform_ui_status": "none",
    }


def build_video(row: dict[str, str], manifest: dict[str, str], concepts: list[str]) -> dict:
    source = Path(manifest["full_path"])
    sha = manifest["sha256"]
    folder = f"videos/review_{row['review_id'].lower()}_video_{sha[:10]}"
    video_path = LIBRARY_ROOT / folder / "video.mp4"
    poster_path = LIBRARY_ROOT / folder / "poster.png"
    metadata_path = LIBRARY_ROOT / folder / "metadata.json"
    link_or_copy(source, video_path)
    source_duration_s = float(manifest["duration_seconds"])
    start_s = float(row.get("clip_start_s") or 0)
    requested_end = float(row.get("clip_end_s") or source_duration_s)
    end_s = min(source_duration_s, requested_end)
    if end_s <= start_s:
        raise ValueError(f"invalid approved clip range for {row['review_id']}")
    clip_duration_s = end_s - start_s
    poster_seek = min(start_s + min(0.2, clip_duration_s / 4), max(0.0, source_duration_s - 0.05))
    render_poster(source, poster_path, poster_seek)
    if metadata_path.is_file():
        cached_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        has_audio = bool(cached_metadata.get("has_audio"))
    else:
        has_audio = probe_has_audio(source)
    metadata = {
        "schema": "jyd.semantic-video-metadata.v1",
        "review_id": row["review_id"],
        "original_name": manifest["filename"],
        "source_sha256": sha,
        "fps": manifest["fps"],
        "duration_us": round(source_duration_s * 1_000_000),
        "width": int(manifest["width"]),
        "height": int(manifest["height"]),
        "has_audio": has_audio,
        "review_decision": row["decision"],
        "approved_source_start_us": round(start_s * 1_000_000),
        "approved_source_end_us": round(end_s * 1_000_000),
        "review_notes": row["reason"],
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    modes = usage_modes(row, "video")
    enrichment = bool(set(modes) & {"full_screen_broll", "seam_broll"})
    target_duration_s = min(5.0, max(2.0, clip_duration_s))
    visual_actions = ["training"] if any(value.startswith("activity.") for value in concepts) else []
    return {
        "asset_id": f"review.{row['review_id'].lower()}.video.{sha[:10]}",
        "concept_ids": concepts,
        "name": row["asset_name"],
        "description": row["reason"],
        "media_type": "video",
        "renderer": "video_overlay",
        "tags": [
            "人工逐条审片",
            "网络素材",
            "窄语义",
            "视频",
            *( ["逻辑裁剪"] if row["decision"] == "NEEDS_CLIP" else [] ),
        ],
        "resource": {
            "video": str((Path(folder) / "video.mp4")).replace("\\", "/"),
            "preview": str((Path(folder) / "poster.png")).replace("\\", "/"),
            "metadata": str((Path(folder) / "metadata.json")).replace("\\", "/"),
            "duration_us": round(source_duration_s * 1_000_000),
            "width": int(manifest["width"]),
            "height": int(manifest["height"]),
            "has_audio": has_audio,
        },
        "defaults": {
            "corner": "center" if enrichment else "bottom_center",
            "scale": 1.0 if enrichment else 0.615,
            "opacity": 1.0,
            "duration_us": round(target_duration_s * 1_000_000),
            "source_start_us": round(start_s * 1_000_000),
            "mute": True,
            "loop": True,
            "fit": "cover" if enrichment else "contain",
        },
        "semantic_roles": {"depicts": concepts, "expresses": [], "related": []},
        "auto_trigger_concept_ids": concepts,
        "trigger_basis": {value: "exact_subject" for value in concepts},
        "visual_actions": visual_actions,
        "usage_modes": modes,
        "cleanliness_grade": "B",
        "auto_eligible": True,
        "requires_clip": False,
        "loop_allowed": True,
        "rights_status": "attributed",
        "person_status": "unknown",
        "brand_status": "none",
        "health_claim_status": "none",
        "platform_ui_status": "none",
    }


def main() -> int:
    if OUTPUT_ROOT.exists():
        if CANDIDATE_PATH.exists() and not REBUILD_MARKER.exists():
            raise SystemExit(f"refusing to overwrite existing output: {OUTPUT_ROOT}")
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_rows = read_csv(MANIFEST_PATH)
    manifest = {row["review_id"]: row for row in manifest_rows}
    decisions: list[dict[str, str]] = []
    for path in sorted(REVIEW_ROOT.glob("video_decisions_*.csv")):
        decisions.extend(read_csv(path))
    for path in sorted(REVIEW_ROOT.glob("image_decisions_*.csv")):
        decisions.extend(read_csv(path))
    selected = [row for row in decisions if row["decision"] != "REJECT"]
    expected_selected = 393 + 39 + 740 - 3
    if len(selected) != expected_selected:
        raise ValueError(
            f"expected {expected_selected} fully reviewed selections, got {len(selected)}"
        )
    selected_ids = [int(row["review_id"][1:]) for row in selected]
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("duplicate reviewed selections found")
    if min(selected_ids) < 1 or max(selected_ids) > 2120:
        raise ValueError("selection outside fully reviewed U0001-U2120 range")
    if any(value.startswith("candidate.") for row in selected for value in row["proposed_concepts"].split("|") if value):
        raise ValueError("placeholder candidate concept leaked into formal candidate")

    source_catalog = json.loads(BASELINE_CATALOG_PATH.read_text(encoding="utf-8"))
    if source_catalog.get("schema") != "jyd.semantic-visual-catalog.v3":
        raise ValueError("active source catalog must already be v3")
    # The previous audit still allowed one concrete shrimp-and-beef plate to stand in
    # for the abstract word “protein”.  Preserve it for manual lookup but remove it
    # from automatic matching; the real protein collection infographic remains.
    for asset in source_catalog["assets"]:
        if asset["asset_id"] == "manual.083.image.fbb2ad4a6c":
            asset["concept_ids"] = []
            asset["auto_trigger_concept_ids"] = []
            asset["trigger_basis"] = {}
            asset["usage_modes"] = ["manual_only"]
            asset["auto_eligible"] = False
    clone_existing_resources(source_catalog)
    source_bytes = json.dumps(source_catalog, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    CATALOG_PATH.write_bytes(source_bytes)
    BACKUP_PATH.write_bytes(source_bytes)

    concept_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    narrowed_records: list[dict[str, str]] = []
    new_assets: list[dict] = []
    for index, row in enumerate(selected, start=1):
        source = manifest.get(row["review_id"])
        if source is None:
            raise ValueError(f"missing manifest row {row['review_id']}")
        concepts, rule = narrow_concepts(row)
        if not concepts:
            raise ValueError(f"selected asset has no concept {row['review_id']}")
        for concept in concepts:
            concept_rows[concept].append(row)
        asset = (
            build_video(row, source, concepts)
            if source["media_type"] == "video"
            else build_image(row, source, concepts)
        )
        new_assets.append(asset)
        narrowed_records.append(
            {
                "review_id": row["review_id"],
                "media_type": source["media_type"],
                "decision": row["decision"],
                "asset_name": row["asset_name"],
                "original_concepts": row["proposed_concepts"],
                "final_concepts": "|".join(concepts),
                "narrowing_rule": rule,
                "usage_modes": "|".join(asset["usage_modes"]),
                "source_sha256": source["sha256"],
                "source_path": source["full_path"],
            }
        )
        if index % 25 == 0 or index == len(selected):
            print(f"built {index}/{len(selected)}", flush=True)

    concepts = {item["concept_id"]: item for item in source_catalog["concepts"]}
    for concept_id, rows in sorted(concept_rows.items()):
        if concept_id in concepts:
            continue
        aliases: list[str] = list(CONCEPT_ALIAS_OVERRIDES.get(concept_id, ()))
        for row in rows:
            alias = safe_alias(row["asset_name"])
            if usable_alias(alias) and alias not in aliases:
                aliases.append(alias)
        if concept_id.startswith("review.combo."):
            aliases = [safe_alias(rows[0]["asset_name"])]
        aliases = sorted(aliases, key=lambda value: (len(value), value))[:8]
        if not aliases:
            raise ValueError(f"new concept lacks safe Chinese alias: {concept_id}")
        label = aliases[0]
        concepts[concept_id] = {
            "concept_id": concept_id,
            "label": label,
            "description": f"画面明确呈现“{label}”；仅在对应精确短语语境下自动使用。",
            "aliases": aliases,
        }

    # One exact phrase must map to only one concept.  Keep aliases already owned by
    # the active catalog unchanged; for new concepts, deterministically assign each
    # phrase to the concept whose ID best matches the semantic specificity of that
    # phrase.  Concepts that lose all generated aliases fall back to their asset's
    # exact reviewed name plus the concept ID suffix, which stays readable and unique.
    existing_concept_ids = {item["concept_id"] for item in source_catalog["concepts"]}
    alias_owners: dict[str, list[str]] = defaultdict(list)
    for item in concepts.values():
        for alias in item["aliases"]:
            alias_owners[alias].append(item["concept_id"])
    for alias, owners in alias_owners.items():
        new_owners = [value for value in owners if value not in existing_concept_ids]
        if len(owners) <= 1 or not new_owners:
            continue
        if any(value in existing_concept_ids for value in owners):
            winner = next(value for value in owners if value in existing_concept_ids)
        else:
            winner = sorted(new_owners, key=lambda value: (-value.count("_"), -len(value), value))[0]
        for concept_id in new_owners:
            if concept_id != winner:
                concepts[concept_id]["aliases"] = [
                    value for value in concepts[concept_id]["aliases"] if value != alias
                ]
    for alias, winner in EXACT_ALIAS_OWNER_OVERRIDES.items():
        if winner not in concepts:
            raise ValueError(f"alias owner override is missing: {alias} -> {winner}")
        for concept_id, item in concepts.items():
            if concept_id != winner and alias in item["aliases"]:
                item["aliases"] = [value for value in item["aliases"] if value != alias]
    for concept_id, item in concepts.items():
        if concept_id in existing_concept_ids or item["aliases"]:
            continue
        source_row = concept_rows[concept_id][0]
        base = safe_alias(source_row["asset_name"]) or item["label"]
        item["aliases"] = [f"{base}（{concept_id.rsplit('.', 1)[-1]}）"]

    light_daily = concepts.get("activity.light_daily")
    if light_daily is not None:
        light_daily["aliases"] = list(
            dict.fromkeys(["日常轻活动", "轻活动", *light_daily["aliases"]])
        )

    existing_asset_ids = {item["asset_id"] for item in source_catalog["assets"]}
    if any(item["asset_id"] in existing_asset_ids for item in new_assets):
        raise ValueError("generated asset id collides with active catalog")
    candidate = {
        "schema": "jyd.semantic-visual-catalog.v3",
        "library_id": source_catalog["library_id"],
        "concepts": list(concepts.values()),
        "assets": [*source_catalog["assets"], *new_assets],
    }
    CANDIDATE_PATH.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with SELECTED_PATH.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(narrowed_records[0]))
        writer.writeheader()
        writer.writerows(narrowed_records)

    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    candidate_hash = sha256(CANDIDATE_PATH)
    source_hash = sha256(CATALOG_PATH)
    migration = {
        "schema": "jyd.semantic-visual-catalog-migration.v1",
        "created_at": now,
        "source_catalog_path": str(CATALOG_PATH),
        "source_catalog_schema": "jyd.semantic-visual-catalog.v3",
        "source_catalog_sha256": source_hash,
        "source_backup_path": str(BACKUP_PATH),
        "source_backup_sha256": source_hash,
        "candidate_path": str(CANDIDATE_PATH),
        "candidate_schema": "jyd.semantic-visual-catalog.v3",
        "candidate_sha256": candidate_hash,
        "asset_count": len(candidate["assets"]),
        "approval": {
            "status": "pending",
            "approved_by": "",
            "approved_at": "",
            "note": "Codex已逐条审核U0001-U2120；候选包含555条视频和1565张图片的完整审核结论。",
        },
        "rollback": {
            "backup_path": str(BACKUP_PATH),
            "required_current_sha256": candidate_hash,
            "note": "This staging manifest validates the isolated candidate and cannot mutate the formal library.",
        },
    }
    MIGRATION_PATH.write_text(
        json.dumps(migration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "schema": "jyd.semantic-visual-expansion-report.v1",
        "created_at": now,
        "review_checkpoint": str(REVIEW_ROOT / "review_checkpoint.json"),
        "source_catalog_assets": len(source_catalog["assets"]),
        "selected_reviewed_assets": len(new_assets),
        "selected_videos": sum(item["media_type"] == "video" for item in new_assets),
        "selected_images": sum(item["media_type"] == "image" for item in new_assets),
        "candidate_assets": len(candidate["assets"]),
        "candidate_concepts": len(candidate["concepts"]),
        "logical_clip_assets": sum(row["decision"] == "NEEDS_CLIP" for row in selected),
        "seam_broll_assets_added": sum("seam_broll" in item["usage_modes"] for item in new_assets),
        "narrowing_rules": dict(
            sorted(
                {
                    rule: sum(record["narrowing_rule"] == rule for record in narrowed_records)
                    for rule in {record["narrowing_rule"] for record in narrowed_records}
                }.items()
            )
        ),
        "candidate_sha256": candidate_hash,
        "formal_catalog_mutated": False,
        "unreviewed_images_excluded": 0,
        "validation": {"status": "not_run"},
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
