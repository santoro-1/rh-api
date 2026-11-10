from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen
import webbrowser


SOURCE_ROOT = Path(__file__).resolve().parents[2]
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(SOURCE_ROOT / "src"))


def _application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def _load_processor_config(data_root: Path) -> dict[str, str]:
    path = data_root / "processor_config.json"
    if not path.is_file():
        return {}
    try:
        # PowerShell 5 may write JSON with an UTF-8 BOM when preparing a
        # deployment-specific package. utf-8-sig accepts both BOM and no-BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in data.items() if value not in (None, "")}


def _resolved_network_config(config: dict[str, str]) -> tuple[str, str, str]:
    cloud_digital_human_url = "https://video.lanyingjk01.com"
    local_digital_human_url = "http://127.0.0.1:8000"
    legacy_account_url = "http://192.168.11.28:8000"
    legacy_cloud_auth_url = "https://auth.lanyingjk01.com"
    explicit_digital_human_url = config.get("digital_human_server_url", "").strip()
    configured_auth_url = (
        explicit_digital_human_url
        or config.get("auth_server_url", cloud_digital_human_url).strip()
    ).rstrip("/")
    shared_processor_url = config.get("shared_processor_url", "").strip().rstrip("/")
    if configured_auth_url in {
        local_digital_human_url,
        legacy_account_url,
        legacy_cloud_auth_url,
    }:
        configured_auth_url = cloud_digital_human_url
    if shared_processor_url == legacy_account_url:
        shared_processor_url = ""
    return configured_auth_url or cloud_digital_human_url, shared_processor_url, "false"


def _detect_draft_root(config: dict[str, str], data_root: Path) -> Path:
    configured = os.environ.get("JYD_WEB_DRAFT_ROOT", "").strip() or config.get("draft_root", "").strip()
    fallback = data_root / "drafts"
    fallback.mkdir(parents=True, exist_ok=True)
    from jyd_probe.runtime_paths import detect_jianying_draft_root

    return detect_jianying_draft_root(configured, fallback=fallback)


def _configure_environment() -> tuple[Path, Path]:
    app_root = _application_root()
    data_root = app_root / "data"
    libraries_root = data_root / "libraries"
    legacy_text_template_root = libraries_root / "text_template_library"
    compact_text_template_root = data_root / "l" / "t"
    text_template_root = (
        legacy_text_template_root
        if legacy_text_template_root.exists() or not getattr(sys, "frozen", False)
        else compact_text_template_root
    )
    storage_root = data_root / "web_storage"
    personal_root = data_root / "personal_libraries"
    config = _load_processor_config(data_root)
    auth_server_url, shared_processor_url, auth_authority = _resolved_network_config(config)

    defaults = {
        "JYD_WEB_STORAGE_ROOT": storage_root,
        "JYD_TEMPLATE_LIBRARY_ROOT": data_root / "template_library",
        "JYD_AUDIO_LIBRARY_ROOT": libraries_root / "audio_library",
        "JYD_EFFECT_LIBRARY_ROOT": libraries_root / "effect_library",
        "JYD_FONT_LIBRARY_ROOT": libraries_root / "font_library",
        "JYD_STICKER_LIBRARY_ROOT": libraries_root / "sticker_library",
        "JYD_CORNER_STICKER_LIBRARY_ROOT": libraries_root / "corner_sticker_library",
        "JYD_SEMANTIC_VISUAL_LIBRARY_ROOT": libraries_root / "semantic_visual_library",
        "JYD_TEXT_EFFECT_LIBRARY_ROOT": libraries_root / "text_effect_library",
        "JYD_TEXT_STYLE_LIBRARY_ROOT": libraries_root / "text_style_library",
        "JYD_TEXT_TEMPLATE_LIBRARY_ROOT": text_template_root,
        "JYD_PERSONAL_LIBRARY_ROOT": personal_root,
        "JYD_DECRYPT_WORK_ROOT": data_root / "decrypted",
        "JYD_DRAFTC_EXE": app_root / "tools" / "jy-draftc.exe",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, str(Path(value).resolve()))
    os.environ.setdefault("JYD_WEB_DRAFT_ROOT", str(_detect_draft_root(config, data_root)))
    os.environ.setdefault(
        "JYD_AUTH_SERVER_URL",
        auth_server_url,
    )
    os.environ.setdefault(
        "JYD_SHARED_PROCESSOR_URL",
        shared_processor_url,
    )
    workbench_environment = (
        "local"
        if urlparse(auth_server_url).hostname in {"127.0.0.1", "localhost", "::1"}
        else "production"
    )
    os.environ.setdefault(
        "JYD_ADMIN_COOKIE_NAME",
        f"jyd_admin_session_{workbench_environment}",
    )
    os.environ.setdefault(
        "JYD_SITE_COOKIE_NAME",
        f"jyd_site_session_{workbench_environment}",
    )
    os.environ.setdefault(
        "JYD_LTX_WORKBENCH_URL",
        config.get("ltx_workbench_url", "").strip()
        or (
            "http://127.0.0.1:8792"
            if workbench_environment == "local"
            else "http://127.0.0.1:8791"
        ),
    )
    os.environ.setdefault(
        "JYD_AUTH_AUTHORITY",
        auth_authority,
    )
    os.environ.setdefault(
        "JYD_ASR_BASE_URL",
        config.get("asr_base_url", "").strip() or "http://127.0.0.1:18084",
    )
    os.environ.setdefault(
        "JYD_ASR_REQUIRED",
        config.get("asr_required", "").strip() or "true",
    )
    storage_root.mkdir(parents=True, exist_ok=True)
    for name in (
        "audio_library",
        "effect_library",
        "font_library",
        "sticker_library",
        "corner_sticker_library",
        "text_effect_library",
        "text_style_library",
        "text_template_library",
    ):
        (personal_root / name).mkdir(parents=True, exist_ok=True)
    (data_root / "logs").mkdir(parents=True, exist_ok=True)
    return app_root, data_root


