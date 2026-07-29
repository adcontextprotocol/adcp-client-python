"""Smoke tests for the v3 reference seller broadening (translator pattern).

Covers:

* All 9 sales methods plus ``sync_accounts`` / ``list_accounts`` are
  present on the platform class (Protocol surface check).
* ``list_accounts`` projects ``billing_entity.bank`` out of every
  account on response (the headline 3.1-readiness claim).
* Translator pattern: the platform calls the upstream over HTTP for
  ad-ops data (products, orders, creatives, delivery, conversions)
  and uses local Postgres only for the commercial-identity layer.
* Phase 3 wiring: the platform resolves its upstream client via the
  framework's :meth:`DecisioningPlatform.upstream_for`. Account is
  ``mode='mock'`` with ``metadata['mock_upstream_url']`` pointing at
  the respx-intercepted base URL; the SDK's
  :class:`UpstreamHttpClient` carries the auth header and projects
  upstream non-2xx onto :class:`AdcpError` codes.

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


def _canonical_create_request(model: Any, payload: dict[str, Any]) -> Any:
    """Translate the test's legacy-upstream selector into the public v7 input."""

    for package in payload.get("packages") or []:
        legacy_refs = package.pop("format_ids", None)
        if not legacy_refs:
            continue
        package["format_option_refs"] = [
            {
                "scope": "product",
                "format_option_id": f"reference_{package['product_id']}_0",
            }
        ]
    return model.model_validate(payload)


# ---------------------------------------------------------------------------
# Protocol surface — every sales-* method plus account ops are callable
# ---------------------------------------------------------------------------


def test_v3_reference_seller_exposes_full_sales_surface() -> None:
    """The seller declares both ``sales-non-guaranteed`` and
    ``sales-guaranteed`` — verify every method on the SalesPlatform
    Protocol (required + optional) is on the class, and that the
    account-op surfaces (``sync_accounts`` / ``list_accounts``) are
    exposed via the :class:`AccountStore` — the framework dispatches
    those tools through ``platform.accounts.upsert`` /
    ``platform.accounts.list``, not through methods on the platform."""
    from unittest.mock import MagicMock

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
        "list_creative_formats_legacy",
        "list_creatives",
    }

    for name in required_methods | optional_methods:
        assert hasattr(V3ReferenceSeller, name), f"V3ReferenceSeller missing {name}"
        attr = getattr(V3ReferenceSeller, name)
        assert callable(attr), f"V3ReferenceSeller.{name} is not callable"

    # Instance-level: account-op tools route through the AccountStore.
    instance = V3ReferenceSeller(sessionmaker=MagicMock(), upstream_api_key="t")
    assert callable(
        getattr(instance.accounts, "upsert", None)
    ), "AccountStore must expose upsert for sync_accounts tool advertising"
    assert callable(
        getattr(instance.accounts, "list", None)
    ), "AccountStore must expose list for list_accounts tool advertising"


def test_capabilities_claim_both_sales_specialisms() -> None:
    """Translator pattern surfaces both specialisms — the upstream
    supports ``delivery_type: guaranteed/non_guaranteed`` directly."""
    from src.platform import V3ReferenceSeller

    # ``specialisms`` is ``list[Specialism | str]`` (#479) — extract slug.
    specialisms = {
        s.value if hasattr(s, "value") else s for s in V3ReferenceSeller.capabilities.specialisms
    }
    assert {"sales-non-guaranteed", "sales-guaranteed"} == specialisms
    assert V3ReferenceSeller.capabilities.media_buy is not None
    assert V3ReferenceSeller.capabilities.media_buy.features is not None
    # Canonical models remain the implementation surface, while this
    # compatibility server explicitly negotiates the legacy 3.1 wire dialect.
    assert V3ReferenceSeller.capabilities.media_buy.features.canonical_creatives is False


def test_storyboard_legacy_format_converter_preserves_the_exact_tuple() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from src.platform import V3ReferenceSeller

    from adcp.canonical_formats import normalize_legacy_creative_request
    from adcp.decisioning import InMemoryTaskRegistry
    from adcp.decisioning.handler import PlatformHandler

    legacy = {
        "agent_url": "https://reference.adcp.org",
        "id": "display_300x250",
    }
    sources: list[Any] = []
    normalized = normalize_legacy_creative_request(
        {"creatives": [{"creative_id": "c1", "format_id": legacy}]},
        legacy_format_converter=V3ReferenceSeller.legacy_format_converter,
        projection_sources=sources,
    )

    assert normalized["creatives"][0]["format_kind"] == "image"
    declaration = sources[0]["format_options"][0]
    assert declaration.legacy_format_refs[0].model_dump(mode="json") == legacy

    platform = _platform_with_upstream()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(
            platform,
            executor=executor,
            registry=InMemoryTaskRegistry(),
        )
    assert handler.legacy_format_converter is platform.legacy_format_converter


def test_platform_declares_upstream_url() -> None:
    """Phase 3 — the platform declares ``upstream_url`` so the
    framework's ``upstream_for`` can route ``mode='live'`` /
    ``'sandbox'`` accounts. The reference seller is mock-mode by
    design, so the value is a placeholder adopters replace; what
    matters is that the attribute exists for the migration template."""
    from src.platform import V3ReferenceSeller

    assert V3ReferenceSeller.upstream_url is not None
    assert isinstance(V3ReferenceSeller.upstream_url, str)
    assert V3ReferenceSeller.upstream_url.startswith(("http://", "https://"))


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
async def test_account_store_upsert_creates_then_updates_and_strips_bank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: drive ``AccountStore.upsert`` with a buyer-supplied
    :class:`AccountReference` carrying full bank details, then project
    the returned rows through the framework's
    :func:`to_wire_sync_accounts_row`. Bank details MUST round-trip
    into the persisted row but MUST NOT appear on the wire-projected
    response."""
    from src.models import BuyerAgent as BuyerAgentRow
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import AuthInfo
    from adcp.decisioning.account_projection import to_wire_sync_accounts_row
    from adcp.decisioning.accounts import ResolveContext
    from adcp.types import SyncAccountsRequest
    from src import platform as platform_module

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

    ba_result = MagicMock()
    ba_result.scalar_one_or_none = MagicMock(return_value=buyer_agent_row)
    # Existing-account probe — returns None to take the ``created`` path.
    missing_result = MagicMock()
    missing_result.scalar_one_or_none = MagicMock(return_value=None)

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=session)
    session.execute = AsyncMock(side_effect=[ba_result, missing_result])
    session.add = MagicMock()
    sessionmaker = MagicMock(return_value=session)

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())

    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key="test-key",
        mock_upstream_url="http://up.test",
    )

    ctx = ResolveContext(
        auth_info=AuthInfo(kind="anonymous", principal="https://signed-buyer.example/"),
        tool_name="sync_accounts",
    )
    # Use the parsed SyncAccountsRequest.accounts[] shape — the framework
    # passes these typed entries straight into upsert(refs, ctx).
    req = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "k_" + "z" * 18,
            "accounts": [
                {
                    "brand": {"domain": "acme-corp.com"},
                    "operator": "pinnacle-media.com",
                    "billing": "agent",
                    "billing_entity": {
                        "legal_name": "Pinnacle Media LLC",
                        "tax_id": "12-3456789",
                        "address": {
                            "street": "123 Main St",
                            "city": "Berlin",
                            "postal_code": "10117",
                            "country": "DE",
                        },
                        "contacts": [
                            {"role": "billing", "name": "AP", "email": "ap@pinnacle.example"}
                        ],
                        "bank": bank_block,
                    },
                }
            ],
        }
    )

    rows = await platform.accounts.upsert(list(req.accounts), ctx=ctx)
    assert len(rows) == 1
    assert rows[0].action == "created"

    # Persisted row carried the full bank block (write side).
    added_row = session.add.call_args.args[0]
    assert added_row.billing_entity["bank"] == bank_block

    # Wire-projected row strips bank (read side).
    wire = to_wire_sync_accounts_row(rows[0])
    assert wire["billing_entity"]["legal_name"] == "Pinnacle Media LLC"
    assert (
        "bank" not in wire["billing_entity"]
    ), f"bank details leaked through to_wire_sync_accounts_row: {wire}"


@pytest.mark.asyncio
async def test_account_store_list_strips_bank_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: drive ``AccountStore.list`` against a mocked session
    whose row carries bank details, project through the framework's
    :func:`to_wire_account`, and assert no response account leaks the
    bank block. Mirrors how the dispatch shim wraps the upstream's
    response.
    """
    from src.models import Account as AccountRow
    from src.models import BuyerAgent as BuyerAgentRow
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import AuthInfo
    from adcp.decisioning.account_projection import to_wire_account
    from adcp.decisioning.accounts import ResolveContext
    from src import platform as platform_module

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

    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key="test-key",
        mock_upstream_url="http://up.test",
    )

    ctx = ResolveContext(
        auth_info=AuthInfo(kind="anonymous", principal="https://signed-buyer.example/"),
        tool_name="list_accounts",
    )
    accounts = await platform.accounts.list({}, ctx=ctx)
    assert len(accounts) == 1
    wire = to_wire_account(accounts[0])
    assert wire["billing_entity"]["legal_name"] == "Pinnacle Media LLC"
    assert wire["billing_entity"]["tax_id"] == "12-3456789"
    assert (
        "bank" not in wire["billing_entity"]
    ), f"bank details leaked through to_wire_account projection: {wire}"


