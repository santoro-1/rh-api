from __future__ import annotations

from dataclasses import dataclass, replace


TERMINAL_SUCCESS = "SUCCESS"
PENDING = "PENDING"
INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class H3SegmentJob:
    segment_id: str
    item_id: str
    segment_index: int
    continuity_mode: str
    status: str = PENDING
    continuity_anchor: str | None = None


def ready_jobs(jobs: list[H3SegmentJob]) -> list[H3SegmentJob]:
    """Return paid submissions currently allowed by fast/soft-chain dependencies."""

    by_item: dict[str, list[H3SegmentJob]] = {}
    for job in jobs:
        if job.continuity_mode not in {"fast", "soft_chain"}:
            raise ValueError("H3 连续性模式不合法")
        by_item.setdefault(job.item_id, []).append(job)
    ready = []
    for item_jobs in by_item.values():
        item_jobs.sort(key=lambda job: job.segment_index)
        if [job.segment_index for job in item_jobs] != list(range(len(item_jobs))):
            raise ValueError("H3 同一脚本行的分段序号必须从 0 连续递增")
        if item_jobs[0].continuity_mode == "fast":
            if any(job.continuity_mode != "fast" for job in item_jobs):
                raise ValueError("H3 同一脚本行不能混用连续性模式")
            ready.extend(job for job in item_jobs if job.status == PENDING)
            continue
        if any(job.continuity_mode != "soft_chain" for job in item_jobs):
            raise ValueError("H3 同一脚本行不能混用连续性模式")
        for job in item_jobs:
            if job.status != PENDING:
                if job.status != TERMINAL_SUCCESS:
                    break
                continue
            if job.segment_index == 0 or (
                item_jobs[job.segment_index - 1].status == TERMINAL_SUCCESS
                and bool(job.continuity_anchor)
            ):
                ready.append(job)
            break
    return sorted(ready, key=lambda job: (job.item_id, job.segment_index))


def invalidate_downstream(
    jobs: list[H3SegmentJob],
    *,
    item_id: str,
    regenerated_segment_index: int,
) -> list[H3SegmentJob]:
    """Invalidate descendants while retaining every historical file and job record."""

    return [
        replace(job, status=INVALIDATED, continuity_anchor=None)
        if job.item_id == item_id and job.segment_index > regenerated_segment_index
        else job
        for job in jobs
    ]

