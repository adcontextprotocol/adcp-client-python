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
5. Optional active-session guardrails and stats for one-shot clients
   that create sessions without closing them.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server import ADCPHandler, create_mcp_server, get_mcp_session_stats


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
    stats = get_mcp_session_stats(mcp)
    assert stats.active_sessions == 0
    assert stats.max_active_sessions is None
    assert stats.session_idle_timeout == 1800.0


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


def test_stateful_opt_in_max_active_sessions() -> None:
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        max_active_sessions=2,
    )
    assert mcp._session_manager.max_active_sessions == 2
    assert get_mcp_session_stats(mcp).max_active_sessions == 2


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_invalid_max_active_sessions_rejected_at_boundary(value: Any) -> None:
    with pytest.raises(ValueError, match="max_active_sessions must be a positive integer"):
        create_mcp_server(
            _BareHandler(),
            name="t",
            advertise_all=True,
            stateless_http=False,
            max_active_sessions=value,
        )


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
        max_active_sessions=1,
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
async def test_stateful_max_active_sessions_rejects_new_sessions() -> None:
    """One-shot clients that repeatedly initialize without closing can
    be bounded with ``max_active_sessions`` while existing sessions keep
    working."""
    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        stateless_http=False,
        max_active_sessions=1,
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
            assert session_id

            stats = get_mcp_session_stats(mcp)
            assert stats.active_sessions == 1
            assert stats.total_sessions_created == 1
            assert stats.sessions_created_last_60s == 1
            assert len(stats.session_age_seconds) == 1

            second_init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t2", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert second_init.status_code == 429
            assert "Too many active MCP sessions" in second_init.text

            list_resp = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={**headers, "mcp-session-id": session_id},
            )
            assert list_resp.status_code == 200, list_resp.text

            delete_resp = await client.delete(
                "/mcp/",
                headers={**headers, "mcp-session-id": session_id},
            )
            assert delete_resp.status_code == 200, delete_resp.text
            assert get_mcp_session_stats(mcp).active_sessions == 0

            after_delete = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t3", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert after_delete.status_code == 200, after_delete.text


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
async def test_stateful_session_is_bound_to_authenticated_principal() -> None:
    """A second valid bearer principal cannot attach to another session."""
    from adcp.server import (
        BearerTokenAuthMiddleware,
        Principal,
        create_mcp_server,
        validator_from_token_map,
    )

    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=validator_from_token_map(
            {
                "token-alice": Principal(caller_identity="alice", tenant_id="tenant-a"),
                "token-bob": Principal(caller_identity="bob", tenant_id="tenant-b"),
            }
        ),
    )
    base_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
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
                        "clientInfo": {"name": "alice", "version": "1"},
                    },
                },
                headers={**base_headers, "authorization": "Bearer token-alice"},
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]

            hijack = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    **base_headers,
                    "authorization": "Bearer token-bob",
                    "mcp-session-id": session_id,
                },
            )
            assert hijack.status_code == 404
            assert "Session not found" in hijack.text

            owner = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    **base_headers,
                    "authorization": "Bearer token-alice",
                    "mcp-session-id": session_id,
                },
            )
            assert owner.status_code == 200, owner.text

            anonymous = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                headers={**base_headers, "mcp-session-id": session_id},
            )
            assert anonymous.status_code == 404
            assert "Session not found" in anonymous.text


@pytest.mark.asyncio
async def test_pre_auth_session_allows_discovery_then_first_authenticated_caller_claims() -> None:
    """Pre-auth initialize is unbound until the first valid principal arrives."""
    from adcp.server import (
        BearerTokenAuthMiddleware,
        Principal,
        create_mcp_server,
        validator_from_token_map,
    )

    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=validator_from_token_map(
            {
                "token-alice": Principal(caller_identity="alice", tenant_id="tenant-a"),
                "token-bob": Principal(caller_identity="bob", tenant_id="tenant-b"),
            }
        ),
    )
    base_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
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
                        "clientInfo": {"name": "pre-auth", "version": "1"},
                    },
                },
                headers=base_headers,
            )
            assert init.status_code == 200, init.text
            session_id = init.headers["mcp-session-id"]

            discovery = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={**base_headers, "mcp-session-id": session_id},
            )
            assert discovery.status_code == 200, discovery.text

            claim = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    **base_headers,
                    "authorization": "Bearer token-alice",
                    "mcp-session-id": session_id,
                },
            )
            assert claim.status_code == 200, claim.text

            for request_headers in (
                {**base_headers, "mcp-session-id": session_id},
                {
                    **base_headers,
                    "authorization": "Bearer token-bob",
                    "mcp-session-id": session_id,
                },
            ):
                rejected = await client.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/list",
                        "params": {},
                    },
                    headers=request_headers,
                )
                assert rejected.status_code == 404
                assert "Session not found" in rejected.text


@pytest.mark.asyncio
async def test_pre_auth_session_rejects_anonymous_non_discovery_reuse() -> None:
    """Network-trust bypass cannot turn an unbound session into anonymous access."""
    from adcp.server import BearerTokenAuthMiddleware, create_mcp_server

    mcp = create_mcp_server(
        _BareHandler(),
        name="t",
        advertise_all=True,
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerTokenAuthMiddleware,
        validate_token=lambda _token: None,
        allow_unauthenticated=True,
    )
    base_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
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
                        "clientInfo": {"name": "pre-auth", "version": "1"},
                    },
                },
                headers=base_headers,
            )
            session_id = init.headers["mcp-session-id"]

            rejected = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={**base_headers, "mcp-session-id": session_id},
            )
            assert rejected.status_code == 404
            assert "Session not found" in rejected.text


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
            assert get_mcp_session_stats(mcp).active_sessions == 0

            init = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
            assert init.status_code == 200, init.text
