"""Smoke tests for the v3 reference seller broadening (translator pattern).

Covers:

* All 9 sales methods plus ``sync_accounts`` / ``list_accounts`` are
  present on the platform class (Protocol surface check).
* ``list_accounts`` projects ``billing_entity.bank`` out of every
  account on response (the headline 3.1-readiness claim).
* Translator pattern: the platform calls the upstream over HTTP for
  ad-ops data (products, orders, creatives, delivery, conversions)
  and uses local Postgres only for the commercial-identity layer.

Tests deliberately avoid spinning up a real Postgres or the JS mock-
server — Postgres I/O is mocked via SQLAlchemy session mocks, and
the upstream HTTP surface is mocked via :mod:`respx`. Storyboard CI
boots the real JS mock-server.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

# Add the example dir to sys.path so `src.*` imports resolve.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))


# ---------------------------------------------------------------------------
# Protocol surface — every sales-* method plus account ops are callable
# ---------------------------------------------------------------------------


def test_v3_reference_seller_exposes_full_sales_surface() -> None:
    """The seller declares both ``sales-non-guaranteed`` and
    ``sales-guaranteed`` — verify every method on the SalesPlatform
    Protocol (required + optional) plus the account ops are present
    on the class."""
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


def test_capabilities_claim_both_sales_specialisms() -> None:
    """Translator pattern surfaces both specialisms — the upstream
    supports ``delivery_type: guaranteed/non_guaranteed`` directly."""
    from src.platform import V3ReferenceSeller

    specialisms = set(V3ReferenceSeller.capabilities.specialisms)
    assert {"sales-non-guaranteed", "sales-guaranteed"} == specialisms


# ---------------------------------------------------------------------------
# list_accounts projection — bank details stripped on response
# ---------------------------------------------------------------------------


def test_list_accounts_projection_strips_bank_details() -> None:
    """The 3.1-readiness headline claim: any account run through
    ``project_account_for_response`` has ``billing_entity.bank``
    cleared.
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
    assert safe.billing_entity.legal_name == "Pinnacle Media LLC"
    payload = safe.model_dump(mode="json", exclude_none=True)
    assert "bank" not in payload["billing_entity"], payload


