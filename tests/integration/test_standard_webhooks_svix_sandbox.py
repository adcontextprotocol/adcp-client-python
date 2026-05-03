"""Real-Svix delivery test (env-gated, opt-in).

Closes the last gap the in-process svix-Python interop test can't cover:
HTTP-transport quirks between our sender and a hosted Svix endpoint.
The Python svix library validates our wire format byte-for-byte; this
test validates that the bytes also survive the hop through real HTTPS
— Content-Type charset handling, Host header normalization, transfer
encoding, anything that lives below the application protocol layer.

Skipped unless ``SVIX_SANDBOX_URL`` and ``SVIX_SANDBOX_SECRET`` are set.
Setup for a maintainer running this:

1. Sign in to https://play.svix.com (or your own Svix instance) and
   create an inbound endpoint. Svix gives you a URL like
   ``https://play.svix.com/in/e_<id>/`` and a secret like
   ``whsec_<base64>``.
2. Set the env vars:

   .. code-block:: bash

       export SVIX_SANDBOX_URL='https://play.svix.com/in/e_<id>/'
       export SVIX_SANDBOX_SECRET='whsec_<base64>'

3. Run:

   .. code-block:: bash

       pytest tests/integration/test_standard_webhooks_svix_sandbox.py -v

CI does not set these vars, so the test no-ops in CI. Run it manually
when changing anything in the Standard Webhooks signing path or the
``WebhookSender`` HTTP transport.
"""

from __future__ import annotations

import os
import uuid

import pytest

from adcp.signing.standard_webhooks import SECRET_PREFIX
from adcp.webhooks import WebhookSender

_SANDBOX_URL = os.environ.get("SVIX_SANDBOX_URL")
_SANDBOX_SECRET = os.environ.get("SVIX_SANDBOX_SECRET")

pytestmark = pytest.mark.skipif(
    not _SANDBOX_URL or not _SANDBOX_SECRET,
    reason=(
        "SVIX_SANDBOX_URL and SVIX_SANDBOX_SECRET must be set to run this "
        "test against a real Svix endpoint. See module docstring for setup."
    ),
)


@pytest.mark.asyncio
async def test_svix_sandbox_accepts_our_signed_webhook() -> None:
    """Deliver a signed webhook to a real Svix endpoint and assert 2xx.

    Svix returns 2xx only when the signature verifies — a malformed
    signature returns 400 with a clear error body. So a 2xx response
    on this endpoint is a strong signal that our Standard Webhooks
    wire format and HTTP transport are interop-correct against the
    canonical hosted implementation.
    """
    assert _SANDBOX_URL is not None  # narrow for type-checker after pytestmark
    assert _SANDBOX_SECRET is not None

    sender = WebhookSender.from_standard_webhooks_secret(
        _SANDBOX_SECRET, key_id="svix-sandbox-test"
    )
    async with sender:
        result = await sender.send_raw(
            url=_SANDBOX_URL,
            idempotency_key=f"whk_{uuid.uuid4().hex}",
            payload={
                "event": "adcp.svix_sandbox_test",
                "test_run_id": uuid.uuid4().hex,
            },
        )

    # Svix surfaces signature-verification failure as 400 with a JSON
    # error body. Any 2xx (200, 201, 202, 204) means the signature
    # verified and the message landed.
    assert result.ok, (
        f"Svix sandbox rejected our signed webhook: status={result.status_code}, "
        f"body={result.response_body[:500]!r}. The Python svix-library interop "
        f"test in test_standard_webhooks.py asserts our wire format is correct "
        f"in-process — a failure here means an HTTP-transport-level divergence "
        f"(Content-Type charset, Host header, transfer encoding) that the "
        f"in-process test can't see."
    )


@pytest.mark.asyncio
async def test_svix_sandbox_rejects_tampered_body() -> None:
    """Smoke-check that the sandbox actually verifies — a signature signed
    over body A, posted with body B, must be rejected. If this passes a
    2xx, the sandbox isn't validating signatures and the happy-path
    test above is meaningless."""
    assert _SANDBOX_URL is not None
    assert _SANDBOX_SECRET is not None

    sender = WebhookSender.from_standard_webhooks_secret(
        _SANDBOX_SECRET, key_id="svix-sandbox-test"
    )
    async with sender:
        # Sign over an empty payload, but inject extra_headers with a
        # custom marker so the sender produces a signature for the
        # original body. Then we use a custom send to deliver mismatched
        # bytes — but WebhookSender deliberately makes this hard. Instead,
        # produce two separate signed deliveries and hand-mangle the
        # second's body before POST.
        #
        # The cleanest tamper-test: sign with the right secret, then
        # POST with a deliberately-wrong webhook-signature header.
        # That uses send_raw + extra_headers to override... but
        # webhook-signature is a reserved header. So we sign with
        # one secret, swap to a sender holding a *different* secret
        # for the same URL, and assert the swap rejects.
        bogus_sender = WebhookSender.from_standard_webhooks_secret(
            SECRET_PREFIX + "A" * 43 + "=",
            key_id="bogus",
        )
        async with bogus_sender:
            result = await bogus_sender.send_raw(
                url=_SANDBOX_URL,
                idempotency_key=f"whk_{uuid.uuid4().hex}",
                payload={"event": "adcp.svix_tamper_test"},
            )

    assert not result.ok, (
        f"Svix sandbox accepted a webhook signed with the wrong secret "
        f"(status={result.status_code}). The sandbox is not verifying "
        f"signatures, which means the happy-path test in this file proves "
        f"nothing. Check the sandbox endpoint configuration."
    )
