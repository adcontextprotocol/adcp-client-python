"""Semantic type aliases for generated AdCP types.

This module provides user-friendly aliases for generated types where the
auto-generated names don't match user expectations from reading the spec.

The code generator (datamodel-code-generator) creates numbered suffixes for
discriminated union variants (e.g., Response1, Response2), but users expect
semantic names (e.g., SuccessResponse, ErrorResponse).

Categories of aliases:

1. Discriminated Union Response Variants
   - Success/Error cases for API responses
   - Named to match the semantic meaning from the spec

2. Preview/Render Types
   - Input/Output/Request/Response variants
   - Numbered types mapped to their semantic purpose

3. Activation Keys
   - Signal activation key variants

DO NOT EDIT the generated types directly - they are regenerated from schemas.
Add aliases here for any types where the generated name is unclear.

Validation:
This module will raise ImportError at import time if any of the referenced
generated types do not exist. This ensures that schema changes are caught
immediately rather than at runtime when users try to use the aliases.
"""

from __future__ import annotations

# Import all generated types that need semantic aliases
from adcp.types._generated import (
    # Activation responses
    ActivateSignalResponse1,
    ActivateSignalResponse2,
    # Activation keys
    ActivationKey1,
    ActivationKey2,
    # Build creative responses
    BuildCreativeResponse1,
    BuildCreativeResponse2,
    # Create media buy responses
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    # DAAST assets
    DaastAsset1,
    DaastAsset2,
    # Preview creative requests
    PreviewCreativeRequest1,
    PreviewCreativeRequest2,
    # Preview creative responses
    PreviewCreativeResponse1,
    PreviewCreativeResponse2,
    # Preview renders
    PreviewRender1,
    PreviewRender2,
    PreviewRender3,
    # Performance feedback responses
    ProvidePerformanceFeedbackResponse1,
    ProvidePerformanceFeedbackResponse2,
    # SubAssets
    SubAsset1,
    SubAsset2,
    # Sync creatives responses
    SyncCreativesResponse1,
    SyncCreativesResponse2,
    # Update media buy requests
    UpdateMediaBuyRequest1,
    UpdateMediaBuyRequest2,
    # Update media buy responses
    UpdateMediaBuyResponse1,
    UpdateMediaBuyResponse2,
    # VAST assets
    VastAsset1,
    VastAsset2,
)

# ============================================================================
# RESPONSE TYPE ALIASES - Success/Error Discriminated Unions
# ============================================================================
# These are atomic operations where the response is EITHER success OR error,
# never both. The numbered suffixes from the generator don't convey this
# critical semantic distinction.

# Activate Signal Response Variants
ActivateSignalSuccessResponse = ActivateSignalResponse1
"""Success response - signal activation succeeded."""

ActivateSignalErrorResponse = ActivateSignalResponse2
"""Error response - signal activation failed."""

# Build Creative Response Variants
BuildCreativeSuccessResponse = BuildCreativeResponse1
"""Success response - creative built successfully, manifest returned."""

BuildCreativeErrorResponse = BuildCreativeResponse2
"""Error response - creative build failed, no manifest created."""

# Create Media Buy Response Variants
CreateMediaBuySuccessResponse = CreateMediaBuyResponse1
"""Success response - media buy created successfully with media_buy_id."""

CreateMediaBuyErrorResponse = CreateMediaBuyResponse2
"""Error response - media buy creation failed, no media buy created."""

# Performance Feedback Response Variants
ProvidePerformanceFeedbackSuccessResponse = ProvidePerformanceFeedbackResponse1
"""Success response - performance feedback accepted."""

ProvidePerformanceFeedbackErrorResponse = ProvidePerformanceFeedbackResponse2
"""Error response - performance feedback rejected."""

# Sync Creatives Response Variants
SyncCreativesSuccessResponse = SyncCreativesResponse1
"""Success response - sync operation processed creatives."""

