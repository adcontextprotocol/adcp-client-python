"""Cross-transport auth coverage for ``serve(auth=BearerTokenAuth(...))``.

Closes the regression filed in #558: bearer-token auth applied via the
existing ``BearerTokenAuthMiddleware`` to ``serve(transport="both")``
left the A2A leg unauthenticated. The fix wires ``auth=`` into both
the MCP middleware and an A2A
:class:`~adcp.server.auth.A2ABearerAuthMiddleware`, with the agent
card publicly accessible per A2A spec §4.1.

Three layers of coverage:

1. **Unit** — :class:`A2ABearerAuthMiddleware` accepts/rejects via
   the same shapes as :class:`BearerTokenAuthMiddleware`.
2. **A2A through ASGI** — full route-level test with
   ``httpx.AsyncClient`` against the a2a-sdk-built Starlette app
   wrapped in our auth middleware.
3. **transport="both"** — the regression case: hit MCP and A2A on
   the same binary; both legs require auth, agent-card and MCP
   discovery are exempt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server import ADCPHandler
from adcp.server.auth import (
    A2ABearerAuthMiddleware,
    BearerTokenAuth,
    Principal,
    validator_from_token_map,
)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class _OkHandler(ADCPHandler):
    """Minimal handler returning structured success on get_products."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": [{"id": "p1", "name": "Display"}], "sandbox": True}


def _auth() -> BearerTokenAuth:
    return BearerTokenAuth(
        validate_token=validator_from_token_map(
            {"good-token": Principal(caller_identity="p-acme", tenant_id="acme")}
        )
    )


# ===========================================================================
# Unit: A2ABearerAuthMiddleware against raw ASGI scope
# ===========================================================================


class TestA2ABearerAuthMiddlewareUnit:
    """Middleware logic verified against raw ASGI scope dicts."""

    def _scope(self, path: str = "/", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
        return {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": list(headers or []),
        }

    @pytest.mark.asyncio
    async def test_valid_token_passes_through_and_populates_scope_user(self):
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        scope = self._scope(headers=[(b"authorization", b"Bearer good-token")])
        await mw(scope, lambda: None, lambda _: None)

        assert len(inner_calls) == 1
        passed_scope = inner_calls[0]
        assert "user" in passed_scope
        assert passed_scope["user"].is_authenticated is True
        assert passed_scope["user"].display_name == "p-acme"
        assert "auth" in passed_scope
        assert passed_scope["auth"].caller_identity == "p-acme"

    @pytest.mark.asyncio
    async def test_missing_header_returns_401(self):
        sent: list[dict] = []
        inner_called = False

        async def inner(_scope: Any, _receive: Any, _send: Any) -> None:
            nonlocal inner_called
            inner_called = True

        async def send(msg: dict) -> None:
            sent.append(msg)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        await mw(self._scope(), lambda: None, send)

        assert not inner_called  # Auth failure short-circuits.
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 401
        assert b"unauthenticated" in sent[1]["body"]

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        sent: list[dict] = []

        async def inner(_scope: Any, _receive: Any, _send: Any) -> None: ...

        async def send(msg: dict) -> None:
            sent.append(msg)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        await mw(
            self._scope(headers=[(b"authorization", b"Bearer bad")]),
            lambda: None,
            send,
        )
        assert sent[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_validator_exception_projects_to_401_not_500(self):
        """A buggy validator must not leak 500 stacks. We log + 401."""

        def boom(_token: str) -> Principal | None:
            raise RuntimeError("token store down")

        sent: list[dict] = []

        async def inner(_scope: Any, _receive: Any, _send: Any) -> None: ...

        async def send(msg: dict) -> None:
            sent.append(msg)

        mw = A2ABearerAuthMiddleware(inner, BearerTokenAuth(validate_token=boom))
        await mw(
            self._scope(headers=[(b"authorization", b"Bearer x")]),
            lambda: None,
            send,
        )
        assert sent[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_agent_card_path_publicly_accessible(self):
        """A2A spec §4.1 — ``/.well-known/agent-card.json`` MUST be
        public regardless of auth config."""
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        # No Authorization header — MUST still pass through.
        await mw(self._scope(path="/.well-known/agent-card.json"), lambda: None, lambda _: None)
        assert len(inner_calls) == 1
        assert "user" not in inner_calls[0]  # No principal injected on public route.

    @pytest.mark.asyncio
    async def test_legacy_agent_json_path_also_exempt(self):
        """``/.well-known/agent.json`` is the 0.3 alias retained by the
        compat shim — exempt for the same spec reason."""
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        await mw(self._scope(path="/.well-known/agent.json"), lambda: None, lambda _: None)
        assert len(inner_calls) == 1

    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through(self):
        """Lifespan events bypass auth entirely — they're not HTTP."""
        inner_calls: list[Any] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, _auth())
        await mw({"type": "lifespan"}, lambda: None, lambda _: None)
        assert len(inner_calls) == 1

    @pytest.mark.asyncio
    async def test_custom_header_name(self):
        cfg = BearerTokenAuth(
            validate_token=validator_from_token_map({"raw-key": Principal(caller_identity="p1")}),
            header_name="x-adcp-auth",
            bearer_prefix_required=False,
        )
        inner_calls: list[dict] = []

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            inner_calls.append(scope)

        mw = A2ABearerAuthMiddleware(inner, cfg)
        await mw(
            self._scope(headers=[(b"x-adcp-auth", b"raw-key")]),
            lambda: None,
            lambda _: None,
        )
        assert inner_calls[0]["user"].display_name == "p1"


# ===========================================================================
# A2A through ASGI: full Starlette stack
# ===========================================================================


@pytest.mark.asyncio
async def test_a2a_agent_card_publicly_accessible_with_auth() -> None:
    """End-to-end: ``/.well-known/agent-card.json`` MUST be public
    even when auth is configured. Path-based exemption inside
    :class:`A2ABearerAuthMiddleware` lets the request through to the
    a2a-sdk's agent-card route."""
    from adcp.server.a2a_server import create_a2a_server

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, _auth())
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    body = response.json()
    assert "name" in body  # Full agent card came back, not a 401 body.


@pytest.mark.asyncio
async def test_a2a_jsonrpc_unauthenticated_returns_http_401() -> None:
    """No Authorization header on a JSON-RPC request → middleware
    short-circuits with HTTP 401. Critical for spec compliance:
    earlier designs that raised HTTPException from inside the
    a2a-sdk dispatcher were swallowed by the v0.3 compat adapter
    and projected to HTTP 200 with a JSON-RPC error body."""
    from adcp.server.a2a_server import create_a2a_server

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, _auth())
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        },
    }
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/", json=body)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthenticated"}


