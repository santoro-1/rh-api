"""Inspect or safely change the production workbench-device rollout mode.

The command uses the application's configured database and signing root.  It
never edits SQLite with ad-hoc SQL, never deploys code, and never exposes key
material.  Every successful change is revision-checked and audited in the same
transaction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from sqlalchemy import func, or_, select


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _snapshot(db, *, now: int) -> dict:
    from app.config import get_settings
    from app.services.device_auth.models import (
        WorkbenchDevice,
        WorkbenchDeviceControl,
        WorkbenchDeviceGrant,
    )

    control = db.get(WorkbenchDeviceControl, 1, populate_existing=True)
    if control is None:
        raise RuntimeError(
            "workbench_device_control is missing; deploy migrations before using this tool"
        )
    unexpired_condition = or_(
        WorkbenchDeviceGrant.expires_at.is_(None),
        WorkbenchDeviceGrant.expires_at > now,
    )
    occupied_condition = (
        WorkbenchDeviceGrant.status.in_(("ACTIVE", "SUSPENDED"))
        & unexpired_condition
    )
    active_condition = (
        (WorkbenchDeviceGrant.status == "ACTIVE")
        & unexpired_condition
    )
    return {
        "schema": "publicvideo.device-control-status.v1",
        "environment": get_settings().app_env,
        "mode": control.mode,
        "revision": control.revision,
        "active_grants": db.scalar(
            select(func.count()).select_from(WorkbenchDeviceGrant).where(active_condition)
        )
        or 0,
        "occupied_grants": db.scalar(
            select(func.count()).select_from(WorkbenchDeviceGrant).where(occupied_condition)
        )
        or 0,
        "pending_grants": db.scalar(
            select(func.count())
            .select_from(WorkbenchDeviceGrant)
            .where(WorkbenchDeviceGrant.status == "PENDING")
        )
        or 0,
        "registered_devices": db.scalar(
            select(func.count()).select_from(WorkbenchDevice)
        )
        or 0,
    }


def _confirmation(old_mode: str, new_mode: str, revision: int) -> str:
    return (
        "CHANGE-WORKBENCH-DEVICE-MODE:"
        f"{old_mode}->{new_mode}:REVISION-{revision}"
    )


def _allowed_transitions(mode: str, revision: int) -> dict[str, str]:
    order = {
        "OFF": ("OBSERVE",),
        "OBSERVE": ("OFF", "ENFORCE"),
        "ENFORCE": ("OBSERVE",),
    }
    return {
        destination: _confirmation(mode, destination, revision)
        for destination in order.get(mode, ())
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读检查或受控切换工作台设备授权模式"
    )
    parser.add_argument(
        "--set-mode", choices=("OFF", "OBSERVE", "ENFORCE"), default=""
    )
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--acknowledge-global-impact",
        action="store_true",
        help="开启 ENFORCE 时确认其影响所有已接入的受保护入口",
    )
    return parser


def run(args: argparse.Namespace, *, session_factory=None, now: int | None = None) -> dict:
    from app.database import SessionLocal
    from app.services.device_auth import service

    timestamp = int(time.time()) if now is None else now
    factory = session_factory or SessionLocal
    with factory() as db:
        before = _snapshot(db, now=timestamp)
        if not args.set_mode:
            return {
                **before,
                "allowed_transitions": _allowed_transitions(
                    before["mode"], before["revision"]
                ),
            }
        if args.expected_revision is None:
            raise ValueError("--expected-revision is required for a mode change")
        expected = _confirmation(
            before["mode"], args.set_mode, args.expected_revision
        )
        if args.confirm != expected:
            raise ValueError(f"confirmation mismatch; inspect and type exactly: {expected}")
        if (before["mode"], args.set_mode) not in service.CONTROL_TRANSITIONS:
            from app.services.device_auth.errors import DeviceAuthError

            raise DeviceAuthError(
                "DEVICE_CONTROL_TRANSITION_DENIED",
                "设备授权模式必须按 OFF、OBSERVE、ENFORCE 的受控顺序切换",
                409,
            )
        if args.set_mode == "ENFORCE":
            if not args.acknowledge_global_impact:
                raise ValueError(
                    "--acknowledge-global-impact is required before ENFORCE"
                )
            if before["active_grants"] < 1:
                raise ValueError("ENFORCE requires at least one approved device grant")
            from app.services.device_auth.tokens import (
                get_device_auth_config,
                load_key_ring,
            )

            # Load and match the real private/public root before changing the row.
            load_key_ring(get_device_auth_config(), signing=True)
        try:
            control = service.change_control_mode(
                db,
                expected_revision=args.expected_revision,
                new_mode=args.set_mode,
                operator=args.operator,
                reason=args.reason,
                now=timestamp,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        after = _snapshot(db, now=timestamp)
        if after["mode"] != control.mode or after["revision"] != control.revision:
            raise RuntimeError("mode change was not persisted")
        return {
            **after,
            "previous_mode": before["mode"],
            "previous_revision": before["revision"],
            "allowed_transitions": _allowed_transitions(
                after["mode"], after["revision"]
            ),
        }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:  # operator CLI: return one bounded, non-secret error
        code = getattr(exc, "code", exc.__class__.__name__)
        print(
            json.dumps(
                {"ok": False, "error": str(code), "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
