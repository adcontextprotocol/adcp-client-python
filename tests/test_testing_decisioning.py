"""Unit tests for ``adcp.testing.decisioning`` helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import httpx
import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account
from adcp.testing import build_asgi_app, build_test_client, make_request_context

# ---- make_request_context ----


def test_make_request_context_default_returns_test_account_id() -> None:
    """No-arg form yields a usable RequestContext with a stable
    ``test-account`` id — the documented default."""
    ctx = make_request_context()
    assert isinstance(ctx, RequestContext)
    assert ctx.account.id == "test-account"
    assert ctx.auth_info is None
    assert ctx.auth_principal is None


def test_make_request_context_account_string_shorthand() -> None:
    """Passing a string for ``account`` builds ``Account(id=<string>)``
    — common test case where adopters only need a stable id."""
    ctx = make_request_context(account="acme")
    assert ctx.account.id == "acme"


def test_make_request_context_account_instance_passes_through() -> None:
    """Full :class:`Account` instances pass through unchanged."""
    acct = Account(id="explicit", metadata={"region": "us"})
    ctx = make_request_context(account=acct)
    assert ctx.account is acct
    assert ctx.account.metadata == {"region": "us"}


def test_make_request_context_threads_optional_fields() -> None:
    """All optional fields land on the constructed context when
    explicitly passed."""
    fixed_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ctx = make_request_context(
        account="t",
        auth_principal="agent.example.com",
        request_id="req-123",
        tenant_id="tenant-a",
        caller_identity="caller-key",
        metadata={"trace_id": "abc"},
        now=fixed_now,
    )
    assert ctx.auth_principal == "agent.example.com"
    assert ctx.request_id == "req-123"
    assert ctx.tenant_id == "tenant-a"
    assert ctx.caller_identity == "caller-key"
    assert ctx.metadata == {"trace_id": "abc"}
    assert ctx.now == fixed_now


def test_make_request_context_state_resolve_default_to_framework_stubs() -> None:
    """Unset ``state`` and ``resolve`` use the framework's v6.0 default
    factory readers — the same shape adopter handlers see in
    production until v6.1 wires real backing stores."""
    ctx = make_request_context()
    # The framework defaults are non-None; we don't assert the type
    # (it's framework-internal) but we verify they're populated so
    # adopter calls into them don't raise AttributeError.
    assert ctx.state is not None
    assert ctx.resolve is not None


# ---- build_asgi_app ----


class _SalesPlatformWithMethods(DecisioningPlatform):
    """Minimal sales platform with the five SalesPlatform required
    methods stubbed — mirrors the shape adopter test fixtures take."""

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_billing=("operator",),
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "x", "status": "active"}

    def update_media_buy(self, mid, p, ctx):
        return {"media_buy_id": mid, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}

    def get_media_buys(self, req, ctx):
        return {"media_buys": []}

    def list_creative_formats_legacy(self, req, ctx):
        return {"creative_formats": []}

    def list_creatives(self, req, ctx):
        return {"creatives": []}

    def provide_performance_feedback(self, req, ctx):
        return {"acknowledged": True}


def test_build_asgi_app_returns_asgi_callable() -> None:
    """The returned object is a callable ASGI app — can be invoked
    directly or handed to a test client."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform)
    # ASGI apps are callables: app(scope, receive, send) is async.
    assert callable(app)


def test_build_asgi_app_uses_conformant_webhook_default() -> None:
    """The test helper matches the production default and needs no
    sync-completion webhook transport."""
    platform = _SalesPlatformWithMethods()
    # Should not require legacy sync-completion transport wiring.
    app = build_asgi_app(platform)
    assert app is not None


def test_build_asgi_app_accepts_name_kwarg() -> None:
    """Smoke: ``name=`` is a recognized kwarg and construction
    succeeds. The wiring of the name to the MCP server is
    framework-internal and verified by the underlying
    ``create_mcp_server`` suite."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, name="custom-test-agent")
    assert app is not None


def test_build_asgi_app_default_name_is_platform_class() -> None:
    """When ``name=`` is omitted, the platform class name is used —
    matches :func:`adcp.decisioning.serve` behavior."""
    platform = _SalesPlatformWithMethods()
    # Construction should not raise; name resolution is internal.
    app = build_asgi_app(platform)
    assert app is not None


def test_build_asgi_app_forwards_advertise_all() -> None:
    """``advertise_all=True`` reaches both factory layers without
    raising."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, advertise_all=True)
    assert app is not None


