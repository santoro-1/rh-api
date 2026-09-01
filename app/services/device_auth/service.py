"""Transactional device registration and grants, with persistent replay defence.

Callers own commit/rollback. Take the database-backed control lock before reading
mutable authorization state; it serializes approvals across processes, including
SQLite where SELECT FOR UPDATE would not provide a quota lock.
"""

from __future__ import annotations

import hashlib
import json
import secrets

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import User
from .errors import DeviceAuthError
from .models import (
    WorkbenchDevice,
    WorkbenchDeviceAuditEvent,
    WorkbenchDeviceChallenge,
    WorkbenchDeviceControl,
    WorkbenchDeviceGrant,
    WorkbenchDevicePolicy,
    WorkbenchDeviceProofReplay,
)
from .protocol import DeviceProof, canonical_json

CHALLENGE_LIFETIME_SECONDS = 120
MAX_ACTIVE_CHALLENGES = 60
MAX_PENDING_GRANTS = 5
SCOPES = frozenset({"cloud:generate", "local:draft", "local:render"})
PURPOSES = frozenset({"register", "exchange", "refresh", "status", "request"})
CONTROL_MODES = frozenset({"OFF", "OBSERVE", "ENFORCE"})
CONTROL_TRANSITIONS = frozenset(
    {
        ("OFF", "OBSERVE"),
        ("OBSERVE", "OFF"),
        ("OBSERVE", "ENFORCE"),
        ("ENFORCE", "OBSERVE"),
    }
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def lock_control(db: Session) -> WorkbenchDeviceControl:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise DeviceAuthError(
            "DEVICE_AUTH_STORAGE_UNSUPPORTED", "设备授权存储不支持安全事务", 503
        )
    db.execute(
        insert(WorkbenchDeviceControl)
        .values(id=1, mode="OFF", revision=1)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.execute(
        update(WorkbenchDeviceControl)
        .where(WorkbenchDeviceControl.id == 1)
        .values(revision=WorkbenchDeviceControl.revision)
    )
    return db.scalars(
        select(WorkbenchDeviceControl)
        .where(WorkbenchDeviceControl.id == 1)
        .execution_options(populate_existing=True)
    ).one()


def current_mode(db: Session) -> str:
    control = db.get(WorkbenchDeviceControl, 1, populate_existing=True)
    return control.mode if control else "OFF"


def change_control_mode(
    db: Session,
    *,
    expected_revision: int,
    new_mode: str,
    operator: str,
    reason: str,
    now: int,
) -> WorkbenchDeviceControl:
    """Change the global rollout mode under the database control lock.

    This is intentionally a server-operator primitive, not a web-admin action.
    ENFORCE cannot be entered directly from OFF, and emergency relaxation must
    pass through OBSERVE so every transition has an explicit audit record.
    """

    control = lock_control(db)
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or type(now) is not int
        or now < 1
    ):
        raise DeviceAuthError(
            "DEVICE_CONTROL_INVALID_REQUEST", "设备授权模式切换参数无效", 400
        )
    if control.revision != expected_revision:
        raise DeviceAuthError(
            "DEVICE_CONTROL_CONFLICT",
            "设备授权模式已被其他操作更新，请重新读取当前状态",
            409,
        )
    if new_mode not in CONTROL_MODES or (control.mode, new_mode) not in CONTROL_TRANSITIONS:
        raise DeviceAuthError(
            "DEVICE_CONTROL_TRANSITION_DENIED",
            "设备授权模式必须按 OFF、OBSERVE、ENFORCE 的受控顺序切换",
            409,
        )
    if (
        not isinstance(operator, str)
        or not operator.strip()
        or len(operator.strip()) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in operator)
        or not isinstance(reason, str)
        or not reason.strip()
        or len(reason.strip()) > 240
        or any(ord(character) < 32 or ord(character) == 127 for character in reason)
    ):
        raise DeviceAuthError(
            "DEVICE_CONTROL_INVALID_REQUEST", "必须填写有效的操作人和切换原因", 400
        )
    old_mode = control.mode
    old_revision = control.revision
    control.mode = new_mode
    control.revision += 1
    _audit(
        db,
        action="device.control_mode_changed",
        actor=None,
        details={
            "operator": operator.strip(),
            "reason": reason.strip(),
            "old_mode": old_mode,
            "new_mode": new_mode,
            "old_revision": old_revision,
            "new_revision": control.revision,
        },
    )
    db.flush()
    return control


