"""Vertical-slice integration tests for ``examples/hello_seller.py``.

Exercises the full v6.0 dispatch path — typed request → account
resolution via :class:`SingletonAccounts` → :class:`RequestContext`
hydration → platform method invocation → typed response — without
spinning up an MCP server. The MCP transport is exercised by the
adcp-client-python repo's own MCP test surface (separate concern);
here we focus on the decisioning framework wiring.

Two-example file plan per the dispatch design's D13:

* This file — sync vertical slice.
* :file:`tests/test_hello_seller_async_handoff_integration.py` —
  hybrid + AdcpError round-trip.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

# examples/ is not a package — add to sys.path so the integration
# tests can import the module directly.
_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

import hello_seller as _hello  # noqa: E402

from adcp.decisioning import (  # noqa: E402
    AdcpError,
    InMemoryTaskRegistry,
)
from adcp.decisioning.handler import PlatformHandler  # noqa: E402
from adcp.server.base import ToolContext  # noqa: E402


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-int-hello-")
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def handler(executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(
        _hello.HelloSeller(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


@pytest.mark.asyncio
async def test_get_products_returns_one_product(handler: PlatformHandler) -> None:
    """End-to-end: typed Pydantic request → resolved account → platform
    method → typed response. The hello_seller stubs return one product
    with all spec-required fields populated."""
    from adcp.types import GetProductsRequest

    resp = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="anything"),
        ToolContext(),
    )
    # Response is the raw dict the platform returned; framework-level
    # serialization happens at the wire layer (out of scope here).
    assert isinstance(resp, dict)
    products = resp["products"]
    assert len(products) == 1
    p = products[0]
    assert p["product_id"] == "display-rotation"
    # Spec-required fields populated.
    for required in (
        "name",
        "description",
        "delivery_type",
        "publisher_properties",
        "format_ids",
        "pricing_options",
        "reporting_capabilities",
    ):
        assert required in p, f"Product missing required field: {required}"


@pytest.mark.asyncio
async def test_create_media_buy_sync_path(handler: PlatformHandler) -> None:
    """Hello seller's create_media_buy is sync — accepts the request
    and returns the success envelope. media_buy_id encodes the
    resolved account.id (proves account resolution wired correctly)."""
    from adcp.types import CreateMediaBuyRequest

    req = CreateMediaBuyRequest(
        account={"account_id": "buyer-1"},
        brand={"domain": "buyer.example.com"},
        idempotency_key="idem_int_test_aaaa1234",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-31T23:59:59Z",
        packages=[
            {
                "product_id": "display-rotation",
                "pricing_option_id": "po-cpm-default",
                "budget": 1000,
            },
        ],
    )
    resp = await handler.create_media_buy(req, ToolContext())
    assert isinstance(resp, dict)
    # SingletonAccounts(account_id="hello") + no auth_info → resolved
    # to "hello:anonymous" per per-principal scoping. The hello seller
    # encodes the resolved account.id into media_buy_id.
    assert resp["media_buy_id"].startswith("mb_hello:anonymous_"), resp
    assert resp["status"] == "active"
    assert len(resp["packages"]) == 1


@pytest.mark.asyncio
async def test_create_media_buy_rejects_empty_packages(
    handler: PlatformHandler,
) -> None:
    """AdcpError raise-and-project — empty packages tripping the
    platform's own correctable rejection. The framework propagates
    AdcpError verbatim (not wrapped to INTERNAL_ERROR) so the wire
    response carries the structured envelope.

    The wire schema also enforces ``packages.minItems: 1``, so
    real-world buyers can't reach this branch — but adopters
    relying on extra business validation (e.g., budget floors,
    blocked products) hit the same code path. We construct via
    ``model_construct`` to bypass Pydantic's pre-validation and
    exercise the platform's defensive check."""
    from adcp.types import CreateMediaBuyRequest

    req = CreateMediaBuyRequest.model_construct(
        account={"account_id": "buyer-1"},
        brand={"domain": "buyer.example.com"},
        idempotency_key="idem_int_test_bbbb1234",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-31T23:59:59Z",
        packages=[],
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(req, ToolContext())
    assert exc_info.value.code == "INVALID_REQUEST"
    assert exc_info.value.recovery == "correctable"
    assert exc_info.value.field == "packages"


@pytest.mark.asyncio
async def test_get_media_buy_delivery_returns_zeros(
    handler: PlatformHandler,
) -> None:
    """Stub delivery snapshot — proves the dispatch path works for a
    second sync read tool."""
    from adcp.types import GetMediaBuyDeliveryRequest

    req = GetMediaBuyDeliveryRequest(
        account={"account_id": "buyer-1"},
        media_buy_ids=["mb_x"],
    )
    resp = await handler.get_media_buy_delivery(req, ToolContext())
    assert isinstance(resp, dict)
    # Wire field is ``media_buy_deliveries`` per
    # ``schemas/cache/media-buy/get-media-buy-delivery-response.json``.
    # Pre-fix the example used the wrong key (``deliveries``); pin the
    # spec field name here so future drift fails the test.
    assert len(resp["media_buy_deliveries"]) == 1
    assert resp["media_buy_deliveries"][0]["totals"]["impressions"] == 0


@pytest.mark.asyncio
async def test_account_resolution_threads_through(
    handler: PlatformHandler,
) -> None:
    """The framework's _build_request_context wires
    ``ctx.account.id`` into the platform method via SingletonAccounts.
    Different auth_info principals (set via ctx.metadata) yield
    different account ids."""
    from adcp.decisioning import AuthInfo
    from adcp.types import GetProductsRequest

    seen_ids: list[str] = []

    # Inject an AuthInfo via ctx.metadata['adcp.auth_info'] and
    # observe via the platform method body.
    class _SpyHelloSeller(_hello.HelloSeller):
        def get_products(self, req, ctx):
            seen_ids.append(ctx.account.id)
            return super().get_products(req, ctx)

    spy = PlatformHandler(
        _SpyHelloSeller(),
        executor=handler._executor,  # share the fixture's executor
        registry=InMemoryTaskRegistry(),
    )

    # Two different principals → two different per-principal account ids.
    for principal in ("buyer-a", "buyer-b"):
        ctx = ToolContext(
            metadata={
                "adcp.auth_info": AuthInfo(
                    kind="signed_request",
                    principal=principal,
                ),
            },
        )
        await spy.get_products(
            GetProductsRequest(buying_mode="brief", brief="x"),
            ctx,
        )

    assert seen_ids == ["hello:buyer-a", "hello:buyer-b"]


@pytest.mark.asyncio
async def test_caller_identity_uses_composite_key(
    handler: PlatformHandler,
) -> None:
    """The framework sets ``ctx.caller_identity`` to the composite
    cache scope key (D9 round-3 — module + qualname + account.id).
    Idempotency middleware reads this; different stores can't collide."""
    from adcp.types import GetProductsRequest

    seen_caller: list[Any] = []

    class _SpySeller(_hello.HelloSeller):
        def get_products(self, req, ctx):
            seen_caller.append(ctx.caller_identity)
            return super().get_products(req, ctx)

    spy_handler = PlatformHandler(
        _SpySeller(),
        executor=handler._executor,
        registry=InMemoryTaskRegistry(),
    )
    await spy_handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="x"),
        ToolContext(),
    )
    assert seen_caller[0] == ("adcp.decisioning.accounts.SingletonAccounts:hello:anonymous")


