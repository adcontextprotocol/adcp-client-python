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
async def test_sync_accounts_strips_bank_details() -> None:
    """sync_accounts must NOT echo bank details in its response (write-only guard)
    and MUST preserve non-write-only fields such as legal_name."""
    from unittest.mock import MagicMock, patch

    from src.platform import V3ReferenceSeller

    from adcp.decisioning import BuyerAgent, RequestContext
    from adcp.types import SyncAccountsRequest

    # --- stub DB session ---------------------------------------------------
    mock_ba_row = MagicMock()
    mock_ba_row.id = "ba_stub123"

    class _StubSession:
        """Returns BuyerAgent row on first execute, no existing account on second."""

        def __init__(self) -> None:
            self._calls = 0

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def begin(self) -> _BeginCM:
            return _BeginCM()

        async def execute(self, _stmt: object) -> MagicMock:
            self._calls += 1
            result = MagicMock()
            if self._calls == 1:
                result.scalar_one_or_none.return_value = mock_ba_row
            else:
                result.scalar_one_or_none.return_value = None  # new account
            return result

        def add(self, _row: object) -> None:
            pass

    class _BeginCM:
        async def __aenter__(self) -> _BeginCM:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    sessionmaker = MagicMock(return_value=_StubSession())

    seller = V3ReferenceSeller(sessionmaker=sessionmaker)

    req = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "smoke-test-sync-key-abc1234567",
            "accounts": [
                {
                    "brand": {"domain": "acme.com"},
                    "operator": "agency.com",
                    "billing": "operator",
                    "billing_entity": {
                        "legal_name": "Acme Corp",
                        "address": {
                            "street": "123 Main St",
                            "city": "Springfield",
                            "postal_code": "62701",
                            "country": "US",
                        },
                        "bank": {
                            "account_holder": "Acme Corp",
                            "iban": "GB29NWBK60161331926819",
                            "bic": "NWBKGB2LXXX",
                        },
                    },
                }
            ],
        }
    )

    fake_tenant = MagicMock()
    fake_tenant.id = "t_smoke123"
    buyer_agent = BuyerAgent(
        agent_url="https://buyer.example.com/",
        display_name="Test Buyer",
        status="active",
        billing_capabilities=frozenset(["operator"]),
    )
    ctx = RequestContext(buyer_agent=buyer_agent)

    with patch("src.platform.current_tenant", return_value=fake_tenant):
        response = await seller.sync_accounts(req, ctx)

    payload = response.model_dump(mode="json", exclude_none=True)
    assert payload["accounts"], "Expected at least one account result"
    for acct_result in payload["accounts"]:
        be = acct_result.get("billing_entity")
        assert be is not None, "billing_entity must be echoed in response"
        assert "bank" not in be, "bank details (write-only) must not appear in response"
        assert be.get("legal_name") == "Acme Corp", "legal_name must be preserved"


@pytest.mark.asyncio
async def test_list_accounts_strips_bank_details() -> None:
    """list_accounts must project billing_entity through the write-only guard —
    bank absent, other fields preserved."""
    from unittest.mock import MagicMock, patch

    from src.platform import V3ReferenceSeller

    from adcp.decisioning import BuyerAgent, RequestContext
    from adcp.types import ListAccountsRequest

    mock_ba_row = MagicMock()
    mock_ba_row.id = "ba_stub456"

    # Synthetic AccountRow with bank details stored in billing_entity
    mock_acct_row = MagicMock()
    mock_acct_row.account_id = "acme.com:agency.com"
    mock_acct_row.name = "Acme Corp"
    mock_acct_row.status = "active"
    mock_acct_row.billing = "operator"
    mock_acct_row.billing_entity = {
        "legal_name": "Acme Corp",
        "address": {
            "street": "123 Main St",
            "city": "Springfield",
            "postal_code": "62701",
            "country": "US",
        },
        "bank": {
            "account_holder": "Acme Corp",
            "iban": "GB29NWBK60161331926819",
            "bic": "NWBKGB2LXXX",
        },
    }
    mock_acct_row.rate_card = None
    mock_acct_row.payment_terms = None
    mock_acct_row.credit_limit = None
    mock_acct_row.sandbox = False
    mock_acct_row.ext = None
    mock_acct_row.reporting_bucket = None

    class _StubSession:
        def __init__(self) -> None:
            self._calls = 0

        async def __aenter__(self) -> _StubSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def execute(self, _stmt: object) -> MagicMock:
            self._calls += 1
            result = MagicMock()
            if self._calls == 1:
                result.scalar_one_or_none.return_value = mock_ba_row
            else:
                scalars_mock = MagicMock()
                scalars_mock.all.return_value = [mock_acct_row]
                result.scalars.return_value = scalars_mock
            return result

    sessionmaker = MagicMock(return_value=_StubSession())

    seller = V3ReferenceSeller(sessionmaker=sessionmaker)
    req = ListAccountsRequest.model_validate({})

    fake_tenant = MagicMock()
    fake_tenant.id = "t_smoke456"
    buyer_agent = BuyerAgent(
        agent_url="https://buyer.example.com/",
        display_name="Test Buyer",
        status="active",
        billing_capabilities=frozenset(["operator"]),
    )
    ctx = RequestContext(buyer_agent=buyer_agent)

    with patch("src.platform.current_tenant", return_value=fake_tenant):
        response = await seller.list_accounts(req, ctx)

    payload = response.model_dump(mode="json", exclude_none=True)
    assert payload["accounts"], "Expected at least one account"
    for acct in payload["accounts"]:
        be = acct.get("billing_entity")
        assert be is not None, "billing_entity must be present"
        assert "bank" not in be, "bank details (write-only) must not appear in response"
        assert be.get("legal_name") == "Acme Corp", "legal_name must be preserved"


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
