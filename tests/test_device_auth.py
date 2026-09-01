from __future__ import annotations

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import User
from app.routes import workbench_device_auth as routes
from app.services.device_auth import service
from app.services.device_auth import authentication
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.models import (
    WorkbenchDevice,
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceGrant,
    WorkbenchDeviceProofReplay,
)
from app.services.device_auth.protocol import (
    canonical_json,
    canonical_uri,
    jwk_thumbprint,
    public_jwk,
    sha256_b64,
    verify_proof,
)
from app.services.device_auth.tokens import (
    ACCESS_TYPE,
    LEASE_TYPE,
    DeviceAuthConfig,
    DeviceKeyRing,
)
from app.services.workbench_auth import issue_workbench_token
from tests.conftest import create_user, login

BASE = "/api/workbench/device-auth"


@pytest.fixture
def ring(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    config = DeviceAuthConfig("http://testserver", "test", "test-key-1", None, None)
    ring = DeviceKeyRing(config, {config.active_kid: private.public_key()}, private)
    monkeypatch.setattr(routes, "get_device_auth_config", lambda: config)
    monkeypatch.setattr(routes, "load_key_ring", lambda config, signing=False: ring)
    monkeypatch.setattr(authentication, "get_device_auth_config", lambda: config)
    monkeypatch.setattr(
        authentication, "load_key_ring", lambda config, signing=False: ring
    )
    return ring


def device_key():
    return ec.generate_private_key(ec.SECP256R1())


def account_token(user):
    return issue_workbench_token(user, get_settings())


def make_proof(key, *, access, nonce, path, method="POST", now=None, overrides=None):
    claims = {
        "jti": secrets.token_urlsafe(24),
        "htm": method,
        "htu": "http://testserver" + path,
        "iat": int(time.time()) if now is None else now,
        "ath": sha256_b64(access),
        "nonce": nonce,
    }
    claims.update(overrides or {})
    return jwt.encode(
        claims,
        key,
        algorithm="ES256",
        headers={"typ": "dpop+jwt", "jwk": public_jwk(key.public_key())},
    )


def auth_headers(
    client, key, access, purpose, *, path=None, method="POST", bound=False
):
    authorization = ("DPoP " if bound else "Bearer ") + access
    response = client.post(
        BASE + "/challenge",
        json={"public_jwk": public_jwk(key.public_key()), "purpose": purpose},
        headers={"Authorization": authorization},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": authorization,
        "DPoP": make_proof(
            key,
            access=access,
            nonce=response.json()["nonce"],
            path=path or BASE + "/" + purpose,
            method=method,
        ),
    }


def register(client, key, token, *, protection="tpm", version="v1", label="测试设备"):
    return client.post(
        BASE + "/register",
        json={"protection": protection, "client_version": version, "label": label},
        headers=auth_headers(client, key, token, "register"),
    )


def approve(admin, registration):
    with SessionLocal() as db:
        grant = service.change_grant(
            db,
            actor_id=admin.id,
            grant_id=registration["grant_id"],
            expected_revision=registration["revision"],
            action="approve",
            now=int(time.time()),
        )
        revision = grant.revision
        db.commit()
    return revision


def exchange(client, key, token):
    return client.post(
        BASE + "/exchange", headers=auth_headers(client, key, token, "exchange")
    )


def test_first_registration_pending_approval_and_versions_keep_identity(client, ring):
    user, admin = create_user("licensed"), create_user("license-admin", is_admin=True)
    key, token = device_key(), account_token(user)
    first = register(client, key, token)
    assert first.status_code == 200
    first = first.json()
    assert first["status"] == "PENDING"
    assert first["protection_verified"] is False
    assert exchange(client, key, token).json()["code"] == "DEVICE_PENDING"
    approved_revision = approve(admin, first)
    for version in ("v2", "v3", "v4"):
        repeat = register(client, key, token, version=version).json()
        assert repeat["device_id"] == first["device_id"]
        assert repeat["grant_id"] == first["grant_id"]
        assert repeat["revision"] == approved_revision
        assert repeat["status"] == "ACTIVE"
    credentials = exchange(client, key, token)
    assert credentials.status_code == 200, credentials.text
    value = credentials.json()
    assert value["token_type"] == "DPoP"
    assert value["expires_in"] == 1800
    access = ring.verify(value["access_token"], typ=ACCESS_TYPE, now=int(time.time()))
    lease = ring.verify(value["local_lease"], typ=LEASE_TYPE, now=int(time.time()))
    assert access["cnf"]["jkt"] == jwk_thumbprint(public_jwk(key.public_key()))
    assert lease["device_id"] == access["device_id"] == first["device_id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 1
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1


def test_copied_token_cannot_be_used_with_another_device_key(client, ring):
    user, admin = create_user("copy-user"), create_user("copy-admin", is_admin=True)
    original, copy = device_key(), device_key()
    token = account_token(user)
    registration = register(client, original, token).json()
    approve(admin, registration)
    access = exchange(client, original, token).json()["access_token"]
    response = client.post(
        BASE + "/challenge",
        json={"public_jwk": public_jwk(copy.public_key()), "purpose": "refresh"},
        headers={"Authorization": "DPoP " + access},
    )
    assert response.status_code == 401
    headers = auth_headers(client, original, access, "refresh", bound=True)
    claims = jwt.decode(headers["DPoP"], options={"verify_signature": False})
    headers["DPoP"] = make_proof(
        copy, access=access, nonce=claims["nonce"], path=BASE + "/refresh"
    )
    response = client.post(BASE + "/refresh", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_DEVICE_PROOF"
    assert exchange(client, copy, token).json()["code"] == "DEVICE_UNREGISTERED"


def test_nonce_replay_and_cross_account_proof_are_rejected(client, ring):
    user, other = create_user("nonce-user"), create_user("nonce-other")
    key, token = device_key(), account_token(user)
    headers = auth_headers(client, key, token, "register")
    payload = {"protection": "tpm"}
    assert (
        client.post(BASE + "/register", json=payload, headers=headers).status_code
        == 200
    )
    assert (
        client.post(BASE + "/register", json=payload, headers=headers).status_code
        == 401
    )
    headers = auth_headers(client, key, token, "register")
    nonce = jwt.decode(headers["DPoP"], options={"verify_signature": False})["nonce"]
    other_token = account_token(other)
    headers = {
        "Authorization": "Bearer " + other_token,
        "DPoP": make_proof(
            key, access=other_token, nonce=nonce, path=BASE + "/register"
        ),
    }
    assert (
        client.post(BASE + "/register", json=payload, headers=headers).status_code
        == 401
    )


def test_revocation_refresh_and_shared_device_grants_remain_independent(client, ring):
    user, other, admin = (
        create_user("shared-a"),
        create_user("shared-b"),
        create_user("shared-admin", is_admin=True),
    )
    key = device_key()
    token, other_token = account_token(user), account_token(other)
    first, second = (
        register(client, key, token).json(),
        register(client, key, other_token).json(),
    )
    assert first["device_id"] == second["device_id"]
    assert first["grant_id"] != second["grant_id"]
    revision = approve(admin, first)
    approve(admin, second)
    access = exchange(client, key, token).json()["access_token"]
    with SessionLocal() as db:
        service.change_grant(
            db,
            actor_id=admin.id,
            grant_id=first["grant_id"],
            expected_revision=revision,
            action="revoke",
            now=int(time.time()),
        )
        db.commit()
    response = client.post(
        BASE + "/refresh",
        headers=auth_headers(client, key, access, "refresh", bound=True),
    )
    assert response.status_code == 403 and response.json()["code"] == "DEVICE_REVOKED"
    assert exchange(client, key, other_token).status_code == 200
    assert register(client, key, token).json()["status"] == "REVOKED"


def test_software_requires_explicit_policy_even_for_admin(client, ring):
    admin = create_user("software-admin", is_admin=True)
    key, token = device_key(), account_token(admin)
    registration = register(client, key, token, protection="software").json()
    with pytest.raises(DeviceAuthError, match="软件保护"):
        approve(admin, registration)
    with SessionLocal() as db:
        service.update_policy(
            db,
            actor_id=admin.id,
            user_id=admin.id,
            max_devices=1,
            allow_software=True,
            now=int(time.time()),
        )
        db.commit()
    approve(admin, registration)
    assert exchange(client, key, token).status_code == 200


def test_concurrent_approvals_cannot_exceed_quota_and_replace_is_atomic(client, ring):
    user, admin = create_user("quota-user"), create_user("quota-admin", is_admin=True)
    token = account_token(user)
    rows = [register(client, device_key(), token).json() for _ in range(2)]

    def attempt(row):
        try:
            approve(admin, row)
            return "approved"
        except DeviceAuthError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, rows))
    assert sorted(results) == ["DEVICE_QUOTA_EXCEEDED", "approved"]
    with SessionLocal() as db:
        records = db.scalars(
            select(WorkbenchDeviceGrant).where(WorkbenchDeviceGrant.user_id == user.id)
        ).all()
        old = next(g for g in records if g.status == "ACTIVE")
        new = next(g for g in records if g.status == "PENDING")
        old_id, new_id, old_revision, new_revision = (
            old.id,
            new.id,
            old.revision,
            new.revision,
        )
        service.replace_grant(
            db,
            actor_id=admin.id,
            old_grant_id=old_id,
            new_grant_id=new_id,
            old_revision=old_revision,
            new_revision=new_revision,
            now=int(time.time()),
        )
        db.commit()
    with SessionLocal() as db:
        assert db.get(WorkbenchDeviceGrant, old_id).status == "REVOKED"
        assert db.get(WorkbenchDeviceGrant, new_id).status == "ACTIVE"


def test_failed_replacement_rolls_back_old_authorization(client, ring):
    user, admin = create_user("replace-user"), create_user(
        "replace-admin", is_admin=True
    )
    token = account_token(user)
    old = register(client, device_key(), token).json()
    revision = approve(admin, old)
    new = register(client, device_key(), token, protection="software").json()
    with SessionLocal() as db:
        with pytest.raises(DeviceAuthError):
            service.replace_grant(
                db,
                actor_id=admin.id,
                old_grant_id=old["grant_id"],
                new_grant_id=new["grant_id"],
                old_revision=revision,
                new_revision=new["revision"],
                now=int(time.time()),
            )
        db.rollback()
    with SessionLocal() as db:
        assert db.get(WorkbenchDeviceGrant, old["grant_id"]).status == "ACTIVE"
        assert db.get(WorkbenchDeviceGrant, new["grant_id"]).status == "PENDING"


@pytest.mark.parametrize(
    "overrides",
    [
        {"htm": "GET"},
        {"htu": "http://evil.example/api"},
        {"htu": "http://testserver/protected?x=1"},
        {"ath": "wrong"},
        {"iat": True},
        {"iat": 1},
        {"nonce": ""},
        {"jti": ""},
    ],
)
def test_proof_rejects_invalid_claims(overrides):
    key = device_key()
    proof = make_proof(
        key,
        access="test-token",
        nonce=secrets.token_urlsafe(32),
        path="/protected",
        overrides=overrides,
    )
    with pytest.raises(DeviceAuthError):
        verify_proof(
            proof,
            method="POST",
            uri="http://testserver/protected",
            access_token="test-token",
            now=int(time.time()),
        )


def test_proof_rejects_wrong_type_private_jwk_future_clock_and_signature():
    key = device_key()
    proof = make_proof(
        key, access="test-token", nonce=secrets.token_urlsafe(32), path="/protected"
    )
    claims = jwt.decode(proof, options={"verify_signature": False})
    jwk = public_jwk(key.public_key())
    for header in (
        {"typ": "JWT", "jwk": jwk},
        {"typ": "dpop+jwt", "jwk": {**jwk, "d": "private"}},
        {"typ": "dpop+jwt", "jwk": jwk, "jku": "http://evil.example/keys"},
    ):
        forged = jwt.encode(claims, key, algorithm="ES256", headers=header)
        with pytest.raises(DeviceAuthError):
            verify_proof(
                forged,
                method="POST",
                uri="http://testserver/protected",
                access_token="test-token",
                now=int(time.time()),
            )
    with pytest.raises(DeviceAuthError):
        verify_proof(
            proof,
            method="POST",
            uri="http://testserver/protected",
            access_token="test-token",
            now=claims["iat"] - 10,
        )


def test_uri_normalization_retains_significant_segments():
    assert (
        canonical_uri("https://EXAMPLE.com:443/a/../%62//c/.")
        == "https://example.com/b//c/"
    )
    assert canonical_uri("https://example.com/a%2fb") == "https://example.com/a%2Fb"
    assert canonical_uri("https://example.com") == "https://example.com/"


def test_admin_page_permissions_csrf_and_stale_revision(client, ring):
    user, admin = create_user("page-user"), create_user("page-admin", is_admin=True)
    registration = register(
        client, device_key(), account_token(user), label="<script>bad</script>"
    ).json()
    login(client, user.username)
    assert client.get("/admin/workbench-devices").status_code == 403
    login(client, admin.username)
    page = client.get("/admin/workbench-devices")
    assert page.status_code == 200
    assert "&lt;script&gt;bad&lt;/script&gt;" in page.text
    path = f"/admin/workbench-devices/{registration['grant_id']}/approve"
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(path, data={"revision": 1}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (
        client.post(path, data={"revision": 1}, follow_redirects=False).status_code
        == 303
    )
    assert (
        client.post(path, data={"revision": 1}, follow_redirects=False).status_code
        == 409
    )


def test_audit_does_not_store_access_tokens_or_private_material(client, ring):
    user = create_user("audit-user")
    token = account_token(user)
    register(client, device_key(), token)
    with SessionLocal() as db:
        audits = db.scalars(select(WorkbenchDeviceAuditEvent)).all()
        assert len(audits) == 1
        assert audits[0].action == "device.requested"
        assert audits[0].details_json == "{}"
        assert token not in repr([vars(event) for event in audits])
        assert (
            db.scalar(select(func.count()).select_from(WorkbenchDeviceProofReplay)) == 1
        )


def test_leading_zero_coordinate_is_canonical_fixed_width():
    # Deterministic key whose coordinate starts with zero; no rare/random-only test.
    for scalar in range(1, 1000):
        key = ec.derive_private_key(scalar, ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        if numbers.x.bit_length() <= 248 or numbers.y.bit_length() <= 248:
            jwk = public_jwk(key.public_key())
            assert len(jwk["x"]) == len(jwk["y"]) == 43
            assert len(jwk_thumbprint(jwk)) == 43
            break
    else:
        pytest.fail("fixture did not find the expected leading-zero coordinate")


def test_request_nonce_reuse_with_fresh_jti_and_replay_across_sessions(client, ring):
    user, key = create_user("persistent-proof"), device_key()
    token, now = account_token(user), int(time.time())
    with SessionLocal() as db:
        nonce = service.issue_challenge(
            db,
            user_id=user.id,
            thumbprint=jwk_thumbprint(public_jwk(key.public_key())),
            purpose="request",
            now=now,
        )
        db.commit()
    raw = make_proof(key, access=token, nonce=nonce, path="/protected", now=now)
    proof = verify_proof(
        raw,
        method="POST",
        uri="http://testserver/protected",
        access_token=token,
        now=now,
    )
    with SessionLocal() as db:
        service.consume_proof(
            db, user_id=user.id, proof=proof, purpose="request", now=now
        )
        db.commit()
    with SessionLocal() as db:
        with pytest.raises(DeviceAuthError) as exc:
            service.consume_proof(
                db, user_id=user.id, proof=proof, purpose="request", now=now
            )
        assert exc.value.code == "DEVICE_PROOF_REPLAY"
        db.rollback()
    fresh = verify_proof(
        make_proof(key, access=token, nonce=nonce, path="/protected", now=now),
        method="POST",
        uri="http://testserver/protected",
        access_token=token,
        now=now,
    )
    with SessionLocal() as db:
        service.consume_proof(
            db, user_id=user.id, proof=fresh, purpose="request", now=now
        )
        db.commit()
    with SessionLocal() as db:
        late = verify_proof(
            make_proof(
                key, access=token, nonce=nonce, path="/protected", now=now + 121
            ),
            method="POST",
            uri="http://testserver/protected",
            access_token=token,
            now=now + 121,
        )
        with pytest.raises(DeviceAuthError) as exc:
            service.consume_proof(
                db, user_id=user.id, proof=late, purpose="request", now=now + 121
            )
        assert exc.value.code == "USE_DPOP_NONCE"


def test_signing_key_overlap_and_lease_access_separation(client, ring):
    user, admin = create_user("rotation-user"), create_user(
        "rotation-admin", is_admin=True
    )
    key, token = device_key(), account_token(user)
    registration = register(client, key, token).json()
    approve(admin, registration)
    credentials = exchange(client, key, token).json()
    now = int(time.time())
    with pytest.raises(DeviceAuthError):
        ring.verify(credentials["local_lease"], typ=ACCESS_TYPE, now=now)
    with pytest.raises(DeviceAuthError):
        ring.verify(credentials["access_token"], typ=LEASE_TYPE, now=now)
    new_key = device_key()
    new_config = DeviceAuthConfig(ring.config.origin, "test", "test-key-2", None, None)
    overlap = DeviceKeyRing(
        new_config,
        {**ring.verification_keys, "test-key-2": new_key.public_key()},
        new_key,
    )
    claims = overlap.verify(credentials["access_token"], typ=ACCESS_TYPE, now=now)
    assert claims["grant_id"] == registration["grant_id"]
    rotated = overlap.sign(claims, typ=ACCESS_TYPE)
    assert (
        overlap.verify(rotated, typ=ACCESS_TYPE, now=now)["device_id"]
        == registration["device_id"]
    )
    with pytest.raises(DeviceAuthError):
        ring.verify(rotated, typ=ACCESS_TYPE, now=now)
    with pytest.raises(DeviceAuthError):
        overlap.verify(rotated, typ=ACCESS_TYPE, now=claims["exp"])


def test_disabled_account_and_password_change_cannot_refresh(client, ring):
    from app.services.security import hash_password

    user, admin = create_user("disabled-user"), create_user(
        "disabled-admin", is_admin=True
    )
    key, token = device_key(), account_token(user)
    registration = register(client, key, token).json()
    approve(admin, registration)
    access = exchange(client, key, token).json()["access_token"]
    headers = auth_headers(client, key, access, "refresh", bound=True)
    with SessionLocal() as db:
        db.get(User, user.id).password_hash = hash_password("another-password")
        db.commit()
    assert (
        client.post(BASE + "/refresh", headers=headers).json()["code"]
        == "LOGIN_REQUIRED"
    )
    with SessionLocal() as db:
        current = db.get(User, user.id)
        current.is_active = False
        replacement = account_token(current)
        db.commit()
    response = client.post(
        BASE + "/challenge",
        json={"public_jwk": public_jwk(key.public_key()), "purpose": "status"},
        headers={"Authorization": "Bearer " + replacement},
    )
    assert response.status_code == 401
