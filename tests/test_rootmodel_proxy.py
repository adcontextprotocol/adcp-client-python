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
    FormatId,
    PlatformDeployment,
    Product,
    PublisherPropertiesAll,
)


@pytest.fixture
def product_with_pricing() -> Product:
    """Create a Product with pricing options for testing."""
    return Product(
        product_id="test",
        name="Test Product",
        description="A test product",
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="example.com", selection_type="all"
            )
        ],
        format_ids=[FormatId(agent_url="http://localhost", id="display_300x250")],
        delivery_type=DeliveryType.guaranteed,
        delivery_measurement=DeliveryMeasurement(provider="Test"),
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


def test_pricing_option_root_still_works(product_with_pricing: Product):
    """Existing .root access pattern is unaffected."""
    po = product_with_pricing.pricing_options[0]

    assert po.root.pricing_option_id == "flat_1"
    assert po.root.pricing_model == "flat_rate"


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


def test_other_rootmodel_unions_have_getattr():
    """Other RootModel union types also proxy attribute access."""
    from adcp.types.generated_poc.core.deployment import Deployment

    dep = PlatformDeployment(type="platform", platform="the-trade-desk", is_live=True)
    wrapped = Deployment(root=dep)

    assert wrapped.type == "platform"
    assert wrapped.platform == "the-trade-desk"
    assert wrapped.is_live is True
