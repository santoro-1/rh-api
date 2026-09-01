from __future__ import annotations

import argparse
import json
import shutil
import uuid

import pytest

from app.database import SessionLocal
from app.models import User
from app.services.device_auth.models import (
    WorkbenchDevice,
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceControl,
    WorkbenchDeviceGrant,
    WorkbenchDevicePolicy,
)
from deploy import device_auth_acceptance, device_auth_control
from tests.conftest import TEST_RUNTIME_ROOT, create_user


def _seed_control(mode="OFF", revision=1):
    with SessionLocal() as db:
        db.add(WorkbenchDeviceControl(id=1, mode=mode, revision=revision))
        db.commit()


def _seed_grant(username: str, *, label: str, thumbprint: str, status: str):
    user = create_user(username, with_config=False)
    with SessionLocal() as db:
        device = WorkbenchDevice(
            thumbprint=thumbprint,
            public_jwk_json=json.dumps(
                {"kty": "EC", "crv": "P-256", "x": "x", "y": "y"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            protection_report="tpm",
            protection_verified=False,
            status="ACTIVE",
        )
        db.add(device)
        db.flush()
        db.add(WorkbenchDevicePolicy(user_id=user.id, max_devices=1, revision=1))
        grant = WorkbenchDeviceGrant(
            user_id=user.id,
            device_id=device.id,
            label=label,
            client_version="acceptance-v1",
            status=status,
            scopes_json=(
                '["cloud:generate","local:draft","local:render"]'
                if status == "ACTIVE"
                else "[]"
            ),
            revision=1,
        )
        db.add(grant)
        db.commit()
        return user.id, device.id, grant.id


def _mode_args(*values: str) -> argparse.Namespace:
    return device_auth_control.build_parser().parse_args(list(values))


def _sample_capture(*, role="A", build="v1", package="a" * 64):
    return {
        "schema": device_auth_acceptance.CAPTURE_SCHEMA,
        "captured_at": 1_788_200_000,
        "environment": "test",
        "role": role,
        "machine_label": f"machine-{role}",
        "build_label": build,
        "package_sha256": package,
        "control": {"mode": "ENFORCE", "revision": 7},
        "account": {"id": 1, "username": "same"},
        "policy": {"max_devices": 1, "allow_software": False, "revision": 1},
        "device": {
            "id": f"device-{role}",
            "thumbprint": role * 43,
            "status": "ACTIVE",
            "protection": "tpm",
            "protection_verified": False,
            "created_at": 1_788_100_000,
            "last_seen_at": 1_788_200_000,
        },
        "grant": {
            "id": f"grant-{role}",
            "label": f"{role}-label",
            "status": "ACTIVE" if role == "A" else "PENDING",
            "client_version": build,
            "revision": 1,
            "scopes": (
                ["cloud:generate", "local:draft", "local:render"]
                if role == "A"
                else []
            ),
            "expires_at": None,
            "created_at": 1_788_100_000,
            "updated_at": 1_788_200_000,
        },
        "audit": [],
    }


def test_mode_tool_is_revision_checked_audited_and_requires_observe(monkeypatch):
    _seed_control()
    inspected = device_auth_control.run(
        _mode_args(), session_factory=SessionLocal, now=1_788_200_000
    )
    assert inspected["mode"] == "OFF"
    confirmation = inspected["allowed_transitions"]["OBSERVE"]

    changed = device_auth_control.run(
        _mode_args(
            "--set-mode",
            "OBSERVE",
            "--expected-revision",
            "1",
            "--operator",
            "san",
            "--reason",
            "A local and B 250 acceptance",
            "--confirm",
            confirmation,
        ),
        session_factory=SessionLocal,
        now=1_788_200_001,
    )
    assert (changed["previous_mode"], changed["mode"], changed["revision"]) == (
        "OFF",
        "OBSERVE",
        2,
    )
    with SessionLocal() as db:
        event = db.query(WorkbenchDeviceAuditEvent).one()
        details = json.loads(event.details_json)
        assert event.actor_user_id is None
        assert event.action == "device.control_mode_changed"
        assert details["operator"] == "san"
        assert details["old_mode"] == "OFF"
        assert details["new_mode"] == "OBSERVE"

    with pytest.raises(ValueError, match="confirmation mismatch"):
        device_auth_control.run(
            _mode_args(
                "--set-mode",
                "ENFORCE",
                "--expected-revision",
                "2",
                "--operator",
                "san",
                "--reason",
                "wrong confirmation must fail",
                "--confirm",
                "wrong",
                "--acknowledge-global-impact",
            ),
            session_factory=SessionLocal,
            now=1_788_200_002,
        )

    with pytest.raises(ValueError, match="at least one approved"):
        device_auth_control.run(
            _mode_args(
                "--set-mode",
                "ENFORCE",
                "--expected-revision",
                "2",
                "--operator",
                "san",
                "--reason",
                "no active grant must fail",
                "--confirm",
                device_auth_control._confirmation("OBSERVE", "ENFORCE", 2),
                "--acknowledge-global-impact",
            ),
            session_factory=SessionLocal,
            now=1_788_200_003,
        )

    _seed_grant("mode-ready-user", label="A-local", thumbprint="A" * 43, status="ACTIVE")
    monkeypatch.setattr(
        "app.services.device_auth.tokens.get_device_auth_config", lambda: object()
    )
    monkeypatch.setattr(
        "app.services.device_auth.tokens.load_key_ring",
        lambda config, *, signing: (config, signing),
    )
    enforced = device_auth_control.run(
        _mode_args(
            "--set-mode",
            "ENFORCE",
            "--expected-revision",
            "2",
            "--operator",
            "san",
            "--reason",
            "short controlled B rejection window",
            "--confirm",
            device_auth_control._confirmation("OBSERVE", "ENFORCE", 2),
            "--acknowledge-global-impact",
        ),
        session_factory=SessionLocal,
        now=1_788_200_004,
    )
    assert enforced["mode"] == "ENFORCE" and enforced["revision"] == 3
    assert list(enforced["allowed_transitions"]) == ["OBSERVE"]


def test_mode_tool_rejects_direct_off_to_enforce():
    _seed_control()
    with pytest.raises(Exception) as caught:
        device_auth_control.run(
            _mode_args(
                "--set-mode",
                "ENFORCE",
                "--expected-revision",
                "1",
                "--operator",
                "san",
                "--reason",
                "must stage through observe",
                "--confirm",
                device_auth_control._confirmation("OFF", "ENFORCE", 1),
                "--acknowledge-global-impact",
            ),
            session_factory=SessionLocal,
            now=1_788_200_000,
        )
    assert getattr(caught.value, "code", "") == "DEVICE_CONTROL_TRANSITION_DENIED"


def test_acceptance_capture_and_copy_verification_detects_machine_binding():
    _seed_control(mode="ENFORCE", revision=7)
    user_id, _, _ = _seed_grant(
        "two-machine-user", label="A-local", thumbprint="A" * 43, status="ACTIVE"
    )
    with SessionLocal() as db:
        user = db.get(User, user_id)
        b_device = WorkbenchDevice(
            thumbprint="B" * 43,
            public_jwk_json='{"crv":"P-256","kty":"EC","x":"x","y":"y"}',
            protection_report="tpm",
            protection_verified=False,
            status="ACTIVE",
        )
        db.add(b_device)
        db.flush()
        db.add(
            WorkbenchDeviceGrant(
                user_id=user.id,
                device_id=b_device.id,
                label="B-250",
                client_version="acceptance-v1",
                status="PENDING",
                scopes_json="[]",
                revision=1,
            )
        )
        db.commit()

    common = {
        "username": "two-machine-user",
        "package_sha256": "c" * 64,
        "build_label": "production-20260901-02",
        "now": 1_788_200_000,
    }
    with SessionLocal() as db:
        a = device_auth_acceptance.capture(
            db,
            role="A",
            grant_label="A-local",
            machine_label="local development computer",
            **common,
        )
        b = device_auth_acceptance.capture(
            db,
            role="B",
            grant_label="B-250",
            machine_label="processor 250",
            **common,
        )
    report = device_auth_acceptance.verify_copy(a, b, now=1_788_200_001)
    assert report["passed"]
    assert all(report["checks"].values())
    assert a["account"] == b["account"]
    assert a["device"]["thumbprint"] != b["device"]["thumbprint"]


def test_acceptance_copy_fails_if_B_is_approved_or_not_enforced():
    base = _sample_capture()
    base["control"] = {"mode": "OBSERVE", "revision": 2}
    b = _sample_capture(role="B")
    b["control"] = {"mode": "OBSERVE", "revision": 2}
    b["grant"].update(status="ACTIVE", scopes=["cloud:generate"])
    result = device_auth_acceptance.verify_copy(base, b, now=1)
    assert not result["passed"]
    assert result["failed_checks"] == [
        "B_is_not_approved",
        "B_was_tested_under_enforcement",
    ]


def test_upgrade_verification_requires_four_distinct_builds_and_preserves_identity():
    captures = []
    for index in range(4):
        captures.append(
            _sample_capture(
                build=f"v{index + 1}", package=f"{index + 1:064x}"
            )
        )
    result = device_auth_acceptance.verify_upgrade(captures, now=2)
    assert result["passed"] and all(result["checks"].values())

    captures[-1]["grant"]["id"] = "new-grant"
    failed = device_auth_acceptance.verify_upgrade(captures, now=3)
    assert not failed["passed"]
    assert failed["failed_checks"] == ["same_grant_id"]


def test_acceptance_evidence_writer_never_overwrites():
    root = TEST_RUNTIME_ROOT / f"device-acceptance-{uuid.uuid4().hex}"
    target = root / "evidence.json"
    try:
        device_auth_acceptance._write_new_json(target, {"schema": "test"})
        with pytest.raises(device_auth_acceptance.AcceptanceError, match="overwrite"):
            device_auth_acceptance._write_new_json(target, {"schema": "other"})
    finally:
        shutil.rmtree(root, ignore_errors=True)
