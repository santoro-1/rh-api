from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import threading
import time
import zipfile
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from app.services.alignment.funasr_http import FunASRHTTPProvider
from app.services.audio import inspect_audio_duration
from app.services.media_segmentation import (
    SegmentPlan,
    cut_audio_segment,
    cut_video_segment,
    detect_silence_midpoints,
    plan_silence_segments,
)
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_ROOT = Path(__file__).resolve().parent
WORKER_VERSION = "1"


class RemoteWorkerError(RuntimeError):
    pass


def configure_logging(service_name: str) -> None:
    log_directory = NODE_ROOT / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_directory / f"{service_name}.log",
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(console)
    root.addHandler(file_handler)


def log_event(
    target_logger: logging.Logger,
    event_code: str,
    message: str,
    **details: object,
) -> None:
    suffix = (
        " "
        + json.dumps(
            {key: value for key, value in details.items() if value is not None},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if details
        else ""
    )
    target_logger.info("[EVENT %s] %s%s", event_code, message, suffix)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RemoteWorkerError(f"{name} 必须是整数") from exc


def _response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        return (response.text or f"HTTP {response.status_code}")[:1000]
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload)[:1000]
    return str(payload)[:1000]


def _metrics(
    *,
    phase: str,
    started: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "phase": phase,
        "workerVersion": WORKER_VERSION,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
    }
    if started is not None:
        value["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    try:
        import psutil

        process = psutil.Process()
        memory = process.memory_info()
        children = process.children(recursive=True)
        tree_rss = memory.rss
        live_children = 0
        for child in children:
            try:
                tree_rss += child.memory_info().rss
                live_children += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        value.update(
            {
                "processRssMb": round(memory.rss / 1024 / 1024, 1),
                "workerTreeRssMb": round(tree_rss / 1024 / 1024, 1),
                "childProcessCount": live_children,
                "systemCpuPercent": psutil.cpu_percent(interval=None),
                "systemMemoryPercent": psutil.virtual_memory().percent,
            }
        )
    except (ImportError, OSError):
        pass
    if extra:
        value.update(extra)
    return value


class RemoteMediaClient:
    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url.rstrip("/") + "/"
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _url(self, value: str) -> str:
        return urljoin(self.server_url, value.lstrip("/"))

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        response = requests.post(
            self._url("/api/media-worker/v1/jobs/claim"),
            headers=self.headers,
            json={
                "workerId": worker_id,
                "capabilities": ["analysis", "cut"],
            },
            timeout=(10, 60),
        )
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise RemoteWorkerError(
                f"领取任务失败：{_response_error(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RemoteWorkerError("领取任务响应格式错误")
        return payload

    def download(self, relative_url: str, target: Path) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            with requests.get(
                self._url(relative_url),
                headers=self.headers,
                stream=True,
                timeout=(10, 1800),
            ) as response:
                if response.status_code >= 400:
                    raise RemoteWorkerError(
                        f"下载任务素材失败：{_response_error(response)}"
                    )
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
                            written += len(chunk)
            os.replace(temporary, target)
            return written
        finally:
            temporary.unlink(missing_ok=True)

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        metrics: dict[str, Any],
    ) -> None:
        response = requests.post(
            self._url(
                f"/api/media-worker/v1/jobs/{job_id}/heartbeat"
            ),
            headers=self.headers,
            json={"leaseId": lease_id, "metrics": metrics},
            timeout=(10, 30),
        )
        if response.status_code >= 400:
            raise RemoteWorkerError(
                f"任务续租失败：{_response_error(response)}"
            )

    def complete_analysis(
        self,
        job_id: str,
        lease_id: str,
        provider: str,
        plans: tuple[SegmentPlan, ...],
        metrics: dict[str, Any],
    ) -> None:
        response = requests.post(
            self._url(f"/api/media-worker/v1/jobs/{job_id}/analysis"),
            headers=self.headers,
            json={
                "leaseId": lease_id,
                "provider": provider,
                "segments": [
                    {
                        "index": plan.index,
                        "startSeconds": plan.start_seconds,
                        "endSeconds": plan.end_seconds,
                        "scriptText": plan.script_text,
                        "alignmentMethod": plan.alignment_method,
                    }
                    for plan in plans
                ],
                "metrics": metrics,
            },
            timeout=(10, 120),
        )
        if response.status_code >= 400:
            raise RemoteWorkerError(
                f"提交分析结果失败：{_response_error(response)}"
            )

    def complete_cut(
        self,
        job_id: str,
        lease_id: str,
        archive: Path,
        metrics: dict[str, Any],
    ) -> None:
        with archive.open("rb") as stream:
            response = requests.post(
                self._url(f"/api/media-worker/v1/jobs/{job_id}/cut"),
                headers=self.headers,
                data={
                    "leaseId": lease_id,
                    "metrics": json.dumps(metrics, ensure_ascii=False),
                },
                files={
                    "archive": (
                        archive.name,
                        stream,
                        "application/zip",
                    )
                },
                timeout=(10, 3600),
            )
        if response.status_code >= 400:
            raise RemoteWorkerError(
                f"上传切割结果失败：{_response_error(response)}"
            )

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        metrics: dict[str, Any],
    ) -> None:
        response = requests.post(
            self._url(f"/api/media-worker/v1/jobs/{job_id}/failed"),
            headers=self.headers,
            json={
                "leaseId": lease_id,
                "error": error[:4000],
                "metrics": metrics,
            },
            timeout=(10, 60),
        )
        if response.status_code >= 400:
            raise RemoteWorkerError(
                f"上报任务失败状态失败：{_response_error(response)}"
            )


