"""Tests for ProposalManager v1 — Protocol + MockProposalManager + per-tenant routing.

Covers:

* Protocol conformance — ``isinstance(MockProposalManager(...),
  ProposalManager)``.
* ``ProposalCapabilities`` declaration validation —
  sales_specialism is required and limited to the two v1 slugs.
* ``MockProposalManager`` forwards ``get_products`` to the
  configured mock URL (mocked via respx).
* ``PlatformRouter(proposal_managers={...})`` — per-tenant binding.
  Tenants with a wired ProposalManager route to it; tenants without
  one fall through to the tenant's platform.get_products
  (back-compat per tenant).
* Orphan ``proposal_managers`` keys (no matching platform) raise
  ``ValueError`` at construction.
* Adopter ProposalManager subclass with custom get_products works.
* Sync and async ``ProposalManager.get_products`` both work.
* Refine routing — ``buying_mode='refine'`` + ``capabilities.refine``
  + ``refine_products`` method present → routes to refine_products;
  any condition missing → falls through to get_products.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import respx

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    MockProposalManager,
    PlatformRouter,
    ProposalCapabilities,
    ProposalManager,
    Recipe,
)
from adcp.decisioning.accounts import Account, AuthInfo
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-proposal-mgr-")
    yield pool
    pool.shutdown(wait=True)


def _make_get_products_request(
    buying_mode: str = "brief",
    brief: str | None = "test brief",
    account_id: str = "acct_test",
):
    """Build a minimal valid GetProductsRequest."""
    from adcp.types import GetProductsRequest

    kwargs: dict[str, Any] = {
        "account": {"account_id": account_id},
        "buying_mode": buying_mode,
    }
    # 'brief' mode requires a brief; 'refine' must NOT carry brief and MUST
    # carry a non-empty refine[] array; 'wholesale' must not carry brief.
    if buying_mode == "brief":
        kwargs["brief"] = brief
    elif buying_mode == "refine":
        kwargs["refine"] = [
            {"scope": "request", "ask": "narrow the brief"}
        ]
    return GetProductsRequest(**kwargs)


def _make_get_products_response() -> dict[str, Any]:
    """Build a wire-shaped GetProductsResponse dict the framework can return."""
    return {
        "products": [
            {
                "product_id": "demo-product",
                "name": "Demo product",
                "description": "Stub product from a mock proposal manager.",
                "delivery_type": "non_guaranteed",
                "publisher_properties": [
                    {"publisher_domain": "example.com", "selection_type": "all"},
                ],
                "format_ids": [
                    {
                        "agent_url": "https://creative.adcontextprotocol.org/",
                        "id": "display_300x250",
                    },
                ],
                "pricing_options": [
                    {
                        "pricing_option_id": "po-cpm-default",
                        "pricing_model": "cpm",
                        "floor_price": 5.0,
                        "currency": "USD",
                    },
                ],
                "reporting_capabilities": {
                    "available_metrics": ["impressions"],
                    "available_reporting_frequencies": ["daily"],
                    "date_range_support": "date_range",
                    "supports_webhooks": False,
                    "expected_delay_minutes": 60,
                    "timezone": "UTC",
                },
                "delivery_measurement": {"provider": "internal"},
            },
        ],
    }


def _products_of(response: Any) -> list[Any]:
    """Pull the products list off either a dict response or a Pydantic
    model.
    """
    if hasattr(response, "products"):
        return list(response.products or [])
    if isinstance(response, dict):
        return list(response.get("products") or [])
    return []


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


class _TenantAccounts:
    """Test AccountStore that returns ``account.metadata['tenant_id']``
    based on the ``account_id`` prefix in the wire ref. Convention:
    ``"<tenant_id>:<rest>"`` — first colon-segment is the tenant.
    """

    resolution = "explicit"

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[Any]:
        del auth_info
        ref = ref or {}
        account_id = str(ref.get("account_id", "tenant_a:default"))
        tenant_id = account_id.split(":", 1)[0]
        return Account(
            id=account_id,
            name=account_id,
            status="active",
            metadata={"tenant_id": tenant_id},
            auth_info=None,
        )


class _StubPlatform(DecisioningPlatform):
    """Minimal platform that records get_products calls so back-compat
    fall-through is observable. Used as a child of PlatformRouter.
    """

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = None  # type: ignore[assignment]

    def __init__(self, label: str = "stub") -> None:
        self.label = label
        self.get_products_calls: list[Any] = []

    async def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        del ctx
        self.get_products_calls.append(req)
        response = _make_get_products_response()
        response["products"][0]["product_id"] = f"{self.label}-product"
        return response


def _make_router(
    *,
    platforms: dict[str, DecisioningPlatform],
    proposal_managers: dict[str, Any] | None = None,
) -> PlatformRouter:
    return PlatformRouter(
        accounts=_TenantAccounts(),
        platforms=platforms,
        proposal_managers=proposal_managers,
        capabilities=DecisioningCapabilities(specialisms=["sales-non-guaranteed"]),
    )


# ---------------------------------------------------------------------------
# Protocol conformance + capabilities
# ---------------------------------------------------------------------------


def test_mock_proposal_manager_is_proposal_manager() -> None:
    """MockProposalManager satisfies the ProposalManager Protocol."""
    manager = MockProposalManager(mock_upstream_url="http://localhost:4500")
    assert isinstance(manager, ProposalManager)


def test_proposal_capabilities_default_non_guaranteed() -> None:
    """MockProposalManager defaults to sales-non-guaranteed."""
    manager = MockProposalManager(mock_upstream_url="http://localhost:4500")
    assert manager.capabilities.sales_specialism == "sales-non-guaranteed"
    # All capability flags default off — adopters opt in explicitly.
    assert manager.capabilities.refine is False
    assert manager.capabilities.dynamic_products is False
    assert manager.capabilities.rate_card_pricing is False
    assert manager.capabilities.availability_reservations is False
    assert manager.capabilities.multi_decisioning is False


def test_proposal_capabilities_guaranteed_slug() -> None:
    """Adopter can declare sales-guaranteed at construction."""
    manager = MockProposalManager(
        mock_upstream_url="http://localhost:4500",
        sales_specialism="sales-guaranteed",
    )
    assert manager.capabilities.sales_specialism == "sales-guaranteed"


def test_proposal_capabilities_invalid_specialism_rejected() -> None:
    """ProposalCapabilities rejects non-sales-* and unknown sales-* slugs."""
    with pytest.raises(AdcpError) as exc_info:
        ProposalCapabilities(sales_specialism="signal-marketplace")  # type: ignore[arg-type]
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "sales-guaranteed" in str(exc_info.value)


def test_mock_proposal_manager_empty_url_rejected() -> None:
    """MockProposalManager refuses an empty / None mock_upstream_url."""
    with pytest.raises(AdcpError) as exc_info:
        MockProposalManager(mock_upstream_url="")
    assert exc_info.value.code == "CONFIGURATION_ERROR"


# ---------------------------------------------------------------------------
# Recipe — typed subclass round-trips
# ---------------------------------------------------------------------------


def test_recipe_subclass_round_trips_through_dict() -> None:
    """Adopter Recipe subclass model_dump → dict → model_validate works."""
    from typing import Literal

    class _GAMRecipe(Recipe):
        recipe_kind: Literal["gam"] = "gam"
        line_item_template_id: str
        ad_unit_ids: list[str]

    original = _GAMRecipe(
        line_item_template_id="lit_001",
        ad_unit_ids=["unit_a", "unit_b"],
    )
    dumped = original.model_dump()
    assert dumped["recipe_kind"] == "gam"
    assert dumped["line_item_template_id"] == "lit_001"

    rehydrated = _GAMRecipe.model_validate(dumped)
    assert rehydrated == original


# ---------------------------------------------------------------------------
# MockProposalManager forwarder behaviour (HTTP mocked via respx)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_mock_proposal_manager_forwards_get_products() -> None:
    """MockProposalManager POSTs to /get_products on the configured URL."""
    base_url = "http://mock-server.test"
    expected = _make_get_products_response()
    route = respx.post(f"{base_url}/get_products").mock(
        return_value=httpx.Response(200, json=expected)
    )

    manager = MockProposalManager(mock_upstream_url=base_url)
    req = _make_get_products_request()
    result = await manager.get_products(req, ctx=None)  # type: ignore[arg-type]

    assert route.called
    assert result == expected

    # Body the forwarder posted is the wire-shape JSON of the request.
    sent = route.calls.last.request
    assert sent.method == "POST"
    # Spot-check that buying_mode survives serialization.
    import json

    body = json.loads(sent.content)
    assert body["buying_mode"] == "brief"
    assert body["brief"] == "test brief"
    await manager.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_mock_proposal_manager_refine_uses_same_endpoint() -> None:
    """Refine forwards to the same /get_products endpoint with
    buying_mode=refine on the body."""
    base_url = "http://mock-server.test"
    expected = _make_get_products_response()
    route = respx.post(f"{base_url}/get_products").mock(
        return_value=httpx.Response(200, json=expected)
    )

    manager = MockProposalManager(mock_upstream_url=base_url)
    req = _make_get_products_request(buying_mode="refine", brief=None)
    await manager.refine_products(req, ctx=None)  # type: ignore[arg-type]

    assert route.called
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["buying_mode"] == "refine"
    await manager.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_mock_proposal_manager_non_dict_response_raises() -> None:
    """Mock-server returning a non-dict surfaces SERVICE_UNAVAILABLE."""
    base_url = "http://mock-server.test"
    respx.post(f"{base_url}/get_products").mock(
        return_value=httpx.Response(200, json=["not", "a", "dict"])
    )

    manager = MockProposalManager(mock_upstream_url=base_url)
    req = _make_get_products_request()
    with pytest.raises(AdcpError) as exc_info:
        await manager.get_products(req, ctx=None)  # type: ignore[arg-type]
    assert exc_info.value.code == "SERVICE_UNAVAILABLE"
    await manager.aclose()


# ---------------------------------------------------------------------------
# Per-tenant ProposalManager binding via PlatformRouter
# ---------------------------------------------------------------------------


def test_router_orphan_proposal_manager_key_rejected() -> None:
    """proposal_managers keys MUST be a subset of platforms keys —
    orphan tenants raise ValueError at construction."""

    class _M:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            return _make_get_products_response()

    with pytest.raises(ValueError, match="orphan tenant_id"):
        PlatformRouter(
            accounts=_TenantAccounts(),
            platforms={"tenant_a": _StubPlatform("a")},
            proposal_managers={"tenant_b": _M()},  # not in platforms
            capabilities=DecisioningCapabilities(specialisms=["sales-non-guaranteed"]),
        )


def test_router_proposal_managers_default_none() -> None:
    """proposal_managers is optional — back-compat with pre-v1 router."""
    router = _make_router(platforms={"tenant_a": _StubPlatform("a")})
    assert router.proposal_manager_for_tenant("tenant_a") is None


@pytest.mark.asyncio
async def test_router_routes_to_proposal_manager_for_wired_tenant(executor) -> None:
    """When tenant_a has a wired ProposalManager, get_products goes to
    the manager, NOT to platform.get_products."""
    proposal_calls: list[Any] = []

    class _CountingManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            del ctx
            proposal_calls.append(req)
            return _make_get_products_response()

    platform_a = _StubPlatform("a")
    router = _make_router(
        platforms={"tenant_a": platform_a},
        proposal_managers={"tenant_a": _CountingManager()},
    )
    handler = _make_handler(router, executor)
    req = _make_get_products_request(account_id="tenant_a:default")
    await handler.get_products(req, ToolContext())

    assert len(proposal_calls) == 1
    assert platform_a.get_products_calls == []  # platform NOT called


@pytest.mark.asyncio
async def test_router_falls_through_for_unwired_tenant(executor) -> None:
    """When tenant_b has no ProposalManager, get_products falls
    through to platform_b.get_products — back-compat per tenant."""

    class _ManagerA:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            raise AssertionError("tenant_a's manager should NOT be called for tenant_b")

    platform_a = _StubPlatform("a")
    platform_b = _StubPlatform("b")
    router = _make_router(
        platforms={"tenant_a": platform_a, "tenant_b": platform_b},
        proposal_managers={"tenant_a": _ManagerA()},
    )
    handler = _make_handler(router, executor)

    # tenant_b — no ProposalManager wired, falls through to platform_b.
    req = _make_get_products_request(account_id="tenant_b:default")
    await handler.get_products(req, ToolContext())
    assert len(platform_b.get_products_calls) == 1
    assert platform_a.get_products_calls == []


@pytest.mark.asyncio
async def test_router_per_tenant_isolation(executor) -> None:
    """Two tenants both with ProposalManagers wired — each routes to
    its own manager, not the other's."""
    a_calls: list[Any] = []
    b_calls: list[Any] = []

    class _ManagerA:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            del ctx
            a_calls.append(req)
            return _make_get_products_response()

    class _ManagerB:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            del ctx
            b_calls.append(req)
            return _make_get_products_response()

    router = _make_router(
        platforms={"tenant_a": _StubPlatform("a"), "tenant_b": _StubPlatform("b")},
        proposal_managers={"tenant_a": _ManagerA(), "tenant_b": _ManagerB()},
    )
    handler = _make_handler(router, executor)

    await handler.get_products(_make_get_products_request(account_id="tenant_a:x"), ToolContext())
    await handler.get_products(_make_get_products_request(account_id="tenant_b:y"), ToolContext())

    assert len(a_calls) == 1
    assert len(b_calls) == 1


