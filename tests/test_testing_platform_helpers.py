"""Tests for adcp.testing.make_request_context and build_asgi_app."""

from __future__ import annotations

from adcp.decisioning import (
    Account,
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import SupportedProtocol
from adcp.decisioning.context import RequestContext
from adcp.testing import build_asgi_app, make_request_context


class _MinimalPlatform(DecisioningPlatform):
    """Smallest valid platform: media_buy protocol, agent billing, singleton account."""

    capabilities = DecisioningCapabilities(
        supported_protocols=[SupportedProtocol.media_buy],
        supported_billing=("agent",),
    )
    accounts = SingletonAccounts(account_id="test-singleton")


# ---------------------------------------------------------------------------
# make_request_context
# ---------------------------------------------------------------------------


def test_make_request_context_default_account_id():
    ctx = make_request_context()
    assert ctx.account.id == "test-account"


def test_make_request_context_caller_identity_is_populated():
    # caller_identity is set for test convenience; its exact format is
    # opaque (production uses a composite key). Assert non-None, not the
    # string value, to avoid teaching adopters to parse the field.
    ctx = make_request_context()
    assert ctx.caller_identity is not None


def test_make_request_context_custom_account_id():
    ctx = make_request_context(account_id="acme-corp")
    assert ctx.account.id == "acme-corp"
    assert ctx.caller_identity is not None


def test_make_request_context_pre_built_account():
    acct = Account(id="pre-built", name="Acme Corp")
    ctx = make_request_context(account=acct)
    assert ctx.account.id == "pre-built"
    assert ctx.account.name == "Acme Corp"
    assert ctx.caller_identity is not None


def test_make_request_context_pre_built_account_ignores_account_id():
    acct = Account(id="pre-built")
    ctx = make_request_context(account=acct, account_id="ignored")
    assert ctx.account.id == "pre-built"


def test_make_request_context_override_request_id():
    ctx = make_request_context(account_id="acme", request_id="req-123")
    assert ctx.request_id == "req-123"
    assert ctx.account.id == "acme"


def test_make_request_context_override_caller_identity():
    ctx = make_request_context(account_id="acme", caller_identity="custom-principal")
    assert ctx.caller_identity == "custom-principal"
    assert ctx.account.id == "acme"


def test_make_request_context_returns_request_context_instance():
    ctx = make_request_context()
    assert isinstance(ctx, RequestContext)


def test_make_request_context_state_and_resolve_stubs_present():
    ctx = make_request_context()
    assert ctx.state is not None
    assert ctx.resolve is not None


# ---------------------------------------------------------------------------
# build_asgi_app
# ---------------------------------------------------------------------------


def test_build_asgi_app_returns_callable():
    platform = _MinimalPlatform()
    app = build_asgi_app(platform)
    assert callable(app)


def test_build_asgi_app_with_explicit_name():
    platform = _MinimalPlatform()
    app = build_asgi_app(platform, name="my-test-server")
    assert callable(app)


def test_build_asgi_app_responds_to_mcp_initialize():
    """Smoke-test: the returned ASGI app handles an MCP initialize request.

    build_asgi_app calls asyncio.run() internally (validate_capabilities_response_shape);
    it must be called from a sync context. The async HTTP exercise runs via
    a nested asyncio.run() which is safe here because no event loop is
    running in a sync pytest function.
    """
    import asyncio

    import httpx
    from asgi_lifespan import LifespanManager

    platform = _MinimalPlatform()
    app = build_asgi_app(platform, name="smoke-test")

    async def _run() -> None:
        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                follow_redirects=True,
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
                            "clientInfo": {"name": "test-client", "version": "1.0"},
                        },
                    },
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body.get("result") is not None

    asyncio.run(_run())
