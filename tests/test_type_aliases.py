"""Tests for semantic type aliases.

Validates that:
1. All aliases import successfully
2. Aliases point to the correct generated types
3. Aliases can be used for type checking
"""

from __future__ import annotations

# Test that all aliases can be imported from the main package
from adcp import (
    ActivateSignalErrorResponse,
    ActivateSignalSuccessResponse,
    BothPreviewRender,
    BuildCreativeErrorResponse,
    BuildCreativeSuccessResponse,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    TextSubAsset,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
)

# Test that generated types still exist
from adcp.types._generated import (
    ActivateSignalResponse1,
    ActivateSignalResponse2,
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
)

# Test that aliases can also be imported from the aliases module
from adcp.types.aliases import (
    ActivateSignalErrorResponse as AliasActivateSignalErrorResponse,
)
from adcp.types.aliases import (
    ActivateSignalSuccessResponse as AliasActivateSignalSuccessResponse,
)
from adcp.types.aliases import (
    BuildCreativeErrorResponse as AliasBuildCreativeErrorResponse,
)
from adcp.types.aliases import (
    BuildCreativeSuccessResponse as AliasBuildCreativeSuccessResponse,
)
from adcp.types.aliases import (
    CreateMediaBuyErrorResponse as AliasCreateMediaBuyErrorResponse,
)
from adcp.types.aliases import (
    CreateMediaBuySuccessResponse as AliasCreateMediaBuySuccessResponse,
)


def test_aliases_import():
    """Test that all aliases can be imported without errors."""
    # If we got here, the imports succeeded
    assert True


def test_aliases_point_to_correct_types():
    """Test that aliases point to the correct generated types."""
    # Response aliases
    assert ActivateSignalSuccessResponse is ActivateSignalResponse1
    assert ActivateSignalErrorResponse is ActivateSignalResponse2
    assert BuildCreativeSuccessResponse is BuildCreativeResponse1
    assert BuildCreativeErrorResponse is BuildCreativeResponse2
    assert CreateMediaBuySuccessResponse is CreateMediaBuyResponse1
    assert CreateMediaBuyErrorResponse is CreateMediaBuyResponse2


def test_aliases_from_main_module_match_aliases_module():
    """Test that aliases from main module match those from aliases module."""
    assert ActivateSignalSuccessResponse is AliasActivateSignalSuccessResponse
    assert ActivateSignalErrorResponse is AliasActivateSignalErrorResponse
    assert BuildCreativeSuccessResponse is AliasBuildCreativeSuccessResponse
    assert BuildCreativeErrorResponse is AliasBuildCreativeErrorResponse
    assert CreateMediaBuySuccessResponse is AliasCreateMediaBuySuccessResponse
    assert CreateMediaBuyErrorResponse is AliasCreateMediaBuyErrorResponse


def test_aliases_have_docstrings():
    """Test that aliases module has helpful docstrings.

    Note: Type aliases don't preserve docstrings in Python, so we check
    that the module itself has documentation explaining the aliases.
    """
    import adcp.types.aliases as aliases_module

    # Module should have documentation
    assert aliases_module.__doc__ is not None
    assert "semantic" in aliases_module.__doc__.lower()
    assert "alias" in aliases_module.__doc__.lower()


def test_semantic_names_are_meaningful():
    """Test that semantic names convey more meaning than generated names."""
    # The semantic name should be more descriptive
    semantic_name = "CreateMediaBuySuccessResponse"
    generated_name = "CreateMediaBuyResponse1"

    # Semantic names include "Success" or "Error" to indicate the outcome
    assert "Success" in semantic_name or "Error" in semantic_name
    # Generated names just have numbers
    assert generated_name.endswith("1") or generated_name.endswith("2")