def _active_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise DeviceAuthError("LOGIN_REQUIRED", "账号已停用或登录失效，请重新登录", 401)
    return user


def _admin(db: Session, actor_id: int) -> User:
    user = _active_user(db, actor_id)
    if not user.is_admin:
        raise DeviceAuthError("ADMIN_REQUIRED", "需要网站管理员权限")
    return user


def policy_for(db: Session, user_id: int) -> WorkbenchDevicePolicy:
    policy = db.get(WorkbenchDevicePolicy, user_id, populate_existing=True)
    if policy is None:
        policy = WorkbenchDevicePolicy(
            user_id=user_id, max_devices=1, allow_software=False, revision=1
        )
        db.add(policy)
        db.flush()
    return policy


def _audit(
    db: Session,
    *,
    action: str,
    actor: int | None,
    subject: int | None = None,
    grant: WorkbenchDeviceGrant | None = None,
    device_id: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        WorkbenchDeviceAuditEvent(
            actor_user_id=actor,
            subject_user_id=grant.user_id if grant else subject,
            device_id=grant.device_id if grant else device_id,
            grant_id=grant.id if grant else None,
            action=action,
            details_json=canonical_json(details or {}),
        )
    )


def issue_challenge(
    db: Session, *, user_id: int, thumbprint: str, purpose: str, now: int
) -> str:
    lock_control(db)
    _active_user(db, user_id)
    if purpose not in PURPOSES or len(thumbprint) != 43:
        raise DeviceAuthError("INVALID_CHALLENGE", "设备验证请求不完整", 400)
    db.execute(
        delete(WorkbenchDeviceChallenge).where(
            WorkbenchDeviceChallenge.expires_at <= now
        )
    )
    db.execute(
        delete(WorkbenchDeviceProofReplay).where(
            WorkbenchDeviceProofReplay.expires_at <= now
        )
    )
    active = db.scalar(
        select(func.count())
        .select_from(WorkbenchDeviceChallenge)
        .where(
            WorkbenchDeviceChallenge.user_id == user_id,
            WorkbenchDeviceChallenge.expires_at > now,
            WorkbenchDeviceChallenge.consumed_at.is_(None),
        )
    )
    if active >= MAX_ACTIVE_CHALLENGES:
        raise DeviceAuthError(
            "DEVICE_CHALLENGE_LIMIT", "设备验证请求过于频繁，请稍后重试", 429
        )
    nonce = secrets.token_urlsafe(32)
    db.add(
        WorkbenchDeviceChallenge(
            digest=digest(nonce),
            user_id=user_id,
            thumbprint=thumbprint,
            purpose=purpose,
            expires_at=now + CHALLENGE_LIFETIME_SECONDS,
        )
    )
    db.flush()
    return nonce


def consume_proof(
    db: Session, *, user_id: int, proof: DeviceProof, purpose: str, now: int
) -> None:
    lock_control(db)
    _active_user(db, user_id)
    challenge = db.get(
        WorkbenchDeviceChallenge, digest(proof.nonce), populate_existing=True
    )
    if (
        challenge is None
        or challenge.user_id != user_id
        or challenge.thumbprint != proof.thumbprint
        or challenge.purpose != purpose
        or challenge.expires_at <= now
        or challenge.consumed_at is not None
    ):
        raise DeviceAuthError("USE_DPOP_NONCE", "设备验证挑战已失效，请重新获取", 401)
    replay_id = digest(proof.thumbprint + ":" + proof.jti)
    if db.get(WorkbenchDeviceProofReplay, replay_id) is not None:
        raise DeviceAuthError(
            "DEVICE_PROOF_REPLAY", "设备验证请求已使用，请重新校验", 401
        )
    # A request nonce can be reused with different jti values (RFC 9449 §11.1).
    # Registration/exchange challenges are strictly single-use.
    if purpose != "request":
        challenge.consumed_at = now
    db.add(
        WorkbenchDeviceProofReplay(
            digest=replay_id,
            expires_at=max(challenge.expires_at, now + CHALLENGE_LIFETIME_SECONDS),
        )
    )
    db.flush()