@pytest.mark.asyncio
async def test_list_accounts_runs_projection_on_every_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: drive ``V3ReferenceSeller.list_accounts`` against a
    mocked session whose row carries bank details and assert no
    response account leaks them.
    """
    import src.platform as platform_module
    from src.models import Account as AccountRow
    from src.models import BuyerAgent as BuyerAgentRow
    from src.platform import V3ReferenceSeller
    from src.upstream import MockUpstreamClient

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
        ext={"network_code": "net_premium_us", "advertiser_id": "adv_volta_motors"},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    ba_result = MagicMock()
    ba_result.scalar_one_or_none = MagicMock(return_value=buyer_agent_row)
    accounts_result = MagicMock()
    accounts_result.scalars = MagicMock(return_value=iter([account_row]))

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[ba_result, accounts_result])
    sessionmaker = MagicMock(return_value=session)

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())

    upstream = MockUpstreamClient(base_url="http://up", api_key="k")
    platform = V3ReferenceSeller(sessionmaker=sessionmaker, upstream=upstream)

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

    payload = resp.model_dump(mode="json", exclude_none=True)
    assert payload["accounts"], "expected at least one account in response"
    for acct in payload["accounts"]:
        assert (
            "billing_entity" in acct
        ), f"billing_entity missing from list_accounts response: {acct}"
        assert (
            "bank" not in acct["billing_entity"]
        ), f"bank details leaked on list_accounts response: {acct}"


# ---------------------------------------------------------------------------
# Translator-pattern HTTP plumbing — upstream is called over httpx
# ---------------------------------------------------------------------------


def _build_account_metadata(network_code: str = "net_premium_us") -> dict[str, Any]:
    return {
        "tenant_id": "t_acme",
        "buyer_agent_id": "ba_acme_signed",
        "account_id": "signed-buyer-main",
        "billing": "operator",
        "sandbox": False,
        "network_code": network_code,
        "advertiser_id": "adv_volta_motors",
    }


def _build_ctx() -> Any:
    """Build a RequestContext with an Account that carries upstream
    routing in metadata. Used by every translator-pattern test."""
    from adcp.decisioning import Account, RequestContext
    from adcp.decisioning.registry import BuyerAgent

    return RequestContext(
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
            metadata=_build_account_metadata(),
        ),
    )


def _platform_with_upstream(
    base_url: str = "http://up.test",
) -> Any:
    """Construct a V3ReferenceSeller with a fresh httpx-based upstream
    client. The respx fixture (per-test) intercepts all outbound calls.
    """
    from src.platform import V3ReferenceSeller
    from src.upstream import MockUpstreamClient

    upstream = MockUpstreamClient(base_url=base_url, api_key="test-key")
    sessionmaker = MagicMock()
    return V3ReferenceSeller(sessionmaker=sessionmaker, upstream=upstream)


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_get_products_translates_upstream_to_adcp(respx_mock: Any) -> None:
    """The platform calls ``GET /v1/products`` and projects the
    upstream's ``pricing.cpm`` + ``min_spend`` onto an AdCP
    :class:`CpmPricingOption`."""
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(
        return_value=httpx.Response(
            200,
            json={
                "products": [
                    {
                        "product_id": "sports_preroll_q2_guaranteed",
                        "name": "Sports Preroll Q2",
                        "delivery_type": "guaranteed",
                        "channel": "video",
                        "ad_unit_ids": ["au_us_video_preroll"],
                        "pricing": {
                            "model": "cpm",
                            "cpm": 35.0,
                            "currency": "USD",
                            "min_spend": 25_000,
                        },
                    }
                ]
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    resp = await platform.get_products(
        GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
    )
    assert len(resp.products) == 1
    p = resp.products[0]
    assert p.product_id == "sports_preroll_q2_guaranteed"
    assert (
        p.delivery_type.value if hasattr(p.delivery_type, "value") else p.delivery_type
    ) == "guaranteed"
    assert p.pricing_options is not None
    option = p.pricing_options[0]
    # Pydantic re-validates as PricingOption (RootModel union); the
    # CpmPricingOption fields land on .root.
    cpm = getattr(option, "root", option)
    assert cpm.pricing_model == "cpm"
    assert cpm.fixed_price == 35.0
    assert cpm.currency == "USD"
    assert cpm.min_spend_per_package == 25_000.0
    # Upstream call carried the X-Network-Code header.
    sent_request = respx_mock.calls.last.request
    assert sent_request.headers.get("X-Network-Code") == "net_premium_us"
    assert sent_request.headers.get("Authorization") == "Bearer test-key"


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_create_media_buy_returns_task_handoff_on_pending_approval(
    respx_mock: Any,
) -> None:
    """When the upstream returns ``pending_approval`` + ``approval_task_id``,
    the platform returns a :class:`TaskHandoff` so the framework
    surfaces the wire ``Submitted`` envelope to the buyer."""
    from src.upstream import MockUpstreamClient

    from adcp.decisioning.types import TaskHandoff
    from adcp.types import CreateMediaBuyRequest

    del MockUpstreamClient  # imported for side-effect docs
    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_q2_volta_launch",
                "name": "Volta Launch",
                "status": "pending_approval",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 25000.0,
                "approval_task_id": "task_abc",
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = CreateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "a" * 18,
            "brand": {"domain": "volta.example"},
            "total_budget": {"amount": 25000.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "sports_preroll_q2_guaranteed",
                    "format_ids": [
                        {
                            "agent_url": "https://reference.adcp.org",
                            "id": "video_16x9_30s",
                        }
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "sports_preroll_q2_guaranteed-cpm",
                }
            ],
        }
    )
    result = await platform.create_media_buy(req, ctx)
    # Translator's slow path — buyer sees Submitted envelope.
    assert isinstance(result, TaskHandoff), f"expected TaskHandoff, got {type(result)!r}"
    # The upstream call carried the buyer's idempotency_key as the
    # client_request_id — replay safety travels through the wire.
    sent = respx_mock.calls.last.request
    body = sent.read().decode("utf-8")
    assert "k_" + "a" * 18 in body
    assert "adv_volta_motors" in body


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_create_media_buy_sync_fast_path_when_upstream_already_approved(
    respx_mock: Any,
) -> None:
    """When the upstream returns ``approved`` directly (no
    approval_task_id), the platform short-circuits to the sync fast
    path and returns :class:`CreateMediaBuySuccessResponse` directly."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_fast_path",
                "name": "Fast Path",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = CreateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "b" * 18,
            "brand": {"domain": "fast.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "sports_preroll_q2_guaranteed",
                    "format_ids": [
                        {
                            "agent_url": "https://reference.adcp.org",
                            "id": "video_16x9_30s",
                        }
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "sports_preroll_q2_guaranteed-cpm",
                }
            ],
        }
    )
    result = await platform.create_media_buy(req, ctx)
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.media_buy_id == "ord_fast_path"


