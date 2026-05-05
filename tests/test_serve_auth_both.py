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
    async def test_valid_token_sets_current_principal_contextvar(self):
        """On auth success, current_principal must be populated inside the
        inner app and reset to None after __call__ returns (#590 regression)."""
        from adcp.server.auth import current_principal, current_tenant

        captured: dict[str, str | None] = {}

        async def inner(scope: Any, _receive: Any, _send: Any) -> None:
            captured["principal"] = current_principal.get()
            captured["tenant"] = current_tenant.get()

        mw = A2ABearerAuthMiddleware(inner, _auth())
        scope = self._scope(headers=[(b"authorization", b"Bearer good-token")])
        await mw(scope, lambda: None, lambda _: None)

        assert captured["principal"] == "p-acme"
        assert captured["tenant"] == "acme"
        # Verify reset-in-finally: contextvar must be cleared after __call__ returns.
        assert current_principal.get() is None
        assert current_tenant.get() is None

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
        # RFC 6750 default body shape — error code is ``invalid_token``.
        assert b"invalid_token" in sent[1]["body"]

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
    body = response.json()
    assert body["error"] == "invalid_token"


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


@pytest.mark.asyncio
async def test_a2a_auth_populates_current_principal_contextvar() -> None:
    """A2ABearerAuthMiddleware must set current_principal contextvar so
    auth_context_factory and adopter code reading it directly see the
    authenticated identity on A2A — same as MCP (regression for #590).

    Verifies both that the var is populated inside the handler AND that it is
    reset to None after the request completes (try/finally contract)."""
    from adcp.server.a2a_server import create_a2a_server
    from adcp.server.auth import current_principal, current_tenant

    observed: dict[str, str | None] = {}

    class _ContextCaptureHandler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
            return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            observed["principal"] = current_principal.get()
            observed["tenant"] = current_tenant.get()
            return {"products": []}

    inner = create_a2a_server(_ContextCaptureHandler(), name="ctx-test", validation=None)
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
    assert observed.get("principal") == "p-acme", "current_principal not set on A2A path"
    assert observed.get("tenant") == "acme", "current_tenant not set on A2A path"
    # Verify reset-in-finally: contextvar must be None after the request.
    assert current_principal.get() is None
    assert current_tenant.get() is None


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


# ===========================================================================
# Structural drift guard: a2a-sdk well-known route renames break loud
# ===========================================================================


def test_discovery_paths_match_a2a_sdk_routes() -> None:
    """Catch silent drift between :data:`_A2A_DISCOVERY_PATHS` and
    a2a-sdk's actual agent-card routes. If a future a2a-sdk release
    renames ``/.well-known/agent-card.json`` (or removes the v0.3
    alias), the frozenset would leave the renamed route
    unauthenticated until someone noticed. This test fails first.

    Walks ``create_agent_card_routes`` against a real ``AgentCard``
    and asserts every registered path is in the frozenset.
    """
    from a2a.server.routes import create_agent_card_routes

    from adcp.server.a2a_server import _build_agent_card
    from adcp.server.auth import _A2A_DISCOVERY_PATHS

    handler = _OkHandler()
    agent_card = _build_agent_card(
        handler,
        name="drift-guard",
        port=0,
        description=None,
        version="1.0.0",
        extra_skills=None,
        advertise_all=False,
        push_notifications_supported=False,
    )
    routes = create_agent_card_routes(agent_card=agent_card)

    registered_paths = [r.path for r in routes]
    assert registered_paths, "a2a-sdk returned no agent-card routes"

    missing = [p for p in registered_paths if p not in _A2A_DISCOVERY_PATHS]
    assert not missing, (
        f"a2a-sdk registers agent-card route(s) {missing!r} that are NOT in "
        f"_A2A_DISCOVERY_PATHS={_A2A_DISCOVERY_PATHS!r}. Update the frozenset "
        f"in adcp.server.auth to include the new path(s) — otherwise A2A "
        f"discovery silently 401s on the renamed/added route."
    )


def test_a2a_agent_card_constant_referenced_directly() -> None:
    """The 1.0 path uses ``a2a.utils.constants.AGENT_CARD_WELL_KNOWN_PATH``
    rather than a string literal. If a2a-sdk changes the constant,
    our frozenset rebases without code changes. This test pins the
    indirection so a future maintainer doesn't accidentally inline
    the string."""
    from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

    from adcp.server.auth import _A2A_DISCOVERY_PATHS

    assert AGENT_CARD_WELL_KNOWN_PATH in _A2A_DISCOVERY_PATHS


