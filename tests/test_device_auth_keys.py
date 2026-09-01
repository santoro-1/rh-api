from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import stat

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from deploy import device_auth_keys as keys


def create(tmp_path: Path, kid="release-1", previous=None):
    return keys.create_key_set(
        tmp_path / ("keys-" + kid),
        origin="https://video.example.com",
        kid=kid,
        previous_document=previous,
    )


def load(path):
    return json.loads(Path(path).read_text(encoding="ascii"))


def test_initial_key_set_has_private_server_key_and_public_only_document(tmp_path):
    result = create(tmp_path)
    private = Path(result["private_key_file"])
    public = Path(result["public_keys_file"])
    env = Path(result["server_environment_file"])
    assert b"BEGIN PRIVATE KEY" in private.read_bytes()
    document = load(public)
    assert document["issuer"] == "https://video.example.com/workbench-device-auth"
    assert set(document["keys"][0]["jwk"]) == {"kty", "crv", "x", "y"}
    assert "PRIVATE" not in public.read_text(encoding="ascii")
    assert "BEGIN" not in env.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert stat.S_IMODE(public.stat().st_mode) == 0o644
        assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    inspected = keys.inspect_key_set(
        public, private_path=private, active_kid="release-1"
    )
    assert inspected["private_key_matches"] is True
    assert inspected["public_document_sha256"] == result["public_document_sha256"]


def test_rotation_adds_public_key_without_copying_old_private_key(tmp_path):
    first = create(tmp_path)
    second = create(tmp_path, "release-2", load(first["public_keys_file"]))
    document = load(second["public_keys_file"])
    assert [item["kid"] for item in document["keys"]] == ["release-1", "release-2"]
    assert not (
        Path(second["output_directory"]) / "device-auth-signing-release-1.pem"
    ).exists()
    assert keys.inspect_key_set(
        Path(second["public_keys_file"]),
        private_path=Path(second["private_key_file"]),
        active_kid="release-2",
    )["private_key_matches"]


@pytest.mark.parametrize(
    "origin",
    [
        "http://video.example.com",
        "https://user@video.example.com",
        "https://video.example.com/path",
        "https://video.example.com?x=1",
        "https://video.example.com\\evil",
        "https://video.example.com:bad",
    ],
)
def test_invalid_production_origins_are_rejected(origin):
    with pytest.raises(keys.KeyToolError):
        keys.canonical_origin(origin)


def test_duplicate_rotation_and_existing_output_never_overwrite(tmp_path):
    first = create(tmp_path)
    original = Path(first["private_key_file"]).read_bytes()
    with pytest.raises(keys.KeyToolError, match="exists"):
        create(tmp_path)
    with pytest.raises(keys.KeyToolError, match="already exists"):
        keys.create_key_set(
            tmp_path / "other",
            origin="https://video.example.com",
            kid="release-1",
            previous_document=load(first["public_keys_file"]),
        )
    assert Path(first["private_key_file"]).read_bytes() == original


def test_private_key_must_match_active_public_key(tmp_path):
    first, other = create(tmp_path), create(tmp_path, "other")
    with pytest.raises(keys.KeyToolError, match="does not match"):
        keys.inspect_key_set(
            Path(first["public_keys_file"]),
            private_path=Path(other["private_key_file"]),
            active_kid="release-1",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(extra=True),
        lambda d: d["keys"][0].update(extra=True),
        lambda d: d["keys"][0]["jwk"].update(d="secret"),
        lambda d: d["keys"].append(d["keys"][0]),
        lambda d: d.update(environment="test"),
    ],
)
def test_public_document_is_strict_and_rejects_private_fields(tmp_path, mutation):
    result = create(tmp_path)
    document = load(result["public_keys_file"])
    mutation(document)
    with pytest.raises(keys.KeyToolError):
        keys.validate_document(document)


