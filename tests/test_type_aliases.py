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


def test_publisher_properties_aliases_imports():
    """Test that PublisherProperties aliases can be imported."""
    from adcp import (
        PropertyId,
        PropertyTag,
        PublisherPropertiesAll,
        PublisherPropertiesById,
        PublisherPropertiesByTag,
    )
    from adcp.types.aliases import (
        PropertyId as AliasPropertyId,
    )
    from adcp.types.aliases import (
        PropertyTag as AliasPropertyTag,
    )
    from adcp.types.aliases import (
        PublisherPropertiesAll as AliasPublisherPropertiesAll,
    )
    from adcp.types.aliases import (
        PublisherPropertiesById as AliasPublisherPropertiesById,
    )
    from adcp.types.aliases import (
        PublisherPropertiesByTag as AliasPublisherPropertiesByTag,
    )

    # Verify all import paths work
    assert PropertyId is AliasPropertyId
    assert PropertyTag is AliasPropertyTag
    assert PublisherPropertiesAll is AliasPublisherPropertiesAll
    assert PublisherPropertiesById is AliasPublisherPropertiesById
    assert PublisherPropertiesByTag is AliasPublisherPropertiesByTag


def test_publisher_properties_aliases_point_to_correct_types():
    """Test that PublisherProperties aliases point to the correct generated types."""
    from adcp import PublisherPropertiesAll, PublisherPropertiesById, PublisherPropertiesByTag
    from adcp.types.generated_poc.product import (
        PublisherProperties,
        PublisherProperties4,
        PublisherProperties5,
    )

    # Verify aliases point to correct types
    assert PublisherPropertiesAll is PublisherProperties
    assert PublisherPropertiesById is PublisherProperties4
    assert PublisherPropertiesByTag is PublisherProperties5

    # Verify they're different types
    assert PublisherPropertiesAll is not PublisherPropertiesById
    assert PublisherPropertiesAll is not PublisherPropertiesByTag
    assert PublisherPropertiesById is not PublisherPropertiesByTag


def test_publisher_properties_aliases_have_correct_discriminators():
    """Test that PublisherProperties aliases have the correct discriminator values."""
    from adcp import PublisherPropertiesAll, PublisherPropertiesById, PublisherPropertiesByTag

    # Check that discriminator field has correct literal type
    all_selection_type = PublisherPropertiesAll.__annotations__["selection_type"]
    by_id_selection_type = PublisherPropertiesById.__annotations__["selection_type"]
    by_tag_selection_type = PublisherPropertiesByTag.__annotations__["selection_type"]

    # Verify the annotations contain Literal types
    assert "selection_type" in PublisherPropertiesAll.__annotations__
    assert "selection_type" in PublisherPropertiesById.__annotations__
    assert "selection_type" in PublisherPropertiesByTag.__annotations__


def test_publisher_properties_aliases_can_instantiate():
    """Test that PublisherProperties aliases can be used to create instances."""
    from adcp import (
        PropertyId,
        PropertyTag,
        PublisherPropertiesAll,
        PublisherPropertiesById,
        PublisherPropertiesByTag,
    )

    # Create PublisherPropertiesAll
    props_all = PublisherPropertiesAll(
        publisher_domain="example.com", selection_type="all"
    )
    assert props_all.publisher_domain == "example.com"
    assert props_all.selection_type == "all"

    # Create PublisherPropertiesById
    props_by_id = PublisherPropertiesById(
        publisher_domain="example.com",
        selection_type="by_id",
        property_ids=[PropertyId("homepage"), PropertyId("sports")],
    )
    assert props_by_id.publisher_domain == "example.com"
    assert props_by_id.selection_type == "by_id"
    assert len(props_by_id.property_ids) == 2

    # Create PublisherPropertiesByTag
    props_by_tag = PublisherPropertiesByTag(
        publisher_domain="example.com",
        selection_type="by_tag",
        property_tags=[PropertyTag("premium"), PropertyTag("video")],
    )
    assert props_by_tag.publisher_domain == "example.com"
    assert props_by_tag.selection_type == "by_tag"
    assert len(props_by_tag.property_tags) == 2


def test_publisher_properties_aliases_in_exports():
    """Test that PublisherProperties aliases are properly exported."""
    import adcp
    import adcp.types.aliases as aliases_module

    # Check main package exports
    assert hasattr(adcp, "PropertyId")
    assert hasattr(adcp, "PropertyTag")
    assert hasattr(adcp, "PublisherPropertiesAll")
    assert hasattr(adcp, "PublisherPropertiesById")
    assert hasattr(adcp, "PublisherPropertiesByTag")

    assert "PropertyId" in adcp.__all__
    assert "PropertyTag" in adcp.__all__
    assert "PublisherPropertiesAll" in adcp.__all__
    assert "PublisherPropertiesById" in adcp.__all__
    assert "PublisherPropertiesByTag" in adcp.__all__

    # Check aliases module exports
    assert hasattr(aliases_module, "PropertyId")
    assert hasattr(aliases_module, "PropertyTag")
    assert hasattr(aliases_module, "PublisherPropertiesAll")
    assert hasattr(aliases_module, "PublisherPropertiesById")
    assert hasattr(aliases_module, "PublisherPropertiesByTag")

    assert "PropertyId" in aliases_module.__all__
    assert "PropertyTag" in aliases_module.__all__
    assert "PublisherPropertiesAll" in aliases_module.__all__
    assert "PublisherPropertiesById" in aliases_module.__all__
    assert "PublisherPropertiesByTag" in aliases_module.__all__


