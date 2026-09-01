from __future__ import annotations

import secrets
import time

import jwt
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.services.device_auth import service
from app.services.device_auth.local_policy import POLICY_TYPE, POLICY_AUDIENCE
from app.services.device_auth.models import WorkbenchDevice, WorkbenchDeviceGrant
from app.services.device_auth.protocol import sha256_b64
from tests.conftest import create_user
from tests.test_device_auth import (
    ring,
    account_token,
    register,
    approve,
    exchange,
    device_key,
)

BASE = "/api/workbench/device-auth/local-policy"


@pytest.mark.parametrize("mode", ["OFF", "OBSERVE", "ENFORCE"])
def test_policy_is_signed_account_bound_and_does_not_register_device(
    client, ring, mode
):
    user = create_user("policy-" + mode.lower())
    token, nonce = account_token(user), secrets.token_urlsafe(32)
    with SessionLocal() as db:
        control = service.lock_control(db)
        control.mode, control.revision = mode, 4
        db.commit()
    response = client.post(
        BASE, json={"nonce": nonce}, headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"policy_token"}
    claims = jwt.decode(
        result["policy_token"],
        ring.signing_key.public_key(),
        algorithms=["ES256"],
        audience=POLICY_AUDIENCE,
        issuer=ring.config.issuer,
    )
    assert jwt.get_unverified_header(result["policy_token"])["typ"] == POLICY_TYPE
    assert claims["mode"] == mode and claims["control_revision"] == 4
    assert claims["user_id"] == user.id and claims["nonce"] == nonce
    assert claims["ath"] == sha256_b64(token)
    assert claims["exp"] - claims["iat"] == 300
    assert "grant_id" not in claims and "scopes" not in claims
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 0
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 0


def test_policy_rejects_missing_identity_injection_and_malformed_nonce(client, ring):
    user = create_user("policy-input")
    token = account_token(user)
    headers = {"Authorization": "Bearer " + token}
    assert client.post(BASE, json={"nonce": "A" * 43}).status_code == 401
    for payload in (
        {"nonce": "short"},
        {"nonce": "a" * 32 + "!"},
        {"nonce": "A" * 43, "mode": "OFF"},
        {"nonce": "A" * 43, "user_id": user.id + 1},
    ):
        assert client.post(BASE, json=payload, headers=headers).status_code == 422
    assert (
        client.post(
            BASE, json={"nonce": "A" * 43}, headers={**headers, "DPoP": "fake"}
        ).status_code
        == 401
    )


def test_policy_cannot_use_bound_token_without_proof_or_disabled_account(client, ring):
    user, admin, key = (
        create_user("policy-bound"),
        create_user("policy-admin", is_admin=True),
        device_key(),
    )
    legacy = account_token(user)
    registration = register(client, key, legacy).json()
    approve(admin, registration)
    bound = exchange(client, key, legacy).json()["access_token"]
    assert (
        client.post(
            BASE, json={"nonce": "A" * 43}, headers={"Authorization": "DPoP " + bound}
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        row = db.get(type(user), user.id)
        row.is_active = False
        db.commit()
    assert (
        client.post(
            BASE,
            json={"nonce": "A" * 43},
            headers={"Authorization": "Bearer " + legacy},
        ).status_code
        == 401
    )
