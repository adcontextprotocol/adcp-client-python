"""``generate_signing_keypair`` — programmatic API (#217).

Round-8 webhooks-9421 agent flagged that ``adcp-keygen`` on PATH isn't
reliable from CI / subprocess contexts where the venv's bin dir isn't
on ``PATH`` (spawning ``.venv/bin/python`` directly). Callers worked
around it with ``Path(sys.executable).parent / "adcp-keygen"`` — fine,
but shell-out is the wrong shape for tests and provisioning code
that already live in the Python process.

This test locks the programmatic API contract: a single entry point
that returns ``(pem_bytes, public_jwk)``, composed with the existing
signing + webhook verification helpers.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing import generate_signing_keypair, public_key_from_jwk

# ---------------------------------------------------------------------------
# Core contract — returns (pem_bytes, public_jwk)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alg", ["ed25519", "es256"])
def test_pem_loads_via_cryptography(alg: str) -> None:
    """The returned PEM must be PKCS#8 and load via cryptography's
    ``load_pem_private_key`` — the lowest-level guarantee the API
    provides. A PEM that won't load is useless regardless of what
    JWK we built alongside it."""
    pem, _ = generate_signing_keypair(alg=alg, kid="test-kid")

    private_key = serialization.load_pem_private_key(pem, password=None)

    if alg == "ed25519":
        assert isinstance(private_key, ed25519.Ed25519PrivateKey)
    else:
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)


@pytest.mark.parametrize("alg", ["ed25519", "es256"])
def test_jwk_matches_pem_public_key(alg: str) -> None:
    """The public JWK must describe the PEM's public half. If a caller
    publishes the JWK at ``jwks_uri`` and signs with the PEM, signatures
    verify — that's the round-trip the signing flow depends on."""
    pem, jwk = generate_signing_keypair(alg=alg, kid="test-kid")

    # Reconstruct public keys from both sides; compare their raw bytes.
    private_key = serialization.load_pem_private_key(pem, password=None)
    from_pem_public = private_key.public_key()
    from_jwk_public = public_key_from_jwk(jwk)

    # The cryptography library exposes equality for public keys via
    # public_bytes comparison.
    from_pem_bytes = from_pem_public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    from_jwk_bytes = from_jwk_public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert from_pem_bytes == from_jwk_bytes


# ---------------------------------------------------------------------------
# Purpose claim — adcp_use pins the key to a signing profile
# ---------------------------------------------------------------------------


def test_default_purpose_is_request_signing() -> None:
    """``request-signing`` is the default because it's the common case
    (agent → seller tool calls). Authors writing webhook-signing keys
    need to opt in explicitly — mixing keys across profiles silently
    fails at first delivery."""
    _, jwk = generate_signing_keypair(alg="ed25519", kid="test-kid")
    assert jwk["adcp_use"] == "request-signing"


def test_webhook_signing_purpose_sets_adcp_use() -> None:
    """The webhook-signing keys used by ``WebhookSender`` need
    ``adcp_use == 'webhook-signing'`` on the JWK. Regression here
    would silently produce keys that verifiers reject."""
    _, jwk = generate_signing_keypair(alg="ed25519", kid="test-kid", purpose="webhook-signing")
    assert jwk["adcp_use"] == "webhook-signing"


def test_invalid_purpose_raises_value_error() -> None:
    """Typos in ``purpose`` should fail loud — a silent fallback would
    silently mint a key with an unexpected ``adcp_use``."""
    with pytest.raises(ValueError, match="purpose must be one of"):
        generate_signing_keypair(
            alg="ed25519",
            kid="test-kid",
            purpose="requets-signing",  # type: ignore[arg-type] # typo
        )


def test_invalid_alg_raises_value_error() -> None:
    """Same fail-loud guard for ``alg``."""
    with pytest.raises(ValueError, match="alg must be"):
        generate_signing_keypair(
            alg="rsa",  # type: ignore[arg-type]
            kid="test-kid",
        )


# ---------------------------------------------------------------------------
# Composition with WebhookSender — the PEM + JWK together sign + verify
# ---------------------------------------------------------------------------


def test_pem_loads_as_webhook_sender_private_key() -> None:
    """The PEM produced with ``purpose='webhook-signing'`` must be
    usable by ``WebhookSender.from_pem`` — the canonical programmatic
    consumer. Round-trip via the sender's loader exercises the full
    integration."""
    from adcp.webhook_sender import WebhookSender

    pem, jwk = generate_signing_keypair(alg="ed25519", kid="test-kid", purpose="webhook-signing")

    sender = WebhookSender.from_pem(
        pem,
        key_id=jwk["kid"],
        alg="ed25519",
    )
    assert sender is not None


# ---------------------------------------------------------------------------
# kid defaults + explicit override
# ---------------------------------------------------------------------------


def test_default_kid_is_non_empty_string() -> None:
    """Default kid is opaque — the format is implementation detail,
    NOT a contract downstream tooling may parse. Assert only that
    callers get a non-empty string. If the default format changes
    (it does — the random suffix was added for collision resistance),
    this test doesn't break."""
    _, jwk = generate_signing_keypair(alg="ed25519")
    assert isinstance(jwk["kid"], str)
    assert jwk["kid"]


