"""``REQUEST_TO_WEBHOOK_CODE`` is the whole translation, and every row runs.

A reader grades the webhook profile's emitted taxonomy off the values in this
table. Three obligations follow, and the tests below are one each:

* **Every row is reachable.** A row whose webhook code no input can produce is
  a row that lies, and a conformance test written against it asserts a code the
  verifier never emits.
* **The table is total over the request family.** A ``request_signature_*``
  code with no row falls through to ``webhook_signature_invalid``, which
  mis-describes every failure except a cryptographic one.
* **No webhook code constant exists without a row.** The webhook profile's
  emitted codes are exactly the table's values; a declared constant outside
  that set is a code the profile cannot produce.

The reachability test is the one that catches a second decider. It calls
``_retag_to_webhook`` row by row, so a precheck or a branch placed ahead of the
table lookup shows up as a row whose emitted code is not the row's value.
"""

from __future__ import annotations

import logging

import pytest

from adcp.signing import errors
from adcp.signing.errors import (
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
    REQUEST_TO_WEBHOOK_CODE,
    WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    SignatureVerificationError,
)
from adcp.signing.key_origins import check_key_origin_consistency
from adcp.signing.webhook_verifier import _retag_to_webhook

# The nine request-family codes the key-discovery chain raises. Written out
# rather than derived from the table: this list is the spec obligation under
# test, and a list computed from the table would agree with the table no matter
# what the table said.
#
# security.mdx (AdCP 3.2.1) § "Webhook callbacks" → JWKS discovery walks
# brand.json fetch → ``agents[]`` match → ``jwks_uri`` fetch → ``keyid``
# resolve and names one rejection code for the chain,
# ``webhook_signature_key_unknown``. Webhook checklist step 7 repeats it and
# adds "Reject if ``keyid`` cannot be resolved to a specific ``agents[]``
# entry in the signer's brand.json". The webhook error taxonomy table carries
# no brand.json, capabilities or key-origin row.
KEY_DISCOVERY_REQUEST_CODES = (
    "request_signature_brand_json_url_missing",
    "request_signature_capabilities_unreachable",
    "request_signature_brand_json_unreachable",
    "request_signature_brand_json_malformed",
    "request_signature_brand_origin_mismatch",
    "request_signature_agent_not_in_brand_json",
    "request_signature_brand_json_ambiguous",
    "request_signature_key_origin_mismatch",
    "request_signature_key_origin_missing",
)


def _code_constants(prefix: str) -> dict[str, str]:
    return {
        name: value
        for name in dir(errors)
        if name.startswith(prefix) and isinstance(value := getattr(errors, name), str)
    }


@pytest.mark.parametrize(("request_code", "webhook_code"), sorted(REQUEST_TO_WEBHOOK_CODE.items()))
def test_every_table_row_is_reachable(request_code: str, webhook_code: str) -> None:
    """The retag emits the row's value for the row's key, for all 29 rows."""
    retagged = _retag_to_webhook(SignatureVerificationError(request_code))
    assert retagged.code == webhook_code


@pytest.mark.parametrize("request_code", KEY_DISCOVERY_REQUEST_CODES)
def test_key_discovery_collapses_to_key_unknown(request_code: str) -> None:
    """Every discovery hop rejects with the one code the webhook profile names."""
    assert REQUEST_TO_WEBHOOK_CODE[request_code] == WEBHOOK_SIGNATURE_KEY_UNKNOWN


def test_collapsed_row_logs_the_request_family_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sender learns its key did not resolve; the log keeps the real reason."""
    with caplog.at_level(logging.WARNING, logger="adcp.signing.webhook_verifier"):
        retagged = _retag_to_webhook(
            SignatureVerificationError("request_signature_brand_json_malformed")
        )
    assert retagged.code == WEBHOOK_SIGNATURE_KEY_UNKNOWN
    assert "request_signature_brand_json_malformed" in caplog.text


def test_mirrored_row_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    """A row that keeps its suffix lost no information, so it stays quiet."""
    with caplog.at_level(logging.WARNING, logger="adcp.signing.webhook_verifier"):
        retagged = _retag_to_webhook(SignatureVerificationError("request_signature_replayed"))
    assert retagged.code == "webhook_signature_replayed"
    assert caplog.text == ""


def test_table_covers_every_request_family_code() -> None:
    """An uncovered code would reach the ``webhook_signature_invalid`` fallback."""
    uncovered = sorted(
        value
        for value in _code_constants("REQUEST_").values()
        if value not in REQUEST_TO_WEBHOOK_CODE
    )
    assert uncovered == []


def test_no_webhook_code_constant_is_unreachable() -> None:
    """The webhook profile's emitted codes are exactly the table's values."""
    emitted = set(REQUEST_TO_WEBHOOK_CODE.values())
    orphaned = sorted(
        value for value in _code_constants("WEBHOOK_").values() if value not in emitted
    )
    assert orphaned == []


@pytest.mark.parametrize(
    ("key_origins", "request_code"),
    [
        (None, REQUEST_SIGNATURE_KEY_ORIGIN_MISSING),
        ({"webhook_signing": "https://other.example"}, REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH),
    ],
)
def test_key_origin_webhook_family_reads_the_table(
    key_origins: dict[str, str] | None, request_code: str
) -> None:
    """``code_family="webhook"`` is a second caller of the same table, not a second rule."""
    with pytest.raises(SignatureVerificationError) as exc_info:
        check_key_origin_consistency(
            jwks_uri="https://keys.example/.well-known/jwks.json",
            key_origins=key_origins,
            purpose="webhook_signing",
            code_family="webhook",
        )
    assert exc_info.value.code == REQUEST_TO_WEBHOOK_CODE[request_code]
