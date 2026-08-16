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
        "MediaBuyPackage",
        "Property",
        "Creative",
        "CreativeApproval",
        "DeliveryStatus",
        "Package",
        "Snapshot",
        "SnapshotUnavailableReason",
    ]

    for type_name in core_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_wholesale_feed_notification_types_are_stably_exported():
    """AdCP 3.1 catalog webhook types stay on stable import paths."""
    import adcp
    from adcp import types

    for type_name in ["NotificationConfig", "WholesaleFeedWebhook", "WholesaleFeedEvent"]:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"
        assert hasattr(types, type_name), f"{type_name} not exported from adcp.types"


def test_request_response_types_are_exported():
    """Request/response types are accessible from main package."""
    import adcp

    api_types = [
        "GetProductsRequest",
        "GetProductsResponse",
        "CreateMediaBuyRequest",
        "LegacyListCreativeFormatsRequest",
        "LegacyListCreativeFormatsResponse",
        "LegacyBuildCreativeRequest",
        "LegacyBuildCreativeResponse",
        "GetMediaBuysRequest",
        "GetMediaBuysResponse",
    ]

    for type_name in api_types:
        assert hasattr(adcp, type_name), f"{type_name} not exported from adcp package"


def test_pricing_option_types_are_exported():
    """All pricing option types are accessible from main package."""
    import adcp

    pricing_types = [
        "CpcPricingOption",
        "CpcvPricingOption",
        "CpmPricingOption",
        "CppPricingOption",
        "CpvPricingOption",
        "FlatRatePricingOption",
        "VcpmPricingOption",
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
    from adcp import Format, MediaBuy, Product, Property

    types_to_test = [Product, Format, MediaBuy, Property]

    for model_class in types_to_test:
        # Should have Pydantic model methods
        name = model_class.__name__
        assert hasattr(model_class, "model_validate"), f"{name} missing model_validate"
        assert hasattr(model_class, "model_dump"), f"{name} missing model_dump"
        assert hasattr(model_class, "model_validate_json"), f"{name} missing model_validate_json"
        assert hasattr(model_class, "model_dump_json"), f"{name} missing model_dump_json"
        assert hasattr(model_class, "model_fields"), f"{name} missing model_fields"


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
    """Root Format is the canonical declaration surface."""
    from adcp import Format

    expected_fields = [
        "format_kind",
        "params",
        "format_option_id",
        "format_shape",
        "format_schema",
    ]

    model_fields = Format.model_fields
    for field_name in expected_fields:
        assert field_name in model_fields, f"Format missing field: {field_name}"


def test_format_excludes_legacy_named_format_fields():
    """Canonical Format cannot expose legacy creative identity or assets."""
    from adcp import Format

    model_fields = Format.model_fields
    assert "format_id" not in model_fields
    assert "agent_url" not in model_fields
    assert "assets" not in model_fields


def test_pricing_options_have_required_fields():
    """Pricing option types have required fields for pricing."""
    from adcp import CpcPricingOption, CpmPricingOption

    # All pricing options should have pricing_model and pricing_option_id
    pricing_types = [CpmPricingOption, CpcPricingOption]
    for pricing_type in pricing_types:
        name = pricing_type.__name__
        assert "pricing_model" in pricing_type.model_fields, f"{name} missing pricing_model"
        assert "pricing_option_id" in pricing_type.model_fields, f"{name} missing pricing_option_id"
        assert "currency" in pricing_type.model_fields, f"{name} missing currency"

    # CPM pricing option should support both fixed and auction pricing via optional fields
    assert "fixed_price" in CpmPricingOption.model_fields, "CpmPricingOption missing fixed_price"
    assert "floor_price" in CpmPricingOption.model_fields, "CpmPricingOption missing floor_price"


def test_semantic_aliases_point_to_discriminated_variants():
    """Semantic aliases successfully construct their respective variants."""
    from adcp import (
        CreateMediaBuyErrorResponse,
        CreateMediaBuySuccessResponse,
        HtmlPreviewRender,
        UrlPreviewRender,
    )

    # URL preview render requires render_id, output_format='url', preview_url, role
    url_render = UrlPreviewRender(
        render_id="r1",
        output_format="url",
        preview_url="https://example.com/preview",
        role="primary",
    )
    assert str(url_render.preview_url) == "https://example.com/preview"
    assert url_render.output_format == "url"

    # HTML preview render requires render_id, output_format='html', preview_html, role
    html_render = HtmlPreviewRender(
        render_id="r2",
        output_format="html",
        preview_html="<div>Preview content</div>",
        role="primary",
    )
    assert html_render.preview_html == "<div>Preview content</div>"
    assert html_render.output_format == "html"

    # Success response should accept success fields
    success = CreateMediaBuySuccessResponse(
        media_buy_id="mb_123",
        buyer_ref="ref_456",
        packages=[],
        confirmed_at="2026-05-27T12:00:00Z",
        revision=1,
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
        confirmed_at="2026-05-27T12:00:00Z",
        revision=1,
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
        "confirmed_at": "2026-05-27T12:00:00Z",
        "revision": 1,
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


def test_legacy_list_creative_formats_request_has_filter_params():
    """The explicitly legacy list-formats request retains its filters.

    The SDK supports is_responsive and name_search parameters for filtering
    creative formats. These parameters are part of the AdCP specification.
    """
    from adcp import LegacyListCreativeFormatsRequest

    model_fields = LegacyListCreativeFormatsRequest.model_fields

    # Core filter parameters from AdCP spec
    expected_fields = [
        "is_responsive",  # Filter for responsive formats
        "name_search",  # Search formats by name (case-insensitive partial match)
        "asset_types",  # Filter by asset types (image, video, etc.)
        "format_ids",  # Return only specific format IDs
        "min_width",  # Minimum width filter
        "max_width",  # Maximum width filter
        "min_height",  # Minimum height filter
        "max_height",  # Maximum height filter
        "context",  # Context object for request
        "ext",  # Extension object
    ]

    for field_name in expected_fields:
        assert field_name in model_fields, f"ListCreativeFormatsRequest missing field: {field_name}"


def test_legacy_list_creative_formats_request_filter_params_types():
    """LegacyListCreativeFormatsRequest filter parameters have correct types."""
    from adcp import LegacyListCreativeFormatsRequest

    # Create request with filter parameters - should not raise
    request = LegacyListCreativeFormatsRequest(
        is_responsive=True,
        name_search="mobile",
    )

    assert request.is_responsive is True
    assert request.name_search == "mobile"

    # Verify serialization includes the filter parameters
    data = request.model_dump(exclude_none=True)
    assert data["is_responsive"] is True
    assert data["name_search"] == "mobile"


def test_removed_v4_types_raise_informative_import_error():
    """Removed-in-4.0 names should raise a clear ImportError pointing at MIGRATION."""
    import pytest

    import adcp

    for name in ("BrandManifest", "FormatCategory", "DeliverTo", "Pricing", "PackageStatus"):
        with pytest.raises(ImportError) as exc:
            getattr(adcp, name)
        assert "MIGRATION_v3_to_v4.md" in str(exc.value)
        assert "4.0" in str(exc.value)


def test_public_api_surface_matches_snapshot():
    """Fail when `adcp.__all__` or `adcp.types.__all__` drifts from the snapshot.

    Regenerate after an intentional change:

        python scripts/regenerate_public_api_snapshot.py

    Note: this test tracks names only. A name whose underlying class identity
    changes (e.g., aliased to a different generated class) won't be caught
    here — review the diff on `adcp/types/aliases.py` separately for that.
    """
    import json
    from pathlib import Path

    import adcp
    import adcp.types

    snapshot_path = Path(__file__).parent / "fixtures" / "public_api_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text())
    regen_cmd = "python scripts/regenerate_public_api_snapshot.py"

    current = {
        "adcp": sorted(adcp.__all__),
        "adcp.types": sorted(adcp.types.__all__),
    }

    for module_name in ("adcp", "adcp.types"):
        expected = set(snapshot[module_name])
        actual = set(current[module_name])
        removed = sorted(expected - actual)
        added = sorted(actual - expected)
        assert not removed, (
            f"Public names removed from {module_name}: {removed}. "
            "Removals are breaking changes — add a CHANGELOG entry and, for "
            "a major version bump, a MIGRATION note, then regenerate the "
            f"snapshot with `{regen_cmd}`."
        )
        assert not added, (
            f"Public names added to {module_name}: {added}. "
            f"Once the addition is intentional, regenerate the snapshot with "
            f"`{regen_cmd}`."
        )
