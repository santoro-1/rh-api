from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


H3_FPS = 24
H3_FRAME_MODULUS = 17
H3_FRAME_REMAINDER = 5
H3_MIN_REQUEST_SECONDS = Decimal("4")
H3_MAX_REQUEST_SECONDS = Decimal("15")
H3_DEFAULT_GENERATION_TAIL_SECONDS = Decimal("0.1")


@dataclass(frozen=True)
class H3DurationPlan:
    audio_duration_seconds: float
    generation_tail_seconds: float
    requested_generation_duration_seconds: float
    quantized_frame_count: int
    effective_generation_duration_seconds: float


def _positive_decimal(value: object, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field}不合法")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}不合法") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError(f"{field}不合法")
    return parsed


def _quantized_frame_count(requested_seconds: Decimal) -> int:
    base_frames = max(
        5,
        int((requested_seconds * H3_FPS).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
    )
    return base_frames + (
        H3_FRAME_REMAINDER - (base_frames % H3_FRAME_MODULUS)
    ) % H3_FRAME_MODULUS


H3_MAX_QUANTIZED_FRAMES = _quantized_frame_count(H3_MAX_REQUEST_SECONDS)


def plan_h3_duration(
    audio_duration_seconds: object,
    generation_tail_seconds: object = H3_DEFAULT_GENERATION_TAIL_SECONDS,
) -> H3DurationPlan:
    """Plan node 84 and node 92 using the workflow's exact positive-number rules.

    The 0.1-second default is a provider request margin. It is not part of the
    final edit and does not change the authoritative segment audio duration.
    """

    audio_duration = _positive_decimal(audio_duration_seconds, "分段音频时长")
    tail_duration = _positive_decimal(
        generation_tail_seconds,
        "H3 生成安全余量",
        allow_zero=True,
    )
    requested_duration = audio_duration + tail_duration
    if requested_duration < H3_MIN_REQUEST_SECONDS:
        raise ValueError("分段音频加生成余量后不能短于 4 秒")
    if requested_duration > H3_MAX_REQUEST_SECONDS:
        raise ValueError("分段音频加生成余量后不能超过 15 秒，必须先重新分段")

    frame_count = _quantized_frame_count(requested_duration)
    if frame_count > H3_MAX_QUANTIZED_FRAMES:
        raise ValueError("H3 帧网格量化结果超过当前适配器上限，必须先重新分段")

    effective_duration = Decimal(frame_count) / H3_FPS
    return H3DurationPlan(
        audio_duration_seconds=float(audio_duration),
        generation_tail_seconds=float(tail_duration),
        requested_generation_duration_seconds=float(requested_duration),
        quantized_frame_count=frame_count,
        effective_generation_duration_seconds=float(effective_duration),
    )
