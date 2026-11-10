from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import re
import shutil
from typing import Any

from .project_store import ProjectStore


def _safe_filename(value: Any, fallback: str) -> str:
    name = Path(str(value or "")).name
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return safe[:160] or fallback


class ProjectResultLibrary:
    """Physical result archive plus account-scoped database index for module 7."""

    def __init__(self, store: ProjectStore, root: str | Path) -> None:
        self.store = store
        self.root = Path(root).expanduser().resolve()

    def prepare_batch(
        self,
        owner_user_id: str,
        project_id: str,
        *,
        operation_type: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        project = self.store.get_project(owner_user_id, project_id)
        batch = self.store.allocate_result_batch(
            owner_user_id,
            project_id,
            export_root=self.root,
            operation_type=operation_type,
            now=now,
        )
        directory = Path(batch["export_path"])
        directory.mkdir(parents=True, exist_ok=False)
        source = project.get("script_source")
        if isinstance(source, dict) and source.get("managed_path"):
            source_path = Path(str(source["managed_path"])).resolve()
            if source_path.is_file():
                filename = _safe_filename(source.get("filename"), "脚本.xlsx")
                shutil.copy2(source_path, directory / filename)
                batch["script_filename"] = filename
                return batch

        # Older projects may predate source-file retention. Keep them usable and
        # make the archive self-describing without pretending the CSV is original.
        fallback = directory / "脚本-由项目数据重建.csv"
        with fallback.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["任务ID", "脚本内容"])
            for item in project.get("items", []):
                writer.writerow([item.get("row_key", ""), item.get("script_text", "")])
        batch["script_filename"] = fallback.name
        batch["script_reconstructed"] = True
        return batch

    def list_results(
        self,
        owner_user_id: str,
        *,
        project_id: str = "",
        status: str = "",
        keyword: str = "",
        date_key: str = "",
        batch_no: int | None = None,
    ) -> dict[str, Any]:
        records = self.store.list_gallery_records(owner_user_id)
        videos_by_batch: dict[str, list[dict[str, Any]]] = {}
        legacy_batches: dict[str, dict[str, Any]] = {}
        for video in records["videos"]:
            metadata = video.get("metadata", {})
            result_batch_id = str(metadata.get("result_batch_id") or "")
            if not result_batch_id:
                external = video.get("external_ref", {})
                jianying_batch_id = str(external.get("batch_id") or "legacy")
                result_batch_id = (
                    f"legacy:{video['project_id']}:{jianying_batch_id}"
                )
                created = str(video.get("created_at") or "")
                try:
                    created_at = datetime.fromisoformat(created)
                    legacy_date_key = created_at.strftime("%Y%m%d")
                    legacy_date_label = f"{created_at.month}.{created_at.day}"
                except ValueError:
                    legacy_date_key = ""
                    legacy_date_label = "历史"
                legacy_batches.setdefault(
                    result_batch_id,
                    {
                        "result_batch_id": result_batch_id,
                        "project_id": video["project_id"],
                        "project_no": video["project_no"],
                        "project_name": video["project_name"],
                        "date_key": legacy_date_key,
                        "date_label": legacy_date_label,
                        "batch_no": 0,
                        "export_path": str(Path(str(video.get("managed_path") or "")).parent),
                        "operation_type": "VARIANT_GENERATE",
                        "status": "SUCCEEDED",
                        "jianying_batch_id": jianying_batch_id,
                        "error_message": "",
                        "created_at": created,
                        "updated_at": created,
                        "legacy": True,
                    },
                )
            path = Path(str(video.get("managed_path") or "")).resolve()
            enriched = {
                **video,
                "result_batch_id": result_batch_id,
                "available": path.is_file(),
                "url": (
                    f"/api/new/projects/{video['project_id']}/items/"
                    f"{video['item_id']}/variants/{video['asset_id']}"
                ),
            }
            videos_by_batch.setdefault(result_batch_id, []).append(enriched)

        raw_batches = [*records["batches"], *legacy_batches.values()]
        clean_project = str(project_id or "").strip()
        clean_status = str(status or "").strip().upper()
        clean_keyword = str(keyword or "").strip().casefold()
        clean_date = str(date_key or "").strip()
        batches: list[dict[str, Any]] = []
        for batch in raw_batches:
            if clean_project and batch["project_id"] != clean_project:
                continue
            if clean_status and batch["status"] != clean_status:
                continue
            if clean_date and clean_date not in {batch["date_key"], batch["date_label"]}:
                continue
            if batch_no is not None and int(batch["batch_no"]) != int(batch_no):
                continue
            videos = videos_by_batch.get(batch["result_batch_id"], [])
            if clean_keyword:
                matched = [
                    video
                    for video in videos
                    if clean_keyword
                    in " ".join(
                        str(video.get(key) or "")
                        for key in (
                            "project_no", "project_name", "row_key", "script_text", "filename"
                        )
                    ).casefold()
                ]
                if not matched:
                    continue
                videos = matched
            batches.append(
                {
                    **batch,
                    "videos": videos,
                    "video_count": len(videos),
                    "available_count": sum(video["available"] for video in videos),
                }
            )

        projects = {
            batch["project_id"]: {
                "project_id": batch["project_id"],
                "project_no": batch["project_no"],
                "project_name": batch["project_name"],
            }
            for batch in raw_batches
        }
        return {
            "schema": "jyd.project-result-library.v1",
            "root": str(self.root),
            "total_batches": len(batches),
            "total_videos": sum(batch["video_count"] for batch in batches),
            "projects": sorted(projects.values(), key=lambda item: item["project_no"], reverse=True),
            "available_dates": sorted(
                {
                    (batch["date_key"], batch["date_label"])
                    for batch in raw_batches
                    if batch.get("date_key")
                },
                reverse=True,
            ),
            "available_batches": sorted(
                {
                    int(batch["batch_no"])
                    for batch in raw_batches
                    if int(batch.get("batch_no") or 0) > 0
                }
            ),
            "batches": batches,
        }