def test_build_asgi_app_rejects_invalid_platform() -> None:
    """Pass-through validation: a platform missing ``accounts`` fails
    via :func:`validate_platform` with a structured AdcpError, the
    same as production :func:`serve` would."""
    from adcp.decisioning.types import AdcpError

    class _BrokenPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            supported_billing=("operator",),
        )
        # accounts intentionally not set — validate_platform should reject

    with pytest.raises(AdcpError):
        build_asgi_app(_BrokenPlatform())


# ---- build_asgi_app: allowed_hosts ----


def test_build_asgi_app_forwards_allowed_hosts() -> None:
    """``allowed_hosts=`` reaches ``create_mcp_server`` — construction
    succeeds and the app is a callable."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, allowed_hosts=["test"])
    assert callable(app)


# ---- build_asgi_app: new serve-layer kwargs ----


def test_build_asgi_app_forwards_context_factory() -> None:
    """``context_factory=`` reaches ``create_mcp_server`` — construction
    succeeds with a custom factory."""
    from adcp.server.base import ToolContext
    from adcp.server.serve import RequestMetadata

    platform = _SalesPlatformWithMethods()

    def my_factory(meta: RequestMetadata) -> ToolContext:
        return ToolContext(caller_identity="test-caller")

    app = build_asgi_app(platform, context_factory=my_factory)
    assert callable(app)


def test_build_asgi_app_forwards_asgi_middleware() -> None:
    """``asgi_middleware=`` is applied outermost — construction succeeds."""
    from typing import Any

    platform = _SalesPlatformWithMethods()

    class _PassthroughMiddleware:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await self.app(scope, receive, send)

    app = build_asgi_app(platform, asgi_middleware=[(_PassthroughMiddleware, {})])
    assert callable(app)


def test_build_asgi_app_forwards_streaming_responses() -> None:
    """``streaming_responses=True`` reaches ``create_mcp_server`` without error."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, streaming_responses=True)
    assert callable(app)


def test_build_asgi_app_forwards_mcp_session_settings() -> None:
    """MCP session controls reach the same factory used by production."""
    from unittest.mock import patch

    from adcp.server.serve import create_mcp_server

    with patch("adcp.server.serve.create_mcp_server", wraps=create_mcp_server) as mocked:
        app = build_asgi_app(
            _SalesPlatformWithMethods(),
            stateless_http=True,
            session_idle_timeout=None,
            max_active_sessions=25,
        )

    assert callable(app)
    assert mocked.call_args.kwargs["stateless_http"] is True
    assert mocked.call_args.kwargs["session_idle_timeout"] is None
    assert mocked.call_args.kwargs["max_active_sessions"] == 25


def test_build_asgi_app_both_forwards_lifespan_and_session_settings() -> None:
    """The combined topology receives the full production-parity surface."""
    from unittest.mock import patch

    from adcp.server.serve import _build_mcp_and_a2a_app

    async def startup() -> None:
        pass

    async def shutdown() -> None:
        pass

    with patch(
        "adcp.server.serve._build_mcp_and_a2a_app",
        wraps=_build_mcp_and_a2a_app,
    ) as mocked:
        app = build_asgi_app(
            _SalesPlatformWithMethods(),
            transport="both",
            stateless_http=True,
            session_idle_timeout=None,
            max_active_sessions=25,
            on_startup=(startup,),
            on_shutdown=(shutdown,),
        )

    assert callable(app)
    assert mocked.call_args.kwargs["stateless_http"] is True
    assert mocked.call_args.kwargs["session_idle_timeout"] is None
    assert mocked.call_args.kwargs["max_active_sessions"] == 25
    assert mocked.call_args.kwargs["on_startup"] == (startup,)
    assert mocked.call_args.kwargs["on_shutdown"] == (shutdown,)


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
def test_build_asgi_app_rejects_lifespan_hooks_for_single_transport(
    transport: Literal["mcp", "a2a"],
) -> None:
    async def startup() -> None:
        pass

    with pytest.raises(ValueError, match="hooks require transport='both'"):
        build_asgi_app(
            _SalesPlatformWithMethods(),
            transport=transport,
            on_startup=(startup,),
        )


def test_build_asgi_app_warns_when_a2a_ignores_mcp_session_settings() -> None:
    with pytest.warns(UserWarning, match="MCP-only session fields"):
        app = build_asgi_app(
            _SalesPlatformWithMethods(),
            transport="a2a",
            session_idle_timeout=None,
        )
    assert callable(app)


