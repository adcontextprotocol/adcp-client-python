from __future__ import annotations

"""Type definitions for AdCP client.

This module provides the public API for AdCP types. All types are imported from
the stable API layer which provides consistent naming regardless of internal
schema evolution.

**IMPORTANT**: Never import directly from adcp.types.generated_poc. Always use
adcp.types or adcp.types.stable for stable, versioned types.
"""

from adcp.types.aliases import (
    BothPreviewRender,
    HtmlPreviewRender,
    InlineDaastAsset,
    InlineVastAsset,
    MediaSubAsset,
    TextSubAsset,
    UrlDaastAsset,
    UrlPreviewRender,
    UrlVastAsset,
)
from adcp.types.base import AdCPBaseModel
from adcp.types.core import (
    Activity,
    ActivityType,
    AgentConfig,
    DebugInfo,
    Protocol,
    TaskResult,
    WebhookMetadata,
)
from adcp.types.core import (
    TaskStatus as CoreTaskStatus,
)

# Import stable public API types
from adcp.types.stable import (
    BrandManifest,
    # Pricing options
    CpcPricingOption,
    CpcvPricingOption,
    CpmAuctionPricingOption,
    CpmFixedRatePricingOption,
    CppPricingOption,
    CpvPricingOption,
    Creative,
    CreativeStatus,
    Error,
    FlatRatePricingOption,
    Format,
    MediaBuy,
    MediaBuyStatus,
    Package,
    PackageStatus,
    PricingModel,
    Product,
    Property,
    VcpmAuctionPricingOption,
    VcpmFixedRatePricingOption,
)

# Note: CoreTaskStatus is for internal task tracking
# Generated TaskStatus from AdCP schema is available via adcp.types.stable
TaskStatus = CoreTaskStatus

__all__ = [
    # Base types
    "AdCPBaseModel",
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
    "Activity",
    "ActivityType",
    "DebugInfo",
    # Semantic aliases for discriminated unions
    "BothPreviewRender",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "TextSubAsset",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
    # Stable API types (commonly used)
    "BrandManifest",
    "Creative",
    "CreativeStatus",
    "Error",
    "Format",
    "MediaBuy",
    "MediaBuyStatus",
    "Package",
    "PackageStatus",
    "PricingModel",
    "Product",
    "Property",
    # Pricing options
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
