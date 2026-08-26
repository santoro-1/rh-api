from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    GenerationBatch,
    GenerationBatchItem,
    GenerationSegment,
    GenerationTask,
)


OUTPUT_NAME = "H3测试结果汇总_20260823"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(value: str, *, maximum: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("_", str(value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "未命名")[:maximum].rstrip(" .")


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == source.stat().st_size:
        return
    shutil.copy2(source, target)


def _concat(inputs: list[Path], output: Path) -> str | None:
    if output.is_file() and output.stat().st_size > 0:
        return None
    list_path = output.parent / "拼接输入.txt"
    lines = []
    for path in inputs:
        escaped = path.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        return completed.stderr[-1500:]
    list_path.unlink(missing_ok=True)
    return None


def main() -> int:
    settings = get_settings()
    output_root = settings.data_dir / OUTPUT_NAME
    output_root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "schema": "h3.test-result-collection.v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "source_data_dir": str(settings.data_dir.resolve()),
        "output_dir": str(output_root.resolve()),
        "batches": [],
    }

    with SessionLocal() as db:
        batches = list(
            db.scalars(
                select(GenerationBatch)
                .where(GenerationBatch.workflow_type == "minimax_h3_ref2va")
                .order_by(GenerationBatch.created_at, GenerationBatch.id)
            ).all()
        )
        for batch_index, batch in enumerate(batches, start=1):
            date_label = batch.created_at.strftime("%Y%m%d")
            folder_name = (
                f"{batch_index:02d}_{date_label}_{_safe_name(batch.name)}_"
                f"{batch.id[:8]}"
            )
            batch_dir = output_root / folder_name
            batch_dir.mkdir(parents=True, exist_ok=True)
            item_rows = list(
                db.execute(
                    select(
                        GenerationBatchItem,
                        GenerationSegment,
                        GenerationTask,
                    )
                    .join(
                        GenerationSegment,
                        GenerationSegment.batch_item_id == GenerationBatchItem.id,
                    )
                    .outerjoin(
                        GenerationTask,
                        GenerationTask.segment_id == GenerationSegment.id,
                    )
                    .where(GenerationBatchItem.batch_id == batch.id)
                    .order_by(
                        GenerationBatchItem.row_number,
                        GenerationSegment.segment_index,
                    )
                ).all()
            )
            entry: dict[str, object] = {
                "order": batch_index,
                "batch_id": batch.id,
                "name": batch.name,
                "request_key": batch.request_key,
                "correlation_id": batch.correlation_id,
                "status": batch.status,
                "created_at": batch.created_at.isoformat(),
                "folder": str(batch_dir.resolve()),
                "segments": [],
                "complete_video": None,
                "notes": [],
            }
            successful_files: list[Path] = []
            for item, segment, task in item_rows:
                result = (
                    settings.data_dir / task.result_path
                    if task is not None and task.result_path
                    else None
                )
                exists = bool(result and result.is_file())
                target = None
                if exists and task is not None:
                    target = batch_dir / (
                        f"行{item.row_number:02d}_段{segment.segment_index + 1:02d}_"
                        f"{task.id[:8]}.mp4"
                    )
                    _copy(result, target)
                    successful_files.append(target)
                entry["segments"].append(
                    {
                        "row": item.row_number,
                        "row_id": item.row_key,
                        "segment": segment.segment_index + 1,
                        "segment_status": segment.status,
                        "task_status": task.status if task else None,
                        "runninghub_task_id": task.runninghub_task_id if task else None,
                        "original_path": str(result.resolve()) if exists else None,
                        "collected_path": str(target.resolve()) if target else None,
                    }
                )

            all_segments_successful = bool(item_rows) and len(successful_files) == len(item_rows)
            if batch.status == "SUCCESS" and all_segments_successful:
                complete_video = batch_dir / "完整拼接结果.mp4"
                if len(successful_files) == 1:
                    _copy(successful_files[0], complete_video)
                    concat_error = None
                else:
                    concat_error = _concat(successful_files, complete_video)
                if concat_error is None and complete_video.is_file():
                    entry["complete_video"] = str(complete_video.resolve())
                else:
                    entry["notes"].append("拼接失败：" + str(concat_error))
            else:
                entry["notes"].append(
                    "该批次并非全部成功；仅收集数据库中已有的成功分段。"
                )
            manifest["batches"].append(entry)

    manifest_path = output_root / "H3测试结果清单.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# H3 测试结果汇总",
        "",
        f"- 汇总目录：`{output_root.resolve()}`",
        f"- 批次数：{len(manifest['batches'])}",
        "",
    ]
    for entry in manifest["batches"]:
        lines.extend(
            [
                f"## {entry['order']:02d}. {entry['name']}",
                "",
                f"- 状态：{entry['status']}",
                f"- 批次 ID：`{entry['batch_id']}`",
                f"- 文件夹：`{entry['folder']}`",
                f"- 完整结果：`{entry['complete_video'] or '无'}`",
                f"- 已收集分段：{sum(1 for value in entry['segments'] if value['collected_path'])}/{len(entry['segments'])}",
                "",
            ]
        )
        for note in entry["notes"]:
            lines.append(f"> {note}")
            lines.append("")
    (output_root / "H3测试结果清单.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output_dir": str(output_root.resolve()),
                "batch_count": len(manifest["batches"]),
                "segment_file_count": sum(
                    1
                    for entry in manifest["batches"]
                    for segment in entry["segments"]
                    if segment["collected_path"]
                ),
                "complete_video_count": sum(
                    1 for entry in manifest["batches"] if entry["complete_video"]
                ),
                "manifest": str(manifest_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
