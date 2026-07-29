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
from typing import Any

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


# Compute the shim list dynamically from the class's ``advertised_tools``
# so any new shim added in a future PR auto-extends regression coverage —
# a contributor can't add a new tool with its Request type stuck under
# ``TYPE_CHECKING`` and have CI miss it.
_ALL_SHIMS = sorted(PlatformHandler.advertised_tools)


def _shim_method(tool_name: str):
    """Map the legacy wire task to its deliberately explicit SDK method."""
    if tool_name == "list_creative_formats":
        tool_name = "list_creative_formats_legacy"
    return getattr(PlatformHandler, tool_name)


# ---- Direct unit-level repro ----


@pytest.mark.parametrize("tool_name", _ALL_SHIMS)
def test_get_type_hints_resolves_for_every_shim(tool_name: str) -> None:
    """``typing.get_type_hints`` must succeed on every shim. Without
    this, the dispatcher's typed-params resolver silently falls back to
    the dict path and the shim's ``params.account`` access blows up at
    runtime with ``'dict' object has no attribute 'account'``."""
    method = _shim_method(tool_name)
    hints = typing.get_type_hints(method)  # MUST NOT raise
    assert "params" in hints, f"{tool_name} missing 'params' annotation"


@pytest.mark.parametrize("tool_name", _ALL_SHIMS)
def test_resolver_returns_typed_request_class_not_none(tool_name: str) -> None:
    """The dispatcher's resolver must return a Pydantic request class,
    NOT ``None``. ``None`` triggers the dict-fallback dispatch path that
    causes the wire-side 500. Asserts a Pydantic ``BaseModel`` subclass
    rather than a specific class name so this auto-covers new shims."""
    from pydantic import BaseModel

    method = _shim_method(tool_name)
    resolved = _resolve_params_pydantic_model(method)
    assert resolved is not None, (
        f"_resolve_params_pydantic_model returned None for {tool_name} — "
        "dispatcher will fall back to dict path and shim will crash on "
        "params.account"
    )
    assert issubclass(resolved, BaseModel), (
        f"{tool_name}: resolver returned {resolved!r} which is not a " "Pydantic BaseModel subclass"
    )
    assert resolved.__name__.endswith("Request"), (
        f"{tool_name}: resolved class {resolved.__name__!r} doesn't look "
        "like a wire Request type"
    )


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
    # Pin the typed-dispatch contract: the platform method MUST receive
    # a Pydantic ``GetProductsRequest`` instance, NOT a raw dict. The
    # wire-dispatch regression this PR fixes was fundamentally about the
    # dispatcher silently handing a dict through the typed annotation
    # path; without this assertion, a future re-break of the resolver
    # that still routes the dict through would pass.
    from adcp.types import GetProductsRequest

    received = platform.calls[0][1]
    assert isinstance(received, GetProductsRequest), (
        f"platform got {type(received).__name__}, expected GetProductsRequest "
        "— resolver regressed to dict-fallback path"
    )
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
    received_req: list[Any] = []
    # Wrap the platform method via __dict__ so we capture what the
    # dispatcher actually delivered post-resolution, before
    # ``_invoke_platform_method`` consumes it.
    orig_build = handler._platform.build_creative

    def _capture(req: Any, ctx: Any) -> Any:
        received_req.append(req)
        return orig_build(req, ctx)

    handler._platform.build_creative = _capture  # type: ignore[method-assign]

    result = await caller(
        {"brief": "synthesize a 30s spot", "idempotency_key": "emma-test-build-creative-001"}
    )
    assert "creative_manifest" in result
    # Same regression guard as get_products — the platform must see a
    # typed ``BuildCreativeRequest``, not the raw wire dict.
    from adcp.types import BuildCreativeRequest

    assert received_req and isinstance(received_req[0], BuildCreativeRequest), (
        f"platform got {type(received_req[0] if received_req else None).__name__}, "
        "expected BuildCreativeRequest"
    )