@pytest.mark.asyncio
async def test_account_store_explicit_id_is_bound_to_authenticated_buyer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account id owned by another buyer is indistinguishable from missing."""
    from adcp.decisioning import AdcpError, AuthInfo
    from src import platform as platform_module

    buyer_result = MagicMock()
    buyer_result.scalar_one_or_none.return_value = MagicMock(id="ba_caller")
    missing_account_result = MagicMock()
    missing_account_result.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[buyer_result, missing_account_result])

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())
    store = platform_module._make_account_store(  # noqa: SLF001 - example integration test
        MagicMock(return_value=session), mock_upstream_url="http://up.test"
    )
    auth = AuthInfo(kind="anonymous", principal="https://caller.example/")
    with pytest.raises(AdcpError) as excinfo:
        await store.resolve({"account_id": "foreign-account"}, auth)
    assert excinfo.value.code == "ACCOUNT_NOT_FOUND"
    account_query = session.execute.await_args_list[1].args[0]
    compiled = str(account_query.compile(compile_kwargs={"literal_binds": True}))
    assert "accounts.buyer_agent_id = 'ba_caller'" in compiled
    assert "accounts.account_id = 'foreign-account'" in compiled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account_ref",
    [
        {"account_id": "account-1"},
        {"brand": {"domain": "brand.example"}, "operator": "operator.example"},
    ],
)
async def test_account_store_without_principal_uses_correctable_auth_missing(
    monkeypatch: pytest.MonkeyPatch,
    account_ref: dict[str, Any],
) -> None:
    from adcp.decisioning import AdcpError
    from src import platform as platform_module

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())
    store = platform_module._make_account_store(  # noqa: SLF001 - example integration test
        MagicMock(), mock_upstream_url="http://up.test"
    )

    with pytest.raises(AdcpError) as excinfo:
        await store.resolve(account_ref, None)

    assert excinfo.value.code == "AUTH_MISSING"
    assert excinfo.value.recovery == "correctable"


@pytest.mark.asyncio
async def test_account_store_upsert_cannot_overwrite_another_buyers_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.models import Account as AccountRow

    from adcp.decisioning import AuthInfo
    from adcp.decisioning.accounts import ResolveContext
    from adcp.types import SyncAccountsRequest
    from src import platform as platform_module

    buyer_result = MagicMock()
    buyer_result.scalar_one_or_none.return_value = MagicMock(id="ba_caller")
    foreign = AccountRow(
        id="a_foreign",
        tenant_id="t_acme",
        buyer_agent_id="ba_other",
        account_id="acme.example::operator.example",
        name="Foreign",
        status="active",
        sandbox=False,
    )
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = foreign
    missing_result = MagicMock()
    missing_result.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=session)
    session.execute = AsyncMock(side_effect=[buyer_result, missing_result, existing_result])

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())
    platform = platform_module.V3ReferenceSeller(
        sessionmaker=MagicMock(return_value=session), upstream_api_key="test-key"
    )
    req = SyncAccountsRequest.model_validate(
        {
            "idempotency_key": "k_" + "a" * 18,
            "accounts": [
                {
                    "brand": {"domain": "one.example"},
                    "operator": "operator.example",
                    "billing": "operator",
                },
                {
                    "brand": {"domain": "acme.example"},
                    "operator": "operator.example",
                    "billing": "operator",
                },
            ],
        }
    )
    ctx = ResolveContext(
        auth_info=AuthInfo(kind="anonymous", principal="https://caller.example/"),
        tool_name="sync_accounts",
    )
    rows = await platform.accounts.upsert(list(req.accounts), ctx)
    assert [row.action for row in rows] == ["created", "failed"]
    assert rows[1].status == "rejected"
    assert rows[1].errors == [
        {
            "code": "ACCOUNT_NOT_FOUND",
            "message": "Account is not visible to the authenticated buyer agent.",
            "recovery": "terminal",
            "field": "accounts[1]",
        }
    ]
    added = session.add.call_args.args[0]
    assert added.account_id == "one.example::operator.example"
    assert foreign.buyer_agent_id == "ba_other"


# ---------------------------------------------------------------------------
# Translator-pattern HTTP plumbing — upstream is called via upstream_for()
# ---------------------------------------------------------------------------


_RESPX_BASE_URL = "http://up.test"


def _build_account_metadata(network_code: str = "net_premium_us") -> dict[str, Any]:
    return {
        "tenant_id": "t_acme",
        "buyer_agent_id": "ba_acme_signed",
        "account_id": "signed-buyer-main",
        "billing": "operator",
        "sandbox": False,
        "network_code": network_code,
        "advertiser_id": "adv_volta_motors",
        # Phase 2 framework-reserved key — read by ``upstream_for`` to
        # point the pooled UpstreamHttpClient at the respx fixture.
        "mock_upstream_url": _RESPX_BASE_URL,
    }


def _build_ctx() -> Any:
    """Build a RequestContext with a ``mode='mock'`` Account whose
    metadata carries the upstream routing (``network_code`` /
    ``advertiser_id``) and the framework-reserved ``mock_upstream_url``.

    The framework's ``upstream_for(ctx)`` reads ``mock_upstream_url``
    to build a pooled :class:`UpstreamHttpClient` pointed at the
    respx-intercepted fixture URL.
    """
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
            mode="mock",
            metadata=_build_account_metadata(),
        ),
    )


def _platform_with_upstream() -> Any:
    """Construct a :class:`V3ReferenceSeller` for translator-pattern
    tests. The platform builds its :class:`UpstreamHttpClient` lazily
    via :meth:`upstream_for`; the per-test respx fixture intercepts
    every outbound HTTP call against ``http://up.test``.
    """
    from src.platform import V3ReferenceSeller

    sessionmaker = MagicMock()
    return V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key="test-key",
    )


def _seed_owned_buy(
    platform: Any,
    ctx: Any,
    order_id: str,
    *,
    packages: dict[str, Any] | None = None,
) -> None:
    """Seed the reference seller's account-owned shadow state.

    Real requests establish this binding through ``create_media_buy``;
    translator unit tests that start from an existing upstream order must
    declare the same ownership explicitly.
    """
    buy_key = platform._buy_key(ctx, order_id)  # noqa: SLF001 - ownership test helper
    platform._buy_state[buy_key] = {  # noqa: SLF001 - ownership test helper
        "packages": packages or {},
        "canceled": False,
        "paused": False,
    }


_LINE_ITEM_COUNTER = {"n": 0}


def _mock_add_line_item_route(respx_mock: Any, order_id: str) -> None:
    """Per-test helper: mock ``POST /v1/orders/{order_id}/lineitems`` to
    return a fresh line_item_id on each call. Mirrors the mock-server's
    behavior (each POST returns a distinct ``line_item_id``)."""
    import re

    def _handler(request: httpx.Request) -> httpx.Response:
        _LINE_ITEM_COUNTER["n"] += 1
        return httpx.Response(
            201,
            json={
                "line_item_id": f"li_test_{_LINE_ITEM_COUNTER['n']:04d}",
                "order_id": order_id,
                "status": "pending_creatives",
                "creative_ids": [],
            },
        )

    respx_mock.post(re.compile(rf"/v1/orders/{re.escape(order_id)}/lineitems$")).mock(
        side_effect=_handler
    )


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_products_translates_upstream_to_adcp(respx_mock: Any) -> None:
    """The platform calls ``GET /v1/products`` and projects the
    upstream's ``pricing.cpm`` + ``min_spend`` onto an AdCP
    :class:`CpmPricingOption`."""
    from adcp.canonical_formats import project_canonical_response_to_legacy
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
    product_payload = p.model_dump(mode="json", exclude_none=True)
    assert "format_ids" not in product_payload
    assert product_payload["format_options"][0]["format_kind"] == "video_hosted"
    assert [ref.id for ref in p.format_options[0].legacy_format_refs] == ["video_16x9_30s"]
    legacy = project_canonical_response_to_legacy(resp)
    assert legacy["products"][0]["format_ids"] == [
        {
            "agent_url": "https://reference.adcp.org",
            "id": "video_16x9_30s",
        }
    ]
    # The SDK's UpstreamHttpClient carried StaticBearer for auth;
    # the upstream helper added the X-Network-Code per-call header.
    sent_request = respx_mock.calls.last.request
    assert sent_request.headers.get("X-Network-Code") == "net_premium_us"
    assert sent_request.headers.get("Authorization") == "Bearer test-key"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_sync_polls_to_success_on_pending_approval(
    respx_mock: Any,
) -> None:
    """When the upstream returns ``pending_approval`` + ``approval_task_id``,
    the platform sync-polls until the approval task completes and returns
    the full :class:`CreateMediaBuySuccessResponse` with ``media_buy_id``.
    AdCP storyboards expect synchronous create — production adopters with
    slow real-world approvals swap to ``ctx.handoff_to_task`` (see the
    docstring in ``platform.create_media_buy``)."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

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
    # Approval task completes on the first poll.
    respx_mock.get("/v1/tasks/task_abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "task_abc",
                "order_id": "ord_q2_volta_launch",
                "status": "completed",
                "result": {"outcome": "approved"},
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    # Re-fetch after polling completes.
    respx_mock.get("/v1/orders/ord_q2_volta_launch").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_q2_volta_launch",
                "name": "Volta Launch",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 25000.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    _mock_add_line_item_route(respx_mock, "ord_q2_volta_launch")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
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
        },
    )
    result = await platform.create_media_buy(req, ctx)
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.media_buy_id == "ord_q2_volta_launch"
    # The upstream call carried the buyer's idempotency_key as the
    # client_request_id — replay safety travels through the wire.
    sent = respx_mock.calls[0].request
    body = sent.read().decode("utf-8")
    assert "k_" + "a" * 18 in body
    assert "adv_volta_motors" in body


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
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
    _mock_add_line_item_route(respx_mock, "ord_fast_path")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
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
        },
    )
    result = await platform.create_media_buy(req, ctx)
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.media_buy_id == "ord_fast_path"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_echoes_packages_with_seller_minted_ids(
    respx_mock: Any,
) -> None:
    """Confirmed-package response shape: seller mints a ``package_id``
    per requested package and echoes the spec-marked echo fields so
    buyers can chain off the id and verify targeting / measurement-terms
    persistence. Without these the AdCP storyboard suite's
    ``inventory_list_targeting`` / ``invalid_transitions`` /
    ``creative_fate_after_cancellation`` scenarios cannot capture
    ``packages[0].package_id`` to drive their follow-up probes."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_lists",
                "name": "Lists Buy",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    _mock_add_line_item_route(respx_mock, "ord_lists")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "l" * 18,
            "brand": {"domain": "lists.example"},
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
                    "context": {"buyer_ref": "line-001"},
                    "targeting_overlay": {
                        "property_list": {
                            "agent_url": "https://reference.adcp.org",
                            "list_id": "prop_premium_news",
                        },
                        "collection_list": {
                            "agent_url": "https://reference.adcp.org",
                            "list_id": "coll_evening_news",
                        },
                    },
                    "creative_assignments": [{"creative_id": "cr_demo_v1"}],
                }
            ],
        },
    )
    result = await platform.create_media_buy(req, ctx)
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.packages is not None
    assert len(result.packages) == 1
    pkg = result.packages[0]
    # package_id is the upstream-issued line_item_id (li_test_NNNN from the
    # _mock_add_line_item_route fixture).
    assert pkg.package_id is not None and pkg.package_id.startswith("li_test_")
    assert pkg.product_id == "sports_preroll_q2_guaranteed"
    assert pkg.context is not None
    assert pkg.context.model_extra["buyer_ref"] == "line-001"
    # Spec-marked echo: list targeting fields persist on the confirmed package.
    assert pkg.targeting_overlay is not None
    assert pkg.targeting_overlay.property_list is not None
    assert pkg.targeting_overlay.property_list.list_id == "prop_premium_news"
    assert pkg.targeting_overlay.collection_list is not None
    assert pkg.targeting_overlay.collection_list.list_id == "coll_evening_news"
    # Buyer supplied a creative_assignment — status reflects upstream-derived
    # status ("approved" → "pending_start"), not pending_creatives.
    assert result.status == "completed"
    assert result.media_buy_status is not None
    assert result.media_buy_status.value == "pending_start"

    # The framework's response scrubber must retain nested model identity.
    # The legacy 3.1 response projector dispatches on Package models; an
    # unchecked model_copy(update=...) turns these into dicts and leaves the
    # canonical format_option_refs shape on the legacy wire response.
    from adcp.decisioning.account_projection import strip_credentials_from_wire_result

    scrubbed = strip_credentials_from_wire_result("create_media_buy", result)
    assert isinstance(scrubbed, CreateMediaBuySuccessResponse)
    assert type(scrubbed.packages[0]) is type(result.packages[0])
    assert scrubbed.packages[0].targeting_overlay is not None
    assert scrubbed.packages[0].targeting_overlay.property_list is not None


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_no_creatives_returns_pending_creatives_status(
    respx_mock: Any,
) -> None:
    """When the buyer supplies no ``creatives`` and no
    ``creative_assignments`` on any package, the seller surfaces
    ``status='pending_creatives'`` so the buyer's next step is
    ``sync_creatives``. AdCP storyboard
    ``pending_creatives_to_start/create_without_creatives`` gates on
    this transition."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_pending_creatives",
                "name": "Pending Creatives Buy",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    _mock_add_line_item_route(respx_mock, "ord_pending_creatives")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "p" * 18,
            "brand": {"domain": "pending.example"},
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
        },
    )
    result = await platform.create_media_buy(req, ctx)
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.status == "completed"
    assert result.media_buy_status is not None
    assert result.media_buy_status.value == "pending_creatives"
    assert result.packages is not None
    assert result.packages[0].package_id is not None
    assert result.packages[0].package_id.startswith("li_test_")


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_context_survives_get_media_buys(respx_mock: Any) -> None:
    """Persist media-buy and package context for rc4 storyboard readbacks."""
    from adcp.types import CreateMediaBuyRequest, GetMediaBuysRequest

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_context",
                "name": "Context Buy",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    _mock_add_line_item_route(respx_mock, "ord_context")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "context": {"correlation_id": "media_buy_seller--create_media_buy"},
            "idempotency_key": "k_" + "c" * 18,
            "brand": {"domain": "context.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "sports_preroll_q2_guaranteed",
                    "format_ids": [
                        {"agent_url": "https://reference.adcp.org", "id": "video_16x9_30s"}
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "sports_preroll_q2_guaranteed-cpm",
                    "context": {"buyer_ref": "pending-creatives-line-001"},
                }
            ],
        },
    )
    create_resp = await platform.create_media_buy(req, ctx)
    package_id = create_resp.packages[0].package_id

    respx_mock.get("/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "order_id": "ord_context",
                        "name": "Context Buy",
                        "status": "delivering",
                        "advertiser_id": "adv_volta_motors",
                        "currency": "USD",
                        "budget": 100.0,
                        "created_at": "2026-04-01T00:00:00Z",
                        "updated_at": "2026-04-01T00:00:00Z",
                    }
                ]
            },
        )
    )
    respx_mock.get("/v1/orders/ord_context/lineitems").mock(
        return_value=httpx.Response(200, json={"line_items": [{"line_item_id": package_id}]})
    )

    get_resp = await platform.get_media_buys(
        GetMediaBuysRequest.model_validate({"media_buy_ids": ["ord_context"]}), ctx
    )
    payload = get_resp.model_dump(mode="json", exclude_none=True)
    media_buy = payload["media_buys"][0]
    assert media_buy["context"]["correlation_id"] == "media_buy_seller--create_media_buy"
    assert media_buy["packages"][0]["context"]["buyer_ref"] == "pending-creatives-line-001"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_cancel_marks_local_state(respx_mock: Any) -> None:
    """A buy-level ``canceled: true`` patch sets the shadow-store flag
    and the response surfaces ``status='canceled'``. Re-cancel raises
    ``NOT_CANCELLABLE``."""
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest, UpdateMediaBuySuccessResponse

    respx_mock.get("/v1/orders/ord_test").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_test",
                "status": "active",
                "advertiser_id": "adv_volta_motors",
            },
        )
    )
    respx_mock.get("/v1/orders/ord_test/lineitems").mock(
        return_value=httpx.Response(200, json={"line_items": []})
    )

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_test")
    patch = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_test",
            "idempotency_key": "k_" + "u" * 18,
            "canceled": True,
            "cancellation_reason": "buyer changed mind",
        }
    )
    result = await platform.update_media_buy("ord_test", patch, ctx)
    assert isinstance(result, UpdateMediaBuySuccessResponse)
    assert result.status == "completed"
    assert result.media_buy_status is not None
    assert result.media_buy_status.value == "canceled"
    assert result.revision == 1

    # Re-cancel — irreversible.
    patch2 = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_test",
            "idempotency_key": "k_" + "v" * 18,
            "canceled": True,
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_test", patch2, ctx)
    assert excinfo.value.code == "NOT_CANCELLABLE"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_unknown_media_buy_id_raises_not_found(
    respx_mock: Any,
) -> None:
    """An unknown ``media_buy_id`` resolves to ``MEDIA_BUY_NOT_FOUND``,
    not ``UNSUPPORTED_FEATURE`` — the storyboard's negative-path probe
    gates on this distinction."""
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    respx_mock.get("/v1/orders/missing").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_test", packages={"li_known": {}})
    patch = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "missing",
            "idempotency_key": "k_" + "n" * 18,
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("missing", patch, ctx)
    assert excinfo.value.code == "MEDIA_BUY_NOT_FOUND"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_unknown_package_id_raises_not_found(
    respx_mock: Any,
) -> None:
    """A package_id not on the upstream order resolves to
    ``PACKAGE_NOT_FOUND``, not ``UNSUPPORTED_FEATURE``."""
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    respx_mock.get("/v1/orders/ord_test").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_test",
                "status": "active",
                "advertiser_id": "adv_volta_motors",
            },
        )
    )
    respx_mock.get("/v1/orders/ord_test/lineitems").mock(
        return_value=httpx.Response(
            200,
            json={"line_items": [{"line_item_id": "li_known"}]},
        )
    )

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_test", packages={"li_known": {}})
    patch = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_test",
            "idempotency_key": "k_" + "p" * 18,
            "packages": [{"package_id": "li_unknown", "paused": True}],
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_test", patch, ctx)
    assert excinfo.value.code == "PACKAGE_NOT_FOUND"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_affected_packages_echo_list_agent_urls(
    respx_mock: Any,
) -> None:
    """Pydantic AnyUrl normalizes host-only URLs with a trailing slash;
    package echoes keep the buyer's list-agent URL stable."""
    from adcp.types import UpdateMediaBuyRequest, UpdateMediaBuySuccessResponse

    respx_mock.get("/v1/orders/ord_test").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_test",
                "status": "delivering",
                "advertiser_id": "adv_volta_motors",
            },
        )
    )
    respx_mock.get("/v1/orders/ord_test/lineitems").mock(
        return_value=httpx.Response(
            200,
            json={"line_items": [{"line_item_id": "li_known"}]},
        )
    )

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(
        platform,
        ctx,
        "ord_test",
        packages={"li_known": {"canceled": False, "paused": False}},
    )
    patch = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_test",
            "idempotency_key": "k_" + "l" * 18,
            "packages": [
                {
                    "package_id": "li_known",
                    "targeting_overlay": {
                        "property_list": {
                            "agent_url": "https://governance.pinnacle-agency.example",
                            "list_id": "prop_news",
                        },
                        "collection_list": {
                            "agent_url": "https://governance.pinnacle-agency.example",
                            "list_id": "coll_news",
                        },
                    },
                }
            ],
        }
    )

    result = await platform.update_media_buy("ord_test", patch, ctx)
    assert isinstance(result, UpdateMediaBuySuccessResponse)
    from adcp.decisioning.account_projection import strip_credentials_from_wire_result

    scrubbed = strip_credentials_from_wire_result("update_media_buy", result)
    assert isinstance(scrubbed, UpdateMediaBuySuccessResponse)
    assert type(scrubbed.affected_packages[0]) is type(result.affected_packages[0])
    payload = scrubbed.model_dump(mode="json", exclude_none=True)
    targeting = payload["affected_packages"][0]["targeting_overlay"]
    assert targeting["property_list"]["agent_url"] == ("https://governance.pinnacle-agency.example")
    assert targeting["collection_list"]["agent_url"] == (
        "https://governance.pinnacle-agency.example"
    )


