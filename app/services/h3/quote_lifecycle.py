"""Atomic, no-provider cancellation of an unsubmitted H3 quote."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import and_, exists, or_, select, update

from app.models import GenerationBatch, GenerationBatchItem, GenerationSegment, GenerationTask, H3BatchConfig, H3SegmentConfig


def quote_token(batch) -> str:
    return hashlib.sha256(f"h3-quote-v1:{batch.id}:{batch.h3_config.input_sha256}".encode()).hexdigest()


def quote_capability(batch) -> dict:
    cancellable = (batch.status == "AWAITING_COST_CONFIRMATION"
                   and batch.h3_config.confirmed_at is None
                   and all(item.status == "AWAITING_COST_CONFIRMATION"
                           and all((segment.status == "AWAITING_COST_CONFIRMATION"
                                    or (segment.status == "WAITING_DEPENDENCY" and segment.segment_index > 0
                                        and segment.h3_config is not None
                                        and segment.h3_config.continuity_mode == "soft_chain"))
                                   and segment.generation_task is None for segment in item.segments)
                           for item in batch.items))
    return {"schema": "runninghub.h3-quote-recovery.v1", "can_cancel_quote": cancellable,
            "quote_token": quote_token(batch)}


def claim_quote(db, batch, next_status: str) -> bool:
    """Both confirm and cancel must acquire this same database compare-and-swap."""
    item_ids = select(GenerationBatchItem.id).where(GenerationBatchItem.batch_id == batch.id)
    # Soft-chain later segments are parked on an anchor already at quote time.
    # This is safe only while the batch is unconfirmed and has zero tasks.
    unsubmitted_segment = or_(
        GenerationSegment.status == "AWAITING_COST_CONFIRMATION",
        and_(GenerationSegment.status == "WAITING_DEPENDENCY", GenerationSegment.segment_index > 0,
             exists(select(H3SegmentConfig.segment_id).where(
                 H3SegmentConfig.segment_id == GenerationSegment.id,
                 H3SegmentConfig.continuity_mode == "soft_chain"))),
    )
    result = db.execute(update(GenerationBatch).where(
        GenerationBatch.id == batch.id,
        GenerationBatch.status == "AWAITING_COST_CONFIRMATION",
        exists(select(H3BatchConfig.batch_id).where(
            H3BatchConfig.batch_id == batch.id, H3BatchConfig.confirmed_at.is_(None))),
        ~exists(select(GenerationTask.id).where(GenerationTask.batch_item_id.in_(item_ids))),
        ~exists(select(GenerationBatchItem.id).where(
            GenerationBatchItem.batch_id == batch.id,
            GenerationBatchItem.status != "AWAITING_COST_CONFIRMATION")),
        ~exists(select(GenerationSegment.id).where(
            GenerationSegment.batch_item_id.in_(item_ids),
            ~unsubmitted_segment)),
    ).values(status=next_status).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        return False
    batch.status = next_status
    return True


def finish_quote_cancellation(db, batch, request_key: str):
    receipt = json.loads(batch.h3_config.fee_snapshot_json or "{}")
    receipt["quote_cancellation"] = {
        "request_key": request_key, "cancelled_at": datetime.now(timezone.utc).isoformat(),
        "quote_token": quote_token(batch),
    }
    batch.h3_config.fee_snapshot_json = json.dumps(receipt, ensure_ascii=False)
    for item in batch.items:
        item.status = "CANCELLED"
        for segment in item.segments:
            segment.status = "CANCELLED"
    db.commit()
    db.refresh(batch)
    return batch
