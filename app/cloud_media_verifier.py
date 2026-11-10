"""One-shot FC worker: claim metadata, verify private OSS object, return receipt.

The image must contain FFprobe and oss2. Invoke this module once per cloud task;
it never receives an object URL or long-lived OSS key from the website.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import oss2
import requests


CHUNK = 1024 * 1024


class _FcCredentials(oss2.credentials.CredentialsProvider):
    def get_credentials(self):
        return oss2.credentials.Credentials(
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            os.environ["ALIBABA_CLOUD_SECURITY_TOKEN"],
        )


def _detected_kind(path: Path) -> str | None:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if probe.returncode:
        return None
    streams = json.loads(probe.stdout).get("streams", [])
    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    with path.open("rb") as source:
        header = source.read(16)
    is_image = (
        header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )
    if is_image and videos and not has_audio:
        kind = "image"
    elif videos:
        kind = "video"
    elif has_audio:
        kind = "audio"
    else:
        return None
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"],
        capture_output=True, timeout=600, check=False,
    )
    return kind if decoded.returncode == 0 else None


def verify_object(job: dict[str, object], bucket: oss2.Bucket, scratch_limit: int,
                  heartbeat=None) -> dict[str, object]:
    expected_size = int(job["size_bytes"])
    if expected_size > scratch_limit:
        return {"outcome": "retryable_error", "error_code": "WORKER_ERROR"}
    key = str(job["object_key"])
    if not key.startswith("media/users/") or not key.endswith("/source"):
        return {"outcome": "invalid", "error_code": "OBJECT_MISSING"}
    with tempfile.TemporaryDirectory(prefix="media-verify-") as temp_dir:
        path = Path(temp_dir) / "source"
        digest = hashlib.sha256()
        size = 0
        last_heartbeat = time.monotonic()
        response = bucket.get_object(key)
        with path.open("wb") as target:
            while chunk := response.read(CHUNK):
                size += len(chunk)
                if size > expected_size or size > scratch_limit:
                    return {"outcome": "invalid", "error_code": "SIZE_MISMATCH"}
                digest.update(chunk)
                target.write(chunk)
                if heartbeat and time.monotonic() - last_heartbeat > 60:
                    heartbeat()
                    last_heartbeat = time.monotonic()
        if size != expected_size:
            return {"outcome": "invalid", "error_code": "SIZE_MISMATCH"}
        actual_sha = digest.hexdigest()
        if actual_sha != job["expected_sha256"]:
            return {"outcome": "invalid", "error_code": "SHA_MISMATCH"}
        kind = _detected_kind(path)
        if kind != job["kind"]:
            return {"outcome": "invalid", "error_code": "FORMAT_INVALID"}
        return {
            "outcome": "verified", "observed_size": size,
            "observed_sha256": actual_sha, "detected_kind": kind,
        }


def run_once() -> bool:
    base = os.environ["MEDIA_CLOUD_API_BASE"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("云端回调地址必须使用 HTTPS")
    token = os.environ["MEDIA_CLOUD_WORKER_TOKEN"]
    if len(token) < 32:
        raise ValueError("云端核验令牌未配置")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    claim = session.post(f"{base}/api/cloud-media/jobs/claim", timeout=15)
    claim.raise_for_status()
    job = claim.json()["job"]
    if job is None:
        return False
    job_id, lease_id = job["job_id"], job["lease_id"]
    callback = f"{base}/api/cloud-media/jobs/{job_id}"

    def heartbeat() -> None:
        reply = session.post(f"{callback}/heartbeat", json={"lease_id": lease_id}, timeout=15)
        reply.raise_for_status()

    try:
        auth = oss2.ProviderAuthV4(_FcCredentials())
        bucket = oss2.Bucket(
            auth, os.environ["MEDIA_OSS_INTERNAL_ENDPOINT"],
            os.environ["MEDIA_OSS_BUCKET"], region="cn-hangzhou",
        )
        limit = int(os.environ.get("MEDIA_CLOUD_SCRATCH_LIMIT_MB", "1024")) * 1024 * 1024
        result = verify_object(job, bucket, limit, heartbeat)
    except (oss2.exceptions.NoSuchKey,):
        result = {"outcome": "invalid", "error_code": "OBJECT_MISSING"}
    except Exception:
        # The traceback may contain request/auth details. Let the bounded lease
        # retry without writing secrets or object bytes to stdout/stderr.
        result = {"outcome": "retryable_error", "error_code": "WORKER_ERROR"}
    reply = session.post(
        f"{callback}/complete", json={"lease_id": lease_id, **result}, timeout=15,
    )
    reply.raise_for_status()
    return True


if __name__ == "__main__":
    run_once()