def test_build_asgi_app_forwards_max_request_size() -> None:
    """``max_request_size=`` is accepted — construction succeeds."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, max_request_size=1024 * 1024)
    assert callable(app)


def test_build_asgi_app_forwards_auth_smoke() -> None:
    """``auth=BearerTokenAuth(...)`` is applied — construction succeeds."""
    from adcp.server.auth import BearerTokenAuth

    platform = _SalesPlatformWithMethods()
    auth = BearerTokenAuth(validate_token=lambda token: token == "tok_test")
    app = build_asgi_app(platform, auth=auth)
    assert callable(app)


def test_build_asgi_app_auth_rejects_unauthenticated() -> None:
    """An app built with ``auth=`` returns 401 for non-discovery requests
    missing a bearer token.

    ``initialize`` is exempt per spec; ``tools/call`` with a non-discovery
    tool is not — the auth middleware should reject it before the request
    reaches the MCP session manager (no lifespan needed for this path).
    """
    import asyncio

    from adcp.server.auth import BearerTokenAuth

    platform = _SalesPlatformWithMethods()
    auth = BearerTokenAuth(validate_token=lambda token: token == "tok_test")
    app = build_asgi_app(platform, auth=auth, allowed_hosts=["test"])

    async def _run() -> int:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # ``tools/call`` with a non-discovery tool is not in the auth
            # bypass list — expect 401 before the request reaches the MCP
            # session manager.
            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_products", "arguments": {}},
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
            return resp.status_code

    status = asyncio.run(_run())
    assert status == 401


def test_build_asgi_app_discovery_endpoint_mounted_when_base_url_provided() -> None:
    """When ``discovery_base_url=`` is given, the well-known discovery endpoint
    is mounted and returns 200."""
    import asyncio

    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(
        platform,
        allowed_hosts=["test"],
        discovery_base_url="http://test",
    )

    async def _run() -> int:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/.well-known/adcp-agents.json")
            return resp.status_code

    assert asyncio.run(_run()) == 200


@pytest.mark.parametrize("transport", ["mcp", "a2a", "both"])
def test_build_asgi_app_discovery_endpoint_absent_by_default(
    transport: Literal["mcp", "a2a", "both"],
) -> None:
    """Without ``discovery_base_url=``, the well-known path returns 404."""
    import asyncio

    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, transport=transport, allowed_hosts=["test"])

    async def _run() -> int:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/.well-known/adcp-agents.json")
            return resp.status_code

    assert asyncio.run(_run()) == 404


async def test_build_asgi_app_path_normalize_applied() -> None:
    """Trailing-slash requests route correctly without 307 redirect.

    The path normalizer strips ``/mcp/`` → ``/mcp`` before dispatch so
    ``follow_redirects=False`` clients see 200, not 307.

    Uses ``asyncio.to_thread`` to build the app (``validate_capabilities_response_shape``
    inside ``create_adcp_server_from_platform`` calls ``asyncio.run()`` which
    cannot be called from a running loop).
    """
    import asyncio as _asyncio

    from asgi_lifespan import LifespanManager

    platform = _SalesPlatformWithMethods()
    app = await _asyncio.to_thread(build_asgi_app, platform, allowed_hosts=["test"])

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            resp = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
    # Path normalizer strips trailing slash — 200, not 307.
    assert resp.status_code == 200


async def test_build_asgi_app_both_routes_mcp_and_a2a() -> None:
    """The unified helper exposes MCP at ``/mcp`` and A2A at the root."""
    import asyncio as _asyncio

    from asgi_lifespan import LifespanManager

    app = await _asyncio.to_thread(
        build_asgi_app,
        _SalesPlatformWithMethods(),
        transport="both",
        allowed_hosts=["test"],
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            mcp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
            agent_card = await client.get("/.well-known/agent.json")

    assert mcp.status_code == 200
    assert agent_card.status_code == 200


async def test_build_asgi_app_both_discovery_lists_both_transports() -> None:
    import asyncio as _asyncio

    app = await _asyncio.to_thread(
        build_asgi_app,
        _SalesPlatformWithMethods(),
        transport="both",
        allowed_hosts=["test"],
        discovery_base_url="http://127.0.0.1",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/.well-known/adcp-agents.json")

    assert response.status_code == 200
    assert {agent["transport"] for agent in response.json()["agents"]} == {"mcp", "a2a"}


async def test_build_test_client_both_applies_auth_to_both_legs() -> None:
    from adcp.server.auth import BearerTokenAuth

    auth = BearerTokenAuth(validate_token=lambda token: token == "tok_test")
    async with build_test_client(
        _SalesPlatformWithMethods(), transport="both", auth=auth
    ) as client:
        mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_products", "arguments": {}},
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        a2a = await client.post("/", json={})
        agent_card = await client.get("/.well-known/agent.json")

    assert mcp.status_code == 401
    assert a2a.status_code == 401
    assert agent_card.status_code == 200


# ---- build_test_client: new serve-layer kwargs ----


async def test_build_test_client_forwards_auth() -> None:
    """``auth=`` is forwarded through ``build_test_client`` — unauthenticated
    non-discovery requests get 401.

    ``tools/call`` with a non-discovery tool is not in the auth bypass list.
    """
    from adcp.server.auth import BearerTokenAuth

    platform = _SalesPlatformWithMethods()
    auth = BearerTokenAuth(validate_token=lambda token: token == "tok_test")
    async with build_test_client(platform, auth=auth) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_products", "arguments": {}},
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 401


async def test_build_test_client_forwards_validation_none() -> None:
    """``validation=None`` disables schema validation — construction succeeds."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform, validation=None) as client:
        assert client is not None


