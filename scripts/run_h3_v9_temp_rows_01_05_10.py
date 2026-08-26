from __future__ import annotations

import argparse
import json
import mimetypes
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


RUN_TAG = "20260823-v9-rows01-05-10-face-only"
SOURCE_ROWS = (1, 2, 3, 4, 5, 10)
SOURCE_CORRELATIONS = {
    row: f"xu-v5-photo-count-r{row:02d}" for row in SOURCE_ROWS
}
IDENTITY_IMAGE = (
    PROJECT_ROOT
    / "data"
    / "h3-temp"
    / "20260823-v9-rows01-05-10"
    / "identity-face-only-final.png"
)


def _upload(path: Path, content_type: str) -> UploadFile:
    return UploadFile(
        file=path.open("rb"),
        filename=path.name,
        headers=Headers({"content-type": content_type}),
    )


def _batch_by_request_key(db, user: User, request_key: str):
    batch_id = db.scalar(
        select(GenerationBatch.id).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    return get_h3_batch(db, user, batch_id) if batch_id else None


def _source_rows(db):
    rows = []
    user_id = None
    for row in SOURCE_ROWS:
        correlation = SOURCE_CORRELATIONS[row]
        source = db.scalar(
            select(GenerationBatch).where(GenerationBatch.correlation_id == correlation)
        )
        if source is None:
            raise RuntimeError(f"找不到来源批次：{correlation}")
        user = db.get(User, source.user_id)
        source = get_h3_batch(db, user, source.id)
        if source is None or len(source.items) != 1:
            raise RuntimeError(f"来源批次结构异常：{correlation}")
        if user_id is None:
            user_id = source.user_id
        elif source.user_id != user_id:
            raise RuntimeError("来源行不属于同一账号")
        item = source.items[0]
        rows.append((row, source, item, json.loads(item.manifest_json)))
    return int(user_id), rows


def _stage_file(db, user: User, path: Path, kind: str, content_type: str, settings):
    upload = _upload(path, content_type)
    try:
        return stage_asset(db, user, upload, kind, settings)
    finally:
        upload.file.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare/confirm H3 v8 prompt test for rows 01-05 and 10."
    )
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-paid-calls", type=int)
    arguments = parser.parse_args()
    if arguments.confirm and arguments.expected_paid_calls is None:
        raise RuntimeError("确认付费前必须显式传入 --expected-paid-calls")

    settings = get_settings()
    if not IDENTITY_IMAGE.is_file():
        raise RuntimeError(f"单张纯身份图不存在：{IDENTITY_IMAGE}")

    with SessionLocal() as db:
        user_id, sources = _source_rows(db)
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise RuntimeError("测试账号不存在或已停用")

        account_ids = [
            int(account["id"])
            for account in h3_account_payload(db, user)["accounts"]
            if account.get("availability") == "AVAILABLE"
        ]
        if not account_ids:
            raise RuntimeError("当前没有 AVAILABLE 的 H3 执行账号")

        batches = []
        summaries = []
        for row, source, item, manifest in sources:
            request_key = f"h3-v9-face-only-r{row:02d}"
            target = _batch_by_request_key(db, user, request_key)
            image_count = 0 if row <= 2 else 1
            if target is None:
                video = safe_relative_path(
                    item.h3_config.reference_video_path, settings.data_dir
                )
                if not video.is_file():
                    raise RuntimeError(f"r{row:02d} 参考视频不存在")

                image_asset_ids = []
                if image_count:
                    image_asset = _stage_file(
                        db,
                        user,
                        IDENTITY_IMAGE,
                        "image",
                        "image/png",
                        settings,
                    )
                    image_asset_ids.append(image_asset.id)
                video_asset = _stage_file(
                    db,
                    user,
                    video,
                    "video",
                    mimetypes.guess_type(str(video))[0] or "video/mp4",
                    settings,
                )
                target, _ = prepare_h3_workbench_batch(
                    db,
                    user,
                    settings,
                    name=f"H3 v9 第 {row} 条：{image_count} 张纯身份图",
                    request_key=request_key,
                    correlation_id=f"{RUN_TAG}-r{row:02d}",
                    reference_image_asset_ids=image_asset_ids,
                    selected_account_ids=account_ids,
                    defaults={
                        "continuity_mode": item.h3_config.continuity_mode,
                        "generation_tail_seconds": 0.5,
                        "resolution": {
                            "aspect_ratio": "9:16 (Portrait Widescreen)",
                            "megapixels": 1,
                            "multiple": 32,
                        },
                    },
                    rows=[
                        {
                            "row_id": f"r{row:02d}",
                            "script_text": manifest["script_text"],
                            "video_asset_id": video_asset.id,
                            "audio_batch_id": item.h3_config.audio_batch_id,
                            "audio_item_id": item.h3_config.audio_item_id,
                            "audio_generation_version": (
                                item.h3_config.audio_generation_version
                            ),
                        }
                    ],
                )

            payload = h3_batch_payload(target)
            batches.append(target)
            summaries.append(
                {
                    "row": row,
                    "source_batch_id": source.id,
                    "target_batch_id": target.id,
                    "input_images": image_count,
                    "identity_image": str(IDENTITY_IMAGE.resolve()) if image_count else None,
                    "reference_video": item.h3_config.reference_video_original_name,
                    "audio_duration_seconds": item.h3_config.audio_duration_seconds,
                    "continuity_mode": item.h3_config.continuity_mode,
                    "segment_count": payload["fee_snapshot"]["segment_count"],
                    "estimated_paid_calls": payload["fee_snapshot"][
                        "estimated_paid_calls"
                    ],
                    "segments": [
                        {
                            "segment": segment.segment_index,
                            "status": segment.status,
                            "runninghub_task_id": (
                                segment.generation_task.runninghub_task_id
                                if segment.generation_task is not None
                                else None
                            ),
                            "task_status": (
                                segment.generation_task.status
                                if segment.generation_task is not None
                                else None
                            ),
                        }
                        for segment in target.items[0].segments
                    ],
                    "prompt_template_version": target.h3_config.prompt_template_version,
                    "status": target.status,
                }
            )

        paid_calls = sum(int(row["estimated_paid_calls"]) for row in summaries)
        segment_count = sum(int(row["segment_count"]) for row in summaries)
        report = {
            "run_tag": RUN_TAG,
            "selected_account_ids": account_ids,
            "rows": summaries,
            "row_count": len(summaries),
            "segment_count": segment_count,
            "estimated_paid_calls": paid_calls,
            "confirmed": bool(arguments.confirm),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not arguments.confirm:
            print("PREPARED_ONLY")
            return 0
        if paid_calls != arguments.expected_paid_calls:
            raise RuntimeError(
                f"费用预检变化：actual={paid_calls}, expected={arguments.expected_paid_calls}"
            )

        for batch in batches:
            confirm_h3_workbench_batch(db, user, batch.id, cost_confirmed=True)
        print(
            f"CONFIRMED: {len(batches)} rows / {paid_calls} paid calls queued; "
            "automatic paid retry is disabled"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
