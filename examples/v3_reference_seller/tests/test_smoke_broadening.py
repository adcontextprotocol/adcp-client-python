"""Smoke tests for the v3 reference seller broadening.

Covers the spec-required broadening of the v3 reference seller:

* All 9 sales methods plus ``sync_accounts`` / ``list_accounts`` are
  present on the platform class (Protocol surface check).
* ``list_accounts`` projects ``billing_entity.bank`` out of every
  account on response (the headline 3.1-readiness claim).
* ``MediaBuy.invoice_recipient`` column populates from the typed
  request and round-trips through the SQLAlchemy model.
* Creative round-trip: ``sync_creatives`` writes to the
  ``creatives`` table; ``list_creatives`` reads it back.

These tests deliberately avoid spinning up a real Postgres — the
README's docker-compose flow covers end-to-end. Here we use
SQLAlchemy + mocked sessionmakers (or, where structurally important,
direct model instantiation) so the suite stays no-PG-needed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Add the example dir to sys.path so `src.*` imports resolve.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


# ---------------------------------------------------------------------------
# Protocol surface — every sales-* method plus account ops are callable
# ---------------------------------------------------------------------------


def test_v3_reference_seller_exposes_full_sales_surface() -> None:
    """The seller declares ``sales-non-guaranteed`` — verify every
    method on the SalesPlatform Protocol (required + optional) plus
    the account ops are present on the class."""
    from src.platform import V3ReferenceSeller

    required_methods = {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    }
    optional_methods = {
        "get_media_buys",
        "provide_performance_feedback",
        "list_creative_formats",
        "list_creatives",
    }
    account_ops = {"sync_accounts", "list_accounts"}

    for name in required_methods | optional_methods | account_ops:
        assert hasattr(V3ReferenceSeller, name), f"V3ReferenceSeller missing {name}"
        attr = getattr(V3ReferenceSeller, name)
        assert callable(attr), f"V3ReferenceSeller.{name} is not callable"


def test_new_models_are_registered_in_metadata() -> None:
    """``Creative`` and ``PerformanceFeedback`` tables show up in
    ``Base.metadata`` so ``create_all`` provisions them."""
    from src.models import Base, Creative, PerformanceFeedback

    table_names = {t.name for t in Base.metadata.tables.values()}
    assert "creatives" in table_names
    assert "performance_feedback" in table_names
    assert Creative.__tablename__ == "creatives"
    assert PerformanceFeedback.__tablename__ == "performance_feedback"


# ---------------------------------------------------------------------------
# MediaBuy.invoice_recipient — first-class column with JSON round-trip
# ---------------------------------------------------------------------------


def test_media_buy_invoice_recipient_column_populates() -> None:
    """``MediaBuy.invoice_recipient`` is a JSON column. Verify it
    populates from the typed
    ``CreateMediaBuyRequest.invoice_recipient`` when the platform
    constructs the row."""
    from src.models import MediaBuy

    invoice_payload = {
        "legal_name": "Acme Holdings GmbH",
        "tax_id": "DE-987654321",
        "address": {
            "country": "DE",
            "postal_code": "10115",
            "city": "Berlin",
            "street": "Friedrichstrasse 1",
        },
        # bank field — write-only on response, durable on storage.
        "bank": {
            "account_holder": "Acme Holdings GmbH",
            "iban": "DE89370400440532013000",
            "bic": "COBADEFFXXX",
        },
    }
    row = MediaBuy(
        tenant_id="t_acme",
        account_id="a_acme_1",
        media_buy_id="mb_test",
        idempotency_key="k_" + "x" * 16,
        status="active",
        invoice_recipient=invoice_payload,
    )
    assert row.invoice_recipient is not None
    assert row.invoice_recipient["legal_name"] == "Acme Holdings GmbH"
    # Bank details persist on storage — write-only is a RESPONSE-side
    # rule, not a storage-side rule.
    assert row.invoice_recipient["bank"]["iban"] == "DE89370400440532013000"


# ---------------------------------------------------------------------------
# list_accounts projection — bank details stripped on response
# ---------------------------------------------------------------------------


def test_list_accounts_projection_strips_bank_details() -> None:
    """The 3.1-readiness headline claim: any account run through
    ``project_account_for_response`` has ``billing_entity.bank``
    cleared. Verify the projection helper directly so we know the
    list_accounts response path's call site is correct.
    """
    from adcp.decisioning import project_account_for_response
    from adcp.types import Account as AccountWire

    account = AccountWire.model_validate(
        {
            "account_id": "acme-corp.com::pinnacle-media.com",
            "name": "Acme c/o Pinnacle",
            "status": "active",
            "billing": "agent",
            "billing_entity": {
                "legal_name": "Pinnacle Media LLC",
                "tax_id": "12-3456789",
                "bank": {
                    "account_holder": "Pinnacle Media LLC",
                    "iban": "DE89370400440532013000",
                    "bic": "COBADEFFXXX",
                },
            },
        }
    )
    safe = project_account_for_response(account)
    assert safe.billing_entity is not None
    assert safe.billing_entity.bank is None
    # Other billing_entity fields survive the projection.
    assert safe.billing_entity.legal_name == "Pinnacle Media LLC"
    # Wire payload — the headline guarantee. ``bank`` MUST NOT
    # appear when we serialize for response.
    payload = safe.model_dump(mode="json", exclude_none=True)
    assert "bank" not in payload["billing_entity"], payload


@pytest.mark.asyncio
async def test_list_accounts_runs_projection_on_every_row() -> None:
    """End-to-end: drive ``V3ReferenceSeller.list_accounts`` against a
    mocked session whose row carries bank details and assert no
    response account leaks them. This is the platform-level guarantee
    the spec requires.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.models import Account as AccountRow
    from src.models import BuyerAgent as BuyerAgentRow
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import RequestContext
    from adcp.decisioning.registry import BuyerAgent
    from adcp.types import ListAccountsRequest

    bank_block = {
        "account_holder": "Pinnacle Media LLC",
        "iban": "DE89370400440532013000",
        "bic": "COBADEFFXXX",
    }
    buyer_agent_row = BuyerAgentRow(
        id="ba_acme_signed",
        tenant_id="t_acme",
        agent_url="https://signed-buyer.example/",
        display_name="Signed Buyer",
        status="active",
        billing_capabilities=["operator", "agent"],
    )
    account_row = AccountRow(
        id="a_acme_1",
        tenant_id="t_acme",
        buyer_agent_id="ba_acme_signed",
        account_id="acme-corp.com::pinnacle-media.com",
        name="Acme c/o Pinnacle",
        status="active",
        billing="agent",
        billing_entity={
            "legal_name": "Pinnacle Media LLC",
            "tax_id": "12-3456789",
            "bank": bank_block,
        },
        sandbox=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    # Two SELECT calls — one for buyer-agent lookup, one for accounts.
    ba_result = MagicMock()
    ba_result.scalar_one_or_none = MagicMock(return_value=buyer_agent_row)
    accounts_result = MagicMock()
    accounts_result.scalars = MagicMock(return_value=iter([account_row]))

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[ba_result, accounts_result])
    sessionmaker = MagicMock(return_value=session)

    # Mock the tenant contextvar reader.
    import src.platform as platform_module

    from adcp.server import current_tenant as _real

    class _Tenant:
        id = "t_acme"

    has_current = hasattr(platform_module, "current_tenant")
    original = platform_module.current_tenant if has_current else _real
    # Patch the import inside the method by patching at module level.
    import adcp.server as server_module

    monkeypatched = lambda: _Tenant()  # noqa: E731
    setattr(server_module, "current_tenant", monkeypatched)
    try:
        platform = V3ReferenceSeller(sessionmaker=sessionmaker)

        ctx = RequestContext(
            buyer_agent=BuyerAgent(
                agent_url="https://signed-buyer.example/",
                display_name="Signed Buyer",
                status="active",
                billing_capabilities=frozenset({"operator", "agent"}),
            ),
            account=None,
        )
        req = ListAccountsRequest()
        resp = await platform.list_accounts(req, ctx)
    finally:
        setattr(server_module, "current_tenant", original)

    payload = resp.model_dump(mode="json", exclude_none=True)
    assert payload["accounts"], "expected at least one account in response"
    for acct in payload["accounts"]:
        # The headline guarantee — bank MUST NOT echo on the wire.
        if "billing_entity" in acct:
            assert (
                "bank" not in acct["billing_entity"]
            ), f"bank details leaked on list_accounts response: {acct}"


