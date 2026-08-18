"""Tests for the SigningProvider abstraction (issue #283).

Coverage:

* :class:`InMemorySigningProvider` round-trips through
  :func:`async_sign_request` + :func:`verify_request_signature` for both
  ed25519 and ecdsa-p256-sha256.
* :func:`async_sign_request` produces byte-identical headers to
  :func:`sign_request` given the same inputs (modulo signature value
  for ECDSA, which is non-deterministic).
* RFC 8941 escaping: a ``key_id`` containing ``"`` and ``\\`` survives
  the sign → header → re-parse → verify round-trip.
* :func:`pem_to_adcp_jwk` produces a JWK byte-shape-identical to
  :func:`generate_signing_keypair`'s public half, accepts SPKI public
  PEMs, and rejects unsupported key types.
* Protocol is :func:`runtime_checkable` so adapter authors can use
  ``isinstance``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from adcp.signing import (
    InMemorySigningProvider,
    SigningProvider,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    async_sign_request,
    generate_signing_keypair,
    parse_signature_input_header,
    pem_to_adcp_jwk,
    private_key_from_jwk,
    sign_request,
    verify_request_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")
ES256_KEY = next(k for k in KEYS if k["kid"] == "test-es256-2026")


def _verify_options(keys: list[dict]) -> VerifyOptions:
    return VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"create_media_buy"}),
        ),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": keys}),
    )


def _run(coro):
    return asyncio.run(coro)


def test_in_memory_provider_satisfies_protocol() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key, key_id="test-ed25519-2026", algorithm="ed25519"
    )
    assert isinstance(provider, SigningProvider)
    assert provider.key_id() == "test-ed25519-2026"
    assert provider.algorithm() == "ed25519"


def test_async_sign_then_verify_ed25519() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key, key_id="test-ed25519-2026", algorithm="ed25519"
    )

    signed = _run(
        async_sign_request(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            provider=provider,
            signing_profile_version="3.2",
        )
    )

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    result = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=_verify_options([ED25519_KEY]),
    )
    assert result.key_id == "test-ed25519-2026"
    assert result.alg == "ed25519"


def test_async_sign_then_verify_es256() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ES256_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key,
        key_id="test-es256-2026",
        algorithm="ecdsa-p256-sha256",
    )

    signed = _run(
        async_sign_request(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            provider=provider,
            signing_profile_version="3.2",
        )
    )

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    result = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=_verify_options([ES256_KEY]),
    )
    assert result.alg == "ecdsa-p256-sha256"


def test_async_sign_includes_content_digest_when_requested() -> None:
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key, key_id="test-ed25519-2026", algorithm="ed25519"
    )
    signed = _run(
        async_sign_request(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers={"Content-Type": "application/json"},
            body=body,
            provider=provider,
            cover_content_digest=True,
            signing_profile_version="3.2",
        )
    )
    assert signed.content_digest is not None
    assert '"content-digest"' in signed.signature_input


def test_sync_and_async_byte_identical_for_ed25519() -> None:
    """With identical inputs (including nonce/created), sync and async
    paths produce byte-identical headers — the canonicalization spine is
    shared and Ed25519 is deterministic.
    """
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key, key_id="test-ed25519-2026", algorithm="ed25519"
    )

    pinned = {
        "method": "POST",
        "url": "https://seller.example.com/adcp/create_media_buy",
        "headers": {"Content-Type": "application/json"},
        "body": body,
        "created": 1714500000,
        "nonce": "AAAAAAAAAAAAAAAAAAAAAA",
        "signing_profile_version": "3.2",
    }
    sync_signed = sign_request(
        **pinned, private_key=private_key, key_id="test-ed25519-2026", alg="ed25519"
    )
    async_signed = _run(async_sign_request(**pinned, provider=provider))

    assert sync_signed.signature_input == async_signed.signature_input
    assert sync_signed.signature == async_signed.signature
    assert sync_signed.content_digest == async_signed.content_digest


def test_sync_and_async_signature_input_identical_for_es256() -> None:
    """Signature-Input and Content-Digest must match between sync and
    async ECDSA paths. The signature value itself is non-deterministic
    (random k), so don't compare it — but everything fed INTO the
    signer is the same canonicalization spine, and that's the
    regression risk: a future change to `_prepare_signature` that
    diverges only on one alg path would slip past the Ed25519 test.
    """
    body = b'{"plan_id":"plan_001"}'
    private_key = private_key_from_jwk(ES256_KEY, d_field="_private_d_for_test_only")
    provider = InMemorySigningProvider(
        private_key=private_key,
        key_id="test-es256-2026",
        algorithm="ecdsa-p256-sha256",
    )

    pinned = {
        "method": "POST",
        "url": "https://seller.example.com/adcp/create_media_buy",
        "headers": {"Content-Type": "application/json"},
        "body": body,
        "created": 1714500000,
        "nonce": "AAAAAAAAAAAAAAAAAAAAAA",
        "cover_content_digest": True,
        "signing_profile_version": "3.2",
    }
    sync_signed = sign_request(
        **pinned,
        private_key=private_key,
        key_id="test-es256-2026",
        alg="ecdsa-p256-sha256",
    )
    async_signed = _run(async_sign_request(**pinned, provider=provider))

    assert sync_signed.signature_input == async_signed.signature_input
    assert sync_signed.content_digest == async_signed.content_digest


def test_key_id_with_quotes_and_backslash_round_trips() -> None:
    """RFC 8941 §3.3.3: ``"`` and ``\\`` are the only sf-string escapes.

    Without the escaping fix, a key_id containing ``"`` would terminate
    the keyid param early, the rest would bleed into the next param,
    parse would diverge, and verify would fail. The fix escapes both
    chars at the serialization point.
    """
    body = b"{}"
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    weird_kid = 'kid"with\\quotes-and-backslash'

    # Build a JWK that advertises the same weird kid so verify can find it.
    kid_jwk = dict(ED25519_KEY)
    kid_jwk["kid"] = weird_kid

    signed = sign_request(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=weird_kid,
        alg="ed25519",
        signing_profile_version="3.2",
    )

    parsed = parse_signature_input_header(signed.signature_input)
    assert parsed["sig1"].params["keyid"] == weird_kid

    headers = {"Content-Type": "application/json", **signed.as_dict()}
    result = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=_verify_options([kid_jwk]),
    )
    assert result.key_id == weird_kid


def test_pem_to_adcp_jwk_matches_generated_keypair_ed25519() -> None:
    """The helper produces the same JWK shape as ``generate_signing_keypair``."""
    pem, expected_jwk = generate_signing_keypair(
        alg="ed25519", kid="round-trip-test", purpose="webhook-signing"
    )
    derived = pem_to_adcp_jwk(pem, kid="round-trip-test", purpose="webhook-signing")
    assert derived == expected_jwk


def test_pem_to_adcp_jwk_matches_generated_keypair_es256() -> None:
    pem, expected_jwk = generate_signing_keypair(
        alg="es256", kid="es256-round-trip", purpose="request-signing"
    )
    derived = pem_to_adcp_jwk(pem, kid="es256-round-trip", purpose="request-signing")
    assert derived == expected_jwk


def test_pem_to_adcp_jwk_accepts_spki_public_pem() -> None:
    """KMS deployments often only have the SPKI public PEM available —
    the private half never leaves the managed store."""
    pem, _ = generate_signing_keypair(alg="ed25519", kid="kms-style", purpose="request-signing")
    private = serialization.load_pem_private_key(pem, password=None)
    spki_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    derived = pem_to_adcp_jwk(spki_pem, kid="kms-style", purpose="request-signing")
    assert derived["adcp_use"] == "request-signing"
    assert derived["alg"] == "EdDSA"
    assert derived["key_ops"] == ["verify"]
    assert "d" not in derived  # never include private scalar
    assert derived["kid"] == "kms-style"


def test_pem_to_adcp_jwk_rejects_invalid_purpose() -> None:
    pem, _ = generate_signing_keypair(alg="ed25519", kid="x", purpose="request-signing")
    with pytest.raises(ValueError, match="purpose must be one of"):
        pem_to_adcp_jwk(pem, kid="x", purpose="some-other-purpose")  # type: ignore[arg-type]


def test_pem_to_adcp_jwk_rejects_empty_kid() -> None:
    pem, _ = generate_signing_keypair(alg="ed25519", kid="x", purpose="request-signing")
    with pytest.raises(ValueError, match="kid must be a non-empty string"):
        pem_to_adcp_jwk(pem, kid="", purpose="request-signing")


def test_pem_to_adcp_jwk_rejects_rsa_pem() -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="unsupported private key type"):
        pem_to_adcp_jwk(rsa_pem, kid="x", purpose="request-signing")


def test_in_memory_provider_rejects_unknown_algorithm() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="algorithm must be one of"):
        InMemorySigningProvider(
            private_key=private_key,
            key_id="x",
            algorithm="rsa-pss",  # type: ignore[arg-type]
        )


def test_in_memory_provider_rejects_empty_key_id() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="key_id must be a non-empty string"):
        InMemorySigningProvider(private_key=private_key, key_id="", algorithm="ed25519")


def test_in_memory_provider_rejects_ed25519_key_with_es256_algorithm() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="requires an EC private key"):
        InMemorySigningProvider(private_key=private_key, key_id="x", algorithm="ecdsa-p256-sha256")


def test_in_memory_provider_rejects_es256_key_with_ed25519_algorithm() -> None:
    private_key = private_key_from_jwk(ES256_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="requires an Ed25519 private key"):
        InMemorySigningProvider(private_key=private_key, key_id="x", algorithm="ed25519")


def test_in_memory_provider_rejects_wrong_curve_for_es256() -> None:
    """A non-P-256 EC key would silently pass `sign_signature_base`'s
    isinstance check and fail later with an opaque OverflowError on
    `r.to_bytes(32, ...)`. The constructor-time check fails clearly."""
    from cryptography.hazmat.primitives.asymmetric import ec

    p384_key = ec.generate_private_key(ec.SECP384R1())
    with pytest.raises(ValueError, match="requires SECP256R1"):
        InMemorySigningProvider(private_key=p384_key, key_id="x", algorithm="ecdsa-p256-sha256")


def test_sign_request_rejects_key_id_with_control_characters() -> None:
    """RFC 8941 §3.3.3 sf-string permits only printable ASCII. CRLF in a
    key_id would otherwise produce a Signature-Input header with a literal
    line break — a header-injection vector at non-httpx integrators."""
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="key_id contains character"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="kid\r\nInjected: 1",
            alg="ed25519",
            signing_profile_version="3.2",
        )


def test_sign_request_rejects_key_id_with_non_ascii() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="key_id contains character"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="kid”",  # right double quotation mark — sf-string parser-divergence risk
            alg="ed25519",
            signing_profile_version="3.2",
        )


def test_sign_request_rejects_label_with_crlf() -> None:
    """``label`` lands unquoted in both Signature-Input and Signature
    headers; a CRLF here would inject extra header bytes. RFC 8941
    §3.1.2 sf-keys are restricted to a token grammar."""
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="not a valid RFC 8941 sf-key"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="ok",
            alg="ed25519",
            label="sig1\r\nX-Injected: 1",
            signing_profile_version="3.2",
        )


def test_sign_request_rejects_label_starting_with_uppercase() -> None:
    """RFC 8941 §3.1.2 sf-keys must start with lowercase letter or '*'."""
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="must start with a lowercase letter"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="ok",
            alg="ed25519",
            label="Sig1",
            signing_profile_version="3.2",
        )


def test_sign_request_rejects_empty_label() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="label must be a non-empty"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="ok",
            alg="ed25519",
            label="",
            signing_profile_version="3.2",
        )


def test_pem_to_adcp_jwk_rejects_rsa_public_pem() -> None:
    """The SPKI public-PEM path through `load_pem_public_key` must
    reject RSA the same way the private-PEM path does."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_spki = rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(ValueError, match="unsupported public key type"):
        pem_to_adcp_jwk(rsa_spki, kid="x", purpose="request-signing")


def test_sign_request_rejects_tag_with_control_characters() -> None:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    with pytest.raises(ValueError, match="tag contains character"):
        sign_request(
            method="POST",
            url="https://seller.example.com/x",
            headers={},
            body=b"{}",
            private_key=private_key,
            key_id="ok",
            alg="ed25519",
            tag="adcp\x00bad",
            signing_profile_version="3.2",
        )
