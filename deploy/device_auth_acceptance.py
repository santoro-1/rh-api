"""Capture and verify authoritative A/B device-authorization acceptance evidence.

Capture reads the configured application database without changing it.  It
records public identifiers and status only: no password, token, private key,
provider key, request body, local path, or hardware serial number is included.
Verification is offline and fails closed when evidence is ambiguous.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time

from sqlalchemy import or_, select


CAPTURE_SCHEMA = "publicvideo.device-acceptance.capture.v1"
REPORT_SCHEMA = "publicvideo.device-acceptance.report.v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,80}\Z")
THUMBPRINT = re.compile(r"[A-Za-z0-9_-]{43}\Z")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AcceptanceError(ValueError):
    pass


def _strict_json(raw: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AcceptanceError("duplicate JSON field")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def _read_capture(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65_536:
        raise AcceptanceError(f"invalid evidence file: {path}")
    try:
        value = _strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read evidence file: {path}") from exc
    try:
        return _validate_capture(value)
    except AcceptanceError as exc:
        raise AcceptanceError(f"invalid evidence structure: {path}") from exc


def _validate_capture(value: dict) -> dict:
    top_fields = {
        "schema",
        "captured_at",
        "environment",
        "role",
        "machine_label",
        "build_label",
        "package_sha256",
        "control",
        "account",
        "policy",
        "device",
        "grant",
        "audit",
    }
    if not isinstance(value, dict) or set(value) != top_fields:
        raise AcceptanceError("unexpected evidence fields")
    if (
        value["schema"] != CAPTURE_SCHEMA
        or type(value["captured_at"]) is not int
        or value["captured_at"] < 1
        or value["environment"] not in {"development", "test", "production"}
        or value["role"] not in {"A", "B"}
    ):
        raise AcceptanceError("invalid evidence header")
    _safe_text(value["machine_label"], "machine label")
    _safe_text(value["build_label"], "build label")
    _normalized_sha256(value["package_sha256"])

    control = value["control"]
    account = value["account"]
    policy = value["policy"]
    device = value["device"]
    grant = value["grant"]
    if (
        not isinstance(control, dict)
        or set(control) != {"mode", "revision"}
        or control["mode"] not in {"OFF", "OBSERVE", "ENFORCE"}
        or type(control["revision"]) is not int
        or control["revision"] < 1
    ):
        raise AcceptanceError("invalid control evidence")
    if (
        not isinstance(account, dict)
        or set(account) != {"id", "username"}
        or type(account["id"]) is not int
        or account["id"] < 1
    ):
        raise AcceptanceError("invalid account evidence")
    _safe_text(account["username"], "username")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"max_devices", "allow_software", "revision"}
        or type(policy["max_devices"]) is not int
        or not 0 <= policy["max_devices"] <= 1000
        or type(policy["allow_software"]) is not bool
        or (
            policy["revision"] is not None
            and (type(policy["revision"]) is not int or policy["revision"] < 1)
        )
    ):
        raise AcceptanceError("invalid policy evidence")
    if not isinstance(device, dict) or set(device) != {
        "id",
        "thumbprint",
        "status",
        "protection",
        "protection_verified",
        "created_at",
        "last_seen_at",
    }:
        raise AcceptanceError("invalid device evidence")
    if (
        not isinstance(device["id"], str)
        or not 1 <= len(device["id"]) <= 128
        or not isinstance(device["thumbprint"], str)
        or not THUMBPRINT.fullmatch(device["thumbprint"])
        or device["status"] not in {"ACTIVE", "SUSPENDED", "REVOKED"}
        or device["protection"] not in {"tpm", "software"}
        or type(device["protection_verified"]) is not bool
        or any(
            type(device[name]) is not int or device[name] < 1
            for name in ("created_at", "last_seen_at")
        )
    ):
        raise AcceptanceError("invalid device evidence")
    if not isinstance(grant, dict) or set(grant) != {
        "id",
        "label",
        "status",
        "client_version",
        "revision",
        "scopes",
        "expires_at",
        "created_at",
        "updated_at",
    }:
        raise AcceptanceError("invalid grant evidence")
    if (
        not isinstance(grant["id"], str)
        or not 1 <= len(grant["id"]) <= 128
        or grant["status"]
        not in {"PENDING", "ACTIVE", "REJECTED", "SUSPENDED", "REVOKED"}
        or not isinstance(grant["label"], str)
        or len(grant["label"]) > 80
        or not isinstance(grant["client_version"], str)
        or len(grant["client_version"]) > 80
        or type(grant["revision"]) is not int
        or grant["revision"] < 1
        or not isinstance(grant["scopes"], list)
        or not all(isinstance(scope, str) for scope in grant["scopes"])
        or len(grant["scopes"]) != len(set(grant["scopes"]))
        or not set(grant["scopes"]).issubset(
            {"cloud:generate", "local:draft", "local:render"}
        )
        or (
            grant["expires_at"] is not None
            and (type(grant["expires_at"]) is not int or grant["expires_at"] < 1)
        )
        or any(
            type(grant[name]) is not int or grant[name] < 1
            for name in ("created_at", "updated_at")
        )
    ):
        raise AcceptanceError("invalid grant evidence")
    if not isinstance(value["audit"], list) or len(value["audit"]) > 20:
        raise AcceptanceError("invalid audit evidence")
    for event in value["audit"]:
        if (
            not isinstance(event, dict)
            or set(event) != {"action", "created_at"}
            or not isinstance(event["action"], str)
            or not 1 <= len(event["action"]) <= 40
            or type(event["created_at"]) is not int
            or event["created_at"] < 1
        ):
            raise AcceptanceError("invalid audit evidence")
    return value


def _write_new_json(path: Path, value: dict) -> None:
    target = path.expanduser().resolve(strict=False)
    parent = target.parent
    if target.exists() or target.is_symlink():
        raise AcceptanceError(f"refusing to overwrite evidence: {target}")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise AcceptanceError("evidence parent must be a real directory")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".device-acceptance-",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _normalized_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceError("package SHA-256 must be text")
    normalized = value.strip().lower()
    if not SHA256.fullmatch(normalized):
        raise AcceptanceError("package SHA-256 must be exactly 64 hexadecimal characters")
    return normalized


def _safe_text(value: str, name: str, *, maximum: int = 80) -> str:
    if not isinstance(value, str):
        raise AcceptanceError(f"invalid {name}")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or not SAFE_LABEL.fullmatch(normalized)
    ):
        raise AcceptanceError(f"invalid {name}")
    return normalized


def capture(
    db,
    *,
    username: str,
    role: str,
    grant_label: str,
    machine_label: str,
    package_sha256: str,
    build_label: str,
    now: int,
) -> dict:
    from app.config import get_settings
    from app.models import User
    from app.services.device_auth.models import (
        WorkbenchDevice,
        WorkbenchDeviceAuditEvent,
        WorkbenchDeviceControl,
        WorkbenchDeviceGrant,
        WorkbenchDevicePolicy,
    )

    if role not in {"A", "B"}:
        raise AcceptanceError("role must be A or B")
    username = _safe_text(username, "username")
    grant_label = _safe_text(grant_label, "grant label")
    machine_label = _safe_text(machine_label, "machine label")
    build_label = _safe_text(build_label, "build label")
    package_sha256 = _normalized_sha256(package_sha256)
    if type(now) is not int or now < 1:
        raise AcceptanceError("invalid capture time")

    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise AcceptanceError("account was not found")
    records = db.execute(
        select(WorkbenchDeviceGrant, WorkbenchDevice)
        .join(WorkbenchDevice, WorkbenchDevice.id == WorkbenchDeviceGrant.device_id)
        .where(
            WorkbenchDeviceGrant.user_id == user.id,
            WorkbenchDeviceGrant.label == grant_label,
        )
    ).all()
    if len(records) != 1:
        raise AcceptanceError(
            "grant label must identify exactly one device for this account"
        )
    grant, device = records[0]
    control = db.get(WorkbenchDeviceControl, 1, populate_existing=True)
    if control is None:
        raise AcceptanceError("device control migration is not initialized")
    policy = db.get(WorkbenchDevicePolicy, user.id, populate_existing=True)
    audit_rows = db.scalars(
        select(WorkbenchDeviceAuditEvent)
        .where(
            or_(
                WorkbenchDeviceAuditEvent.grant_id == grant.id,
                WorkbenchDeviceAuditEvent.device_id == device.id,
            )
        )
        .order_by(WorkbenchDeviceAuditEvent.created_at.desc())
        .limit(20)
    ).all()
    return {
        "schema": CAPTURE_SCHEMA,
        "captured_at": now,
        "environment": get_settings().app_env,
        "role": role,
        "machine_label": machine_label,
        "build_label": build_label,
        "package_sha256": package_sha256,
        "control": {"mode": control.mode, "revision": control.revision},
        "account": {"id": user.id, "username": user.username},
        "policy": {
            "max_devices": policy.max_devices if policy else 1,
            "allow_software": policy.allow_software if policy else False,
            "revision": policy.revision if policy else None,
        },
        "device": {
            "id": device.id,
            "thumbprint": device.thumbprint,
            "status": device.status,
            "protection": device.protection_report,
            "protection_verified": device.protection_verified,
            "created_at": device.created_at,
            "last_seen_at": device.last_seen_at,
        },
        "grant": {
            "id": grant.id,
            "label": grant.label,
            "status": grant.status,
            "client_version": grant.client_version,
            "revision": grant.revision,
            "scopes": sorted(json.loads(grant.scopes_json)),
            "expires_at": grant.expires_at,
            "created_at": grant.created_at,
            "updated_at": grant.updated_at,
        },
        "audit": [
            {"action": row.action, "created_at": row.created_at} for row in audit_rows
        ],
    }


def verify_copy(a: dict, b: dict, *, now: int) -> dict:
    a = _validate_capture(a)
    b = _validate_capture(b)
    checks = {
        "roles_are_A_and_B": a.get("role") == "A" and b.get("role") == "B",
        "same_account": a.get("account") == b.get("account"),
        "same_copied_package": a.get("package_sha256") == b.get("package_sha256"),
        "single_device_quota": a.get("policy", {}).get("max_devices") == 1
        and b.get("policy", {}).get("max_devices") == 1,
        "different_machine_keys": a.get("device", {}).get("thumbprint")
        != b.get("device", {}).get("thumbprint"),
        "different_device_ids": a.get("device", {}).get("id")
        != b.get("device", {}).get("id"),
        "different_grant_ids": a.get("grant", {}).get("id")
        != b.get("grant", {}).get("id"),
        "A_remains_active": a.get("device", {}).get("status") == "ACTIVE"
        and a.get("grant", {}).get("status") == "ACTIVE",
        "B_is_not_approved": b.get("grant", {}).get("status")
        in {"PENDING", "REJECTED", "REVOKED"},
        "B_was_tested_under_enforcement": b.get("control", {}).get("mode")
        == "ENFORCE",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": REPORT_SCHEMA,
        "kind": "cross-machine-copy-rejection",
        "verified_at": now,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "A_evidence_sha256": _evidence_digest(a),
        "B_evidence_sha256": _evidence_digest(b),
    }


def verify_upgrade(captures: list[dict], *, now: int) -> dict:
    if len(captures) < 4:
        raise AcceptanceError("upgrade acceptance requires at least four A captures")
    captures = [_validate_capture(item) for item in captures]
    first = captures[0]
    checks = {
        "all_role_A": all(item.get("role") == "A" for item in captures),
        "same_account": all(item.get("account") == first.get("account") for item in captures),
        "same_machine_key": all(
            item.get("device", {}).get("thumbprint")
            == first.get("device", {}).get("thumbprint")
            for item in captures
        ),
        "same_device_id": all(
            item.get("device", {}).get("id") == first.get("device", {}).get("id")
            for item in captures
        ),
        "same_grant_id": all(
            item.get("grant", {}).get("id") == first.get("grant", {}).get("id")
            for item in captures
        ),
        "all_active": all(
            item.get("device", {}).get("status") == "ACTIVE"
            and item.get("grant", {}).get("status") == "ACTIVE"
            for item in captures
        ),
        "four_distinct_builds": len(
            {item.get("build_label") for item in captures}
        )
        == len(captures),
        "four_distinct_packages": len(
            {item.get("package_sha256") for item in captures}
        )
        == len(captures),
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": REPORT_SCHEMA,
        "kind": "same-machine-upgrade-without-reactivation",
        "verified_at": now,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "evidence_sha256": [_evidence_digest(item) for item in captures],
    }


def _evidence_digest(value: dict) -> str:
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="记录并校验工作台 A/B 双机授权验收")
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture", help="从服务器数据库生成只读证据")
    capture_parser.add_argument("--username", required=True)
    capture_parser.add_argument("--role", choices=("A", "B"), required=True)
    capture_parser.add_argument("--grant-label", required=True)
    capture_parser.add_argument("--machine-label", required=True)
    capture_parser.add_argument("--package-sha256", required=True)
    capture_parser.add_argument("--build-label", required=True)
    capture_parser.add_argument("--output", type=Path, required=True)

    copy_parser = commands.add_parser("verify-copy", help="验证复制到 B 后未继承 A 授权")
    copy_parser.add_argument("--a", type=Path, required=True)
    copy_parser.add_argument("--b", type=Path, required=True)
    copy_parser.add_argument("--output", type=Path, required=True)

    upgrade_parser = commands.add_parser(
        "verify-upgrade", help="验证 A 机至少四次构建更新未重新激活"
    )
    upgrade_parser.add_argument("--capture", type=Path, action="append", required=True)
    upgrade_parser.add_argument("--output", type=Path, required=True)
    return parser


def run(args: argparse.Namespace, *, session_factory=None, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else now
    if args.command == "capture":
        from app.database import SessionLocal

        factory = session_factory or SessionLocal
        with factory() as db:
            result = capture(
                db,
                username=args.username,
                role=args.role,
                grant_label=args.grant_label,
                machine_label=args.machine_label,
                package_sha256=args.package_sha256,
                build_label=args.build_label,
                now=timestamp,
            )
    elif args.command == "verify-copy":
        result = verify_copy(_read_capture(args.a), _read_capture(args.b), now=timestamp)
    else:
        result = verify_upgrade(
            [_read_capture(path) for path in args.capture], now=timestamp
        )
    _write_new_json(args.output, result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": getattr(exc, "code", exc.__class__.__name__),
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": bool(result.get("passed", True)),
                "schema": result["schema"],
                "output": str(args.output.resolve()),
                "failed_checks": result.get("failed_checks", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.get("passed", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
