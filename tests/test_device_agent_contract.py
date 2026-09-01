"""Actual RH permit -> actual JYD signature -> durable central proof contract.

Native keys, real central queues and suppliers are never used by this suite.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import secrets
import time
from types import SimpleNamespace

import jwt
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import GenerationTask, User
from app.services.device_auth import service
from app.services.device_auth.agent_permits import PERMIT_TYPE
from app.services.device_auth.models import WorkbenchDevice, WorkbenchDeviceGrant
from app.services.device_auth.tokens import ACCESS_TYPE
from tests.conftest import create_user
from tests.test_device_auth import ring, account_token, auth_headers
from tests.test_device_auth_client_contract import (
    activated,
    new_session,
    TestIdentity,
    TrustedIssuer,
)
from jyd_probe.device_agent_protocol import (
    AgentRequestContext,
    sign_agent_request,
    verify_agent_permit,
)
from jyd_probe.device_agent_gate import AgentAuthorizationGate
from jyd_probe.device_agent_client import AgentRequestAuthorizer
from jyd_probe.device_auth_protocol import DeviceAuthorizationError
from jyd_probe.task_store import SQLiteTaskStore

PATH = "/api/workbench/device-auth/agent-permit"
ORIGIN = "http://192.168.1.250:8010"


def set_mode(value):
    with SessionLocal() as db:
        control = service.lock_control(db)
        control.mode = value
        control.revision += 1
        db.commit()


@pytest.fixture
def flow(client, ring, tmp_path):
    user, admin, identity, session, registration = activated(client, ring)
    set_mode("ENFORCE")
    store = SQLiteTaskStore(tmp_path / "central.db")
    trust = TrustedIssuer(
        ring.config.origin, ring.config.environment, ring.verification_keys
    )
    gate = AgentAuthorizationGate(
        store, ring.config.origin, trust_resolver=lambda _: trust
    )
    return SimpleNamespace(
        user=user,
        admin=admin,
        identity=identity,
        session=session,
        registration=registration,
        store=store,
        trust=trust,
        gate=gate,
        authorizer=AgentRequestAuthorizer(session),
    )


def context(
    action="claim",
    *,
    payload=None,
    origin=ORIGIN,
    agent_id="processor-01",
    job_id="job-1",
):
    if action == "register":
        path, payload = "/api/agents/register", payload or {
            "agent_id": agent_id,
            "name": "Test",
        }
    elif action in {"heartbeat", "claim"}:
        path = f"/api/agents/{agent_id}/{action}"
    else:
        path = f"/api/agents/{agent_id}/jobs/{job_id}/{action}"
    return AgentRequestContext.for_request(origin, agent_id, path, payload or {})


def signed(flow, ctx=None):
    ctx = ctx or context()
    nonce = flow.gate.challenge(ctx)["nonce"]
    headers = flow.authorizer.headers(ctx, nonce)
    return ctx, headers["X-Workbench-Agent-Permit"], headers["X-Workbench-Agent-Proof"]


def test_actual_permit_and_native_style_proof_reach_central_validator(flow):
    ctx, permit, proof = signed(flow)
    decision = flow.gate.verify(ctx, permit=permit, proof=proof)
    assert decision.user_id == flow.user.id and decision.intent == "execute"
    assert decision.device_id == flow.registration["device_id"]
    assert decision.grant_id == flow.registration["grant_id"]
    assert decision.thumbprint == flow.identity.open_existing().thumbprint
    assert decision.scopes == {"local:draft", "local:render"}
    assert flow.identity.created == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GenerationTask)) == 0
        assert db.scalar(select(func.count()).select_from(WorkbenchDevice)) == 1
        assert db.scalar(select(func.count()).select_from(WorkbenchDeviceGrant)) == 1


@pytest.mark.parametrize("mode", ["OFF", "OBSERVE"])
def test_signed_rollout_works_without_automatic_device_registration(
    client, ring, flow, mode
):
    user, identity = create_user("agent-rollout"), TestIdentity()
    set_mode(mode)
    flow.session = new_session(client, ring, user, identity)
    flow.authorizer = AgentRequestAuthorizer(flow.session)
    ctx, permit, proof = signed(flow)
    decision = flow.gate.verify(ctx, permit=permit, proof=proof)
    assert decision.user_id == user.id and decision.mode == mode
    assert decision.thumbprint is None and proof == "" and identity.created == 0


@pytest.mark.parametrize("intent", ["execute", "report"])
def test_enforced_permit_cannot_be_obtained_by_legacy_token_alone(client, flow, intent):
    response = client.post(
        PATH,
        json={
            "nonce": secrets.token_urlsafe(32),
            "context_hash": "x" * 43,
            "intent": intent,
        },
        headers={"Authorization": "Bearer " + account_token(flow.user)},
    )
    assert response.status_code == 401
    assert response.json()["code"] in {
        "DEVICE_BOUND_TOKEN_REQUIRED",
        "DEVICE_KEY_PROOF_REQUIRED",
    }


@pytest.mark.parametrize("state", ["PENDING", "SUSPENDED", "REVOKED"])
def test_stopped_device_can_report_with_original_key_but_not_claim(flow, state):
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, flow.registration["grant_id"])
        grant.status, grant.revision = state, grant.revision + 1
        db.commit()
    with pytest.raises(DeviceAuthorizationError):
        signed(flow)
    ctx, permit, proof = signed(
        flow, context("complete", payload={"result": {"exported": True}})
    )
    decision = flow.gate.verify(ctx, permit=permit, proof=proof)
    assert decision.intent == "report" and decision.scopes == frozenset()
    assert (
        decision.user_id == flow.user.id
        and decision.thumbprint == flow.identity.open_existing().thumbprint
    )
    with pytest.raises(DeviceAuthorizationError):
        flow.gate.verify(context(), permit=permit, proof=proof)


def test_permission_revision_refreshes_original_grant_once(flow):
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, flow.registration["grant_id"])
        grant.scopes_json = '["local:draft"]'
        grant.revision += 1
        db.commit()
    before = flow.session._transport.paths.count(PATH)
    ctx, permit, proof = signed(flow)
    decision = flow.gate.verify(ctx, permit=permit, proof=proof)
    assert decision.scopes == {"local:draft"}
    assert flow.session._transport.paths.count(PATH) == before + 2
    assert (
        decision.grant_id == flow.registration["grant_id"]
        and flow.identity.created == 1
    )


def test_report_does_not_need_old_device_access_token(flow):
    flow.session._credentials = None
    ctx, permit, proof = signed(flow, context("complete"))
    assert flow.gate.verify(ctx, permit=permit, proof=proof).intent == "report"
    assert flow.session._credentials is None


def test_copying_account_to_uninitialized_agent_does_not_grant_enforced_permit(
    client, ring, flow
):
    identity = TestIdentity()
    flow.authorizer = AgentRequestAuthorizer(
        new_session(client, ring, flow.user, identity)
    )
    with pytest.raises(DeviceAuthorizationError):
        signed(flow)
    assert identity.created == 0


def test_same_key_different_account_cannot_inherit_execute_grant(client, ring, flow):
    other = create_user("agent-other-account")
    flow.authorizer = AgentRequestAuthorizer(
        new_session(client, ring, other, flow.identity)
    )
    with pytest.raises(DeviceAuthorizationError):
        signed(flow)


@pytest.mark.parametrize(
    "ctx",
    [
        context(origin="http://192.168.1.188:8010"),
        context(agent_id="processor-02"),
        context("heartbeat"),
        context(payload={"injected": True}),
    ],
    ids=["other-central", "other-agent", "other-path", "other-body"],
)
def test_permit_cannot_be_forwarded_to_a_different_request(flow, ctx):
    original, permit, proof = signed(flow)
    with pytest.raises(DeviceAuthorizationError):
        flow.gate.verify(ctx, permit=permit, proof=proof)
    assert (
        flow.gate.verify(original, permit=permit, proof=proof).user_id == flow.user.id
    )


def test_public_key_and_permit_alone_cannot_fake_native_proof(flow):
    ctx, permit, proof = signed(flow)
    other = TestIdentity()
    other.initialize_for_activation()
    claims = jwt.decode(permit, options={"verify_signature": False})
    forged = sign_agent_request(
        other.open_existing(), permit, ctx, nonce=claims["nonce"], now=int(time.time())
    )
    for bad_proof in ("", forged, proof + "changed"):
        with pytest.raises(DeviceAuthorizationError):
            flow.gate.verify(ctx, permit=permit, proof=bad_proof)
    assert flow.gate.verify(ctx, permit=permit, proof=proof).user_id == flow.user.id


def test_one_challenge_is_consumed_atomically_and_survives_gate_restart(flow):
    ctx, permit, proof = signed(flow)

    def attempt():
        try:
            flow.gate.verify(ctx, permit=permit, proof=proof)
            return "accepted"
        except DeviceAuthorizationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["AGENT_PROOF_REPLAYED", "accepted"]
    restarted = AgentAuthorizationGate(
        flow.store, flow.trust.origin, trust_resolver=lambda _: flow.trust
    )
    with pytest.raises(DeviceAuthorizationError) as error:
        restarted.verify(ctx, permit=permit, proof=proof)
    assert error.value.code == "AGENT_PROOF_REPLAYED"


def test_restart_invalidates_outstanding_challenges_even_with_old_clock(flow):
    ctx, permit, proof = signed(flow)
    restarted = AgentAuthorizationGate(
        flow.store, flow.trust.origin, trust_resolver=lambda _: flow.trust
    )
    with pytest.raises(DeviceAuthorizationError):
        restarted.verify(ctx, permit=permit, proof=proof)


def test_clock_rollback_does_not_restore_a_permit(flow):
    ctx, permit, proof = signed(flow)
    original_clock = flow.gate.clock
    flow.gate.clock = lambda: original_clock() - 60
    with pytest.raises(DeviceAuthorizationError) as error:
        flow.gate.verify(ctx, permit=permit, proof=proof)
    assert error.value.code == "DEVICE_AGENT_CLOCK_ERROR"


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "PublicVideoWorkbench:local"},
        {"intent": "report"},
        {"mode": "unknown"},
        {"user_id": True},
        {"control_revision": 0},
        {"iat": True},
        {"cnf": None},
        {"scopes": []},
        {"scopes": ["cloud:generate"]},
        {"scopes": ["local:draft", "local:draft"]},
        {"exp": 1},
        {"nonce": "invalid"},
        {"context_hash": "x" * 43},
        {"extra": True},
        {"environment": "production"},
        {"grant_id": None},
    ],
    ids=lambda change: next(iter(change)),
)
def test_strict_agent_permit_contract_rejects_misissued_claims(flow, ring, change):
    ctx, permit, proof = signed(flow)
    claims = jwt.decode(permit, options={"verify_signature": False})
    claims.update(change)
    bad = ring.sign(claims, typ=PERMIT_TYPE)
    with pytest.raises(DeviceAuthorizationError):
        verify_agent_permit(flow.trust, bad, ctx, now=time.time())


def test_agent_permit_cannot_be_used_as_a_cloud_business_token(client, flow, ring):
    ctx, permit, proof = signed(flow)
    response = client.post(
        "/api/workbench/device-auth/challenge",
        headers={"Authorization": "DPoP " + permit},
        json={
            "purpose": "request",
            "public_jwk": flow.identity.open_existing().public_jwk,
        },
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "INVALID_DEVICE_TOKEN"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "OFF"},
        {"user_id": 999},
        {"scopes": ["local:render"]},
        {"intent": "cloud:generate"},
    ],
)
def test_api_rejects_client_supplied_authority_fields(client, flow, extra):
    payload = {
        "nonce": secrets.token_urlsafe(32),
        "context_hash": "x" * 43,
        "intent": "execute",
        **extra,
    }
    response = client.post(
        PATH,
        json=payload,
        headers={"Authorization": "Bearer " + account_token(flow.user)},
    )
    assert response.status_code == 422


def test_challenge_capacity_and_consumed_credentials_are_not_stored(flow):
    ctx, permit, proof = signed(flow)
    flow.gate.verify(ctx, permit=permit, proof=proof)
    for _ in range(16):
        flow.gate.challenge(ctx)
    with pytest.raises(DeviceAuthorizationError) as error:
        flow.gate.challenge(ctx)
    assert error.value.status_code == 429
    with flow.store._connect() as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(device_agent_challenges)")
        }
        rows = [
            dict(row) for row in db.execute("SELECT * FROM device_agent_challenges")
        ]
    assert columns == {
        "nonce_hash",
        "agent_id",
        "context_hash",
        "expires_at",
        "consumed",
    }
    assert permit not in json.dumps(rows) and proof not in json.dumps(rows)


def seed_job(flow, job_id, *, owner=None, skip_export=False):
    status = {"job_id": job_id, "status": "pending"}
    if owner is not None:
        status["device_authorization"] = {
            "user_id": owner,
            "mode": "OFF",
            "device_id": "source-central",
        }
    flow.store.add_job(
        job_id,
        {
            "output": {"skip_export": skip_export},
            "device_authorization": {"user_id": flow.user.id, "mode": "OFF"},
        },
        status,
    )


def queue_decision(flow):
    ctx, permit, proof = signed(flow)
    return flow.gate.verify(ctx, permit=permit, proof=proof)


def register_queue(flow, agent_id="processor-01"):
    from jyd_probe.device_agent_operations import register_agent

    ctx, permit, proof = signed(flow, context("register", agent_id=agent_id))
    decision = flow.gate.verify(ctx, permit=permit, proof=proof)
    return register_agent(flow.store, agent_id, {"name": "target"}, decision)


def test_verified_agent_claims_only_server_recorded_owner_not_payload_flags(flow):
    register_queue(flow)
    seed_job(flow, "legacy")
    seed_job(flow, "other", owner=flow.user.id + 100)
    seed_job(flow, "owned", owner=flow.user.id)
    decision = queue_decision(flow)
    claimed = flow.store.claim_job("processor-01", authorization=decision)
    assert claimed["job_id"] == "owned"
    assert claimed["status"]["device_authorization"]["device_id"] == "source-central"
    assert (
        claimed["status"]["agent_device_authorization"]["device_id"]
        == decision.device_id
    )
    assert flow.store.get_status("legacy")["status"] == "pending"
    assert flow.store.get_status("other")["status"] == "pending"


def test_draft_only_agent_skips_render_jobs_without_claiming_or_failing_them(flow):
    register_queue(flow)
    seed_job(flow, "render", owner=flow.user.id)
    seed_job(flow, "draft", owner=flow.user.id, skip_export=True)
    with SessionLocal() as db:
        grant = db.get(WorkbenchDeviceGrant, flow.registration["grant_id"])
        grant.scopes_json = '["local:draft"]'
        grant.revision += 1
        db.commit()
    claimed = flow.store.claim_job("processor-01", authorization=queue_decision(flow))
    assert claimed["job_id"] == "draft"
    assert flow.store.get_status("render")["status"] == "pending"


def test_repeated_claim_keeps_assignment_but_other_principal_cannot_take_it(flow):
    register_queue(flow)
    seed_job(flow, "original", owner=flow.user.id)
    decision = queue_decision(flow)
    first = flow.store.claim_job("processor-01", authorization=decision)
    repeated = flow.store.claim_job("processor-01", authorization=queue_decision(flow))
    assert first == repeated
    for other in (
        replace(decision, user_id=flow.user.id + 1),
        replace(decision, thumbprint="x" * 43),
    ):
        with pytest.raises(DeviceAuthorizationError) as error:
            flow.store.claim_job("processor-01", authorization=other)
        assert error.value.code == "DEVICE_AGENT_ASSIGNMENT_MISMATCH"
    assert flow.store.get_status("original")["retry_count"] == 0


def test_expired_authorized_lease_keeps_original_task_for_proved_reporting(flow):
    from jyd_probe.device_agent_operations import start_job, report_job

    register_queue(flow)
    register_queue(flow, "processor-02")
    seed_job(flow, "original", owner=flow.user.id)
    decision = queue_decision(flow)
    flow.store.claim_job("processor-01", authorization=decision)
    execution_id = "c" * 32
    ctx, permit, proof = signed(
        flow,
        context("start", job_id="original", payload={"execution_id": execution_id}),
    )
    starter = flow.gate.verify(ctx, permit=permit, proof=proof)
    start_job(flow.store, "processor-01", "original", execution_id, starter)
    with flow.store._transaction() as db:
        db.execute(
            "UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00' WHERE job_id='original'"
        )
    assert flow.store.recover_expired_leases() == []
    status = flow.store.get_status("original")
    assert status["status"] == "running" and status["agent_recovery_required"] is True
    assert status["assigned_agent_id"] == "processor-01" and status["retry_count"] == 0
    with pytest.raises(DeviceAuthorizationError) as error:
        flow.store.claim_job("processor-01", authorization=queue_decision(flow))
    assert error.value.code == "DEVICE_AGENT_EXECUTION_UNCERTAIN"
    assert (
        flow.store.claim_job("processor-02", authorization=queue_decision(flow)) is None
    )
    ctx, permit, proof = signed(
        flow,
        context(
            "complete",
            job_id="original",
            payload={"execution_id": execution_id, "result": {"exported": True}},
        ),
    )
    reporter = flow.gate.verify(ctx, permit=permit, proof=proof)
    result, changed = report_job(
        flow.store,
        "processor-01",
        "original",
        execution_id,
        reporter,
        action="complete",
        payload={"result": {"exported": True}},
    )
    assert changed is True
    assert (
        result["status"] == "completed"
        and flow.store.get_status("original")["retry_count"] == 0
    )


def test_unverified_or_expired_decision_never_claims_a_job(flow):
    register_queue(flow)
    seed_job(flow, "original", owner=flow.user.id)
    decision = queue_decision(flow)
    for bad in (
        {"user_id": flow.user.id, "mode": "OFF"},
        replace(decision, expires_at=int(time.time()) - 1),
        replace(decision, intent="report"),
    ):
        with pytest.raises(DeviceAuthorizationError):
            flow.store.claim_job("processor-01", authorization=bad)
    assert flow.store.get_status("original")["status"] == "pending"