def _workspace_path(deployment_mode: str) -> str:
    return "/app/new" if deployment_mode == "standalone" else "/app"


def _lan_addresses(port: int, workspace_path: str = "/app") -> list[str]:
    addresses: list[str] = []
    try:
        candidates = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        candidates = []
    for address in candidates:
        if address.startswith(("127.", "169.254.")) or ":" in address:
            continue
        url = f"http://{address}:{port}{workspace_path}"
        if url not in addresses:
            addresses.append(url)
    return addresses


def _append_startup_log(data_root: Path, message: str) -> None:
    logging.getLogger("jyd_probe.server").info(message)


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _asr_is_healthy(base_url: str) -> bool:
    try:
        with urlopen(base_url.rstrip("/") + "/healthz", timeout=2) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _asr_runtime_layout(app_root: Path, config: dict[str, str]) -> tuple[Path, Path, Path] | None:
    configured = (
        os.environ.get("JYD_ASR_RUNTIME_ROOT", "").strip()
        or config.get("asr_runtime_root", "").strip()
    )
    candidates: list[Path] = []
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = app_root / candidate
        candidates.append(candidate.resolve())
    candidates.append((app_root / "asr_runtime").resolve())
    if not getattr(sys, "frozen", False) and len(SOURCE_ROOT.parents) >= 2:
        candidates.append(
            (SOURCE_ROOT.parents[1] / "数字人" / "runninghub_mvp").resolve()
        )

    for root in candidates:
        portable_python = root / "python" / "python.exe"
        if portable_python.is_file() and (root / "media_node" / "asr_service" / "app.py").is_file():
            return portable_python, root, root / "media_node" / ".runtime" / "models"
        development_python = root / "media_node" / ".runtime" / "venv" / "Scripts" / "python.exe"
        if development_python.is_file() and (root / "media_node" / "asr_service" / "app.py").is_file():
            return development_python, root, root / "media_node" / ".runtime" / "models"
    return None


