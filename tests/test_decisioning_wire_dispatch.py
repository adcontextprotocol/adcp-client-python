"""Wire-path dispatch tests for the PlatformHandler shims.

Regression for the Emma sales-direct backend test (verdict 2/10): every
wire ``tools/call`` failed with ``'dict' object has no attribute
'account'``. Root cause was a layering bug — ``handler.py`` imported the
Pydantic Request types only inside ``if TYPE_CHECKING:`` while the
dispatcher's :func:`_resolve_params_pydantic_model` calls
``typing.get_type_hints(method)`` at runtime. The forward-ref names
weren't in the module's globals, ``get_type_hints`` raised
``NameError``, the resolver swallowed it (debug log only), and the
dispatcher fell back to the dict path. Handler shims then did
``params.account`` on a dict and 500'd.

Unit tests in ``test_decisioning_handler_shims.py`` passed because they
call shim methods directly, bypassing ``create_tool_caller`` and the
resolver. This file pins the wire path so a future regression can't
slip through.
"""

from __future__ import annotations

import typing
from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.mcp_tools import _resolve_params_pydantic_model, create_tool_caller


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wire-dispatch-")
    yield pool
    pool.shutdown(wait=True)


# ---- Direct unit-level repro ----


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
        "build_creative",
        "get_signals",
        "activate_signal",
        "sync_audiences",
        "check_governance",
        "get_brand_identity",
        "list_content_standards",
        "create_property_list",
        "create_collection_list",
    ],
)
def test_get_type_hints_resolves_for_every_shim(tool_name: str) -> None:
    """``typing.get_type_hints`` must succeed on every shim. Without
    this, the dispatcher's typed-params resolver silently falls back to
    the dict path and the shim's ``params.account`` access blows up at
    runtime with ``'dict' object has no attribute 'account'``."""
    method = getattr(PlatformHandler, tool_name)
    hints = typing.get_type_hints(method)  # MUST NOT raise
    assert "params" in hints, f"{tool_name} missing 'params' annotation"


@pytest.mark.parametrize(
    "tool_name,expected_request",
    [
        ("get_products", "GetProductsRequest"),
        ("create_media_buy", "CreateMediaBuyRequest"),
        ("update_media_buy", "UpdateMediaBuyRequest"),
        ("sync_creatives", "SyncCreativesRequest"),
        ("get_media_buy_delivery", "GetMediaBuyDeliveryRequest"),
        ("build_creative", "BuildCreativeRequest"),
        ("get_signals", "GetSignalsRequest"),
        ("sync_audiences", "SyncAudiencesRequest"),
        ("check_governance", "CheckGovernanceRequest"),
        ("get_brand_identity", "GetBrandIdentityRequest"),
        ("list_content_standards", "ListContentStandardsRequest"),
        ("create_property_list", "CreatePropertyListRequest"),
        ("create_collection_list", "CreateCollectionListRequest"),
    ],
)
def test_resolver_returns_typed_request_class_not_none(
    tool_name: str, expected_request: str
) -> None:
    """The dispatcher's resolver must return the Pydantic request class,
    NOT ``None``. ``None`` triggers the dict-fallback dispatch path that
    causes the wire-side 500."""
    method = getattr(PlatformHandler, tool_name)
    resolved = _resolve_params_pydantic_model(method)
    assert resolved is not None, (
        f"_resolve_params_pydantic_model returned None for {tool_name} — "
        "dispatcher will fall back to dict path and shim will crash on "
        "params.account"
    )
    assert (
        resolved.__name__ == expected_request
    ), f"{tool_name}: expected {expected_request}, got {resolved.__name__}"


# ---- End-to-end wire path ----


class _SalesDirectStub(DecisioningPlatform):
    """Minimal sales-direct platform — covers every required SalesAgent
    method with believable in-memory fixtures."""

    capabilities = DecisioningCapabilities(specialisms=["sales-direct"])
    accounts = SingletonAccounts(account_id="emma-test")

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def get_products(self, req, ctx):
        self.calls.append(("get_products", req))
        return {"products": [{"product_id": "p1", "name": "stub"}]}

    def create_media_buy(self, req, ctx):
        self.calls.append(("create_media_buy", req))
        return {"media_buy_id": "mb_1", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        self.calls.append(("update_media_buy", (media_buy_id, patch)))
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        self.calls.append(("sync_creatives", req))
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        self.calls.append(("get_media_buy_delivery", req))
        return {"media_buy_deliveries": []}


@pytest.mark.asyncio
async def test_wire_dispatch_get_products_does_not_crash(executor) -> None:
    """End-to-end: ``create_tool_caller`` wrapping ``PlatformHandler``
    must dispatch a wire-shape dict payload to the platform method
    without crashing on ``params.account``. This is the exact failure
    mode Emma's sales-direct backend test surfaced."""
    platform = _SalesDirectStub()
    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    caller = create_tool_caller(handler, "get_products")
    wire_payload = {
        "brief": "test campaign",
        "promoted_offering": "shoes",
        "buying_mode": "brief",
    }
    result = await caller(wire_payload)
    assert platform.calls and platform.calls[0][0] == "get_products"
    assert "products" in result


@pytest.mark.asyncio
async def test_wire_dispatch_non_sales_tool_does_not_crash(executor) -> None:
    """Non-sales tool (PR #337's surface): same wire path, same bug.
    Covers the breadth-sprint Protocol families that are most exposed
    by this regression."""

    class _CreativeBuilder(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["creative-generative"])
        accounts = SingletonAccounts(account_id="emma-test")

        def build_creative(self, req, ctx):
            return {"creative_manifest": {"creative_id": "cr_1"}}

    handler = PlatformHandler(
        _CreativeBuilder(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    caller = create_tool_caller(handler, "build_creative")
    result = await caller(
        {"brief": "synthesize a 30s spot", "idempotency_key": "emma-test-build-creative-001"}
    )
    assert "creative_manifest" in result