def register_device(
    db: Session,
    *,
    user_id: int,
    proof: DeviceProof,
    protection: str,
    label: str,
    client_version: str,
    now: int,
) -> tuple[WorkbenchDevice, WorkbenchDeviceGrant]:
    lock_control(db)
    _active_user(db, user_id)
    if (
        protection not in {"tpm", "software"}
        or len(label) > 80
        or len(client_version) > 80
    ):
        raise DeviceAuthError("INVALID_DEVICE_REGISTRATION", "设备登记信息无效", 400)
    device = db.scalar(
        select(WorkbenchDevice).where(WorkbenchDevice.thumbprint == proof.thumbprint)
    )
    if device:
        if (
            device.public_jwk_json != canonical_json(proof.jwk)
            or device.protection_report != protection
        ):
            raise DeviceAuthError(
                "DEVICE_IDENTITY_MISMATCH", "设备身份或密钥保护方式与原登记不一致"
            )
        grant = db.scalar(
            select(WorkbenchDeviceGrant).where(
                WorkbenchDeviceGrant.user_id == user_id,
                WorkbenchDeviceGrant.device_id == device.id,
            )
        )
        if grant:
            # Version changes are diagnostic only, never a new device or approval.
            grant.client_version = client_version
            device.last_seen_at = now
            return device, grant
    pending = db.scalar(
        select(func.count())
        .select_from(WorkbenchDeviceGrant)
        .where(
            WorkbenchDeviceGrant.user_id == user_id,
            WorkbenchDeviceGrant.status == "PENDING",
        )
    )
    if pending >= MAX_PENDING_GRANTS:
        raise DeviceAuthError(
            "DEVICE_PENDING_LIMIT", "待审批设备过多，请先联系管理员处理", 429
        )
    if device is None:
        device = WorkbenchDevice(
            thumbprint=proof.thumbprint,
            public_jwk_json=canonical_json(proof.jwk),
            protection_report=protection,
            protection_verified=False,
            created_at=now,
            last_seen_at=now,
        )
        db.add(device)
        db.flush()
    policy_for(db, user_id)
    grant = WorkbenchDeviceGrant(
        user_id=user_id,
        device_id=device.id,
        label=label.strip(),
        client_version=client_version,
        status="PENDING",
        scopes_json="[]",
        revision=1,
        created_at=now,
        updated_at=now,
    )
    db.add(grant)
    db.flush()
    _audit(db, action="device.requested", actor=user_id, grant=grant)
    return device, grant


def find_registration(
    db: Session, *, user_id: int, thumbprint: str
) -> tuple[WorkbenchDevice | None, WorkbenchDeviceGrant | None]:
    device = db.scalar(
        select(WorkbenchDevice)
        .where(WorkbenchDevice.thumbprint == thumbprint)
        .execution_options(populate_existing=True)
    )
    grant = (
        None
        if device is None
        else db.scalar(
            select(WorkbenchDeviceGrant)
            .where(
                WorkbenchDeviceGrant.user_id == user_id,
                WorkbenchDeviceGrant.device_id == device.id,
            )
            .execution_options(populate_existing=True)
        )
    )
    return device, grant


def require_active_grant(
    db: Session, *, user_id: int, thumbprint: str, now: int, scope: str | None = None
) -> tuple[WorkbenchDevice, WorkbenchDeviceGrant, WorkbenchDevicePolicy]:
    _active_user(db, user_id)
    device, grant = find_registration(db, user_id=user_id, thumbprint=thumbprint)
    if device is None or grant is None:
        raise DeviceAuthError("DEVICE_UNREGISTERED", "此设备尚未申请授权")
    if device.status != "ACTIVE":
        raise DeviceAuthError("DEVICE_" + device.status, "此设备已被管理员停用")
    if grant.status != "ACTIVE":
        raise DeviceAuthError(
            "DEVICE_" + grant.status, "设备尚未获批或账号的设备授权已停用"
        )
    if grant.expires_at is not None and grant.expires_at <= now:
        raise DeviceAuthError("DEVICE_GRANT_EXPIRED", "设备授权已到期，请联系管理员")
    policy = policy_for(db, user_id)
    if device.protection_report != "tpm" and not policy.allow_software:
        raise DeviceAuthError(
            "DEVICE_SOFTWARE_NOT_ALLOWED", "此账号未获准使用软件保护设备"
        )
    if scope is not None and scope not in json.loads(grant.scopes_json):
        raise DeviceAuthError("DEVICE_SCOPE_DENIED", "此设备未获得该功能的授权")
    return device, grant, policy


