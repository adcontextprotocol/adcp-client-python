from __future__ import annotations

"""Utility functions."""

from adcp.utils.format_assets import (
    CanonicalFormatAssetsInput,
    FormatAssetsInput,
    LegacyFormatAssetsInput,
    get_asset_count,
    get_format_assets,
    get_individual_assets,
    get_optional_assets,
    get_repeatable_groups,
    get_required_assets,
    has_assets,
    normalize_assets_required,
    uses_deprecated_assets_field,
)
from adcp.utils.operation_id import create_operation_id

__all__ = [
    "create_operation_id",
    "FormatAssetsInput",
    "CanonicalFormatAssetsInput",
    "LegacyFormatAssetsInput",
    # Format asset utilities
    "get_format_assets",
    "normalize_assets_required",
    "get_required_assets",
    "get_optional_assets",
    "get_individual_assets",
    "get_repeatable_groups",
    "uses_deprecated_assets_field",
    "get_asset_count",
    "has_assets",
]