@pytest.mark.asyncio
async def test_a2a_jsonrpc_authenticated_passes_through() -> None:
    """Valid bearer header → request reaches the handler."""
    from adcp.server.a2a_server import create_a2a_server

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, _auth())
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        },
    }
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/", json=body, headers={"Authorization": "Bearer good-token"}
            )
    assert response.status_code == 200


# ===========================================================================
# transport="both": the regression case from #558
# ===========================================================================


def _build_both_app(auth: Any | None = None) -> Any:
    """Build the unified MCP+A2A ASGI app via the same path
    ``serve(transport="both")`` uses, but without uvicorn so we can
    drive it through ``httpx.AsyncClient``."""
    from adcp.server.serve import _build_mcp_and_a2a_app

    return _build_mcp_and_a2a_app(
        _OkHandler(),
        name="test-agent",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        auth=auth,
    )


@pytest.mark.asyncio
async def test_both_transport_a2a_leg_requires_auth_when_configured() -> None:
    """The original bug: ``serve(transport="both", auth=...)`` was
    expected to gate both legs but didn't. This test asserts the A2A
    leg now rejects unauthenticated JSON-RPC under the unified
    binary."""
    app = _build_both_app(_auth())
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/", json=body)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_both_transport_a2a_leg_accepts_valid_token() -> None:
    """Auth is configured AND token is valid → A2A leg succeeds."""
    app = _build_both_app(_auth())
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/", json=body, headers={"Authorization": "Bearer good-token"}
            )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_both_transport_agent_card_publicly_accessible() -> None:
    """A2A discovery (``/.well-known/agent-card.json``) MUST be public
    even with ``auth=`` configured."""
    app = _build_both_app(_auth())
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_both_transport_mcp_leg_requires_auth_when_configured() -> None:
    """MCP leg under the unified binary still gates non-discovery
    requests on a bearer token. Discovery methods (``initialize`` /
    ``tools/list`` / ``get_adcp_capabilities``) bypass per
    :class:`BearerTokenAuthMiddleware`'s body-peek logic."""
    app = _build_both_app(_auth())
    # tools/call without a token → 401 from the MCP middleware.
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": "get_products", "arguments": {}},
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            response = await client.post(
                "/mcp",
                json=body,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
    assert response.status_code == 401
    assert "unauthenticated" in response.text


@pytest.mark.asyncio
async def test_both_transport_no_auth_runs_unauthenticated() -> None:
    """Without ``auth=``, both legs accept everything (preserves the
    pre-fix unauthenticated default — turning auth on is opt-in)."""
    app = _build_both_app(auth=None)
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": {}}}],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/", json=body)
    # No auth configured → A2A serves the request without checking.
    assert response.status_code == 200


# ===========================================================================
# Type-guard: serve(auth=...) rejects non-BearerTokenAuth
# ===========================================================================


def test_serve_auth_rejects_wrong_type_mcp() -> None:
    from adcp.server.serve import _wrap_mcp_with_auth

    with pytest.raises(TypeError, match="BearerTokenAuth"):
        _wrap_mcp_with_auth(MagicMock(), {"validate_token": lambda t: None})


def test_serve_auth_rejects_wrong_type_a2a() -> None:
    from adcp.server.serve import _wrap_a2a_with_auth

    with pytest.raises(TypeError, match="BearerTokenAuth"):
        _wrap_a2a_with_auth(MagicMock(), "not-a-config")


def test_serve_auth_none_is_noop() -> None:
    from adcp.server.serve import _wrap_a2a_with_auth, _wrap_mcp_with_auth

    sentinel = MagicMock()
    assert _wrap_mcp_with_auth(sentinel, None) is sentinel
    assert _wrap_a2a_with_auth(sentinel, None) is sentinel


def test_public_exports_include_new_symbols() -> None:
    import adcp.server as srv

    assert hasattr(srv, "BearerTokenAuth")
    assert hasattr(srv, "A2ABearerAuthMiddleware")
    assert "BearerTokenAuth" in srv.__all__
    assert "A2ABearerAuthMiddleware" in srv.__all__
