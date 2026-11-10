from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
import uuid
import zipfile

from .draft_crypto import is_plain_json_file, prepare_plain_draft_dir
from .draft_import_analyzer import DEFAULT_HASH_LIMIT_BYTES, analyze_draft_import
from .draft_upload_plan import build_draft_upload_plan
from .draft_transfer import build_transfer_package
from .audio_export import export_audio_library
from .effect_export import export_effect_library
from .font_export import export_font_library
from .sticker_export import export_sticker_library
from .text_effect_export import export_text_effect_library
from .text_template_export import export_text_template_library
from .runtime_paths import (
    collector_state_root,
    detect_jianying_draft_root,
    is_frozen,
    libraries_root,
)
from .logging_config import log_event


DEFAULT_RENDER_SERVER_URL = "http://127.0.0.1:8010"
logger = logging.getLogger("jyd_probe.collector")


@dataclass
class LocalCollectorSettings:
    draft_root: Path
    state_root: Path
    workspace_root: Path
    decrypt_work_root: Path
    font_library_root: Path
    personal_library_root: Path | None = None
    render_server_url: str = DEFAULT_RENDER_SERVER_URL
    access_token: str = "operator123"
    draft_root_mode: str = "auto"

    @classmethod
    def defaults(cls) -> "LocalCollectorSettings":
        state_root = collector_state_root()
        configured_root = os.environ.get("JYD_LOCAL_DRAFT_ROOT", "").strip()
        draft_root = (
            Path(configured_root).expanduser().resolve()
            if configured_root
            else detect_default_draft_root()
        )
        return cls(
            draft_root=draft_root,
            state_root=state_root,
            workspace_root=Path(
                os.environ.get(
                    "JYD_COLLECTOR_WORKSPACE_ROOT",
                    state_root if is_frozen() else libraries_root(),
                )
            ).expanduser().resolve(),
            decrypt_work_root=Path(
                os.environ.get("JYD_COLLECTOR_DECRYPT_ROOT", state_root / "decrypted")
            ).expanduser().resolve(),
            font_library_root=Path(
                os.environ.get(
                    "JYD_FONT_LIBRARY_ROOT",
                    state_root / "font_library" if is_frozen() else libraries_root() / "font_library",
                )
            ).expanduser().resolve(),
            personal_library_root=Path(
                os.environ.get("JYD_PERSONAL_LIBRARY_ROOT", state_root / "personal_libraries")
            ).expanduser().resolve(),
            render_server_url=os.environ.get(
                "JYD_RENDER_SERVER_URL", DEFAULT_RENDER_SERVER_URL
            ).strip(),
            access_token=os.environ.get("JYD_ACCESS_TOKEN", "operator123").strip()
            or "operator123",
            draft_root_mode="manual" if configured_root else "auto",
        )


