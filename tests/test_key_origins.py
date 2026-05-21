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


# ----- spec-mandated structured detail fields -----


def test_consistency_mismatch_carries_structured_detail() -> None:
    # Spec #3690 step 7: ``request_signature_key_origin_mismatch`` MUST
    # carry ``{purpose, expected_origin, actual_origin}`` as structured
    # fields, not just an opaque message string. Middleware adapters
    # surface them on the 401 / in a DLQ.
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://attacker.example.org/.well-known/jwks.json",
            key_origins={"request_signing": "https://keys.brand.com"},
            purpose="request_signing",
        )
    detail = exc_info.value.detail
    assert detail is not None
    assert detail["purpose"] == "request_signing"
    assert detail["expected_origin"] == "keys.brand.com"
    assert detail["actual_origin"] == "attacker.example.org"


def test_consistency_missing_carries_structured_detail() -> None:
    # Spec #3690 step 7: ``_key_origin_missing`` MUST carry
    # ``{purpose, posture}``.
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={},
            purpose="request_signing",
            posture="required",
        )
    detail = exc_info.value.detail
    assert detail is not None
    assert detail["purpose"] == "request_signing"
    assert detail["posture"] == "required"


def test_consistency_missing_detail_omits_posture_when_unsupplied() -> None:
    # ``posture`` is optional; when the caller doesn't pass one, the
    # detail dict carries only ``purpose``. Adapters reading
    # ``detail.get("posture")`` see ``None`` rather than an empty string.
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={},
            purpose="request_signing",
        )
    detail = exc_info.value.detail
    assert detail is not None
    assert detail == {"purpose": "request_signing"}


# ----- canonicalization edge cases (regressions for reviewer findings) -----


def test_consistency_trailing_fqdn_dot_compares_equal_either_side() -> None:
    # ``host.example.`` and ``host.example`` are the same FQDN at the
    # protocol layer (the dot denotes the root zone). An attacker who
    # controls capabilities could otherwise declare the dot form while
    # the brand.json serves the no-dot form (or vice versa) and weaponize
    # the check to deny verification against the legitimate counterparty.
    # Both directions must compare equal.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com./jwks.json",  # trailing dot
        key_origins={"request_signing": "https://keys.brand.com"},
        purpose="request_signing",
    )
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/jwks.json",
        key_origins={"request_signing": "https://keys.brand.com."},  # trailing dot
        purpose="request_signing",
    )


def test_consistency_bare_host_with_port_normalizes_symmetrically() -> None:
    # Capability declaring ``keys.brand.com:8443`` (bare host with port)
    # must normalize the same way the URL form would — stripping the
    # port — so it compares equal to a resolved jwks_uri of
    # ``https://keys.brand.com/...``. Without symmetric normalization,
    # an attacker with capability-write access could supply a
    # bare-host-with-port declaration to force a mismatch against the
    # operator's brand.json origin.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/.well-known/jwks.json",
        key_origins={"request_signing": "keys.brand.com:8443"},
        purpose="request_signing",
    )


def test_consistency_bare_host_with_userinfo_rejects_or_normalizes() -> None:
    # Declarations with userinfo (``user@host``) are spec-suspicious;
    # the helper must NOT accidentally accept the host portion while
    # ignoring the user. Symmetric urlsplit-based normalization strips
    # userinfo the same way it does for URL inputs, so the comparison
    # collapses to ``host == host``.
    check_key_origin_consistency(
        jwks_uri="https://keys.brand.com/.well-known/jwks.json",
        key_origins={"request_signing": "user@keys.brand.com"},
        purpose="request_signing",
    )


def test_consistency_idn_a_label_equals_u_label() -> None:
    # IDN U-label (``münchen.example``) and A-label (Punycode
    # ``xn--mnchen-3ya.example``) refer to the same host. Canonicalization
    # to A-label via ``host.encode("idna")`` must make them compare equal
    # regardless of which form each side uses.
    check_key_origin_consistency(
        jwks_uri="https://xn--mnchen-3ya.example/.well-known/jwks.json",
        key_origins={"request_signing": "münchen.example"},
        purpose="request_signing",
    )
    check_key_origin_consistency(
        jwks_uri="https://münchen.example/.well-known/jwks.json",
        key_origins={"request_signing": "xn--mnchen-3ya.example"},
        purpose="request_signing",
    )


def test_consistency_unparseable_declared_origin_fails_closed() -> None:
    # Symmetric to ``test_consistency_raises_mismatch_on_invalid_jwks_uri``
    # but with the unparseable string on the *declared* side. A future
    # refactor must not silently invert the fail direction — both sides
    # must fail closed.
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.brand.com/.well-known/jwks.json",
            key_origins={"request_signing": "not a host"},
            purpose="request_signing",
        )
    assert exc_info.value.code == REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH
