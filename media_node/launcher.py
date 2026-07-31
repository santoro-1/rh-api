from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests


NODE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = NODE_ROOT.parent
WORKER_ENV = NODE_ROOT / ".env"
LEGACY_WORKER_ENV = PROJECT_ROOT / ".env.worker"


def _load_worker_env() -> None:
    environment_file = (
        WORKER_ENV if WORKER_ENV.is_file() else LEGACY_WORKER_ENV
    )
    if not environment_file.is_file():
        raise RuntimeError(
            "缺少 media_node/.env，请复制 media_node/.env.example "
            "后填写服务器地址和令牌"
        )
    if environment_file == LEGACY_WORKER_ENV:
        print("检测到根目录旧版 .env.worker，本次兼容读取；建议迁移到 media_node/.env。")
    for raw_line in environment_file.read_text(encoding="utf-8").splitlines():
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
    runtime_root = NODE_ROOT / ".runtime"
    python = runtime_root / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        legacy_runtime = PROJECT_ROOT / ".asr-runtime"
        legacy_python = legacy_runtime / "venv" / "Scripts" / "python.exe"
        if legacy_python.is_file():
            runtime_root = legacy_runtime
            python = legacy_python
    if not python.is_file():
        raise RuntimeError(
            "缺少媒体节点运行环境，请先执行 media_node/安装媒体节点.ps1"
        )
    environment = os.environ.copy()
    environment.setdefault(
        "MODELSCOPE_CACHE",
        str(runtime_root / "models"),
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
            "media_node.asr_service.app:app",
            "--host",
            os.getenv("ASR_HOST", "127.0.0.1"),
            "--port",
            os.getenv("ASR_PORT", "18084"),
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
        from media_node.worker import run

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
