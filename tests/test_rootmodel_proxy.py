"""Tests for RootModel __getattr__ proxy.

Validates that RootModel union types proxy attribute access to the wrapped type,
so users don't need .root to access inner model attributes.

See: https://github.com/adcontextprotocol/adcp-client-python/issues/145
"""

from __future__ import annotations

import pytest

from adcp.types import (
    CpmPricingOption,
    DeliveryMeasurement,
    DeliveryType,
    FlatRatePricingOption,
    Format,
    Product,
    ReportingCapabilities,
)


@pytest.fixture
def product_with_pricing() -> Product:
    """Create a Product with pricing options for testing."""
    return Product(
        product_id="test",
        name="Test Product",
        description="A test product",
        publisher_properties=[{"publisher_domain": "example.com", "selection_type": "all"}],
        format_options=[Format(format_kind="image", params={"width": 300, "height": 250})],
        delivery_type=DeliveryType.guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Test"),
        reporting_capabilities=ReportingCapabilities(
            available_metrics=[],
            available_reporting_frequencies=["daily"],
            date_range_support="date_range",
            expected_delay_minutes=60,
            supports_webhooks=False,
            timezone="UTC",
        ),
        pricing_options=[
            FlatRatePricingOption(
                pricing_option_id="flat_1",
                pricing_model="flat_rate",
                fixed_price=100.0,
                currency="USD",
            ),
            CpmPricingOption(
                pricing_option_id="cpm_1",
                pricing_model="cpm",
                floor_price=2.50,
                currency="USD",
            ),
        ],
    )


def test_pricing_option_attribute_access(product_with_pricing: Product):
    """PricingOption RootModel proxies attribute access to the inner type."""
    po = product_with_pricing.pricing_options[0]

    assert po.pricing_option_id == "flat_1"
    assert po.pricing_model == "flat_rate"
    assert po.fixed_price == 100.0
    assert po.currency == "USD"


def test_cpm_variant_attribute_access(product_with_pricing: Product):
    """CPM variant attributes are accessible through the proxy."""
    po = product_with_pricing.pricing_options[1]

    assert po.pricing_model == "cpm"
    assert po.floor_price == 2.50
    assert po.pricing_option_id == "cpm_1"


def test_pricing_option_is_the_arm_itself(product_with_pricing: Product):
    """A union root publishes its arms, so an element has no wrapper to unwrap."""
    po = product_with_pricing.pricing_options[0]

    assert type(po).__name__ == "FlatRatePricingOption"
    assert not hasattr(po, "root")


def test_pricing_option_iteration(product_with_pricing: Product):
    """Can iterate pricing options and access attributes directly."""
    models = [po.pricing_model for po in product_with_pricing.pricing_options]
    assert models == ["flat_rate", "cpm"]


def test_pricing_option_missing_attribute_raises(product_with_pricing: Product):
    """Accessing a non-existent attribute still raises AttributeError."""
    po = product_with_pricing.pricing_options[0]

    with pytest.raises(AttributeError):
        po.nonexistent_field


def test_pricing_option_private_attribute_raises(product_with_pricing: Product):
    """Private/dunder attributes are not proxied to root."""
    po = product_with_pricing.pricing_options[0]

    with pytest.raises(AttributeError):
        po._nonexistent


def test_pricing_option_serialization_roundtrip(product_with_pricing: Product):
    """Serialization and validation are unaffected by the __getattr__ proxy."""
    po = product_with_pricing.pricing_options[0]

    dumped = po.model_dump(mode="json")
    assert dumped["pricing_option_id"] == "flat_1"
    assert dumped["pricing_model"] == "flat_rate"

    # Round-trip through validation
    restored = type(po).model_validate(dumped)
    assert restored.pricing_option_id == "flat_1"
    assert restored.pricing_model == "flat_rate"


def test_retained_rootmodel_unions_have_getattr():
    """A union another union discriminates against keeps its wrapper and its proxy.

    ``AssetVariant`` discriminates on ``asset_type``, and both ``VastAsset``
    shapes carry ``asset_type='vast'``, so the wrapper is the one choice that
    tag maps to.
    """
    from adcp.types.generated_poc.core.assets.asset_union import VastAsset, VastAsset1

    inner = VastAsset1(
        asset_type="vast", delivery_type="url", asset_id="a-1", url="https://cdn.test/v.xml"
    )
    wrapped = VastAsset(root=inner)

    assert wrapped.asset_type == "vast"
    assert wrapped.delivery_type == "url"
    assert wrapped.asset_id == "a-1"
