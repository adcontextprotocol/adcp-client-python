"""The webhook profile emits only the codes its error taxonomy defines.

security.mdx (AdCP 3.2.1) § "Webhook error taxonomy" lists seventeen codes.
It has no ``webhook_signature_required``, ``_components_unexpected``,
``_jwks_unavailable`` or ``_jwks_untrusted``: checklist step 7 and the JWKS
discovery steps name ``webhook_signature_key_unknown`` for every key-resolution
failure, and senders treat any ``webhook_*`` code on a 401 as terminal, so an
undefined code is one no sender was written to read.

The retry classification that the request profile carries in its code
spelling survives in-process as ``SignatureVerificationError.transient``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from adcp.signing import errors
from adcp.signing.errors import (
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
    REQUEST_TO_WEBHOOK_CODE,
    WEBHOOK_ERROR_CODES,
    WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    SignatureVerificationError,
)
from adcp.signing.jwks import AsyncCachingJwksResolver, CachingJwksResolver, SSRFValidationError
from adcp.signing.webhook_verifier import _retag_to_webhook
from adcp.validation.version import resolve_bundle_key
from adcp.webhook_receiver import _www_authenticate_header

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADCP_VERSION = (_REPO_ROOT / "src" / "adcp" / "ADCP_VERSION").read_text().strip()

#: security.mdx (AdCP 3.2.1) § "Webhook error taxonomy", row by row. Written out
#: rather than read from ``WEBHOOK_ERROR_CODES``: this is the spec obligation
#: the SDK's set is graded against.
SPEC_WEBHOOK_CODES = frozenset(
    {
        "webhook_signature_header_malformed",
        "webhook_signature_params_incomplete",
        "webhook_signature_tag_invalid",
        "webhook_signature_alg_not_allowed",
        "webhook_signature_window_invalid",
        "webhook_signature_components_incomplete",
        "webhook_signature_key_unknown",
        "webhook_signature_key_purpose_invalid",
        "webhook_signature_key_revoked",
        "webhook_signature_revocation_stale",
        "webhook_signature_invalid",
        "webhook_signature_digest_mismatch",
        "webhook_body_malformed",
        "webhook_target_uri_malformed",
        "webhook_signature_replayed",
        "webhook_signature_rate_abuse",
        "webhook_mode_mismatch",
    }
)

#: Codes earlier releases emitted that the taxonomy does not define.
REMOVED_WEBHOOK_CODES = (
    "webhook_signature_required",
    "webhook_signature_components_unexpected",
    "webhook_signature_jwks_unavailable",
    "webhook_signature_jwks_untrusted",
)


def test_sdk_webhook_code_set_is_the_spec_list() -> None:
    assert WEBHOOK_ERROR_CODES == SPEC_WEBHOOK_CODES


@pytest.mark.parametrize(("request_code", "webhook_code"), sorted(REQUEST_TO_WEBHOOK_CODE.items()))
def test_every_table_row_maps_to_a_spec_code(request_code: str, webhook_code: str) -> None:
    assert webhook_code in SPEC_WEBHOOK_CODES, request_code


def test_every_webhook_code_constant_is_a_spec_code() -> None:
    constants = {
        name: value
        for name in dir(errors)
        if name.startswith("WEBHOOK_") and isinstance(value := getattr(errors, name), str)
    }
    assert {name: v for name, v in constants.items() if v not in SPEC_WEBHOOK_CODES} == {}


def test_vendored_webhook_vectors_expect_only_spec_codes() -> None:
    """The conformance corpus for the pinned version grades the same list."""
    vectors = _REPO_ROOT / "src" / "adcp" / "_compliance" / _ADCP_VERSION / "test-vectors"
    negatives = sorted((vectors / "webhook-signing" / "negative").glob("*.json"))
    assert negatives
    expected = {
        json.loads(path.read_text())["expected_outcome"]["error_code"] for path in negatives
    }
    assert expected <= SPEC_WEBHOOK_CODES


@pytest.mark.parametrize("code", sorted(SPEC_WEBHOOK_CODES))
def test_receiver_challenge_carries_every_spec_code(code: str) -> None:
    """``webhook_target_uri_malformed`` used to be rewritten to ``_invalid`` here."""
    assert _www_authenticate_header(code) == {"WWW-Authenticate": f'Signature error="{code}"'}


@pytest.mark.parametrize("code", REMOVED_WEBHOOK_CODES)
def test_receiver_challenge_never_carries_a_removed_code(code: str) -> None:
    assert _www_authenticate_header(code) == {
        "WWW-Authenticate": 'Signature error="webhook_signature_invalid"'
    }


# ---------------------------------------------------------------------------
# Retry classification, in-process
# ---------------------------------------------------------------------------


def _pinned_recovery() -> dict[str, str]:
    bundle = resolve_bundle_key(_ADCP_VERSION)
    enum = json.loads(
        (
            _REPO_ROOT / "schemas" / "cache" / bundle / "enums" / "request-signing-error-code.json"
        ).read_text()
    )
    return {
        code: meta["recovery"]
        for code, meta in enum["enumMetadata"].items()
        if isinstance(meta, dict)
    }


@pytest.mark.parametrize("code", sorted(_pinned_recovery()))
def test_transient_default_follows_the_pinned_recovery_class(code: str) -> None:
    expected = _pinned_recovery()[code] == "transient"
    assert SignatureVerificationError(code).transient is expected


@pytest.mark.parametrize(
    ("request_code", "transient"),
    [(REQUEST_SIGNATURE_JWKS_UNAVAILABLE, True), (REQUEST_SIGNATURE_JWKS_UNTRUSTED, False)],
)
def test_jwks_failures_share_one_wire_code_and_keep_their_class(
    request_code: str, transient: bool
) -> None:
    retagged = _retag_to_webhook(SignatureVerificationError(request_code))
    assert retagged.code == WEBHOOK_SIGNATURE_KEY_UNKNOWN
    assert retagged.transient is transient


def test_explicit_transient_overrides_the_code_default() -> None:
    assert SignatureVerificationError(WEBHOOK_SIGNATURE_KEY_UNKNOWN).transient is False
    assert SignatureVerificationError(WEBHOOK_SIGNATURE_KEY_UNKNOWN, transient=True).transient


_JWKS_URI = "https://keys.example.com/.well-known/jwks.json"


@pytest.mark.parametrize(
    ("ssrf_transient", "code"),
    [(True, REQUEST_SIGNATURE_JWKS_UNAVAILABLE), (False, REQUEST_SIGNATURE_JWKS_UNTRUSTED)],
    ids=["unresolvable", "refused"],
)
def test_caching_resolver_keeps_an_unresolvable_host_transient(
    ssrf_transient: bool, code: str
) -> None:
    """The SSRF gate marks a DNS failure transient; the resolver must not promote it."""

    def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        raise SSRFValidationError("gate", transient=ssrf_transient)

    with pytest.raises(SignatureVerificationError) as caught:
        CachingJwksResolver(_JWKS_URI, fetcher=fetcher)("kid")
    assert caught.value.code == code
    assert caught.value.transient is ssrf_transient


@pytest.mark.parametrize(
    ("ssrf_transient", "code"),
    [(True, REQUEST_SIGNATURE_JWKS_UNAVAILABLE), (False, REQUEST_SIGNATURE_JWKS_UNTRUSTED)],
    ids=["unresolvable", "refused"],
)
def test_async_caching_resolver_keeps_an_unresolvable_host_transient(
    ssrf_transient: bool, code: str
) -> None:
    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        raise SSRFValidationError("gate", transient=ssrf_transient)

    with pytest.raises(SignatureVerificationError) as caught:
        asyncio.run(AsyncCachingJwksResolver(_JWKS_URI, fetcher=fetcher)("kid"))
    assert caught.value.code == code
    assert caught.value.transient is ssrf_transient