def _start_embedded_asr(
    app_root: Path,
    data_root: Path,
    config: dict[str, str],
) -> subprocess.Popen[bytes] | None:
    base_url = os.environ.get("JYD_ASR_BASE_URL", "http://127.0.0.1:18084").rstrip("/")
    if _asr_is_healthy(base_url):
        print("本地 ASR: 已运行，直接复用")
        return None
    if os.environ.get("JYD_ASR_REQUIRED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return None
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        print(f"本地 ASR: 使用外部地址 {base_url}，不由工作台启动")
        return None
    layout = _asr_runtime_layout(app_root, config)
    if layout is None:
        print("本地 ASR: 未找到内置运行时；生成精确字幕时会明确报错")
        return None
    python, module_root, models_root = layout
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(module_root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment.setdefault("MODELSCOPE_CACHE", str(models_root))
    environment.setdefault("ASR_MODEL", "paraformer-zh")
    environment.setdefault("ASR_VAD_MODEL", "fsmn-vad")
    environment.setdefault("ASR_DEVICE", "cpu")
    for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment.pop(proxy_name, None)
    port = parsed.port or 18084
    log_path = data_root / "logs" / "asr.log"
    log_handle = log_path.open("ab")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            [
                str(python),
                "-s",
                "-m",
                "uvicorn",
                "media_node.asr_service.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=module_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
    finally:
        log_handle.close()
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError(f"内置 ASR 启动失败，请查看 {log_path}")
        if _asr_is_healthy(base_url):
            print("本地 ASR: CPU 轻量模型已就绪")
            return process
        time.sleep(1)
    process.terminate()
    raise RuntimeError(f"内置 ASR 30 秒内未就绪，请查看 {log_path}")


def _start_embedded_collector(port: int = 8765) -> bool:
    if _port_is_open("127.0.0.1", port):
        return False
    from jyd_probe.local_collector_api import create_local_collector_app
    from jyd_probe.logging_config import configure_file_logging
    import uvicorn

    configure_file_logging(
        _application_root() / "data" / "logs",
        "collector.log",
        logger_name="jyd_probe.collector",
        propagate=False,
    )

    collector_server = uvicorn.Server(
        uvicorn.Config(
            create_local_collector_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    threading.Thread(
        target=collector_server.run,
        name="jyd-embedded-collector",
        daemon=True,
    ).start()
    return True


def _write_shared_connection_files(
    app_root: Path,
    lan_urls: list[str],
    agent_token: str,
) -> None:
    if not lan_urls:
        return
    primary_url = lan_urls[0]
    shortcut = "[InternetShortcut]\n" f"URL={primary_url}\n"
    (app_root / "打开公用工作台.url").write_text(shortcut, encoding="utf-8-sig")

    lines = [
        "公用工作台已经启动。",
        "",
        "给普通使用者：",
        "把同目录下的‘打开公用工作台.url’发给对方，双击即可打开。",
        "如果快捷文件打不开，也可以把下面任意一个地址发给对方：",
        *lan_urls,
        "",
        "增加剪映处理电脑：",
        "打开 JianyingRenderAgent，在‘工作台地址’中填写上面的地址，",
        f"在‘接入密码’中填写：{agent_token}",
        "每台处理电脑需要安装剪映，并保持电脑不锁屏。",
        "",
        "本文件会在每次启动时自动更新。",
    ]
    (app_root / "公用工作台连接说明.txt").write_text("\n".join(lines), encoding="utf-8-sig")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动剪映处理机服务")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--render-job",
        default="",
        help="只执行指定的 jyd.render_job.v1 文件，完成后退出",
    )
    parser.add_argument("--execution-mode", choices=("embedded", "agent"), default="embedded")
    parser.add_argument(
        "--deployment-mode",
        choices=("standalone", "shared"),
        default="",
        help="standalone 仅本机使用；shared 允许局域网员工连接",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    startup_config = _load_processor_config(_application_root() / "data")
    deployment_mode = args.deployment_mode or startup_config.get("deployment_mode", "standalone")
    if deployment_mode not in {"standalone", "shared"}:
        deployment_mode = "standalone"
    host = args.host or startup_config.get("host", "") or (
        "127.0.0.1" if deployment_mode == "standalone" else "0.0.0.0"
    )
    os.environ["JYD_EXECUTION_MODE"] = args.execution_mode
    # Request-level checks still restrict local file operations to loopback clients.
    os.environ["JYD_ALLOW_LOCAL_FILE_ACCESS"] = "true"
    from jyd_probe.logging_config import configure_file_logging

    app_root = _application_root()
    data_root = app_root / "data"
    configure_file_logging(
        data_root / "logs",
        "server.log",
        logger_name="jyd_probe.server",
        propagate=False,
    )
    asr_process: subprocess.Popen[bytes] | None = None
    try:
        app_root, data_root = _configure_environment()
        if args.render_job:
            configure_file_logging(
                data_root / "logs",
                "render.log",
                logger_name="jyd_probe.render",
                propagate=False,
            )
            from jyd_probe.render_job import run_render_job_file

            result = run_render_job_file(args.render_job)
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0
        asr_process = _start_embedded_asr(app_root, data_root, startup_config)
        configure_file_logging(data_root / "logs", "workbench.log")
        configure_file_logging(
            data_root / "logs",
            "render.log",
            logger_name="jyd_probe.render",
            propagate=False,
        )
        from jyd_probe.template_library import rebase_template_library_paths

        rebase_stats = rebase_template_library_paths(os.environ["JYD_TEMPLATE_LIBRARY_ROOT"])
        from jyd_probe.web_api import create_app

        app = create_app()
        collector_started = _start_embedded_collector()
        workspace_path = _workspace_path(deployment_mode)
        local_url = f"http://127.0.0.1:{args.port}{workspace_path}"
        lan_urls = _lan_addresses(args.port, workspace_path)
        if deployment_mode == "shared":
            _write_shared_connection_files(app_root, lan_urls, str(app.state.agent_token))
        print("=" * 68)
        print("剪映处理机已启动。关闭此窗口会停止服务。")
        print(f"本机地址: {local_url}")
        for url in lan_urls:
            print(f"局域网地址: {url}")
        print(f"内部访问密码文件: {os.environ['JYD_WEB_STORAGE_ROOT']}\\access_password.txt")
        print(f"管理员密码文件: {os.environ['JYD_WEB_STORAGE_ROOT']}\\admin_password.txt")
        print(f"处理机令牌文件: {os.environ['JYD_WEB_STORAGE_ROOT']}\\agent_token.txt")
        print(f"执行模式: {args.execution_mode}")
        print(f"使用模式: {deployment_mode}")
        print(f"数字人网站账号与任务源: {os.environ['JYD_AUTH_SERVER_URL']}")
        if os.environ.get("JYD_SHARED_PROCESSOR_URL"):
            print(f"其他工作台: {os.environ['JYD_SHARED_PROCESSOR_URL']}")
        if deployment_mode == "shared" and lan_urls:
            print("连接文件已生成: 打开公用工作台.url")
            print("添加处理电脑说明: 公用工作台连接说明.txt")
        print(f"本机是否为账号中心: {os.environ['JYD_AUTH_AUTHORITY']}")
        print(f"剪映草稿目录: {os.environ['JYD_WEB_DRAFT_ROOT']}")
        print(f"精确字幕 ASR: {os.environ['JYD_ASR_BASE_URL']}")
        print("草稿采集工具: 已集成启动" if collector_started else "草稿采集工具: 已有程序在运行")
        print("=" * 68)
        _append_startup_log(
            data_root,
            f"host={host} port={args.port} deployment={deployment_mode} lan_urls={lan_urls} rebase={rebase_stats}",
        )
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(local_url)).start()

        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=args.port, log_config=None)
        )

        def request_graceful_shutdown() -> None:
            app.state.runtime_control.shutdown_requested.wait()
            server.should_exit = True

        threading.Thread(
            target=request_graceful_shutdown,
            name="jyd-managed-shutdown",
            daemon=True,
        ).start()
        server.run()
        return 0
    except BaseException as exc:
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _append_startup_log(data_root, details)
        print(details, file=sys.stderr)
        return 1
    finally:
        if asr_process is not None and asr_process.poll() is None:
            asr_process.terminate()
            try:
                asr_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                asr_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
