from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_ENV = PROJECT_ROOT / ".env.worker"


def _load_worker_env() -> None:
    if not WORKER_ENV.is_file():
        raise RuntimeError(
            "缺少 .env.worker，请复制 .env.worker.example 后填写服务器地址和令牌"
        )
    for raw_line in WORKER_ENV.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


def _asr_healthy() -> bool:
    try:
        response = requests.get(
            os.getenv(
                "ASR_BASE_URL", "http://127.0.0.1:18084"
            ).rstrip("/")
            + "/healthz",
            timeout=2,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _start_asr() -> subprocess.Popen[bytes] | None:
    if _asr_healthy():
        print("检测到本机 ASR 已运行，将直接复用。")
        return None
    python = PROJECT_ROOT / ".asr-runtime" / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError(
            "缺少 .asr-runtime ASR 环境，请先按 asr_service/README.md 安装"
        )
    environment = os.environ.copy()
    environment.setdefault(
        "MODELSCOPE_CACHE",
        str(PROJECT_ROOT / ".asr-runtime" / "models"),
    )
    environment.setdefault("ASR_MODEL", "paraformer-zh")
    environment.setdefault("ASR_VAD_MODEL", "fsmn-vad")
    environment.setdefault("ASR_DEVICE", "cpu")
    for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment.pop(proxy_name, None)
    process = subprocess.Popen(
        [
            str(python),
            "-m",
            "uvicorn",
            "asr_service.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18084",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
    )
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError("ASR 服务启动失败，请查看上方输出")
        if _asr_healthy():
            print("本机 ASR 已就绪。")
            return process
        time.sleep(1)
    process.terminate()
    raise RuntimeError("ASR 服务 30 秒内没有就绪")


def main() -> int:
    _load_worker_env()
    asr_process = _start_asr()
    try:
        from scripts.remote_media_worker import run

        return run()
    finally:
        if asr_process is not None and asr_process.poll() is None:
            asr_process.terminate()
            try:
                asr_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                asr_process.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"远程媒体节点启动失败：{exc}", file=sys.stderr)
        input("按回车键关闭窗口……")
        raise SystemExit(1)
