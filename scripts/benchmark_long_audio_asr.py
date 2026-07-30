from __future__ import annotations

import argparse
import json
import mimetypes
import time

import requests
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import LongAudioProject
from app.services.alignment.funasr_http import _parse_tokens
from app.services.alignment.script_timestamps import (
    plan_script_aligned_segments,
)
from app.services.storage import safe_relative_path


def _project(project_id: str | None) -> LongAudioProject:
    with SessionLocal() as db:
        if project_id:
            project = db.get(LongAudioProject, project_id)
        else:
            project = db.scalar(
                select(LongAudioProject)
                .order_by(LongAudioProject.created_at.desc())
                .limit(1)
            )
        if project is None:
            raise RuntimeError("没有找到长音频预处理项目")
        db.expunge(project)
        return project


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只读测试最新长音频项目的 ASR 对齐效果"
    )
    parser.add_argument("--project-id")
    arguments = parser.parse_args()
    settings = get_settings()
    project = _project(arguments.project_id)
    audio_path = safe_relative_path(project.audio_path, settings.data_dir)
    if not audio_path.is_file():
        raise RuntimeError(f"音频文件不存在：{audio_path}")

    headers = (
        {"Authorization": f"Bearer {settings.asr_shared_token}"}
        if settings.asr_shared_token
        else {}
    )
    media_type = (
        mimetypes.guess_type(audio_path.name)[0]
        or "application/octet-stream"
    )
    started = time.perf_counter()
    with audio_path.open("rb") as stream:
        response = requests.post(
            f"{settings.asr_base_url}/v1/transcribe",
            headers=headers,
            files={"audio": (audio_path.name, stream, media_type)},
            timeout=(10, settings.asr_request_timeout_seconds),
        )
    response.raise_for_status()
    payload = response.json()
    client_elapsed = time.perf_counter() - started
    plans = plan_script_aligned_segments(
        project.script_text,
        project.duration_seconds,
        _parse_tokens(payload),
    )

    print(
        json.dumps(
            {
                "projectId": project.id,
                "audioDurationSeconds": round(project.duration_seconds, 3),
                "clientElapsedSeconds": round(client_elapsed, 3),
                "serviceProcessingSeconds": payload.get("processingSeconds"),
                "serviceRssMb": payload.get("rssMb"),
                "cudaPeakMb": payload.get("cudaPeakMb"),
                "device": payload.get("device"),
                "model": payload.get("model"),
                "recognizedTokenCount": len(payload.get("tokens") or []),
                "segmentCount": len(plans),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print()
    for plan in plans:
        start_text = plan.script_text[:18].replace("\n", " ")
        end_text = plan.script_text[-18:].replace("\n", " ")
        print(
            f"{plan.index:02d} "
            f"{plan.start_seconds:8.3f}-{plan.end_seconds:8.3f}s "
            f"{plan.alignment_method:17s} "
            f"{start_text} ... {end_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

