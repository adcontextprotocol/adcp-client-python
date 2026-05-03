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
    from src.models import Account, Base, BuyerAgent, MediaBuy, PerformanceFeedback, Tenant

    table_names = {t.name for t in Base.metadata.tables.values()}
    assert {
        "tenants",
        "buyer_agents",
        "accounts",
        "media_buys",
        "performance_feedback",
    } <= table_names
    # Sanity: every model is in the metadata.
    for cls in (Tenant, BuyerAgent, Account, MediaBuy, PerformanceFeedback):
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


def test_platform_has_all_nine_sales_methods() -> None:
    """V3ReferenceSeller exposes all nine SalesPlatform methods."""
    from src.platform import V3ReferenceSeller

    required = {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
    }
    missing = required - set(dir(V3ReferenceSeller))
    assert not missing, f"Missing methods: {missing}"


@pytest.mark.asyncio
async def test_list_creative_formats_returns_valid_response() -> None:
    """list_creative_formats returns a spec-valid empty catalog."""
    from src.platform import V3ReferenceSeller

    from adcp.types import ListCreativeFormatsRequest, ListCreativeFormatsResponse

    platform = V3ReferenceSeller(sessionmaker=lambda: None)  # type: ignore[arg-type]
    req = ListCreativeFormatsRequest()
    resp = await platform.list_creative_formats(req, ctx=None)  # type: ignore[arg-type]
    assert isinstance(resp, ListCreativeFormatsResponse)
    assert resp.formats == []


@pytest.mark.asyncio
async def test_list_creatives_returns_valid_response() -> None:
    """list_creatives returns a spec-valid empty result."""
    from src.platform import V3ReferenceSeller

    from adcp.types import ListCreativesRequest, ListCreativesResponse

    platform = V3ReferenceSeller(sessionmaker=lambda: None)  # type: ignore[arg-type]
    req = ListCreativesRequest()
    resp = await platform.list_creatives(req, ctx=None)  # type: ignore[arg-type]
    assert isinstance(resp, ListCreativesResponse)
    assert resp.creatives == []
    assert resp.query_summary.total_matching == 0


def test_performance_feedback_table_has_idempotency_constraint() -> None:
    """PerformanceFeedback table declares the idempotency unique constraint."""
    from src.models import Base

    table = Base.metadata.tables["performance_feedback"]
    constraint_names = {c.name for c in table.constraints}
    assert "perf_feedback_idem_uk" in constraint_names
