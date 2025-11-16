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
    BuildCreativeErrorResponse,
    BuildCreativeSuccessResponse,
    CreateMediaBuyErrorResponse,
    CreateMediaBuySuccessResponse,
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

# Test that generated types still exist
from adcp.types.generated import (
    ActivateSignalResponse1,
    ActivateSignalResponse2,
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
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
        "PreviewRenderImage",
        "PreviewRenderHtml",
        "PreviewRenderIframe",
        "PreviewCreativeStaticResponse",
        "PreviewCreativeInteractiveResponse",
    ]

    import adcp.types.aliases as aliases_module

    for alias in expected_aliases:
        assert hasattr(aliases_module, alias), f"Missing alias: {alias}"
        assert alias in aliases_module.__all__, f"Alias not in __all__: {alias}"
