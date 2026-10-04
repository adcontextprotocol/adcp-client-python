"""The 401 challenge for a signature failure, byte for byte.

security.mdx § Transport error taxonomy, "`WWW-Authenticate` format": "AdCP
does NOT define a realm value for request-signing challenges. Verifiers MUST
emit ``WWW-Authenticate: Signature error="<code>"`` with no ``realm``
parameter and no other parameters."

Three places in the SDK emit that header — the request leg, the webhook leg,
and the bearer middleware's challenge for an unsigned call to a ``required``
operation. These assertions are equality, because peers and conformance
harnesses match the value as a string: a substring check
(``"Signature" in challenge``) passes on a header carrying parameters the
verifier must not send, which is how the three came to disagree on one 401.

``tests/test_signed_request_verification.py`` asserts the same bytes on the
wire, on both the MCP and the A2A leg.
"""

from __future__ import annotations

import pytest

from adcp.server.auth import _www_authenticate
from adcp.signing import signature_challenge, unauthorized_response_headers
from adcp.signing.errors import (
    REQUEST_SIGNATURE_REQUIRED,
    WEBHOOK_SIGNATURE_HEADER_MALFORMED,
    SignatureVerificationError,
)
from adcp.webhook_receiver import _www_authenticate_header


def test_challenge_value_is_the_spec_string() -> None:
    assert (
        signature_challenge(REQUEST_SIGNATURE_REQUIRED)
        == 'Signature error="request_signature_required"'
    )


@pytest.mark.parametrize(
    ("leg", "challenge"),
    [
        (
            "request",
            unauthorized_response_headers(SignatureVerificationError(REQUEST_SIGNATURE_REQUIRED))[
                "WWW-Authenticate"
            ],
        ),
        (
            "webhook",
            _www_authenticate_header(WEBHOOK_SIGNATURE_HEADER_MALFORMED)["WWW-Authenticate"],
        ),
        ("bearer-fallback", _www_authenticate(REQUEST_SIGNATURE_REQUIRED)),
    ],
)
def test_every_emitter_sends_the_signature_challenge_alone(leg: str, challenge: str) -> None:
    code = WEBHOOK_SIGNATURE_HEADER_MALFORMED if leg == "webhook" else REQUEST_SIGNATURE_REQUIRED
    assert challenge == f'Signature error="{code}"'


def test_bearer_only_401_keeps_the_rfc_6750_challenge() -> None:
    """No signature failure, so the 401 is a bearer rejection and RFC 6750 §3
    governs it. The signing profile's challenge does not displace it."""
    assert _www_authenticate(None) == 'Bearer realm="adcp", error="invalid_token"'