def test_all_response_aliases_exported():
    """Test that all expected response type aliases are exported."""
    expected_aliases = [
        # Activate signal
        "ActivateSignalSuccessResponse",
        "ActivateSignalErrorResponse",
        # Build creative
        "BuildCreativeSuccessResponse",
        "BuildCreativeErrorResponse",
        # Create media buy
        "CreateMediaBuySuccessResponse",
        "CreateMediaBuyErrorResponse",
        # Performance feedback
        "ProvidePerformanceFeedbackSuccessResponse",
        "ProvidePerformanceFeedbackErrorResponse",
        # Sync creatives
        "SyncCreativesSuccessResponse",
        "SyncCreativesErrorResponse",
        # Update media buy
        "UpdateMediaBuySuccessResponse",
        "UpdateMediaBuyErrorResponse",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"


def test_all_request_aliases_exported():
    """Test that all expected request type aliases are exported."""
    expected_aliases = [
        "PreviewCreativeFormatRequest",
        "PreviewCreativeManifestRequest",
        "UpdateMediaBuyPackagesRequest",
        "UpdateMediaBuyPropertiesRequest",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"


def test_all_activation_key_aliases_exported():
    """Test that all activation key aliases are exported."""
    expected_aliases = [
        "PropertyIdActivationKey",
        "PropertyTagActivationKey",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"


def test_all_preview_render_aliases_exported():
    """Test that all preview render aliases are exported."""
    expected_aliases = [
        "PreviewCreativeStaticResponse",
        "PreviewCreativeInteractiveResponse",
        # Semantic aliases based on output_format discriminator
        "UrlPreviewRender",
        "HtmlPreviewRender",
        "BothPreviewRender",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"


def test_all_asset_type_aliases_exported():
    """Test that all asset type aliases are exported."""
    expected_aliases = [
        # VAST assets
        "UrlVastAsset",
        "InlineVastAsset",
        # DAAST assets
        "UrlDaastAsset",
        "InlineDaastAsset",
        # SubAssets
        "MediaSubAsset",
        "TextSubAsset",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"


def test_discriminated_union_aliases_point_to_correct_types():
    """Test that discriminated union aliases point to the correct generated types."""
    from adcp.types._generated import (
        DaastAsset1,
        DaastAsset2,
        PreviewRender1,
        PreviewRender2,
        PreviewRender3,
        SubAsset1,
        SubAsset2,
        VastAsset1,
        VastAsset2,
    )

    # Preview renders
    assert UrlPreviewRender is PreviewRender1
    assert HtmlPreviewRender is PreviewRender2
    assert BothPreviewRender is PreviewRender3

    # VAST assets
    assert UrlVastAsset is VastAsset1
    assert InlineVastAsset is VastAsset2

    # DAAST assets
    assert UrlDaastAsset is DaastAsset1
    assert InlineDaastAsset is DaastAsset2

    # SubAssets
    assert MediaSubAsset is SubAsset1
    assert TextSubAsset is SubAsset2


def test_semantic_aliases_can_be_imported_from_main_package():
    """Test that new semantic aliases can be imported from the main adcp package."""
    from adcp import (
        BothPreviewRender as MainBothPreviewRender,
    )
    from adcp import (
        HtmlPreviewRender as MainHtmlPreviewRender,
    )
    from adcp import (
        InlineDaastAsset as MainInlineDaastAsset,
    )
    from adcp import (
        InlineVastAsset as MainInlineVastAsset,
    )
    from adcp import (
        MediaSubAsset as MainMediaSubAsset,
    )
    from adcp import (
        TextSubAsset as MainTextSubAsset,
    )
    from adcp import (
        UrlDaastAsset as MainUrlDaastAsset,
    )
    from adcp import (
        UrlPreviewRender as MainUrlPreviewRender,
    )
    from adcp import (
        UrlVastAsset as MainUrlVastAsset,
    )

    # Verify they match the aliases module exports
    assert MainUrlPreviewRender is UrlPreviewRender
    assert MainHtmlPreviewRender is HtmlPreviewRender
    assert MainBothPreviewRender is BothPreviewRender
    assert MainUrlVastAsset is UrlVastAsset
    assert MainInlineVastAsset is InlineVastAsset
    assert MainUrlDaastAsset is UrlDaastAsset
    assert MainInlineDaastAsset is InlineDaastAsset
    assert MainMediaSubAsset is MediaSubAsset
    assert MainTextSubAsset is TextSubAsset


def test_package_type_aliases_imports():
    """Test that Package type aliases can be imported."""
    from adcp import CreatedPackageReference, Package
    from adcp.types import CreatedPackageReference as TypesCreatedPackageReference
    from adcp.types import Package as TypesPackage
    from adcp.types.aliases import CreatedPackageReference as AliasCreatedPackageReference
    from adcp.types.aliases import Package as AliasPackage

    # Verify all import paths work
    assert Package is TypesPackage
    assert Package is AliasPackage
    assert CreatedPackageReference is TypesCreatedPackageReference
    assert CreatedPackageReference is AliasCreatedPackageReference


def test_package_type_aliases_point_to_correct_modules():
    """Test that Package aliases point to the correct generated types."""
    from adcp import CreatedPackageReference, Package
    from adcp.types.generated_poc.create_media_buy_response import (
        Package as ResponsePackage,
    )
    from adcp.types.generated_poc.package import Package as DomainPackage

    # Package should point to the full domain package
    assert Package is DomainPackage

    # CreatedPackageReference should point to the response package
    assert CreatedPackageReference is ResponsePackage

    # Verify they're different types
    assert Package is not CreatedPackageReference


def test_package_type_aliases_have_correct_fields():
    """Test that Package type aliases have the expected fields."""
    from adcp import CreatedPackageReference, Package

    # Package should have all operational fields
    package_fields = set(Package.__annotations__.keys())
    expected_package_fields = {
        "bid_price",
        "budget",
        "buyer_ref",
        "creative_assignments",
        "format_ids_to_provide",
        "impressions",
        "pacing",
        "package_id",
        "pricing_option_id",
        "product_id",
        "status",
        "targeting_overlay",
    }
    assert package_fields == expected_package_fields, (
        f"Package fields mismatch. "
        f"Expected: {expected_package_fields}, Got: {package_fields}"
    )

    # CreatedPackageReference should only have IDs
    created_fields = set(CreatedPackageReference.__annotations__.keys())
    expected_created_fields = {"buyer_ref", "package_id"}
    assert created_fields == expected_created_fields, (
        f"CreatedPackageReference fields mismatch. "
        f"Expected: {expected_created_fields}, Got: {created_fields}"
    )


def test_package_type_aliases_in_exports():
    """Test that Package type aliases are properly exported."""
    import adcp
    import adcp.types.aliases as aliases_module

    # Check main package exports
    assert hasattr(adcp, "Package")
    assert hasattr(adcp, "CreatedPackageReference")
    assert "Package" in adcp.__all__
    assert "CreatedPackageReference" in adcp.__all__

    # Check aliases module exports
    assert hasattr(aliases_module, "Package")
    assert hasattr(aliases_module, "CreatedPackageReference")
    assert "Package" in aliases_module.__all__
    assert "CreatedPackageReference" in aliases_module.__all__


def test_package_aliases_can_instantiate():
    """Test that Package type aliases can be used to create instances."""
    from adcp import CreatedPackageReference, Package
    from adcp.types import PackageStatus

    # Create a CreatedPackageReference (minimal fields)
    created = CreatedPackageReference(buyer_ref="buyer-123", package_id="pkg-456")
    assert created.buyer_ref == "buyer-123"
    assert created.package_id == "pkg-456"

    # Create a Package (all required fields)
    pkg = Package(package_id="pkg-789", status=PackageStatus.draft)
    assert pkg.package_id == "pkg-789"
    assert pkg.status == PackageStatus.draft
    assert pkg.buyer_ref is None  # Optional field


def test_stable_package_export_is_full_package():
    """Test that stable.py exports the Package as Package."""
    from adcp.types.stable import Package as StablePackage

    # Stable Package should be the full package
    stable_fields = set(StablePackage.__annotations__.keys())
    assert len(stable_fields) == 12, "Stable Package should have 12 fields (full package)"
    assert "budget" in stable_fields
    assert "pricing_option_id" in stable_fields
    assert "product_id" in stable_fields