@pytest.mark.asyncio
async def test_router_dispatches_sync_proposal_manager(executor) -> None:
    """Sync get_products on a ProposalManager runs on the thread pool."""

    class _SyncManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        def get_products(self, req, ctx):
            del req, ctx
            return _make_get_products_response()

    router = _make_router(
        platforms={"tenant_a": _StubPlatform("a")},
        proposal_managers={"tenant_a": _SyncManager()},
    )
    handler = _make_handler(router, executor)
    req = _make_get_products_request(account_id="tenant_a:default")
    response = await handler.get_products(req, ToolContext())
    assert _products_of(response)


# ---------------------------------------------------------------------------
# Refine routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refine_routed_when_capability_and_method_present(executor) -> None:
    """buying_mode='refine' + refine capability + refine_products method
    → router routes to refine_products."""
    refine_calls: list[Any] = []
    get_calls: list[Any] = []

    class _RefineManager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            refine=True,
        )

        async def get_products(self, req, ctx):
            del ctx
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            del ctx
            refine_calls.append(req)
            return _make_get_products_response()

    router = _make_router(
        platforms={"tenant_a": _StubPlatform("a")},
        proposal_managers={"tenant_a": _RefineManager()},
    )
    handler = _make_handler(router, executor)
    req = _make_get_products_request(
        buying_mode="refine", brief=None, account_id="tenant_a:default"
    )
    await handler.get_products(req, ToolContext())

    assert len(refine_calls) == 1
    assert len(get_calls) == 0