class HeartbeatLoop:
    def __init__(
        self,
        client: RemoteMediaClient,
        job_id: str,
        lease_id: str,
        started: float,
        interval_seconds: int,
    ) -> None:
        self.client = client
        self.job_id = job_id
        self.lease_id = lease_id
        self.started = started
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{job_id[:8]}",
            daemon=True,
        )

    def __enter__(self) -> "HeartbeatLoop":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.client.heartbeat(
                    self.job_id,
                    self.lease_id,
                    _metrics(phase="processing", started=self.started),
                )
            except Exception as exc:
                logger.warning("远程媒体任务续租暂时失败：%s", exc)


def _source_path(directory: Path, original_name: str, fallback: str) -> Path:
    suffix = Path(original_name).suffix
    if not suffix or len(suffix) > 10:
        suffix = Path(fallback).suffix
    return directory / f"{Path(fallback).stem}{suffix}"


def _plans(raw: Any) -> list[SegmentPlan]:
    if not isinstance(raw, list) or not raw:
        raise RemoteWorkerError("切割任务没有分段方案")
    result: list[SegmentPlan] = []
    for position, value in enumerate(raw, start=1):
        if not isinstance(value, dict):
            raise RemoteWorkerError(f"第 {position} 段格式错误")
        result.append(
            SegmentPlan(
                index=position,
                start_seconds=float(value["startSeconds"]),
                end_seconds=float(value["endSeconds"]),
                script_text=str(value["scriptText"]),
                alignment_method=str(
                    value.get("alignmentMethod") or "manual_review"
                ),
            )
        )
    return result


def _create_segment_archive(
    directory: Path,
    plans: list[SegmentPlan],
    audio_source: Path,
    video_source: Path | None,
) -> Path:
    output = directory / "segments.zip"
    for plan in plans:
        audio_target = (
            directory / "audio" / f"segment-{plan.index:03d}.mp3"
        )
        cut_audio_segment(
            audio_source,
            audio_target,
            start_seconds=plan.start_seconds,
            end_seconds=plan.end_seconds,
        )
        if video_source is not None:
            video_target = (
                directory / "video" / f"segment-{plan.index:03d}.mp4"
            )
            audio_duration = inspect_audio_duration(audio_target)
            cut_video_segment(
                video_source,
                video_target,
                start_seconds=plan.start_seconds,
                duration_seconds=audio_duration,
            )
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as bundle:
        kinds = [("audio", "mp3")]
        if video_source is not None:
            kinds.append(("video", "mp4"))
        for kind, extension in kinds:
            for plan in plans:
                path = (
                    directory
                    / kind
                    / f"segment-{plan.index:03d}.{extension}"
                )
                bundle.write(path, path.relative_to(directory).as_posix())
    return output


