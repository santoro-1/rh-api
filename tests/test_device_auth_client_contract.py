"""Cross-repository contract tests using actual JYD client and RH server code.

No native CNG calls, production database, network requests or provider generation.
Set JYD_DEVICE_AUTH_SOURCE to the JYD src directory in a separate CI checkout.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from sqlalchemy import func, select

from app.database import SessionLocal
from app.services.device_auth import service
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.models import WorkbenchDevice, WorkbenchDeviceGrant
from app.services.device_auth.protocol import public_jwk, jwk_thumbprint, verify_proof
from tests.conftest import create_user
from tests.test_device_auth import ring, approve, account_token
from tests.test_h3_quote_lifecycle import pending_quote

_default_source = (
    Path(__file__).resolve().parents[2].parent / "公寓" / "jyd_plain_json_probe" / "src"
)
_client_source = Path(os.getenv("JYD_DEVICE_AUTH_SOURCE", str(_default_source)))
if not (_client_source / "jyd_probe" / "device_authorization.py").is_file():
    pytest.skip(
        "JYD checkout is required for the desktop/server contract",
        allow_module_level=True,
    )
sys.path.insert(0, str(_client_source))

from jyd_probe.device_auth_protocol import (
    DeviceAuthorizationError,
    TrustedIssuer,
    VerifiedCredentials,
)
from jyd_probe.device_authorization import DeviceAuthorizationSession


class TestKey:
    __test__ = False
    protection = "tpm"

    def __init__(self, key):
        self.key = key
        self.public_jwk = public_jwk(key.public_key())
        self.thumbprint = jwk_thumbprint(self.public_jwk)

    def sign(self, message):
        r, s = utils.decode_dss_signature(
            self.key.sign(message, ec.ECDSA(hashes.SHA256()))
        )
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def close(self):
        pass


class TestIdentity:
    __test__ = False

    def __init__(self):
        self.key = None
        self.created = 0

    def open_existing(self):
        return TestKey(self.key) if self.key is not None else None

    def initialize_for_activation(self, **_):
        if self.key is None:
            self.key = ec.generate_private_key(ec.SECP256R1())
            self.created += 1
        return self.open_existing()


class ServerTransport:
    def __init__(self, client):
        self.client = client
        self.paths = []

    def request(self, *, method, path, headers, payload=None):
        self.paths.append(path)
        response = self.client.request(method, path, headers=headers, json=payload)
        body = response.json()
        if response.status_code >= 400:
            raise DeviceAuthorizationError(
                body.get("code", "INVALID_RESPONSE"),
                body.get("detail", "test request failed"),
                status_code=response.status_code,
            )
        return body


def new_session(client, ring, user, identity):
    return DeviceAuthorizationSession(
        user_id=user.id,
        login_token=account_token(user),
        trust=TrustedIssuer(
            ring.config.origin, ring.config.environment, ring.verification_keys
        ),
        identity=identity,
        transport=ServerTransport(client),
    )


def activated(client, ring):
    user, admin, identity = (
        create_user("client-device-user"),
        create_user("client-device-admin", is_admin=True),
        TestIdentity(),
    )
    session = new_session(client, ring, user, identity)
    registration = session.register(
        label="test processing machine", client_version="v1"
    )
    assert session.summary()["state"] == "PENDING"
    with pytest.raises(DeviceAuthorizationError) as waiting:
        session.refresh()
    assert waiting.value.code == "DEVICE_PENDING"
    approve(admin, registration)
    session.refresh()
    return user, admin, identity, session, registration


def test_real_registration_approval_and_four_client_versions_reuse_identity(
    client, ring
):
    user, admin, identity, session, registration = activated(client, ring)
    expected = (
        session.summary()["device_id"],
        session.summary()["grant_id"],
        session.summary()["thumbprint"],
    )
    for version in ("v2", "v3", "v4"):
        session.close()
        session = new_session(client, ring, user, identity)
        session.refresh()  # no local credential cache and no registration needed
        assert (
            session.summary()["device_id"],
            session.summary()["grant_id"],
            session.summary()["thumbprint"],
        ) == expected
        assert not any(path.endswith("/register") for path in session._transport.paths)
        session.register(client_version=version)  # explicit repeat remains idempotent
        session.refresh()
        assert (
            session.require_local("local:render")["grant_id"]
            == registration["grant_id"]
        )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 1
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1
    assert identity.created == 1


def test_real_client_proofs_verify_once_and_request_nonce_can_be_reused(client, ring):
    user, _, identity, session, _ = activated(client, ring)
    args = dict(
        method="POST", path="/api/workbench/h3/batches/prepare", scope="cloud:generate"
    )
    proofs = []
    for _ in range(2):
        headers = session.request_headers(**args)
        proof = verify_proof(
            headers["DPoP"],
            method=args["method"],
            uri=ring.config.origin + args["path"],
            access_token=headers["Authorization"].removeprefix("DPoP "),
            expected_thumbprint=identity.open_existing().thumbprint,
            now=int(time.time()),
        )
        with SessionLocal() as db:
            service.consume_proof(
                db,
                user_id=user.id,
                proof=proof,
                purpose="request",
                now=int(time.time()),
            )
            db.commit()
        proofs.append(proof)
    assert proofs[0].nonce == proofs[1].nonce
    assert proofs[0].jti != proofs[1].jti
    with SessionLocal() as db, pytest.raises(DeviceAuthError) as replay:
        service.consume_proof(
            db,
            user_id=user.id,
            proof=proofs[0],
            purpose="request",
            now=int(time.time()),
        )
    assert replay.value.code == "DEVICE_PROOF_REPLAY"


def test_real_server_credentials_copied_to_other_key_fail_both_sides(client, ring):
    user, _, identity, session, _ = activated(client, ring)
    clone = TestIdentity()
    clone.initialize_for_activation()
    credentials = session._credentials
    payload = {
        "access_token": credentials.access_token,
        "local_lease": credentials.local_lease,
        "token_type": "DPoP",
        "refresh_after_seconds": 300,
        "device_id": credentials.claims["device_id"],
        "grant_id": credentials.claims["grant_id"],
        "thumbprint": identity.open_existing().thumbprint,
    }
    with pytest.raises(DeviceAuthorizationError):
        VerifiedCredentials.from_response(
            payload,
            session.trust,
            user_id=user.id,
            thumbprint=clone.open_existing().thumbprint,
            now=time.time(),
        )
    response = client.post(
        "/api/workbench/device-auth/challenge",
        json={"public_jwk": clone.open_existing().public_jwk, "purpose": "request"},
        headers={"Authorization": "DPoP " + credentials.access_token},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "DEVICE_IDENTITY_MISMATCH"


def test_actual_revocation_does_not_fall_back_to_unexpired_local_lease(client, ring):
    user, admin, identity, session, registration = activated(client, ring)
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, registration["grant_id"])
        service.change_grant(
            db,
            actor_id=admin.id,
            grant_id=grant.id,
            expected_revision=grant.revision,
            action="revoke",
            now=int(time.time()),
        )
        db.commit()
    with pytest.raises(DeviceAuthorizationError) as revoked:
        session.refresh(force=True)
    assert revoked.value.code == "DEVICE_REVOKED"
    assert session.summary()["state"] == "REVOKED"
    with pytest.raises(DeviceAuthorizationError):
        session.require_local("local:draft")
    assert identity.created == 1


def test_same_machine_account_does_not_inherit_another_account_grant(client, ring):
    _, _, identity, session, _ = activated(client, ring)
    other = create_user("client-other-user")
    other_session = new_session(client, ring, other, identity)
    with pytest.raises(DeviceAuthorizationError) as missing:
        other_session.refresh()
    assert missing.value.code == "DEVICE_UNREGISTERED"
    assert identity.created == 1
    assert session.require_local("local:draft")["user_id"] != other.id


def test_actual_signed_local_policy_enforces_key_and_changes_without_reactivation(
    client, ring
):
    from jyd_probe.device_local_execution import LocalDeviceAuthorizer

    user, admin, identity, session, registration = activated(client, ring)
    for mode in ("OFF", "OBSERVE", "ENFORCE"):
        with SessionLocal() as db:
            control = service.lock_control(db)
            control.mode = mode
            control.revision += 1
            db.commit()
        assert session.local_policy_mode(force=True) == mode
        decision = LocalDeviceAuthorizer(session).authorize(
            {"local:draft", "local:render"}
        )
        assert decision.user_id == user.id and decision.mode == mode
        if mode == "ENFORCE":
            assert decision.grant_id == registration["grant_id"]
    assert identity.created == 1
    clone = new_session(client, ring, user, TestIdentity())
    assert clone.local_policy_mode() == "ENFORCE"
    with pytest.raises(DeviceAuthorizationError):
        LocalDeviceAuthorizer(clone).authorize({"local:draft"})
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1


def test_actual_auth_center_h3_requests_bind_device_and_keep_recovery_paths(
    client, ring, pending_quote, monkeypatch, tmp_path
):
    import io
    import json
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    from app.models import User, GenerationTask
    from app.services.device_auth.models import WorkbenchDeviceOperation
    from jyd_probe.auth_center import AuthCenterClient, AuthCenterDeviceError
    from jyd_probe.device_authorization_routes import DeviceSessionRegistry
    from jyd_probe.device_business_transport import DeviceBusinessProofs

    legacy, quote, username = pending_quote
    with SessionLocal() as db:
        user = db.query(User).filter_by(username=username).one()
        user_id = user.id
    admin = create_user("client-business-admin", is_admin=True)
    identity = TestIdentity()
    registry = DeviceSessionRegistry(
        "http://testserver",
        session_factory=lambda user_id, login_token: DeviceAuthorizationSession(
            user_id=user_id,
            login_token=login_token,
            trust=TrustedIssuer(
                ring.config.origin, ring.config.environment, ring.verification_keys
            ),
            identity=identity,
            transport=ServerTransport(client),
        ),
    )
    session = registry.get(str(user_id), legacy)
    registration = session.register(label="test", client_version="v1")
    approve(admin, registration)
    with SessionLocal() as db:
        control = service.lock_control(db)
        control.mode = "ENFORCE"
        db.commit()
    calls = []

    class Response:
        def __init__(self, response):
            self.status, self.headers = response.status_code, response.headers
            self.stream = io.BytesIO(response.content)

        def read(self, *args):
            return self.stream.read(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def request(req, **_):
        path = urlsplit(req.full_url).path
        headers = dict(req.header_items())
        payload = json.loads(req.data) if req.data else None
        calls.append((path, headers, payload))
        response = client.request(req.method, path, headers=headers, json=payload)
        if response.status_code >= 400:
            raise HTTPError(
                req.full_url,
                response.status_code,
                "test denial",
                {},
                io.BytesIO(response.content),
            )
        return Response(response)

    monkeypatch.setattr("jyd_probe.auth_center.urlopen", request)
    monkeypatch.setattr("jyd_probe.auth_center._device_urlopen", request)
    transport = AuthCenterClient("http://testserver")
    transport.device_header_provider = DeviceBusinessProofs(
        registry, account_resolver=transport.verify
    )
    result = transport.confirm_h3_batch(legacy, quote["batch_id"])
    assert result["confirmed_at"]
    sent = [entry for entry in calls if entry[0].endswith("/confirm")]
    assert len(sent) == 1 and sent[0][1]["Authorization"].startswith("DPoP ")
    assert "access_token" not in sent[0][2]
    with SessionLocal() as db:
        operation = db.query(WorkbenchDeviceOperation).filter(
            WorkbenchDeviceOperation.operation_kind.like("h3.%")
        ).one()
        assert operation.grant_id == registration["grant_id"]
        original_task_ids = {row.id for row in db.query(GenerationTask).all()}
        grant = db.get(WorkbenchDeviceGrant, registration["grant_id"])
        service.change_grant(
            db,
            actor_id=admin.id,
            grant_id=grant.id,
            expected_revision=grant.revision,
            action="revoke",
            now=int(time.time()),
        )
        db.commit()
    calls.clear()
    with pytest.raises(AuthCenterDeviceError) as rejected:
        transport.prepare_h3_batch(legacy, {"request_key": "no-new-paid-work"})
    assert rejected.value.error_code == "DEVICE_REVOKED"
    assert (
        len(
            [
                entry
                for entry in calls
                if entry[0] == "/api/workbench/h3-batches/prepare"
            ]
        )
        == 1
    )
    assert (
        transport.get_h3_batch(legacy, quote["batch_id"])["batch_id"]
        == quote["batch_id"]
    )
    try:
        session.refresh(force=True)
    except DeviceAuthorizationError:
        pass
    # With no usable device credentials left, the account can still retrieve an
    # existing receipt/result. It cannot use this fallback to create a new task.
    calls.clear()
    assert (
        transport.confirm_h3_batch(legacy, quote["batch_id"])["batch_id"]
        == quote["batch_id"]
    )
    assert len([entry for entry in calls if entry[0].endswith("/confirm")]) == 1
    path = result["items"][0]["input_raw_cues_download_url"]
    target = tmp_path / "raw-cues.json"
    assert (
        transport._download(
            path,
            legacy,
            target,
            max_bytes=1024 * 1024,
            timeout_seconds=4,
            failure_message="download failed",
        )
        > 0
    )
    assert identity.created == 1
    with SessionLocal() as db:
        assert {row.id for row in db.query(GenerationTask).all()} == original_task_ids
        assert db.query(WorkbenchDeviceOperation).filter(
            WorkbenchDeviceOperation.operation_kind.like("h3.%")
        ).count() == 1
    registry.close()
