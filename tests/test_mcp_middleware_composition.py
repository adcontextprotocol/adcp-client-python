"""Integration test: custom HTTP middleware composes with SDK-registered tools.

Downstream agents (salesagent, creative agents) need to wire their own
auth middleware around tools registered by ``create_mcp_server()``. This
test proves the composition path works end-to-end:

1. ``mcp.streamable_http_app()`` returns a Starlette app that accepts
   ``.add_middleware()``.
2. The middleware fires before tool dispatch and can reject requests
   (401 Unauthorized) or let them through.
3. When the middleware lets the request through, a ``context_factory``
   passed to ``create_mcp_server()`` builds a :class:`ToolContext` the
   handler receives — populated from the middleware's side-channel
   (``contextvars.ContextVar``).
4. Tools in :data:`adcp.server.DISCOVERY_TOOLS` are callable without
   auth (the spec-mandated handshake path).
5. JSON-RPC methods in :data:`adcp.server.DISCOVERY_METHODS`
   (``initialize``, ``notifications/initialized``, ``tools/list``) are
   callable pre-auth — MCP treats handshake + inventory as discovery.

If any of this regresses, salesagent and every other downstream has to
keep their wrapper layer (``mcp_context_wrapper.py``, custom
``@mcp.tool()`` scaffolding) forever. Failing here is the signal to fix
the integration, not the test.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from adcp.server import (
    DISCOVERY_METHODS,
    DISCOVERY_TOOLS,
    ADCPHandler,
    RequestMetadata,
    ToolContext,
    create_mcp_server,
)

_current_principal: ContextVar[str | None] = ContextVar("test_current_principal", default=None)
_current_tenant: ContextVar[str | None] = ContextVar("test_current_tenant", default=None)

# Per-request state attribute names. With the default stateful
# streamable-http transport, the dispatch task is a different async
# task than the middleware's, so ``ContextVar`` propagation breaks.
# ``request.state`` survives the boundary because the dispatch reads
# the originating Starlette ``Request`` from
# ``mcp.server.lowlevel.server.request_ctx`` and we plumb it through
# ``RequestMetadata.request_context``.
_REQUEST_STATE_PRINCIPAL = "test_principal"
_REQUEST_STATE_TENANT = "test_tenant"


class _RecordingHandler(ADCPHandler):
    """Handler that records the ToolContext each call received."""

    def __init__(self) -> None:
        self.calls: list[ToolContext | None] = []

    async def get_adcp_capabilities(
        self, params: Any, context: ToolContext | None = None
    ) -> dict[str, Any]:
        self.calls.append(context)
        return {"adcp": {"major_versions": [3]}}

    async def get_products(self, params: Any, context: ToolContext | None = None) -> dict[str, Any]:
        self.calls.append(context)
        return {"products": []}


class _AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that validates Authorization headers.

    Lets the MCP discovery layer through (``DISCOVERY_METHODS`` +
    ``tools/call`` → ``DISCOVERY_TOOLS``) without a token; rejects
    anything else lacking a valid token. On a valid token, stashes
    principal + tenant in ContextVars so the handler-side
    ``context_factory`` can read them.
    """

    VALID_TOKENS: dict[str, tuple[str, str]] = {
        "token-acme": ("principal-acme-1", "tenant-acme"),
        "token-beta": ("principal-beta-9", "tenant-beta"),
    }

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        method, tool_name = await _peek_jsonrpc(request)
        is_discovery = method in DISCOVERY_METHODS or (
            method == "tools/call" and tool_name in DISCOVERY_TOOLS
        )

        if not is_discovery:
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if token not in self.VALID_TOKENS:
                return JSONResponse({"error": "unauthenticated"}, status_code=401)
            principal, tenant = self.VALID_TOKENS[token]
        else:
            principal = None
            tenant = None

        # Write to BOTH ``request.state`` (survives the stateful
        # streamable-http session-task boundary) and the legacy
        # ContextVars (read by adopters who haven't migrated). The
        # dispatch-side ``_build_context`` prefers ``request.state``.
        setattr(request.state, _REQUEST_STATE_PRINCIPAL, principal)
        setattr(request.state, _REQUEST_STATE_TENANT, tenant)
        _principal_token = _current_principal.set(principal)
        _tenant_token = _current_tenant.set(tenant)

        try:
            return await call_next(request)
        finally:
            _current_principal.reset(_principal_token)
            _current_tenant.reset(_tenant_token)