def process_job(
    client: RemoteMediaClient,
    job: dict[str, Any],
    *,
    work_root: Path,
    heartbeat_seconds: int,
) -> None:
    job_id = str(job.get("jobId") or "")
    lease_id = str(job.get("leaseId") or "")
    action = str(job.get("action") or "")
    workflow_type = str(job.get("workflowType") or "ltx_lip_sync")
    if not job_id or not lease_id or action not in {"analysis", "cut"}:
        raise RemoteWorkerError("服务器返回的任务格式错误")
    source = job.get("source")
    if not isinstance(source, dict):
        raise RemoteWorkerError("服务器没有返回素材地址")

    started = time.perf_counter()
    job_directory = work_root / job_id
    shutil.rmtree(job_directory, ignore_errors=True)
    job_directory.mkdir(parents=True, exist_ok=True)
    audio_path = _source_path(
        job_directory,
        str(source.get("audioName") or ""),
        "source.mp3",
    )
    video_path = _source_path(
        job_directory,
        str(source.get("videoName") or ""),
        "source.mp4",
    )
    try:
        with HeartbeatLoop(
            client,
            job_id,
            lease_id,
            started,
            heartbeat_seconds,
        ):
            audio_bytes = client.download(
                str(source.get("audioUrl") or ""),
                audio_path,
            )
            if action == "analysis":
                if workflow_type == "digital_human":
                    result_provider = "vad_silence"
                    result_plans = tuple(
                        plan_silence_segments(
                            float(job.get("durationSeconds") or 0),
                            detect_silence_midpoints(audio_path),
                        )
                    )
                else:
                    provider = FunASRHTTPProvider(
                        base_url=os.getenv(
                            "ASR_BASE_URL", "http://127.0.0.1:18084"
                        ),
                        shared_token=os.getenv("ASR_SHARED_TOKEN", ""),
                        timeout_seconds=_env_int(
                            "ASR_REQUEST_TIMEOUT_SECONDS", 1800
                        ),
                    )
                    result = provider.align(
                        audio_path,
                        str(job.get("scriptText") or ""),
                    )
                    result_provider = result.provider
                    result_plans = result.plans
                metrics = _metrics(
                    phase="analysis_completed",
                    started=started,
                    extra={
                        "audioDownloadMb": round(
                            audio_bytes / 1024 / 1024, 1
                        ),
                        "audioDurationSeconds": job.get(
                            "durationSeconds"
                        ),
                        "segmentCount": len(result_plans),
                    },
                )
                client.complete_analysis(
                    job_id,
                    lease_id,
                    result_provider,
                    result_plans,
                    metrics,
                )
            else:
                video_bytes = 0
                video_source = None
                if workflow_type == "ltx_lip_sync":
                    video_bytes = client.download(
                        str(source.get("videoUrl") or ""),
                        video_path,
                    )
                    video_source = video_path
                plans = _plans(job.get("segments"))
                archive = _create_segment_archive(
                    job_directory,
                    plans,
                    audio_path,
                    video_source,
                )
                metrics = _metrics(
                    phase="cut_completed",
                    started=started,
                    extra={
                        "audioDownloadMb": round(
                            audio_bytes / 1024 / 1024, 1
                        ),
                        "videoDownloadMb": round(
                            video_bytes / 1024 / 1024, 1
                        ),
                        "archiveUploadMb": round(
                            archive.stat().st_size / 1024 / 1024, 1
                        ),
                        "segmentCount": len(plans),
                    },
                )
                client.complete_cut(
                    job_id,
                    lease_id,
                    archive,
                    metrics,
                )
        log_event(
            logger,
            "remote_media.completed",
            "笔记本媒体节点已完成任务",
            job_id=job_id,
            action=action,
            metrics=metrics,
        )
    except Exception as exc:
        metrics = _metrics(
            phase="failed",
            started=started,
            extra={"action": action},
        )
        try:
            client.fail(job_id, lease_id, str(exc), metrics)
        except Exception as report_exc:
            logger.warning("无法向服务器上报失败状态：%s", report_exc)
        raise
    finally:
        shutil.rmtree(job_directory, ignore_errors=True)


def run() -> int:
    configure_logging("remote_media_worker")
    server_url = os.getenv("MEDIA_WORKER_SERVER_URL", "").strip()
    token = os.getenv("MEDIA_WORKER_TOKEN", "").strip()
    if not server_url:
        raise RemoteWorkerError("media_node/.env 缺少 MEDIA_WORKER_SERVER_URL")
    if len(token) < 32:
        raise RemoteWorkerError(
            "media_node/.env 中的 MEDIA_WORKER_TOKEN 至少需要 32 个字符"
        )
    worker_id = os.getenv("MEDIA_WORKER_ID", "").strip()
    if not worker_id:
        worker_id = f"{socket.gethostname()}-media"
    poll_seconds = max(_env_int("MEDIA_WORKER_POLL_SECONDS", 10), 2)
    heartbeat_seconds = max(
        _env_int("MEDIA_WORKER_HEARTBEAT_SECONDS", 60), 10
    )
    configured_work_root = Path(
        os.getenv("MEDIA_WORKER_WORK_DIR", "./data/remote-worker")
    )
    work_root = (
        configured_work_root
        if configured_work_root.is_absolute()
        else NODE_ROOT / configured_work_root
    ).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    client = RemoteMediaClient(server_url, token)
    log_event(
        logger,
        "remote_media.started",
        "远程媒体节点已启动",
        worker_id=worker_id,
        server_url=server_url,
    )
    while True:
        try:
            job = client.claim(worker_id)
            if job is None:
                time.sleep(poll_seconds)
                continue
            process_job(
                client,
                job,
                work_root=work_root,
                heartbeat_seconds=heartbeat_seconds,
            )
        except KeyboardInterrupt:
            return 0
        except Exception:
            logger.exception("远程媒体节点任务失败")
            time.sleep(poll_seconds)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