SyncCreativesErrorResponse = SyncCreativesResponse2
"""Error response - sync operation failed."""

# Update Media Buy Response Variants
UpdateMediaBuySuccessResponse = UpdateMediaBuyResponse1
"""Success response - media buy updated successfully."""

UpdateMediaBuyErrorResponse = UpdateMediaBuyResponse2
"""Error response - media buy update failed, no changes applied."""

# ============================================================================
# REQUEST TYPE ALIASES - Operation Variants
# ============================================================================

# Preview Creative Request Variants
PreviewCreativeFormatRequest = PreviewCreativeRequest1
"""Preview request using format_id to identify creative format."""

PreviewCreativeManifestRequest = PreviewCreativeRequest2
"""Preview request using creative_manifest_url to identify creative."""

# Update Media Buy Request Variants
UpdateMediaBuyPackagesRequest = UpdateMediaBuyRequest1
"""Update request modifying packages in the media buy."""

UpdateMediaBuyPropertiesRequest = UpdateMediaBuyRequest2
"""Update request modifying media buy properties (not packages)."""

# ============================================================================
# ACTIVATION KEY ALIASES
# ============================================================================

PropertyIdActivationKey = ActivationKey1
"""Activation key using property_id for identification."""

PropertyTagActivationKey = ActivationKey2
"""Activation key using property_tags for identification."""

# ============================================================================
# PREVIEW/RENDER TYPE ALIASES
# ============================================================================

# Preview Creative Response Variants
PreviewCreativeStaticResponse = PreviewCreativeResponse1
"""Preview response with static renders (image/HTML snapshots)."""

PreviewCreativeInteractiveResponse = PreviewCreativeResponse2
"""Preview response with interactive renders (iframe embedding)."""

# Preview Render Variants (discriminated by output_format)
UrlPreviewRender = PreviewRender1
"""Preview render with output_format='url' - provides preview_url for iframe embedding."""

HtmlPreviewRender = PreviewRender2
"""Preview render with output_format='html' - provides preview_html for direct embedding."""

BothPreviewRender = PreviewRender3
"""Preview render with output_format='both' - provides both preview_url and preview_html."""

# ============================================================================
# ASSET TYPE ALIASES - Delivery & Kind Discriminated Unions
# ============================================================================

# VAST Asset Variants (discriminated by delivery_type)
UrlVastAsset = VastAsset1
"""VAST asset delivered via URL endpoint - delivery_type='url'."""

InlineVastAsset = VastAsset2
"""VAST asset with inline XML content - delivery_type='inline'."""

# DAAST Asset Variants (discriminated by delivery_type)
UrlDaastAsset = DaastAsset1
"""DAAST asset delivered via URL endpoint - delivery_type='url'."""

InlineDaastAsset = DaastAsset2
"""DAAST asset with inline XML content - delivery_type='inline'."""

# SubAsset Variants (discriminated by asset_kind)
MediaSubAsset = SubAsset1
"""SubAsset for media content (images, videos) - asset_kind='media', provides content_uri."""

TextSubAsset = SubAsset2
"""SubAsset for text content (headlines, body text) - asset_kind='text', provides content."""

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Activation responses
    "ActivateSignalSuccessResponse",
    "ActivateSignalErrorResponse",
    # Activation keys
    "PropertyIdActivationKey",
    "PropertyTagActivationKey",
    # Asset type aliases
    "BothPreviewRender",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "MediaSubAsset",
    "TextSubAsset",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
    # Build creative responses
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    # Create media buy responses
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    # Performance feedback responses
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
    # Preview creative requests
    "PreviewCreativeFormatRequest",
    "PreviewCreativeManifestRequest",
    # Preview creative responses
    "PreviewCreativeStaticResponse",
    "PreviewCreativeInteractiveResponse",
    # Sync creatives responses
    "SyncCreativesSuccessResponse",
    "SyncCreativesErrorResponse",
    # Update media buy requests
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
    # Update media buy responses
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuyErrorResponse",
]
