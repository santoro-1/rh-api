"""MiniMax H3 Ref2VA compilation and workflow graph services."""

from app.services.h3.duration import H3DurationPlan, plan_h3_duration
from app.services.h3.graph import (
    H3DynamicGraphBuilder,
    H3GraphBuildRequest,
    H3GraphBuildResult,
    load_default_h3_graph_builder,
)
from app.services.h3.prompt import (
    H3PromptRequest,
    compile_loop_anchor_ref2va_prompt,
    compile_ref2va_prompt,
)
from app.services.h3.segmentation import (
    H3TimestampedSegment,
    plan_h3_aligned_segments,
    plan_h3_timestamped_segments,
)

__all__ = [
    "H3DurationPlan",
    "H3DynamicGraphBuilder",
    "H3GraphBuildRequest",
    "H3GraphBuildResult",
    "H3PromptRequest",
    "H3TimestampedSegment",
    "compile_loop_anchor_ref2va_prompt",
    "compile_ref2va_prompt",
    "load_default_h3_graph_builder",
    "plan_h3_aligned_segments",
    "plan_h3_duration",
    "plan_h3_timestamped_segments",
]
