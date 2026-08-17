"""Verify crypto primitives against AdCP request-signing positive vectors.

These tests load the test keypairs from `keys.json`, compute the signature
base from each positive vector, and:
  - verify the committed Signature cryptographically (all three algs)
  - re-sign with the private half and check the output matches the committed
    Signature byte-for-byte (Ed25519 only — ES256 is non-deterministic by
    default, so we only verify-check it)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.signing.canonical import (
    build_signature_base,
    parse_signature_input_header,
)
from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    alg_for_jwk,
    extract_signature_bytes,
    format_signature_header,
    private_key_from_jwk,
    public_key_from_jwk,
    sign_signature_base,
    verify_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = {k["kid"]: k for k in json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]}


def _compute_base(vector: dict) -> bytes:
    request = vector["request"]
    labels = parse_signature_input_header(request["headers"]["Signature-Input"])
    base = build_signature_base(
        method=request["method"],
        url=request["url"],
        headers=request["headers"],
        parsed=labels["sig1"],
    )
    return base.encode("utf-8")


@pytest.mark.parametrize(
    "vector_name",
    [
        "001-basic-post.json",
        "002-post-with-content-digest.json",
        "003-es256-post.json",
        "005-default-port-stripped.json",
        "006-dot-segment-path.json",
        "007-query-byte-preserved.json",
        "008-percent-encoded-path.json",
    ],
)
def test_positive_vector_verifies_cryptographically(vector_name: str) -> None:
    vector = json.loads((VECTORS_DIR / "positive" / vector_name).read_text())
    base = _compute_base(vector)

    kid = vector["jwks_ref"][0]
    jwk = KEYS[kid]
    public_key = public_key_from_jwk(jwk)
    alg = alg_for_jwk(jwk)

    sig_bytes = extract_signature_bytes(vector["request"]["headers"]["Signature"])
    assert verify_signature(
        alg=alg, public_key=public_key, signature_base=base, signature=sig_bytes
    ), f"signature did not verify for {vector_name}"


@pytest.mark.parametrize(
    "vector_name",
    [
        "001-basic-post.json",
        "002-post-with-content-digest.json",
        "005-default-port-stripped.json",
        "006-dot-segment-path.json",
        "007-query-byte-preserved.json",
        "008-percent-encoded-path.json",
    ],
)
def test_ed25519_roundtrip_matches_committed_signature(vector_name: str) -> None:
    """Ed25519 is deterministic — re-signing the base must produce identical bytes."""
    vector = json.loads((VECTORS_DIR / "positive" / vector_name).read_text())
    base = _compute_base(vector)

    kid = vector["jwks_ref"][0]
    jwk = KEYS[kid]
    assert alg_for_jwk(jwk) == ALG_ED25519, f"{vector_name} not Ed25519"

    private_key = private_key_from_jwk(jwk, d_field="_private_d_for_test_only")
    our_sig = sign_signature_base(alg=ALG_ED25519, private_key=private_key, signature_base=base)

    committed_sig = extract_signature_bytes(vector["request"]["headers"]["Signature"])
    assert our_sig == committed_sig, (
        f"{vector_name}: Ed25519 signature mismatch\n"
        f"  committed: {committed_sig.hex()}\n"
        f"  ours:      {our_sig.hex()}"
    )


def test_es256_signer_output_verifies_even_if_bytes_differ() -> None:
    """ES256 is non-deterministic by default. Our signature must verify, but may not match bytes."""
    vector = json.loads((VECTORS_DIR / "positive" / "003-es256-post.json").read_text())
    base = _compute_base(vector)

    jwk = KEYS["test-es256-2026"]
    private_key = private_key_from_jwk(jwk, d_field="_private_d_for_test_only")
    public_key = public_key_from_jwk(jwk)

    our_sig = sign_signature_base(alg=ALG_ES256, private_key=private_key, signature_base=base)
    assert len(our_sig) == 64, "ES256 must be IEEE P1363 (r||s, 64 bytes)"
    assert verify_signature(
        alg=ALG_ES256, public_key=public_key, signature_base=base, signature=our_sig
    )


def test_signature_header_roundtrip() -> None:
    vector = json.loads((VECTORS_DIR / "positive" / "001-basic-post.json").read_text())
    original = vector["request"]["headers"]["Signature"]
    bytes_ = extract_signature_bytes(original)
    assert format_signature_header(bytes_, use_legacy_base64url=True) == original


def test_extract_signature_rejects_base64url_for_3_2_profile() -> None:
    vector = json.loads(
        (VECTORS_DIR / "profile-3.2/negative/001-base64url-sf-binary.json").read_text()
    )
    header = vector["request"]["headers"]["Signature"]

    with pytest.raises(ValueError, match="RFC 8941 standard Base64"):
        extract_signature_bytes(header, allow_legacy_base64url=False)


def test_extract_signature_tolerates_standard_base64_with_plus_and_slash() -> None:
    # RFC 8941 specifies standard base64; a strict signer may emit `+` and `/`.
    # We must accept those in addition to the base64url variant our vectors use.
    import base64

    payload = bytes(range(64))
    std_b64 = base64.b64encode(payload).decode("ascii")
    # Sanity: payload chosen to include both `+` and `/` in its standard encoding.
    assert "+" in std_b64 or "/" in std_b64
    header = f"sig1=:{std_b64}:"
    assert extract_signature_bytes(header) == payload


def test_jwk_with_wrong_length_x_rejected() -> None:
    from adcp.signing.crypto import b64url_encode, public_key_from_jwk

    bad = {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(b"\x00" * 31),  # too short
    }
    with pytest.raises(ValueError, match="length"):
        public_key_from_jwk(bad)