@pytest.mark.asyncio
async def test_create_media_buy_aggressive_terms_raises_terms_rejected() -> None:
    """``measurement_terms.billing_measurement.max_variance_percent == 0``
    is unworkable for any real measurement vendor; the platform rejects
    up front with ``TERMS_REJECTED``. The aggressive-terms storyboard
    gates on this specific code."""
    from adcp.decisioning import AdcpError
    from adcp.types import CreateMediaBuyRequest

    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "brand": {"domain": "acmeoutdoor.example"},
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "t" * 18,
            "start_time": "2026-05-01T00:00:00Z",
            "end_time": "2026-05-31T23:59:59Z",
            "packages": [
                {
                    "product_id": "prod_test",
                    "budget": 25000,
                    "pricing_option_id": "po_test",
                    "measurement_terms": {
                        "billing_measurement": {
                            "vendor": {"domain": "videoamp.example"},
                            "measurement_window": "c30",
                            "max_variance_percent": 0,
                        },
                        "makegood_policy": {"available_remedies": ["credit"]},
                    },
                }
            ],
        },
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.create_media_buy(req, ctx)
    assert excinfo.value.code == "TERMS_REJECTED"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
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
                    "format_kind": "image",
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
@respx.mock(base_url=_RESPX_BASE_URL)
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
    # get_media_buys reads line_items for each matched order to project
    # per-package state. Only ord_volta_1 passes the advertiser_id filter.
    respx_mock.get("/v1/orders/ord_volta_1/lineitems").mock(
        return_value=httpx.Response(200, json={"line_items": []})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_volta_1")
    resp = await platform.get_media_buys(GetMediaBuysRequest(), ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    media_buys = payload["media_buys"]
    assert len(media_buys) == 1
    assert media_buys[0]["media_buy_id"] == "ord_volta_1"
    # delivering → active per the AdCP MediaBuyStatus mapping.
    assert media_buys[0]["status"] == "active"
    assert "pause" in media_buys[0]["valid_actions"]
    assert payload["sandbox"] is True


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
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
    # The platform double-fetches the order to project the right
    # AdCP MediaBuyStatus (DeliveryReport doesn't carry status).
    respx_mock.get("/v1/orders/ord_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_1",
                "name": "Volta",
                "status": "delivering",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 25000.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_1")
    req = GetMediaBuyDeliveryRequest.model_validate({"media_buy_ids": ["ord_1"]})
    resp = await platform.get_media_buy_delivery(req, ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    assert len(payload["media_buy_deliveries"]) == 1
    row = payload["media_buy_deliveries"][0]
    assert row["media_buy_id"] == "ord_1"
    assert row["status"] == "active"
    assert row["totals"]["impressions"] == 1_000_000
    assert payload["currency"] == "USD"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
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
    respx_mock.get("/v1/orders/ord_1").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_1", "advertiser_id": "adv_volta_motors"},
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_1")
    req = ProvidePerformanceFeedbackRequest.model_validate(
        {
            "idempotency_key": "k_" + "p" * 18,
            "media_buy_id": "ord_1",
            "metric_type": "conversion_rate",
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
    assert "conversion_rate" in body
    assert "0.87" in body


@pytest.mark.asyncio
async def test_provide_performance_feedback_rejects_non_conversion_rate_metric() -> None:
    """The CAPI mapping only round-trips ``conversion_rate`` cleanly.
    Other AdCP metric_types raise ``INVALID_REQUEST`` rather than
    fabricating a synthetic event upstream. No upstream call is made
    (respx not wired — any HTTP attempt would surface as a different
    failure)."""
    from adcp.decisioning import AdcpError
    from adcp.types import ProvidePerformanceFeedbackRequest

    with respx.mock(base_url=_RESPX_BASE_URL) as respx_mock:
        platform = _platform_with_upstream()
        ctx = _build_ctx()
        req = ProvidePerformanceFeedbackRequest.model_validate(
            {
                "idempotency_key": "k_" + "r" * 18,
                "media_buy_id": "ord_1",
                "metric_type": "overall_performance",
                "performance_index": 0.87,
                "measurement_period": {
                    "start": "2026-04-01T00:00:00Z",
                    "end": "2026-04-30T23:59:59Z",
                },
            }
        )
        with pytest.raises(AdcpError) as excinfo:
            await platform.provide_performance_feedback(req, ctx)
        assert excinfo.value.code == "INVALID_REQUEST"
        assert excinfo.value.field == "metric_type"
        assert respx_mock.calls.call_count == 0


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_provide_performance_feedback_404_translates_to_media_buy_not_found(
    respx_mock: Any,
) -> None:
    """Upstream 404 on the order routes to the spec-conformant
    ``MEDIA_BUY_NOT_FOUND`` AdCP error code, not a generic 500. The
    SDK's :class:`UpstreamHttpClient` projects POST 404 → the
    default ``not_found_code`` (``MEDIA_BUY_NOT_FOUND``)."""
    from adcp.decisioning import AdcpError
    from adcp.types import ProvidePerformanceFeedbackRequest

    respx_mock.get("/v1/orders/ord_missing").mock(
        return_value=httpx.Response(404, json={"code": "ORDER_NOT_FOUND", "message": "missing"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = ProvidePerformanceFeedbackRequest.model_validate(
        {
            "idempotency_key": "k_" + "q" * 18,
            "media_buy_id": "ord_missing",
            "metric_type": "conversion_rate",
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
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_list_creatives_filters_to_account_advertiser(respx_mock: Any) -> None:
    """``GET /v1/creatives`` returns the upstream catalog; we project
    onto AdCP shape and filter to this AdCP account's advertiser_id."""
    from adcp.canonical_formats import (
        migrated_format_option_id,
        project_canonical_response_to_legacy,
    )
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
    assert resp["query_summary"]["total_matching"] == 1
    creative = resp["creatives"][0]
    assert creative["creative_id"] == "up_cr_1"
    assert "format_id" not in creative
    assert creative["format_option_ref"] == {
        "scope": "publisher",
        "publisher_domain": "reference.adcp.org",
        "format_option_id": migrated_format_option_id(
            {
                "agent_url": "https://reference.adcp.org",
                "id": "display_300x250",
            }
        ),
    }

    # The private declaration sidecar is retained only in-process. At the
    # server boundary it authorizes an exact downgrade; no reverse guessing is
    # needed after the upstream's bare ID is paired with this seller's owner.
    legacy = project_canonical_response_to_legacy(resp)
    assert legacy["creatives"][0]["format_id"] == {
        "agent_url": "https://reference.adcp.org",
        "id": "display_300x250",
    }
    assert "format_kind" not in legacy["creatives"][0]
    assert "format_option_ref" not in legacy["creatives"][0]


@pytest.mark.asyncio
async def test_list_creative_formats_is_static_no_upstream_call() -> None:
    """The upstream has no formats endpoint — the platform serves a
    static catalog. The test asserts no upstream call is made."""
    from adcp.types import LegacyListCreativeFormatsRequest

    with respx.mock(base_url=_RESPX_BASE_URL) as respx_mock:
        platform = _platform_with_upstream()
        ctx = _build_ctx()
        resp = await platform.list_creative_formats_legacy(LegacyListCreativeFormatsRequest(), ctx)
        assert len(resp.formats) >= 1
        assert respx_mock.calls.call_count == 0


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_rejects_foreign_advertiser_order(respx_mock: Any) -> None:
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    respx_mock.get("/v1/orders/ord_foreign").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_foreign", "advertiser_id": "adv_other"},
        )
    )
    platform = _platform_with_upstream()
    req = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_foreign",
            "idempotency_key": "k_" + "u" * 18,
            "paused": True,
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_foreign", req, _build_ctx())
    assert excinfo.value.code == "MEDIA_BUY_NOT_FOUND"
    assert not any(call.request.url.path.endswith("/lineitems") for call in respx_mock.calls)


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_update_media_buy_rejects_other_account_with_shared_advertiser(
    respx_mock: Any,
) -> None:
    """Advertiser identity alone is not an account ownership boundary."""
    from dataclasses import replace

    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    respx_mock.get("/v1/orders/ord_shared").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_shared", "advertiser_id": "adv_volta_motors"},
        )
    )
    platform = _platform_with_upstream()
    owner_ctx = _build_ctx()
    _seed_owned_buy(platform, owner_ctx, "ord_shared")
    assert owner_ctx.account is not None
    other_ctx = replace(owner_ctx, account=replace(owner_ctx.account, id="a_other_buyer"))
    req = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "other-buyer"},
            "media_buy_id": "ord_shared",
            "idempotency_key": "k_" + "o" * 18,
            "paused": True,
        }
    )

    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_shared", req, other_ctx)
    assert excinfo.value.code == "MEDIA_BUY_NOT_FOUND"
    assert not any(call.request.url.path.endswith("/lineitems") for call in respx_mock.calls)


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_foreign_and_missing_media_buy_errors_are_identical(respx_mock: Any) -> None:
    """Ownership hiding covers the complete public error envelope."""
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    route = respx_mock.get("/v1/orders/ord_hidden")
    route.side_effect = [
        httpx.Response(
            200,
            json={"order_id": "ord_hidden", "advertiser_id": "adv_other"},
        ),
        httpx.Response(404),
    ]
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_hidden")
    errors = []
    for suffix in ("f", "m"):
        req = UpdateMediaBuyRequest.model_validate(
            {
                "account": {"account_id": "signed-buyer-main"},
                "media_buy_id": "ord_hidden",
                "idempotency_key": "k_" + suffix * 18,
                "paused": True,
            }
        )
        with pytest.raises(AdcpError) as excinfo:
            await platform.update_media_buy("ord_hidden", req, ctx)
        errors.append(excinfo.value.to_wire())

    assert errors[0] == errors[1]


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_order_missing_ownership_metadata_is_upstream_failure(respx_mock: Any) -> None:
    from adcp.decisioning import AdcpError
    from adcp.types import UpdateMediaBuyRequest

    respx_mock.get("/v1/orders/ord_malformed").mock(
        return_value=httpx.Response(200, json={"order_id": "ord_malformed"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_malformed")
    req = UpdateMediaBuyRequest.model_validate(
        {
            "account": {"account_id": "signed-buyer-main"},
            "media_buy_id": "ord_malformed",
            "idempotency_key": "k_" + "m" * 18,
            "paused": True,
        }
    )

    with pytest.raises(AdcpError) as excinfo:
        await platform.update_media_buy("ord_malformed", req, ctx)

    assert excinfo.value.code == "SERVICE_UNAVAILABLE"
    assert excinfo.value.recovery == "transient"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_delivery_omits_foreign_advertiser_order(respx_mock: Any) -> None:
    from adcp.types import GetMediaBuyDeliveryRequest

    respx_mock.get("/v1/orders/ord_foreign").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_foreign", "advertiser_id": "adv_other"},
        )
    )
    platform = _platform_with_upstream()
    req = GetMediaBuyDeliveryRequest.model_validate({"media_buy_ids": ["ord_foreign"]})
    response = await platform.get_media_buy_delivery(req, _build_ctx())
    assert response.media_buy_deliveries == []
    assert not any(call.request.url.path.endswith("/delivery") for call in respx_mock.calls)


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_performance_feedback_rejects_foreign_advertiser_order(
    respx_mock: Any,
) -> None:
    from adcp.decisioning import AdcpError
    from adcp.types import ProvidePerformanceFeedbackRequest

    respx_mock.get("/v1/orders/ord_foreign").mock(
        return_value=httpx.Response(
            200,
            json={"order_id": "ord_foreign", "advertiser_id": "adv_other"},
        )
    )
    platform = _platform_with_upstream()
    req = ProvidePerformanceFeedbackRequest.model_validate(
        {
            "idempotency_key": "k_" + "f" * 18,
            "media_buy_id": "ord_foreign",
            "metric_type": "conversion_rate",
            "performance_index": 1.0,
            "measurement_period": {
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-30T23:59:59Z",
            },
        }
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.provide_performance_feedback(req, _build_ctx())
    assert excinfo.value.code == "MEDIA_BUY_NOT_FOUND"
    assert not any(call.request.method == "POST" for call in respx_mock.calls)


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_creative_id_mapping_is_scoped_to_account(respx_mock: Any) -> None:
    from adcp.types import SyncCreativesRequest

    uploads = iter(["up_account_a", "up_account_b"])

    def upload(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"creative_id": next(uploads)})

    route = respx_mock.post("/v1/creatives").mock(side_effect=upload)
    platform = _platform_with_upstream()
    req = SyncCreativesRequest.model_validate(
        {
            "account": {"account_id": "account"},
            "idempotency_key": "k_" + "c" * 18,
            "creatives": [
                {
                    "creative_id": "shared-id",
                    "name": "Creative",
                    "format_kind": "image",
                    "assets": {},
                }
            ],
        }
    )
    ctx_a = _build_ctx()
    ctx_b = _build_ctx()
    ctx_b.account.id = "a_acme_2"
    ctx_b.account.metadata["account_id"] = "other-account"
    ctx_b.account.metadata["advertiser_id"] = "adv_other"

    await platform.sync_creatives(req, ctx_a)
    await platform.sync_creatives(req, ctx_b)
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_account_loader_rejects_account_missing_upstream_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account whose ``ext`` lacks ``network_code`` or
    ``advertiser_id`` is unusable for the translator pattern. The
    AccountStore rejects with ``SERVICE_UNAVAILABLE`` (transient — the
    fix is upstream onboarding) rather than dispatching to a method
    that would 500 on upstream call."""
    from src.models import Account as AccountRow
    from src.platform import _make_account_store

    from adcp.decisioning import AdcpError, AuthInfo
    from src import platform as platform_module

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
    buyer_result = MagicMock()
    buyer_result.scalar_one_or_none = MagicMock(return_value=MagicMock(id="ba_x"))
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=bad_row)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[buyer_result, result])
    sessionmaker = MagicMock(return_value=session)

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())

    store = _make_account_store(sessionmaker, mock_upstream_url="http://up.test")
    with pytest.raises(AdcpError) as excinfo:
        await store.resolve(
            {"account_id": "bad-acct"},
            AuthInfo(kind="anonymous", principal="https://signed-buyer.example/"),
        )
    assert excinfo.value.code == "SERVICE_UNAVAILABLE"
    assert excinfo.value.recovery == "transient"


@pytest.mark.asyncio
async def test_account_loader_returns_mock_mode_with_upstream_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3 wiring: the AccountStore stamps every Account it
    resolves with ``mode='mock'`` and ``metadata['mock_upstream_url']``
    so the framework's ``upstream_for(ctx)`` routes the adapter at the
    mock-server fixture URL.
    """
    from src.models import Account as AccountRow
    from src.platform import _make_account_store

    from adcp.decisioning import AuthInfo
    from src import platform as platform_module

    good_row = AccountRow(
        id="a_good",
        tenant_id="t_acme",
        buyer_agent_id="ba_x",
        account_id="good-acct",
        name="Good Account",
        status="active",
        billing="operator",
        sandbox=False,
        ext={"network_code": "net_premium_us", "advertiser_id": "adv_volta_motors"},
    )
    buyer_result = MagicMock()
    buyer_result.scalar_one_or_none = MagicMock(return_value=MagicMock(id="ba_x"))
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=good_row)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=[buyer_result, result])
    sessionmaker = MagicMock(return_value=session)

    class _Tenant:
        id = "t_acme"

    monkeypatch.setattr(platform_module, "current_tenant", lambda: _Tenant())

    store = _make_account_store(sessionmaker, mock_upstream_url="http://127.0.0.1:4503")
    account = await store.resolve(
        {"account_id": "good-acct"},
        AuthInfo(kind="anonymous", principal="https://signed-buyer.example/"),
    )
    assert account.mode == "mock"
    assert account.metadata["mock_upstream_url"] == "http://127.0.0.1:4503"
    # Routing data still flows through metadata so platform methods
    # pluck network_code / advertiser_id directly.
    assert account.metadata["network_code"] == "net_premium_us"
    assert account.metadata["advertiser_id"] == "adv_volta_motors"


# ---------------------------------------------------------------------------
# Failure-path coverage — every callsite that hits upstream should
# project network / json / 5xx failures onto structured AdcpError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_products_401_translates_to_auth_required(respx_mock: Any) -> None:
    """A 401 from the upstream surfaces as the spec code
    ``AUTH_REQUIRED`` via the SDK's UpstreamHttpClient projection."""
    from adcp.decisioning import AdcpError
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(
        return_value=httpx.Response(401, json={"message": "bad bearer"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    with pytest.raises(AdcpError) as excinfo:
        await platform.get_products(
            GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
        )
    assert excinfo.value.code == "AUTH_REQUIRED"
    assert excinfo.value.recovery == "correctable"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_products_500_translates_to_service_unavailable(respx_mock: Any) -> None:
    """A 500 surfaces as ``SERVICE_UNAVAILABLE`` with
    ``recovery='transient'`` so buyers retry."""
    from adcp.decisioning import AdcpError
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    with pytest.raises(AdcpError) as excinfo:
        await platform.get_products(
            GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
        )
    assert excinfo.value.code == "SERVICE_UNAVAILABLE"
    assert excinfo.value.recovery == "transient"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_products_429_translates_to_rate_limited(respx_mock: Any) -> None:
    """A 429 surfaces as ``RATE_LIMITED`` (transient)."""
    from adcp.decisioning import AdcpError
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(
        return_value=httpx.Response(429, json={"message": "slow down"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    with pytest.raises(AdcpError) as excinfo:
        await platform.get_products(
            GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
        )
    assert excinfo.value.code == "RATE_LIMITED"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_products_404_translates_to_account_not_found(respx_mock: Any) -> None:
    """A 404 from get_products means an unknown network/account, not a
    missing media buy. The :mod:`upstream` helper passes
    ``not_found_code='ACCOUNT_NOT_FOUND'`` to the SDK client so the
    spec-correct AdCP code surfaces on the wire."""
    from adcp.decisioning import AdcpError
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(
        return_value=httpx.Response(404, json={"message": "no such network"})
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    with pytest.raises(AdcpError) as excinfo:
        await platform.get_products(
            GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
        )
    assert excinfo.value.code == "ACCOUNT_NOT_FOUND"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_upstream_malformed_json_raises_clean_error(respx_mock: Any) -> None:
    """A non-JSON response body on a 5xx upstream still produces a
    structured ``AdcpError`` rather than leaking a ``ValueError``."""
    from adcp.decisioning import AdcpError
    from adcp.types import GetProductsRequest

    respx_mock.get("/v1/products").mock(
        return_value=httpx.Response(500, text="<html>nginx oopsie</html>")
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    with pytest.raises(AdcpError) as excinfo:
        await platform.get_products(
            GetProductsRequest.model_validate({"buying_mode": "wholesale"}), ctx
        )
    assert excinfo.value.code == "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# create_media_buy polling correctness — the polling loop must NOT
# project a success when the upstream is still pending or rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_no_task_id_path_refetches_and_projects(
    respx_mock: Any,
) -> None:
    """When the upstream returns no ``approval_task_id`` AND status is
    not already ``approved``/``delivering``, the platform refetches the
    order once and projects from the actual current status — never
    enters the polling loop (no signal to drive it)."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_no_task",
                "name": "No Task Path",
                "status": "draft",  # no approval_task_id, not approved
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    # Second call: refetch returns it now-approved (e.g. upstream
    # auto-approval landed between create and refetch).
    respx_mock.get("/v1/orders/ord_no_task").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_no_task",
                "name": "No Task Path",
                "status": "approved",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    _mock_add_line_item_route(respx_mock, "ord_no_task")
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "n" * 18,
            "brand": {"domain": "fast.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "p1",
                    "format_ids": [
                        {"agent_url": "https://reference.adcp.org", "id": "video_16x9_30s"}
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "p1-cpm",
                }
            ],
        },
    )
    result = await platform.create_media_buy(req, ctx)
    # No-task-id path returns synchronously — no TaskHandoff.
    assert isinstance(result, CreateMediaBuySuccessResponse)
    assert result.media_buy_id == "ord_no_task"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_no_task_id_path_raises_on_pending(
    respx_mock: Any,
) -> None:
    """When the no-task-id refetch still shows ``pending_approval``,
    the platform raises ``SERVICE_UNAVAILABLE`` (transient) rather
    than fabricating a success."""
    from adcp.decisioning import AdcpError
    from adcp.types import CreateMediaBuyRequest

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_stuck",
                "name": "Stuck",
                "status": "draft",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    respx_mock.get("/v1/orders/ord_stuck").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_stuck",
                "name": "Stuck",
                "status": "pending_approval",
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
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "s" * 18,
            "brand": {"domain": "stuck.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "p1",
                    "format_ids": [
                        {"agent_url": "https://reference.adcp.org", "id": "video_16x9_30s"}
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "p1-cpm",
                }
            ],
        },
    )
    with pytest.raises(AdcpError) as excinfo:
        await platform.create_media_buy(req, ctx)
    assert excinfo.value.code == "SERVICE_UNAVAILABLE"
    assert excinfo.value.recovery == "transient"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_raises_when_polling_times_out(
    respx_mock: Any,
) -> None:
    """When the approval task never completes within the polling
    window, the polling coroutine raises ``SERVICE_UNAVAILABLE``
    (transient). The framework projects this as a wire-shaped task
    failure — never a fabricated success."""
    from src.platform import V3ReferenceSeller

    from adcp.decisioning import AdcpError
    from adcp.types import CreateMediaBuyRequest

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_timeout",
                "name": "Timeout",
                "status": "pending_approval",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "approval_task_id": "task_timeout",
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    # Every poll returns ``pending`` — the loop must exhaust.
    respx_mock.get("/v1/tasks/task_timeout").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "task_timeout",
                "order_id": "ord_timeout",
                "status": "pending",
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    sessionmaker = MagicMock()
    # Tighten polling so the test finishes fast — 2 iterations × 0.001s.
    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key="test-key",
        approval_poll_interval_s=0.001,
        approval_poll_max_iterations=2,
    )
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "t" * 18,
            "brand": {"domain": "timeout.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "p1",
                    "format_ids": [
                        {"agent_url": "https://reference.adcp.org", "id": "video_16x9_30s"}
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "p1-cpm",
                }
            ],
        },
    )
    # Sync-poll exhausts the polling window and raises directly.
    with pytest.raises(AdcpError) as excinfo:
        await platform.create_media_buy(req, ctx)
    assert excinfo.value.code == "SERVICE_UNAVAILABLE"
    assert excinfo.value.recovery == "transient"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_create_media_buy_raises_when_task_rejected(respx_mock: Any) -> None:
    """When the upstream approval task completes with
    ``outcome='rejected'``, the polling coroutine raises
    ``POLICY_VIOLATION`` (terminal). The framework projects this as a
    wire-shaped task failure."""
    from adcp.decisioning import AdcpError
    from adcp.types import CreateMediaBuyRequest

    respx_mock.post("/v1/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "ord_rejected",
                "name": "Rejected",
                "status": "pending_approval",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "approval_task_id": "task_rej",
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    respx_mock.get("/v1/tasks/task_rej").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "task_rej",
                "order_id": "ord_rejected",
                "status": "completed",
                "result": {
                    "outcome": "rejected",
                    "reviewer_note": "Brand-safety violation.",
                },
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    req = _canonical_create_request(
        CreateMediaBuyRequest,
        {
            "account": {"account_id": "signed-buyer-main"},
            "idempotency_key": "k_" + "x" * 18,
            "brand": {"domain": "rejected.example"},
            "total_budget": {"amount": 100.0, "currency": "USD"},
            "start_time": "asap",
            "end_time": "2026-06-30T23:59:59Z",
            "packages": [
                {
                    "product_id": "p1",
                    "format_ids": [
                        {"agent_url": "https://reference.adcp.org", "id": "video_16x9_30s"}
                    ],
                    "budget": 100.0,
                    "pricing_option_id": "p1-cpm",
                }
            ],
        },
    )
    # Sync-poll reaches the rejected task and raises directly.
    with pytest.raises(AdcpError) as excinfo:
        await platform.create_media_buy(req, ctx)
    assert excinfo.value.code == "POLICY_VIOLATION"
    assert "Brand-safety" in str(excinfo.value)


# ---------------------------------------------------------------------------
# get_media_buy_delivery — status reflects upstream order state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_media_buy_delivery_projects_completed_status(
    respx_mock: Any,
) -> None:
    """A completed upstream order must surface as AdCP ``completed``,
    not as ``active`` — buyers rely on terminal-state semantics for
    finalization."""
    from adcp.types import GetMediaBuyDeliveryRequest

    respx_mock.get("/v1/orders/ord_done/delivery").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_done",
                "currency": "USD",
                "reporting_period": {
                    "start": "2026-03-01T00:00:00Z",
                    "end": "2026-03-31T23:59:59Z",
                },
                "totals": {"impressions": 500_000, "clicks": 2000, "spend": 1000.0},
            },
        )
    )
    respx_mock.get("/v1/orders/ord_done").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_done",
                "name": "Done",
                "status": "completed",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 1000.0,
                "created_at": "2026-03-01T00:00:00Z",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_done")
    req = GetMediaBuyDeliveryRequest.model_validate({"media_buy_ids": ["ord_done"]})
    resp = await platform.get_media_buy_delivery(req, ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    assert len(payload["media_buy_deliveries"]) == 1
    assert payload["media_buy_deliveries"][0]["status"] == "completed"


@pytest.mark.asyncio
@respx.mock(base_url=_RESPX_BASE_URL)
async def test_get_media_buy_delivery_projects_canceled_status(
    respx_mock: Any,
) -> None:
    """A canceled upstream order surfaces as AdCP ``canceled`` —
    not as the previously-hardcoded ``active``."""
    from adcp.types import GetMediaBuyDeliveryRequest

    respx_mock.get("/v1/orders/ord_killed/delivery").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_killed",
                "currency": "USD",
                "reporting_period": {
                    "start": "2026-04-01T00:00:00Z",
                    "end": "2026-04-15T23:59:59Z",
                },
                "totals": {"impressions": 100, "clicks": 1, "spend": 1.0},
            },
        )
    )
    respx_mock.get("/v1/orders/ord_killed").mock(
        return_value=httpx.Response(
            200,
            json={
                "order_id": "ord_killed",
                "name": "Killed",
                "status": "canceled",
                "advertiser_id": "adv_volta_motors",
                "currency": "USD",
                "budget": 100.0,
                "created_at": "2026-04-01T00:00:00Z",
                "updated_at": "2026-04-15T00:00:00Z",
            },
        )
    )
    platform = _platform_with_upstream()
    ctx = _build_ctx()
    _seed_owned_buy(platform, ctx, "ord_killed")
    req = GetMediaBuyDeliveryRequest.model_validate({"media_buy_ids": ["ord_killed"]})
    resp = await platform.get_media_buy_delivery(req, ctx)
    payload = resp.model_dump(mode="json", exclude_none=True)
    assert payload["media_buy_deliveries"][0]["status"] == "canceled"