async def _peek_jsonrpc(request: Request) -> tuple[str | None, str | None]:
    """Extract ``(method, tool_name)`` from the incoming JSON-RPC body
    without consuming it for downstream handlers. ``tool_name`` is set
    only for ``tools/call``."""
    # Starlette caches ``request._body`` on first read, so subsequent
    # reads inside the app still see the bytes.
    body = await request.body()
    if not body:
        return None, None
    try:
        import json

        payload = json.loads(body)
    except ValueError:
        return None, None
    # JSON-RPC 2.0 batch arrays fall through to auth (fail closed).
    if not isinstance(payload, dict):
        return None, None
    method = payload.get("method")
    method = method if isinstance(method, str) else None
    if method != "tools/call":
        return method, None
    params = payload.get("params") or {}
    name = params.get("name")
    return method, (name if isinstance(name, str) else None)


def _build_context(meta: RequestMetadata) -> ToolContext:
    """Read auth state off the request the SDK threaded into
    ``meta.request_context``. This is the pattern adopters should use
    in stateful streamable-http mode (the default). The
    :mod:`contextvars`-based pattern only works when stateless mode is
    explicitly opted in."""
    principal = None
    tenant = None
    if meta.request_context is not None:
        principal = getattr(meta.request_context.state, _REQUEST_STATE_PRINCIPAL, None)
        tenant = getattr(meta.request_context.state, _REQUEST_STATE_TENANT, None)
    return ToolContext(
        request_id=meta.request_id,
        caller_identity=principal,
        tenant_id=tenant,
        metadata={"tool_name": meta.tool_name, "transport": meta.transport},
    )


@pytest.fixture
async def handler_and_client() -> Any:
    handler = _RecordingHandler()
    mcp = create_mcp_server(
        handler,
        name="test-agent",
        context_factory=_build_context,
        # Tests assert middleware composition / context-factory plumbing
        # against a stub handler that returns minimal payloads — opt out
        # of the framework's strict-by-default wire-conformance check
        # so a non-spec-conformant stub response doesn't get rewritten
        # into a VALIDATION_ERROR before the assertion runs.
        validation=None,
        # Allow in-process test host — MCP's DNS-rebinding protection
        # rejects unknown Host headers by default when enabled.
        allowed_hosts=["localhost", "127.0.0.1"],
    )
    app = mcp.streamable_http_app()
    app.add_middleware(_AuthMiddleware)

    # FastMCP's streamable HTTP session manager initializes a TaskGroup
    # via the Starlette app lifespan. httpx.ASGITransport does not run
    # lifespan by default — asgi-lifespan handles startup/shutdown and
    # surfaces exceptions raised during startup so test failures report
    # the real error instead of hanging.
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            yield handler, client


@pytest.mark.asyncio
async def test_discovery_tool_is_callable_without_auth(handler_and_client: Any) -> None:
    handler, client = handler_and_client

    await _initialize_session(client)
    response = await _call_tool(client, "get_adcp_capabilities", {})

    assert response.status_code == 200, response.text
    payload = _parse_event_stream(response.text)
    assert "result" in payload, payload
    assert handler.calls, "handler was not invoked"
    call_context = handler.calls[-1]
    # Discovery calls have no authenticated principal — that's the whole point.
    assert call_context is not None
    assert call_context.caller_identity is None
    assert call_context.tenant_id is None


@pytest.mark.asyncio
async def test_authenticated_tool_call_populates_caller_identity(
    handler_and_client: Any,
) -> None:
    handler, client = handler_and_client

    await _initialize_session(client, headers={"Authorization": "Bearer token-acme"})
    response = await _call_tool(
        client,
        "get_products",
        {"brief": "coffee"},
        headers={"Authorization": "Bearer token-acme"},
    )

    assert response.status_code == 200, response.text
    call_context = handler.calls[-1]
    assert call_context is not None
    assert call_context.caller_identity == "principal-acme-1"
    assert call_context.tenant_id == "tenant-acme"


@pytest.mark.asyncio
async def test_missing_token_blocks_non_discovery_tool(handler_and_client: Any) -> None:
    handler, client = handler_and_client

    response = await _call_tool(client, "get_products", {"brief": "coffee"})

    assert response.status_code == 401
    assert not handler.calls, (
        "handler was invoked despite missing auth — middleware did NOT "
        "compose with the tool dispatch"
    )


