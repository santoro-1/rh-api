"""ES256/JWK and RFC 9449 proof validation using PyJWT/cryptography.

This module has no database or HTTP side effects. Nonce consumption and replay
rejection MUST additionally run in the authorization service transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from .errors import DeviceAuthError


MAX_JWT_SIZE = 8192
PROOF_MAX_AGE_SECONDS = 60
PROOF_FUTURE_SKEW_SECONDS = 5
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def sha256_b64(value: str) -> str:
    return b64url(hashlib.sha256(value.encode("ascii")).digest())


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject() -> DeviceAuthError:
    return DeviceAuthError(
        "INVALID_DEVICE_PROOF", "设备持钥证明无效，请重新校验授权", 401
    )


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def strict_jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bound sizes and reject ambiguous JOSE before passing to the crypto library."""
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_JWT_SIZE:
            raise ValueError()
        parts = token.split(".")
        if len(parts) != 3 or any(not _B64URL.fullmatch(part) for part in parts):
            raise ValueError()
        decoded = []
        for part in parts[:2]:
            raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            if b64url(raw) != part:
                raise ValueError()
            obj = json.loads(
                raw,
                object_pairs_hook=_no_duplicates,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(obj, dict):
                raise ValueError()
            decoded.append(obj)
        return decoded[0], decoded[1]
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise _reject() from exc


def canonical_public_jwk(value: Any) -> dict[str, str]:
    try:
        if not isinstance(value, dict) or set(value) != {"kty", "crv", "x", "y"}:
            raise ValueError()
        if value["kty"] != "EC" or value["crv"] != "P-256":
            raise ValueError()
        for name in ("x", "y"):
            coordinate = value[name]
            if (
                not isinstance(coordinate, str)
                or len(coordinate) != 43
                or not _B64URL.fullmatch(coordinate)
            ):
                raise ValueError()
            raw = base64.urlsafe_b64decode(coordinate + "=")
            if len(raw) != 32 or b64url(raw) != coordinate:
                raise ValueError()
        key = jwt.PyJWK.from_dict(value, algorithm="ES256").key
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError()
        return {name: value[name] for name in ("crv", "kty", "x", "y")}
    except (ValueError, TypeError, KeyError, jwt.PyJWTError) as exc:
        raise _reject() from exc


def jwk_thumbprint(value: Any) -> str:
    return sha256_b64(canonical_json(canonical_public_jwk(value)))


def public_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise _reject()
    numbers = key.public_numbers()
    # RFC 7518 coordinates have fixed field width, including leading zero bytes.
    return canonical_public_jwk(
        {
            "kty": "EC",
            "crv": "P-256",
            "x": b64url(numbers.x.to_bytes(32, "big")),
            "y": b64url(numbers.y.to_bytes(32, "big")),
        }
    )


def canonical_uri(value: str) -> str:
    """RFC 3986 scheme/host/port/percent/dot normalization, no query or fragment."""
    try:
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        ):
            raise ValueError()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError()
        if "?" in value or "#" in value or "\\" in value:
            raise ValueError()
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        path = parsed.path or "/"
        if re.search(r"%(?![0-9A-Fa-f]{2})", path):
            raise ValueError()
        path = re.sub(
            r"%([0-9A-Fa-f]{2})",
            lambda m: (
                chr(int(m[1], 16))
                if chr(int(m[1], 16)) in _UNRESERVED
                else "%" + m[1].upper()
            ),
            path,
        )
        # Preserve empty path segments and trailing slash while removing dot segments.
        segments: list[str] = []
        for segment in path.split("/")[1:]:
            if segment == ".":
                continue
            if segment == "..":
                if segments:
                    segments.pop()
            else:
                segments.append(segment)
        normalized = "/" + "/".join(segments)
        if path.endswith(("/.", "/..")) and not normalized.endswith("/"):
            normalized += "/"
        return urlunsplit((parsed.scheme, host, normalized, "", ""))
    except (ValueError, TypeError) as exc:
        raise _reject() from exc


@dataclass(frozen=True)
class DeviceProof:
    jwk: dict[str, str]
    thumbprint: str
    jti: str
    nonce: str
    issued_at: int


def verify_proof(
    token: str,
    *,
    method: str,
    uri: str,
    access_token: str,
    now: int,
    expected_thumbprint: str | None = None,
) -> DeviceProof:
    try:
        header, claims = strict_jwt_parts(token)
        if (
            set(header) != {"typ", "alg", "jwk"}
            or header["typ"] != "dpop+jwt"
            or header["alg"] != "ES256"
        ):
            raise _reject()
        jwk = canonical_public_jwk(header["jwk"])
        thumbprint = jwk_thumbprint(jwk)
        if expected_thumbprint is not None and not hmac.compare_digest(
            thumbprint, expected_thumbprint
        ):
            raise _reject()
        # Claims are validated below against an injected clock, not PyJWT's wall clock.
        jwt.decode(
            token,
            jwt.PyJWK.from_dict(jwk).key,
            algorithms=["ES256"],
            options={
                "verify_aud": False,
                "verify_iat": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
        required = {"jti", "htm", "htu", "iat", "ath", "nonce"}
        if not required.issubset(claims) or type(claims["iat"]) is not int:
            raise _reject()
        if (
            not now - PROOF_MAX_AGE_SECONDS
            <= claims["iat"]
            <= now + PROOF_FUTURE_SKEW_SECONDS
        ):
            raise _reject()
        if claims["htm"] != method.upper() or canonical_uri(
            claims["htu"]
        ) != canonical_uri(uri):
            raise _reject()
        for name in ("jti", "nonce"):
            if (
                not isinstance(claims[name], str)
                or not 16 <= len(claims[name]) <= 128
                or not _B64URL.fullmatch(claims[name])
            ):
                raise _reject()
        if not isinstance(claims["ath"], str) or not hmac.compare_digest(
            claims["ath"], sha256_b64(access_token)
        ):
            raise _reject()
        return DeviceProof(
            jwk, thumbprint, claims["jti"], claims["nonce"], claims["iat"]
        )
    except (ValueError, TypeError, KeyError, UnicodeError, jwt.PyJWTError) as exc:
        raise _reject() from exc