@pytest.mark.asyncio
async def test_update_media_buy_raises_unsupported_feature() -> None:
    """The mock upstream has no order-update endpoint. The platform
    raises spec-conformant ``UNSUPPORTED_FEATURE`` so buyers get a
    structured error instead of a 500."""
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    patch = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_test",
            "idempotency_key": "k_" + "u" * 18,
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_test", patch, ctx)
    assert excinfo.value.code == "UNSUPPORTED_FEATURE"


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_sync_creatives_uploads_each_creative_to_upstream(
    respx_mock: Any,
) -> None:
    """One ``POST /v1/creatives`` per creative, with the AdCP
    ``creative_id`` passed through as ``client_request_id``."""
    from adcp.types import SyncCreativesRequest

    route = respx_mock.post("/v1/creatives").mock(
        return_value=httpx.Response(
            201,
            json={
                "creative_id": "up_cr_1",
                "name": "Spring 300x250",
                "format_id": "display_300x250",
                "advertiser_id": "adv_volta_motors",
                "status": "active",
                "created_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = SyncCreativesRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "c" * 18,
            "creatives": [
                {
                    "creative_id": "spring-300x250",
                    "name": "Spring 300x250",
                    "format_id": {
                        "agent_url": "https://reference.adcp.org",
                        "id": "display_300x250",
                    },
                    "assets": {},
                }
            ],
        }
    )
    resp = await platform.sync_creatives(req, ctx)
    assert route.called
    assert len(resp.creatives) == 1
    assert resp.creatives[0].creative_id == "spring-300x250"
    sent = respx_mock.calls.last.request
    body = sent.read().decode("utf-8")
    # AdCP creative_id passed as client_request_id for upstream dedup.
    assert "spring-300x250" in body


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_get_media_buys_filters_by_advertiser_id(respx_mock: Any) -> None:
    """The upstream's ``GET /v1/orders`` is per-network; we filter to
    this AdCP account's ``advertiser_id`` so a misrouted buyer can't
    see another advertiser's orders on the same network."""
    from adcp.types import GetMediaBuysRequest

    respx_mock.get("/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "order_id": "ord_volta_1",
                        "name": "Volta",
                        "status": "delivering",
                        "advertiser_id": "adv_volta_motors",
                        "currency": "USD",
                        "budget": 25000.0,
                        "created_at": "2026-04-01T00:00:00Z",
                        "updated_at": "2026-04-01T00:00:00Z",
                    },
                    {
                        "order_id": "ord_other_1",
                        "name": "Other Advertiser",
                        "status": "delivering",
                        "advertiser_id": "adv_other",
                        "currency": "USD",
                        "budget": 5000.0,
                        "created_at": "2026-04-01T00:00:00Z",
                        "updated_at": "2026-04-01T00:00:00Z",
                    },
                ]
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    resp = await platform.get_media_buys(GetMediaBuysRequest(), ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    media_buys = payload["media_buys"]
    assert len(media_buys) == 1
    assert media_buys[0]["media_buy_id"] == "ord_volta_1"
    # delivering → active per the AdCP MediaBuyStatus mapping.
    assert media_buys[0]["status"] == "active"


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_get_media_buy_delivery_translates_upstream_report(
    respx_mock: Any,
) -> None:
    """``GET /v1/orders/{id}/delivery`` → AdCP delivery shape."""
    from adcp.types import GetMediaBuyDeliveryRequest

    respx_mock.get("/v1/orders/ord_1/delivery").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_1",
                "currency": "USD",
                "reporting_period": {
                    "start": "2026-04-01T00:00:00Z",
                    "end": "2026-04-30T23:59:59Z",
                },
                "totals": {
                    "impressions": 1_000_000,
                    "clicks": 5000,
                    "spend": 1234.56,
                },
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = GetMediaBuyDeliveryRequest.model_validate({"media_buy_ids": ["ord_1"]})
    resp = await platform.get_media_buy_delivery(req, ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    assert len(payload["media_buy_deliveries"]) == 1
    row = payload["media_buy_deliveries"][0]
    assert row["media_buy_id"] == "ord_1"
    assert row["totals"]["impressions"] == 1_000_000
    assert payload["currency"] == "USD"


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_provide_performance_feedback_posts_capi_conversion(
    respx_mock: Any,
) -> None:
    """Performance feedback projects to a ``POST /conversions`` (CAPI)
    call upstream — CAPI is the GAM-flavored equivalent of perf
    feedback."""
    from adcp.types import ProvidePerformanceFeedbackRequest

    route = respx_mock.post("/v1/orders/ord_1/conversions").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_1", "events_received": 1, "events_deduplicated": 0},
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = ProvidePerformanceFeedbackRequest.model_validate(
        {
            "idempotency_key": "k_" + "p" * 18,
            "media_buy_id": "ord_1",
            "metric_type": "overall_performance",
            "performance_index": 0.87,
            "measurement_period": {
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-30T23:59:59Z",
            },
        }
    )
    resp = await platform.provide_performance_feedback(req, ctx)
    assert route.called
    assert resp.success is True
    body = respx_mock.calls.last.request.read().decode("utf-8")
    assert "overall_performance" in body
    assert "0.87" in body


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_provide_performance_feedback_404_translates_to_media_buy_not_found(
    respx_mock: Any,
) -> None:
    """Upstream 404 on the order routes to the spec-conformant
    ``MEDIA_BUY_NOT_FOUND`` AdCP error code, not a generic 500."""
    from adcp.decisioning import AdcpError
    from adcp.types import ProvidePerformanceFeedbackRequest

    respx_mock.post("/v1/orders/ord_missing/conversions").mock(
        return_value=httpx.Response(404, json={"code": "ORDER_NOT_FOUND", "message": "missing"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = ProvidePerformanceFeedbackRequest.model_validate(
        {
            "idempotency_key": "k_" + "q" * 18,
            "media_buy_id": "ord_missing",
            "metric_type": "overall_performance",
            "performance_index": 0.5,
            "measurement_period": {
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-30T23:59:59Z",
            },
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.provide_performance_feedback(req, ctx)
    assert excinfo.value.code == "MEDIA_BUY_NOT_FOUND"


@pytest.mark.asyncio
@respx.mock(base_url="http://up.test")
async def test_list_creatives_filters_to_account_advertiser(respx_mock: Any) -> None:
    """``GET /v1/creatives`` returns the upstream catalog; we project
    onto AdCP shape and filter to this AdCP account's advertiser_id."""
    from adcp.types import ListCreativesRequest

    respx_mock.get("/v1/creatives").mock(
        return_value=httpx.Response(
            200,
            json={
                "creatives": [
                    {
                        "creative_id": "up_cr_1",
                        "name": "Volta Spring",
                        "format_id": "display_300x250",
                        "advertiser_id": "adv_volta_motors",
                        "status": "active",
                        "created_at": "2026-04-01T00:00:00Z",
                    },
                    {
                        "creative_id": "up_cr_2",
                        "name": "Other Brand",
                        "format_id": "display_300x250",
                        "advertiser_id": "adv_other",
                        "status": "active",
                        "created_at": "2026-04-01T00:00:00Z",
                    },
                ]
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    resp = await platform.list_creatives(ListCreativesRequest(), ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    assert payload["query_summary"]["total_matching"] == 1
    assert payload["creatives"][0]["creative_id"] == "up_cr_1"


@pytest.mark.asyncio
async def test_list_creative_formats_is_static_no_upstream_call() -> None:
    """The upstream has no formats endpoint — the platform serves a
    static catalog. The test asserts no upstream call is made."""
    from adcp.types import ListCreativeFormatsRequest

    with respx.mock(base_url="http://up.test") as respx_mock:
        platform = _platform_with_upstream()
        ctx = _build_ctx()
        resp = await platform.list_creative_formats(ListCreativeFormatsRequest(), ctx)
        assert len(resp.formats) >= 1
        assert respx_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_account_loader_rejects_account_missing_upstream_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account whose ``ext`` lacks ``network_code`` or
    ``advertiser_id`` is unusable for the translator pattern. The
    AccountStore rejects with ``INTERNAL_ERROR`` rather than dispatching
    to a method that would 500 on upstream call."""
    import src.platform as platform_module
    from src.models import Account as AccountRow
    from src.platform import _make_account_store

    from adcp.decisioning import AdcpError

    bad_row = AccountRow(
        id="a_bad",
        tenant_id="t_acme",
        buyer_agent_id="ba_x",
        account_id="bad-acct",
        name="Bad Account",
        status="active",
        billing="operator",
        sandbox=False,
        ext=None,
    )
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=bad_row)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=result)
    sessionmaker = MagicMock(return_value=session)

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())

    store = _make_account_store(sessionmaker)
    with pytest.raises(AdcpError) as excinfo:
        await store.resolve({"account_id": "bad-acct"})
    assert excinfo.value.code == "INTERNAL_ERROR"