def validate_bound_claims(
    db: Session, *, claims: dict, now: int, scope: str | None = None
) -> tuple[WorkbenchDevice, WorkbenchDeviceGrant, WorkbenchDevicePolicy]:
    device, grant, policy = require_active_grant(
        db,
        user_id=claims["user_id"],
        thumbprint=claims["cnf"]["jkt"],
        now=now,
        scope=scope,
    )
    if (
        device.id != claims["device_id"]
        or grant.id != claims["grant_id"]
        or grant.revision != claims["grant_revision"]
        or policy.revision != claims["policy_revision"]
    ):
        raise DeviceAuthError(
            "AUTH_REFRESH_REQUIRED", "设备授权已更新，请重新校验", 401
        )
    if scope is not None and scope not in claims["scopes"]:
        raise DeviceAuthError("DEVICE_SCOPE_DENIED", "设备凭据不允许该功能")
    return device, grant, policy


def _seat_count(db: Session, user_id: int, now: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(WorkbenchDeviceGrant)
        .where(
            WorkbenchDeviceGrant.user_id == user_id,
            WorkbenchDeviceGrant.status.in_(["ACTIVE", "SUSPENDED"]),
            (WorkbenchDeviceGrant.expires_at.is_(None))
            | (WorkbenchDeviceGrant.expires_at > now),
        )
    )


def update_policy(
    db: Session,
    *,
    actor_id: int,
    user_id: int,
    max_devices: int,
    allow_software: bool,
    now: int,
) -> WorkbenchDevicePolicy:
    lock_control(db)
    _admin(db, actor_id)
    _active_user(db, user_id)
    if type(max_devices) is not int or not 0 <= max_devices <= 1000:
        raise DeviceAuthError("INVALID_DEVICE_QUOTA", "设备额度必须为 0 到 1000", 400)
    if max_devices < _seat_count(db, user_id, now):
        raise DeviceAuthError(
            "DEVICE_QUOTA_IN_USE", "已有授权占用该额度，请先撤销不再使用的设备", 409
        )
    policy = policy_for(db, user_id)
    if (policy.max_devices, policy.allow_software) != (max_devices, allow_software):
        policy.max_devices = max_devices
        policy.allow_software = allow_software
        policy.revision += 1
        _audit(
            db,
            action="device.policy_updated",
            actor=actor_id,
            subject=user_id,
            details={
                "max_devices": max_devices,
                "allow_software": allow_software,
                "revision": policy.revision,
            },
        )
    return policy


def _get_grant(db: Session, grant_id: str, revision: int) -> WorkbenchDeviceGrant:
    grant = db.get(WorkbenchDeviceGrant, grant_id, populate_existing=True)
    if grant is None:
        raise DeviceAuthError("DEVICE_GRANT_NOT_FOUND", "设备申请不存在", 404)
    if grant.revision != revision:
        raise DeviceAuthError(
            "DEVICE_GRANT_CONFLICT", "设备状态已被其他操作更新，请刷新后重试", 409
        )
    return grant


def _approve(
    db: Session,
    grant: WorkbenchDeviceGrant,
    *,
    scopes: list[str],
    now: int,
    expires_at: int | None = None,
) -> None:
    _active_user(db, grant.user_id)
    policy = policy_for(db, grant.user_id)
    device = db.get(WorkbenchDevice, grant.device_id, populate_existing=True)
    if device.status != "ACTIVE":
        raise DeviceAuthError("DEVICE_NOT_ACTIVE", "请先恢复设备的全局状态", 409)
    if device.protection_report != "tpm" and not policy.allow_software:
        raise DeviceAuthError(
            "DEVICE_SOFTWARE_NOT_ALLOWED", "请先明确允许此账号的软件保护兼容模式"
        )
    if not scopes or not set(scopes).issubset(SCOPES):
        raise DeviceAuthError("INVALID_DEVICE_SCOPES", "设备授权范围无效", 400)
    if expires_at is not None and (type(expires_at) is not int or expires_at <= now):
        raise DeviceAuthError(
            "INVALID_DEVICE_EXPIRY", "授权到期时间必须晚于当前时间", 400
        )
    owns_seat = grant.status in {"ACTIVE", "SUSPENDED"} and (
        grant.expires_at is None or grant.expires_at > now
    )
    if not owns_seat and _seat_count(db, grant.user_id, now) >= policy.max_devices:
        raise DeviceAuthError(
            "DEVICE_QUOTA_EXCEEDED", "账号设备额度已用完，请先撤销旧设备或使用换机", 409
        )
    grant.status = "ACTIVE"
    grant.scopes_json = canonical_json(sorted(set(scopes)))
    grant.expires_at = expires_at


