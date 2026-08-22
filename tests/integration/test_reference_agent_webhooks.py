"""Integration test: does a real AdCP reference agent's signed webhook
verify against our :class:`WebhookReceiver`?

## The problem this test solves

All of our webhook tests use an in-process signer + in-process receiver.
That catches wire-format bugs and the sender↔receiver contract — but it
cannot catch:

* The reference agent's actual JWKS is reachable at the claimed URL.
* The agent signs with ``adcp/webhook-signing/v1`` (not still on the legacy
  HMAC path, or missing PR #2423).
* The agent's signature base canonicalization matches ours across the
  real network (content-digest, @target-uri canonicalization).
* The dedup contract holds end-to-end — retries from the agent's
  delivery-retry policy all carry the same ``idempotency_key``.

## Why this test is skipped by default

The reference agent needs to POST to a URL we own. That URL must be
reachable from the agent's network — not ``http://localhost``. Running this
test end-to-end requires one of:

1. **A tunnel**: ngrok / Cloudflare Tunnel / Tailscale Funnel pointing a
   public URL at ``http://localhost:<port>`` on the machine running pytest.
2. **A pre-deployed public listener**: an endpoint you control that the
   reference agent is already whitelisted to POST to, which forwards the
   received request + headers back to your test harness (e.g., via a
   webhook.site bridge or your own staging service).
3. **Colocated dev**: run this test *from* the reference agent's network
   (e.g., inside the same VPC).

## How to run manually

Option 1 (ngrok)::

    # terminal 1
    pip install adcp[test]
    python -m tests.integration.test_reference_agent_webhooks

    # terminal 2
    ngrok http 8765
    # copy the public URL (e.g. https://abc123.ngrok-free.app)

    # terminal 3
    export REFERENCE_AGENT_WEBHOOK_URL=https://abc123.ngrok-free.app/webhooks/adcp
    export REFERENCE_AGENT_URL=https://test-agent.adcontextprotocol.org
    pytest tests/integration/test_reference_agent_webhooks.py -v

Option 2 (public listener): set ``REFERENCE_AGENT_WEBHOOK_URL`` to a URL
that terminates at code you control, and adapt the assertion path to poll
that service for the received webhook.

## Current deployment status (as of this commit)

PR #2423 (RFC 9421 webhook signing) was merged to the AdCP spec on
2026-04-19. The reference agent at ``test-agent.adcontextprotocol.org``
may or may not have picked up the change — this test is the check. When it
hasn't, the test fails with ``signature_missing`` (the agent is still
sending unsigned or legacy-HMAC-only webhooks). That's a useful signal,
not a bug in this SDK.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
FastAPI = fastapi.FastAPI
Request = fastapi.Request
from fastapi.responses import JSONResponse  # noqa: E402
from uvicorn import Config, Server  # noqa: E402

from adcp import ADCPClient
from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import CachingJwksResolver
from adcp.types import AgentConfig, GetProductsRequest, Protocol
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
)

REFERENCE_AGENT_URL = os.environ.get("REFERENCE_AGENT_URL")
PUBLIC_WEBHOOK_URL = os.environ.get("REFERENCE_AGENT_WEBHOOK_URL")

# Skip unless BOTH env vars are set. This test cannot produce a meaningful
# result without a reference agent to call AND a reachable URL to hand them.
pytestmark = pytest.mark.skipif(
    not (REFERENCE_AGENT_URL and PUBLIC_WEBHOOK_URL),
    reason=(
        "Integration test requires REFERENCE_AGENT_URL "
        "(e.g. https://test-agent.adcontextprotocol.org) and "
        "REFERENCE_AGENT_WEBHOOK_URL (a public URL tunneled to this process). "
        "See module docstring for setup."
    ),
)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _build_capture_app(
    received: list[dict[str, Any]],
    receiver: WebhookReceiver,
) -> FastAPI:
    """FastAPI app that captures each webhook's verify outcome into ``received``."""
    app = FastAPI()

    # Accept either the bare path or a task-type / operation-id suffix that
    # the AdCP client's webhook_url_template interpolates.
    @app.post("/webhooks/adcp")
    @app.post("/webhooks/adcp/{task_type}")
    @app.post("/webhooks/adcp/{task_type}/{operation_id}")
    async def webhook_endpoint(request: Request) -> JSONResponse:
        outcome = await receiver.receive(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=await request.body(),
        )
        if outcome.http_status is None:
            outcome = await receiver.acknowledge(outcome)
        received.append(
            {
                "rejected": outcome.rejected,
                "rejection_reason": outcome.rejection_reason,
                "duplicate": outcome.duplicate,
                "sender_identity": outcome.sender_identity,
                "task_id": getattr(outcome.payload, "task_id", None) if outcome.payload else None,
                "idempotency_key": outcome.idempotency_key,
            }
        )
        if outcome.rejected:
            return JSONResponse(
                {"error": outcome.rejection_reason},
                status_code=outcome.http_status or 400,
                headers=dict(outcome.response_headers),
            )
        return JSONResponse({"received": True}, status_code=outcome.http_status or 200)

    return app


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_agent_signs_webhook_per_2423() -> None:
    """Call an async op on the reference agent with our webhook URL; verify
    the agent's signed webhook lands and verifies.

    If this fails with ``signature_missing``, the reference agent has not
    deployed PR #2423 yet. If it fails with ``signature_invalid`` or
    ``signature_key_unknown``, the agent IS signing but something about the
    profile doesn't match — and that's exactly the kind of interop bug this
    integration test exists to catch.
    """
    received: list[dict[str, Any]] = []

    # Resolve the agent's JWKS from its adagents.json. This is the real
    # production path — a buyer doesn't hard-code the JWKS, they follow the
    # publisher's published discovery. The sync CachingJwksResolver fetches
    # on first miss and caches per-kid with a cooldown on failure.
    agent_url = REFERENCE_AGENT_URL
    assert agent_url is not None  # checked by skipif, but satisfy mypy

    resolver = CachingJwksResolver(
        jwks_uri=f"{agent_url.rstrip('/')}/.well-known/adagents.json",
    )

    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(jwks_resolver=resolver),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="reference-buyer",
            publisher_scope_for=lambda _signer: "reference-seller",
        ),
    )
    port = _pick_free_port()
    app = _build_capture_app(received, receiver)
    server = Server(Config(app=app, host="127.0.0.1", port=port, log_level="error"))
    server_task = asyncio.create_task(server.serve())

    try:
        # Wait for the server to be ready.
        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)
        assert server.started, "local webhook server never started"

        # Call an async op on the reference agent. The op MUST be one that
        # asynchronously triggers a webhook — for get_products this is
        # typically synchronous, but we call it anyway as the smoke test
        # against the simplest op. Sellers who async-queue get_products will
        # send a webhook; sellers that return synchronously won't, and this
        # test logs a skip-esque message.
        config = AgentConfig(
            id="reference",
            agent_uri=agent_url,
            protocol=Protocol.MCP,
        )
        async with ADCPClient(
            config,
            webhook_url_template=PUBLIC_WEBHOOK_URL + "/{task_type}/{operation_id}",
        ) as client:
            request = GetProductsRequest.model_validate(
                {"brief": "Test webhook integration", "buying_mode": "brief"}
            )
            result = await client.get_products(request)

        # Give the agent a generous window to deliver the webhook if the op
        # was async. 30s covers typical seller retry/back-off.
        for _ in range(60):
            if received:
                break
            await asyncio.sleep(0.5)

        if not received:
            pytest.skip(
                f"reference agent returned {getattr(result, 'success', '?')} synchronously "
                f"without delivering a webhook — this agent may not support async "
                f"get_products, or the webhook URL was not reachable from the agent's "
                f"network. This test needs an async op to fire."
            )

        # Assertions against the first received webhook.
        wh = received[0]
        assert wh["rejected"] is False, (
            f"reference agent's signed webhook failed verification: "
            f"reason={wh['rejection_reason']!r}. If this is 'signature_missing' "
            f"the agent has not deployed PR #2423. If it's 'signature_invalid' or "
            f"'signature_key_unknown', the agent signs but something about the "
            f"profile doesn't match (check adcp_use, tag, JWKS discovery)."
        )
        assert wh["sender_identity"], "verifier produced a signer with no identity"
        assert wh["idempotency_key"], (
            "reference agent's webhook payload lacks idempotency_key — "
            "PR #2417 not deployed upstream"
        )
    finally:
        server.should_exit = True
        await server_task


def _main() -> None:
    """Convenience: run the capture server standalone for manual testing.

    ``python -m tests.integration.test_reference_agent_webhooks`` — starts a
    local server on the port printed to stdout, leaves it running so an
    operator can tunnel a public URL and invoke the reference agent from
    another terminal.
    """
    received: list[dict[str, Any]] = []
    resolver_path = Path(os.environ.get("JWKS_FILE", ""))
    if not resolver_path.is_file():
        print(
            "set JWKS_FILE=<path to a jwks.json> to run standalone; using empty JWKS",
            flush=True,
        )
    # Static resolver for standalone mode — user provides the file.
    from adcp.signing import StaticJwksResolver

    jwks_dict: dict[str, Any] = (
        json.loads(resolver_path.read_text()) if resolver_path.is_file() else {"keys": []}
    )
    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(jwks_resolver=StaticJwksResolver(jwks_dict)),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="reference-buyer",
            publisher_scope_for=lambda _signer: "reference-seller",
        ),
    )
    port = int(os.environ.get("PORT", "8765"))
    app = _build_capture_app(received, receiver)
    import uvicorn

    print(f"webhook capture server listening on http://127.0.0.1:{port}/webhooks/adcp", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    _main()
