"""Deprecated compatibility shim for callers of the first MVP release.

New web and worker code must use ``app.workflows``.  Keeping this tiny wrapper
lets local scripts written against the original MVP continue to work without
duplicating digital-human node IDs outside its adapter.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.audio import parse_timecode
from app.workflows.digital_human import DigitalHumanWorkflow


def build_payload(
    image_file_name: str,
    audio_file_name: str,
    start_time: str,
    end_time: str,
    prompt: str,
    instance_type: str,
) -> dict:
    task = SimpleNamespace(
        input_payload=None,
        image_path="",
        audio_path="",
        image_original_name="",
        audio_original_name="",
        audio_duration_seconds=0,
        start_seconds=parse_timecode(start_time),
        end_seconds=parse_timecode(end_time),
        prompt=prompt,
    )
    return DigitalHumanWorkflow().build_payload(
        task,
        {"image": image_file_name, "audio": audio_file_name},
        ai_app_id="",
        instance_type=instance_type,
        settings={},
    )