@pytest.mark.asyncio
async def test_initialize_is_callable_without_auth(handler_and_client: Any) -> None:
    """``initialize`` is pre-auth per MCP spec. Pins the contract so a
    future tightening of the gate breaks here, not in every fixture."""
    _, client = handler_and_client

    response = await _initialize_session(client)

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_tools_list_is_callable_without_auth(handler_and_client: Any) -> None:
    """``tools/list`` is pre-auth per MCP spec (discovery handshake).

    An unauthenticated client gets the tool inventory. Operators who
    consider the inventory sensitive can strip ``tools/list`` from
    ``DISCOVERY_METHODS`` in their own middleware — this test locks in
    the default posture.
    """
    _, client = handler_and_client

    await _initialize_session(client)
    response = await _list_tools(client)

    assert response.status_code == 200, response.text
    payload = _parse_event_stream(response.text)
    assert "result" in payload, payload
    tools = payload["result"].get("tools", [])
    # Handler only overrides two tools but the base class advertises
    # the full AdCP surface — just assert the list was returned.
    assert isinstance(tools, list)
    assert tools, "tools/list returned an empty inventory"


@pytest.mark.asyncio
async def test_tools_list_bypasses_gate_even_with_invalid_token(
    handler_and_client: Any,
) -> None:
    """Negative control: an invalid ``Authorization`` header must NOT
    cause the gate to reject ``tools/list``. Proves the gate is
    consulting :data:`DISCOVERY_METHODS` rather than missing-header
    being coincidentally treated as 'no auth attempt'."""
    _, client = handler_and_client

    await _initialize_session(client)
    response = await _list_tools(client, headers={"Authorization": "Bearer not-valid"})

    assert response.status_code == 200, response.text


def test_discovery_tools_frozenset_contract() -> None:
    # Protects against accidental widening/narrowing of the spec-mandated
    # auth-optional set. Callers extend via ``DISCOVERY_TOOLS | {...}``.
    assert DISCOVERY_TOOLS == frozenset({"get_adcp_capabilities"})


def test_discovery_methods_frozenset_contract() -> None:
    # The MCP discovery layer is ``initialize`` (session handshake),
    # ``notifications/initialized`` (handshake-completion notification),
    # and ``tools/list`` (inventory). Widening this set silently lets
    # mutations through the auth gate; narrowing breaks clients that
    # expect pre-auth discovery.
    assert DISCOVERY_METHODS == frozenset({"initialize", "notifications/initialized", "tools/list"})


def test_validate_discovery_set_accepts_base_set() -> None:
    from adcp.server import validate_discovery_set

    # The base DISCOVERY_TOOLS set must always validate — any regression
    # here means we added a mutation tool to the spec-mandated handshake.
    validate_discovery_set(DISCOVERY_TOOLS)


def test_validate_discovery_set_accepts_read_only_extension() -> None:
    from adcp.server import validate_discovery_set

    # list_creative_formats is annotated read-only — downstream that
    # wants to make format listing public should be allowed to.
    validate_discovery_set(DISCOVERY_TOOLS | {"list_creative_formats"})


def test_validate_discovery_set_rejects_mutation_tool() -> None:
    from adcp.server import validate_discovery_set

    with pytest.raises(ValueError, match="non-read-only"):
        validate_discovery_set(DISCOVERY_TOOLS | {"create_media_buy"})


