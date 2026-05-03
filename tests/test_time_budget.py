"""Tests for adcp.decisioning.time_budget deadline wrapper.

Covers:
- resolve_time_budget unit conversion (seconds / minutes / hours / days)
- resolve_time_budget campaign→None and absent→None
- project_incomplete_response wire shape (min_length=1, scope='products')
- get_products shim: short budget against slow adapter → incomplete[]
- get_products shim: within-budget adapter → unchanged response
- get_products shim: absent time_budget → no deadline
- get_products shim: campaign unit → no deadline
- IncrementalGetProducts / ProductsCheckpoint importable from adcp.decisioning
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    IncrementalGetProducts,
    InMemoryTaskRegistry,
    ProductsCheckpoint,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.time_budget import (
    project_incomplete_response,
    resolve_time_budget,
)
from adcp.server.base import ToolContext
from adcp.types import GetProductsRequest


# ---------------------------------------------------------------------------
# resolve_time_budget
# ---------------------------------------------------------------------------


def test_resolve_time_budget_none_input():
    assert resolve_time_budget(None) is None


@pytest.mark.parametrize(
    "unit, interval, expected",
    [
        ("seconds", 5, 5.0),
        ("minutes", 2, 120.0),
        ("hours", 1, 3600.0),
        ("days", 1, 86400.0),
    ],
)
def test_resolve_time_budget_unit_conversion(unit, interval, expected):
    tb = _make_time_budget(interval=interval, unit=unit)
    assert resolve_time_budget(tb) == expected


def test_resolve_time_budget_campaign_returns_none():
    """unit='campaign' must produce None (no SDK-managed deadline)."""
    tb = _make_time_budget(interval=1, unit="campaign")
    assert resolve_time_budget(tb) is None


def test_resolve_time_budget_enum_unit():
    """Enum-valued unit is normalised to string via .value."""

    class FakeUnit:
        value = "minutes"

    class FakeTB:
        unit = FakeUnit()
        interval = 3

    assert resolve_time_budget(FakeTB()) == 180.0


def test_resolve_time_budget_plain_dict():
    tb = {"interval": 10, "unit": "seconds"}
    assert resolve_time_budget(tb) == 10.0


def test_resolve_time_budget_unknown_unit_returns_none(caplog):
    tb = _make_time_budget(interval=1, unit="lightyears")
    result = resolve_time_budget(tb)
    assert result is None
    assert "Unrecognised time_budget unit" in caplog.text


# ---------------------------------------------------------------------------
# project_incomplete_response
# ---------------------------------------------------------------------------


def test_project_incomplete_response_shape():
    resp = project_incomplete_response(interval=5, unit="seconds")
    assert resp["products"] == []
    # min_length=1 on the wire schema — must have at least one entry
    assert len(resp["incomplete"]) >= 1
    item = resp["incomplete"][0]
    assert item["scope"] == "products"
    assert isinstance(item["description"], str) and item["description"]
    assert "estimated_wait" in item


def test_project_incomplete_response_contains_budget_info():
    resp = project_incomplete_response(interval=30, unit="minutes")
    description = resp["incomplete"][0]["description"]
    assert "30" in description
    assert "minutes" in description


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-tb-")
    yield pool
    pool.shutdown(wait=True)


def _make_platform_with_delay(delay: float):
    """Return a DecisioningPlatform whose get_products sleeps for delay seconds."""

    class _SlowSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="test")

        async def get_products(self, req, ctx):
            await asyncio.sleep(delay)
            return {"products": [{"product_id": "p1", "name": "Product 1"}]}

    return _SlowSeller()


def _make_time_budget(*, interval: int, unit: str):
    """Create a minimal time_budget-like object."""

    class TB:
        pass

    tb = TB()
    tb.interval = interval  # type: ignore[attr-defined]
    tb.unit = unit  # type: ignore[attr-defined]
    return tb


# ---------------------------------------------------------------------------
# get_products shim integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_timeout_returns_incomplete(executor):
    """1s budget against a 10s adapter returns the incomplete[] shape."""
    handler = PlatformHandler(
        _make_platform_with_delay(10.0),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    req = GetProductsRequest.model_construct(
        account=None,
        time_budget=_make_time_budget(interval=1, unit="seconds"),
    )
    result = await handler.get_products(req, context=ToolContext())

    # Must be dict or model with products=[] and incomplete list
    if hasattr(result, "incomplete"):
        # Pydantic model path
        assert result.incomplete is not None and len(result.incomplete) >= 1
        assert result.products == [] or result.products is None or list(result.products) == []  # type: ignore[union-attr]
    else:
        assert isinstance(result, dict)
        assert result["products"] == []
        assert len(result["incomplete"]) >= 1
        assert result["incomplete"][0]["scope"] == "products"


@pytest.mark.asyncio
async def test_get_products_within_budget_passes_through(executor):
    """Adopter that returns within budget passes response through unchanged."""

    class _FastSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="test")

        async def get_products(self, req, ctx):
            return {"products": [{"product_id": "p1", "name": "Fast Product"}]}

    handler = PlatformHandler(
        _FastSeller(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    req = GetProductsRequest.model_construct(
        account=None,
        time_budget=_make_time_budget(interval=10, unit="seconds"),
    )
    result = await handler.get_products(req, context=ToolContext())

    products = result.get("products") if isinstance(result, dict) else list(getattr(result, "products", []))  # type: ignore[union-attr]
    assert len(products) == 1
    pid = products[0].get("product_id") if isinstance(products[0], dict) else products[0].product_id  # type: ignore[union-attr]
    assert pid == "p1"
    # No incomplete key / field when fully resolved
    incomplete = result.get("incomplete") if isinstance(result, dict) else getattr(result, "incomplete", None)
    assert not incomplete


@pytest.mark.asyncio
async def test_get_products_absent_time_budget_no_deadline(executor):
    """When time_budget is absent, the platform runs to completion with no deadline."""

    class _SlowButUnlimited(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="test")

        async def get_products(self, req, ctx):
            await asyncio.sleep(0.05)  # brief delay — no deadline should fire
            return {"products": [{"product_id": "p2", "name": "Unlimited"}]}

    handler = PlatformHandler(
        _SlowButUnlimited(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    req = GetProductsRequest.model_construct(account=None, time_budget=None)
    result = await handler.get_products(req, context=ToolContext())
    products = result.get("products") if isinstance(result, dict) else list(getattr(result, "products", []))  # type: ignore[union-attr]
    assert len(products) == 1


@pytest.mark.asyncio
async def test_get_products_campaign_unit_no_deadline(executor):
    """unit='campaign' must not install a deadline; slow platform runs to completion."""

    class _CampaignSeller(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="test")

        async def get_products(self, req, ctx):
            await asyncio.sleep(0.05)
            return {"products": [{"product_id": "p3", "name": "Campaign Product"}]}

    handler = PlatformHandler(
        _CampaignSeller(),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    req = GetProductsRequest.model_construct(
        account=None,
        time_budget=_make_time_budget(interval=1, unit="campaign"),
    )
    result = await handler.get_products(req, context=ToolContext())
    products = result.get("products") if isinstance(result, dict) else list(getattr(result, "products", []))  # type: ignore[union-attr]
    assert len(products) == 1


@pytest.mark.asyncio
async def test_get_products_timeout_logs_warning(executor, caplog):
    """A timeout emits a WARNING with budget info."""
    import logging

    handler = PlatformHandler(
        _make_platform_with_delay(10.0),
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    req = GetProductsRequest.model_construct(
        account=None,
        time_budget=_make_time_budget(interval=1, unit="seconds"),
    )
    with caplog.at_level(logging.WARNING, logger="adcp.decisioning.handler"):
        await handler.get_products(req, context=ToolContext())
    assert any("timed out" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Public API importability
# ---------------------------------------------------------------------------


def test_incremental_get_products_importable_from_decisioning():
    from adcp.decisioning import IncrementalGetProducts as IGP  # noqa: F401

    assert IGP is IncrementalGetProducts


def test_products_checkpoint_importable_from_decisioning():
    from adcp.decisioning import ProductsCheckpoint as PC  # noqa: F401

    assert PC is ProductsCheckpoint


def test_products_checkpoint_accumulates_batches():
    cp = ProductsCheckpoint()
    cp.add_batch({"products": [{"product_id": "a"}]})
    cp.add_batch({"products": [{"product_id": "b"}]})
    assert len(cp.batches) == 2
    assert cp.batches[0]["products"][0]["product_id"] == "a"


def test_incremental_get_products_is_runtime_checkable():
    """Protocol is @runtime_checkable so isinstance works at runtime."""

    class FakeIncremental:
        async def get_products_incremental(self, req, ctx, checkpoint):
            yield {}  # pragma: no cover

    assert isinstance(FakeIncremental(), IncrementalGetProducts)