# ===========================================================================
# RFC 6750 / RFC 7235 compliance: 401 must carry WWW-Authenticate
# ===========================================================================


@pytest.mark.asyncio
async def test_401_includes_www_authenticate_header() -> None:
    """RFC 7235 §3.1 + RFC 6750 §3 mandate ``WWW-Authenticate: Bearer``
    on 401 responses. Without it RFC-compliant clients (including
    browsers) won't surface the auth challenge to the user."""
    from adcp.server.a2a_server import create_a2a_server

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, _auth())
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}}
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/", json=body)
    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.lower().startswith("bearer")
    assert "realm" in challenge.lower()


@pytest.mark.asyncio
async def test_401_body_uses_rfc6750_error_codes() -> None:
    """RFC 6750 §3.1 defines ``invalid_token`` / ``invalid_request`` /
    ``insufficient_scope``. Default body uses ``invalid_token`` so
    OAuth-aware tooling parses it correctly."""
    from adcp.server.a2a_server import create_a2a_server

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, _auth())
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/", json={"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}}
            )
    assert response.status_code == 401
    body = response.json()
    assert body.get("error") == "invalid_token"


# ===========================================================================
# CORS preflight: OPTIONS must bypass auth
# ===========================================================================


@pytest.mark.asyncio
async def test_options_preflight_bypasses_auth() -> None:
    """Browser-origin clients send ``OPTIONS`` before any authenticated
    POST. Returning 401 on the preflight breaks CORS — the buyer
    never gets a chance to retry with a token. The middleware must
    pass OPTIONS through unauthenticated."""
    inner_calls: list[dict] = []

    async def inner(scope: Any, _receive: Any, _send: Any) -> None:
        inner_calls.append(scope)

    mw = A2ABearerAuthMiddleware(inner, _auth())
    scope = {
        "type": "http",
        "method": "OPTIONS",
        "path": "/",
        "headers": [],
    }
    await mw(scope, lambda: None, lambda _: None)
    assert len(inner_calls) == 1  # Inner reached.
    assert "user" not in inner_calls[0]  # No principal injected on preflight.


# ===========================================================================
# Async-validator rejection at boot, not at request time
# ===========================================================================


def test_async_validator_rejected_at_serve_boot_time() -> None:
    """Async validators on A2A fail at config time so production
    deployments don't ship with silently-failing auth that only
    surfaces on first traffic. MCP middleware awaits async
    validators transparently; A2A's middleware path is sync."""
    from adcp.server.serve import _wrap_a2a_with_auth

    async def async_validator(_token: str) -> Principal | None:
        return Principal(caller_identity="p1")

    cfg = BearerTokenAuth(validate_token=async_validator)
    with pytest.raises(TypeError, match="async"):
        _wrap_a2a_with_auth(MagicMock(), cfg)


def test_sync_lambda_validator_passes_boot_check() -> None:
    """Sync lambda / function validators are accepted unchanged."""
    from adcp.server.serve import _wrap_a2a_with_auth

    cfg = BearerTokenAuth(validate_token=lambda t: None)
    # No exception — the wrap returns an A2ABearerAuthMiddleware instance.
    wrapped = _wrap_a2a_with_auth(MagicMock(), cfg)
    assert isinstance(wrapped, A2ABearerAuthMiddleware)


# ===========================================================================
# Validator-exception suppression survives the full ASGI stack
# ===========================================================================


@pytest.mark.asyncio
async def test_validator_exception_returns_401_through_full_stack() -> None:
    """The unit-level test asserts the middleware short-circuits with
    401 when the validator raises. This test asserts the same shape
    survives the full Starlette / a2a-sdk stack — i.e., the 500
    suppression isn't an artifact of the unit harness."""
    from adcp.server.a2a_server import create_a2a_server

    def boom(_token: str) -> Principal | None:
        raise RuntimeError("token store down")

    inner = create_a2a_server(_OkHandler(), name="test-agent", validation=None)
    app = A2ABearerAuthMiddleware(inner, BearerTokenAuth(validate_token=boom))
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/",
                json={"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {}},
                headers={"Authorization": "Bearer x"},
            )
    assert response.status_code == 401  # Not 500.
