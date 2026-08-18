"""The malformed-authority gate, observed through the two verifier entry points.

`canonicalization.json` grades `canonicalize_target_uri()` by calling it
directly (test_canonicalization.py), so the vectors prove the *rule* but say
nothing about the two boundaries that translate it onto the wire:

* :func:`adcp.signing.verify_request_signature` must surface the gate as the
  ``request_target_uri_malformed`` code at step 6 -- and *not* as the
  ``request_signature_header_malformed`` produced by the neighbouring
  ``except (ValueError, KeyError)`` clause. The typed canonicalization error is
  itself a ``ValueError``, so the narrow clause is only reachable while it sits
  first; a later reorder is invisible to every direct-call test.
* :func:`adcp.webhooks.verify_webhook_signature` must retag that code into the
  webhook family through ``REQUEST_TO_WEBHOOK_CODE`` rather than falling through
  to the generic ``webhook_signature_invalid`` + "add to REQUEST_TO_WEBHOOK_CODE"
  warning.

Both tests sign against a well-formed URL and then present the malformed URL to
the verifier. Signing the malformed URL directly is not an option: the same gate
lives on the signing path, so the fixture would die before producing headers.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from adcp.signing import (
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    private_key_from_jwk,
    sign_request,
    verify_request_signature,
)
from adcp.signing.errors import (
    WEBHOOK_TARGET_URI_MALFORMED,
    SignatureVerificationError,
)
from adcp.webhooks import (
    WebhookVerifyOptions,
    sign_webhook,
    verify_webhook_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
REQUEST_ED25519 = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")
WEBHOOK_ED25519 = {
    **copy.deepcopy(REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}

# The code string canonicalization.json ships for every `reject: true` case.
# Spelled out rather than imported: it does not exist in `adcp.signing` yet, and
# an ImportError would make this module fail at collection instead of failing on
# the assertion that states the contract.
REQUEST_TARGET_URI_MALFORMED = "request_target_uri_malformed"

# `malformed-bare-ipv6` from canonicalization.json: an unbracketed IPv6 literal
# in the authority. Chosen over the other five reject shapes because it survives
# `urlsplit()` intact, so it reaches the verifier's signature-base construction
# (step 6) rather than dying earlier -- exactly the path the boundary owns.
MALFORMED_URL = "https://fe80::1/p"

SIGNED_URL = "https://seller.example.com/adcp/create_media_buy"
WEBHOOK_URL = "https://buyer.example.com/webhooks/adcp"
CREATED = 1776520800
BODY = b'{"idempotency_key":"whk_abc123","task_id":"t1"}'


def _signed_request_headers(*, url: str = SIGNED_URL) -> dict[str, str]:
    """Real request-signing headers produced by the real signer."""
    private_key = private_key_from_jwk(REQUEST_ED25519, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"}
    signed = sign_request(
        method="POST",
        url=url,
        headers=headers,
        body=BODY,
        private_key=private_key,
        key_id=REQUEST_ED25519["kid"],
        alg="ed25519",
        created=CREATED,
        signing_profile_version="3.2",
    )
    return {**headers, **signed.as_dict()}


def _request_verify_options() -> VerifyOptions:
    return VerifyOptions(
        now=float(CREATED),
        capability=VerifierCapability(covers_content_digest="either"),
        operation="create_media_buy",
        jwks_resolver=StaticJwksResolver({"keys": [REQUEST_ED25519]}),
    )


def _signed_webhook_headers(*, url: str = WEBHOOK_URL) -> dict[str, str]:
    """Real webhook-signing headers produced by the real webhook signer."""
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    headers = {"Content-Type": "application/json"}
    signed = sign_webhook(
        method="POST",
        url=url,
        headers=headers,
        body=BODY,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
    )
    return {**headers, **signed.as_dict()}


def _webhook_verify_options() -> WebhookVerifyOptions:
    return WebhookVerifyOptions(
        jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
    )


def test_request_verifier_emits_target_uri_malformed_for_bad_authority() -> None:
    """The gate reaches the wire as its own code, not the header-malformed catch-all.

    The request carries a valid signature over a well-formed URL; the verifier is
    handed the malformed one. Signature-base construction is the first thing that
    touches the URL, so whichever clause catches the canonicalization failure
    decides the code the caller (and the 401's ``WWW-Authenticate``) sees.

    This assertion fails if the narrow ``except TargetUriMalformedError`` clause is
    absent, or is moved below the ``except (ValueError, KeyError)`` clause that
    yields ``request_signature_header_malformed`` -- the reorder that no
    direct-call canonicalization test can see.
    """
    headers = _signed_request_headers()

    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_request_signature(
            method="POST",
            url=MALFORMED_URL,
            headers=headers,
            body=BODY,
            options=_request_verify_options(),
        )

    assert exc_info.value.code == REQUEST_TARGET_URI_MALFORMED, (
        f"verify_request_signature({MALFORMED_URL!r}) must reject with "
        f"{REQUEST_TARGET_URI_MALFORMED!r}; got {exc_info.value.code!r}"
    )
    assert exc_info.value.step == 6


def test_webhook_verifier_retags_target_uri_malformed_without_map_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The webhook route retags the new code instead of falling through.

    ``_retag_to_webhook`` maps request-family codes via ``REQUEST_TO_WEBHOOK_CODE``
    and, on a miss, emits ``webhook_signature_invalid`` plus a per-request warning
    that literally asks for the map to be updated. A new request-family code with
    no entry is therefore silently *tolerated* -- the caller just gets a vaguer
    code and the receiver's logs get noisier.

    The expected twin is ``webhook_target_uri_malformed``. It is named without
    the ``webhook_signature_`` prefix the rest of the family carries because the
    spec names it that way: security.mdx's webhook checklist lists it in the
    error taxonomy and requires it at step 10 for a malformed or mismatched
    authority. Retagging onto ``webhook_signature_header_malformed`` instead
    would put a stable but WRONG code on the wire.
    """
    headers = _signed_webhook_headers()

    with caplog.at_level(logging.WARNING, logger="adcp.signing.webhook_verifier"):
        with pytest.raises(SignatureVerificationError) as exc_info:
            verify_webhook_signature(
                method="POST",
                url=MALFORMED_URL,
                headers=headers,
                body=BODY,
                options=_webhook_verify_options(),
            )

    assert exc_info.value.code == WEBHOOK_TARGET_URI_MALFORMED, (
        f"verify_webhook_signature({MALFORMED_URL!r}) must retag "
        f"{REQUEST_TARGET_URI_MALFORMED!r} to {WEBHOOK_TARGET_URI_MALFORMED!r}; "
        f"got {exc_info.value.code!r}"
    )
    unmapped = [
        record.getMessage()
        for record in caplog.records
        if "REQUEST_TO_WEBHOOK_CODE" in record.getMessage()
    ]
    assert not unmapped, (
        "the request-family code must be present in REQUEST_TO_WEBHOOK_CODE; "
        f"the retag fell through to the unknown-code branch: {unmapped}"
    )
