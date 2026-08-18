"""Direct unit tests for verifier behaviors not covered by vectors.

Covers: downstream ValueError/KeyError wrapping, type-validation of required
params, duplicate-component rejection, revocation-staleness plumbing, keyid/
nonce length bounds, and VerifyOptions frozen/kw-only.
"""

from __future__ import annotations

import dataclasses
import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from adcp.signing import (
    REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED,
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_REVOCATION_STALE,
    RevocationList,
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    sign_request,
    verify_request_signature,
)
from adcp.signing.crypto import private_key_from_jwk

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


def _sign_basic(
    *,
    method: str = "POST",
    url: str = "https://seller.example.com/adcp/create_media_buy",
    body: bytes = b"{}",
    created: int = 1776520800,
) -> tuple[dict[str, str], bytes]:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"}
    signed = sign_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        created=created,
        signing_profile_version="3.2",
    )
    return {**headers, **signed.as_dict()}, body


def _options(
    *,
    now: float = 1776520800.0,
    capability: VerifierCapability | None = None,
    revocation_list: RevocationList | None = None,
    jwks: dict[str, Any] | None = None,
) -> VerifyOptions:
    resolver = StaticJwksResolver(jwks or {"keys": [ED25519_KEY]})
    return VerifyOptions(
        now=now,
        capability=capability or VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=resolver,
        revocation_list=revocation_list,
    )


# ---- 1e: downstream ValueError/KeyError wrapping ----