def test_property_id_and_tag_are_root_models():
    """Test that PropertyId and PropertyTag are properly constrained string types."""
    from adcp import PropertyId, PropertyTag

    # Create valid PropertyId and PropertyTag
    prop_id = PropertyId("my_property_id")
    prop_tag = PropertyTag("premium")

    # Verify they are created successfully
    assert prop_id.root == "my_property_id"
    assert prop_tag.root == "premium"

    # PropertyTag should be a subclass of PropertyId
    assert issubclass(PropertyTag, PropertyId)


def test_deployment_aliases_imports():
    """Test that Deployment aliases can be imported."""
    from adcp import AgentDeployment, PlatformDeployment
    from adcp.types.aliases import AgentDeployment as AliasAgentDeployment
    from adcp.types.aliases import PlatformDeployment as AliasPlatformDeployment

    # Verify all import paths work
    assert PlatformDeployment is AliasPlatformDeployment
    assert AgentDeployment is AliasAgentDeployment


def test_deployment_aliases_point_to_correct_types():
    """Test that Deployment aliases point to the correct generated types."""
    from adcp import AgentDeployment, PlatformDeployment
    from adcp.types.generated_poc.deployment import Deployment1, Deployment2

    # Verify aliases point to correct types
    assert PlatformDeployment is Deployment1
    assert AgentDeployment is Deployment2

    # Verify they're different types
    assert PlatformDeployment is not AgentDeployment


def test_deployment_aliases_can_instantiate():
    """Test that Deployment aliases can be used to create instances."""
    from adcp import AgentDeployment, PlatformDeployment
    from datetime import datetime, timezone

    # Create PlatformDeployment
    platform_deployment = PlatformDeployment(
        type="platform", platform="the-trade-desk", is_live=True
    )
    assert platform_deployment.type == "platform"
    assert platform_deployment.platform == "the-trade-desk"
    assert platform_deployment.is_live is True

    # Create AgentDeployment
    agent_deployment = AgentDeployment(
        type="agent", agent_url="https://agent.example.com", is_live=False
    )
    assert agent_deployment.type == "agent"
    assert str(agent_deployment.agent_url) == "https://agent.example.com/"
    assert agent_deployment.is_live is False


def test_destination_aliases_imports():
    """Test that Destination aliases can be imported."""
    from adcp import AgentDestination, PlatformDestination
    from adcp.types.aliases import AgentDestination as AliasAgentDestination
    from adcp.types.aliases import PlatformDestination as AliasPlatformDestination

    # Verify all import paths work
    assert PlatformDestination is AliasPlatformDestination
    assert AgentDestination is AliasAgentDestination


def test_destination_aliases_point_to_correct_types():
    """Test that Destination aliases point to the correct generated types."""
    from adcp import AgentDestination, PlatformDestination
    from adcp.types.generated_poc.destination import Destination1, Destination2

    # Verify aliases point to correct types
    assert PlatformDestination is Destination1
    assert AgentDestination is Destination2

    # Verify they're different types
    assert PlatformDestination is not AgentDestination


def test_destination_aliases_can_instantiate():
    """Test that Destination aliases can be used to create instances."""
    from adcp import AgentDestination, PlatformDestination

    # Create PlatformDestination
    platform_dest = PlatformDestination(type="platform", platform="amazon-dsp")
    assert platform_dest.type == "platform"
    assert platform_dest.platform == "amazon-dsp"

    # Create AgentDestination
    agent_dest = AgentDestination(type="agent", agent_url="https://agent.example.com")
    assert agent_dest.type == "agent"
    assert str(agent_dest.agent_url) == "https://agent.example.com/"


def test_deployment_destination_aliases_in_exports():
    """Test that Deployment and Destination aliases are properly exported."""
    import adcp
    import adcp.types.aliases as aliases_module

    # Check main package exports
    assert hasattr(adcp, "PlatformDeployment")
    assert hasattr(adcp, "AgentDeployment")
    assert hasattr(adcp, "PlatformDestination")
    assert hasattr(adcp, "AgentDestination")

    assert "PlatformDeployment" in adcp.__all__
    assert "AgentDeployment" in adcp.__all__
    assert "PlatformDestination" in adcp.__all__
    assert "AgentDestination" in adcp.__all__

    # Check aliases module exports
    assert hasattr(aliases_module, "PlatformDeployment")
    assert hasattr(aliases_module, "AgentDeployment")
    assert hasattr(aliases_module, "PlatformDestination")
    assert hasattr(aliases_module, "AgentDestination")

    assert "PlatformDeployment" in aliases_module.__all__
    assert "AgentDeployment" in aliases_module.__all__
    assert "PlatformDestination" in aliases_module.__all__
    assert "AgentDestination" in aliases_module.__all__
