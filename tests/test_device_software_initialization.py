"""Software bootstrap permission using real RH/JYD code, never native keys.

Only the isolated test database and ephemeral cryptography keys are used. The
permit is NOT a device grant, and its local one-shot object is not global replay
protection for a future helper transport.
"""

from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager, nullcontext
import json
import time

import jwt
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import GenerationTask, User
from app.services.device_auth import service
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.initialization import (
    PERMIT_AUDIENCE,
    PERMIT_TYPE,
    issue_software_initialization_permit,
)
from app.services.device_auth.models import (
    WorkbenchDevice,
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceGrant,
    WorkbenchDeviceOperation,
)
from app.services.device_auth.protocol import sha256_b64
from tests.conftest import create_user
from tests.test_device_auth import (
    account_token,
    approve,
    auth_headers,
    device_key,
    exchange,
    register,
    ring,
)
from tests.test_device_auth_client_contract import (
    ServerTransport,
    TestIdentity,
    TestKey,
    new_session,
)
from jyd_probe.device_auth_protocol import DeviceAuthorizationError, TrustedIssuer
from jyd_probe.device_identity_store import MachineDeviceIdentity
from jyd_probe.device_identity_setup import (
    DeviceSetupCoordinator,
    InteractiveWindowsDeviceIdentity,
    dispatch_setup_helper,
    HELPER_FLAG,
)
from jyd_probe.device_identity_windows import DeviceIdentityError
from jyd_probe import device_software_initialization as software_client
from jyd_probe.device_software_initialization import (
    PERMIT_PATH,
    SoftwareInitializationContext,
    request_software_initialization_permit,
    verify_software_initialization_permit,
)


def context():
    return SoftwareInitializationContext(
        1234, 133123456789012345, "S-1-5-21-1-2-3-1001"
    )


def set_permission(user, allowed=True, *, mode=None):
    with SessionLocal() as db:
        control = service.lock_control(db)
        if mode is not None:
            control.mode = mode
        policy = service.policy_for(db, user.id)
        policy.allow_software = allowed
        policy.revision += 1
        db.commit()


def request(client, user, ctx=None, *, headers=None, payload=None):
    ctx = ctx or context()
    return client.post(
        PERMIT_PATH,
        json=payload or {"nonce": ctx.nonce, "context_hash": ctx.context_hash},
        headers=headers or {"Authorization": "Bearer " + account_token(user)},
    )


def assert_no_business_state():
    with SessionLocal() as db:
        for model in (
            WorkbenchDevice,
            WorkbenchDeviceGrant,
            WorkbenchDeviceOperation,
            GenerationTask,
        ):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("mode", ["OFF", "OBSERVE", "ENFORCE"])
def test_server_requires_explicit_policy_in_every_mode(client, ring, mode):
    user = create_user("software-" + mode.lower())
    set_permission(user, False, mode=mode)
    response = request(client, user)
    assert response.status_code == 403
    assert response.json()["code"] == "DEVICE_SOFTWARE_NOT_ALLOWED"
    assert_no_business_state()
    with SessionLocal() as db:
        assert (
            db.scalar(select(func.count()).select_from(WorkbenchDeviceAuditEvent)) == 0
        )


@pytest.mark.parametrize("mode", ["OFF", "OBSERVE", "ENFORCE"])
def test_server_permit_bound_to_account_context_and_purpose_only(client, ring, mode):
    user, ctx = create_user("software-allowed"), context()
    set_permission(user, mode=mode)
    response = request(client, user, ctx)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"initialization_permit"}
    permit = response.json()["initialization_permit"]
    claims = jwt.decode(
        permit,
        ring.signing_key.public_key(),
        algorithms=["ES256"],
        audience=PERMIT_AUDIENCE,
        issuer=ring.config.issuer,
    )
    assert jwt.get_unverified_header(permit)["typ"] == PERMIT_TYPE
    assert claims["user_id"] == user.id and claims["sub"] == str(user.id)
    submitted_token = response.request.headers["authorization"].removeprefix("Bearer ")
    assert claims["ath"] == sha256_b64(submitted_token)
    assert claims["nonce"] == ctx.nonce
    assert claims["context_hash"] == ctx.context_hash
    assert claims["software_allowed"] is True
    assert claims["action"] == "initialize-software-key"
    assert claims["exp"] - claims["iat"] == 120
    assert claims["nbf"] == claims["iat"]
    assert not {"device_id", "grant_id", "scopes"}.intersection(claims)
    assert_no_business_state()
    with SessionLocal() as db:
        audit = db.scalar(select(WorkbenchDeviceAuditEvent))
        assert audit.action == "device.software_initialization_permitted"
        assert audit.actor_user_id == audit.subject_user_id == user.id
        assert audit.device_id is None and audit.grant_id is None
        assert set(json.loads(audit.details_json)) == {
            "policy_revision",
            "context_hash",
            "expires_at",
        }
        assert ctx.operator_sid not in audit.details_json
        assert ctx.nonce not in audit.details_json
        assert permit not in audit.details_json