class LocalCollectorService:
    def __init__(self, settings: LocalCollectorSettings | None = None):
        self.settings = settings or LocalCollectorSettings.defaults()
        self.settings.state_root.mkdir(parents=True, exist_ok=True)
        self.settings.decrypt_work_root.mkdir(parents=True, exist_ok=True)
        if self.settings.personal_library_root is None:
            self.settings.personal_library_root = self.settings.state_root / "personal_libraries"
        self.settings.personal_library_root.mkdir(parents=True, exist_ok=True)
        self._load_saved_config()
        log_event(
            logger,
            "collector.initialized",
            "草稿采集器已初始化",
            component="collector",
            draft_root_mode=self.settings.draft_root_mode,
        )

    def get_config(self) -> dict[str, Any]:
        return {
            "draft_root": str(self.settings.draft_root),
            "draft_root_exists": self.settings.draft_root.is_dir(),
            "draft_root_mode": self.settings.draft_root_mode,
            "workspace_root": str(self.settings.workspace_root),
            "state_root": str(self.settings.state_root),
            "font_library_root": str(self.settings.font_library_root),
            "personal_library_root": str(self.settings.personal_library_root),
            "render_server_url": self.settings.render_server_url,
            "access_token_configured": bool(self.settings.access_token),
        }

    def set_draft_root(self, value: str | Path) -> dict[str, Any]:
        root = Path(value).expanduser().resolve()
        if not root.exists():
            raise FileNotFoundError(f"剪映草稿目录不存在: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"剪映草稿路径不是目录: {root}")
        self.settings.draft_root = root
        self.settings.draft_root_mode = "manual"
        self._save_config()
        log_event(
            logger,
            "collector.draft_root_updated",
            "剪映草稿目录已更新",
            component="collector",
            draft_root_mode="manual",
        )
        return self.get_config()

    def set_render_server_url(self, value: str) -> dict[str, Any]:
        self.settings.render_server_url = self._normalize_server_url(value)
        self._save_config()
        return self.get_config()

    def set_access_token(self, value: str) -> dict[str, Any]:
        self.settings.access_token = value.strip()
        self._save_config()
        return self.get_config()

    def set_personal_library_root(self, value: str | Path) -> dict[str, Any]:
        root = Path(value).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise NotADirectoryError(f"个人素材库路径不是目录: {root}")
        self.settings.personal_library_root = root
        self._save_config()
        return self.get_config()

    def select_media_file(self, media_kind: str = "video") -> dict[str, Any]:
        """Show a native picker and return a local path without copying it."""

        kind = media_kind.strip().lower()
        filetypes = {
            "video": [
                ("视频文件", "*.mp4 *.mov *.avi *.mkv *.webm"),
                ("所有文件", "*.*"),
            ],
            "audio": [
                ("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac"),
                ("所有文件", "*.*"),
            ],
            "excel": [
                ("Excel 文件", "*.xlsx *.xls"),
                ("所有文件", "*.*"),
            ],
        }
        if kind not in filetypes:
            raise ValueError("media_kind 只能是 video、audio 或 excel")
        selected = self._ask_open_filename(filetypes[kind])
        if not selected:
            return {"cancelled": True, "kind": kind}
        path = Path(selected).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"选择的文件不存在: {path}")
        return {
            "cancelled": False,
            "kind": kind,
            "path": str(path),
            "name": path.name,
            "size": path.stat().st_size,
        }

    def select_output_folder(self) -> dict[str, Any]:
        """Show a native folder picker for standalone render output."""

        selected = self._ask_directory()
        if not selected:
            return {"cancelled": True}
        path = Path(selected).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"选择的导出目录不存在: {path}")
        return {"cancelled": False, "path": str(path), "name": path.name or str(path)}

    @staticmethod
    def _ask_open_filename(filetypes: list[tuple[str, str]]) -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:
            raise RuntimeError("当前采集器缺少 Windows 文件选择组件") from exc
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            root.update_idletasks()
            root.lift()
            root.focus_force()
            root.update()
            return str(filedialog.askopenfilename(parent=root, title="选择本机文件", filetypes=filetypes))
        finally:
            root.destroy()

    @staticmethod
    def _ask_directory() -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as exc:
            raise RuntimeError("当前采集器缺少 Windows 文件夹选择组件") from exc
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            root.update_idletasks()
            root.lift()
            root.focus_force()
            root.update()
            return str(filedialog.askdirectory(parent=root, title="选择视频导出文件夹", mustexist=True))
        finally:
            root.destroy()

    def list_drafts(self) -> list[dict[str, Any]]:
        root = self.settings.draft_root
        if not root.exists():
            raise FileNotFoundError(f"剪映草稿目录不存在: {root}")

        drafts: list[dict[str, Any]] = []
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            content_path = directory / "draft_content.json"
            if not content_path.is_file():
                continue
            try:
                stat = content_path.stat()
            except OSError:
                continue
            plain = is_plain_json_file(content_path)
            summary = self._plain_draft_summary(content_path) if plain else {}
            drafts.append(
                {
                    "name": directory.name,
                    "path": str(directory.resolve()),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "modified_timestamp": stat.st_mtime,
                    "content_size_bytes": stat.st_size,
                    "encryption_status": "plain" if plain else "encrypted",
                    **summary,
                }
            )
        drafts.sort(
            key=lambda item: (float(item.get("modified_timestamp", 0)), str(item.get("name", ""))),
            reverse=True,
        )
        return drafts

    def analyze_draft(
        self,
        draft_dir: str | Path,
        *,
        hash_limit_bytes: int | None = DEFAULT_HASH_LIMIT_BYTES,
    ) -> dict[str, Any]:
        source_dir = Path(draft_dir).expanduser().resolve()
        self._ensure_inside_draft_root(source_dir)
        if not (source_dir / "draft_content.json").is_file():
            raise FileNotFoundError(f"草稿缺少 draft_content.json: {source_dir}")

        prepared = prepare_plain_draft_dir(
            source_dir,
            work_root=self.settings.decrypt_work_root,
        )
        with (prepared.draft_dir / "draft_content.json").open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("draft_content.json 顶层必须是 JSON 对象")

        report = analyze_draft_import(
            data,
            source_draft_dir=prepared.source_dir,
            analyzed_draft_dir=prepared.draft_dir,
            was_decrypted=prepared.was_decrypted,
            workspace_root=self.settings.workspace_root,
            hash_limit_bytes=hash_limit_bytes,
        )
        report_id = uuid.uuid4().hex
        report["report_id"] = report_id
        report_path = self._reports_root / f"{report_id}.json"
        self._write_json(report_path, report)
        log_event(
            logger,
            "collector.draft_analyzed",
            "剪映草稿分析完成",
            component="collector",
            report_id=report_id,
            was_decrypted=prepared.was_decrypted,
        )
        return report

    def get_report(self, report_id: str) -> dict[str, Any]:
        safe_id = "".join(char for char in report_id if char.isalnum())
        if not safe_id or safe_id != report_id:
            raise ValueError("分析报告 ID 不合法")
        path = self._reports_root / f"{safe_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"分析报告不存在: {report_id}")
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"分析报告格式错误: {path}")
        return data

    def extract_fonts(self, draft_dir: str | Path, *, replace: bool = False) -> dict[str, Any]:
        source_dir = Path(draft_dir).expanduser().resolve()
        self._ensure_inside_draft_root(source_dir)
        prepared = prepare_plain_draft_dir(
            source_dir,
            work_root=self.settings.decrypt_work_root,
        )
        with (prepared.draft_dir / "draft_content.json").open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("draft_content.json 顶层必须是 JSON 对象")
        return export_font_library(
            data,
            self.settings.font_library_root,
            source_draft_dir=prepared.source_dir,
            analyzed_draft_dir=prepared.draft_dir,
            source_label=str(source_dir),
            replace=replace,
        ).as_dict()

    def collect_personal_assets(
        self,
        draft_dir: str | Path,
        *,
        replace: bool = False,
        kinds: list[str] | None = None,
        upload: bool = False,
        server_url: str = "",
    ) -> dict[str, Any]:
        """Extract all supported reusable assets from one draft into the personal library."""

        source_dir = Path(draft_dir).expanduser().resolve()
        self._ensure_inside_draft_root(source_dir)
        prepared = prepare_plain_draft_dir(source_dir, work_root=self.settings.decrypt_work_root)
        with (prepared.draft_dir / "draft_content.json").open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("draft_content.json 顶层必须是 JSON 对象")
        root = Path(self.settings.personal_library_root).resolve()
        source_label = str(source_dir)
        allowed_kinds = {
            "audio", "effects", "fonts", "stickers", "corner_stickers",
            "text_effects", "text_templates",
        }
        selected_kinds = list(dict.fromkeys(kinds or [
            "audio", "effects", "fonts", "stickers", "text_effects", "text_templates",
        ]))
        invalid_kinds = [kind for kind in selected_kinds if kind not in allowed_kinds]
        if invalid_kinds:
            raise ValueError(f"不支持的个人素材类型: {', '.join(invalid_kinds)}")
        if not selected_kinds:
            raise ValueError("请至少选择一种个人素材")

        def collect(run) -> dict[str, Any]:
            try:
                return {"ok": True, **run().as_dict()}
            except (ValueError, OSError, RuntimeError) as exc:
                return {"ok": False, "collected_count": 0, "message": str(exc)}

        collectors = {
            "audio": lambda: export_audio_library(
                    data, root / "audio_library", source_label=source_label, replace=replace
                ),
            "effects": lambda: export_effect_library(
                    data, root / "effect_library", source_label=source_label, replace=replace
                ),
            "fonts": lambda: export_font_library(
                    data,
                    root / "font_library",
                    source_draft_dir=prepared.source_dir,
                    analyzed_draft_dir=prepared.draft_dir,
                    source_label=source_label,
                    replace=replace,
                ),
            "stickers": lambda: export_sticker_library(
                    data, root / "sticker_library", source_label=source_label, replace=replace
                ),
            "corner_stickers": lambda: export_sticker_library(
                    data,
                    root / "corner_sticker_library",
                    source_label=source_label,
                    replace=replace,
                    usage="corner_decoration",
                ),
            "text_effects": lambda: export_text_effect_library(
                    data, root / "text_effect_library", source_label=source_label, replace=replace
                ),
            "text_templates": lambda: export_text_template_library(
                    data, root / "text_template_library", source_label=source_label, replace=replace
                ),
        }
        results = {kind: collect(collectors[kind]) for kind in selected_kinds}
        response = {
            "ok": True,
            "draft": source_dir.name,
            "personal_library_root": str(root),
            "results": results,
        }
        if upload:
            successful_kinds = [kind for kind, item in results.items() if item.get("ok")]
            if not successful_kinds:
                raise RuntimeError("所选素材均未能提取，无法上传")
            package = self._build_personal_asset_package(root, successful_kinds)
            response["upload"] = self._post_personal_asset_package(
                self._normalize_server_url(server_url or self.settings.render_server_url),
                package["path"],
                checksum=package["checksum_sha256"],
                access_token=self.settings.access_token,
            )
            response["package"] = {
                "filename": package["path"].name,
                "size": package["path"].stat().st_size,
                "checksum_sha256": package["checksum_sha256"],
            }
        log_event(
            logger,
            "collector.assets_collected",
            "个人素材采集完成",
            component="collector",
            kinds=selected_kinds,
            uploaded=upload,
            success_count=sum(1 for item in results.values() if item.get("ok")),
        )
        return response

    def _build_personal_asset_package(self, root: Path, kinds: list[str]) -> dict[str, Any]:
        directory_by_kind = {
            "audio": "audio_library",
            "effects": "effect_library",
            "fonts": "font_library",
            "stickers": "sticker_library",
            "corner_stickers": "corner_sticker_library",
            "text_effects": "text_effect_library",
            "text_templates": "text_template_library",
        }
        package_path = self._packages_root / f"personal_assets_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "personal_assets_manifest.json",
                json.dumps(
                    {
                        "schema": "jyd.personal_asset_transfer.v1",
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "kinds": kinds,
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
            )
            for kind in kinds:
                directory_name = directory_by_kind[kind]
                source = root / directory_name
                if not source.is_dir():
                    continue
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, (Path(directory_name) / path.relative_to(source)).as_posix())
        digest = hashlib.sha256()
        with package_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": package_path, "checksum_sha256": digest.hexdigest()}

    @staticmethod
    def _post_personal_asset_package(
        server_url: str,
        package_path: Path,
        *,
        checksum: str,
        access_token: str = "",
    ) -> dict[str, Any]:
        parsed = urlsplit(server_url)
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parsed.hostname, parsed.port, timeout=300)
        target = f"{parsed.path.rstrip('/')}/api/personal-assets/import"
        try:
            connection.putrequest("POST", target)
            connection.putheader("Content-Type", "application/zip")
            connection.putheader("Content-Length", str(package_path.stat().st_size))
            connection.putheader("X-Package-SHA256", checksum)
            if access_token.strip() and not import_ticket:
                connection.putheader("X-JYD-Access-Token", access_token.strip())
            connection.endheaders()
            with package_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(chunk)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise ConnectionError(f"无法连接网站后端 {server_url}: {exc}") from exc
        finally:
            connection.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"detail": raw.decode("utf-8", errors="replace")}
        if response.status < 200 or response.status >= 300:
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            raise RuntimeError(f"网站后端拒绝个人素材上传（HTTP {response.status}）: {detail}")
        if not isinstance(payload, dict):
            raise RuntimeError("网站后端返回了无法识别的个人素材结果")
        return payload

    def create_upload_plan(
        self,
        report_id: str,
        policies: dict[str, str],
        *,
        mode: str = "default",
    ) -> dict[str, Any]:
        report = self.get_report(report_id)
        plan = build_draft_upload_plan(report, policies, mode=mode)
        plan_id = uuid.uuid4().hex
        plan["plan_id"] = plan_id
        self._write_json(self._plans_root / f"{plan_id}.json", plan)
        log_event(
            logger,
            "collector.upload_plan_created",
            "草稿上传清单已创建",
            component="collector",
            report_id=report_id,
            plan_id=plan_id,
        )
        return plan

    def get_upload_plan(self, plan_id: str) -> dict[str, Any]:
        safe_id = "".join(char for char in plan_id if char.isalnum())
        if not safe_id or safe_id != plan_id:
            raise ValueError("上传清单 ID 不合法")
        path = self._plans_root / f"{safe_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"上传清单不存在: {plan_id}")
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"上传清单格式错误: {path}")
        return data

    def upload_plan(
        self,
        plan_id: str,
        *,
        template_name: str = "",
        template_lifecycle: str = "",
        server_url: str = "",
        template_import_ticket: str = "",
    ) -> dict[str, Any]:
        plan = self.get_upload_plan(plan_id)
        if plan.get("mode") == "template_center" and not template_import_ticket.strip():
            raise ValueError("模板中心上传缺少当前账号的一次性上传凭证")
        destination = self._packages_root / f"{plan_id}.zip"
        package = build_transfer_package(plan, destination)
        base_url = self._normalize_server_url(server_url or self.settings.render_server_url)
        response = self._post_package(
            base_url,
            destination,
            checksum=str(package["checksum_sha256"]),
            template_name=template_name,
            template_lifecycle=template_lifecycle,
            access_token=self.settings.access_token,
            template_import_ticket=template_import_ticket,
        )
        result = {
            "plan_id": plan_id,
            "server_url": base_url,
            "package": {key: value for key, value in package.items() if key != "manifest"},
            "server_result": response,
        }
        self._write_json(self._uploads_root / f"{plan_id}.json", result)
        log_event(
            logger,
            "collector.upload_completed",
            "草稿包上传完成",
            component="collector",
            plan_id=plan_id,
        )
        return result

    @property
    def _config_path(self) -> Path:
        return self.settings.state_root / "config.json"

    @property
    def _reports_root(self) -> Path:
        path = self.settings.state_root / "reports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _plans_root(self) -> Path:
        path = self.settings.state_root / "upload_plans"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _packages_root(self) -> Path:
        path = self.settings.state_root / "packages"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _uploads_root(self) -> Path:
        path = self.settings.state_root / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_saved_config(self) -> None:
        if not self._config_path.is_file():
            return
        try:
            with self._config_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                return
            draft_root = str(data.get("draft_root", "")).strip()
            draft_root_mode = str(data.get("draft_root_mode", "manual")).strip().lower()
            if draft_root_mode == "auto":
                self.settings.draft_root = detect_default_draft_root()
                self.settings.draft_root_mode = "auto"
            elif draft_root:
                self.settings.draft_root = Path(draft_root).expanduser().resolve()
                self.settings.draft_root_mode = "manual"
            server_url = str(data.get("render_server_url", "")).strip()
            if server_url:
                self.settings.render_server_url = self._normalize_server_url(server_url)
            access_token = str(data.get("access_token", "")).strip()
            if access_token:
                self.settings.access_token = access_token
            personal_root = str(data.get("personal_library_root", "")).strip()
            if personal_root:
                self.settings.personal_library_root = Path(personal_root).expanduser().resolve()
                self.settings.personal_library_root.mkdir(parents=True, exist_ok=True)
        except (OSError, json.JSONDecodeError):
            return

    def _save_config(self) -> None:
        self._write_json(
            self._config_path,
            {
                "draft_root": str(self.settings.draft_root),
                "draft_root_mode": self.settings.draft_root_mode,
                "render_server_url": self.settings.render_server_url,
                "access_token": self.settings.access_token,
                "personal_library_root": str(self.settings.personal_library_root),
            },
        )

    @staticmethod
    def _normalize_server_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("网站后端地址必须是有效的 http:// 或 https:// 地址")
        if parsed.query or parsed.fragment:
            raise ValueError("网站后端地址不能包含查询参数或锚点")
        return normalized

    @staticmethod
    def _post_package(
        server_url: str,
        package_path: Path,
        *,
        checksum: str,
        template_name: str,
        template_lifecycle: str = "",
        access_token: str = "",
        template_import_ticket: str = "",
    ) -> dict[str, Any]:
        parsed = urlsplit(server_url)
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(parsed.hostname, parsed.port, timeout=300)
        base_path = parsed.path.rstrip("/")
        import_ticket = template_import_ticket.strip()
        target = (
            f"{base_path}/api/new/jianying-template-imports/{quote(import_ticket, safe='')}"
            if import_ticket
            else f"{base_path}/api/draft-imports"
        )
        query = {}
        if not import_ticket and template_name.strip():
            query["template_name"] = template_name.strip()
        if not import_ticket and template_lifecycle.strip():
            query["lifecycle"] = template_lifecycle.strip()
        if query:
            target += f"?{urlencode(query)}"
        try:
            connection.putrequest("POST", target)
            connection.putheader("Content-Type", "application/zip")
            connection.putheader("Content-Length", str(package_path.stat().st_size))
            connection.putheader("X-Package-SHA256", checksum)
            if access_token.strip():
                connection.putheader("X-JYD-Access-Token", access_token.strip())
            connection.endheaders()
            with package_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    connection.send(chunk)
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise ConnectionError(f"无法连接网站后端 {server_url}: {exc}") from exc
        finally:
            connection.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"detail": raw.decode("utf-8", errors="replace")}
        if response.status < 200 or response.status >= 300:
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            raise RuntimeError(f"网站后端拒绝上传（HTTP {response.status}）: {detail}")
        if not isinstance(payload, dict):
            raise RuntimeError("网站后端返回了无法识别的数据")
        return payload

    def _ensure_inside_draft_root(self, path: Path) -> None:
        root = self.settings.draft_root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"只能分析当前剪映草稿目录中的项目: {root}") from exc
        if path == root:
            raise ValueError("请选择一个具体草稿，不要选择草稿根目录")

    @staticmethod
    def _plain_draft_summary(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        tracks = data.get("tracks", [])
        canvas = data.get("canvas_config", {})
        return {
            "duration_us": int(data.get("duration", 0) or 0),
            "track_count": len(tracks) if isinstance(tracks, list) else 0,
            "canvas_width": int(canvas.get("width", 0) or 0) if isinstance(canvas, dict) else 0,
            "canvas_height": int(canvas.get("height", 0) or 0) if isinstance(canvas, dict) else 0,
        }

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def detect_default_draft_root() -> Path:
    return detect_jianying_draft_root()