@pytest.mark.asyncio
async def test_refine_falls_through_when_capability_off(executor) -> None:
    """buying_mode='refine' but capability.refine=False → routes to
    get_products (the manager handles it internally)."""
    get_calls: list[Any] = []

    class _NoRefineManager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-non-guaranteed",
            refine=False,
        )

        async def get_products(self, req, ctx):
            del ctx
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            raise AssertionError("refine_products should NOT have been called")

    router = _make_router(
        platforms={"tenant_a": _StubPlatform("a")},
        proposal_managers={"tenant_a": _NoRefineManager()},
    )
    handler = _make_handler(router, executor)
    req = _make_get_products_request(
        buying_mode="refine", brief=None, account_id="tenant_a:default"
    )
    await handler.get_products(req, ToolContext())

    assert len(get_calls) == 1


@pytest.mark.asyncio
async def test_brief_mode_never_routes_to_refine(executor) -> None:
    """buying_mode='brief' always routes to get_products even when refine
    is supported."""
    get_calls: list[Any] = []

    class _RefineManager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            refine=True,
        )

        async def get_products(self, req, ctx):
            del ctx
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            raise AssertionError("refine_products should NOT be called for brief mode")

    router = _make_router(
        platforms={"tenant_a": _StubPlatform("a")},
        proposal_managers={"tenant_a": _RefineManager()},
    )
    handler = _make_handler(router, executor)
    req = _make_get_products_request(buying_mode="brief", account_id="tenant_a:default")
    await handler.get_products(req, ToolContext())

    assert len(get_calls) == 1
