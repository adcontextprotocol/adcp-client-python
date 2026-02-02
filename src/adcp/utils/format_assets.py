"""Format Asset Utilities.

Provides utilities for working with format assets from the `assets` field.

Example:
    ```python
    from adcp import Format
    from adcp.utils.format_assets import get_format_assets, get_required_assets

    # Get all assets from a format
    all_assets = get_format_assets(format)

    # Get only required assets
    required = get_required_assets(format)
    ```
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, Union

from adcp.types.generated_poc.core.format import Assets as AssetsModel
from adcp.types.generated_poc.core.format import Assets5 as Assets5Model

if TYPE_CHECKING:
    from adcp.types.generated_poc.core.format import Assets, Assets5, Format

# Type alias for any format asset (individual or repeatable group)
FormatAsset = Union["Assets", "Assets5"]


def get_format_assets(format: Format) -> list[FormatAsset]:
    """Get assets from a Format.

    Returns the list of assets from the format's `assets` field.
    Returns empty list if no assets are defined (flexible format with no assets).

    Args:
        format: The Format object from list_creative_formats response

    Returns:
        List of assets

    Example:
        ```python
        formats = await agent.simple.list_creative_formats()
        for format in formats.formats:
            assets = get_format_assets(format)
            print(f"{format.name} has {len(assets)} assets")
        ```
    """
    if format.assets and len(format.assets) > 0:
        return list(format.assets)

    return []


def normalize_assets_required(assets_required: list[Any]) -> list[FormatAsset]:
    """Convert deprecated assets_required to new assets format.

    .. deprecated:: 3.2.0
        The ``assets_required`` field was removed in ADCP 3.0.0-beta.2.
        This function will be removed in a future version.

    All assets in assets_required are required by definition (that's why they were in
    that array). The new `assets` field has an explicit `required: boolean` to allow
    both required AND optional assets.

    Args:
        assets_required: The deprecated assets_required array

    Returns:
        Normalized assets as Pydantic models with explicit required=True
    """
    warnings.warn(
        "normalize_assets_required() is deprecated. "
        "The assets_required field was removed in ADCP 3.0.0-beta.2. "
        "This function will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2,
    )
    normalized: list[FormatAsset] = []
    for asset in assets_required:
        # Get asset data as dict
        if isinstance(asset, dict):
            asset_dict = asset
        else:
            asset_dict = asset.model_dump() if hasattr(asset, "model_dump") else dict(asset)

        # Check if it's a repeatable group (has asset_group_id) or individual asset
        if "asset_group_id" in asset_dict:
            # Repeatable group - use Assets5Model
            normalized.append(Assets5Model(**{**asset_dict, "required": True}))
        else:
            # Individual asset - use AssetsModel
            normalized.append(AssetsModel(**{**asset_dict, "required": True}))

    return normalized


def get_required_assets(format: Format) -> list[FormatAsset]:
    """Get only required assets from a Format.

    Args:
        format: The Format object

    Returns:
        List of required assets only

    Example:
        ```python
        required_assets = get_required_assets(format)
        print(f"Must provide {len(required_assets)} assets")
        ```
    """
    return [asset for asset in get_format_assets(format) if _is_required(asset)]


def get_optional_assets(format: Format) -> list[FormatAsset]:
    """Get only optional assets from a Format.

    Note: When using deprecated `assets_required`, this will always return empty
    since assets_required only contained required assets.

    Args:
        format: The Format object

    Returns:
        List of optional assets only

    Example:
        ```python
        optional_assets = get_optional_assets(format)
        print(f"Can optionally provide {len(optional_assets)} additional assets")
        ```
    """
    return [asset for asset in get_format_assets(format) if not _is_required(asset)]


def get_individual_assets(format: Format) -> list[FormatAsset]:
    """Get individual assets (not repeatable groups) from a Format.

    Args:
        format: The Format object

    Returns:
        List of individual assets (item_type='individual')
    """
    return [asset for asset in get_format_assets(format) if _get_item_type(asset) == "individual"]


def get_repeatable_groups(format: Format) -> list[FormatAsset]:
    """Get repeatable asset groups from a Format.

    Args:
        format: The Format object

    Returns:
        List of repeatable asset groups (item_type='repeatable_group')
    """
    return [
        asset for asset in get_format_assets(format) if _get_item_type(asset) == "repeatable_group"
    ]


def uses_deprecated_assets_field(format: Format) -> bool:
    """Check if format uses deprecated assets_required field.

    .. deprecated:: 3.2.0
        The ``assets_required`` field was removed in ADCP 3.0.0-beta.2.
        This function always returns False and will be removed in a future version.

    Args:
        format: The Format object

    Returns:
        Always False (deprecated field no longer exists)
    """
    warnings.warn(
        "uses_deprecated_assets_field() is deprecated and always returns False. "
        "The assets_required field was removed in ADCP 3.0.0-beta.2. "
        "This function will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2,
    )
    return False


def get_asset_count(format: Format) -> int:
    """Get the count of assets in a format (for display purposes).

    Args:
        format: The Format object

    Returns:
        Number of assets, or 0 if none defined
    """
    return len(get_format_assets(format))


def has_assets(format: Format) -> bool:
    """Check if a format has any assets defined.

    Args:
        format: The Format object

    Returns:
        True if format has assets, False otherwise
    """
    return get_asset_count(format) > 0


# Internal helpers


def _is_required(asset: FormatAsset) -> bool:
    """Check if an asset is required."""
    return getattr(asset, "required", False) is True


def _get_item_type(asset: FormatAsset) -> str:
    """Get the item_type of an asset."""
    return getattr(asset, "item_type", "individual")
