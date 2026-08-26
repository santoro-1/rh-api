from __future__ import annotations

import json
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
from app.models import GenerationBatch, GenerationTask  # noqa: E402
from app.services.h3_workbench import get_h3_batch  # noqa: E402
from app.services.storage import safe_relative_path  # noqa: E402


ROWS = (
    (1, "h3-v9-face-only-r01"),
    (2, "h3-v9-face-only-r02"),
    (3, "h3-v9-face-only-r03"),
    (4, "h3-v9-face-only-r04"),
    (5, "h3-v9-face-only-r05"),
    (10, "h3-v9-face-only-r10"),
)
OUTPUT_PREFIX = "H3_v9_第01-05及10条汇总"


def _absolute_data_path(value: str, data_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else safe_relative_path(value, data_dir)


def _copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target.resolve())


def _json(value: str | None, fallback):
    if not value:
        return fallback
    return json.loads(value)


def _run_ffmpeg(inputs: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output.with_suffix(".concat.txt")
    concat_file.write_text(
        "".join(
            f"file '{str(path.resolve()).replace(chr(39), chr(39) * 2)}'\n"
            for path in inputs
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )


def _find_final_video(item, settings, assembled_target: Path) -> Path:
    if item.merged_video_status == "SUCCESS" and item.merged_video_path:
        merged = _absolute_data_path(item.merged_video_path, settings.data_dir)
        if merged.is_file():
            return merged

    successful: list[tuple[int, Path]] = []
    for segment in item.segments:
        task = segment.generation_task
        if task is None or task.status != "SUCCESS" or not task.result_path:
            continue
        result = _absolute_data_path(task.result_path, settings.data_dir)
        if result.is_file():
            successful.append((segment.segment_index, result))
    if len(successful) == 1:
        return successful[0][1]
    if successful and len(successful) == len(item.segments):
        _run_ffmpeg(
            [path for _, path in sorted(successful)],
            assembled_target,
        )
        return assembled_target
    raise RuntimeError(
        f"{item.row_key} 没有可直接汇总的完整成片："
        f"merged={item.merged_video_status}, successful_segments={len(successful)}"
    )


def main() -> int:
    settings = get_settings()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = settings.data_dir / f"{OUTPUT_PREFIX}_{stamp}"
    output_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "schema": "h3.v9.rows01-05-10.collection.v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "output_dir": str(output_root.resolve()),
        "rows": [],
    }
    collected_videos: list[Path] = []

    with SessionLocal() as db:
        for row_number, request_key in ROWS:
            batch = db.scalar(
                select(GenerationBatch).where(GenerationBatch.request_key == request_key)
            )
            if batch is None:
                raise RuntimeError(f"找不到第 {row_number} 条批次：{request_key}")
            batch = get_h3_batch(db, batch.user, batch.id)
            if batch is None or len(batch.items) != 1 or batch.h3_config is None:
                raise RuntimeError(f"第 {row_number} 条批次结构异常")

            item = batch.items[0]
            config = item.h3_config
            if any(
                segment.generation_task is None
                or segment.generation_task.status != "SUCCESS"
                for segment in item.segments
            ):
                raise RuntimeError(f"第 {row_number} 条仍有未成功分段，停止汇总")

            row_dir = output_root / f"{row_number:02d}"
            input_dir = row_dir / "输入"
            prompt_dir = input_dir / "分段提示词"
            final_source = _find_final_video(
                item,
                settings,
                row_dir / f"{row_number:02d}_分段临时拼接.mp4",
            )
            final_target = row_dir / f"{row_number:02d}_最终成片.mp4"
            _copy(final_source, final_target)
            collected_videos.append(final_target)

            reference_video = _absolute_data_path(
                config.reference_video_path, settings.data_dir
            )
            full_audio = _absolute_data_path(config.full_audio_path, settings.data_dir)
            raw_cues = _absolute_data_path(config.raw_cues_path, settings.data_dir)
            source_reference_name = f"n封面{row_number}-1{reference_video.suffix.lower()}"
            copied_video = _copy(
                reference_video,
                input_dir / f"参考视频_{source_reference_name}",
            )
            copied_audio = _copy(
                full_audio,
                input_dir / f"{row_number:02d}_完整音频{full_audio.suffix.lower()}",
            )
            copied_cues = _copy(raw_cues, input_dir / f"分段依据_{raw_cues.name}")

            image_entries = _json(batch.h3_config.reference_images_json, [])
            copied_images = []
            for image_index, image in enumerate(image_entries, start=1):
                source = _absolute_data_path(str(image["path"]), settings.data_dir)
                target = input_dir / f"Picture_{image_index}{source.suffix.lower()}"
                copied_images.append(
                    {
                        "label": f"Picture {image_index}",
                        "source_path": str(source.resolve()),
                        "copied_path": _copy(source, target),
                        "sha256": image.get("sha256"),
                    }
                )

            segments = []
            for segment in item.segments:
                task: GenerationTask | None = segment.generation_task
                output_metadata = _json(task.output_metadata, {}) if task else {}
                prompt_path = prompt_dir / f"段{segment.segment_index:02d}.txt"
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(segment.prompt, encoding="utf-8")
                segments.append(
                    {
                        "segment": segment.segment_index,
                        "script_text": segment.script_text,
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "audio_path": str(
                            _absolute_data_path(
                                segment.audio_path, settings.data_dir
                            ).resolve()
                        ),
                        "prompt_file": str(prompt_path.resolve()),
                        "prompt_template_version": (
                            segment.h3_config.prompt_template_version
                            if segment.h3_config is not None
                            else batch.h3_config.prompt_template_version
                        ),
                        "requested_duration_seconds": (
                            segment.h3_config.requested_generation_duration_seconds
                            if segment.h3_config is not None
                            else None
                        ),
                        "effective_duration_seconds": (
                            output_metadata.get("normalized_timeline_duration_seconds")
                            or (
                                segment.h3_config.effective_generation_duration_seconds
                                if segment.h3_config is not None
                                else None
                            )
                        ),
                        "head_trim": output_metadata.get("head_trim"),
                        "frame_count": (
                            segment.h3_config.quantized_frame_count
                            if segment.h3_config is not None
                            else None
                        ),
                        "runninghub_task_id": task.runninghub_task_id if task else None,
                        "task_status": task.status if task else None,
                    }
                )

            row_manifest = {
                "row": row_number,
                "batch_id": batch.id,
                "batch_name": batch.name,
                "request_key": batch.request_key,
                "correlation_id": batch.correlation_id,
                "status": batch.status,
                "script_text": _json(item.manifest_json, {}).get("script_text", ""),
                "input_mode": (
                    "0图（仅参考视频）"
                    if row_number <= 2
                    else "1张去背景纯脸身份图"
                ),
                "pictures": copied_images,
                "reference_video": {
                    "source_original_name": source_reference_name,
                    "original_name": config.reference_video_original_name,
                    "source_path": str(reference_video.resolve()),
                    "copied_path": copied_video,
                    "sha256": config.reference_video_sha256,
                },
                "audio": {
                    "original_name": config.full_audio_original_name,
                    "source_path": str(full_audio.resolve()),
                    "copied_path": copied_audio,
                    "duration_seconds": config.audio_duration_seconds,
                    "sha256": config.full_audio_sha256,
                    "raw_cues_path": copied_cues,
                },
                "generation": {
                    "prompt_profile_id": batch.h3_config.prompt_profile_id,
                    "prompt_template_version": batch.h3_config.prompt_template_version,
                    "continuity_mode": config.continuity_mode,
                    "aspect_ratio": config.aspect_ratio,
                    "megapixels": config.megapixels,
                    "multiple": config.multiple,
                    "generation_tail_seconds": batch.h3_config.generation_tail_seconds,
                    "adapter_version": batch.h3_config.adapter_version,
                    "segment_count": config.segment_count,
                },
                "segments": segments,
                "final_video": str(final_target.resolve()),
            }
            (row_dir / "输入与参数.json").write_text(
                json.dumps(row_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            manifest["rows"].append(row_manifest)

    combined = output_root / "第01-05及10条_顺序合并预览.mp4"
    _run_ffmpeg(collected_videos, combined)
    manifest["combined_video"] = str(combined.resolve())
    manifest_path = output_root / "第01-05及10条_输入与结果总表.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    readme_lines = [
        "# H3 v9 第 01～05、10 条测试汇总",
        "",
        "- 合并预览：`第01-05及10条_顺序合并预览.mp4`（按 01 → 05 → 10 排列）",
        "- 每条目录都包含最终成片、实际图片、参考视频、完整音频、分段依据和逐段完整提示词。",
        "- 第 01～02 条：不输入图片，仅使用各自参考视频。",
        "- 第 03～05、10 条：共用 1 张去背景、仅保留脸和颈部的身份参考图。",
        "",
        "| 条目 | 图片输入 | 参考视频 | 音频时长 | 衔接 | 分段 |",
        "|---:|---|---|---:|---|---:|",
    ]
    for row in manifest["rows"]:
        readme_lines.append(
            "| {row:02d} | {mode}（{pictures} 张） | {video} | {duration:.3f}s | "
            "{continuity} | {segments} |".format(
                row=row["row"],
                mode=row["input_mode"],
                pictures=len(row["pictures"]),
                video=row["reference_video"]["source_original_name"],
                duration=row["audio"]["duration_seconds"],
                continuity=row["generation"]["continuity_mode"],
                segments=row["generation"]["segment_count"],
            )
        )
    readme_lines.extend(["", "## 台词", ""])
    for row in manifest["rows"]:
        readme_lines.extend([f"### {row['row']:02d}", "", row["script_text"], ""])
    (output_root / "查看说明.md").write_text(
        "\n".join(readme_lines), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "output_dir": str(output_root.resolve()),
                "combined_video": str(combined.resolve()),
                "manifest": str(manifest_path.resolve()),
                "rows": [
                    {
                        "row": row["row"],
                        "status": row["status"],
                        "pictures": len(row["pictures"]),
                        "video": row["reference_video"]["original_name"],
                        "audio": row["audio"]["original_name"],
                        "duration": row["audio"]["duration_seconds"],
                        "continuity": row["generation"]["continuity_mode"],
                        "segments": row["generation"]["segment_count"],
                    }
                    for row in manifest["rows"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