@pytest.mark.asyncio
async def test_advertised_tools_class_attribute_set(
    handler: PlatformHandler,
) -> None:
    """The codegen-target ``advertised_tools`` ClassVar is populated
    at class definition time on PlatformHandler — adopters get a
    focused tools/list filter without manual registration (after
    prep PR #318 wires __init_subclass__ auto-registration)."""
    assert "get_products" in PlatformHandler.advertised_tools
    assert "create_media_buy" in PlatformHandler.advertised_tools
    assert "update_media_buy" in PlatformHandler.advertised_tools
    assert "sync_creatives" in PlatformHandler.advertised_tools
    assert "get_media_buy_delivery" in PlatformHandler.advertised_tools


@pytest.mark.asyncio
async def test_get_media_buys_returns_spec_valid_envelope(handler: PlatformHandler) -> None:
    """Stub returns a wire shape that satisfies ``GetMediaBuysResponse``."""
    from adcp.types import GetMediaBuysRequest, GetMediaBuysResponse

    req = GetMediaBuysRequest(account={"account_id": "buyer-1"})
    resp = await handler.get_media_buys(req, ToolContext())
    # Validate against the canonical Pydantic model — catches drift
    # between stub and spec, not just dict-key presence.
    GetMediaBuysResponse.model_validate(resp)
    assert resp["media_buys"] == []


@pytest.mark.asyncio
async def test_list_creative_formats_returns_spec_valid_envelope(
    handler: PlatformHandler,
) -> None:
    """Stub returns a wire shape that satisfies ``ListCreativeFormatsResponse``."""
    from adcp.types import ListCreativeFormatsRequest, ListCreativeFormatsResponse

    req = ListCreativeFormatsRequest()
    resp = await handler.list_creative_formats(req, ToolContext())
    ListCreativeFormatsResponse.model_validate(resp)
    assert resp["formats"] == []


@pytest.mark.asyncio
async def test_list_creatives_returns_spec_valid_envelope(handler: PlatformHandler) -> None:
    """Stub returns a wire shape that satisfies ``ListCreativesResponse``,
    including the spec-required ``query_summary`` and ``pagination``
    envelopes (a buyer hitting the example otherwise gets a non-conformant
    response)."""
    from adcp.types import ListCreativesRequest, ListCreativesResponse

    req = ListCreativesRequest(account={"account_id": "buyer-1"})
    resp = await handler.list_creatives(req, ToolContext())
    ListCreativesResponse.model_validate(resp)
    assert resp["creatives"] == []


@pytest.mark.asyncio
async def test_provide_performance_feedback_acknowledges(handler: PlatformHandler) -> None:
    """Smoke: stub returns success acknowledgment for provide_performance_feedback."""
    from adcp.types import ProvidePerformanceFeedbackRequest

    req = ProvidePerformanceFeedbackRequest(
        account={"account_id": "buyer-1"},
        media_buy_id="mb_test",
        idempotency_key="perf-feedback-test-key-001",
        measurement_period={"start": "2026-05-01T00:00:00Z", "end": "2026-05-31T23:59:59Z"},
        performance_index=1.0,
        feedback=[],
    )
    resp = await handler.provide_performance_feedback(req, ToolContext())
    assert isinstance(resp, dict)
    assert resp["success"] is True


@pytest.mark.asyncio
async def test_validate_platform_no_soft_warns_on_hello_seller() -> None:
    """HelloSeller passes validate_platform without any soft-warn for the
    four RECOMMENDED_METHODS_PER_SPECIALISM methods."""
    import warnings

    from adcp.decisioning.dispatch import validate_platform

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        validate_platform(_hello.HelloSeller())
    soft_warns = [
        x
        for x in w
        if any(
            m in str(x.message)
            for m in [
                "get_media_buys",
                "list_creative_formats",
                "list_creatives",
                "provide_performance_feedback",
            ]
        )
    ]
    assert soft_warns == [], f"Unexpected soft-warns: {[str(x.message) for x in soft_warns]}"
