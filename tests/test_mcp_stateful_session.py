"""Coverage for stateful streamable-http (the default).

The default is ``stateless_http=False`` — the only mode where
``StreamableHTTPSessionManager``'s idle-reap path runs. Stateless
mode in upstream MCP holds GET-SSE streams open without idle
eviction; production adopters saw connections accumulate. Adopters
who can't run stateful (multi-replica without sticky LB on
``Mcp-Session-Id``) opt back into stateless via
``stateless_http=True``; ``session_idle_timeout`` (default 1800s)
caps idle stateful sessions.

These tests exercise:
1. Default builds a stateful session manager with
   ``session_idle_timeout=1800``.
2. End-to-end auth propagation: the SDK's built-in
   ``BearerTokenAuthMiddleware`` + ``auth_context_factory``
   surface the principal/tenant on a real ``tools/call`` POST
   through the real session manager.
3. The session-id contract: a stateful session reuses across calls
   and rejects requests without ``Mcp-Session-Id``.
4. The upstream constraint that ``session_idle_timeout`` cannot
   combine with ``stateless=True`` — we suppress the timeout
   automatically when adopters opt back into stateless rather than
   letting the upstream constructor raise.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server import ADCPHandler, create_mcp_server


class _BareHandler(ADCPHandler[Any]):
    """Minimal handler — exercises only server construction."""


def test_default_is_stateful() -> None:
    """Default flipped from stateless to stateful in v4.7. Stateless
    mode in upstream MCP holds GET-SSE streams open with no idle
    eviction, leading to the connection-leak adopters reported (~100
    stuck connections across hours). Stateful with
    ``session_idle_timeout=1800`` reaps abandoned sessions properly."""
    mcp = create_mcp_server(_BareHandler(), name="t", advertise_all=True)
    assert mcp.settings.stateless_http is False
    assert mcp.settings.json_response is True
    assert mcp._session_manager.session_idle_timeout == 1800.0
    assert mcp._session_manager.stateless is False


def test_stateless_opt_in_drops_idle_timeout() -> None:
    """Adopters who explicitly opt into stateless (multi-replica
    without affinity, no shared session store) need the upstream
    constructor to accept ``stateless=True`` — which forbids
    ``session_idle_timeout``. Verify we suppress before construction."""
    mcp = create_mcp_server(_BareHandler(), name="t", advertise_all=True, stateless_http=True)
    assert mcp.settings.stateless_http is True
    assert mcp._session_manager.session_idle_timeout is None
    assert mcp._session_manager.stateless is True


def test_stateful_opt_in_explicit_timeout() -> None:
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        session_idle_timeout=600.0,
    )
    assert mcp._session_manager.session_idle_timeout == 600.0


def test_stateful_with_disabled_timeout() -> None:
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        session_idle_timeout=None,
    )
    # Adopter explicitly opted out of reaping — pass through.
    assert mcp._session_manager.session_idle_timeout is None
    assert mcp._session_manager.stateless is False


def test_stateless_suppresses_caller_supplied_timeout() -> None:
    """Upstream raises if ``stateless=True`` AND
    ``session_idle_timeout`` is set. We suppress before construction so
    the default-arg combo doesn't blow up adopters who never touched
    these knobs."""
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=True,
        session_idle_timeout=600.0,
    )
    assert mcp._session_manager.session_idle_timeout is None


def test_negative_idle_timeout_rejected_at_boundary() -> None:
    """Upstream raises ``ValueError`` for ``session_idle_timeout <= 0``.
    We catch it at the SDK boundary so adopters see a message that
    mentions the framework parameter name, not an upstream stack trace."""
    with pytest.raises(ValueError, match="session_idle_timeout must be positive"):
        create_mcp_server(
            _BareHandler(),
            name="t",
            advertise_all=True,
            stateless_http=False,
            session_idle_timeout=0,
        )


def test_streaming_responses_keeps_json_response_off() -> None:
    """Adopters who genuinely emit progress events flip
    ``streaming_responses=True``; that path must NOT also force
    ``json_response=True`` (which would defeat the point)."""
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        streaming_responses=True,
    )
    # FastMCP's default is False; staying False means the SSE-stream
    # code path remains active for tools that need progress.
    assert mcp.settings.json_response is False


@pytest.mark.asyncio
async def test_stateful_session_reuses_across_calls() -> None:
    """End-to-end sanity: the same ``Mcp-Session-Id`` from
    ``initialize`` is accepted on a follow-up ``tools/list``. In
    stateless mode the second request would fail with "Missing session
    ID" (as today's middleware tests show); proving stateful mode
    accepts the bound session id is the regression guard."""
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            init_resp = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert init_resp.status_code == 200, init_resp.text
            session_id = init_resp.headers.get("mcp-session-id")
            assert session_id, "stateful mode must return Mcp-Session-Id"

            list_resp = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={**headers, "mcp-session-id": session_id},
            )
            assert list_resp.status_code == 200, list_resp.text


@pytest.mark.asyncio
async def test_stateful_auth_propagates_via_request_state() -> None:
    """The headline guarantee for the default flip: in stateful mode
    (the default), middleware-set state on the Starlette ``Request``
    reaches ``context_factory`` via ``meta.request_context.state``.
    Without this, the contextvar-only auth pattern would silently fail
    in production because the session task is a different async task
    than the middleware's. This test wires the SDK's built-in
    ``BearerTokenAuthMiddleware`` + ``auth_context_factory`` end-to-end
    and asserts the principal/tenant arrive at the handler."""
    from adcp.server import (
        BearerTokenAuthMiddleware,
        Principal,
        ToolContext,
        auth_context_factory,
        create_mcp_server,
        validator_from_token_map,
    )

    received: dict[str, Any] = {}

    class _Recording(_BareHandler):
        async def get_products(
            self, params: Any, context: ToolContext | None = None
        ) -> dict[str, Any]:
            received["caller_identity"] = context.caller_identity if context is not None else None
            received["tenant_id"] = context.tenant_id if context is not None else None
            return {"products": []}

    mcp = create_mcp_server(
        _Recording(),
        name="t",
        advertise_all=True,
        context_factory=auth_context_factory,
        allowed_hosts=["localhost", "127.0.0.1"],
        validation=None,
    )
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=validator_from_token_map(
            {"tk-acme": Principal(caller_identity="p-acme", tenant_id="t-acme")}
        ),
    )

    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "authorization": "Bearer tk-acme",
    }

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]

            call = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={**headers, "mcp-session-id": session_id},
            )
            assert call.status_code == 200, call.text

    assert received["caller_identity"] == "p-acme"
    assert received["tenant_id"] == "t-acme"


@pytest.mark.asyncio
async def test_stateful_rejects_request_without_session_id() -> None:
    """Inverse of the above — without ``Mcp-Session-Id`` the upstream
    SDK returns 400 ``Missing session ID``. Locks the contract that
    adopters who flip stateful mode know to thread the session id."""
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            resp = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 400
            assert "session" in resp.text.lower()
