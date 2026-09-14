"""Unit tests for the minimal JWS parse/verify primitive.

These exercise the compact and general-JSON forms against a freshly-generated
Ed25519 / ES256 key, round-tripping through the existing crypto primitives.
Negative tests cover the invariants the AdCP governance profile relies on:
``alg=none`` rejection, ``typ`` mismatch, unknown ``kid``, tampered payload,
and malformed shapes.
"""

from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    b64url_encode,
    sign_signature_base,
)
from adcp.signing.jws import (
    JwsMalformedError,
    JwsSignatureInvalidError,
    JwsUnknownKeyError,
    parse_compact_jws,
    parse_general_json_jws,
    verify_jws_document,
)

EXPECTED_TYP = "adcp-gov-revocation+jws"
PAYLOAD_JSON = {
    "version": 1,
    "issuer": "https://gov.example.com",
    "updated": "2026-04-18T14:00:00Z",
    "next_update": "2026-04-18T14:15:00Z",
    "revoked_jtis": [],
    "revoked_kids": ["gov-2026-03"],
}


# -- helpers ------------------------------------------------------------


def _ed25519_jwk_and_key() -> tuple[dict[str, object], ed25519.Ed25519PrivateKey]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "gov-key-1",
        "x": b64url_encode(public_bytes),
    }
    return jwk, private_key


def _es256_jwk_and_key() -> tuple[dict[str, object], ec.EllipticCurvePrivateKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": "gov-key-ec",
        "x": b64url_encode(numbers.x.to_bytes(32, "big")),
        "y": b64url_encode(numbers.y.to_bytes(32, "big")),
    }
    return jwk, private_key


def _sign_compact(
    header: dict[str, object],
    payload: dict[str, object] | bytes,
    *,
    jws_alg: str,
    private_key: object,
) -> str:
    b64_header = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    if isinstance(payload, dict):
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    else:
        payload_bytes = payload
    b64_payload = b64url_encode(payload_bytes)
    signing_input = (b64_header + "." + b64_payload).encode("ascii")
    internal_alg = ALG_ED25519 if jws_alg == "EdDSA" else ALG_ES256
    signature = sign_signature_base(
        alg=internal_alg,
        private_key=private_key,  # type: ignore[arg-type]
        signature_base=signing_input,
    )
    return b64_header + "." + b64_payload + "." + b64url_encode(signature)


def _compact_to_general_json(token: str) -> dict[str, object]:
    b64_header, b64_payload, b64_signature = token.split(".")
    return {
        "payload": b64_payload,
        "signatures": [
            {"protected": b64_header, "signature": b64_signature},
        ],
    }


def _resolver_for(jwk: dict[str, object]) -> object:
    def resolve(keyid: str) -> dict[str, object] | None:
        return jwk if keyid == jwk["kid"] else None

    return resolve


# -- parse_compact_jws --------------------------------------------------


def test_parse_compact_jws_splits_segments() -> None:
    # Build a fake compact-shaped string inline so the test literal isn't
    # a plausible-looking JWT (high-entropy-secret scanners flag otherwise).
    header_b64 = b64url_encode(b"FAKE-HEADER")
    payload_b64 = b64url_encode(b"FAKE-PAYLOAD")
    sig_b64 = b64url_encode(b"FAKE-SIG")
    token = f"{header_b64}.{payload_b64}.{sig_b64}"

    parsed_header, parsed_payload, parsed_sig = parse_compact_jws(token)
    # Parser returns the original base64url substrings verbatim (no decode +
    # re-encode); only the signature is decoded to bytes.
    assert parsed_header == header_b64
    assert parsed_payload == payload_b64
    assert parsed_sig == b"FAKE-SIG"


def test_parse_compact_jws_rejects_wrong_segment_count() -> None:
    with pytest.raises(JwsMalformedError, match="3 dot-separated"):
        parse_compact_jws("only.two")
    with pytest.raises(JwsMalformedError, match="3 dot-separated"):
        parse_compact_jws("one.two.three.four")


def test_parse_compact_jws_rejects_empty_segments() -> None:
    with pytest.raises(JwsMalformedError, match="empty segment"):
        parse_compact_jws(".p.s")
    with pytest.raises(JwsMalformedError, match="empty segment"):
        parse_compact_jws("h..s")


def test_parse_compact_jws_rejects_non_string() -> None:
    with pytest.raises(JwsMalformedError):
        parse_compact_jws({"not": "a string"})  # type: ignore[arg-type]


# -- parse_general_json_jws ---------------------------------------------