@pytest.mark.parametrize(
    "changed",
    [
        {"nonce": "short"},
        {"nonce": True},
        {"context_hash": "A" * 42},
        {"context_hash": "!" * 43},
        {"software_allowed": True},
        {"user_id": 999},
        {"operator_sid": "S-1-5-18"},
        {"device_id": "fake"},
    ],
)
def test_server_strict_body_cannot_inject_approval_or_identity(client, ring, changed):
    user, ctx = create_user("software-input"), context()
    set_permission(user)
    response = request(
        client,
        user,
        payload={"nonce": ctx.nonce, "context_hash": ctx.context_hash, **changed},
    )
    assert response.status_code == 422
    assert_no_business_state()


def test_permission_not_shared_between_accounts_and_revoke_applies_next_request(
    client, ring
):
    user, other = create_user("software-one"), create_user("software-two")
    set_permission(user)
    assert request(client, user).status_code == 200
    assert request(client, other).status_code == 403
    set_permission(user, False)
    assert request(client, user).json()["code"] == "DEVICE_SOFTWARE_NOT_ALLOWED"
    assert_no_business_state()


def test_missing_disabled_and_mixed_account_credentials_rejected(client, ring):
    user, ctx = create_user("software-auth"), context()
    set_permission(user)
    payload = {"nonce": ctx.nonce, "context_hash": ctx.context_hash}
    assert client.post(PERMIT_PATH, json=payload).status_code == 401
    assert (
        request(
            client,
            user,
            headers={
                "Authorization": "Bearer " + account_token(user),
                "DPoP": "not-a-proof",
            },
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        db.get(User, user.id).is_active = False
        db.commit()
    assert request(client, user).status_code == 401
    assert_no_business_state()


def test_bound_token_requires_current_proof_and_permit_is_not_cloud_token(client, ring):
    user, admin, key = (
        create_user("software-bound"),
        create_user("software-admin", is_admin=True),
        device_key(),
    )
    token = account_token(user)
    registration = register(client, key, token).json()
    approve(admin, registration)
    bound = exchange(client, key, token).json()["access_token"]
    set_permission(user)
    assert (
        request(client, user, headers={"Authorization": "DPoP " + bound}).status_code
        == 401
    )
    headers = auth_headers(client, key, bound, "request", path=PERMIT_PATH, bound=True)
    permit_response = request(client, user, headers=headers)
    assert permit_response.status_code == 200, permit_response.text
    assert request(client, user, headers=headers).status_code == 401  # proof replay
    permit = permit_response.json()["initialization_permit"]
    assert (
        request(client, user, headers={"Authorization": "DPoP " + permit}).status_code
        == 401
    )
    assert (
        request(client, user, headers={"Authorization": "Bearer " + permit}).status_code
        == 401
    )


def test_signing_failure_does_not_commit_a_success_audit(client, ring, monkeypatch):
    user = create_user("software-signing-failure")
    set_permission(user)

    def failed_sign(*args, **kwargs):
        raise DeviceAuthError(
            "DEVICE_AUTH_NOT_CONFIGURED", "test signing unavailable", 503
        )

    monkeypatch.setattr(type(ring), "sign", failed_sign)
    assert request(client, user).status_code == 503
    assert_no_business_state()
    with SessionLocal() as db:
        assert (
            db.scalar(select(func.count()).select_from(WorkbenchDeviceAuditEvent)) == 0
        )


class Clock:
    def __init__(self):
        self.wall, self.mono = time.time(), 100.0


@pytest.fixture
def signed(client, ring):
    user, ctx, clock = create_user("software-client"), context(), Clock()
    set_permission(user)
    token = account_token(user)
    with SessionLocal() as db:
        permit = issue_software_initialization_permit(
            db,
            ring,
            user_id=user.id,
            account_token=token,
            nonce=ctx.nonce,
            context_hash=ctx.context_hash,
            now=int(clock.wall),
        )["initialization_permit"]
        db.commit()
    trust = TrustedIssuer(
        ring.config.origin, ring.config.environment, ring.verification_keys
    )
    return user, ctx, clock, token, permit, trust


def verify(signed, permit=None, **overrides):
    user, ctx, clock, token, original, trust = signed
    kwargs = dict(
        context=ctx, user_id=user.id, account_hash=sha256_b64(token), now=clock.wall
    )
    kwargs.update(overrides)
    return verify_software_initialization_permit(
        trust, original if permit is None else permit, **kwargs
    )


def test_real_session_permit_neither_initializes_nor_registers(client, ring):
    user, ctx, identity = create_user("software-session"), context(), TestIdentity()
    set_permission(user)
    session = new_session(client, ring, user, identity)
    permit = session.software_initialization_permit(context=ctx)
    assert identity.created == 0
    assert session._transport.paths == [PERMIT_PATH]
    assert "jwt" not in repr(permit) and "S-1-5" not in repr(permit)
    assert session.summary()["state"] != "ACTIVE"
    assert_no_business_state()
    with pytest.raises(DeviceAuthorizationError):
        session.require_local("local:render")
    encoded = permit.consume_for_initializer(ctx)
    assert jwt.get_unverified_header(encoded)["typ"] == PERMIT_TYPE
    with pytest.raises(DeviceAuthorizationError) as caught:
        permit.consume_for_initializer(ctx)
    assert caught.value.code == "SOFTWARE_INITIALIZATION_ALREADY_USED"
    session.close()
    with pytest.raises(DeviceAuthorizationError) as caught:
        session.software_initialization_permit(context=ctx)
    assert caught.value.code == "LOGIN_REQUIRED"


@pytest.mark.parametrize(
    "changed",
    [
        {"schema": "publicvideo.local-policy.v1"},
        {"action": "generate"},
        {"aud": "PublicVideoWorkbench:cloud"},
        {"environment": "production"},
        {"iss": "https://attacker.example"},
        {"software_allowed": 1},
        {"software_allowed": False},
        {"user_id": True},
        {"sub": "999"},
        {"ath": "A" * 43},
        {"nonce": "B" * 43},
        {"context_hash": "C" * 43},
        {"grant_id": "injected"},
        {"policy_revision": 0},
        {"iat": True},
        {"jti": "short"},
        {"scopes": ["cloud:generate"]},
    ],
)
def test_client_rejects_signed_but_wrong_claims(signed, ring, changed):
    claims = dict(verify(signed))
    claims.update(changed)
    with pytest.raises(DeviceAuthorizationError) as caught:
        verify(signed, ring.sign(claims, typ=PERMIT_TYPE))
    assert caught.value.code == "INVALID_SOFTWARE_INITIALIZATION_PERMIT"


@pytest.mark.parametrize(
    "kind",
    ["wrong-purpose", "wrong-key", "too-long", "future", "expired", "not-before"],
)
def test_client_enforces_signature_purpose_and_time(signed, ring, kind):
    claims = dict(verify(signed))
    typ = PERMIT_TYPE
    if kind == "wrong-purpose":
        typ = "workbench-lease+jwt"
    elif kind == "too-long":
        claims["exp"] = claims["iat"] + 121
    elif kind == "future":
        claims["nbf"] = claims["iat"] = int(signed[2].wall) + 20
    elif kind == "expired":
        claims["exp"] = int(signed[2].wall)
    elif kind == "not-before":
        claims["nbf"] += 1
    permit = ring.sign(claims, typ=typ)
    if kind == "wrong-key":
        permit = jwt.encode(
            claims,
            device_key(),
            algorithm="ES256",
            headers={"typ": typ, "kid": ring.config.active_kid},
        )
    with pytest.raises(DeviceAuthorizationError):
        verify(signed, permit)


@pytest.mark.parametrize(
    "changed",
    [
        {"process_id": 4321},
        {"creation_time": 133123456789012346},
        {"operator_sid": "S-1-5-21-1-2-3-1002"},
        {"nonce": "Z" * 43},
    ],
)
def test_client_rejects_context_substitution(signed, changed):
    with pytest.raises(DeviceAuthorizationError):
        verify(signed, context=replace(signed[1], **changed))


@pytest.mark.parametrize(
    "scenario",
    ["expires", "wall-back", "mono-back", "nan", "slow-network", "other-context"],
)
def test_in_memory_permit_does_not_extend_after_delay_or_clock_changes(
    client, ring, signed, scenario
):
    user, ctx, clock, token, _, trust = signed

    class DelayedTransport(ServerTransport):
        def request(self, **kwargs):
            result = super().request(**kwargs)
            if scenario == "slow-network":
                clock.wall += 121
                clock.mono += 121
            return result

    def obtain():
        return request_software_initialization_permit(
            trust=trust,
            transport=DelayedTransport(client),
            user_id=user.id,
            account_token=token,
            context=ctx,
            wall_clock=lambda: clock.wall,
            monotonic_clock=lambda: clock.mono,
        )

    if scenario == "slow-network":
        with pytest.raises(DeviceAuthorizationError):
            obtain()
        return
    permit = obtain()
    if scenario == "expires":
        clock.wall += 121
        clock.mono += 121
    elif scenario == "wall-back":
        clock.wall -= 10
    elif scenario == "mono-back":
        clock.mono -= 1
    elif scenario == "nan":
        clock.mono = float("nan")
    elif scenario == "other-context":
        ctx = replace(ctx, process_id=5678)
    with pytest.raises(DeviceAuthorizationError) as caught:
        permit.consume_for_initializer(ctx)
    assert caught.value.code == "SOFTWARE_INITIALIZATION_EXPIRED"


def test_returned_claims_are_immutable(signed):
    claims = verify(signed)
    with pytest.raises(TypeError):
        claims["software_allowed"] = False


class MemoryMachine:
    """CNG-like provider inventory with ephemeral cryptography keys, no Windows."""

    def __init__(self):
        self.keys = {"tpm": None, "software": None}
        self.record = None
        self.creates = 0

    def read(self):
        return self.record

    def remember(self, record):
        assert self.record is None or self.record == record
        self.record = record

    def provider(self, *, protection):
        machine = self

        class Key(TestKey):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        class Provider:
            def open_existing(self):
                raw = machine.keys[protection]
                if raw is None:
                    return None
                key = Key(raw)
                key.protection = protection
                return key

            def initialize_for_activation(self, **kwargs):
                assert machine.keys[protection] is None
                machine.keys[protection] = device_key()
                machine.creates += 1
                return self.open_existing()

        return Provider()

    def identity(self):
        return MachineDeviceIdentity(
            store=self, identity_factory=self.provider, lock_factory=nullcontext
        )


def software_bootstrap(
    client, ring, user, monkeypatch, *, invalid_handoff=False, expires_after_lock=False
):
    """Real session/helper/managed-key/server code; only UAC/IPC/CNG are simulated."""
    machine, ctx = MemoryMachine(), context()
    channel_holder = []
    trust = TrustedIssuer(
        ring.config.origin, ring.config.environment, ring.verification_keys
    )
    original_verify = software_client.verify_initializer_handoff
    calls = []

    def verify(raw, *, context, now):
        calls.append(True)
        if expires_after_lock and len(calls) > 1:
            now += 121

        def resolve(origin):
            assert origin == trust.origin
            return trust

        return original_verify(raw, context=context, now=now, trust_resolver=resolve)

    monkeypatch.setattr(software_client, "verify_initializer_handoff", verify)

    class Channel:
        def __init__(self, context, permit):
            self.context, self.permit, self.pid = context, permit, None
            channel_holder.append(self)

        def bind_helper(self, pid):
            self.pid = pid

        def close(self):
            pass

        def receive(self):
            assert self.pid == 999
            value = self.permit.initializer_handoff(self.context)
            if invalid_handoff:
                value["initialization_permit"] = "bad-token"
            return json.dumps(value).encode()

    class HelperApi:
        @contextmanager
        def verified_operator(self, pid, created):
            assert (pid, created) == (ctx.process_id, ctx.creation_time)
            yield ctx.operator_sid

        def receive_software_permit(self, context):
            assert context == ctx
            return channel_holder[-1].receive()

    class ParentApi:
        launched = 0

        def software_context(self):
            return ctx

        def launch(self, mode, *, nonce=None):
            assert mode == "initialize-software" and nonce == ctx.nonce
            self.launched += 1
            return 77

        def process_id(self, handle):
            assert handle == 77
            return 999

        def wait(self, handle, milliseconds):
            return dispatch_setup_helper(
                [
                    HELPER_FLAG,
                    "initialize-software",
                    f"{ctx.process_id}:{ctx.creation_time}",
                    ctx.nonce,
                ],
                api_factory=HelperApi,
                identity_factory=machine.identity,
            )

        def close(self, handle):
            assert handle == 77

    api = ParentApi()
    coordinator = DeviceSetupCoordinator(
        api_factory=lambda: api, channel_factory=Channel
    )
    identity = InteractiveWindowsDeviceIdentity(
        identity=machine.identity(), coordinator=coordinator
    )
    session = new_session(client, ring, user, identity)
    return machine, api, identity, session


def test_real_software_bootstrap_pending_approval_and_upgrade_reuses_original_identity(
    client, ring, monkeypatch
):
    user, admin = create_user("software-full"), create_user(
        "software-full-admin", is_admin=True
    )
    set_permission(user)
    machine, api, identity, session = software_bootstrap(
        client, ring, user, monkeypatch
    )
    with pytest.raises(DeviceAuthorizationError):
        session.status()
    assert machine.creates == api.launched == 0
    registration = session.register_software(
        label="test software machine", client_version="v1"
    )
    assert session.summary()["state"] == "PENDING"
    assert machine.creates == api.launched == 1
    assert machine.record.protection == "software"
    with pytest.raises(DeviceAuthorizationError) as caught:
        session.require_local("local:render")
    assert caught.value.code == "DEVICE_PENDING"
    approve(admin, registration)
    session.refresh(force=True)
    expected = (
        session.summary()["thumbprint"],
        registration["device_id"],
        registration["grant_id"],
    )
    for version in ("v2", "v3", "v4"):
        session.close()
        session = new_session(client, ring, user, identity)
        session.refresh(force=True)
        assert (
            session.summary()["thumbprint"],
            session.summary()["device_id"],
            session.summary()["grant_id"],
        ) == expected
        assert not any(
            path.endswith(("/register", "/software-initialization-permit"))
            for path in session._transport.paths
        )
        session.register_software(
            client_version=version
        )  # even explicit repeat reuses existing key
    assert machine.creates == api.launched == 1


def test_real_software_bootstrap_policy_denial_never_reaches_helper_or_cng(
    client, ring, monkeypatch
):
    user = create_user("software-bootstrap-denied")
    machine, api, _, session = software_bootstrap(client, ring, user, monkeypatch)
    with pytest.raises(DeviceAuthorizationError) as caught:
        session.register_software()
    assert caught.value.code == "DEVICE_SOFTWARE_NOT_ALLOWED"
    assert machine.creates == api.launched == 0
    assert machine.record is None
    assert_no_business_state()


@pytest.mark.parametrize("failure", ["bad-message", "expires-after-lock"])
def test_real_helper_rechecks_signed_permission_before_creating_any_key(
    client, ring, monkeypatch, failure
):
    user = create_user("software-bootstrap-failure")
    set_permission(user)
    machine, api, _, session = software_bootstrap(
        client,
        ring,
        user,
        monkeypatch,
        invalid_handoff=failure == "bad-message",
        expires_after_lock=failure == "expires-after-lock",
    )
    with pytest.raises(DeviceIdentityError) as caught:
        session.register_software()
    assert caught.value.code == "SOFTWARE_APPROVAL_REQUIRED"
    assert api.launched == 1 and machine.creates == 0
    assert machine.record is None
    assert_no_business_state()
