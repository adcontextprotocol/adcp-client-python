"""Unit tests for ``adcp.testing.decisioning`` helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import Account
from adcp.testing import build_asgi_app, make_request_context

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


def test_build_asgi_app_returns_asgi_callable() -> None:
    """The returned object is a callable ASGI app — can be invoked
    directly or handed to a test client."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform)
    # ASGI apps are callables: app(scope, receive, send) is async.
    assert callable(app)


def test_build_asgi_app_default_skips_webhook_gate() -> None:
    """A sales platform without webhook_sender wired would normally
    trip the F12 boot-time gate. The helper's
    ``auto_emit_completion_webhooks=False`` default skips it so tests
    can construct the app without wiring webhook infra."""
    platform = _SalesPlatformWithMethods()
    # Should not raise the F12 gate AdcpError.
    app = build_asgi_app(platform)
    assert app is not None


def test_build_asgi_app_threads_name() -> None:
    """``name=`` reaches the underlying MCP server."""
    platform = _SalesPlatformWithMethods()
    app = build_asgi_app(platform, name="custom-test-agent")
    # The Starlette app exposes the underlying FastMCP via internal
    # state; we don't assert the wiring path (framework-internal),
    # just that constructing with ``name`` doesn't raise.
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
