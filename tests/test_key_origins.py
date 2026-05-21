"""Tests for :mod:`adcp.signing.key_origins`.

Behavior under test (matches ADCP request-signing spec #3690 step 7):

* Declared origin equals resolved ``jwks_uri`` host → success (no raise).
* Purpose absent from ``key_origins`` → raises ``*_key_origin_missing``.
* Declared origin differs from resolved host → raises
  ``*_key_origin_mismatch``.
* Canonicalization: ASCII-lowercase + IDNA-A-label so case differences
  and IDN U-label vs A-label compare equal.
* ``code_family`` switches between request and webhook code families.
* ``None`` / empty ``key_origins`` map equivalent.
"""

from __future__ import annotations

import pytest

from adcp.signing.errors import (
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH,
    WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING,
    SignatureVerificationError,
)
from adcp.signing.key_origins import check_key_origin_consistency

# ----- success path -----


def test_consistency_passes_when_declared_matches_resolved() -> None:
    # No raise — the function returns None on success.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/.well-known/jwks.json",
        key_origins={"request_signing": "https://keys.brand.com"},
        purpose="request_signing",
    )


def test_consistency_passes_with_bare_host_declaration() -> None:
    # Capabilities may declare ``identity.key_origins`` as bare hosts.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/.well-known/jwks.json",
        key_origins={"request_signing": "keys.brand.com"},
        purpose="request_signing",
    )


def test_consistency_passes_case_insensitive() -> None:
    # Spec mandates origin canonicalization (lowercase host). A
    # mixed-case declaration should not silently reject a lowercased
    # resolved URI.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/.well-known/jwks.json",
        key_origins={"request_signing": "https://KEYS.Brand.Com"},
        purpose="request_signing",
    )


# ----- missing-declaration path -----


def test_consistency_raises_missing_when_purpose_absent() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={"webhook_signing": "https://keys.brand.com"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISSING
    assert exc_info.value.step == 7


def test_consistency_raises_missing_when_key_origins_is_none() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins=None,
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISSING


def test_consistency_raises_missing_when_key_origins_is_empty() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISSING


def test_consistency_missing_includes_posture_when_supplied() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={},
            purpose="request_signing",
            posture="required",
        )
    assert "posture=required" in str(exc_info.value)


# ----- mismatch path -----


def test_consistency_raises_mismatch_on_different_host() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://attacker.example.org/.well-known/jwks.json",
            key_origins={"request_signing": "https://keys.brand.com"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH


def test_consistency_raises_mismatch_on_subdomain_drift() -> None:
    # A subdomain is not the declared origin — origins are exact hosts,
    # not eTLD+1 (host-bound JWKS prevents lateral movement within an
    # operator's own subdomains).
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://other-tenant.brand.com/.well-known/jwks.json",
            key_origins={"request_signing": "https://keys.brand.com"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH


def test_consistency_raises_mismatch_on_invalid_jwks_uri() -> None:
    # An unparseable ``jwks_uri`` cannot match anything; fail closed.
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="not a url",
            key_origins={"request_signing": "https://keys.brand.com"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH


# ----- webhook code family -----


def test_consistency_webhook_family_uses_webhook_codes() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://attacker.example.org/.well-known/jwks.json",
            key_origins={"webhook_signing": "https://keys.brand.com"},
            purpose="webhook_signing",
            code_family="webhook",
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH


def test_consistency_webhook_family_missing_uses_webhook_code() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={},
            purpose="webhook_signing",
            code_family="webhook",
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING
