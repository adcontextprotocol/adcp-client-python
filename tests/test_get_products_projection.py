"""Tests for the built-in fields projection on get_products responses.

Covers _project_product_fields from adcp.decisioning._get_products_helpers
and the end-to-end handler shim integration.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning._get_products_helpers import (
    _NON_ENUM_PRODUCT_FIELDS,
    _REQUIRED_PRODUCT_FIELDS,
    _project_product_fields,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.base import ToolContext
from adcp.types import GetProductsField, GetProductsRequest, GetProductsResponse, Product

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_MINIMAL_PRODUCT_DICT: dict[str, Any] = {
    "product_id": "p1",
    "name": "Product 1",
    "description": "A test product",
    "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
    "format_options": [
        {
            "format_option_id": "p1-display",
            "format_kind": "image",
            "params": {"sizes": [{"width": 300, "height": 250}]},
        }
    ],
    "delivery_type": "non_guaranteed",
    "pricing_options": [{"pricing_model": "cpm", "pricing_option_id": "po1", "currency": "USD"}],
    "reporting_capabilities": {
        "available_reporting_frequencies": ["daily"],
        "expected_delay_minutes": 60,
        "timezone": "UTC",
        "supports_webhooks": False,
        "available_metrics": ["impressions"],
        "date_range_support": "date_range",
    },
}


def _make_product(**overrides: Any) -> Product:
    return Product.model_validate({**_MINIMAL_PRODUCT_DICT, **overrides})


def _make_response(*products: Product) -> GetProductsResponse:
    return GetProductsResponse(products=list(products))


# ---------------------------------------------------------------------------
# Constant integrity tests — must match the live model
# ---------------------------------------------------------------------------


def test_required_product_fields_matches_model() -> None:
    """_REQUIRED_PRODUCT_FIELDS must equal the set of is_required() fields
    in Product.model_fields.  If the schema is regenerated and a required
    field changes, this test surfaces the drift immediately."""
    live = frozenset(k for k, v in Product.model_fields.items() if v.is_required())
    assert _REQUIRED_PRODUCT_FIELDS == live


def test_required_product_fields_includes_expected_names() -> None:
    """Smoke-check the schema-required product fields."""
    expected = {
        "product_id",
        "name",
        "description",
        "publisher_properties",
        "delivery_type",
        "pricing_options",
        "reporting_capabilities",
    }
    assert expected <= _REQUIRED_PRODUCT_FIELDS


def test_non_enum_product_fields_does_not_overlap_field1() -> None:
    """Non-enum fields must be disjoint from the GetProductsField enum values."""
    field1_vals = frozenset(f.value for f in GetProductsField)
    assert not (_NON_ENUM_PRODUCT_FIELDS & field1_vals)


def test_non_enum_product_fields_includes_pass_through_fields() -> None:
    """Non-selectable optional fields should all be in the pass-through set."""
    expected = {
        "cancellation_policy",
        "ext",
        "material_submission",
        "measurement_readiness",
        "property_targeting_allowed",
        "vendor_metric_optimization",
    }
    assert expected <= _NON_ENUM_PRODUCT_FIELDS


# ---------------------------------------------------------------------------
# Core projection behaviour
# ---------------------------------------------------------------------------


def test_projection_with_all_fields_preserves_everything() -> None:
    """Round-trip with all enum values — output Product has same field set."""
    product = _make_product(channels=["display"])
    response = _make_response(product)
    projected = _project_product_fields(response, list(GetProductsField))
    result = projected.products[0]
    # All declared fields present (same as original)
    assert result.model_dump() == product.model_dump()


def test_projection_drops_unrequested_optional_enum_field() -> None:
    """Requesting only product_id drops channels even when it was set."""
    product = _make_product(channels=["display"])
    response = _make_response(product)
    projected = _project_product_fields(response, [GetProductsField.product_id])
    result = projected.products[0]
    assert result.channels is None


def test_projection_keeps_all_required_fields_even_when_not_requested() -> None:
    """All eight required fields survive even when the buyer only asks for
    product_id — they cannot be absent from a valid Product."""
    product = _make_product()
    response = _make_response(product)
    projected = _project_product_fields(response, [GetProductsField.product_id])
    result = projected.products[0]
    dumped = result.model_dump()
    for field in _REQUIRED_PRODUCT_FIELDS:
        assert dumped[field] is not None, f"Required field {field!r} was dropped"


def test_projection_keeps_product_id_and_name_when_not_in_fields() -> None:
    """product_id and name are always retained per the wire-schema spec."""
    product = _make_product()
    response = _make_response(product)
    # Explicitly do NOT include product_id or name in the requested fields
    fields = [
        f for f in GetProductsField if f not in (GetProductsField.product_id, GetProductsField.name)
    ]
    projected = _project_product_fields(response, fields)
    result = projected.products[0]
    assert result.product_id == "p1"
    assert result.name == "Product 1"


def test_projection_preserves_extra_allow_fields() -> None:
    """Extension fields (extra='allow') must survive the projection."""
    product = _make_product(**{"my_ext_field": "ext_value", "seller_data": {"x": 1}})
    assert product.model_extra == {"my_ext_field": "ext_value", "seller_data": {"x": 1}}
    response = _make_response(product)
    projected = _project_product_fields(response, [GetProductsField.product_id])
    result = projected.products[0]
    dumped = result.model_dump()
    assert dumped.get("my_ext_field") == "ext_value"
    assert dumped.get("seller_data") == {"x": 1}


def test_projection_preserves_non_enum_declared_fields() -> None:
    """Non-enum optional fields like property_targeting_allowed pass through."""
    product = _make_product(property_targeting_allowed=True)
    response = _make_response(product)
    # Only request product_id — non-enum fields should NOT be dropped
    projected = _project_product_fields(response, [GetProductsField.product_id])
    result = projected.products[0]
    assert result.property_targeting_allowed is True


def test_projection_preserves_requested_signal_targeting_allowed() -> None:
    """signal_targeting_allowed is now a selectable GetProductsField value."""
    product = _make_product(signal_targeting_allowed=True)
    response = _make_response(product)
    projected = _project_product_fields(
        response,
        [GetProductsField.product_id, GetProductsField.signal_targeting_allowed],
    )
    result = projected.products[0]
    assert result.signal_targeting_allowed is True


def test_projection_does_not_corrupt_default_false_bool_fields() -> None:
    """property_targeting_allowed / signal_targeting_allowed have default=False.
    Projection must not change them to None."""
    product = _make_product()
    # Both fields default to False (not None).
    assert product.property_targeting_allowed is False
    assert product.signal_targeting_allowed is False
    response = _make_response(product)
    projected = _project_product_fields(response, [GetProductsField.product_id])
    result = projected.products[0]
    assert result.property_targeting_allowed is False
    assert result.signal_targeting_allowed is False


def test_projection_applies_to_all_products_in_response() -> None:
    """Multiple products in the response are each projected."""
    p1 = _make_product(product_id="p1", channels=["display"])
    p2 = _make_product(product_id="p2", channels=["ctv"])
    response = _make_response(p1, p2)
    projected = _project_product_fields(response, [GetProductsField.product_id])
    assert projected.products[0].channels is None
    assert projected.products[1].channels is None
    assert projected.products[0].product_id == "p1"
    assert projected.products[1].product_id == "p2"


def test_projection_preserves_other_response_fields() -> None:
    """proposals, errors, property_list_applied and other response fields
    are untouched by product projection."""
    product = _make_product()
    response = GetProductsResponse(
        products=[product],
        property_list_applied=True,
        errors=None,
    )
    projected = _project_product_fields(response, [GetProductsField.product_id])
    assert projected.property_list_applied is True
    assert projected.errors is None


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> ThreadPoolExecutor:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-proj-")
    yield pool
    pool.shutdown(wait=True)


def _make_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor) -> PlatformHandler:
    return PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())


@pytest.mark.asyncio
async def test_handler_no_fields_returns_response_unchanged(executor) -> None:
    """When params.fields is None the projection is skipped entirely."""
    product = _make_product(channels=["display"])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[product])

    handler = _make_handler(_Platform(), executor)
    req = GetProductsRequest(buying_mode="brief", brief="any")
    assert req.fields is None
    resp = await handler.get_products(req, ToolContext())
    # channels preserved because no projection ran
    assert resp.products[0].channels is not None


@pytest.mark.asyncio
async def test_handler_with_fields_drops_unrequested_optional(executor) -> None:
    """When params.fields is set, the handler applies projection."""
    product = _make_product(channels=["display"])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[product])

    handler = _make_handler(_Platform(), executor)
    req = GetProductsRequest(
        buying_mode="brief",
        brief="any",
        fields=[GetProductsField.product_id],
    )
    resp = await handler.get_products(req, ToolContext())
    assert resp.products[0].channels is None
    assert resp.products[0].product_id == "p1"


@pytest.mark.asyncio
async def test_handler_fields_preserves_required_fields(executor) -> None:
    """Handler projection never drops required fields."""
    product = _make_product()

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[product])

    handler = _make_handler(_Platform(), executor)
    req = GetProductsRequest(
        buying_mode="brief",
        brief="any",
        fields=[GetProductsField.product_id],
    )
    resp = await handler.get_products(req, ToolContext())
    result = resp.products[0]
    dumped = result.model_dump()
    for field in _REQUIRED_PRODUCT_FIELDS:
        assert dumped[field] is not None, f"Handler dropped required field {field!r}"


@pytest.mark.asyncio
async def test_handler_fields_empty_list_is_noop(executor) -> None:
    """Empty fields list (cannot arrive from wire, min_length=1, but defensive)
    must not alter the response."""
    product = _make_product(channels=["display"])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[product])

    handler = _make_handler(_Platform(), executor)
    # Manually construct request with empty fields list to test the guard
    req = GetProductsRequest(buying_mode="brief", brief="any")
    object.__setattr__(req, "fields", [])  # bypass Pydantic min_length=1
    resp = await handler.get_products(req, ToolContext())
    # Guard: `if params.fields:` is False for empty list — no projection
    assert resp.products[0].channels is not None
