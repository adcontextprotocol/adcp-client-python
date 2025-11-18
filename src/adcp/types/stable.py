"""Stable public API for AdCP types.

This module provides a stable, versioned API that shields users from internal
implementation details and schema evolution. All types exported here are
guaranteed to be stable within a major version.

Internal Implementation:
- Types are generated from JSON schemas into adcp.types.generated_poc
- The generator may create numbered variants (e.g., BrandManifest1, BrandManifest2)
  when schema evolution creates multiple valid structures
- This module provides clean, unnumbered aliases pointing to the canonical version

**IMPORTANT**: Never import directly from adcp.types.generated_poc or adcp.types.generated.
Always import from adcp.types or adcp.types.stable.

Schema Evolution:
- When schemas change, we update the alias targets here
- Users see stable names (BrandManifest, Product, etc.)
- Breaking changes require major version bumps
"""

from __future__ import annotations

# Import all generated types from internal consolidated module
from adcp.types._generated import (
    # Core request/response types
    ActivateSignalRequest,
    ActivateSignalResponse,
    # Assets
    AudioAsset,
    # Core domain types
    BrandManifest,  # Clean single type after upstream schema fix
    BuildCreativeRequest,
    BuildCreativeResponse,
    # Pricing options
    CpcPricingOption,
    CpcvPricingOption,
    CpmAuctionPricingOption,
    CpmFixedRatePricingOption,
    CppPricingOption,
    CpvPricingOption,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    Creative,
    CreativeManifest,
    # Enums and constants
    CreativeStatus,
    CssAsset,
    Error,
    FlatRatePricingOption,
    Format,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    HtmlAsset,
    ImageAsset,
    JavascriptAsset,
    ListAuthorizedPropertiesRequest,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    MarkdownAsset,
    MediaBuy,
    MediaBuyStatus,
    Package,
    PackageStatus,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    PricingModel,
    Product,
    Property,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    TasksGetRequest,
    TasksGetResponse,
    TasksListRequest,
    TasksListResponse,
    TaskStatus,
    TaskType,
    TextAsset,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
    UrlAsset,
    VcpmAuctionPricingOption,
    VcpmFixedRatePricingOption,
    VideoAsset,
    WebhookAsset,
)

# Note: BrandManifest is currently split into BrandManifest1/2 due to upstream schema
# using anyOf incorrectly. This will be fixed upstream to create a single BrandManifest type.
# For now, users should use BrandManifest1 (url required) which is most common.

# Note: BrandManifest is now a single clean type
# Re-export BrandManifest directly (no alias needed)

# Re-export all stable types
__all__ = [
    # Request/Response types
    "ActivateSignalRequest",
    "ActivateSignalResponse",
    "BuildCreativeRequest",
    "BuildCreativeResponse",
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "GetMediaBuyDeliveryRequest",
    "GetMediaBuyDeliveryResponse",
    "GetProductsRequest",
    "GetProductsResponse",
    "GetSignalsRequest",
    "GetSignalsResponse",
    "ListAuthorizedPropertiesRequest",
    "ListAuthorizedPropertiesResponse",
    "ListCreativeFormatsRequest",
    "ListCreativeFormatsResponse",
    "ListCreativesRequest",
    "ListCreativesResponse",
    "PreviewCreativeRequest",
    "PreviewCreativeResponse",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "SyncCreativesRequest",
    "SyncCreativesResponse",
    "TasksGetRequest",
    "TasksGetResponse",
    "TasksListRequest",
    "TasksListResponse",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    # Domain types
    "BrandManifest",  # Stable alias for BrandManifest1 (temporary until upstream fix)
    "Creative",
    "CreativeManifest",
    "Error",
    "Format",
    "MediaBuy",
    "Package",
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
    # Status enums
    "CreativeStatus",
    "MediaBuyStatus",
    "PackageStatus",
    "PricingModel",
    "TaskStatus",
    "TaskType",
    # Assets
    "AudioAsset",
    "CssAsset",
    "HtmlAsset",
    "ImageAsset",
    "JavascriptAsset",
    "MarkdownAsset",
    "TextAsset",
    "UrlAsset",
    "VideoAsset",
    "WebhookAsset",
]
