"""End-to-end wire-compat test: 0.3-shape JSON-RPC still works.

Spins up our 1.0-based :func:`~adcp.server.a2a_server.create_a2a_server`
app, hits it with a hand-crafted 0.3 ``message/send`` request via raw
httpx (no a2a-sdk on the client side), and asserts the response lands
in the 0.3 wire shape:

- ``result.status.state == "completed"`` (lowercase spec form)
- ``result.kind == "task"`` (string discriminator)

This guards against future regressions if ``enable_v0_3_compat=True``
is accidentally dropped from :func:`create_a2a_server` — a follow-up
would get ``TASK_STATE_COMPLETED`` back and break every existing
buyer-side 0.3 client.

Pattern cribbed from ``.context/poc/poc.py``.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn

from adcp.protocols.a2a import A2AAdapter
from adcp.server import ADCPHandler
from adcp.server.a2a_server import create_a2a_server
from adcp.types.core import AgentConfig, Protocol, TaskStatus
from adcp.validation.client_hooks import ValidationHookConfig

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)


class _EchoHandler(ADCPHandler):
    """Minimal handler — returns a tiny spec-compliant payload. The
    assertions are on the JSON-RPC envelope shape, not the handler."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": []}


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _running_server() -> AsyncIterator[str]:
    port = _pick_free_port()
    app = create_a2a_server(_EchoHandler(), name="wire-compat-agent", port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn failed to start within timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_v03_message_send_gets_v03_task_response():
    """A 0.3-format ``message/send`` (camelCase ids, ``kind: "text"``
    parts, lowercase ``role: "user"``) must still succeed and come back
    in the 0.3 envelope shape."""
    rpc_v03 = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "msg-1",
                "role": "user",
                "parts": [
                    {
                        "kind": "data",
                        "data": {"skill": "get_products", "parameters": {"brief": "test"}},
                    }
                ],
            },
        },
    }

    async with _running_server() as base_url:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(base_url, json=rpc_v03)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    result = body["result"]
    # 0.3 uses a ``kind`` discriminator (string) instead of the proto's
    # ``task``/``message`` oneof name on the outer result envelope.
    assert result.get("kind") == "task", result
    # 0.3 uses the lowercase spec-string form of the enum — the whole
    # reason ``enable_v0_3_compat=True`` is load-bearing.
    state = result.get("status", {}).get("state")
    assert state == "completed", (
        f"Expected 0.3 lowercase state 'completed', got {state!r}. "
        "This test guards against enable_v0_3_compat=True being "
        "accidentally disabled in create_a2a_server."
    )


@pytest.mark.asyncio
async def test_agent_card_endpoint_advertises_both_interfaces():
    """The well-known AgentCard JSON must list both the 0.3 and 1.0
    protocol bindings under ``supportedInterfaces`` so clients of
    either era can negotiate the right transport."""
    async with _running_server() as base_url:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"{base_url}/.well-known/agent-card.json")

    assert resp.status_code == 200, resp.text
    card = resp.json()
    interfaces = card.get("supportedInterfaces") or card.get("supported_interfaces") or []
    # Extract protocol versions
    versions = {
        (iface.get("protocolVersion") or iface.get("protocol_version")) for iface in interfaces
    }
    assert "0.3" in versions, card
    assert "1.0" in versions, card


@pytest.mark.asyncio
async def test_malformed_params_returns_clean_jsonrpc_error():
    """A 0.3-shaped method name with a malformed ``params`` body must
    come back as a JSON-RPC error envelope, not a 500 / uncaught
    exception. Guards against future a2a-sdk upgrades quietly narrowing
    the 0.3 adapter's validator — we should always see a structured
    JSON-RPC error, never a transport-level failure.
    """
    # ``params.message`` intentionally missing required ``parts`` / ``role``
    # so the 0.3 validator rejects it at parse-time.
    malformed = {
        "jsonrpc": "2.0",
        "id": "bad-1",
        "method": "message/send",
        "params": {"message": {"messageId": "m-bad"}},
    }

    async with _running_server() as base_url:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(base_url, json=malformed)

    # JSON-RPC-over-HTTP returns 200 with a structured error body;
    # validation failures must never bubble up as a 500.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, f"expected JSON-RPC error envelope, got: {body}"
    assert "result" not in body, body
    # Must be a legal JSON-RPC error code (not 0 / None).
    assert isinstance(body["error"].get("code"), int)