def test_parse_general_json_rejects_missing_fields() -> None:
    with pytest.raises(JwsMalformedError, match="must have"):
        parse_general_json_jws({"payload": "p"})
    with pytest.raises(JwsMalformedError, match="must have"):
        parse_general_json_jws({"signatures": []})


def test_parse_general_json_rejects_multiple_signatures() -> None:
    doc = {
        "payload": "cA",
        "signatures": [
            {"protected": "aA", "signature": "cw"},
            {"protected": "aA", "signature": "cw"},
        ],
    }
    with pytest.raises(JwsMalformedError, match="multiple entries"):
        parse_general_json_jws(doc)


def test_parse_general_json_rejects_empty_signatures() -> None:
    with pytest.raises(JwsMalformedError, match="non-empty array"):
        parse_general_json_jws({"payload": "cA", "signatures": []})


# -- verify_detached_jws (round-trip) -----------------------------------


@pytest.mark.parametrize(
    ("jws_alg", "factory"),
    [
        ("EdDSA", _ed25519_jwk_and_key),
        ("ES256", _es256_jwk_and_key),
    ],
)
def test_compact_jws_round_trip_verifies(jws_alg: str, factory) -> None:
    jwk, key = factory()
    header = {"alg": jws_alg, "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg=jws_alg, private_key=key)

    verified = verify_jws_document(
        token,
        jwks_resolver=_resolver_for(jwk),
        expected_typ=EXPECTED_TYP,
    )
    assert verified == PAYLOAD_JSON


def test_general_json_jws_round_trip_verifies() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    verified = verify_jws_document(
        _compact_to_general_json(token),
        jwks_resolver=_resolver_for(jwk),
        expected_typ=EXPECTED_TYP,
    )
    assert verified == PAYLOAD_JSON


# -- verify_detached_jws: negative cases --------------------------------


def test_reject_alg_none() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "none", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="alg 'none' not allowed"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_unknown_alg() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "HS256", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="not allowed"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_missing_alg() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="alg"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_wrong_typ() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": "some-other+jws"}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="typ .* does not match expected"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_missing_kid() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="kid"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_unknown_kid() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": "different-key", "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsUnknownKeyError):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_crit_with_entries() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {
        "alg": "EdDSA",
        "kid": jwk["kid"],
        "typ": EXPECTED_TYP,
        "crit": ["some-ext"],
    }
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="crit"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_tampered_payload() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)

    b64_header, _, b64_signature = token.split(".")
    # Swap in a different payload while keeping the original signature.
    tampered_payload = {**PAYLOAD_JSON, "revoked_kids": ["ATTACKER-CONTROLLED"]}
    b64_payload = b64url_encode(json.dumps(tampered_payload, separators=(",", ":")).encode())
    tampered_token = f"{b64_header}.{b64_payload}.{b64_signature}"

    with pytest.raises(JwsSignatureInvalidError):
        verify_jws_document(
            tampered_token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP
        )


def test_reject_bad_signature_bytes() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, PAYLOAD_JSON, jws_alg="EdDSA", private_key=key)
    # Flip one byte of the signature.
    b64_header, b64_payload, b64_signature = token.split(".")
    from adcp.signing.crypto import b64url_decode

    sig_bytes = bytearray(b64url_decode(b64_signature))
    sig_bytes[0] ^= 0xFF
    tampered = f"{b64_header}.{b64_payload}.{b64url_encode(bytes(sig_bytes))}"

    with pytest.raises(JwsSignatureInvalidError):
        verify_jws_document(tampered, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_non_dict_payload() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    # JSON array payload — spec requires an object.
    token = _sign_compact(header, b'[{"not":"an object"}]', jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="not a JSON object"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_reject_non_json_payload() -> None:
    jwk, key = _ed25519_jwk_and_key()
    header = {"alg": "EdDSA", "kid": jwk["kid"], "typ": EXPECTED_TYP}
    token = _sign_compact(header, b"not valid json", jws_alg="EdDSA", private_key=key)

    with pytest.raises(JwsMalformedError, match="not valid JSON"):
        verify_jws_document(token, jwks_resolver=_resolver_for(jwk), expected_typ=EXPECTED_TYP)


def test_verify_jws_document_rejects_non_string_non_dict() -> None:
    with pytest.raises(JwsMalformedError, match="compact string or JSON"):
        verify_jws_document(
            123, jwks_resolver=lambda _kid: None, expected_typ=EXPECTED_TYP  # type: ignore[arg-type]
        )
