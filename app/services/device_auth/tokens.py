"""Independent server signing keys; never derive licensing keys from APP_SECRET_KEY."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import get_settings
from app.models import User
from app.services.workbench_auth import _password_revision

from .errors import DeviceAuthError
from .models import WorkbenchDevice, WorkbenchDeviceGrant, WorkbenchDevicePolicy
from .protocol import canonical_public_jwk, canonical_uri, public_jwk, strict_jwt_parts

PRODUCT = "PublicVideoWorkbench"
ACCESS_TYPE = "workbench-access+jwt"
LEASE_TYPE = "workbench-lease+jwt"
ACCESS_AUDIENCE = "PublicVideoWorkbench:cloud"
LEASE_AUDIENCE = "PublicVideoWorkbench:local"
TOKEN_LIFETIME_SECONDS = 1800


@dataclass(frozen=True)
class DeviceAuthConfig:
    origin: str
    environment: str
    active_kid: str
    private_key_file: Path | None
    verification_keys_file: Path | None

    @property
    def issuer(self) -> str:
        return self.origin + "/workbench-device-auth"


def get_device_auth_config() -> DeviceAuthConfig:
    settings = get_settings()
    raw = os.getenv("WORKBENCH_DEVICE_AUTH_ORIGIN", "").strip().rstrip("/")
    try:
        origin = canonical_uri(raw).rstrip("/")
        parsed = urlsplit(origin)
        if parsed.path:
            raise ValueError()
        if parsed.scheme != "https" and not (
            settings.app_env != "production"
            and parsed.hostname in {"localhost", "127.0.0.1", "testserver"}
        ):
            raise ValueError()
    except (ValueError, DeviceAuthError) as exc:
        raise DeviceAuthError(
            "DEVICE_AUTH_NOT_CONFIGURED", "设备授权服务地址未正确配置", 503
        ) from exc
    kid = os.getenv("WORKBENCH_DEVICE_AUTH_KEY_ID", "").strip()
    if (
        not kid
        or len(kid) > 80
        or not all(c.isascii() and (c.isalnum() or c in "-_.") for c in kid)
    ):
        raise DeviceAuthError(
            "DEVICE_AUTH_NOT_CONFIGURED", "设备授权签名密钥未配置", 503
        )
    private = os.getenv("WORKBENCH_DEVICE_AUTH_PRIVATE_KEY_FILE", "").strip()
    public = os.getenv("WORKBENCH_DEVICE_AUTH_PUBLIC_KEYS_FILE", "").strip()
    return DeviceAuthConfig(
        origin,
        settings.app_env,
        kid,
        Path(private) if private else None,
        Path(public) if public else None,
    )


@dataclass(frozen=True)
class DeviceKeyRing:
    config: DeviceAuthConfig
    verification_keys: dict[str, ec.EllipticCurvePublicKey]
    signing_key: ec.EllipticCurvePrivateKey | None = None

    def sign(self, claims: dict, *, typ: str) -> str:
        if self.signing_key is None:
            raise DeviceAuthError(
                "DEVICE_AUTH_NOT_CONFIGURED", "设备授权签发服务暂不可用", 503
            )
        return jwt.encode(
            claims,
            self.signing_key,
            algorithm="ES256",
            headers={"kid": self.config.active_kid, "typ": typ},
        )

    def verify(self, token: str, *, typ: str, now: int) -> dict:
        try:
            header, claims = strict_jwt_parts(token)
            if (
                set(header) != {"alg", "typ", "kid"}
                or header["typ"] != typ
                or header["alg"] != "ES256"
            ):
                raise ValueError()
            key = self.verification_keys.get(header["kid"])
            if key is None:
                raise ValueError()
            audience = ACCESS_AUDIENCE if typ == ACCESS_TYPE else LEASE_AUDIENCE
            jwt.decode(
                token,
                key,
                algorithms=["ES256"],
                audience=audience,
                issuer=self.config.issuer,
                options={
                    "verify_iat": False,
                    "verify_exp": False,
                    "verify_nbf": False,
                    "require": ["iss", "aud", "sub", "iat", "exp", "nbf", "jti"],
                },
            )
            for field in (
                "iat",
                "exp",
                "nbf",
                "user_id",
                "grant_revision",
                "policy_revision",
            ):
                if type(claims.get(field)) is not int:
                    raise ValueError()
            if (
                not claims["iat"] <= now + 5
                or not claims["nbf"] <= now + 5
                or not now < claims["exp"]
            ):
                raise ValueError()
            if not 0 < claims["exp"] - claims["iat"] <= TOKEN_LIFETIME_SECONDS:
                raise ValueError()
            if (
                claims.get("product") != PRODUCT
                or claims.get("environment") != self.config.environment
            ):
                raise ValueError()
            if (
                claims.get("sub") != str(claims["user_id"])
                or claims.get("schema") != "runninghub.workbench-auth.v2"
            ):
                raise ValueError()
            if not isinstance(claims.get("cnf"), dict) or set(claims["cnf"]) != {"jkt"}:
                raise ValueError()
            if (
                not isinstance(claims["cnf"]["jkt"], str)
                or len(claims["cnf"]["jkt"]) != 43
            ):
                raise ValueError()
            if not isinstance(claims.get("scopes"), list) or not all(
                isinstance(s, str) for s in claims["scopes"]
            ):
                raise ValueError()
            for field in (
                "device_id",
                "grant_id",
                "jti",
                "username",
                "password_revision",
            ):
                if not isinstance(claims.get(field), str) or not claims[field]:
                    raise ValueError()
            return claims
        except (
            ValueError,
            TypeError,
            KeyError,
            jwt.PyJWTError,
            DeviceAuthError,
        ) as exc:
            raise DeviceAuthError(
                "INVALID_DEVICE_TOKEN", "设备授权凭据失效，请重新校验授权", 401
            ) from exc


def load_key_ring(config: DeviceAuthConfig, *, signing: bool = False) -> DeviceKeyRing:
    try:
        if (
            config.verification_keys_file is None
            or config.verification_keys_file.stat().st_size > 65536
        ):
            raise ValueError()
        document = json.loads(config.verification_keys_file.read_text(encoding="utf-8"))
        if (
            document.get("schema") != "publicvideo.device-trust.v1"
            or document.get("issuer") != config.issuer
            or document.get("environment") != config.environment
        ):
            raise ValueError()
        keys = {}
        for item in document["keys"]:
            kid = item["kid"]
            if not isinstance(kid, str) or not kid or kid in keys:
                raise ValueError()
            jwk = canonical_public_jwk(item["jwk"])
            keys[kid] = jwt.PyJWK.from_dict(jwk, algorithm="ES256").key
        if config.active_kid not in keys:
            raise ValueError()
        private = None
        if signing:
            if (
                config.private_key_file is None
                or config.private_key_file.stat().st_size > 16384
            ):
                raise ValueError()
            private = serialization.load_pem_private_key(
                config.private_key_file.read_bytes(), password=None
            )
            if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(
                private.curve, ec.SECP256R1
            ):
                raise ValueError()
            if public_jwk(private.public_key()) != public_jwk(keys[config.active_kid]):
                raise ValueError()
        return DeviceKeyRing(config, keys, private)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        DeviceAuthError,
        jwt.PyJWTError,
    ) as exc:
        raise DeviceAuthError(
            "DEVICE_AUTH_NOT_CONFIGURED", "设备授权密钥配置无效，请联系管理员", 503
        ) from exc


def issue_credentials(
    ring: DeviceKeyRing,
    *,
    user: User,
    device: WorkbenchDevice,
    grant: WorkbenchDeviceGrant,
    policy: WorkbenchDevicePolicy,
    now: int,
) -> dict:
    import secrets

    expires = min(
        now + TOKEN_LIFETIME_SECONDS, grant.expires_at or now + TOKEN_LIFETIME_SECONDS
    )
    claims = {
        "schema": "runninghub.workbench-auth.v2",
        "iss": ring.config.issuer,
        "product": PRODUCT,
        "environment": ring.config.environment,
        "sub": str(user.id),
        "user_id": user.id,
        "username": user.username,
        "password_revision": _password_revision(user),
        "device_id": device.id,
        "grant_id": grant.id,
        "grant_revision": grant.revision,
        "policy_revision": policy.revision,
        "cnf": {"jkt": device.thumbprint},
        "scopes": json.loads(grant.scopes_json),
        "iat": now,
        "nbf": now,
        "exp": expires,
    }
    access = ring.sign(
        {**claims, "aud": ACCESS_AUDIENCE, "jti": secrets.token_urlsafe(24)},
        typ=ACCESS_TYPE,
    )
    lease = ring.sign(
        {**claims, "aud": LEASE_AUDIENCE, "jti": secrets.token_urlsafe(24)},
        typ=LEASE_TYPE,
    )
    return {
        "access_token": access,
        "token_type": "DPoP",
        "local_lease": lease,
        "expires_in": expires - now,
        "refresh_after_seconds": min(300, max(1, expires - now)),
        "device_id": device.id,
        "grant_id": grant.id,
        "thumbprint": device.thumbprint,
    }