def change_grant(
    db: Session,
    *,
    actor_id: int,
    grant_id: str,
    expected_revision: int,
    action: str,
    now: int,
    scopes: list[str] | None = None,
    label: str | None = None,
    expires_at: int | None = None,
) -> WorkbenchDeviceGrant:
    lock_control(db)
    _admin(db, actor_id)
    grant = _get_grant(db, grant_id, expected_revision)
    before = grant.status
    if action == "approve" and before in {"PENDING", "REJECTED", "REVOKED"}:
        _approve(
            db, grant, scopes=scopes or sorted(SCOPES), now=now, expires_at=expires_at
        )
    elif action == "resume" and before == "SUSPENDED":
        _approve(
            db,
            grant,
            scopes=json.loads(grant.scopes_json),
            now=now,
            expires_at=grant.expires_at,
        )
    elif action == "suspend" and before == "ACTIVE":
        grant.status = "SUSPENDED"
    elif action == "reject" and before == "PENDING":
        grant.status = "REJECTED"
    elif action == "revoke" and before != "REVOKED":
        grant.status = "REVOKED"
    elif action == "rename" and label is not None and len(label) <= 80:
        grant.label = label.strip()
    else:
        raise DeviceAuthError(
            "DEVICE_STATE_CONFLICT", "当前设备状态不能执行此操作，请刷新后检查", 409
        )
    grant.revision += 1
    grant.updated_at = now
    _audit(
        db,
        action="device." + action,
        actor=actor_id,
        grant=grant,
        details={"before": before, "after": grant.status, "revision": grant.revision},
    )
    db.flush()
    return grant


def replace_grant(
    db: Session,
    *,
    actor_id: int,
    old_grant_id: str,
    new_grant_id: str,
    old_revision: int,
    new_revision: int,
    now: int,
) -> WorkbenchDeviceGrant:
    lock_control(db)
    _admin(db, actor_id)
    old = _get_grant(db, old_grant_id, old_revision)
    new = _get_grant(db, new_grant_id, new_revision)
    if (
        old.id == new.id
        or old.user_id != new.user_id
        or old.status not in {"ACTIVE", "SUSPENDED"}
        or new.status not in {"PENDING", "REJECTED", "REVOKED"}
    ):
        raise DeviceAuthError(
            "INVALID_DEVICE_REPLACEMENT",
            "换机必须选择同一账号的旧授权和新的设备申请",
            409,
        )
    old.status = "REVOKED"
    old.revision += 1
    old.updated_at = now
    db.flush()
    _approve(
        db, new, scopes=json.loads(old.scopes_json), now=now, expires_at=old.expires_at
    )
    new.revision += 1
    new.updated_at = now
    _audit(
        db,
        action="device.replaced_old",
        actor=actor_id,
        grant=old,
        details={"replacement_grant_id": new.id},
    )
    _audit(
        db,
        action="device.replaced_new",
        actor=actor_id,
        grant=new,
        details={"previous_grant_id": old.id},
    )
    db.flush()
    return new


def change_device_status(
    db: Session, *, actor_id: int, device_id: str, status: str, now: int
) -> None:
    lock_control(db)
    _admin(db, actor_id)
    device = db.get(WorkbenchDevice, device_id, populate_existing=True)
    if device is None:
        raise DeviceAuthError("DEVICE_NOT_FOUND", "设备不存在", 404)
    if status not in {"ACTIVE", "SUSPENDED", "REVOKED"}:
        raise DeviceAuthError("INVALID_DEVICE_STATUS", "设备状态无效", 400)
    if device.status != status:
        before = device.status
        device.status = status
        db.execute(
            update(WorkbenchDeviceGrant)
            .where(WorkbenchDeviceGrant.device_id == device_id)
            .values(revision=WorkbenchDeviceGrant.revision + 1, updated_at=now)
        )
        _audit(
            db,
            action="device.global_status",
            actor=actor_id,
            device_id=device_id,
            details={"before": before, "after": status},
        )


def public_status(
    device: WorkbenchDevice | None, grant: WorkbenchDeviceGrant | None, *, now: int
) -> dict:
    state = "UNREGISTERED"
    if device and grant:
        state = device.status if device.status != "ACTIVE" else grant.status
        if (
            state == "ACTIVE"
            and grant.expires_at is not None
            and grant.expires_at <= now
        ):
            state = "EXPIRED"
    return {
        "status": state,
        "device_id": device.id if device else None,
        "grant_id": grant.id if grant else None,
        "thumbprint": device.thumbprint if device else None,
        "label": grant.label if grant else "",
        "revision": grant.revision if grant else None,
        "protection_report": device.protection_report if device else None,
        "protection_verified": bool(device and device.protection_verified),
    }
