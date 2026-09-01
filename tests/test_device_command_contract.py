"""Real command-login/JYD/server contract; no native keys, rendering or network."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import User
from app.services.device_auth import service
from app.services.device_auth.models import WorkbenchDevice, WorkbenchDeviceGrant
from tests.conftest import create_user
from tests.test_device_auth import ring, account_token
from tests.test_device_auth_client_contract import (
    activated,
    TestIdentity,
    ServerTransport,
    DeviceAuthorizationSession,
    TrustedIssuer,
)
from jyd_probe import device_command_authorization as commands, device_trust_roots
from jyd_probe.device_auth_protocol import DeviceAuthorizationError
from jyd_probe.device_local_execution import (
    current_local_decision,
    current_local_authorizer,
)


class Reply(io.BytesIO):
    def __init__(self, response, uri):
        super().__init__(response.content)
        self.status, self.uri = response.status_code, uri

    def geturl(self):
        return self.uri


class AccountOpener:
    def __init__(self, client):
        self.client, self.paths = client, []

    def open(self, request, timeout):
        path = urlsplit(request.full_url).path
        self.paths.append(path)
        response = self.client.request(
            request.get_method(),
            path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        return Reply(response, request.full_url)


def wired(client, ring, monkeypatch, identity):
    trust = TrustedIssuer(
        ring.config.origin, ring.config.environment, ring.verification_keys
    )
    opener = AccountOpener(client)
    account_client = commands.CommandAccountClient(trust, opener=opener)
    sessions, transports = [], []

    def factory(**kw):
        transport = ServerTransport(client)
        session = DeviceAuthorizationSession(**kw, transport=transport)
        sessions.append(session)
        transports.append(transport)
        return session

    monkeypatch.setattr(device_trust_roots, "TRUSTED_ISSUERS", ({"test-only": True},))
    monkeypatch.setattr(commands, "bundled_trust", lambda url: trust)
    monkeypatch.setattr(commands, "CommandAccountClient", lambda trust: account_client)
    monkeypatch.setattr(commands, "MachineDeviceIdentity", lambda: identity)
    monkeypatch.setattr(commands.DeviceLeaseCache, "for_machine", lambda: None)
    monkeypatch.setattr(commands, "DeviceAuthorizationSession", factory)
    return account_client, sessions, transports, opener


def mode(value):
    with SessionLocal() as db:
        control = service.lock_control(db)
        control.mode = value
        control.revision += 1
        db.commit()


def stdin_account(monkeypatch, token):
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    return SimpleNamespace(device_token_stdin=True)


def test_command_login_and_four_fresh_sessions_reuse_one_approved_device(
    client, ring, monkeypatch
):
    user, _, identity, original, registration = activated(client, ring)
    original.close()
    mode("ENFORCE")
    account_client, sessions, transports, opener = wired(
        client, ring, monkeypatch, identity
    )
    logged_in = account_client.login(user.username, "password123")
    for _version in ("v1", "v2", "v3", "v4"):
        args = stdin_account(monkeypatch, logged_in.token)
        with commands.command_authorization(args, server_url=ring.config.origin):
            decision = current_local_decision({"local:draft", "local:render"})
            assert decision.user_id == user.id
            assert decision.device_id == registration["device_id"]
            assert decision.grant_id == registration["grant_id"]
        assert sessions[-1]._closed and current_local_authorizer() is None
    assert identity.created == 1
    assert len(sessions) == 4
    assert opener.paths == ["/api/auth/center/login"] + ["/api/auth/center/verify"] * 4
    assert not any(
        path.endswith("/register")
        for transport in transports
        for path in transport.paths
    )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 1
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1


@pytest.mark.parametrize("other_account", [False, True])
def test_copied_token_or_other_account_cannot_inherit_command_authorization(
    client, ring, monkeypatch, other_account
):
    user, _, identity, original, _ = activated(client, ring)
    original.close()
    mode("ENFORCE")
    if other_account:
        user = create_user("other-command-account")
    else:
        identity = TestIdentity()  # Simulated different machine, no persisted key.
    _, sessions, _, _ = wired(client, ring, monkeypatch, identity)
    with pytest.raises(DeviceAuthorizationError):
        with commands.command_authorization(
            stdin_account(monkeypatch, account_token(user))
        ):
            current_local_decision({"local:render"})
    assert all(session._closed for session in sessions)
    assert identity.created == (1 if other_account else 0)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1


@pytest.mark.parametrize("state", ["PENDING", "REVOKED", "SUSPENDED"])
def test_command_does_not_resume_an_unapproved_or_stopped_device(
    client, ring, monkeypatch, state
):
    user, _, identity, original, registration = activated(client, ring)
    original.close()
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, registration["grant_id"])
        grant.status = state
        grant.revision += 1
        db.commit()
    mode("ENFORCE")
    _, sessions, _, _ = wired(client, ring, monkeypatch, identity)
    with pytest.raises(DeviceAuthorizationError):
        with commands.command_authorization(
            stdin_account(monkeypatch, account_token(user))
        ):
            current_local_decision({"local:draft"})
    assert sessions[-1]._closed and identity.created == 1


@pytest.mark.parametrize("rollout_mode", ["OFF", "OBSERVE"])
def test_signed_rollout_allows_command_without_automatic_registration(
    client, ring, monkeypatch, rollout_mode
):
    user, identity = create_user("command-rollout"), TestIdentity()
    mode(rollout_mode)
    wired(client, ring, monkeypatch, identity)
    with commands.command_authorization(
        stdin_account(monkeypatch, account_token(user))
    ):
        decision = current_local_decision({"local:draft"})
        assert decision.mode == rollout_mode and decision.user_id == user.id
    assert identity.created == 0
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 0


def test_disabled_website_account_is_rejected_before_a_machine_session(
    client, ring, monkeypatch
):
    user = create_user("command-disabled")
    token = account_token(user)
    with SessionLocal() as db:
        db.get(User, user.id).is_active = False
        db.commit()
    identity = TestIdentity()
    _, sessions, _, _ = wired(client, ring, monkeypatch, identity)
    with pytest.raises(DeviceAuthorizationError) as error:
        with commands.command_authorization(stdin_account(monkeypatch, token)):
            pytest.fail("disabled account executed")
    assert error.value.code == "LOGIN_REQUIRED"
    assert sessions == [] and identity.created == 0