@pytest.mark.asyncio
async def test_unknown_method_returns_method_not_found():
    """Method names outside the 0.3 / 1.0 JSON-RPC method sets must
    come back as a clean ``MethodNotFound`` error, not a transport
    failure. Ensures the router hasn't quietly narrowed."""
    unknown = {
        "jsonrpc": "2.0",
        "id": "bad-2",
        "method": "definitely/not/a/real/method",
        "params": {},
    }

    async with _running_server() as base_url:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(base_url, json=unknown)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body, body
    # JSON-RPC 2.0 reserves -32601 for Method Not Found; the a2a-sdk
    # uses this code for unknown method names.
    assert body["error"].get("code") == -32601, body


@pytest.mark.asyncio
async def test_v10_client_pinned_to_v03_against_dual_server():
    """Reverse-direction guard: our 1.0-based Python client, pinned to
    ``force_a2a_version="0.3"``, must successfully round-trip against
    the dual-advertising server's 0.3 JSON-RPC interface.

    Existing ``test_v03_message_send_gets_v03_task_response`` covers
    0.3 client → 1.0 server by hand-crafting the JSON-RPC envelope.
    This test covers the other direction: a production-shape ADCP
    caller picking the 0.3 transport via the public ``force_a2a_version``
    pin, exercising :func:`_filter_card_to_version` and
    :class:`~a2a.client.ClientFactory` transport negotiation end-to-end.

    Proves:

    - The card filter keeps only the 0.3 ``AgentInterface`` when a
      dual-advertising peer is pinned.
    - ``ClientFactory`` selects a 0.3-compatible transport and
      ``_send_and_aggregate`` drains the resulting ``StreamResponse``.
    - ``a2a_protocol_versions`` still reports both advertised versions
      (the filter is applied after the card is cached, not before).
    """
    async with _running_server() as base_url:
        config = AgentConfig(id="wire-compat-test", agent_uri=base_url, protocol=Protocol.A2A)
        adapter = A2AAdapter(config, force_a2a_version="0.3")
        # Skip schema validation — the ``_EchoHandler`` response is a
        # minimal ``{"products": []}`` stub. Validation noise here would
        # distract from the wire-compat assertion this test exists for.
        adapter.configure_validation(ValidationHookConfig(requests="off", responses="off"))

        try:
            result = await adapter.get_products({"buying_mode": "wholesale"})
        finally:
            await adapter.close()

    assert result.success, f"expected success, got status={result.status!r} error={result.error!r}"
    assert result.status == TaskStatus.COMPLETED, result.status
    assert result.data == {"products": []}, result.data
    # Card cache retains the unfiltered advertisement so callers can
    # still probe what the peer supports before pinning.
    assert adapter.a2a_protocol_versions == ["0.3", "1.0"], adapter.a2a_protocol_versions


@pytest.mark.asyncio
async def test_v10_client_pinned_to_unadvertised_version_raises():
    """``force_a2a_version`` must fail loudly when the peer advertises
    no matching interface, rather than falling through to a silent
    transport-negotiation error deep in the a2a-sdk. Guards the
    user-visible error path in :meth:`A2AAdapter._get_a2a_client`."""
    from adcp.exceptions import ADCPConnectionError

    async with _running_server() as base_url:
        config = AgentConfig(id="wire-compat-test", agent_uri=base_url, protocol=Protocol.A2A)
        adapter = A2AAdapter(config, force_a2a_version="9.9")

        try:
            with pytest.raises(ADCPConnectionError, match="does not advertise"):
                await adapter.get_products({"buying_mode": "wholesale"})
        finally:
            await adapter.close()