def test_compile_client_contains_public_roots_only_and_is_deterministic(tmp_path):
    result = create(tmp_path)
    target = tmp_path / "jyd/src/jyd_probe/device_trust_roots.py"
    target.parent.mkdir(parents=True)
    target.write_text("TRUSTED_ISSUERS: tuple[dict, ...] = ()\n", encoding="utf-8")
    compiled = keys.compile_client_roots([load(result["public_keys_file"])], target)
    source = target.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in source and "'d':" not in source
    tree = ast.parse(source)
    assignment = next(node for node in tree.body if isinstance(node, ast.AnnAssign))
    value = ast.literal_eval(assignment.value)
    assert value[0]["origin"] == "https://video.example.com"
    assert value[0]["environment"] == "production"
    canonical = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    import hashlib

    assert (
        compiled["trust_sha256"]
        == hashlib.sha256(canonical.encode("ascii")).hexdigest()
    )
    with pytest.raises(keys.KeyToolError, match="duplicate"):
        keys.compile_client_roots(
            [load(result["public_keys_file"]), load(result["public_keys_file"])], target
        )


def test_compile_requires_existing_regular_target_and_no_temp_collision(tmp_path):
    result = create(tmp_path)
    document = load(result["public_keys_file"])
    target = tmp_path / "missing.py"
    with pytest.raises(keys.KeyToolError, match="does not exist"):
        keys.compile_client_roots([document], target)
    target.write_text("TRUSTED_ISSUERS = ()", encoding="utf-8")
    (tmp_path / "missing.py.new").write_text("collision", encoding="utf-8")
    with pytest.raises(keys.KeyToolError, match="temporary"):
        keys.compile_client_roots([document], target)
    assert target.read_text(encoding="utf-8") == "TRUSTED_ISSUERS = ()"


def test_cli_refuses_key_creation_on_non_linux_and_requires_exact_confirm(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(keys.sys, "platform", "win32")
    assert (
        keys.main(
            [
                "init",
                "--output-directory",
                str(tmp_path / "keys"),
                "--origin",
                "https://video.example.com",
                "--kid",
                "release-1",
                "--confirm",
                keys.CONFIRM_CREATE,
            ]
        )
        == 1
    )
    assert not (tmp_path / "keys").exists()


def test_key_creation_refuses_source_checkout(monkeypatch):
    monkeypatch.setattr(keys.sys, "platform", "linux")
    output = keys.PROJECT_ROOT / ".must-never-contain-private-device-keys"
    assert not output.exists()
    assert keys.main(
        [
            "init",
            "--output-directory",
            str(output),
            "--origin",
            "https://video.example.com",
            "--kid",
            "release-1",
            "--confirm",
            keys.CONFIRM_CREATE,
        ]
    ) == 1
    assert not output.exists()


def test_cli_inspect_is_read_only_and_compile_requires_confirmation(tmp_path, capsys):
    result = create(tmp_path)
    public = Path(result["public_keys_file"])
    before = public.read_bytes()
    assert keys.main(["inspect", "--public-document", str(public)]) == 0
    assert json.loads(capsys.readouterr().out)["kids"] == ["release-1"]
    jyd = tmp_path / "jyd"
    target = jyd / "src/jyd_probe/device_trust_roots.py"
    target.parent.mkdir(parents=True)
    target.write_text("TRUSTED_ISSUERS = ()", encoding="utf-8")
    assert (
        keys.main(
            [
                "compile-client",
                "--public-document",
                str(public),
                "--jyd-project-root",
                str(jyd),
                "--confirm",
                "wrong",
            ]
        )
        == 1
    )
    assert target.read_text(encoding="utf-8") == "TRUSTED_ISSUERS = ()"
    assert public.read_bytes() == before


def test_unencrypted_p256_pkcs8_only(tmp_path):
    result = create(tmp_path)
    private = Path(result["private_key_file"])
    rsa_like = private.with_name("wrong.pem")
    rsa_like.write_bytes(b"not a key")
    with pytest.raises(keys.KeyToolError, match="invalid"):
        keys.inspect_key_set(
            Path(result["public_keys_file"]),
            private_path=rsa_like,
            active_kid="release-1",
        )
    loaded = serialization.load_pem_private_key(private.read_bytes(), password=None)
    assert isinstance(loaded, ec.EllipticCurvePrivateKey)
