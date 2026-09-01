"""Initialize, rotate, inspect, and compile device-authorization signing trust.

Private-key creation is deliberately limited to an explicitly confirmed Linux
server operation.  The client compiler accepts public JWK documents only.
Nothing in this tool registers a device, changes production data, or deploys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pprint
import re
import stat
import sys
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


SCHEMA = "publicvideo.device-trust.v1"
CONFIRM_CREATE = "CREATE-SERVER-DEVICE-SIGNING-KEY"
CONFIRM_COMPILE = "COMPILE-APPROVED-PUBLIC-ROOTS"
MAX_DOCUMENT_BYTES = 65_536
KID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
B64URL = re.compile(r"[A-Za-z0-9_-]{43}\Z")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KeyToolError(ValueError):
    pass


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _strict_json(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise KeyToolError("duplicate JSON field")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def _b64url(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _public_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    if not isinstance(key.curve, ec.SECP256R1):
        raise KeyToolError("only P-256 public keys are supported")
    numbers = key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def _validate_jwk(value) -> dict[str, str]:
    import base64

    if not isinstance(value, dict) or set(value) != {"kty", "crv", "x", "y"}:
        raise KeyToolError("public JWK must contain only kty/crv/x/y")
    if value["kty"] != "EC" or value["crv"] != "P-256":
        raise KeyToolError("only P-256 public JWKs are supported")
    coordinates = []
    for name in ("x", "y"):
        text = value[name]
        if not isinstance(text, str) or not B64URL.fullmatch(text):
            raise KeyToolError("invalid public JWK coordinate")
        raw = base64.urlsafe_b64decode(text + "=")
        if len(raw) != 32 or _b64url(raw) != text:
            raise KeyToolError("non-canonical public JWK coordinate")
        coordinates.append(int.from_bytes(raw, "big"))
    try:
        ec.EllipticCurvePublicNumbers(
            coordinates[0], coordinates[1], ec.SECP256R1()
        ).public_key()
    except ValueError as exc:
        raise KeyToolError("public JWK point is not on P-256") from exc
    return {name: value[name] for name in ("kty", "crv", "x", "y")}


def canonical_origin(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise KeyToolError("origin is required")
    if any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        raise KeyToolError("origin must be ASCII without whitespace")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or "\\" in value
    ):
        raise KeyToolError("production origin must be an HTTPS root origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise KeyToolError("invalid origin port") from exc
    if port is not None and port != 443:
        host += f":{port}"
    return "https://" + host


def validate_document(value) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "issuer",
        "environment",
        "keys",
    }:
        raise KeyToolError("invalid public trust document fields")
    if value["schema"] != SCHEMA or value["environment"] != "production":
        raise KeyToolError("a release trust document must be production schema v1")
    issuer = value["issuer"]
    if not isinstance(issuer, str) or not issuer.endswith("/workbench-device-auth"):
        raise KeyToolError("invalid device authorization issuer")
    origin = canonical_origin(issuer[: -len("/workbench-device-auth")])
    if issuer != origin + "/workbench-device-auth":
        raise KeyToolError("non-canonical device authorization issuer")
    keys = value["keys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
        raise KeyToolError("public trust document requires 1-16 keys")
    result_keys = []
    seen = set()
    for item in keys:
        if not isinstance(item, dict) or set(item) != {"kid", "jwk"}:
            raise KeyToolError("invalid public trust key fields")
        kid = item["kid"]
        if not isinstance(kid, str) or not KID.fullmatch(kid) or kid in seen:
            raise KeyToolError("invalid or duplicate public key ID")
        seen.add(kid)
        result_keys.append({"kid": kid, "jwk": _validate_jwk(item["jwk"])})
    return {
        "schema": SCHEMA,
        "issuer": issuer,
        "environment": "production",
        "keys": result_keys,
    }


def load_document(path: Path) -> dict:
    _assert_regular_no_link(path, max_bytes=MAX_DOCUMENT_BYTES)
    try:
        return validate_document(_strict_json(path.read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KeyToolError("public trust document is unreadable") from exc


def _assert_regular_no_link(
    path: Path, *, max_bytes: int | None = None, expect_directory: bool = False
) -> None:
    absolute = path.absolute()
    if not absolute.exists():
        raise KeyToolError(f"file does not exist: {absolute}")
    for item in (absolute, *absolute.parents):
        info = item.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise KeyToolError("symlink/reparse paths are not allowed")
    info = absolute.stat()
    expected_type = (
        stat.S_ISDIR(info.st_mode) if expect_directory else stat.S_ISREG(info.st_mode)
    )
    if not expected_type or (max_bytes is not None and info.st_size > max_bytes):
        raise KeyToolError("file is not regular or is oversized")


def _write_exclusive(path: Path, value: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        position = 0
        while position < len(value):
            position += os.write(descriptor, value[position:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _document_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def _document_digest(document: dict) -> str:
    canonical = json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def create_key_set(
    output_directory: Path,
    *,
    origin: str,
    kid: str,
    previous_document: dict | None = None,
) -> dict:
    """Create one new private key and a public document in a new directory."""
    origin = canonical_origin(origin)
    if not KID.fullmatch(kid):
        raise KeyToolError("invalid key ID")
    previous_keys = []
    if previous_document is not None:
        previous = validate_document(previous_document)
        if previous["issuer"] != origin + "/workbench-device-auth":
            raise KeyToolError("rotation origin differs from the previous document")
        previous_keys = previous["keys"]
        if any(item["kid"] == kid for item in previous_keys):
            raise KeyToolError("rotation key ID already exists")
        if len(previous_keys) >= 16:
            raise KeyToolError("public trust document already has 16 keys")
    output_directory = output_directory.absolute()
    if _is_within(output_directory, PROJECT_ROOT):
        raise KeyToolError(
            "server private keys must be created outside the source checkout"
        )
    if output_directory.exists():
        raise KeyToolError(
            "output directory already exists; never overwrite key material"
        )
    parent = output_directory.parent
    if not parent.is_dir():
        raise KeyToolError("output parent directory does not exist")
    _assert_regular_no_link(parent, expect_directory=True)
    os.mkdir(output_directory, 0o700)
    os.chmod(output_directory, 0o700)
    private = ec.generate_private_key(ec.SECP256R1())
    private_bytes = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    private_path = output_directory / f"device-auth-signing-{kid}.pem"
    public_path = output_directory / "device-auth-public-keys.json"
    env_path = output_directory / "SERVER-DEVICE-AUTH.env"
    new_key = {"kid": kid, "jwk": _public_jwk(private.public_key())}
    document = validate_document(
        {
            "schema": SCHEMA,
            "issuer": origin + "/workbench-device-auth",
            "environment": "production",
            "keys": [*previous_keys, new_key],
        }
    )
    _write_exclusive(private_path, private_bytes, 0o600)
    _write_exclusive(public_path, _document_bytes(document), 0o644)
    env = "\n".join(
        (
            f"WORKBENCH_DEVICE_AUTH_ORIGIN={origin}",
            f"WORKBENCH_DEVICE_AUTH_KEY_ID={kid}",
            f"WORKBENCH_DEVICE_AUTH_PRIVATE_KEY_FILE={private_path}",
            f"WORKBENCH_DEVICE_AUTH_PUBLIC_KEYS_FILE={public_path}",
            "",
        )
    ).encode("utf-8")
    _write_exclusive(env_path, env, 0o600)
    _fsync_directory(output_directory)
    return {
        "output_directory": str(output_directory),
        "private_key_file": str(private_path),
        "public_keys_file": str(public_path),
        "server_environment_file": str(env_path),
        "active_kid": kid,
        "issuer": document["issuer"],
        "public_document_sha256": _document_digest(document),
    }


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inspect_key_set(
    document_path: Path, *, private_path: Path | None, active_kid: str | None
) -> dict:
    document = load_document(document_path)
    kids = [item["kid"] for item in document["keys"]]
    result = {
        "schema": SCHEMA,
        "issuer": document["issuer"],
        "environment": document["environment"],
        "kids": kids,
        "public_document_sha256": _document_digest(document),
    }
    if private_path is not None:
        if active_kid not in kids:
            raise KeyToolError("active key ID is absent from the public document")
        _assert_regular_no_link(private_path, max_bytes=16_384)
        try:
            key = serialization.load_pem_private_key(
                private_path.read_bytes(), password=None
            )
        except (OSError, ValueError, TypeError) as exc:
            raise KeyToolError("private signing key is invalid or encrypted") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise KeyToolError("private signing key must be P-256")
        public = next(
            item["jwk"] for item in document["keys"] if item["kid"] == active_kid
        )
        if _public_jwk(key.public_key()) != public:
            raise KeyToolError("private signing key does not match active public key")
        result["active_kid"] = active_kid
        result["private_key_matches"] = True
    return result


def compile_client_roots(documents: list[dict], target: Path) -> dict:
    roots = []
    seen_origins = set()
    for value in documents:
        document = validate_document(value)
        origin = document["issuer"][: -len("/workbench-device-auth")]
        if origin in seen_origins:
            raise KeyToolError("duplicate client trust origin")
        seen_origins.add(origin)
        roots.append(
            {
                "origin": origin,
                "environment": "production",
                "keys": document["keys"],
            }
        )
    if not 1 <= len(roots) <= 8:
        raise KeyToolError("client release requires 1-8 public trust origins")
    target = target.absolute()
    if not target.is_file():
        raise KeyToolError("client trust source file does not exist")
    _assert_regular_no_link(target, max_bytes=65_536)
    text = (
        """\"\"\"Release-owned public trust configuration, compiled into the backend.\n\nThis file contains PUBLIC P-256 keys only. Server and device private keys must\nnever be added here or read from mutable client configuration.\n\"\"\"\n\nTRUSTED_ISSUERS: tuple[dict, ...] = %s\n"""
        % pprint.pformat(tuple(roots), width=100, sort_dicts=True)
    )
    if "PRIVATE KEY" in text or re.search(r"['\"]d['\"]\s*:", text):
        raise KeyToolError("private key material cannot be compiled into the client")
    temporary = target.with_name(target.name + ".new")
    if temporary.exists():
        raise KeyToolError("temporary trust source already exists")
    _write_exclusive(temporary, text.encode("utf-8"), 0o644)
    os.replace(temporary, target)
    _fsync_directory(target.parent)
    canonical = json.dumps(
        roots, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return {
        "target": str(target),
        "issuer_count": len(roots),
        "trust_sha256": hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--output-directory", required=True, type=Path)
    initialize.add_argument("--origin", required=True)
    initialize.add_argument("--kid", required=True)
    initialize.add_argument("--confirm", required=True)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("--output-directory", required=True, type=Path)
    rotate.add_argument("--origin", required=True)
    rotate.add_argument("--kid", required=True)
    rotate.add_argument("--previous-public-document", required=True, type=Path)
    rotate.add_argument("--confirm", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--public-document", required=True, type=Path)
    inspect.add_argument("--private-key", type=Path)
    inspect.add_argument("--active-kid")
    compile = commands.add_parser("compile-client")
    compile.add_argument("--public-document", action="append", required=True, type=Path)
    compile.add_argument("--jyd-project-root", required=True, type=Path)
    compile.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"init", "rotate"}:
            if not sys.platform.startswith("linux"):
                raise KeyToolError(
                    "create signing keys only on the controlled Linux server"
                )
            if args.confirm != CONFIRM_CREATE:
                raise KeyToolError(
                    "exact signing-key creation confirmation is required"
                )
            previous = (
                load_document(args.previous_public_document)
                if args.command == "rotate"
                else None
            )
            result = create_key_set(
                args.output_directory,
                origin=args.origin,
                kid=args.kid,
                previous_document=previous,
            )
        elif args.command == "inspect":
            if (args.private_key is None) != (args.active_kid is None):
                raise KeyToolError(
                    "private key and active key ID must be provided together"
                )
            result = inspect_key_set(
                args.public_document,
                private_path=args.private_key,
                active_kid=args.active_kid,
            )
        else:
            if args.confirm != CONFIRM_COMPILE:
                raise KeyToolError(
                    "exact public-root compilation confirmation is required"
                )
            target = (
                args.jyd_project_root / "src" / "jyd_probe" / "device_trust_roots.py"
            )
            result = compile_client_roots(
                [load_document(path) for path in args.public_document], target
            )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (KeyToolError, OSError, TypeError, KeyError) as exc:
        print(
            "Device authorization key operation blocked: " + str(exc), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
