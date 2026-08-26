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
from app.services.h3.postprocess import extract_reference_frame  # noqa: E402
from app.services.h3_workbench import (  # noqa: E402
    confirm_h3_workbench_batch,
    get_h3_batch,
    h3_account_payload,
    h3_batch_payload,
    prepare_h3_workbench_batch,
)
from app.services.storage import safe_relative_path  # noqa: E402


RUN_TAG = "20260823-v8-rows06-10-a"
SOURCE_CORRELATIONS = tuple(f"xu-v5-photo-count-r{row:02d}" for row in range(6, 11))
HEADSHOT_SOURCE_REQUEST_KEY = "h3-v6-crop-ab-20260823-headshots"
EXPECTED_ROWS = 5
EXPECTED_SEGMENTS = 7


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
    for correlation in SOURCE_CORRELATIONS:
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
            raise RuntimeError("第 6～10 行来源不属于同一账号")
        item = source.items[0]
        rows.append((source, item, json.loads(item.manifest_json)))
    return int(user_id), rows


def _stage_file(db, user: User, path: Path, kind: str, content_type: str, settings):
    upload = _upload(path, content_type)
    try:
        return stage_asset(db, user, upload, kind, settings)
    finally:
        upload.file.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare or confirm H3 v8 rows 06-10: single frames then headshots."
    )
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    output_root = settings.data_dir / "h3-temp" / RUN_TAG

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

        headshot_source = db.scalar(
            select(GenerationBatch).where(
                GenerationBatch.request_key == HEADSHOT_SOURCE_REQUEST_KEY
            )
        )
        if headshot_source is None or headshot_source.user_id != user.id:
            raise RuntimeError("找不到今天的大头照来源批次")
        headshots = [
            safe_relative_path(str(image["path"]), settings.data_dir)
            for image in json.loads(headshot_source.h3_config.reference_images_json)
        ]
        if len(headshots) != 3 or not all(path.is_file() for path in headshots):
            raise RuntimeError("今天的大头照必须恰好为 3 张且文件完整")

        batches = []
        summaries = []
        for offset, (source, item, manifest) in enumerate(sources, start=6):
            row_tag = f"r{offset:02d}"
            request_key = f"h3-v8-rows06-10a-{row_tag}"
            target = _batch_by_request_key(db, user, request_key)
            image_mode = "single_video_frame" if offset <= 8 else "three_headshots"
            if target is None:
                video = safe_relative_path(item.h3_config.reference_video_path, settings.data_dir)
                if not video.is_file():
                    raise RuntimeError(f"{row_tag} 参考视频不存在")
                if offset <= 8:
                    frame = output_root / row_tag / "single-frame-from-video-0.5s.png"
                    extract_reference_frame(video, frame)
                    image_paths = [frame]
                else:
                    image_paths = headshots

                image_assets = [
                    _stage_file(
                        db,
                        user,
                        path,
                        "image",
                        mimetypes.guess_type(str(path))[0] or "image/jpeg",
                        settings,
                    )
                    for path in image_paths
                ]
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
                    name=f"H3 v8 第 {offset} 条：{image_mode}",
                    request_key=request_key,
                    correlation_id=f"{RUN_TAG}-{row_tag}",
                    reference_image_asset_ids=[asset.id for asset in image_assets],
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
                            "row_id": row_tag,
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
                    "row": offset,
                    "source_batch_id": source.id,
                    "target_batch_id": target.id,
                    "video": item.h3_config.reference_video_original_name,
                    "image_mode": image_mode,
                    "user_image_count": 1 if offset <= 8 else 3,
                    "continuity_mode": item.h3_config.continuity_mode,
                    "audio_duration_seconds": item.h3_config.audio_duration_seconds,
                    "segment_count": payload["fee_snapshot"]["segment_count"],
                    "estimated_paid_calls": payload["fee_snapshot"][
                        "estimated_paid_calls"
                    ],
                    "status": target.status,
                }
            )

        segment_count = sum(int(row["segment_count"]) for row in summaries)
        paid_calls = sum(int(row["estimated_paid_calls"]) for row in summaries)
        if len(batches) != EXPECTED_ROWS or segment_count != EXPECTED_SEGMENTS:
            raise RuntimeError(
                f"分段预检不符合授权：rows={len(batches)}, segments={segment_count}"
            )
        if paid_calls != EXPECTED_SEGMENTS:
            raise RuntimeError(
                f"费用预检不符合授权：paid_calls={paid_calls}, expected={EXPECTED_SEGMENTS}"
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

        for batch in batches:
            confirm_h3_workbench_batch(db, user, batch.id, cost_confirmed=True)
        print("CONFIRMED: rows 06-10 queued; automatic paid retry is disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
