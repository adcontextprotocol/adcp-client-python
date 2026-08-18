"""Generate a keypair, load it, sign with it, verify with the matching JWK."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from adcp.signing import (
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    verify_request_signature,
)
from adcp.signing.keygen import generate_ed25519, generate_es256, main


@pytest.mark.parametrize(
    ("generator", "alg"),
    [(generate_ed25519, "ed25519"), (generate_es256, "ecdsa-p256-sha256")],
)
def test_generated_keypair_signs_and_verifies(generator, alg: str) -> None:
    pem, jwk = generator(kid="test-kid")
    assert jwk["adcp_use"] == "request-signing"
    assert jwk["use"] == "sig"
    assert jwk["key_ops"] == ["verify"]

    private_key = serialization.load_pem_private_key(pem, password=None)

    body = b'{"x":1}'
    url = "https://seller.example.com/adcp/create_media_buy"
    signed = sign_request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,  # type: ignore[arg-type]
        key_id="test-kid",
        alg=alg,
        signing_profile_version="3.2",
    )
    headers = {"Content-Type": "application/json", **signed.as_dict()}

    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [jwk]}),
    )
    verify_request_signature(method="POST", url=url, headers=headers, body=body, options=options)


def test_cli_main_writes_pem_and_prints_jwks(tmp_path: Path, capsys) -> None:
    out = tmp_path / "key.pem"
    rc = main(["--alg", "ed25519", "--out", str(out), "--kid", "my-kid"])
    assert rc == 0
    assert out.exists() and out.stat().st_size > 0
    # mode 600
    assert oct(out.stat().st_mode)[-3:] == "600"

    captured = capsys.readouterr()
    jwks = json.loads(captured.out)
    assert jwks["keys"][0]["kid"] == "my-kid"
    assert jwks["keys"][0]["adcp_use"] == "request-signing"


def test_cli_main_refuses_overwrite_without_force(tmp_path: Path, capsys) -> None:
    out = tmp_path / "key.pem"
    out.write_bytes(b"existing")
    rc = main(["--alg", "ed25519", "--out", str(out)])
    assert rc == 2
    assert out.read_bytes() == b"existing"


@pytest.mark.parametrize("generator", [generate_ed25519, generate_es256])
def test_generated_encrypted_pem_requires_passphrase(generator) -> None:
    passphrase = b"correct horse battery staple"
    pem, _ = generator(kid="test-kid", passphrase=passphrase)

    # No passphrase → cryptography raises TypeError.
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(pem, password=None)

    # Wrong passphrase → cryptography raises ValueError.
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(pem, password=b"wrong")

    loaded = serialization.load_pem_private_key(pem, password=passphrase)
    assert loaded is not None


def test_cli_main_encrypt_prompts_and_writes_encrypted_pem(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "key.pem"
    passphrase = "correct horse battery staple"
    prompts = iter([passphrase, passphrase])
    monkeypatch.setattr("adcp.signing.keygen.getpass.getpass", lambda _prompt: next(prompts))

    rc = main(["--alg", "ed25519", "--out", str(out), "--kid", "enc-kid", "--encrypt"])
    assert rc == 0
    assert oct(out.stat().st_mode)[-3:] == "600"

    pem = out.read_bytes()
    assert b"ENCRYPTED" in pem
    loaded = serialization.load_pem_private_key(pem, password=passphrase.encode())
    assert loaded is not None

    captured = capsys.readouterr()
    assert "encrypted" in captured.err


def test_cli_main_encrypt_rejects_mismatched_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "key.pem"
    prompts = iter(["one", "two"])
    monkeypatch.setattr("adcp.signing.keygen.getpass.getpass", lambda _prompt: next(prompts))

    with pytest.raises(SystemExit) as exc:
        main(["--alg", "ed25519", "--out", str(out), "--encrypt"])
    assert exc.value.code == 2
    assert not out.exists()


def test_cli_main_encrypt_rejects_empty_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "key.pem"
    monkeypatch.setattr("adcp.signing.keygen.getpass.getpass", lambda _prompt: "")

    with pytest.raises(SystemExit) as exc:
        main(["--alg", "ed25519", "--out", str(out), "--encrypt"])
    assert exc.value.code == 2
    assert not out.exists()


def test_cli_main_force_overwrites_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "key.pem"
    out.write_bytes(b"old contents")
    rc = main(["--alg", "ed25519", "--out", str(out), "--kid", "replaced", "--force"])
    assert rc == 0
    pem = out.read_bytes()
    assert pem != b"old contents"
    assert b"PRIVATE KEY" in pem
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_cli_main_encrypt_with_force_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "key.pem"
    out.write_bytes(b"old contents")
    passphrase = "xkcd forced overwrite phrase"
    prompts = iter([passphrase, passphrase])
    monkeypatch.setattr("adcp.signing.keygen.getpass.getpass", lambda _prompt: next(prompts))

    rc = main(["--alg", "ed25519", "--out", str(out), "--encrypt", "--force"])
    assert rc == 0
    pem = out.read_bytes()
    assert b"ENCRYPTED" in pem
    serialization.load_pem_private_key(pem, password=passphrase.encode())
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_nfc_normalized_passphrase_round_trip() -> None:
    # "é" has two canonical forms: NFC (U+00E9, one codepoint) and NFD
    # (U+0065 U+0301, two codepoints). Users typing on different platforms
    # may produce either form; keygen normalizes to NFC so the resulting
    # PEM is decryptable from a passphrase re-typed on any platform.
    nfc = "caf\u00e9"
    nfd = "cafe\u0301"
    assert nfc != nfd
    assert nfc.encode("utf-8") != nfd.encode("utf-8")

    pem, _ = generate_ed25519(kid="nfc-kid", passphrase=nfc.encode("utf-8"))
    # Loading with the NFC form succeeds; NFD form would fail because
    # BestAvailableEncryption keyed off raw NFC bytes.
    loaded = serialization.load_pem_private_key(pem, password=nfc.encode("utf-8"))
    assert loaded is not None
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(pem, password=nfd.encode("utf-8"))


def test_cli_main_encrypt_normalizes_nfd_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a macOS-style NFD passphrase at the prompt; keygen should
    # NFC-normalize before encrypting, so NFC-typed decryption succeeds.
    out = tmp_path / "key.pem"
    nfd = "cafe\u0301"
    nfc = "caf\u00e9"
    prompts = iter([nfd, nfd])
    monkeypatch.setattr("adcp.signing.keygen.getpass.getpass", lambda _prompt: next(prompts))

    rc = main(["--alg", "ed25519", "--out", str(out), "--encrypt"])
    assert rc == 0
    pem = out.read_bytes()
    # Decryption with the NFC form of the same string succeeds because
    # keygen normalized before encoding.
    loaded = serialization.load_pem_private_key(pem, password=nfc.encode("utf-8"))
    assert loaded is not None