def test_default_kid_is_collision_resistant_within_process() -> None:
    """**Same-day regeneration guard.** Two calls on the same UTC day
    must produce distinct default kids. Without the random suffix,
    callers who rotate twice in 24h end up publishing two JWKs under
    the same kid, verifiers cache the first, signatures from the
    second fail with REQUEST_SIGNATURE_INVALID — silent verification
    failure. Regression here reintroduces that footgun."""
    _, jwk_a = generate_signing_keypair(alg="ed25519")
    _, jwk_b = generate_signing_keypair(alg="ed25519")
    assert jwk_a["kid"] != jwk_b["kid"]


def test_explicit_kid_is_preserved() -> None:
    """Callers managing their own rotation supply a kid; the API
    passes it through verbatim."""
    _, jwk = generate_signing_keypair(alg="ed25519", kid="rotation-2026-04-20-a")
    assert jwk["kid"] == "rotation-2026-04-20-a"


# ---------------------------------------------------------------------------
# Encrypted PEM — passphrase round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alg", ["ed25519", "es256"])
def test_encrypted_pem_requires_passphrase_to_load(alg: str) -> None:
    """``passphrase`` parameter wraps the PEM in BestAvailableEncryption.
    Without it (None), the PEM is plain PKCS#8. Exercise both algs so
    a regression breaking the es256 encryption path can't escape."""
    passphrase = b"test-passphrase-not-a-real-secret"
    pem, _ = generate_signing_keypair(alg=alg, kid="test-kid", passphrase=passphrase)

    # Unencrypted load fails — the PEM requires the passphrase.
    with pytest.raises((TypeError, ValueError)):
        serialization.load_pem_private_key(pem, password=None)

    # Correct passphrase loads cleanly.
    key = serialization.load_pem_private_key(pem, password=passphrase)
    expected_type = ed25519.Ed25519PrivateKey if alg == "ed25519" else ec.EllipticCurvePrivateKey
    assert isinstance(key, expected_type)


# ---------------------------------------------------------------------------
# Shape contract — JWK fields per alg
# ---------------------------------------------------------------------------


def test_ed25519_jwk_shape() -> None:
    """Ed25519 JWKs MUST have OKP kty, Ed25519 crv, EdDSA alg."""
    _, jwk = generate_signing_keypair(alg="ed25519", kid="k")
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert jwk["alg"] == "EdDSA"
    assert jwk["use"] == "sig"
    assert jwk["key_ops"] == ["verify"]
    assert "x" in jwk
    # Private scalar MUST NOT be in the JWK — it's public-only by design.
    assert "d" not in jwk


def test_es256_jwk_shape() -> None:
    """ES256 JWKs MUST have EC kty, P-256 crv, ES256 alg."""
    _, jwk = generate_signing_keypair(alg="es256", kid="k")
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert jwk["alg"] == "ES256"
    assert "x" in jwk
    assert "y" in jwk
    assert "d" not in jwk


# ---------------------------------------------------------------------------
# Top-level re-export
# ---------------------------------------------------------------------------


def test_exported_from_adcp_signing() -> None:
    """``generate_signing_keypair`` is part of the signing public API.
    Callers should be able to ``from adcp.signing import
    generate_signing_keypair`` without digging into a submodule."""
    import adcp.signing as signing

    assert hasattr(signing, "generate_signing_keypair")
    assert "generate_signing_keypair" in signing.__all__


def test_cli_main_delegates_to_generate_signing_keypair(tmp_path, monkeypatch) -> None:
    """**Shared-spine invariant.** The CLI's ``main()`` must go through
    ``generate_signing_keypair`` so a regression in either surface
    shows up in both. Spy on the helper and assert the CLI called it
    with the expected kwargs — a weaker assertion (``callable(main)``)
    would pass even if someone silently re-inlined the CLI's key
    generation."""
    from adcp.signing import keygen

    captured: dict[str, object] = {}
    real = keygen.generate_signing_keypair

    def spy(**kwargs: object) -> object:
        captured.update(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(keygen, "generate_signing_keypair", spy)

    out = tmp_path / "cli.pem"
    rc = keygen.main(["--alg", "ed25519", "--out", str(out), "--kid", "cli-test-kid"])
    assert rc == 0
    assert captured == {
        "alg": "ed25519",
        "kid": "cli-test-kid",
        "purpose": "request-signing",
        "passphrase": None,
    }


# ---------------------------------------------------------------------------
# Signatures produced with the PEM verify against the JWK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alg", ["ed25519", "es256"])
def test_signature_produced_with_pem_verifies_against_jwk(alg: str) -> None:
    """End-to-end signing + verification using the PEM + JWK this
    helper produces. This is the property every other check is
    ultimately in service of."""
    import time

    from adcp.signing import (
        StaticJwksResolver,
        VerifierCapability,
        VerifyOptions,
        sign_request,
        verify_request_signature,
    )

    pem, jwk = generate_signing_keypair(alg=alg, kid="test-programmatic")
    private_key = serialization.load_pem_private_key(pem, password=None)

    alg_rfc = "ed25519" if alg == "ed25519" else "ecdsa-p256-sha256"
    body = b'{"programmatic": true}'
    url = "https://seller.example.com/adcp/sync_creatives"
    signed = sign_request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,  # type: ignore[arg-type]
        key_id=jwk["kid"],
        alg=alg_rfc,
        signing_profile_version="3.2",
    )
    headers = {"Content-Type": "application/json", **signed.as_dict()}

    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest="either",
            required_for=frozenset({"sync_creatives"}),
        ),
        operation="sync_creatives",
        jwks_resolver=StaticJwksResolver({"keys": [jwk]}),
    )
    verify_request_signature(method="POST", url=url, headers=headers, body=body, options=options)
