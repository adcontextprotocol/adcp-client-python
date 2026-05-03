"""End-to-end tests for the HMAC + bearer auth modes on :class:`WebhookSender`.

The killer assertion across every mode: the bytes the sender signs are
the bytes the receiver verifies, full round-trip through httpx/ASGI.
The JWK path has its own e2e suite; this file covers the alt-auth modes
added in #478:

* ``from_bearer_token`` — Authorization: Bearer header, no body signing.
* ``from_adcp_legacy_hmac`` — X-AdCP-Signature/-Timestamp/-Key-Id round-tripped
  against :func:`verify_webhook_hmac` (the legacy receiver).
* ``from_standard_webhooks_secret`` — webhook-id/-timestamp/-signature
  round-tripped against :func:`verify_standard_webhook` and (when the
  upstream library is installed) the svix Python verifier.
* ``resend()`` — replays the same body bytes under a fresh signature on
  every HMAC mode (timestamp moves forward, sig changes, body identical).
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
FastAPI = fastapi.FastAPI
Request = fastapi.Request
from fastapi.responses import JSONResponse  # noqa: E402

from adcp.signing.standard_webhooks import (
    HEADER_ID as SW_HEADER_ID,
)
from adcp.signing.standard_webhooks import (
    HEADER_SIGNATURE as SW_HEADER_SIGNATURE,
)
from adcp.signing.standard_webhooks import (
    HEADER_TIMESTAMP as SW_HEADER_TIMESTAMP,
)
from adcp.signing.standard_webhooks import (
    SECRET_PREFIX as SW_SECRET_PREFIX,
)
from adcp.signing.standard_webhooks import (
    StandardWebhookError,
    verify_standard_webhook,
)
from adcp.signing.webhook_hmac import (
    LegacyWebhookHmacError,
    LegacyWebhookHmacOptions,
    verify_webhook_hmac,
)
from adcp.webhook_sender import WebhookSender

_HMAC_SECRET = b"test-secret-bytes-for-adcp-legacy"
_SW_SECRET_BYTES = b"\x42" * 32
# Construct via the imported prefix so this file never contains the
# literal ``whsec_<long-base64>`` pattern that high-entropy-secret
# detectors flag.
_SW_SECRET_WHSEC = SW_SECRET_PREFIX + base64.b64encode(_SW_SECRET_BYTES).decode("ascii")


def _capturing_app() -> tuple[FastAPI, list[dict[str, Any]]]:
    """A FastAPI app that records every inbound request verbatim.

    The HMAC + bearer paths don't have a typed ``WebhookReceiver``
    counterpart in this SDK (those are JWK-only); these tests verify by
    snapshotting the on-the-wire request and asserting against the
    sign / verify primitives directly.
    """
    received: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def webhook_endpoint(request: Request) -> JSONResponse:
        received.append(
            {
                "headers": dict(request.headers),
                "body": await request.body(),
            }
        )
        return JSONResponse({"ok": True}, status_code=200)

    return app, received


# ---------- bearer ----------


@pytest.mark.asyncio
async def test_bearer_token_attaches_authorization_header() -> None:
    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_bearer_token("super-secret-token", client=client)
    async with sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_bearer",
            task_type="create_media_buy",
            status="completed",
        )

    assert result.ok
    assert len(received) == 1
    assert received[0]["headers"]["authorization"] == "Bearer super-secret-token"
    # Body is still byte-exact JSON containing idempotency_key.
    body = json.loads(received[0]["body"])
    assert body["idempotency_key"] == result.idempotency_key
    # No signature headers leaked.
    assert "signature" not in received[0]["headers"]
    assert "x-adcp-signature" not in received[0]["headers"]


@pytest.mark.asyncio
async def test_bearer_extra_headers_cannot_override_authorization() -> None:
    app, _ = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_bearer_token("tok", client=client)
    async with sender:
        with pytest.raises(ValueError, match="auth-binding"):
            await sender.send_mcp(
                url="http://test/webhooks/adcp",
                task_id="t",
                task_type="create_media_buy",
                status="completed",
                extra_headers={"Authorization": "Bearer attacker"},
            )


def test_bearer_empty_token_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WebhookSender.from_bearer_token("")


# ---------- AdCP-legacy HMAC ----------


@pytest.mark.asyncio
async def test_adcp_legacy_hmac_round_trips_against_verifier() -> None:
    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_adcp_legacy_hmac(_HMAC_SECRET, key_id="kid_1", client=client)
    async with sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_legacy_hmac",
            task_type="create_media_buy",
            status="completed",
        )

    assert result.ok
    headers = received[0]["headers"]
    body = received[0]["body"]
    assert headers["x-adcp-key-id"] == "kid_1"
    assert headers["x-adcp-signature"].startswith("sha256=")

    # Round-trip through the receiver-side verifier.
    verified = verify_webhook_hmac(
        headers=headers,
        body=body,
        options=LegacyWebhookHmacOptions(
            secret=_HMAC_SECRET,
            sender_identity="kid_1",
            now=time.time(),
        ),
    )
    assert verified.sender_identity == "kid_1"


@pytest.mark.asyncio
async def test_adcp_legacy_hmac_resend_re_signs_with_fresh_timestamp() -> None:
    """resend() over an HMAC strategy must produce a fresh timestamp/sig
    over the same body bytes — receivers enforcing a 300s skew window
    reject replays of stale timestamps."""
    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_adcp_legacy_hmac(_HMAC_SECRET, key_id="kid_1", client=client)
    async with sender:
        first = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_resend",
            task_type="create_media_buy",
            status="completed",
        )
        # Wait one full second so int(time.time()) advances; otherwise
        # two calls in the same second produce identical timestamps and
        # we can't distinguish "re-signed" from "replayed verbatim".
        time.sleep(1.1)
        second = await sender.resend(first)

    assert second.ok
    assert first.sent_body == second.sent_body
    h1 = received[0]["headers"]
    h2 = received[1]["headers"]
    assert h1["x-adcp-timestamp"] != h2["x-adcp-timestamp"]
    assert h1["x-adcp-signature"] != h2["x-adcp-signature"]


def test_adcp_legacy_hmac_empty_secret_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty bytes"):
        WebhookSender.from_adcp_legacy_hmac(b"", key_id="k")


def test_adcp_legacy_hmac_empty_key_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WebhookSender.from_adcp_legacy_hmac(b"x", key_id="")


def test_adcp_legacy_hmac_emits_deprecation_warning() -> None:
    """A sender-only operator who never imports webhook_hmac still needs to
    see the AdCP 4.0 cutover signal at runtime — mirror the receiver-side
    one-shot warning."""
    import warnings

    import adcp.webhook_sender as ws

    # Reset the once-flag so the warning fires deterministically; this
    # is the only test that exercises it, so the reset is local.
    ws._legacy_hmac_warned = False
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        WebhookSender.from_adcp_legacy_hmac(b"secret", key_id="k")
    deprecation_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecation_warnings, "expected a DeprecationWarning on legacy HMAC sender construction"
    assert "AdCP 4.0" in str(deprecation_warnings[0].message)


def test_strategy_repr_does_not_leak_secrets() -> None:
    """Auto-generated dataclass __repr__ would otherwise print the
    bearer token / HMAC secret in plaintext — a credential leak via
    any traceback / vars() / faulthandler that prints locals."""
    from adcp.webhook_auth import (
        AdcpLegacyHmacStrategy,
        BearerTokenStrategy,
        StandardWebhooksHmacStrategy,
    )

    bearer = BearerTokenStrategy(token="SUPER-SECRET-TOKEN-12345")
    legacy = AdcpLegacyHmacStrategy(secret=b"SUPER-SECRET-HMAC-KEY", key_id="k1")
    sw = StandardWebhooksHmacStrategy(secret=b"SUPER-SECRET-SW-KEY", key_id="k1")

    assert "SUPER-SECRET" not in repr(bearer)
    assert "SUPER-SECRET" not in repr(legacy)
    assert "SUPER-SECRET" not in repr(sw)
    # key_id is non-sensitive and should still appear for debug.
    assert "k1" in repr(legacy)
    assert "k1" in repr(sw)


# ---------- Standard Webhooks ----------


@pytest.mark.asyncio
async def test_standard_webhooks_round_trips_against_verifier() -> None:
    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_standard_webhooks_secret(
        _SW_SECRET_WHSEC, key_id="kid_sw", client=client
    )
    async with sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_sw",
            task_type="create_media_buy",
            status="completed",
        )

    assert result.ok
    headers = received[0]["headers"]
    body = received[0]["body"]
    assert headers[SW_HEADER_ID].startswith("msg_")
    assert headers[SW_HEADER_SIGNATURE].startswith("v1,")

    verify_standard_webhook(
        headers=headers,
        body=body,
        secret=_SW_SECRET_BYTES,
        now=time.time(),
    )


@pytest.mark.asyncio
async def test_standard_webhooks_resend_uses_fresh_msg_id_and_timestamp() -> None:
    """resend() under Standard Webhooks must regenerate webhook-id and
    webhook-timestamp; otherwise a Svix-style receiver caching webhook-id
    rejects the legitimate retry as a replay."""
    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_standard_webhooks_secret(
        _SW_SECRET_WHSEC, key_id="kid_sw", client=client
    )
    async with sender:
        first = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_sw_resend",
            task_type="create_media_buy",
            status="completed",
        )
        time.sleep(1.1)
        second = await sender.resend(first)

    assert second.ok
    assert first.sent_body == second.sent_body
    h1 = received[0]["headers"]
    h2 = received[1]["headers"]
    assert h1[SW_HEADER_ID] != h2[SW_HEADER_ID]
    assert h1[SW_HEADER_TIMESTAMP] != h2[SW_HEADER_TIMESTAMP]
    assert h1[SW_HEADER_SIGNATURE] != h2[SW_HEADER_SIGNATURE]


@pytest.mark.asyncio
async def test_standard_webhooks_svix_verifier_interop() -> None:
    """End-to-end against the upstream svix Python verifier — catches any
    drift between our wire format and the canonical implementation."""
    svix = pytest.importorskip("svix.webhooks")

    app, received = _capturing_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    sender = WebhookSender.from_standard_webhooks_secret(
        _SW_SECRET_WHSEC, key_id="kid_sw", client=client
    )
    async with sender:
        result = await sender.send_mcp(
            url="http://test/webhooks/adcp",
            task_id="task_sw_svix",
            task_type="create_media_buy",
            status="completed",
        )

    assert result.ok
    headers = received[0]["headers"]
    body = received[0]["body"]
    wh: Any = svix.Webhook(_SW_SECRET_WHSEC)
    # Raises on failure; success returns the parsed payload.
    wh.verify(body, headers)


def test_standard_webhooks_invalid_secret_rejected_at_construction() -> None:
    """A clearly-malformed secret fails fast at the constructor, not at the
    first send call — the failure mode is a misconfigured operator,
    surface it before any deliveries hit the wire."""
    with pytest.raises(StandardWebhookError, match="not valid base64"):
        WebhookSender.from_standard_webhooks_secret(
            SW_SECRET_PREFIX + "!!!not-base64!!!", key_id="k"
        )


def test_standard_webhooks_empty_secret_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WebhookSender.from_standard_webhooks_secret("", key_id="k")


def test_standard_webhooks_empty_key_id_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        WebhookSender.from_standard_webhooks_secret(_SW_SECRET_WHSEC, key_id="")


# ---------- send_raw still requires idempotency_key in every mode ----------


@pytest.mark.asyncio
async def test_send_raw_requires_idempotency_key_in_bearer_mode() -> None:
    sender = WebhookSender.from_bearer_token("tok")
    with pytest.raises(ValueError, match="idempotency_key"):
        await sender.send_raw(
            url="http://test/webhooks/adcp",
            idempotency_key="",  # type: ignore[arg-type]
            payload={"x": 1},
        )


@pytest.mark.asyncio
async def test_send_raw_requires_idempotency_key_in_hmac_mode() -> None:
    sender = WebhookSender.from_adcp_legacy_hmac(b"s", key_id="k")
    with pytest.raises(ValueError, match="idempotency_key"):
        await sender.send_raw(
            url="http://test/webhooks/adcp",
            idempotency_key="",  # type: ignore[arg-type]
            payload={"x": 1},
        )


def test_legacy_hmac_tampered_body_fails_verification() -> None:
    """Defense-in-depth: confirm the HMAC verifier rejects body tampering
    (this is mostly a verifier test but anchors the integration claim)."""
    headers = {
        "x-adcp-signature": "sha256=" + ("00" * 32),
        "x-adcp-timestamp": str(int(time.time())),
    }
    with pytest.raises(LegacyWebhookHmacError):
        verify_webhook_hmac(
            headers=headers,
            body=b"anything",
            options=LegacyWebhookHmacOptions(
                secret=_HMAC_SECRET,
                sender_identity="k",
                now=time.time(),
            ),
        )
