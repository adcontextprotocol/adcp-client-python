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
async def test_tenant_router_strips_port_and_lowercases_host() -> None:
    """The middleware passes the raw Host header. RFC 7230 makes it
    case-insensitive and lets the client include ``:port``; the
    Protocol docstring is explicit that implementations strip the
    port suffix as needed. ``ACME.localhost:3001`` and
    ``acme.localhost`` MUST hit the same DB row."""
    from src.tenant_router import SqlSubdomainTenantRouter

    captured: list[str] = []

    class _CapturingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))

            class _Result:
                def scalar_one_or_none(self):
                    return None

            return _Result()

    router = SqlSubdomainTenantRouter(sessionmaker=lambda: _CapturingSession())  # type: ignore[arg-type]
    await router.resolve("ACME.localhost:3001")
    assert captured, "expected a SQL execute"
    assert (
        "'acme.localhost'" in captured[-1]
    ), f"router did not normalize host before query: {captured[-1]!r}"


@pytest.mark.asyncio
async def test_buyer_registry_returns_none_without_tenant() -> None:
    """Without a tenant context (ContextVar unset), the registry
    returns None — the framework dispatch then rejects with
    PERMISSION_DENIED (with no ``details`` so the unrecognized-agent
    path is wire-indistinguishable from a recognized-but-denied
    response)."""
    from src.buyer_registry import TenantScopedBuyerAgentRegistry

    from adcp.decisioning import ApiKeyCredential

    registry = TenantScopedBuyerAgentRegistry(sessionmaker=lambda: None)  # type: ignore[arg-type]
    # No `current_tenant()` set — the registry should short-circuit
    # to None without touching the DB.
    cred = ApiKeyCredential(kind="api_key", key_id="any")
    assert await registry.resolve_by_agent_url("https://x/") is None
    assert await registry.resolve_by_credential(cred) is None