def test_unsupported_derived_component_wraps_as_header_malformed() -> None:
    # Craft Signature-Input declaring @path, which the AdCP profile does not
    # support — build_signature_base raises ValueError. The verifier must wrap
    # that as REQUEST_SIGNATURE_HEADER_MALFORMED rather than bubbling it up.
    url = "https://seller.example.com/adcp/create_media_buy"
    created = 1776520800
    expires = created + 300
    sig_input = (
        '("@method" "@target-uri" "@authority" "@path");'
        f"created={created};expires={expires};"
        'nonce="a";keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    # No Content-Type header — @path is the only novel component so it reaches
    # build_signature_base (step 6) before component-set checks short-circuit.
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url=url,
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc.value.step == 6


def test_missing_covered_header_wraps_as_header_malformed() -> None:
    created = 1776520800
    expires = created + 300
    # Cover x-custom but do not provide the header in the request.
    sig_input = (
        '("@method" "@target-uri" "@authority" "x-custom");'
        f"created={created};expires={expires};"
        'nonce="a";keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc.value.step == 6


def test_non_integer_created_wraps_as_header_malformed() -> None:
    # `created=abc` — unquoted non-integer. _parse_params raises ValueError,
    # but parse_signature_input_header is what's called first (step 1).
    sig_input = (
        '("@method" "@target-uri" "@authority");'
        "created=abc;expires=1776521100;"
        'nonce="a";keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED


# ---- 1d: type validation of required params ----


def test_wrong_type_nonce_wraps_as_header_malformed() -> None:
    # nonce unquoted becomes an int — wrong type triggers type check at step 2.
    sig_input = (
        '("@method" "@target-uri" "@authority");'
        "created=1776520800;expires=1776521100;"
        'nonce=123;keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED
    assert exc.value.step == 2


# ---- 1g: duplicate covered components ----


def test_duplicate_components_rejected() -> None:
    sig_input = (
        '("@method" "@target-uri" "@authority" "@method");'
        "created=1776520800;expires=1776521100;"
        'nonce="a";keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED
    assert exc.value.step == 6


# ---- 4b: revocation staleness ----


def test_stale_revocation_list_raises_stale() -> None:
    headers, body = _sign_basic()
    stale = RevocationList(
        issuer="example.com",
        updated="2020-01-01T00:00:00Z",
        next_update="2020-01-02T00:00:00Z",
    )
    opts = _options(revocation_list=stale)
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=opts,
        )
    assert exc.value.code == REQUEST_SIGNATURE_REVOCATION_STALE
    assert exc.value.step == 9


def test_revocation_list_parses_z_suffix_on_python_3_10() -> None:
    # AdCP vectors use `Z` for UTC; Python 3.10's fromisoformat rejects it.
    # RevocationList.is_stale must normalize before parsing.
    from datetime import datetime, timezone

    stale = RevocationList(
        issuer="x",
        updated="2020-01-01T00:00:00Z",
        next_update="2020-01-01T00:00:01Z",
    )
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert stale.is_stale(now) is True

    fresh = RevocationList(
        issuer="x",
        updated="2100-01-01T00:00:00Z",
        next_update="2100-01-01T00:00:01Z",
    )
    assert fresh.is_stale(now) is False


def test_fresh_revocation_list_passes() -> None:
    headers, body = _sign_basic()
    fresh = RevocationList(
        issuer="example.com",
        updated="2100-01-01T00:00:00Z",
        next_update="2100-01-02T00:00:00Z",
    )
    opts = _options(revocation_list=fresh)
    signer = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=opts,
    )
    assert signer.key_id == "test-ed25519-2026"


# ---- 9b: keyid/nonce length bounds ----


def test_long_keyid_rejected() -> None:
    long_keyid = "k" * 300
    created = 1776520800
    expires = created + 300
    sig_input = (
        '("@method" "@target-uri" "@authority");'
        f"created={created};expires={expires};"
        f'nonce="a";keyid="{long_keyid}";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED


def test_long_nonce_rejected() -> None:
    long_nonce = "n" * 300
    created = 1776520800
    expires = created + 300
    sig_input = (
        '("@method" "@target-uri" "@authority");'
        f"created={created};expires={expires};"
        f'nonce="{long_nonce}";keyid="test-ed25519-2026";alg="ed25519";'
        'tag="adcp/request-signing/v1"'
    )
    headers = {
        "Signature-Input": f"sig1={sig_input}",
        "Signature": "sig1=:AAAA:",
    }
    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=b"{}",
            options=_options(),
        )
    assert exc.value.code == REQUEST_SIGNATURE_HEADER_MALFORMED


# ---- 8a: VerifyOptions frozen + kw-only ----


def test_verify_options_is_frozen() -> None:
    opts = _options()
    with pytest.raises(dataclasses.FrozenInstanceError):
        opts.now = 0.0  # type: ignore[misc]


def test_verify_options_rejects_positional() -> None:
    with pytest.raises(TypeError):
        VerifyOptions(  # type: ignore[misc]
            1776520800.0,
            VerifierCapability(covers_content_digest="either"),
            "create_media_buy",
            StaticJwksResolver({"keys": [ED25519_KEY]}),
        )


# ---- 6a: VerifierCapability default ----


def test_verifier_capability_defaults_to_wire_digest_policy() -> None:
    cap = VerifierCapability()
    assert cap.covers_content_digest == "either"


def test_default_capability_accepts_spec_legal_signature_without_body_binding() -> None:
    headers, body = _sign_basic()
    options = VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
    )

    signer = verify_request_signature(
        method="POST",
        url="https://seller.example.com/adcp/create_media_buy",
        headers=headers,
        body=body,
        options=options,
    )
    assert signer.key_id


def test_legacy_replay_store_warns_and_remains_compatible() -> None:
    class LegacyReplayStore:
        def __init__(self) -> None:
            self.entries: set[tuple[str, str]] = set()

        def seen(self, keyid: str, nonce: str) -> bool:
            return (keyid, nonce) in self.entries

        def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> None:
            del ttl_seconds
            self.entries.add((keyid, nonce))

        def at_capacity(self, keyid: str) -> bool:
            del keyid
            return False

    headers, body = _sign_basic()
    store = LegacyReplayStore()
    options = VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        replay_store=store,
    )

    with pytest.warns(DeprecationWarning, match=r"does not implement atomic claim\(\)"):
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )

    with warnings.catch_warnings(record=True) as emitted:
        with pytest.raises(SignatureVerificationError) as exc:
            verify_request_signature(
                method="POST",
                url="https://seller.example.com/adcp/create_media_buy",
                headers=headers,
                body=body,
                options=options,
            )
    assert emitted == []
    assert exc.value.code == "request_signature_replayed"


def test_atomic_replay_store_invalid_result_fails_closed() -> None:
    class InvalidReplayStore:
        def seen(self, keyid: str, nonce: str) -> bool:
            return False

        def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> None:
            pass

        def at_capacity(self, keyid: str) -> bool:
            return False

        def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> bool:
            return True

    headers, body = _sign_basic()
    options = VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        replay_store=InvalidReplayStore(),  # type: ignore[arg-type]
    )

    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )
    assert exc.value.code == "request_signature_rate_abuse"
    assert exc.value.step == 13


def test_atomic_replay_capacity_rejection_is_step_13() -> None:
    class CapacityReplayStore:
        def seen(self, keyid: str, nonce: str) -> bool:
            return False

        def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> bool:
            return False

        def at_capacity(self, keyid: str) -> bool:
            return False

        def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> str:
            return "capacity"

    headers, body = _sign_basic()
    options = VerifyOptions(
        now=1776520800.0,
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [ED25519_KEY]}),
        replay_store=CapacityReplayStore(),  # type: ignore[arg-type]
    )

    with pytest.raises(SignatureVerificationError) as exc:
        verify_request_signature(
            method="POST",
            url="https://seller.example.com/adcp/create_media_buy",
            headers=headers,
            body=body,
            options=options,
        )

    assert exc.value.code == "request_signature_rate_abuse"
    assert exc.value.step == 13
