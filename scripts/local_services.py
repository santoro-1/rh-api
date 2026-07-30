from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from app.config import get_settings
from app.services.logging_config import (
    configure_logging,
    log_event,
    write_heartbeat,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = get_settings().runtime_dir / "local-services.json"
STOP_FILE = get_settings().runtime_dir / "local-services.stop.json"
logger = logging.getLogger(__name__)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a reliable existence check on Windows.
        # Query the process handle without requesting termination rights.
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _existing_supervisor() -> dict[str, object]:
    state = _read_json(STATE_FILE)
    heartbeat = _read_json(
        get_settings().runtime_dir / "launcher.heartbeat.json"
    )
    try:
        pid = int(state["supervisorPid"])
    except (KeyError, TypeError, ValueError):
        return {}
    if (
        _process_alive(pid)
        and state.get("token")
        and state.get("token") == heartbeat.get("token")
    ):
        return state
    return {}


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _spawn_service(
    name: str,
    executable: str,
    args: list[str],
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], object]:
    get_settings().logs_dir.mkdir(parents=True, exist_ok=True)
    stream: object = subprocess.DEVNULL
    process = subprocess.Popen(
        [executable, *args],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=subprocess.STDOUT,
        env=environment,
        creationflags=_creation_flags(),
    )
    return process, stream


def _local_asr_command() -> tuple[str, list[str], dict[str, str]] | None:
    if not _port_available(18084):
        log_event(
            logger,
            "system.asr_already_running",
            "检测到本地 ASR 端口已在监听，本次不重复启动",
        )
        return None
    python = PROJECT_ROOT / ".asr-runtime" / "venv" / "Scripts" / "python.exe"
    if not python.is_file():
        return None
    try:
        ready = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util,sys;"
                    "names=('funasr','psutil','uvicorn');"
                    "sys.exit(0 if all(importlib.util.find_spec(name) "
                    "for name in names) else 1)"
                ),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        log_event(
            logger,
            "system.asr_probe_failed",
            "ASR 环境检查超时或无法执行；本次不启动 ASR 服务",
            level=logging.WARNING,
        )
        return None
    if ready.returncode != 0:
        log_event(
            logger,
            "system.asr_not_ready",
            "检测到 ASR 环境，但依赖尚未安装完成；本次不启动 ASR 服务",
            level=logging.WARNING,
        )
        return None
    environment = os.environ.copy()
    for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment.pop(proxy_name, None)
    environment.setdefault(
        "MODELSCOPE_CACHE",
        str(PROJECT_ROOT / ".asr-runtime" / "models"),
    )
    environment.setdefault("ASR_MODEL", "paraformer-zh")
    environment.setdefault("ASR_VAD_MODEL", "fsmn-vad")
    environment.setdefault("ASR_DEVICE", "cpu")
    return (
        str(python),
        [
            "-m",
            "uvicorn",
            "asr_service.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18084",
        ],
        environment,
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _run_migrations() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_creation_flags(),
        timeout=120,
        check=False,
    )
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if output:
        logger.info("数据库迁移输出：%s", output)
    if result.returncode != 0:
        raise RuntimeError("数据库迁移失败，请查看 launcher.log")
    log_event(
        logger,
        "system.migration_completed",
        "数据库结构检查完成",
    )


def run_supervisor() -> int:
    configure_logging("launcher", console=False)
    existing = _existing_supervisor()
    if existing:
        log_event(
            logger,
            "system.already_running",
            "系统已经运行，正在打开网页",
            supervisor_pid=existing.get("supervisorPid"),
        )
        webbrowser.open("http://127.0.0.1:8000/generate/batch")
        return 0
    if not _port_available(8000):
        log_event(
            logger,
            "system.start_failed",
            "端口 8000 已被其他程序占用，系统未启动",
            level=logging.ERROR,
            port=8000,
        )
        return 1

    # The token ties the stop file, state file and launcher heartbeat to this
    # exact supervisor process, preventing a stale stop request from killing a
    # later startup.
    token = uuid.uuid4().hex
    _run_migrations()
    commands: dict[str, tuple[str, list[str], dict[str, str] | None]] = {
        "web": (sys.executable, ["-m", "scripts.serve_web"], None),
        "audio_worker": (
            sys.executable,
            ["-m", "app.workers.audio_worker"],
            None,
        ),
        "media_worker": (
            sys.executable,
            ["-m", "app.workers.media_worker"],
            None,
        ),
        "video_worker": (
            sys.executable,
            ["-m", "app.workers.task_worker"],
            None,
        ),
    }
    if asr_command := _local_asr_command():
        commands["asr_service"] = asr_command
    children: dict[str, subprocess.Popen[bytes]] = {}
    streams: dict[str, object] = {}
    try:
        for name, command in commands.items():
            children[name], streams[name] = _spawn_service(name, *command)
            log_event(
                logger,
                "system.service_started",
                "本地服务进程已启动",
                service=name,
                pid=children[name].pid,
            )
        _write_json(
            STATE_FILE,
            {
                "supervisorPid": os.getpid(),
                "token": token,
                "children": {name: process.pid for name, process in children.items()},
            },
        )
        STOP_FILE.unlink(missing_ok=True)
        for _ in range(40):
            if not _port_available(8000):
                webbrowser.open("http://127.0.0.1:8000/generate/batch")
                break
            time.sleep(0.25)

        while True:
            write_heartbeat(
                "launcher",
                token=token,
                children={name: process.pid for name, process in children.items()},
            )
            stop_request = _read_json(STOP_FILE)
            if stop_request.get("token") == token:
                log_event(
                    logger,
                    "system.stop_requested",
                    "收到停止系统请求，正在安全收尾",
                )
                break
            for name, process in list(children.items()):
                if process.poll() is None:
                    continue
                log_event(
                    logger,
                    "system.service_restarting",
                    "子服务异常退出，正在自动重启",
                    level=logging.ERROR,
                    service=name,
                    exit_code=process.returncode,
                )
                stream = streams.pop(name, None)
                if hasattr(stream, "close"):
                    stream.close()
                children[name], streams[name] = _spawn_service(
                    name,
                    *commands[name],
                )
                _write_json(
                    STATE_FILE,
                    {
                        "supervisorPid": os.getpid(),
                        "token": token,
                        "children": {
                            child_name: child.pid
                            for child_name, child in children.items()
                        },
                    },
                )
            time.sleep(2)
    finally:
        for process in children.values():
            _terminate(process)
        for stream in streams.values():
            if hasattr(stream, "close"):
                stream.close()
        STATE_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        (get_settings().runtime_dir / "launcher.heartbeat.json").unlink(
            missing_ok=True
        )
        log_event(
            logger,
            "system.stopped",
            "本地服务已全部停止",
        )
    return 0


def request_stop() -> int:
    state = _existing_supervisor()
    if not state:
        print("本地服务当前没有运行。")
        return 0
    _write_json(STOP_FILE, {"token": state["token"]})
    pid = int(state["supervisorPid"])
    for _ in range(50):
        if not _process_alive(pid):
            print("本地服务已停止。")
            return 0
        time.sleep(0.2)
    print("已发送停止请求，服务仍在收尾，请稍后查看运行状态。")
    return 0


def show_status() -> int:
    state = _existing_supervisor()
    print("本地服务正在运行。" if state else "本地服务没有运行。")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "run":
        return run_supervisor()
    if command == "stop":
        return request_stop()
    if command == "status":
        return show_status()
    print("用法：python -m scripts.local_services [run|stop|status]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
