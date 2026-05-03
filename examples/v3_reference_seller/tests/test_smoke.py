"""Smoke tests for the v3 reference seller.

Verify the components import cleanly, the Protocol shapes match the
framework's expectations, and the platform constructs without errors.
End-to-end PG tests live in the README's docker-compose flow — these
tests are the no-PG-needed safety net.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add the example dir to sys.path so `src.*` imports resolve.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


def test_models_import_and_declare_tables() -> None:
    from src.models import Account, Base, BuyerAgent, MediaBuy, Tenant

    table_names = {t.name for t in Base.metadata.tables.values()}
    assert {"tenants", "buyer_agents", "accounts", "media_buys"} <= table_names
    # Sanity: every model is in the metadata.
    for cls in (Tenant, BuyerAgent, Account, MediaBuy):
        assert cls.__tablename__ in table_names


def test_platform_satisfies_decisioning_protocol() -> None:
    """The platform impl exists and can be inspected without an
    actual session — adopter middleware would never construct without
    a real sessionmaker, but the class shape doesn't depend on it."""
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import DecisioningPlatform
    from adcp.decisioning.specialisms import SalesPlatform

    assert issubclass(V3ReferenceSeller, DecisioningPlatform)
    assert issubclass(V3ReferenceSeller, SalesPlatform)
    assert "sales-non-guaranteed" in V3ReferenceSeller.capabilities.specialisms


def test_buyer_registry_satisfies_protocol() -> None:
    from src.buyer_registry import TenantScopedBuyerAgentRegistry

    from adcp.decisioning import BuyerAgentRegistry

    # Construct without a sessionmaker — the registry's lookups never
    # fire here, so a placeholder is fine for the structural check.
    registry = TenantScopedBuyerAgentRegistry(sessionmaker=lambda: None)  # type: ignore[arg-type]
    assert isinstance(registry, BuyerAgentRegistry)


def test_audit_sink_implements_protocol() -> None:
    from src.audit import DbAuditSink

    from adcp.audit_sink import AuditSink

    sink = DbAuditSink(sessionmaker=lambda: None)  # type: ignore[arg-type]
    assert isinstance(sink, AuditSink)


def test_tenant_router_satisfies_protocol() -> None:
    from src.tenant_router import SqlSubdomainTenantRouter

    from adcp.server import SubdomainTenantRouter

    router = SqlSubdomainTenantRouter(sessionmaker=lambda: None)  # type: ignore[arg-type]
    assert isinstance(router, SubdomainTenantRouter)


@pytest.mark.asyncio
async def test_tenant_router_returns_none_without_session_match() -> None:
    """Resolution against a session that yields no row returns None
    — the middleware then 404s the request."""
    from src.tenant_router import SqlSubdomainTenantRouter

    class _NullSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, _stmt):
            class _Result:
                def scalar_one_or_none(self):
                    return None

            return _Result()

    router = SqlSubdomainTenantRouter(sessionmaker=lambda: _NullSession())  # type: ignore[arg-type]
    result = await router.resolve("unknown.example.com")
    assert result is None


@pytest.mark.asyncio
async def test_buyer_registry_returns_none_without_tenant() -> None:
    """Without a tenant context (ContextVar unset), the registry
    returns None — the framework dispatch then rejects with
    REQUEST_AUTH_UNRECOGNIZED_AGENT."""
    from src.buyer_registry import TenantScopedBuyerAgentRegistry

    from adcp.decisioning import ApiKeyCredential

    registry = TenantScopedBuyerAgentRegistry(sessionmaker=lambda: None)  # type: ignore[arg-type]
    # No `current_tenant()` set — the registry should short-circuit
    # to None without touching the DB.
    cred = ApiKeyCredential(kind="api_key", key_id="any")
    assert await registry.resolve_by_agent_url("https://x/") is None
    assert await registry.resolve_by_credential(cred) is None


# ---------------------------------------------------------------------------
# Validation config smoke tests (no DB, no HTTP)
# ---------------------------------------------------------------------------


def test_build_validation_config_strict_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_validation_config() defaults to strict when ADCP_ENV is unset."""
    monkeypatch.delenv("ADCP_ENV", raising=False)

    from src.app import _build_validation_config

    cfg = _build_validation_config()
    assert cfg.requests == "strict"
    assert cfg.responses == "strict"


def test_build_validation_config_warn_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_validation_config() returns warn mode when ADCP_ENV=production."""
    monkeypatch.setenv("ADCP_ENV", "production")

    from src.app import _build_validation_config

    cfg = _build_validation_config()
    assert cfg.requests == "warn"
    assert cfg.responses == "warn"


def test_build_validation_config_warn_for_prod_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_validation_config() also accepts ADCP_ENV=prod (short form)."""
    monkeypatch.setenv("ADCP_ENV", "prod")

    from src.app import _build_validation_config

    cfg = _build_validation_config()
    assert cfg.requests == "warn"


@pytest.mark.asyncio
async def test_strict_validation_rejects_malformed_request() -> None:
    """Strict mode rejects an empty get_products call before the handler runs."""
    from adcp.exceptions import ADCPTaskError
    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller
    from adcp.validation import ValidationHookConfig

    class _StubSeller(ADCPHandler):  # type: ignore[type-arg]
        called = False

        async def get_products(self, params: dict, context: ToolContext | None = None) -> dict:  # type: ignore[override]
            _StubSeller.called = True
            return {"products": []}

    handler = _StubSeller()
    caller = create_tool_caller(
        handler, "get_products", validation=ValidationHookConfig(requests="strict")
    )
    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({})
    assert not handler.called, "handler must not be called when request is invalid"
    assert exc_info.value.errors[0].code == "VALIDATION_ERROR"
    assert exc_info.value.errors[0].details["side"] == "request"


@pytest.mark.asyncio
async def test_warn_validation_processes_malformed_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn mode logs a warning but still dispatches the handler."""
    import logging

    from adcp.server.base import ADCPHandler, ToolContext
    from adcp.server.mcp_tools import create_tool_caller
    from adcp.validation import ValidationHookConfig

    class _StubSeller(ADCPHandler):  # type: ignore[type-arg]
        called = False

        async def get_products(self, params: dict, context: ToolContext | None = None) -> dict:  # type: ignore[override]
            _StubSeller.called = True
            return {"products": []}

    handler = _StubSeller()
    caller = create_tool_caller(
        handler, "get_products", validation=ValidationHookConfig(requests="warn")
    )
    with caplog.at_level(logging.WARNING, logger="adcp.server.mcp_tools"):
        result = await caller({})
    assert handler.called, "handler must be called in warn mode"
    assert isinstance(result.get("products"), list)
    assert any("validation warning" in r.message.lower() for r in caplog.records)
