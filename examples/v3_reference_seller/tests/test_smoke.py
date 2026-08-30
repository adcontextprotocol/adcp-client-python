"""Smoke tests for the v3 reference seller (translator pattern).

Verify the components import cleanly, the Protocol shapes match the
framework's expectations, and the platform constructs without errors.

Translator-pattern tests (HTTP-mocked upstream calls) live in
:mod:`test_smoke_translator`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add the example dir to sys.path so `src.*` imports resolve.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


def test_models_import_and_declare_tables() -> None:
    from src.models import Account, Base, BuyerAgent, Tenant

    table_names = {t.name for t in Base.metadata.tables.values()}
    # Translator pattern — no MediaBuy / Creative / PerformanceFeedback
    # tables. Ad-ops state lives upstream.
    assert {"tenants", "buyer_agents", "accounts"} <= table_names
    assert "media_buys" not in table_names
    assert "creatives" not in table_names
    assert "performance_feedback" not in table_names
    for cls in (Tenant, BuyerAgent, Account):
        assert cls.__tablename__ in table_names

    api_key_index = next(
        index for index in BuyerAgent.__table__.indexes if index.name == "buyer_agents_api_key_uidx"
    )
    assert api_key_index.unique is True


def test_platform_satisfies_decisioning_protocol() -> None:
    """The platform impl exists and can be inspected without an
    actual session — the class shape doesn't depend on a real
    sessionmaker or upstream client."""
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import DecisioningPlatform
    from adcp.decisioning.specialisms import SalesPlatform

    assert issubclass(V3ReferenceSeller, DecisioningPlatform)
    assert issubclass(V3ReferenceSeller, SalesPlatform)
    # Translator claims BOTH guaranteed and non-guaranteed sales —
    # real GAM-shaped publishers sell both surfaces.
    # ``specialisms`` is ``list[Specialism | str]`` (#479) — extract slug
    # uniformly via ``.value``.
    declared = {
        s.value if hasattr(s, "value") else s for s in V3ReferenceSeller.capabilities.specialisms
    }
    assert "sales-non-guaranteed" in declared
    assert "sales-guaranteed" in declared


def test_buyer_registry_satisfies_protocol() -> None:
    from src.buyer_registry import TenantScopedBuyerAgentRegistry

    from adcp.decisioning import BuyerAgentRegistry

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
    cred = ApiKeyCredential(kind="api_key", key_id="any")
    assert await registry.resolve_by_agent_url("https://x/") is None
    assert await registry.resolve_by_credential(cred) is None


@pytest.mark.asyncio
async def test_buyer_registry_denies_ambiguous_legacy_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-index duplicate credentials fail closed instead of raising."""
    from types import SimpleNamespace

    from src.buyer_registry import TenantScopedBuyerAgentRegistry

    from adcp.decisioning import ApiKeyCredential

    result = MagicMock()
    result.scalars.return_value.all.return_value = [MagicMock(), MagicMock()]
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "src.buyer_registry.current_tenant",
        lambda: SimpleNamespace(id="tenant-a"),
    )
    registry = TenantScopedBuyerAgentRegistry(sessionmaker=MagicMock(return_value=session))

    resolved = await registry.resolve_by_credential(
        ApiKeyCredential(kind="api_key", key_id="legacy-duplicate")
    )

    assert resolved is None
    statement = session.execute.await_args.args[0]
    assert statement._limit_clause.value == 2  # noqa: SLF001 - query safety assertion


def test_platform_default_does_not_advertise_webhook_signing() -> None:
    """Out-of-the-box, the reference seller advertises no
    webhook-signing capability — the constructor flag is opt-in. Boot
    succeeds without a PEM keypair.
    """
    from src.platform import V3ReferenceSeller

    assert V3ReferenceSeller.capabilities.webhook_signing is None
    assert V3ReferenceSeller.capabilities.adcp is not None
    assert V3ReferenceSeller.capabilities.adcp.idempotency.supported is False


def test_platform_advertises_webhook_signing_when_alg_passed() -> None:
    """With ``webhook_signing_alg`` wired, the per-instance capabilities
    advertise ``webhook_signing.supported=True`` and the matching
    algorithm — the #384 boot validator gates on this exact shape.
    """
    from src.platform import V3ReferenceSeller

    seller = V3ReferenceSeller(
        sessionmaker=lambda: None,  # type: ignore[arg-type]
        upstream_api_key="test-key",
        mock_upstream_url=None,
        webhook_signing_alg="ed25519",
        webhook_retry_horizon_seconds=172800,
    )
    ws = seller.capabilities.webhook_signing
    assert ws is not None
    assert ws.supported is True
    assert ws.profile == "adcp/webhook-signing/v1"
    assert ws.delivery_retry_horizon_seconds == 172800
    assert ws.algorithms is not None
    assert [a.value for a in ws.algorithms] == ["ed25519"]


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated_tenant", ["tenant-a", None])
async def test_bearer_context_rejects_cross_tenant_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    authenticated_tenant: str | None,
) -> None:
    """A mismatched or absent token tenant cannot be rebound to the host."""
    import src.app as app_module

    from adcp.decisioning import AdcpError
    from adcp.server import ToolContext
    from adcp.server.auth import (
        AUTHENTICATED_TENANT_METADATA_KEY,
        ROUTED_TENANT_METADATA_KEY,
    )

    monkeypatch.setattr(
        app_module,
        "auth_context_factory",
        lambda _meta: ToolContext(
            tenant_id=authenticated_tenant,
            metadata={
                AUTHENTICATED_TENANT_METADATA_KEY: authenticated_tenant,
                ROUTED_TENANT_METADATA_KEY: "tenant-b",
            },
        ),
    )
    context = app_module._build_context_factory()(object())
    assert context.tenant_id == "tenant-b"

    with pytest.raises(AdcpError) as excinfo:
        await app_module.enforce_authenticated_tenant("get_products", {}, context, AsyncMock())
    assert excinfo.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_token_loader_rejects_duplicate_bearer_identifiers() -> None:
    """A bearer value must resolve to exactly one buyer and tenant."""
    import src.app as app_module

    rows = [
        MagicMock(
            api_key_id="duplicate",
            agent_url="https://a.example/",
            tenant_id="tenant-a",
        ),
        MagicMock(
            api_key_id="duplicate",
            agent_url="https://b.example/",
            tenant_id="tenant-b",
        ),
    ]
    result = MagicMock()
    result.scalars.return_value = rows
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=result)

    with pytest.raises(RuntimeError, match="Duplicate buyer-agent api_key_id"):
        await app_module._load_token_map(MagicMock(return_value=session))
