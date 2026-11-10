from app.services.alignment.base import AlignmentResult, AudioAlignmentProvider
from app.services.alignment.funasr_http import FunASRHTTPProvider
from app.services.alignment.registry import (
    get_alignment_provider,
    register_alignment_provider,
)

__all__ = [
    "AlignmentResult",
    "AudioAlignmentProvider",
    "FunASRHTTPProvider",
    "get_alignment_provider",
    "register_alignment_provider",
]
