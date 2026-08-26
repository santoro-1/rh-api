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


SOURCE_REQUEST_KEYS = (
    "h3-v7-temp-0823a-r02",
    "h3-v7-temp-0823a-r03",
)
RUN_TAG = "h3-v7-soft-chain-0823a"
EXPECTED_SEGMENTS = 4


def _upload(path: Path, content_type: str) -> UploadFile:
    return UploadFile(
        file=path.open("rb"),
        filename=path.name,
        headers=Headers({"content-type": content_type}),
    )


def _get_by_request_key(db, user: User, request_key: str):
    batch_id = db.scalar(
        select(GenerationBatch.id).where(
            GenerationBatch.user_id == user.id,
            GenerationBatch.request_key == request_key,
        )
    )
    return get_h3_batch(db, user, batch_id) if batch_id else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the exact r02/r03 H3 v7 inputs in soft-chain mode."
    )
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    settings = get_settings()

    with SessionLocal() as db:
        source_stub = db.scalar(
            select(GenerationBatch).where(
                GenerationBatch.request_key == SOURCE_REQUEST_KEYS[0]
            )
        )
        if source_stub is None:
            raise RuntimeError("找不到第 2 条快速模式来源批次")
        user = db.get(User, source_stub.user_id)
        if user is None or not user.is_active:
            raise RuntimeError("来源用户不存在或已停用")

        account_payload = h3_account_payload(db, user)
        account_ids = [
            int(account["id"])
            for account in account_payload["accounts"]
            if account.get("availability") == "AVAILABLE"
        ]
        if not account_ids:
            raise RuntimeError("没有 AVAILABLE 的 H3 执行账号")

        prepared_batches = []
        summaries = []
        for source_key in SOURCE_REQUEST_KEYS:
            source = _get_by_request_key(db, user, source_key)
            if source is None or len(source.items) != 1:
                raise RuntimeError(f"来源批次不存在或结构异常：{source_key}")
            source_item = source.items[0]
            row_id = source_item.row_key
            request_key = f"{RUN_TAG}-{row_id}"
            target = _get_by_request_key(db, user, request_key)
            if target is None:
                reference_images = json.loads(source.h3_config.reference_images_json)
                if len(reference_images) != 1:
                    raise RuntimeError(f"{row_id} 不是单 Picture 1 测试")
                picture_path = safe_relative_path(
                    str(reference_images[0]["path"]), settings.data_dir
                )
                video_path = safe_relative_path(
                    source_item.h3_config.reference_video_path, settings.data_dir
                )
                if not picture_path.is_file() or not video_path.is_file():
                    raise RuntimeError(f"{row_id} 的原始测试素材不存在")

                picture_upload = _upload(picture_path, "image/png")
                video_upload = _upload(
                    video_path,
                    mimetypes.guess_type(str(video_path))[0] or "video/mp4",
                )
                try:
                    picture_asset = stage_asset(
                        db, user, picture_upload, "image", settings
                    )
                    video_asset = stage_asset(db, user, video_upload, "video", settings)
                finally:
                    picture_upload.file.close()
                    video_upload.file.close()

                manifest = json.loads(source_item.manifest_json)
                target, _ = prepare_h3_workbench_batch(
                    db,
                    user,
                    settings,
                    name=f"H3 v7 连续模式对照 {row_id}",
                    request_key=request_key,
                    correlation_id=request_key,
                    reference_image_asset_ids=[picture_asset.id],
                    selected_account_ids=account_ids,
                    defaults={
                        "continuity_mode": "soft_chain",
                        "generation_tail_seconds": 0.5,
                        "resolution": {
                            "aspect_ratio": "9:16 (Portrait Widescreen)",
                            "megapixels": 1,
                            "multiple": 32,
                        },
                    },
                    rows=[
                        {
                            "row_id": row_id,
                            "script_text": manifest["script_text"],
                            "video_asset_id": video_asset.id,
                            "audio_batch_id": source_item.h3_config.audio_batch_id,
                            "audio_item_id": source_item.h3_config.audio_item_id,
                            "audio_generation_version": (
                                source_item.h3_config.audio_generation_version
                            ),
                        }
                    ],
                )
            payload = h3_batch_payload(target)
            prepared_batches.append(target)
            summaries.append(
                {
                    "row_id": row_id,
                    "source_batch_id": source.id,
                    "target_batch_id": target.id,
                    "status": target.status,
                    "segment_count": payload["fee_snapshot"]["segment_count"],
                    "estimated_paid_calls": payload["fee_snapshot"][
                        "estimated_paid_calls"
                    ],
                    "continuity_mode": "soft_chain",
                }
            )

        segment_count = sum(int(row["segment_count"]) for row in summaries)
        paid_calls = sum(int(row["estimated_paid_calls"]) for row in summaries)
        if segment_count != EXPECTED_SEGMENTS or paid_calls != EXPECTED_SEGMENTS:
            raise RuntimeError(
                "连续模式费用预检与授权不一致，拒绝确认："
                f"segments={segment_count}, paid_calls={paid_calls}"
            )
        print(
            json.dumps(
                {
                    "run_tag": RUN_TAG,
                    "selected_account_ids": account_ids,
                    "rows": summaries,
                    "segment_count": segment_count,
                    "estimated_paid_calls": paid_calls,
                    "confirmed": bool(args.confirm),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.confirm:
            print("PREPARED_ONLY")
            return 0

        for target in prepared_batches:
            confirm_h3_workbench_batch(
                db, user, target.id, cost_confirmed=True
            )
        print("CONFIRMED: r02/r03 soft-chain comparison queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
