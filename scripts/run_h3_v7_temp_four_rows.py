from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import GenerationBatch, User  # noqa: E402
from app.services.batch_assets import stage_asset  # noqa: E402
from app.services.h3_workbench import (  # noqa: E402
    confirm_h3_workbench_batch,
    get_h3_batch,
    h3_account_payload,
    h3_batch_payload,
    prepare_h3_workbench_batch,
)
from app.services.storage import safe_relative_path  # noqa: E402


RUN_TAG = "20260823-v7-picture1-temp-a"
SOURCE_CORRELATIONS = (
    "xu-v5-photo-count-r01",
    "xu-v5-photo-count-r02",
    "xu-v5-photo-count-r03",
    "xu-v5-photo-count-r04",
)
EXPECTED_ROWS = 4
EXPECTED_SEGMENTS = 8


def _run_ffmpeg(source: Path, target: Path) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            "0.5",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "format=rgb24",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not target.is_file():
        raise RuntimeError(
            f"从参考视频截取临时 Picture 1 失败：{source}\n{completed.stderr[-1200:]}"
        )


def _upload(path: Path, content_type: str) -> UploadFile:
    return UploadFile(
        file=path.open("rb"),
        filename=path.name,
        headers=Headers({"content-type": content_type}),
    )


def _existing_batch(db, user_id: int, request_key: str):
    batch_id = db.scalar(
        select(GenerationBatch.id).where(
            GenerationBatch.user_id == user_id,
            GenerationBatch.request_key == request_key,
        )
    )
    return get_h3_batch(db, db.get(User, user_id), batch_id) if batch_id else None


def _source_rows(db, settings):
    rows = []
    user_id = None
    for position, correlation in enumerate(SOURCE_CORRELATIONS, start=1):
        source_batch = db.scalar(
            select(GenerationBatch).where(
                GenerationBatch.correlation_id == correlation
            )
        )
        if source_batch is None:
            raise RuntimeError(f"找不到临时测试来源：{correlation}")
        source_batch = get_h3_batch(db, db.get(User, source_batch.user_id), source_batch.id)
        if source_batch is None or len(source_batch.items) != 1:
            raise RuntimeError(f"临时测试来源结构不符合预期：{correlation}")
        if user_id is None:
            user_id = source_batch.user_id
        elif source_batch.user_id != user_id:
            raise RuntimeError("四条临时测试来源不属于同一个本地账号")
        item = source_batch.items[0]
        manifest = json.loads(item.manifest_json)
        duration = float(item.h3_config.audio_duration_seconds)
        if duration <= 15:
            raise RuntimeError(f"{correlation} 音频只有 {duration:.3f}s，不满足 15s 以上")
        video = safe_relative_path(item.h3_config.reference_video_path, settings.data_dir)
        if not video.is_file():
            raise RuntimeError(f"参考视频不存在：{video}")
        rows.append(
            {
                "position": position,
                "correlation": correlation,
                "script_text": str(manifest["script_text"]),
                "audio_batch_id": item.h3_config.audio_batch_id,
                "audio_item_id": item.h3_config.audio_item_id,
                "audio_generation_version": item.h3_config.audio_generation_version,
                "audio_duration_seconds": duration,
                "video": video,
            }
        )
    return int(user_id), rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or confirm four temporary H3 v7 Picture-1 tests."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="After exact fee preflight, confirm and queue the paid H3 segments.",
    )
    args = parser.parse_args()
    settings = get_settings()
    output_root = settings.data_dir / "h3-temp" / RUN_TAG

    with SessionLocal() as db:
        user_id, sources = _source_rows(db, settings)
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise RuntimeError("临时测试所属本地账号不存在或已停用")

        accounts = h3_account_payload(db, user)
        available_account_ids = [
            int(account["id"])
            for account in accounts["accounts"]
            if account.get("availability") == "AVAILABLE"
        ]
        if not available_account_ids:
            raise RuntimeError("当前没有 AVAILABLE 的 H3 执行账号，未创建付费任务")

        batches = []
        for source in sources:
            position = int(source["position"])
            row_tag = f"r{position:02d}"
            request_key = f"h3-v7-temp-0823a-{row_tag}"
            batch = _existing_batch(db, user.id, request_key)
            if batch is None:
                frame = output_root / row_tag / "picture-1-from-video-0.5s.png"
                _run_ffmpeg(Path(source["video"]), frame)
                image_upload = _upload(frame, "image/png")
                video_type = mimetypes.guess_type(str(source["video"]))[0] or "video/mp4"
                video_upload = _upload(Path(source["video"]), video_type)
                try:
                    image_asset = stage_asset(db, user, image_upload, "image", settings)
                    video_asset = stage_asset(db, user, video_upload, "video", settings)
                finally:
                    image_upload.file.close()
                    video_upload.file.close()
                batch, _ = prepare_h3_workbench_batch(
                    db,
                    user,
                    settings,
                    name=f"H3 v7 临时测试 {row_tag}",
                    request_key=request_key,
                    correlation_id=f"h3-v7-temp-0823a-{row_tag}",
                    reference_image_asset_ids=[image_asset.id],
                    selected_account_ids=available_account_ids,
                    defaults={
                        "continuity_mode": "fast",
                        "generation_tail_seconds": 0.5,
                        "resolution": {
                            "aspect_ratio": "9:16 (Portrait Widescreen)",
                            "megapixels": 1,
                            "multiple": 32,
                        },
                    },
                    rows=[
                        {
                            "row_id": row_tag,
                            "script_text": source["script_text"],
                            "video_asset_id": video_asset.id,
                            "audio_batch_id": source["audio_batch_id"],
                            "audio_item_id": source["audio_item_id"],
                            "audio_generation_version": source[
                                "audio_generation_version"
                            ],
                        }
                    ],
                )
            batches.append(batch)

        previews = [h3_batch_payload(batch) for batch in batches]
        segment_count = sum(int(value["fee_snapshot"]["segment_count"]) for value in previews)
        estimated_paid_calls = sum(
            int(value["fee_snapshot"]["estimated_paid_calls"]) for value in previews
        )
        if len(batches) != EXPECTED_ROWS or segment_count != EXPECTED_SEGMENTS:
            raise RuntimeError(
                "费用预检数量与用户授权不一致，拒绝确认："
                f"rows={len(batches)}, segments={segment_count}"
            )

        print(
            json.dumps(
                {
                    "run_tag": RUN_TAG,
                    "user_id": user.id,
                    "selected_account_ids": available_account_ids,
                    "rows": [
                        {
                            "source": source["correlation"],
                            "audio_duration_seconds": source["audio_duration_seconds"],
                            "batch_id": batch.id,
                            "status": batch.status,
                            "segment_count": preview["fee_snapshot"]["segment_count"],
                            "estimated_paid_calls": preview["fee_snapshot"][
                                "estimated_paid_calls"
                            ],
                        }
                        for source, batch, preview in zip(sources, batches, previews)
                    ],
                    "segment_count": segment_count,
                    "estimated_paid_calls": estimated_paid_calls,
                    "confirmed": bool(args.confirm),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        if not args.confirm:
            print("PREPARED_ONLY: rerun with --confirm to queue paid H3 tasks")
            return 0

        for batch in batches:
            confirm_h3_workbench_batch(
                db,
                user,
                batch.id,
                cost_confirmed=True,
            )
        print("CONFIRMED: four rows queued; no automatic retry is performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