async def test_build_test_client_runs_production_lifespan_hooks() -> None:
    events: list[str] = []

    async def startup() -> None:
        events.append("startup")

    async def shutdown() -> None:
        events.append("shutdown")

    async with build_test_client(
        _SalesPlatformWithMethods(),
        transport="both",
        on_startup=(startup,),
        on_shutdown=(shutdown,),
    ):
        assert events == ["startup"]

    assert events == ["startup", "shutdown"]


# ---- build_test_client ----


async def test_build_test_client_yields_httpx_async_client() -> None:
    """The context manager yields an ``httpx.AsyncClient`` instance."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform) as client:
        assert isinstance(client, httpx.AsyncClient)


async def test_build_test_client_default_base_url() -> None:
    """Default ``base_url="http://test"`` is used when not overridden."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform) as client:
        assert str(client.base_url) == "http://test"


async def test_build_test_client_custom_base_url() -> None:
    """``base_url`` override is forwarded to the client."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform, base_url="http://localhost") as client:
        assert str(client.base_url) == "http://localhost"


async def test_build_test_client_can_make_request() -> None:
    """The yielded client can actually reach the mounted MCP endpoint."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform) as client:
        resp = await client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code == 200


async def test_build_test_client_headers_kwarg() -> None:
    """Default ``headers=`` are attached to the client — not silently dropped."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform, headers={"x-custom": "value"}) as client:
        assert "x-custom" in dict(client.headers)


async def test_build_test_client_follow_redirects_default_true() -> None:
    """``follow_redirects`` defaults to ``True`` on the yielded client."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform) as client:
        assert client.follow_redirects is True


async def test_build_test_client_follow_redirects_override() -> None:
    """``follow_redirects=False`` is respected."""
    platform = _SalesPlatformWithMethods()
    async with build_test_client(platform, follow_redirects=False) as client:
        assert client.follow_redirects is False


def test_build_test_client_raises_import_error_without_asgi_lifespan() -> None:
    """Missing ``asgi-lifespan`` raises ``ImportError`` with an actionable message."""
    import sys
    import unittest.mock

    platform = _SalesPlatformWithMethods()
    with unittest.mock.patch.dict(sys.modules, {"asgi_lifespan": None}):
        with pytest.raises(ImportError, match="asgi-lifespan is required"):
            import asyncio

            asyncio.run(build_test_client(platform).__aenter__())


# ---- build_asgi_app: pre_validation_hooks ----


def test_build_asgi_app_forwards_pre_validation_hooks() -> None:
    """``pre_validation_hooks=`` is accepted and forwarded to ``create_mcp_server``
    — construction succeeds and the app is callable."""
    from typing import Any

    platform = _SalesPlatformWithMethods()

    def my_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {**args, "buying_mode": "brief"}

    app = build_asgi_app(
        platform,
        pre_validation_hooks={"get_products": my_hook},
    )
    assert callable(app)


async def test_build_test_client_forwards_pre_validation_hooks() -> None:
    """``pre_validation_hooks=`` is forwarded through ``build_test_client``
    — construction succeeds and the context manager yields a client."""
    from typing import Any

    platform = _SalesPlatformWithMethods()

    def buying_mode_hook(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {**args, "buying_mode": args.get("buying_mode", "brief")}

    async with build_test_client(
        platform,
        pre_validation_hooks={"get_products": buying_mode_hook},
    ) as client:
        assert client is not None
