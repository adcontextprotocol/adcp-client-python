"""Tests for ProposalManager v1 — Protocol + MockProposalManager + dispatcher routing.

Covers:

* Protocol conformance — ``isinstance(MockProposalManager(...),
  ProposalManager)``.
* ``ProposalCapabilities`` declaration validation —
  sales_specialism is required and limited to the two v1 slugs.
* ``MockProposalManager`` forwards ``get_products`` to the
  configured mock URL (mocked via respx).
* Dispatcher with ``proposal_manager=`` wired routes get_products
  to the manager.
* Dispatcher without ``proposal_manager=`` falls through to
  ``platform.get_products`` (back-compat — every existing example
  keeps working).
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
    ProposalCapabilities,
    ProposalManager,
    Recipe,
    SingletonAccounts,
)
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


def _make_get_products_request(buying_mode: str = "brief", brief: str | None = "test brief"):
    """Build a minimal valid GetProductsRequest."""
    from adcp.types import GetProductsRequest

    kwargs: dict[str, Any] = {
        "account": {"account_id": "acct_test"},
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
    model. The handler's ``cast()`` is a static-type lie — adopter
    returns flow through unchanged.
    """
    if hasattr(response, "products"):
        return list(response.products or [])
    if isinstance(response, dict):
        return list(response.get("products") or [])
    return []


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    proposal_manager: Any | None = None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        proposal_manager=proposal_manager,
    )


class _StubPlatform(DecisioningPlatform):
    """Minimal platform that records get_products calls so back-compat
    fall-through is observable."""

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="seller")

    def __init__(self) -> None:
        self.get_products_calls: list[Any] = []

    async def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        self.get_products_calls.append(req)
        return _make_get_products_response()


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
# Dispatcher routing — proposal_manager wired vs. not wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_routes_to_proposal_manager_when_wired(executor) -> None:
    """When proposal_manager is wired, get_products goes to the manager,
    NOT to platform.get_products."""
    proposal_calls: list[Any] = []

    class _CountingManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        async def get_products(self, req, ctx):
            proposal_calls.append(req)
            return _make_get_products_response()

    platform = _StubPlatform()
    handler = _make_handler(platform, executor, proposal_manager=_CountingManager())
    req = _make_get_products_request()
    await handler.get_products(req, ToolContext())

    assert len(proposal_calls) == 1
    assert platform.get_products_calls == []  # platform NOT called


@pytest.mark.asyncio
async def test_dispatcher_falls_through_to_platform_when_no_manager(executor) -> None:
    """When proposal_manager is None, get_products goes to platform.get_products
    — backward-compatible with every existing adopter."""
    platform = _StubPlatform()
    handler = _make_handler(platform, executor, proposal_manager=None)
    req = _make_get_products_request()
    await handler.get_products(req, ToolContext())

    assert len(platform.get_products_calls) == 1


@pytest.mark.asyncio
async def test_adopter_proposal_manager_subclass(executor) -> None:
    """An adopter ProposalManager class with custom logic dispatches."""

    class _AdopterManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-guaranteed")

        async def get_products(self, req, ctx):
            response = _make_get_products_response()
            response["products"][0]["name"] = "Adopter custom product"
            return response

    handler = _make_handler(_StubPlatform(), executor, proposal_manager=_AdopterManager())
    req = _make_get_products_request()
    response = await handler.get_products(req, ToolContext())

    # The shim's cast() is a static-type hint, not runtime validation —
    # adopters returning dicts get dicts back through the dispatcher;
    # the framework's transport layer handles serialization.
    products = _products_of(response)
    assert len(products) == 1
    assert products[0]["name"] == "Adopter custom product"


@pytest.mark.asyncio
async def test_dispatcher_routes_sync_proposal_manager(executor) -> None:
    """Sync get_products on a ProposalManager runs on the thread pool."""

    class _SyncManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

        def get_products(self, req, ctx):
            return _make_get_products_response()

    handler = _make_handler(_StubPlatform(), executor, proposal_manager=_SyncManager())
    req = _make_get_products_request()
    response = await handler.get_products(req, ToolContext())
    assert _products_of(response)


# ---------------------------------------------------------------------------
# Refine routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refine_routed_when_capability_and_method_present(executor) -> None:
    """buying_mode='refine' + refine capability + refine_products method
    → dispatch routes to refine_products."""
    refine_calls: list[Any] = []
    get_calls: list[Any] = []

    class _RefineManager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            refine=True,
        )

        async def get_products(self, req, ctx):
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            refine_calls.append(req)
            return _make_get_products_response()

    handler = _make_handler(_StubPlatform(), executor, proposal_manager=_RefineManager())
    req = _make_get_products_request(buying_mode="refine", brief=None)
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
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            raise AssertionError("refine_products should NOT have been called")

    handler = _make_handler(_StubPlatform(), executor, proposal_manager=_NoRefineManager())
    req = _make_get_products_request(buying_mode="refine", brief=None)
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
            get_calls.append(req)
            return _make_get_products_response()

        async def refine_products(self, req, ctx):
            raise AssertionError("refine_products should NOT be called for brief mode")

    handler = _make_handler(_StubPlatform(), executor, proposal_manager=_RefineManager())
    req = _make_get_products_request(buying_mode="brief")
    await handler.get_products(req, ToolContext())

    assert len(get_calls) == 1
