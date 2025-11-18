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

from pydantic import AnyUrl

# Import all generated types
from adcp.types.generated import (
    # Core request/response types
    ActivateSignalRequest,
    ActivateSignalResponse,
    BuildCreativeRequest,
    BuildCreativeResponse,
    CreateMediaBuyRequest,
    CreateMediaBuyResponse,
    GetMediaBuyDeliveryRequest,
    GetMediaBuyDeliveryResponse,
    GetProductsRequest,
    GetProductsResponse,
    GetSignalsRequest,
    GetSignalsResponse,
    ListAuthorizedPropertiesRequest,
    ListAuthorizedPropertiesResponse,
    ListCreativeFormatsRequest,
    ListCreativeFormatsResponse,
    ListCreativesRequest,
    ListCreativesResponse,
    PreviewCreativeRequest,
    PreviewCreativeResponse,
    ProvidePerformanceFeedbackRequest,
    ProvidePerformanceFeedbackResponse,
    SyncCreativesRequest,
    SyncCreativesResponse,
    TasksGetRequest,
    TasksGetResponse,
    TasksListRequest,
    TasksListResponse,
    UpdateMediaBuyRequest,
    UpdateMediaBuyResponse,
    # Core domain types
    BrandManifest1,
    Creative,
    CreativeManifest,
    Error,
    Format,
    MediaBuy,
    Package,
    Product,
    Property,
    # Pricing options
    CpcPricingOption,
    CpcvPricingOption,
    CpmAuctionPricingOption,
    CpmFixedRatePricingOption,
    CppPricingOption,
    CpvPricingOption,
    FlatRatePricingOption,
    VcpmAuctionPricingOption,
    VcpmFixedRatePricingOption,
    # Enums and constants
    CreativeStatus,
    MediaBuyStatus,
    PackageStatus,
    PricingModel,
    TaskStatus,
    TaskType,
    # Assets
    AudioAsset,
    CssAsset,
    HtmlAsset,
    ImageAsset,
    JavascriptAsset,
    MarkdownAsset,
    TextAsset,
    UrlAsset,
    VideoAsset,
    WebhookAsset,
)

# Stable aliases for types with numbered variants
# These provide a clean public API that hides schema evolution details
BrandManifest = BrandManifest1

# Union types for oneOf schemas
# BrandManifestReference represents the union from brand-manifest-ref.json:
# either an inline BrandManifest object OR a URL string pointing to a hosted manifest
BrandManifestReference = BrandManifest1 | AnyUrl

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
    "BrandManifest",  # Stable alias for BrandManifest1
    "BrandManifestReference",  # Union type: BrandManifest | AnyUrl
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