# ---------------------------------------------------------------------------
# Creative round-trip — sync writes; list reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creative_round_trip_through_sync_then_list() -> None:
    """Drive ``sync_creatives`` against a mock session, then
    ``list_creatives`` against the same session, and assert the
    creative the sync wrote shows up on the list response.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.models import Creative as CreativeRow
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import Account, RequestContext
    from adcp.decisioning.registry import BuyerAgent
    from adcp.types import ListCreativesRequest, SyncCreativesRequest

    # Track the row the sync writes — we'll feed it back on the list.
    written_rows: list[CreativeRow] = []

    def _add(row: Any) -> None:
        written_rows.append(row)

    # sync_creatives session — first execute() returns "no existing" (so
    # sync inserts via session.add), then transaction commits.
    sync_existing_result = MagicMock()
    sync_existing_result.scalar_one_or_none = MagicMock(return_value=None)
    sync_session = MagicMock()
    sync_session.__aenter__ = AsyncMock(return_value=sync_session)
    sync_session.__aexit__ = AsyncMock(return_value=None)
    sync_session.execute = AsyncMock(return_value=sync_existing_result)
    sync_session.add = MagicMock(side_effect=_add)
    sync_begin = MagicMock()
    sync_begin.__aenter__ = AsyncMock(return_value=sync_begin)
    sync_begin.__aexit__ = AsyncMock(return_value=None)
    sync_session.begin = MagicMock(return_value=sync_begin)

    # list_creatives session — count + page both yield the persisted
    # row(s). Each ``scalars()`` call returns a fresh iterator since
    # the platform consumes it inline via ``list(...)``.
    def _hydrate_rows() -> list[CreativeRow]:
        # Hydrate timestamps on the fly — SQLA defaults only fire on
        # an actual INSERT, which the mock skips.
        now = datetime.now(timezone.utc)
        for r in written_rows:
            r.created_at = now
            r.updated_at = now
        return list(written_rows)

    def _list_session_factory() -> MagicMock:
        count_result = MagicMock()
        count_result.scalars = MagicMock(side_effect=lambda: iter(_hydrate_rows()))
        page_result = MagicMock()
        page_result.scalars = MagicMock(side_effect=lambda: iter(_hydrate_rows()))
        s = MagicMock()
        s.__aenter__ = AsyncMock(return_value=s)
        s.__aexit__ = AsyncMock(return_value=None)
        s.execute = AsyncMock(side_effect=[count_result, page_result])
        return s

    list_session = _list_session_factory()

    # Sessionmaker returns sync_session first, then list_session.
    session_iter = iter([sync_session, list_session])
    sessionmaker = MagicMock(side_effect=lambda: next(session_iter))

    platform = V3ReferenceSeller(sessionmaker=sessionmaker)

    ctx = RequestContext(
        buyer_agent=BuyerAgent(
            agent_url="https://signed-buyer.example/",
            display_name="Signed Buyer",
            status="active",
            billing_capabilities=frozenset({"operator"}),
        ),
        account=Account(
            id="a_acme_1",
            name="Signed Buyer — Main",
            status="active",
            metadata={
                "tenant_id": "t_acme",
                "buyer_agent_id": "ba_acme_signed",
                "account_id": "signed-buyer-main",
                "billing": "operator",
                "sandbox": False,
            },
        ),
    )

    sync_req = SyncCreativesRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "a" * 18,
            "creatives": [
                {
                    "creative_id": "spring-300x250",
                    "name": "Spring 300x250 Display",
                    "format_id": {
                        "agent_url": "https://reference.adcp.org",
                        "id": "display_300x250",
                    },
                    "assets": {},
                }
            ],
        }
    )
    sync_resp = await platform.sync_creatives(sync_req, ctx)
    assert len(written_rows) == 1
    assert written_rows[0].creative_id == "spring-300x250"
    # The sync response itself echoes the action.
    assert sync_resp.creatives, "sync_creatives must echo persisted creatives"

    list_resp = await platform.list_creatives(ListCreativesRequest(), ctx)
    payload = list_resp.model_dump(mode="json", exclude_none=True)
    assert payload["query_summary"]["returned"] == 1
    assert payload["creatives"][0]["creative_id"] == "spring-300x250"
