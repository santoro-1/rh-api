from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.routes.dependencies import check_rate_limit
from app.services.device_auth import service
from app.services.device_auth.authentication import (
    authenticated_account,
    consume_request_proof,
)
from app.services.device_auth.errors import DeviceAuthError
from app.services.device_auth.initialization import issue_software_initialization_permit
from app.services.device_auth.agent_permits import issue_agent_permit
from app.services.device_auth.local_policy import issue_local_policy
from app.services.device_auth.protocol import jwk_thumbprint
from app.services.device_auth.tokens import (
    get_device_auth_config,
    issue_credentials,
    load_key_ring,
)


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(
    prefix="/api/workbench/device-auth",
    tags=["workbench-device-auth"],
    dependencies=[Depends(_no_cache)],
)


class ChallengeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_jwk: dict
    purpose: Literal["register", "exchange", "refresh", "status", "request"]


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protection: Literal["tpm", "software"]
    label: str = Field(default="", max_length=80)
    client_version: str = Field(default="", max_length=80)


class LocalPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SoftwareInitializationInput(LocalPolicyInput):
    context_hash: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class AgentPermitInput(SoftwareInitializationInput):
    intent: Literal["execute", "report"]


@router.post("/agent-permit")
def agent_request_permit(
    request: Request, payload: AgentPermitInput, db: Session = Depends(get_db)
):
    check_rate_limit(request, "device-agent-permit", 240)
    user, token, bound = authenticated_account(request, db)
    proof = None
    if bound is not None or (
        payload.intent == "report" and request.headers.get("dpop")
    ):
        proof = consume_request_proof(
            request, db, user=user, token=token, bound=bound, purpose="request"
        )
    elif request.headers.get("dpop"):
        raise DeviceAuthError(
            "DEVICE_BOUND_TOKEN_REQUIRED", "新执行请求需要设备绑定凭据", 401
        )
    ring = load_key_ring(get_device_auth_config(), signing=True)
    service.lock_control(db)
    # Account/policy changes while waiting for the lock must take effect now.
    user, _, bound = authenticated_account(request, db)
    result = issue_agent_permit(
        db,
        ring,
        user_id=user.id,
        bound=bound,
        thumbprint=proof.thumbprint if proof is not None else None,
        nonce=payload.nonce,
        context_hash=payload.context_hash,
        intent=payload.intent,
        now=int(time.time()),
    )
    db.commit()
    return result


@router.post("/software-initialization-permit")
def software_initialization_permit(
    request: Request,
    payload: SoftwareInitializationInput,
    db: Session = Depends(get_db),
):
    check_rate_limit(request, "device-software-initialization", 30)
    user, token, bound = authenticated_account(request, db)
    if bound is not None:
        consume_request_proof(
            request, db, user=user, token=token, bound=bound, purpose="request"
        )
    elif request.headers.get("dpop"):
        raise DeviceAuthError(
            "DEVICE_BOUND_TOKEN_REQUIRED", "设备初始化凭据不一致", 401
        )
    ring = load_key_ring(get_device_auth_config(), signing=True)
    service.lock_control(db)
    # Revalidate the account after waiting for the shared policy-control lock.
    user, token, _ = authenticated_account(request, db)
    result = issue_software_initialization_permit(
        db,
        ring,
        user_id=user.id,
        account_token=token,
        nonce=payload.nonce,
        context_hash=payload.context_hash,
        now=int(time.time()),
    )
    db.commit()
    return result


@router.post("/local-policy")
def local_policy(
    request: Request, payload: LocalPolicyInput, db: Session = Depends(get_db)
):
    check_rate_limit(request, "device-local-policy", 60)
    user, token, bound = authenticated_account(request, db)
    # Bootstrap policy needs no machine key, including on an unregistered machine.
    # A bound token still needs its normal request proof, not bearer possession.
    if bound is not None:
        consume_request_proof(
            request, db, user=user, token=token, bound=bound, purpose="request"
        )
    elif request.headers.get("dpop"):
        raise DeviceAuthError("DEVICE_BOUND_TOKEN_REQUIRED", "设备策略凭据不一致", 401)
    ring = load_key_ring(get_device_auth_config(), signing=True)
    control = service.lock_control(db)
    user, token, _ = authenticated_account(request, db)
    result = issue_local_policy(
        ring,
        user_id=user.id,
        account_token=token,
        nonce=payload.nonce,
        mode=control.mode,
        revision=control.revision,
        now=int(time.time()),
    )
    db.commit()
    return result


@router.post("/challenge")
def challenge(request: Request, payload: ChallengeInput, db: Session = Depends(get_db)):
    check_rate_limit(request, "device-challenge", 120)
    get_device_auth_config()  # Never issue a challenge for an unconfigured origin.
    user, _, bound = authenticated_account(request, db)
    thumbprint = jwk_thumbprint(payload.public_jwk)
    if bound is not None and thumbprint != bound["cnf"]["jkt"]:
        raise DeviceAuthError(
            "DEVICE_IDENTITY_MISMATCH", "设备密钥与当前凭据不一致", 401
        )
    nonce = service.issue_challenge(
        db,
        user_id=user.id,
        thumbprint=thumbprint,
        purpose=payload.purpose,
        now=int(time.time()),
    )
    db.commit()
    return {"nonce": nonce, "expires_in": service.CHALLENGE_LIFETIME_SECONDS}


@router.post("/register")
def register(
    request: Request, payload: RegistrationInput, db: Session = Depends(get_db)
):
    check_rate_limit(request, "device-register", 30)
    user, token, bound = authenticated_account(request, db)
    proof = consume_request_proof(
        request, db, user=user, token=token, bound=bound, purpose="register"
    )
    device, grant = service.register_device(
        db,
        user_id=user.id,
        proof=proof,
        protection=payload.protection,
        label=payload.label,
        client_version=payload.client_version,
        now=int(time.time()),
    )
    result = service.public_status(device, grant, now=int(time.time()))
    db.commit()
    return result


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    user, token, bound = authenticated_account(request, db)
    proof = consume_request_proof(
        request, db, user=user, token=token, bound=bound, purpose="status"
    )
    device, grant = service.find_registration(
        db, user_id=user.id, thumbprint=proof.thumbprint
    )
    return service.public_status(device, grant, now=int(time.time()))


def _exchange(request: Request, db: Session, *, refresh: bool):
    check_rate_limit(request, "device-exchange", 30)
    user, token, bound = authenticated_account(request, db, require_bound=refresh)
    proof = consume_request_proof(
        request,
        db,
        user=user,
        token=token,
        bound=bound,
        purpose="refresh" if refresh else "exchange",
    )
    ring = load_key_ring(get_device_auth_config(), signing=True)
    service.lock_control(db)
    user, _, _ = authenticated_account(request, db, require_bound=refresh)
    # Refresh checks CURRENT grant/policy, not old revisions: a still-approved
    # device transparently picks up admin changes without reactivation.
    device, grant, policy = service.require_active_grant(
        db, user_id=user.id, thumbprint=proof.thumbprint, now=int(time.time())
    )
    db.refresh(user)
    result = issue_credentials(
        ring, user=user, device=device, grant=grant, policy=policy, now=int(time.time())
    )
    device.last_seen_at = int(time.time())
    db.commit()
    return result


@router.post("/exchange")
def exchange(request: Request, db: Session = Depends(get_db)):
    return _exchange(request, db, refresh=False)


@router.post("/refresh")
def refresh(request: Request, db: Session = Depends(get_db)):
    return _exchange(request, db, refresh=True)