def test_validate_discovery_set_rejects_unknown_tool() -> None:
    from adcp.server import validate_discovery_set

    with pytest.raises(ValueError, match="unknown tool"):
        validate_discovery_set(DISCOVERY_TOOLS | {"not_a_real_tool"})


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _initialize_session(
    client: httpx.AsyncClient, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Send an MCP ``initialize`` JSON-RPC call. Required before any
    ``tools/call`` — and in stateful streamable-http, the response's
    ``Mcp-Session-Id`` header must be echoed on every subsequent request
    targeting the same session."""
    request_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if headers:
        request_headers.update(headers)
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    response = await client.post("/mcp/", json=body, headers=request_headers)
    session_id = response.headers.get("mcp-session-id")
    if session_id is not None:
        client.headers["mcp-session-id"] = session_id
    return response


async def _call_tool(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a JSON-RPC ``tools/call`` to the MCP endpoint."""
    request_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if headers:
        request_headers.update(headers)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    return await client.post("/mcp/", json=body, headers=request_headers)


async def _list_tools(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a JSON-RPC ``tools/list`` to the MCP endpoint."""
    request_headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if headers:
        request_headers.update(headers)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    return await client.post("/mcp/", json=body, headers=request_headers)


def _parse_event_stream(body: str) -> dict[str, Any]:
    """Parse SSE event-stream body from FastMCP into a dict."""
    import json

    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    return json.loads(body) if body.strip() else {}


# ----------------------------------------------------------------------
# MCP middleware parity with A2A — ``create_mcp_server(middleware=[...])``
# ----------------------------------------------------------------------


@pytest.fixture
async def middleware_events() -> list[str]:
    return []


@pytest.fixture
async def middleware_handler_and_client(middleware_events: list[str]) -> Any:
    """Fixture that wires a SkillMiddleware chain onto the MCP server.
    Mirrors ``handler_and_client`` above but without the HTTP auth
    layer so the middleware chain is the only thing under test."""
    handler = _RecordingHandler()

    async def outer(skill_name, params, context, call_next):
        middleware_events.append(f"outer-pre:{skill_name}")
        result = await call_next()
        middleware_events.append(f"outer-post:{skill_name}")
        return result

    async def inner(skill_name, params, context, call_next):
        middleware_events.append(f"inner-pre:{skill_name}")
        result = await call_next()
        middleware_events.append(f"inner-post:{skill_name}")
        return result

    mcp = create_mcp_server(
        handler,
        name="mw-test",
        context_factory=_build_context,
        middleware=[outer, inner],
        validation=None,  # transport plumbing test, not wire-conformance
    )
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security.allowed_hosts = ["localhost", "127.0.0.1"]
    app = mcp.streamable_http_app()

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            yield handler, client


@pytest.mark.asyncio
async def test_mcp_middleware_composes_outermost_first(
    middleware_handler_and_client: Any,
    middleware_events: list[str],
) -> None:
    """MCP ``middleware=[outer, inner]`` matches A2A semantics: outer
    pre-event comes first, then inner pre-event, then handler, then
    inner post, then outer post. Stale ordering or reversed composition
    would regress cross-transport parity."""
    _, client = middleware_handler_and_client

    await _initialize_session(client)
    resp = await _call_tool(client, "get_adcp_capabilities", {})

    assert resp.status_code == 200, resp.text
    assert middleware_events == [
        "outer-pre:get_adcp_capabilities",
        "inner-pre:get_adcp_capabilities",
        "inner-post:get_adcp_capabilities",
        "outer-post:get_adcp_capabilities",
    ], middleware_events


@pytest.mark.asyncio
async def test_mcp_middleware_can_short_circuit() -> None:
    """Middleware that returns without calling ``call_next()`` MUST
    stop the chain — handler doesn't run. Rate limiters use this."""

    handler_calls: list[str] = []

    class _ShortCircuitTarget(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            handler_calls.append("called")
            return {"adcp": {"major_versions": [3]}}

    async def rate_limiter(skill_name, params, context, call_next):
        return {"error": "rate-limited", "skill": skill_name}

    mcp = create_mcp_server(
        _ShortCircuitTarget(),
        name="sc-test",
        middleware=[rate_limiter],
    )
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security.allowed_hosts = ["localhost", "127.0.0.1"]
    app = mcp.streamable_http_app()

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            await _initialize_session(client)
            resp = await _call_tool(client, "get_adcp_capabilities", {})

    assert resp.status_code == 200, resp.text
    assert handler_calls == [], (
        "middleware short-circuited but the handler still ran — MCP middleware "
        "chain did not honour the 'skip call_next to skip handler' contract"
    )


@pytest.mark.asyncio
async def test_mcp_middleware_sees_tool_context() -> None:
    """Middleware gets the same ToolContext the handler will receive.
    When no context_factory is configured, middleware sees a default
    ToolContext (not None) so the typed signature holds."""

    seen: list[ToolContext] = []

    async def record_context(skill_name, params, context, call_next):
        seen.append(context)
        return await call_next()

    class _Handler(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    mcp = create_mcp_server(
        _Handler(),
        name="ctx-test",
        middleware=[record_context],
    )
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.settings.transport_security.allowed_hosts = ["localhost", "127.0.0.1"]
    app = mcp.streamable_http_app()

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            follow_redirects=True,
        ) as client:
            await _initialize_session(client)
            await _call_tool(client, "get_adcp_capabilities", {})

    assert len(seen) == 1
    # No context_factory configured → middleware receives a synthesised
    # default ToolContext so the signature type holds. Verified
    # explicitly so a future change that passes None instead breaks here.
    assert isinstance(seen[0], ToolContext)
