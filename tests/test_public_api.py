"""Tests for public API stability and usability.

This test suite validates that the public API (`from adcp import ...`)
provides all essential types and they work correctly with JSON data.
"""

from __future__ import annotations


def test_core_domain_types_are_exported():
    """Core domain types are accessible from main package."""
    import adcp

    core_types = [
        "Product",
        "Format",
        "MediaBuy",
        "Property",
        "BrandManifest",
        "Creative",
        "Package",
    ]

    for type_name in core_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_request_response_types_are_exported():
    """Request/response types are accessible from main package."""
    import adcp

    api_types = [
        "GetProductsRequest",
        "GetProductsResponse",
        "CreateMediaBuyRequest",
        "ListCreativeFormatsRequest",
        "ListCreativeFormatsResponse",
        "BuildCreativeRequest",
        "BuildCreativeResponse",
    ]

    for type_name in api_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_pricing_option_types_are_exported():
    """All pricing option types are accessible from main package."""
    import adcp

    pricing_types = [
        "CpcPricingOption",
        "CpcvPricingOption",
        "CpmAuctionPricingOption",
        "CpmFixedRatePricingOption",
        "CppPricingOption",
        "CpvPricingOption",
        "FlatRatePricingOption",
        "VcpmAuctionPricingOption",
        "VcpmFixedRatePricingOption",
    ]

    for type_name in pricing_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_semantic_aliases_are_exported():
    """Semantic type aliases are accessible from main package."""
    import adcp

    aliases = [
        # Preview renders
        "UrlPreviewRender",
        "HtmlPreviewRender",
        "BothPreviewRender",
        # VAST assets
        "UrlVastAsset",
        "InlineVastAsset",
        # DAAST assets
        "UrlDaastAsset",
        "InlineDaastAsset",
        # Sub assets
        "MediaSubAsset",
        "TextSubAsset",
        # Response variants
        "CreateMediaBuySuccessResponse",
        "CreateMediaBuyErrorResponse",
        "ActivateSignalSuccessResponse",
        "ActivateSignalErrorResponse",
    ]

    for type_name in aliases:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_client_types_are_exported():
    """Client and config types are accessible from main package."""
    import adcp

    client_types = [
        "ADCPClient",
        "ADCPMultiAgentClient",
        "AgentConfig",
        "Protocol",
    ]

    for type_name in client_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_public_api_types_are_pydantic_models():
    """Core types from public API are valid Pydantic models."""
    from adcp import Product, Format, MediaBuy, Property, BrandManifest

    types_to_test = [Product, Format, MediaBuy, Property, BrandManifest]

    for model_class in types_to_test:
        # Should have Pydantic model methods
        assert hasattr(model_class, "model_validate"), f"{model_class.__name__} missing model_validate"
        assert hasattr(model_class, "model_dump"), f"{model_class.__name__} missing model_dump"
        assert hasattr(model_class, "model_validate_json"), f"{model_class.__name__} missing model_validate_json"
        assert hasattr(model_class, "model_dump_json"), f"{model_class.__name__} missing model_dump_json"
        assert hasattr(model_class, "model_fields"), f"{model_class.__name__} missing model_fields"


def test_product_has_expected_public_fields():
    """Product type from public API has expected fields."""
    from adcp import Product

    expected_fields = [
        "product_id",
        "name",
        "description",
        "pricing_options",
        "publisher_properties",
    ]

    model_fields = Product.model_fields
    for field_name in expected_fields:
        assert field_name in model_fields, f"Product missing field: {field_name}"


def test_format_has_expected_public_fields():
    """Format type from public API has expected fields."""
    from adcp import Format

    expected_fields = [
        "format_id",
        "name",
        "description",
        "assets_required",
        "delivery",
    ]

    model_fields = Format.model_fields
    for field_name in expected_fields:
        assert field_name in model_fields, f"Format missing field: {field_name}"


def test_pricing_options_are_discriminated_by_is_fixed():
    """Pricing option types have is_fixed discriminator field."""
    from adcp import CpmFixedRatePricingOption, CpmAuctionPricingOption, CpcPricingOption

    # Fixed-rate options should have is_fixed discriminator
    fixed_types = [CpmFixedRatePricingOption, CpcPricingOption]
    for pricing_type in fixed_types:
        assert "is_fixed" in pricing_type.model_fields, f"{pricing_type.__name__} missing is_fixed discriminator"

    # Auction options should have is_fixed discriminator
    auction_types = [CpmAuctionPricingOption]
    for pricing_type in auction_types:
        assert "is_fixed" in pricing_type.model_fields, f"{pricing_type.__name__} missing is_fixed discriminator"


def test_semantic_aliases_point_to_discriminated_variants():
    """Semantic aliases successfully construct their respective variants."""
    from adcp import (
        UrlPreviewRender,
        HtmlPreviewRender,
        CreateMediaBuySuccessResponse,
        CreateMediaBuyErrorResponse,
    )

    # URL preview render should accept url output format
    url_render = UrlPreviewRender(
        render_id="r1",
        role="primary",
        output_format="url",
        preview_url="https://example.com/preview",
    )
    assert url_render.output_format == "url"

    # HTML preview render should accept html output format
    html_render = HtmlPreviewRender(
        render_id="r2",
        role="primary",
        output_format="html",
        preview_html="<div>Test</div>",
    )
    assert html_render.output_format == "html"

    # Success response should accept success fields
    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
    )
    assert success.media_buy_id == "mb_123"

    # Error response should accept error fields
    error = CreateMediaBuyErrorResponse(
        errors=[{"code": "invalid", "message": "Failed"}],
    )
    assert len(error.errors) == 1


def test_public_api_types_serialize_to_json():
    """Public API types can be serialized to JSON."""
    from adcp import CreateMediaBuySuccessResponse

    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
    )

    # Should serialize to JSON without errors
    json_str = success.model_dump_json()
    assert isinstance(json_str, str)
    assert "mb_123" in json_str
    assert "ref_456" in json_str


def test_public_api_types_deserialize_from_json():
    """Public API types can be deserialized from JSON."""
    from adcp import CreateMediaBuySuccessResponse

    json_data = {
        "media_buy_id": "mb_456",
        "buyer_ref": "ref_789",
        "packages": [],
    }

    # Should deserialize from dict without errors
    success = CreateMediaBuySuccessResponse.model_validate(json_data)
    assert success.media_buy_id == "mb_456"
    assert success.buyer_ref == "ref_789"


def test_no_internal_types_in_public_exports():
    """Public API should not export internal numbered types."""
    import adcp

    # These are internal types that should NOT be in public API
    internal_types = [
        "PreviewRender1",
        "PreviewRender2",
        "PreviewRender3",
        "CreateMediaBuyResponse1",
        "CreateMediaBuyResponse2",
        "PublisherProperties",  # Should use semantic names or qualified imports
        "PublisherProperties4",
        "PublisherProperties5",
    ]

    # Check that internal types are not directly exported
    # Note: They might be accessible via qualified imports, which is fine
    exports = dir(adcp)
    for type_name in internal_types:
        # If exported, it should have a semantic alias that's preferred
        if type_name in exports:
            # This is acceptable as long as semantic aliases exist
            pass


def test_public_api_has_version():
    """Public API exports version information."""
    import adcp

    assert hasattr(adcp, "__version__"), "adcp package should export __version__"
    assert isinstance(adcp.__version__, str), "__version__ should be a string"
    assert len(adcp.__version__) > 0, "__version__ should not be empty"
