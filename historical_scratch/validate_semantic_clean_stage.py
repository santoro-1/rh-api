from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


sys.stdout.reconfigure(encoding="utf-8")

REPO_LIBRARY = Path(r"D:\工作内容\轻盈健\公寓\jyd_plain_json_probe\data\libraries\semantic_visual_library")
STAGE = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\semantic_clean_stage_20260819")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


original = json.loads((REPO_LIBRARY / "catalog.json").read_text(encoding="utf-8"))
candidate = json.loads((STAGE / "catalog.json").read_text(encoding="utf-8"))
original_quarantine = json.loads(
    (REPO_LIBRARY / "quarantine" / "platform_ui_pending.json").read_text(encoding="utf-8")
)
candidate_quarantine = json.loads((STAGE / "platform_ui_pending.json").read_text(encoding="utf-8"))
manifest = json.loads((STAGE / "manifest.json").read_text(encoding="utf-8"))

assert len(manifest) == 45
assert len(candidate["assets"]) == len(original["assets"]) + 45
assert len(candidate_quarantine["assets"]) == 45
assert candidate_quarantine["assets"] == original_quarantine["assets"][:45]

candidate_by_id = {asset["asset_id"]: asset for asset in candidate["assets"]}
assert len(candidate_by_id) == len(candidate["assets"])
new_ids = {row["new_asset_id"] for row in manifest}
assert len(new_ids) == 45
assert not new_ids.intersection({asset["asset_id"] for asset in original["assets"]})

for row in manifest:
    asset = candidate_by_id[row["new_asset_id"]]
    folder = STAGE / "videos" / row["folder"]
    video = folder / "video.mp4"
    poster = folder / "poster.png"
    metadata_path = folder / "metadata.json"
    assert video.is_file() and poster.is_file() and metadata_path.is_file()
    assert sha256(video) == row["sha256"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["lineage"]["source_asset_id"] == row["source_asset_id"]
    assert metadata["content_sha256"] == row["sha256"]
    assert asset["resource"]["duration_us"] == row["duration_us"]
    assert asset["resource"]["width"] == row["width"]
    assert asset["resource"]["height"] == row["height"]
    assert asset["cleanliness_grade"] == "A"
    assert asset["platform_ui_status"] == "none"
    assert asset["auto_eligible"] is True
    assert asset["requires_clip"] is False
    assert asset["defaults"]["source_start_us"] == 0
    assert asset["defaults"]["duration_us"] <= asset["resource"]["duration_us"]

for asset_id in {
    "broll.20260814.001.video.0acd36aeb5",
    "broll.20260814.012.video.40af9611e5",
    "broll.20260814.017.video.ab8e7a8cd1",
    "broll.20260814.018.video.bd046fca01",
    "broll.20260814.021.video.fd64795cf2",
    "broll.20260814.026.video.e74d4bb115",
    "broll.20260814.029.video.68731f13cd",
    "broll.20260814.031.video.80eb4148e8",
}:
    assert candidate_by_id[asset_id]["platform_ui_status"] == "none"
    assert asset_id not in {row["asset_id"] for row in candidate_quarantine["assets"]}

print("stage validation passed")
print(f"original_assets={len(original['assets'])} candidate_assets={len(candidate['assets'])}")
print(f"quarantine={len(original_quarantine['assets'])}->{len(candidate_quarantine['assets'])}")
