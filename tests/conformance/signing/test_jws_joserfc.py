"""Regression tests for the AdCP profile rules kept on top of ``joserfc``.

``adcp.signing.jws`` hands JOSE parsing and signature verification to
``joserfc``. These tests check that each AdCP rule still holds: the strict
JSON pre-parse, the alg allow-list, ``typ`` / ``kid`` / ``crit``, strict
base64url, the Ed25519-only ``EdDSA`` pin, and the full-length P-256
coordinate check. They also check that joserfc's defaults do not reject what
the profile accepts: unknown header parameters, large payloads, and the
deprecated ``EdDSA`` alg name.

Tokens are signed with ``cryptography`` directly, not with joserfc, so the
library is never only checking its own output.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import warnings
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519

from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    b64url_encode,
    sign_signature_base,
)
from adcp.signing.jws import (
    JwsMalformedError,
    JwsSignatureInvalidError,
    averify_detached_jws,
    averify_jws_document,
    parse_compact_jws,
    parse_general_json_jws,
    verify_detached_jws,
    verify_jws_document,
)

TYP = "adcp-gov-revocation+jws"
KID = "gov-key-1"
PAYLOAD: dict[str, Any] = {
    "version": 1,
    "issuer": "https://gov.example.com",
    "updated": "2026-04-18T14:00:00Z",
    "next_update": "2026-04-18T14:15:00Z",
    "revoked_jtis": [],
    "revoked_kids": ["gov-2026-03"],
}


# -- helpers ------------------------------------------------------------


def _ed25519() -> tuple[dict[str, Any], ed25519.Ed25519PrivateKey]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed25519",
        "alg": "EdDSA",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": KID,
        "x": b64url_encode(private_key.public_key().public_bytes_raw()),
    }
    return jwk, private_key


def _es256() -> tuple[dict[str, Any], ec.EllipticCurvePrivateKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "alg": "ES256",
        "use": "sig",
        "key_ops": ["verify"],
        "kid": KID,
        "x": b64url_encode(numbers.x.to_bytes(32, "big")),
        "y": b64url_encode(numbers.y.to_bytes(32, "big")),
    }
    return jwk, private_key


def _header(**overrides: Any) -> dict[str, Any]:
    header: dict[str, Any] = {"alg": "EdDSA", "kid": KID, "typ": TYP}
    header.update(overrides)
    return {k: v for k, v in header.items() if v is not None}


def _b64_json(value: Any) -> str:
    return b64url_encode(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _sign_raw(b64_header: str, b64_payload: str, private_key: Any, internal_alg: str) -> str:
    signature = sign_signature_base(
        alg=internal_alg,
        private_key=private_key,
        signature_base=(b64_header + "." + b64_payload).encode("ascii"),
    )
    return f"{b64_header}.{b64_payload}.{b64url_encode(signature)}"


def _sign(
    private_key: Any,
    *,
    header: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    internal_alg: str = ALG_ED25519,
) -> str:
    return _sign_raw(
        _b64_json(header if header is not None else _header()),
        _b64_json(payload if payload is not None else PAYLOAD),
        private_key,
        internal_alg,
    )


def _resolver(jwk: dict[str, Any]) -> Any:
    def resolve(kid: str) -> dict[str, Any] | None:
        return jwk if kid == jwk["kid"] else None

    return resolve


def _verify(token: str | dict[str, Any], jwk: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return verify_jws_document(token, jwks_resolver=_resolver(jwk), expected_typ=TYP, **kwargs)


# -- strict JSON (parser-differential defense) ---------------------------


def test_duplicate_key_in_protected_header_rejected() -> None:
    jwk, key = _ed25519()
    raw_header = f'{{"alg":"EdDSA","kid":"{KID}","typ":"other+jws","typ":"{TYP}"}}'
    token = _sign_raw(b64url_encode(raw_header.encode()), _b64_json(PAYLOAD), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="protected header is not valid JSON"):
        _verify(token, jwk)


def test_duplicate_key_in_payload_rejected_even_when_signed() -> None:
    # A validly signed list whose second "revoked_kids" erases the first:
    # json.loads (what joserfc uses) would read the empty one.
    jwk, key = _ed25519()
    raw_payload = (
        '{"version":1,"issuer":"https://gov.example.com",'
        '"revoked_kids":["gov-2026-03"],"revoked_kids":[]}'
    )
    token = _sign_raw(_b64_json(_header()), b64url_encode(raw_payload.encode()), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="JWS payload is not valid JSON: duplicate_key"):
        _verify(token, jwk)


def test_nested_duplicate_key_in_payload_rejected() -> None:
    jwk, key = _ed25519()
    raw_payload = '{"version":1,"meta":{"a":1,"a":2}}'
    token = _sign_raw(_b64_json(_header()), b64url_encode(raw_payload.encode()), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="duplicate_key"):
        _verify(token, jwk)


@pytest.mark.parametrize("raw_payload", [b'{"version":NaN}', b'{"version":1e999}'])
def test_non_finite_number_in_payload_rejected(raw_payload: bytes) -> None:
    jwk, key = _ed25519()
    token = _sign_raw(_b64_json(_header()), b64url_encode(raw_payload), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="not valid JSON"):
        _verify(token, jwk)


def test_non_utf8_payload_rejected() -> None:
    jwk, key = _ed25519()
    token = _sign_raw(
        _b64_json(_header()), b64url_encode('{"v":"é"}'.encode("utf-16")), key, ALG_ED25519
    )
    with pytest.raises(JwsMalformedError, match="not valid JSON: invalid_utf8"):
        _verify(token, jwk)


def test_header_must_be_object() -> None:
    jwk, key = _ed25519()
    token = _sign_raw(_b64_json(["alg", "EdDSA"]), _b64_json(PAYLOAD), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="protected header is not a JSON object"):
        _verify(token, jwk)


# -- alg allow-list -----------------------------------------------------


def test_alg_none_with_empty_signature_rejected() -> None:
    jwk, _ = _ed25519()
    token = f"{_b64_json(_header(alg='none'))}.{_b64_json(PAYLOAD)}."
    # Compact parse rejects the empty signature segment first.
    with pytest.raises(JwsMalformedError):
        _verify(token, jwk)
    # The detached path reaches the alg check directly.
    with pytest.raises(JwsMalformedError, match="alg 'none' not allowed"):
        verify_detached_jws(
            b64_protected=_b64_json(_header(alg="none")),
            b64_payload=_b64_json(PAYLOAD),
            signature=b"",
            jwks_resolver=_resolver(jwk),
            expected_typ=TYP,
        )


def test_hs256_key_confusion_rejected() -> None:
    # Classic alg-confusion: HMAC the token with the public key bytes as secret.
    jwk, _ = _ed25519()
    b64_header = _b64_json(_header(alg="HS256"))
    b64_payload = _b64_json(PAYLOAD)
    mac = hmac.new(
        jwk["x"].encode(), f"{b64_header}.{b64_payload}".encode(), hashlib.sha256
    ).digest()
    token = f"{b64_header}.{b64_payload}.{b64url_encode(mac)}"
    with pytest.raises(JwsMalformedError, match="alg 'HS256' not allowed"):
        _verify(token, jwk)


@pytest.mark.parametrize("alg", ["Ed25519", "ES384", "RS256", "eddsa", ""])
def test_algs_outside_profile_rejected(alg: str) -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(alg=alg))
    with pytest.raises(JwsMalformedError, match="not allowed"):
        _verify(token, jwk)


def test_caller_allow_list_narrows_profile() -> None:
    jwk, key = _ed25519()
    token = _sign(key)
    with pytest.raises(JwsMalformedError, match="not allowed"):
        _verify(token, jwk, allowed_algs=frozenset({"ES256"}))


def test_caller_allow_list_cannot_widen_profile() -> None:
    # Pre-joserfc this raised KeyError; a non-profile alg is now just refused.
    jwk, key = _ed25519()
    token = _sign(key, header=_header(alg="RS256"))
    with pytest.raises(JwsMalformedError, match="not allowed"):
        _verify(token, jwk, allowed_algs=frozenset({"RS256", "EdDSA"}))


def test_empty_allow_list_rejects_everything() -> None:
    jwk, key = _ed25519()
    with pytest.raises(JwsMalformedError, match="not allowed"):
        _verify(_sign(key), jwk, allowed_algs=frozenset())


def test_eddsa_verify_emits_no_deprecation_warning() -> None:
    # joserfc warns that RFC 9864 deprecates "EdDSA"; AdCP mandates the name.
    jwk, key = _ed25519()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _verify(_sign(key), jwk) == PAYLOAD


# -- typ / kid / crit ---------------------------------------------------


@pytest.mark.parametrize(
    "typ", ["ADCP-GOV-REVOCATION+JWS", "adcp-gov-revocation", "adcp-gov+jws", " " + TYP, None]
)
def test_typ_must_match_byte_for_byte(typ: str | None) -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(typ=typ))
    with pytest.raises(JwsMalformedError, match="typ"):
        _verify(token, jwk)


@pytest.mark.parametrize("kid", ["", 7, ["gov-key-1"]])
def test_kid_must_be_non_empty_string(kid: Any) -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(kid=kid))
    with pytest.raises(JwsMalformedError, match="kid"):
        _verify(token, jwk)


def test_header_rejected_before_key_resolution() -> None:
    jwk, key = _ed25519()
    calls: list[str] = []

    def resolve(kid: str) -> dict[str, Any] | None:
        calls.append(kid)
        return jwk

    token = _sign(key, header=_header(typ="other+jws"))
    with pytest.raises(JwsMalformedError):
        verify_jws_document(token, jwks_resolver=resolve, expected_typ=TYP)
    assert calls == []


def test_empty_crit_accepted() -> None:
    jwk, key = _ed25519()
    assert _verify(_sign(key, header=_header(crit=[])), jwk) == PAYLOAD


@pytest.mark.parametrize("crit", [["b64"], "b64", {}])
def test_non_empty_or_malformed_crit_rejected(crit: Any) -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(crit=crit, b64=True))
    with pytest.raises(JwsMalformedError, match="crit"):
        _verify(token, jwk)


def test_rfc7797_b64_false_without_crit_rejected() -> None:
    # joserfc honors RFC 7797; unencoded payloads need "b64" in crit, which
    # the profile forbids, so b64=false can never verify.
    jwk, key = _ed25519()
    token = _sign(key, header=_header(b64=False))
    with pytest.raises(JwsMalformedError):
        _verify(token, jwk)


def test_unknown_header_parameters_accepted() -> None:
    # joserfc's strict header check would reject these; the profile ignores
    # non-critical parameters, as the pre-joserfc verifier did.
    jwk, key = _ed25519()
    token = _sign(key, header=_header(iat=1776520800, x_adcp_note="ok"))
    assert _verify(token, jwk) == PAYLOAD


def test_registered_header_parameter_with_wrong_type_rejected() -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(jku="not-a-url"))
    with pytest.raises(JwsMalformedError):
        _verify(token, jwk)


# -- strict base64url ---------------------------------------------------


def test_padded_signature_rejected_by_parsers() -> None:
    _, key = _ed25519()
    token = _sign(key)
    b64_header, b64_payload, b64_signature = token.split(".")
    padded = b64_signature + "=" * (-len(b64_signature) % 4)
    assert padded != b64_signature
    with pytest.raises(JwsMalformedError, match="signature is not valid base64url"):
        parse_compact_jws(f"{b64_header}.{b64_payload}.{padded}")
    with pytest.raises(JwsMalformedError, match="signature is not valid base64url"):
        parse_general_json_jws(
            {"payload": b64_payload, "signatures": [{"protected": b64_header, "signature": padded}]}
        )


def test_padded_header_rejected() -> None:
    jwk, key = _ed25519()
    b64_header = _b64_json(_header())
    padded = b64_header + "=" * (-len(b64_header) % 4)
    assert padded != b64_header
    token = _sign_raw(padded, _b64_json(PAYLOAD), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="protected header is not valid base64url"):
        _verify(token, jwk)


def test_padded_payload_rejected_even_when_signed() -> None:
    jwk, key = _ed25519()
    b64_payload = _b64_json(PAYLOAD)
    padded = b64_payload + "=" * (-len(b64_payload) % 4)
    assert padded != b64_payload
    token = _sign_raw(_b64_json(_header()), padded, key, ALG_ED25519)
    with pytest.raises(JwsMalformedError):
        _verify(token, jwk)


def test_standard_alphabet_signature_rejected() -> None:
    _, key = _ed25519()
    token = _sign(key)
    b64_header, b64_payload, b64_signature = token.split(".")
    std = b64_signature.replace("-", "+").replace("_", "/")
    if std == b64_signature:  # pragma: no cover - ~1 in 10^9 for 86 chars
        pytest.skip("signature happened to contain no '-' or '_'")
    with pytest.raises(JwsMalformedError, match="not valid base64url"):
        parse_compact_jws(f"{b64_header}.{b64_payload}.{std}")


def test_non_canonical_trailing_bits_rejected() -> None:
    # An Ed25519 signature is 64 bytes -> 86 chars with 2 unused trailing bits.
    _, key = _ed25519()
    token = _sign(key)
    b64_header, b64_payload, b64_signature = token.split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(b64_signature[-1])
    tweaked = b64_signature[:-1] + alphabet[last | 0b01]
    if tweaked == b64_signature:
        tweaked = b64_signature[:-1] + alphabet[last ^ 0b10]
    # Same decoded bytes under a lenient decoder; a different wire string.
    assert base64.urlsafe_b64decode(tweaked + "==") == base64.urlsafe_b64decode(
        b64_signature + "=="
    )
    with pytest.raises(JwsMalformedError, match="not valid base64url"):
        parse_compact_jws(f"{b64_header}.{b64_payload}.{tweaked}")


# -- key / alg binding --------------------------------------------------


def test_eddsa_with_ed448_key_rejected() -> None:
    # RFC 8037 EdDSA also covers Ed448; the AdCP profile is Ed25519 only.
    private_key = ed448.Ed448PrivateKey.generate()
    jwk = {
        "kty": "OKP",
        "crv": "Ed448",
        "kid": KID,
        "x": b64url_encode(private_key.public_key().public_bytes_raw()),
    }
    b64_header, b64_payload = _b64_json(_header()), _b64_json(PAYLOAD)
    signature = private_key.sign(f"{b64_header}.{b64_payload}".encode())
    token = f"{b64_header}.{b64_payload}.{b64url_encode(signature)}"
    with pytest.raises(ValueError, match="unsupported JWK kty/crv"):
        _verify(token, jwk)


def test_es256_header_with_ed25519_key_is_signature_invalid() -> None:
    jwk, key = _ed25519()
    token = _sign(key, header=_header(alg="ES256"))
    with pytest.raises(JwsSignatureInvalidError):
        _verify(token, jwk)


def test_es256_der_signature_rejected() -> None:
    # JWS ES256 is raw r||s (RFC 7518 section 3.4); a DER signature must fail.
    jwk, key = _es256()
    b64_header, b64_payload = _b64_json(_header(alg="ES256")), _b64_json(PAYLOAD)
    der = key.sign(f"{b64_header}.{b64_payload}".encode(), ec.ECDSA(hashes.SHA256()))
    token = f"{b64_header}.{b64_payload}.{b64url_encode(der)}"
    with pytest.raises(JwsSignatureInvalidError):
        _verify(token, jwk)


def test_es256_raw_signature_general_json_round_trip() -> None:
    jwk, key = _es256()
    token = _sign(key, header=_header(alg="ES256"), internal_alg=ALG_ES256)
    b64_header, b64_payload, b64_signature = token.split(".")
    doc = {
        "payload": b64_payload,
        "signatures": [{"protected": b64_header, "signature": b64_signature}],
    }
    assert _verify(doc, jwk) == PAYLOAD


def test_jwk_with_encryption_use_rejected() -> None:
    jwk, key = _ed25519()
    jwk = {**jwk, "use": "enc", "key_ops": ["encrypt"]}
    with pytest.raises(JwsSignatureInvalidError, match="designed to be used for 'enc'"):
        _verify(_sign(key), jwk)


def test_jwk_without_verify_key_op_rejected() -> None:
    jwk, key = _ed25519()
    jwk = {**jwk, "key_ops": ["sign"]}
    with pytest.raises(JwsSignatureInvalidError):
        _verify(_sign(key), jwk)


def test_jwk_bound_to_other_alg_rejected() -> None:
    jwk, key = _ed25519()
    jwk = {**jwk, "alg": "ES256"}
    with pytest.raises(JwsSignatureInvalidError):
        _verify(_sign(key), jwk)


def test_jwk_with_rfc9864_ed25519_alg_accepted() -> None:
    jwk, key = _ed25519()
    jwk = {**jwk, "alg": "Ed25519"}
    assert _verify(_sign(key), jwk) == PAYLOAD


def test_jwk_with_extra_members_accepted() -> None:
    jwk, key = _ed25519()
    jwk = {**jwk, "adcp_use": "governance-signing"}
    assert _verify(_sign(key), jwk) == PAYLOAD


def test_unsupported_jwk_kty_raises_value_error() -> None:
    # Unchanged contract: an unusable JWK is a ValueError, not a JwsError.
    _, key = _ed25519()
    with pytest.raises(ValueError, match="unsupported JWK kty/crv"):
        _verify(_sign(key), {"kty": "RSA", "kid": KID, "n": "AQAB", "e": "AQAB"})


def test_p256_jwk_with_overlong_coordinate_rejected() -> None:
    jwk, key = _es256()
    numbers = key.public_key().public_numbers()
    jwk = {**jwk, "x": b64url_encode(numbers.x.to_bytes(33, "big"))}
    token = _sign(key, header=_header(alg="ES256"), internal_alg=ALG_ES256)
    with pytest.raises(ValueError, match="32 bytes"):
        _verify(token, jwk)


# -- general JSON shape ---------------------------------------------------


def test_unprotected_header_is_ignored() -> None:
    # kid only in the unprotected header: the profile reads protected only.
    jwk, key = _ed25519()
    token = _sign(key, header=_header(kid=None))
    b64_header, b64_payload, b64_signature = token.split(".")
    doc = {
        "payload": b64_payload,
        "signatures": [
            {"protected": b64_header, "header": {"kid": KID}, "signature": b64_signature}
        ],
    }
    with pytest.raises(JwsMalformedError, match="kid"):
        _verify(doc, jwk)


def test_flattened_json_rejected() -> None:
    jwk, key = _ed25519()
    b64_header, b64_payload, b64_signature = _sign(key).split(".")
    doc = {"payload": b64_payload, "protected": b64_header, "signature": b64_signature}
    with pytest.raises(JwsMalformedError, match="must have"):
        _verify(doc, jwk)


# -- size -----------------------------------------------------------------


def test_payload_larger_than_joserfc_default_cap_accepted() -> None:
    # joserfc caps payloads at 128 kB by default; the fetch layer's
    # max_body_bytes (1 MiB default) is the bound that applies.
    jwk, key = _ed25519()
    payload = {**PAYLOAD, "revoked_jtis": [f"01HWZX{i:020d}" for i in range(8000)]}
    token = _sign(key, payload=payload)
    assert len(token) > 128_000
    assert _verify(token, jwk) == payload


def test_long_kid_header_accepted() -> None:
    jwk, key = _ed25519()
    long_kid = "k" * 600
    jwk = {**jwk, "kid": long_kid}
    assert _verify(_sign(key, header=_header(kid=long_kid)), jwk) == PAYLOAD


# -- async paths ----------------------------------------------------------


def _async_resolver(jwk: dict[str, Any]) -> Any:
    async def resolve(kid: str) -> dict[str, Any] | None:
        return jwk if kid == jwk["kid"] else None

    return resolve


async def test_async_document_rejects_duplicate_payload_keys() -> None:
    jwk, key = _ed25519()
    raw_payload = b'{"version":1,"version":2}'
    token = _sign_raw(_b64_json(_header()), b64url_encode(raw_payload), key, ALG_ED25519)
    with pytest.raises(JwsMalformedError, match="duplicate_key"):
        await averify_jws_document(token, jwks_resolver=_async_resolver(jwk), expected_typ=TYP)


async def test_async_detached_round_trip_and_tamper() -> None:
    jwk, key = _es256()
    token = _sign(key, header=_header(alg="ES256"), internal_alg=ALG_ES256)
    b64_header, b64_payload, signature = parse_compact_jws(token)
    verified = await averify_detached_jws(
        b64_protected=b64_header,
        b64_payload=b64_payload,
        signature=signature,
        jwks_resolver=_async_resolver(jwk),
        expected_typ=TYP,
    )
    assert verified == PAYLOAD
    with pytest.raises(JwsSignatureInvalidError):
        await averify_detached_jws(
            b64_protected=b64_header,
            b64_payload=_b64_json({**PAYLOAD, "revoked_kids": []}),
            signature=signature,
            jwks_resolver=_async_resolver(jwk),
            expected_typ=TYP,
        )
