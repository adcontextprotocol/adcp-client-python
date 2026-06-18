# mypy: disable-error-code="valid-type"
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

from typing import Annotated as _Annotated
from typing import Any, TypeAlias

from pydantic import ConfigDict, Discriminator, Tag

from adcp.types import _generated as _g
from adcp.types._generated import (
    # Account reference variants
    AccountReference1,
    AccountReference2,
    # Activation key variants
    ActivationKey1,
    ActivationKey2,
    # Authorized agents
    AuthorizedAgents,
    AuthorizedAgents1,
    AuthorizedAgents2,
    AuthorizedAgents3,
    AuthorizedAgents4,
    AuthorizedAgents5,
    ConsentBasis,
    CpaPricingOption,
    CpcPricingOption,
    CpcvPricingOption,
    CpmPricingOption,
    CppPricingOption,
    CpvPricingOption,
    # DAAST assets
    DaastAsset1,
    DaastAsset2,
    # Deployment types
    Deployment1,
    Deployment2,
    # Destination types
    Destination1,
    Destination2,
    FlatRatePricingOption,
    # Get account financials responses
    # Single-class request types (flattened from validation-only oneOf)
    GetCreativeDeliveryRequest,
    # Single-class request types (flattened from validation-only oneOf)
    GetProductsRequest,
    GetSignalsRequest,
    # Preview renders (discriminated union by output_format)
    PreviewRender1,  # output_format='url'
    PreviewRender2,  # output_format='html'
    PreviewRender3,  # output_format='both'
    # Publisher properties types
    PropertyId,
    PropertyTag,
    ProvidePerformanceFeedbackRequest,
    # (SignalPricingOption is now a single RootModel wrapping VendorPricingOption.)
    SiSendMessageRequest,
    TimeBasedPricingOption,
    UpdateMediaBuyRequest,
    # VAST assets
    VastAsset1,
    VastAsset2,
    VcpmPricingOption,
)
from adcp.types._generated import (
    BuildCreativeResponse3 as _BuildCreativeResponse3,
)
from adcp.types._generated import (
    BuildCreativeResponse4 as _BuildCreativeResponse4,
)
from adcp.types._generated import (
    BuildCreativeResponse5 as _BuildCreativeResponse5,
)
from adcp.types._generated import (
    BuildCreativeResponse6 as _BuildCreativeResponse6,
)
from adcp.types.generated_poc.core.async_response_refs.media_buy.get_products_async_response_input_required import (  # noqa: E501
    GetProductsInputRequired,
)
from adcp.types.generated_poc.core.async_response_refs.media_buy.get_products_async_response_submitted import (  # noqa: E501
    GetProductsSubmitted,
)
from adcp.types.generated_poc.core.async_response_refs.media_buy.get_products_async_response_working import (  # noqa: E501
    GetProductsWorking,
)
from adcp.types.generated_poc.core.async_response_refs.signals.get_signals_async_response_submitted import (  # noqa: E501
    GetSignalsSubmitted,
)
from adcp.types.generated_poc.core.async_response_refs.signals.get_signals_async_response_working import (  # noqa: E501
    GetSignalsWorking,
)
from adcp.types.generated_poc.core.error import (
    Recovery,
    Source,
)
from adcp.types.generated_poc.media_buy.create_media_buy_response import (
    CreateMediaBuyResponse1,
    CreateMediaBuyResponse2,
    CreateMediaBuyResponse3,
)
from adcp.types.generated_poc.media_buy.get_products_response import (
    GetProductsResponse as _GetProductsSuccessResponse,
)
from adcp.types.generated_poc.signals.get_signals_response import (
    GetSignalsResponse as _GetSignalsSuccessResponse,
)


def _generated_alias(name: str, fallback_name: str) -> Any:
    try:
        return getattr(_g, name)
    except AttributeError:
        return getattr(_g, fallback_name)


AcquireRightsResponse1 = _generated_alias("AcquireRightsResponse1", "AcquireRightsResponse")
AcquireRightsResponse2 = _generated_alias("AcquireRightsResponse2", "AcquireRightsResponse")
AcquireRightsResponse3 = _generated_alias("AcquireRightsResponse3", "AcquireRightsResponse")
AcquireRightsResponse4 = _generated_alias("AcquireRightsResponse4", "AcquireRightsResponse")
ActivateSignalResponse1 = _generated_alias("ActivateSignalResponse1", "ActivateSignalResponse")
ActivateSignalResponse2 = _generated_alias("ActivateSignalResponse2", "ActivateSignalResponse")
BuildCreativeResponse1 = _generated_alias("BuildCreativeResponse1", "BuildCreativeResponse")
BuildCreativeResponse2 = _generated_alias("BuildCreativeResponse2", "BuildCreativeResponse")
BuildCreativeResponse3: TypeAlias = _BuildCreativeResponse3
BuildCreativeResponse4: TypeAlias = _BuildCreativeResponse4
BuildCreativeResponse5: TypeAlias = _BuildCreativeResponse5
BuildCreativeResponse6: TypeAlias = _BuildCreativeResponse6
CalibrateContentResponse1 = _generated_alias(
    "CalibrateContentResponse1", "CalibrateContentResponse"
)
CalibrateContentResponse2 = _generated_alias(
    "CalibrateContentResponse2", "CalibrateContentResponse"
)
ComplyTestControllerResponse1 = _generated_alias(
    "ComplyTestControllerResponse1", "ComplyTestControllerResponse"
)
ComplyTestControllerResponse2 = _generated_alias(
    "ComplyTestControllerResponse2", "ComplyTestControllerResponse"
)
ComplyTestControllerResponse3 = _generated_alias(
    "ComplyTestControllerResponse3", "ComplyTestControllerResponse"
)
ComplyTestControllerResponse4 = _generated_alias(
    "ComplyTestControllerResponse4", "ComplyTestControllerResponse"
)
CreateContentStandardsResponse1 = _generated_alias(
    "CreateContentStandardsResponse1", "CreateContentStandardsResponse"
)
CreateContentStandardsResponse2 = _generated_alias(
    "CreateContentStandardsResponse2", "CreateContentStandardsResponse"
)
GetAccountFinancialsResponse1 = _generated_alias(
    "GetAccountFinancialsResponse1", "GetAccountFinancialsResponse"
)
GetAccountFinancialsResponse2 = _generated_alias(
    "GetAccountFinancialsResponse2", "GetAccountFinancialsResponse"
)
GetBrandIdentityResponse1 = _generated_alias(
    "GetBrandIdentityResponse1", "GetBrandIdentityResponse"
)
GetBrandIdentityResponse2 = _generated_alias(
    "GetBrandIdentityResponse2", "GetBrandIdentityResponse"
)
GetContentStandardsResponse1 = _generated_alias(
    "GetContentStandardsResponse1", "GetContentStandardsResponse"
)
GetContentStandardsResponse2 = _generated_alias(
    "GetContentStandardsResponse2", "GetContentStandardsResponse"
)
GetCreativeFeaturesResponse1 = _generated_alias(
    "GetCreativeFeaturesResponse1", "GetCreativeFeaturesResponse"
)
GetCreativeFeaturesResponse2 = _generated_alias(
    "GetCreativeFeaturesResponse2", "GetCreativeFeaturesResponse"
)
GetMediaBuyArtifactsResponse1 = _generated_alias(
    "GetMediaBuyArtifactsResponse1", "GetMediaBuyArtifactsResponse"
)
GetMediaBuyArtifactsResponse2 = _generated_alias(
    "GetMediaBuyArtifactsResponse2", "GetMediaBuyArtifactsResponse"
)
GetRightsResponse1 = _generated_alias("GetRightsResponse1", "GetRightsResponse")
GetRightsResponse2 = _generated_alias("GetRightsResponse2", "GetRightsResponse")
ListContentStandardsResponse1 = _generated_alias(
    "ListContentStandardsResponse1", "ListContentStandardsResponse"
)
ListContentStandardsResponse2 = _generated_alias(
    "ListContentStandardsResponse2", "ListContentStandardsResponse"
)
LogEventResponse1 = _generated_alias("LogEventResponse1", "LogEventResponse")
LogEventResponse2 = _generated_alias("LogEventResponse2", "LogEventResponse")
PreviewCreativeResponse1 = _generated_alias("PreviewCreativeResponse1", "PreviewCreativeResponse")
PreviewCreativeResponse2 = _generated_alias("PreviewCreativeResponse2", "PreviewCreativeResponse")
PreviewCreativeResponse3 = _generated_alias("PreviewCreativeResponse3", "PreviewCreativeResponse")
ProvidePerformanceFeedbackResponse1 = _generated_alias(
    "ProvidePerformanceFeedbackResponse1", "ProvidePerformanceFeedbackResponse"
)
ProvidePerformanceFeedbackResponse2 = _generated_alias(
    "ProvidePerformanceFeedbackResponse2", "ProvidePerformanceFeedbackResponse"
)
SyncAccountsResponse1 = _generated_alias("SyncAccountsResponse1", "SyncAccountsResponse")
SyncAccountsResponse2 = _generated_alias("SyncAccountsResponse2", "SyncAccountsResponse")
SyncAudiencesResponse1 = _generated_alias("SyncAudiencesResponse1", "SyncAudiencesResponse")
SyncAudiencesResponse2 = _generated_alias("SyncAudiencesResponse2", "SyncAudiencesResponse")
SyncAudiencesResponse3 = _generated_alias("SyncAudiencesResponse3", "SyncAudiencesResponse")
SyncCatalogsResponse1 = _generated_alias("SyncCatalogsResponse1", "SyncCatalogsResponse")
SyncCatalogsResponse2 = _generated_alias("SyncCatalogsResponse2", "SyncCatalogsResponse")
SyncCatalogsResponse3 = _generated_alias("SyncCatalogsResponse3", "SyncCatalogsResponse")
SyncCreativesResponse1 = _generated_alias("SyncCreativesResponse1", "SyncCreativesResponse")
SyncCreativesResponse2 = _generated_alias("SyncCreativesResponse2", "SyncCreativesResponse")
SyncCreativesResponse3 = _generated_alias("SyncCreativesResponse3", "SyncCreativesResponse")
SyncEventSourcesResponse1 = _generated_alias(
    "SyncEventSourcesResponse1", "SyncEventSourcesResponse"
)
SyncEventSourcesResponse2 = _generated_alias(
    "SyncEventSourcesResponse2", "SyncEventSourcesResponse"
)
UpdateContentStandardsResponse1 = _generated_alias(
    "UpdateContentStandardsResponse1", "UpdateContentStandardsResponse"
)
UpdateContentStandardsResponse2 = _generated_alias(
    "UpdateContentStandardsResponse2", "UpdateContentStandardsResponse"
)
UpdateMediaBuyResponse1 = _generated_alias("UpdateMediaBuyResponse1", "UpdateMediaBuyResponse")
UpdateMediaBuyResponse2 = _generated_alias("UpdateMediaBuyResponse2", "UpdateMediaBuyResponse")
UpdateMediaBuyResponse3 = _generated_alias("UpdateMediaBuyResponse3", "UpdateMediaBuyResponse")
ValidateContentDeliveryResponse1 = _generated_alias(
    "ValidateContentDeliveryResponse1", "ValidateContentDeliveryResponse"
)
ValidateContentDeliveryResponse2 = _generated_alias(
    "ValidateContentDeliveryResponse2", "ValidateContentDeliveryResponse"
)

# CatalogFieldBinding1 = catalog_group binding; give it a semantic name.
from adcp.types._generated import CatalogFieldBinding1 as CatalogGroupBinding
from adcp.types._generated import (
    PublisherPropertySelector1 as PublisherPropertiesInternal,
)
from adcp.types._generated import (
    PublisherPropertySelector2 as PublisherPropertiesByIdInternal,
)
from adcp.types._generated import (
    PublisherPropertySelector3 as PublisherPropertiesByTagInternal,
)

# Note: Package collision resolved by PR #223
# Both create_media_buy and update_media_buy now return full Package objects
# No more separate reference type needed
# Import Package from _generated (still uses qualified name for internal reasons)
from adcp.types._generated import _PackageFromPackage as Package

# ``ProductFormatDeclaration`` comes from ``adcp.types.canonical_decl``
# (a hand-rolled class) rather than ``generated_poc`` because the codegen
# can't represent the discriminated oneOf — see canonical_decl.py.
from adcp.types.canonical_decl import ProductFormatDeclaration
from adcp.types.generated_poc.core.assets.pixel_tracker_asset import (
    Event as PixelTrackerEvent,
)
from adcp.types.generated_poc.core.assets.pixel_tracker_asset import (
    Method as PixelTrackerMethod,
)
from adcp.types.generated_poc.core.assets.pixel_tracker_asset import (
    PixelTrackerAsset,
)

# ----------------------------------------------------------------------------
# Canonical-formats public surface (AdCP 3.1)
# ----------------------------------------------------------------------------
# The v2 catalog-side canonical-formats vocabulary lives across several
# generated_poc paths. Re-export the public-facing classes under clean names
# so adopters import them from ``adcp.types`` without having to reach into
# ``generated_poc/``. Two renames clean up codegen-derived class names that
# include the schema title's parenthetical descriptor:
#
# * ``CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement`` →
#   ``CanonicalFormatAgentPlacement``
# * ``CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven`` →
#   ``CanonicalFormatSponsoredPlacement``
#
# All other canonical format classes keep their generated names. Registry
# types are renamed from the generic codegen forms (``Mapping``, ``V1Pattern``,
# ``V2``) to scoped names that make sense once imported into ``adcp.types``.
from adcp.types.generated_poc.core.canonical_format_kind import (
    CanonicalFormatKind,
)
from adcp.types.generated_poc.core.canonical_projection_ref import (
    AssetSource as CanonicalAssetSource,
)
from adcp.types.generated_poc.core.canonical_projection_ref import (
    CanonicalProjectionReference,
)
from adcp.types.generated_poc.core.canonical_projection_slot_override import (
    CanonicalProjectionSlotOverride as CanonicalSlotOverride,
)

# AdCP 3.0.1 renamed core/format-id.json title from "Format ID" to
# "Format Reference (Structured Object)". The canonical class lives at
# core/format_id.py:FormatReferenceStructuredObject; the bundled-message
# duplicate (which _generated picks up first under the bare name `FormatId`)
# is a stale per-message inline. Re-export the canonical class as `FormatId`
# so downstream code that builds Format(format_id=FormatId(...)) keeps working.
from adcp.types.generated_poc.core.format_id import (
    FormatReferenceStructuredObject as FormatId,
)
from adcp.types.generated_poc.core.product_format_declaration import (
    SellerPreference as ProductFormatSellerPreference,
)
from adcp.types.generated_poc.formats.canonical._base import (
    CanonicalFormatBase,
)
from adcp.types.generated_poc.formats.canonical._base import (
    CompositionModel as CanonicalCompositionModel,
)
from adcp.types.generated_poc.formats.canonical.agent_placement import (
    CanonicalFormatAgentPlacementAiSurfaceSponsoredPlacement as CanonicalFormatAgentPlacement,
)
from adcp.types.generated_poc.formats.canonical.audio_daast import (
    CanonicalFormatDaastAudio,
)
from adcp.types.generated_poc.formats.canonical.audio_hosted import (
    CanonicalFormatHostedAudio,
)
from adcp.types.generated_poc.formats.canonical.display_tag import (
    CanonicalFormatDisplayTag,
)
from adcp.types.generated_poc.formats.canonical.html5 import (
    CanonicalFormatHtml5Banner,
)
from adcp.types.generated_poc.formats.canonical.image import (
    CanonicalFormatImage,
)
from adcp.types.generated_poc.formats.canonical.image_carousel import (
    CanonicalFormatImageCarousel,
)
from adcp.types.generated_poc.formats.canonical.native_in_feed import (
    CanonicalFormatNativeInFeed,
)
from adcp.types.generated_poc.formats.canonical.responsive_creative import (
    CanonicalFormatResponsiveCreative,
)
from adcp.types.generated_poc.formats.canonical.sponsored_placement import (
    CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven as CanonicalFormatSponsoredPlacement,
)
from adcp.types.generated_poc.formats.canonical.video_hosted import (
    CanonicalFormatHostedVideo,
)
from adcp.types.generated_poc.formats.canonical.video_vast import (
    CanonicalFormatVastVideo,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    V2 as V1CanonicalV2Projection,  # noqa: N811 — codegen class ``V2``; rename here
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    Dimensions as V1CanonicalDimensions,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    Mapping as V1CanonicalMapping,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    Structural as V1CanonicalStructural,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    V1Pattern as V1CanonicalGlobPattern,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    V1Pattern1 as V1CanonicalStructuralPattern,
)
from adcp.types.generated_poc.registries.v1_canonical_mapping import (
    V1V2CanonicalFormatMappingRegistry,
)

try:
    from adcp.types.generated_poc.creative.sync_creatives_response import (
        Creative as SyncCreativeResultInternal,
    )
except ImportError:
    SyncCreativeResultInternal = _g.SyncCreativesResponse  # type: ignore[misc,assignment]

# Status name collides across many modules. Preserve backward compat by importing
# the specific variant that was exported on main (media buy delivery status).
from adcp.types.generated_poc.media_buy.get_media_buy_delivery_response import (
    Status as MediaBuyDeliveryStatus,
)

# Audience name collides in _generated (delivery breakdown wins over sync request)
from adcp.types.generated_poc.media_buy.sync_audiences_request import (
    Audience as SyncAudiencesAudienceInternal,
)

# Import nested types that aren't exported by _generated but are useful for type hints
try:
    from adcp.types.generated_poc.media_buy.sync_catalogs_response import (
        Catalog as SyncCatalogResultInternal,
    )
except ImportError:
    SyncCatalogResultInternal = _g.SyncCatalogsResponse  # type: ignore[misc,assignment]

# ============================================================================
# ACCOUNT REFERENCE ALIASES - Identification Method Discriminated Unions
# ============================================================================
# AccountReference is a discriminated union with two identification methods:
#
# 1. By seller-assigned ID (account_id):
#    - Use when the buyer manages accounts via list_accounts or sync_accounts
#    - Requires seller-assigned account_id string
#
# 2. By natural key (brand + operator):
#    - Use when the seller resolves accounts internally from brand identity
#    - Requires brand reference + operator domain

AccountReferenceById = AccountReference1
"""Account reference using a seller-assigned account ID.

Use when the buyer manages accounts (e.g., picked from list_accounts or
sync_accounts). The account_id must match one returned by the seller.

Fields:
- account_id: Seller-assigned account identifier

Example:
    ```python
    from adcp import AccountReferenceById

    account = AccountReferenceById(account_id="acc_acme_001")
    ```
"""

AccountReferenceByNaturalKey = AccountReference2
"""Account reference using brand + operator natural key.

Use when the seller resolves accounts internally from brand identity.
The seller looks up the account based on the brand/operator combination.

Fields:
- brand: BrandReference identifying the advertiser
- operator: Domain of the entity operating on the brand's behalf

Example:
    ```python
    from adcp import AccountReferenceByNaturalKey

    account = AccountReferenceByNaturalKey(
        brand={"domain": "acme-corp.com"},
        operator="acme-corp.com"
    )
    ```
"""

# ============================================================================
# RESPONSE TYPE ALIASES - Success/Error Discriminated Unions
# ============================================================================
# These are atomic operations where the response is EITHER success OR error,
# never both. The numbered suffixes from the generator don't convey this
# critical semantic distinction.

# Activate Signal Response Variants
ActivateSignalSuccessResponse: TypeAlias = ActivateSignalResponse1
"""Success response - signal activation succeeded."""

ActivateSignalErrorResponse: TypeAlias = ActivateSignalResponse2
"""Error response - signal activation failed."""

# Build Creative Response Variants
BuildCreativeSuccessResponse: TypeAlias = BuildCreativeResponse1
"""Success response - creative built successfully, manifest returned."""

BuildCreativeErrorResponse: TypeAlias = BuildCreativeResponse2
"""Error response - creative build failed, no manifest created."""

BuildCreativeSubmittedResponse: TypeAlias = BuildCreativeResponse6
"""Submitted (async) envelope - creative build accepted for async processing."""

# Create Media Buy Response Variants
CreateMediaBuySuccessResponse = CreateMediaBuyResponse1
"""Success response - media buy created successfully with media_buy_id."""

CreateMediaBuyErrorResponse = CreateMediaBuyResponse2
"""Error response - media buy creation failed, no media buy created."""

CreateMediaBuySubmittedResponse = CreateMediaBuyResponse3
"""Submitted (async) envelope - operation accepted for asynchronous processing.

Returned when the seller has handed the request off to a long-running task
(e.g., HITL approval, governance review). The buyer receives a ``task_id`` to
poll via ``tasks/get`` or to correlate with push-notification callbacks; the
``media_buy_id`` is issued later on the completion artifact, not here.

Status discriminator: ``status == 'submitted'`` (task-level), distinguishing
this envelope from the synchronous success branch whose ``status`` field
carries a ``MediaBuyStatus`` value (``pending_creatives``, ``pending_start``,
``active``).
"""

# Get Products Response Variants
#
# The rc.9 ``get_products_response`` schema is a single flat success
# object — the async arms (submitted / working / input_required) ship as
# separate ``core/async_response_refs`` schemas, NOT unioned into the
# top-level response. So ``GetProductsResponse`` (the public name) stays
# the constructable success class; these aliases name the orphaned async
# arms semantically, mirroring ``CreateMediaBuySubmittedResponse`` even
# though create_media_buy's submitted arm IS unioned into its generated
# response (its schema is a oneOf, get_products' is not).
GetProductsSuccessResponse: TypeAlias = _GetProductsSuccessResponse
"""Success response - product catalog issued in-line (synchronous read)."""

GetProductsSubmittedResponse: TypeAlias = GetProductsSubmitted
"""Submitted (async) envelope - brief / refine discovery handed off to a
background task. Carries ``task_id`` + ``status='submitted'``; the
``products`` array is issued on the completion artifact (poll
``tasks/get``), not here. Wholesale calls MUST NOT produce this arm."""

GetProductsWorkingResponse: TypeAlias = GetProductsWorking
"""Working (async) progress envelope for an in-flight get_products task."""

GetProductsInputRequiredResponse: TypeAlias = GetProductsInputRequired
"""Input-required (async) envelope - the seller paused discovery to
solicit buyer clarification (CLARIFICATION_NEEDED / BUDGET_REQUIRED)."""

#: Full async-aware union for ``get_products``. Includes the synchronous
#: success arm plus the three async arms the rc.9 spec ships for this
#: verb. The public ``GetProductsResponse`` name remains the success
#: class (so direct construction / ``model_validate`` keep working);
#: this union is the honest type of "any get_products response shape on
#: the wire," used by callers that pattern-match across sync and async.
GetProductsResponseUnion: TypeAlias = (
    _GetProductsSuccessResponse
    | GetProductsSubmitted
    | GetProductsWorking
    | GetProductsInputRequired
)

# Get Signals Response Variants
#
# get_signals ships ONLY submitted + working async arms — NO
# input_required (signal discovery cannot pause to solicit buyer input).
# Same flat-success-schema posture as get_products.
GetSignalsSuccessResponse: TypeAlias = _GetSignalsSuccessResponse
"""Success response - signal catalog issued in-line (synchronous read)."""

GetSignalsSubmittedResponse: TypeAlias = GetSignalsSubmitted
"""Submitted (async) envelope - brief discovery handed off to a
background task. Carries ``task_id`` + ``status='submitted'``; the
``signals`` array is issued on the completion artifact. Wholesale calls
MUST NOT produce this arm."""

GetSignalsWorkingResponse: TypeAlias = GetSignalsWorking
"""Working (async) progress envelope for an in-flight get_signals task."""

#: Full async-aware union for ``get_signals``. Includes the synchronous
#: success arm plus the two async arms (submitted / working). Has NO
#: input_required arm — narrower than ``GetProductsResponseUnion``.
GetSignalsResponseUnion: TypeAlias = (
    _GetSignalsSuccessResponse | GetSignalsSubmitted | GetSignalsWorking
)

# Performance Feedback Response Variants
ProvidePerformanceFeedbackSuccessResponse: TypeAlias = ProvidePerformanceFeedbackResponse1
"""Success response - performance feedback accepted."""

ProvidePerformanceFeedbackErrorResponse: TypeAlias = ProvidePerformanceFeedbackResponse2
"""Error response - performance feedback rejected."""

# Sync Creatives Response Variants
SyncCreativesSuccessResponse: TypeAlias = SyncCreativesResponse1
"""Success response - sync operation processed creatives."""

SyncCreativesErrorResponse: TypeAlias = SyncCreativesResponse2
"""Error response - sync operation failed."""

SyncCreativesSubmittedResponse: TypeAlias = SyncCreativesResponse3
"""Submitted (async) envelope - creative sync accepted for async processing."""

# Sync Creative Result (nested type from SyncCreativesResponse1.creatives[])
SyncCreativeResult: TypeAlias = SyncCreativeResultInternal
"""Result of syncing a single creative - indicates action taken (created, updated, failed, etc.)

This is the item type from SyncCreativesSuccessResponse.creatives[]. In TypeScript, this would be:
    type SyncCreativeResult = SyncCreativesSuccessResponse["creatives"][number]

Example usage:
    from adcp import SyncCreativeResult, SyncCreativesSuccessResponse

    def process_result(result: SyncCreativeResult) -> None:
        if result.action == "created":
            print(f"Created creative {result.creative_id}")
        elif result.action == "failed":
            print(f"Failed: {result.errors}")
"""

# Sync Accounts Response Variants
SyncAccountsSuccessResponse: TypeAlias = SyncAccountsResponse1
"""Success response - accounts synced successfully."""

SyncAccountsErrorResponse: TypeAlias = SyncAccountsResponse2
"""Error response - account sync failed."""

# Log Event Response Variants
LogEventSuccessResponse: TypeAlias = LogEventResponse1
"""Success response - events logged successfully."""

LogEventErrorResponse: TypeAlias = LogEventResponse2
"""Error response - event logging failed."""

# Sync Catalogs Response Variants
SyncCatalogsSuccessResponse: TypeAlias = SyncCatalogsResponse1
"""Success response - sync operation processed catalogs (may include per-catalog failures)."""

SyncCatalogsErrorResponse: TypeAlias = SyncCatalogsResponse2
"""Error response - operation failed completely, no catalogs were processed."""

SyncCatalogsSubmittedResponse: TypeAlias = SyncCatalogsResponse3
"""Submitted (async) envelope - catalog sync accepted for async processing."""

# Sync Catalog Result (nested type from SyncCatalogsResponse1.catalogs[])
SyncCatalogResult: TypeAlias = SyncCatalogResultInternal
"""Result of syncing a single catalog - indicates action taken and per-item status.

This is the item type from SyncCatalogsSuccessResponse.catalogs[]. In TypeScript, this would be:
    type SyncCatalogResult = SyncCatalogsSuccessResponse["catalogs"][number]

Example usage:
    from adcp import SyncCatalogResult, SyncCatalogsSuccessResponse

    def process_result(result: SyncCatalogResult) -> None:
        if result.action == "created":
            print(f"Created catalog {result.catalog_id}")
        elif result.action == "failed":
            print(f"Failed: {result.errors}")
"""

# Sync Event Sources Response Variants
SyncEventSourcesSuccessResponse: TypeAlias = SyncEventSourcesResponse1
"""Success response - event sources synced successfully."""

SyncEventSourcesErrorResponse: TypeAlias = SyncEventSourcesResponse2
"""Error response - event source sync failed."""

# Calibrate Content Response Variants
CalibrateContentSuccessResponse: TypeAlias = CalibrateContentResponse1
"""Success response - content calibration completed."""

CalibrateContentErrorResponse: TypeAlias = CalibrateContentResponse2
"""Error response - content calibration failed."""

# Validate Content Delivery Response Variants
ValidateContentDeliverySuccessResponse: TypeAlias = ValidateContentDeliveryResponse1
"""Success response - content delivery validated."""

ValidateContentDeliveryErrorResponse: TypeAlias = ValidateContentDeliveryResponse2
"""Error response - content delivery validation failed."""

# Get Content Standards Response Variants
GetContentStandardsSuccessResponse: TypeAlias = GetContentStandardsResponse1
"""Success response - content standards retrieved."""

GetContentStandardsErrorResponse: TypeAlias = GetContentStandardsResponse2
"""Error response - content standards retrieval failed."""

# List Content Standards Response Variants
ListContentStandardsSuccessResponse: TypeAlias = ListContentStandardsResponse1
"""Success response - content standards listed."""

ListContentStandardsErrorResponse: TypeAlias = ListContentStandardsResponse2
"""Error response - content standards listing failed."""

# Create Content Standards Response Variants
CreateContentStandardsSuccessResponse: TypeAlias = CreateContentStandardsResponse1
"""Success response - content standards created."""

CreateContentStandardsErrorResponse: TypeAlias = CreateContentStandardsResponse2
"""Error response - content standards creation failed."""

# Update Content Standards Response Variants
UpdateContentStandardsSuccessResponse: TypeAlias = UpdateContentStandardsResponse1
"""Success response - content standards updated, returns standards_id."""

UpdateContentStandardsErrorResponse: TypeAlias = UpdateContentStandardsResponse2
"""Error response - content standards update failed, includes errors."""

# Get Media Buy Artifacts Response Variants
GetMediaBuyArtifactsSuccessResponse: TypeAlias = GetMediaBuyArtifactsResponse1
"""Success response - media buy artifacts retrieved."""

GetMediaBuyArtifactsErrorResponse: TypeAlias = GetMediaBuyArtifactsResponse2
"""Error response - media buy artifacts retrieval failed."""

# Update Media Buy Response Variants
UpdateMediaBuySuccessResponse: TypeAlias = UpdateMediaBuyResponse1
"""Success response - media buy updated successfully."""

UpdateMediaBuyErrorResponse: TypeAlias = UpdateMediaBuyResponse2
"""Error response - media buy update failed, no changes applied."""

UpdateMediaBuySubmittedResponse: TypeAlias = UpdateMediaBuyResponse3
"""Submitted (async) envelope - media buy update accepted for async processing."""

# Get Account Financials Response Variants
GetAccountFinancialsSuccessResponse: TypeAlias = GetAccountFinancialsResponse1
"""Success response - account financials retrieved."""

GetAccountFinancialsErrorResponse: TypeAlias = GetAccountFinancialsResponse2
"""Error response - account financials retrieval failed."""

# Sync Audiences Response Variants
SyncAudiencesSuccessResponse: TypeAlias = SyncAudiencesResponse1
"""Success response - audiences synced successfully."""

SyncAudiencesErrorResponse: TypeAlias = SyncAudiencesResponse2
"""Error response - audiences sync failed."""

SyncAudiencesSubmittedResponse: TypeAlias = SyncAudiencesResponse3
"""Submitted (async) envelope - audience sync accepted for async processing."""

# Get Creative Features Response Variants
GetCreativeFeaturesSuccessResponse: TypeAlias = GetCreativeFeaturesResponse1
"""Success response - creative features retrieved."""

GetCreativeFeaturesErrorResponse: TypeAlias = GetCreativeFeaturesResponse2
"""Error response - creative features retrieval failed."""

# ============================================================================
# BRAND RIGHTS RESPONSE ALIASES
# ============================================================================
# AcquireRightsResponse is a 4-way union discriminated by status field.

AcquireRightsAcquiredResponse: TypeAlias = AcquireRightsResponse1
"""Rights acquired - includes generation_credentials and terms."""

AcquireRightsPendingResponse: TypeAlias = AcquireRightsResponse2
"""Rights require approval from the rights holder."""

AcquireRightsRejectedResponse: TypeAlias = AcquireRightsResponse3
"""Rights request was rejected."""

AcquireRightsErrorResponse: TypeAlias = AcquireRightsResponse4
"""Error response - request validation or processing failed."""

GetBrandIdentitySuccessResponse: TypeAlias = GetBrandIdentityResponse1
"""Success response - brand identity data returned."""

GetBrandIdentityErrorResponse: TypeAlias = GetBrandIdentityResponse2
"""Error response - brand identity lookup failed."""

GetRightsSuccessResponse: TypeAlias = GetRightsResponse1
"""Success response - available rights returned."""

GetRightsErrorResponse: TypeAlias = GetRightsResponse2
"""Error response - rights lookup failed."""

# ============================================================================
# COMPLIANCE TEST CONTROLLER ALIASES
# ============================================================================
# Request is now a single class with a `scenario` enum field — per-scenario
# request aliases were removed. Response remains a 4-way discriminated union.

ComplyListScenariosResponse: TypeAlias = ComplyTestControllerResponse1
"""Success - lists supported scenarios."""

ComplyStateTransitionResponse: TypeAlias = ComplyTestControllerResponse2
"""Success - state transition completed."""

ComplySimulationResponse: TypeAlias = ComplyTestControllerResponse3
"""Success - simulation completed."""

ComplyErrorResponse: TypeAlias = ComplyTestControllerResponse4
"""Error - operation failed."""

# ============================================================================
# REQUEST TYPE ALIASES - Operation Variants
# ============================================================================

# PreviewCreativeRequest is now a single class with a `request_type` enum field —
# per-mode request aliases were removed. Dispatch on request.request_type instead.

# Get Products Request Aliases (backward compat — now a single class)
GetProductsRefineRequest: TypeAlias = GetProductsRequest
"""Get products request — use buying_mode field to select mode."""

# Performance Feedback Request Aliases (backward compat — now a single class)
ProvidePerformanceFeedbackByMediaBuyRequest: TypeAlias = ProvidePerformanceFeedbackRequest
"""Performance feedback request — use media_buy_id or buyer_ref field."""

ProvidePerformanceFeedbackByBuyerRefRequest: TypeAlias = ProvidePerformanceFeedbackRequest
"""Performance feedback request — use media_buy_id or buyer_ref field."""

# Update Media Buy Request Aliases (backward compat — now a single class)
UpdateMediaBuyPackagesRequest: TypeAlias = UpdateMediaBuyRequest
"""Update media buy request — use media_buy_id or buyer_ref field."""

UpdateMediaBuyPropertiesRequest: TypeAlias = UpdateMediaBuyRequest
"""Update media buy request — use media_buy_id or buyer_ref field."""

# Get Creative Delivery Request Aliases (backward compat — now a single class)
GetCreativeDeliveryByMediaBuyRequest: TypeAlias = GetCreativeDeliveryRequest
"""Request creative delivery — use media_buy_ids, media_buy_buyer_refs, or creative_ids."""

GetCreativeDeliveryByBuyerRefRequest: TypeAlias = GetCreativeDeliveryRequest
"""Request creative delivery — use media_buy_ids, media_buy_buyer_refs, or creative_ids."""

GetCreativeDeliveryByCreativeRequest: TypeAlias = GetCreativeDeliveryRequest
"""Request creative delivery — use media_buy_ids, media_buy_buyer_refs, or creative_ids."""

# Get Products Request Aliases (backward compat — now a single class)
GetProductsBriefRequest: TypeAlias = GetProductsRequest
"""Get products request — use buying_mode field to select mode."""

GetProductsWholesaleRequest: TypeAlias = GetProductsRequest
"""Get products request — use buying_mode field to select mode."""

# Get Signals Request Aliases (backward compat — now a single class)
GetSignalsDiscoveryRequest: TypeAlias = GetSignalsRequest
"""Get signals request — use signal_spec and/or signal_ids fields."""

GetSignalsLookupRequest: TypeAlias = GetSignalsRequest
"""Get signals request — use signal_spec and/or signal_ids fields."""

# SI Send Message Request Aliases (backward compat — now a single class)
SiSendTextMessageRequest: TypeAlias = SiSendMessageRequest
"""Send message request — use message and/or action_response fields."""

SiSendActionResponseRequest: TypeAlias = SiSendMessageRequest
"""Send message request — use message and/or action_response fields."""

# ============================================================================
# ACTIVATION KEY ALIASES
# ============================================================================

SegmentIdActivationKey: TypeAlias = ActivationKey1
"""Activation key using segment ID targeting - type='segment_id'."""

KeyValueActivationKey: TypeAlias = ActivationKey2
"""Activation key using key-value pair targeting - type='key_value'."""

# ============================================================================
# PREVIEW/RENDER TYPE ALIASES
# ============================================================================

# Preview Creative Response Variants
PreviewCreativeSingleResponse: TypeAlias = PreviewCreativeResponse1
"""Single preview response with previews array and expires_at - response_type='single'."""

PreviewCreativeBatchResponse: TypeAlias = PreviewCreativeResponse2
"""Batch preview response with results array - response_type='batch'."""

PreviewCreativeVariantResponse: TypeAlias = PreviewCreativeResponse3
"""Variant preview response with variant_id and rendered pieces - response_type='variant'."""


# Preview Render Aliases (discriminated union by output_format)
UrlPreviewRender: TypeAlias = PreviewRender1
"""Preview render with output_format='url' and preview_url for iframe embedding."""

HtmlPreviewRender: TypeAlias = PreviewRender2
"""Preview render with output_format='html' and preview_html for direct embedding."""

BothPreviewRender: TypeAlias = PreviewRender3
"""Preview render with output_format='both' and both preview_url and preview_html."""

# ============================================================================
# ASSET TYPE ALIASES - Delivery & Kind Discriminated Unions
# ============================================================================

# VAST Asset Variants (discriminated by delivery_type)
UrlVastAsset: TypeAlias = VastAsset1
"""VAST asset delivered via URL endpoint - delivery_type='url'."""

InlineVastAsset: TypeAlias = VastAsset2
"""VAST asset with inline XML content - delivery_type='inline'."""

# DAAST Asset Variants (discriminated by delivery_type)
UrlDaastAsset: TypeAlias = DaastAsset1
"""DAAST asset delivered via URL endpoint - delivery_type='url'."""

InlineDaastAsset: TypeAlias = DaastAsset2
"""DAAST asset with inline XML content - delivery_type='inline'."""

# ============================================================================
# PACKAGE TYPE ALIASES - Resolving Type Name Collisions
# ============================================================================
# The AdCP schemas define two genuinely different types both named "Package":
#
# 1. Full Package (from package.json schema):
#    - Complete operational package with all fields (budget, pricing_option_id, etc.)
#    - Used in MediaBuy, update operations, and package management
#    - Has 12+ fields for full package configuration
#
# Package collision resolved by PR #223:
# - create-media-buy-response.json now returns full Package objects (not minimal refs)
# - update-media-buy-response.json already returned full Package objects
# - Both operations return identical Package structures
# - Single Package type imported above, no aliases needed

# ============================================================================
# PUBLISHER PROPERTIES ALIASES - Selection Type Discriminated Unions
# ============================================================================
# The AdCP schemas define PublisherProperties as a discriminated union with
# three variants based on the `selection_type` field:
#
# 1. All Properties (selection_type='all'):
#    - Includes all properties from the publisher
#    - Only requires publisher_domain
#
# 2. By ID (selection_type='by_id'):
#    - Specific properties selected by property_id
#    - Requires publisher_domain + property_ids array
#
# 3. By Tag (selection_type='by_tag'):
#    - Properties selected by tags
#    - Requires publisher_domain + property_tags array
#
# These semantic aliases match the discriminator values and make code more
# readable when constructing or pattern-matching publisher properties.

PublisherPropertiesAll: TypeAlias = PublisherPropertiesInternal
"""Publisher properties covering all properties from the publisher.

This variant uses selection_type='all' and includes all properties listed
in the publisher's adagents.json file.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['all']

Example:
    ```python
    from adcp import PublisherPropertiesAll

    props = PublisherPropertiesAll(
        publisher_domain="example.com",
        selection_type="all"
    )
    ```
"""

PublisherPropertiesById: TypeAlias = PublisherPropertiesByIdInternal
"""Publisher properties selected by specific property IDs.

This variant uses selection_type='by_id' and specifies an explicit list
of property IDs from the publisher's adagents.json file.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['by_id']
- property_ids: List of PropertyId (non-empty)

Example:
    ```python
    from adcp import PublisherPropertiesById, PropertyId

    props = PublisherPropertiesById(
        publisher_domain="example.com",
        selection_type="by_id",
        property_ids=[PropertyId("homepage"), PropertyId("sports_section")]
    )
    ```
"""

PublisherPropertiesByTag: TypeAlias = PublisherPropertiesByTagInternal
"""Publisher properties selected by tags.

This variant uses selection_type='by_tag' and specifies property tags.
The product covers all properties in the publisher's adagents.json that
have these tags.

Fields:
- publisher_domain: Domain where adagents.json is hosted
- selection_type: Literal['by_tag']
- property_tags: List of PropertyTag (non-empty)

Example:
    ```python
    from adcp import PublisherPropertiesByTag, PropertyTag

    props = PublisherPropertiesByTag(
        publisher_domain="example.com",
        selection_type="by_tag",
        property_tags=[PropertyTag("premium"), PropertyTag("video")]
    )
    ```
"""

# ============================================================================
# DEPLOYMENT & DESTINATION ALIASES - Signal Deployment Type Discriminated Unions
# ============================================================================
# The AdCP schemas define Deployment and Destination as discriminated unions
# with two variants based on the `type` field:
#
# Deployment (where a signal is activated):
# - Platform (type='platform'): DSP platform with platform ID
# - Agent (type='agent'): Sales agent with agent URL
#
# Destination (where a signal can be activated):
# - Platform (type='platform'): Target DSP platform
# - Agent (type='agent'): Target sales agent
#
# These are used in GetSignalsResponse to describe signal availability and
# activation status across different advertising platforms and agents.

PlatformDeployment = Deployment1
"""Signal deployment to a DSP platform.

This variant uses type='platform' for platform-based signal deployments
like The Trade Desk, Amazon DSP, etc.

Fields:
- type: Literal['platform']
- platform: Platform identifier (e.g., 'the-trade-desk')
- account: Optional account identifier
- is_live: Whether signal is currently active
- deployed_at: Activation timestamp if live
- activation_key: Targeting key if live and accessible
- estimated_activation_duration_minutes: Time to complete activation

Example:
    ```python
    from adcp import PlatformDeployment

    deployment = PlatformDeployment(
        type="platform",
        platform="the-trade-desk",
        account="advertiser-123",
        is_live=True,
        deployed_at=datetime.now(timezone.utc)
    )
    ```
"""

AgentDeployment = Deployment2
"""Signal deployment to a sales agent.

This variant uses type='agent' for agent-based signal deployments
using agent URLs.

Fields:
- type: Literal['agent']
- agent_url: URL identifying the destination agent
- account: Optional account identifier
- is_live: Whether signal is currently active
- deployed_at: Activation timestamp if live
- activation_key: Targeting key if live and accessible
- estimated_activation_duration_minutes: Time to complete activation

Example:
    ```python
    from adcp import AgentDeployment

    deployment = AgentDeployment(
        type="agent",
        agent_url="https://agent.example.com",
        is_live=False,
        estimated_activation_duration_minutes=30.0
    )
    ```
"""

PlatformDestination = Destination1
"""Available signal destination on a DSP platform.

This variant uses type='platform' for platform-based signal destinations.

Fields:
- type: Literal['platform']
- platform: Platform identifier (e.g., 'the-trade-desk', 'amazon-dsp')
- account: Optional account identifier on the platform

Example:
    ```python
    from adcp import PlatformDestination

    destination = PlatformDestination(
        type="platform",
        platform="the-trade-desk",
        account="advertiser-123"
    )
    ```
"""

AgentDestination = Destination2
"""Available signal destination via a sales agent.

This variant uses type='agent' for agent-based signal destinations.

Fields:
- type: Literal['agent']
- agent_url: URL identifying the destination agent
- account: Optional account identifier on the agent

Example:
    ```python
    from adcp import AgentDestination

    destination = AgentDestination(
        type="agent",
        agent_url="https://agent.example.com",
        account="partner-456"
    )
    ```
"""

# ============================================================================
# AUTHORIZED AGENTS ALIASES - Authorization Type Discriminated Unions
# ============================================================================
# The AdCP adagents.json schema defines AuthorizedAgents as a discriminated
# union with four variants based on the `authorization_type` field:
#
# 1. Property IDs (authorization_type='property_ids'):
#    - Agent authorized for specific property IDs
#    - Requires property_ids array
#
# 2. Property Tags (authorization_type='property_tags'):
#    - Agent authorized for properties matching tags
#    - Requires property_tags array
#
# 3. Inline Properties (authorization_type='inline_properties'):
#    - Agent authorized with inline property definitions
#    - Requires properties array with full Property objects
#
# 4. Publisher Properties (authorization_type='publisher_properties'):
#    - Agent authorized for properties from other publisher domains
#    - Requires publisher_properties array
#
# These define which sales agents are authorized to sell inventory and which
# properties they can access.

AuthorizedAgentsByPropertyId = AuthorizedAgents
"""Authorized agent with specific property IDs.

This variant uses authorization_type='property_ids' for agents authorized
to sell specific properties identified by their IDs.

Fields:
- authorization_type: Literal['property_ids']
- authorized_for: Human-readable description
- property_ids: List of PropertyId (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByPropertyId, PropertyId

    agent = AuthorizedAgentsByPropertyId(
        authorization_type="property_ids",
        authorized_for="Premium display inventory",
        property_ids=[PropertyId("homepage"), PropertyId("sports")],
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByPropertyTag = AuthorizedAgents1
"""Authorized agent with property tags.

This variant uses authorization_type='property_tags' for agents authorized
to sell properties identified by matching tags.

Fields:
- authorization_type: Literal['property_tags']
- authorized_for: Human-readable description
- property_tags: List of PropertyTag (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByPropertyTag, PropertyTag

    agent = AuthorizedAgentsByPropertyTag(
        authorization_type="property_tags",
        authorized_for="Video inventory",
        property_tags=[PropertyTag("video"), PropertyTag("premium")],
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByInlineProperties = AuthorizedAgents2
"""Authorized agent with inline property definitions.

This variant uses authorization_type='inline_properties' for agents with
inline Property objects rather than references to the top-level properties array.

Fields:
- authorization_type: Literal['inline_properties']
- authorized_for: Human-readable description
- properties: List of Property objects (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import AuthorizedAgentsByInlineProperties
    from adcp.types.stable import Property

    agent = AuthorizedAgentsByInlineProperties(
        authorization_type="inline_properties",
        authorized_for="Custom inventory bundle",
        properties=[...],  # Full Property objects
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsByPublisherProperties = AuthorizedAgents3
"""Authorized agent for properties from other publishers.

This variant uses authorization_type='publisher_properties' for agents
authorized to sell inventory from other publisher domains.

Fields:
- authorization_type: Literal['publisher_properties']
- authorized_for: Human-readable description
- publisher_properties: List of PublisherPropertySelector variants (non-empty)
- url: Agent's API endpoint URL

Example:
    ```python
    from adcp.types.aliases import (
        AuthorizedAgentsByPublisherProperties,
        PublisherPropertiesAll
    )

    agent = AuthorizedAgentsByPublisherProperties(
        authorization_type="publisher_properties",
        authorized_for="Network inventory across publishers",
        publisher_properties=[
            PublisherPropertiesAll(
                publisher_domain="publisher1.com",
                selection_type="all"
            )
        ],
        url="https://agent.example.com"
    )
    ```
"""

AuthorizedAgentsBySignalId = AuthorizedAgents4
"""Authorized agent for specific signal IDs.

This variant uses authorization_type='signal_ids' for agents authorized
to resell specific signals identified by their IDs.

Fields:
- authorization_type: Literal['signal_ids']
- authorized_for: Human-readable description of signals authorization
- signal_ids: List of SignalId (non-empty)
- url: Authorized signals agent's API endpoint URL
"""

AuthorizedAgentsBySignalTag = AuthorizedAgents5
"""Authorized agent for signals matching tags.

This variant uses authorization_type='signal_tags' for agents authorized
to resell signals identified by matching tags.

Fields:
- authorization_type: Literal['signal_tags']
- authorized_for: Human-readable description of signals authorization
- signal_tags: List of SignalTag (non-empty)
- url: Authorized signals agent's API endpoint URL
"""

# ============================================================================
# UNION TYPE ALIASES - For Type Hints and Pattern Matching
# ============================================================================
# These union aliases provide convenient types for function signatures,
# type hints, and pattern matching without having to manually construct
# the union each time.

# Deployment union (for signals)
Deployment = PlatformDeployment | AgentDeployment
"""Union type for all deployment variants.

Use this for type hints when a function accepts any deployment type:

Example:
    ```python
    def process_deployment(deployment: Deployment) -> None:
        if isinstance(deployment, PlatformDeployment):
            print(f"Platform: {deployment.platform}")
        elif isinstance(deployment, AgentDeployment):
            print(f"Agent: {deployment.agent_url}")
    ```
"""

# Destination union (for signals)
Destination = PlatformDestination | AgentDestination
"""Union type for all destination variants.

Use this for type hints when a function accepts any destination type:

Example:
    ```python
    def format_destination(dest: Destination) -> str:
        if isinstance(dest, PlatformDestination):
            return f"Platform: {dest.platform}"
        elif isinstance(dest, AgentDestination):
            return f"Agent: {dest.agent_url}"
    ```
"""

# Authorized agent union (for adagents.json)
AuthorizedAgent = (
    AuthorizedAgentsByPropertyId
    | AuthorizedAgentsByPropertyTag
    | AuthorizedAgentsByInlineProperties
    | AuthorizedAgentsByPublisherProperties
    | AuthorizedAgentsBySignalId
    | AuthorizedAgentsBySignalTag
)
"""Union type for all authorized agent variants.

Use this for type hints when processing agents from adagents.json:

Example:
    ```python
    def validate_agent(agent: AuthorizedAgent) -> bool:
        match agent.authorization_type:
            case "property_ids":
                return len(agent.property_ids) > 0
            case "property_tags":
                return len(agent.property_tags) > 0
            case "inline_properties":
                return len(agent.properties) > 0
            case "publisher_properties":
                return len(agent.publisher_properties) > 0
    ```
"""

# Publisher properties union (for product requests)
PublisherProperties = PublisherPropertiesAll | PublisherPropertiesById | PublisherPropertiesByTag
"""Union type for all publisher properties variants.

Use this for type hints in product filtering:

Example:
    ```python
    def filter_products(props: PublisherProperties) -> None:
        match props.selection_type:
            case "all":
                print("All properties from publisher")
            case "by_id":
                print(f"Properties: {props.property_ids}")
            case "by_tag":
                print(f"Tags: {props.property_tags}")
    ```
"""

# ============================================================================
# SIGNAL PRICING OPTION ALIASES
# ============================================================================
# SignalPricingOption is now a RootModel wrapping VendorPricingOption; the
# per-model variant aliases (CpmSignalPricingOption, etc.) were removed.
# Dispatch on the wrapped VendorPricingOption.root.model field instead.

# ============================================================================
# FIELD ENUM ALIASES - Disambiguating FieldModel Name Collision
# ============================================================================
# The code generator produces FieldModel in both get_products_request and
# get_brand_identity_request. The generator renamed the get_products_request
# variant to Field1, but _generated.py imports Field1 from tasks_list_request
# (alphabetical wins), so the get_products version is not reachable via
# _generated at all. Import it directly from its source module.

from adcp.types.generated_poc.media_buy.get_products_request import (
    Field1 as GetProductsFieldInternal,
)

GetProductsField = GetProductsFieldInternal
"""Field enum for GetProductsRequest - controls which product fields are returned.

Values include product_id, name, description, pricing_options, placements, etc.
"""

from adcp.types._generated import FieldModel as GetBrandIdentityFieldInternal

GetBrandIdentityField = GetBrandIdentityFieldInternal
"""Field enum for GetBrandIdentityRequest - controls which brand identity fields are returned.

Values include description, industry, logos, colors, fonts, tone, tagline, etc.
"""

# ============================================================================
# SYNC AUDIENCES INPUT ALIASES
# ============================================================================
# The Audience input type for SyncAudiencesRequest is exported here following
# the same pattern as SyncCreativeResult and SyncCatalogResult.

SyncAudiencesAudience = SyncAudiencesAudienceInternal
"""Audience segment payload for SyncAudiencesRequest.audiences[].

Required: audience_id (buyer's identifier for the audience).
Optional: name, consent_basis, add (AudienceMember items), remove, delete.

Example:
    ```python
    from adcp import SyncAudiencesAudience, SyncAudiencesRequest

    request = SyncAudiencesRequest(
        account={"account_id": "acc_123"},
        audiences=[
            SyncAudiencesAudience(
                audience_id="seg_456",
                name="High-value customers",
                consent_basis="consent"
            )
        ]
    )
    ```
"""

# ============================================================================
# PRICING OPTION UNION TYPE - For Type Hints Without RootModel Wrapper
# ============================================================================
# The generated PricingOption is a RootModel wrapper that mypy doesn't recognize
# as compatible with the individual variant types. This union alias provides a
# way to type-hint pricing options without the wrapper, fixing mypy list-item errors.

PricingOption = (
    CpmPricingOption
    | VcpmPricingOption
    | CpcPricingOption
    | CpcvPricingOption
    | CpvPricingOption
    | CppPricingOption
    | CpaPricingOption
    | FlatRatePricingOption
    | TimeBasedPricingOption
)
"""Union type for all pricing option variants.

Use this for type hints when constructing Product.pricing_options or any field
that accepts pricing options. This fixes mypy list-item errors that occur when
using the individual variant types.

Example:
    ```python
    from adcp.types import Product, CpmPricingOption, PricingOption

    # Type hint for a list of pricing options
    def get_pricing(options: list[PricingOption]) -> None:
        for opt in options:
            print(f"Model: {opt.pricing_model}")

    # Use in Product construction (no more mypy errors!)
    product = Product(
        product_id="test",
        name="Test Product",
        pricing_options=[
            CpmPricingOption(
                pricing_model="cpm",
                floor_price=1.50,
                currency="USD"
            )
        ]
    )
    ```
"""

# ============================================================================
# CREATIVE FORMAT ASSET ALIASES - Discriminated Union on asset_type
# ============================================================================
# AdCP creative format definitions enumerate asset slots as a discriminated
# union on the ``asset_type`` field (image, video, audio, text, markdown,
# html, css, javascript, vast, daast, url, webhook, brief, catalog). The
# code generator emits numbered class names (``Assets``, ``Assets81``,
# ``Assets82``, ...) that renumber between releases whenever the upstream
# ``$defs`` ordering shifts. These aliases pin semantic names so consumers
# never import ``AssetsNN`` directly.
#
# Two asset shapes exist in a format definition:
#
# 1. Individual asset slots (``item_type='individual'``) — top-level slots in
#    a creative format. Aliased as ``<Type>FormatAsset``. The ``Format``
#    prefix disambiguates from the separate asset-content types
#    (``VideoContent``, ``HtmlContent``, etc. in ``adcp.types``) which
#    describe the actual asset payload (codec, duration, file URL)
#    delivered by creative sync — a distinct concept.
# 2. Group asset variants — the same asset types nested inside a
#    ``RepeatableAssetGroup`` (``Assets94``). Aliased as ``<Type>FormatGroupAsset``.
#
# Stability contract: these aliases are covered by
# ``tests/test_asset_aliases_stable.py`` which asserts each alias resolves
# to a class whose ``asset_type`` literal default matches the expected
# value. Generator renumbering is caught there, not in downstream code.

from adcp.types.generated_poc.core import format as _format_module
from adcp.types.generated_poc.core.format import BaseGroupAsset as _BaseGroupAsset
from adcp.types.generated_poc.core.format import BaseIndividualAsset as _BaseIndividualAsset


def _format_asset_class(asset_type: str, *, group: bool = False) -> type:
    base = _BaseGroupAsset if group else _BaseIndividualAsset
    for value in vars(_format_module).values():
        if not isinstance(value, type) or value is base or not issubclass(value, base):
            continue
        field = getattr(value, "model_fields", {}).get("asset_type")
        if field is not None and field.default == asset_type:
            return value
    raise ImportError(f"Could not find generated format asset class for {asset_type!r}")


def _repeatable_asset_group_class() -> type:
    for value in vars(_format_module).values():
        if not isinstance(value, type):
            continue
        field = getattr(value, "model_fields", {}).get("item_type")
        if field is not None and field.default == "repeatable_group":
            return value
    raise ImportError("Could not find generated repeatable asset group class")


_ImageFormatAssetInternal = _format_asset_class("image")
_VideoFormatAssetInternal = _format_asset_class("video")
_AudioFormatAssetInternal = _format_asset_class("audio")
_TextFormatAssetInternal = _format_asset_class("text")
_MarkdownFormatAssetInternal = _format_asset_class("markdown")
_HtmlFormatAssetInternal = _format_asset_class("html")
_CssFormatAssetInternal = _format_asset_class("css")
_JavascriptFormatAssetInternal = _format_asset_class("javascript")
_VastFormatAssetInternal = _format_asset_class("vast")
_DaastFormatAssetInternal = _format_asset_class("daast")
_UrlFormatAssetInternal = _format_asset_class("url")
_WebhookFormatAssetInternal = _format_asset_class("webhook")
_BriefFormatAssetInternal = _format_asset_class("brief")
_CatalogFormatAssetInternal = _format_asset_class("catalog")
_RepeatableAssetGroupInternal = _repeatable_asset_group_class()
_ImageFormatGroupAssetInternal = _format_asset_class("image", group=True)
_VideoFormatGroupAssetInternal = _format_asset_class("video", group=True)
_AudioFormatGroupAssetInternal = _format_asset_class("audio", group=True)
_TextFormatGroupAssetInternal = _format_asset_class("text", group=True)
_MarkdownFormatGroupAssetInternal = _format_asset_class("markdown", group=True)
_HtmlFormatGroupAssetInternal = _format_asset_class("html", group=True)
_CssFormatGroupAssetInternal = _format_asset_class("css", group=True)
_JavascriptFormatGroupAssetInternal = _format_asset_class("javascript", group=True)
_VastFormatGroupAssetInternal = _format_asset_class("vast", group=True)
_DaastFormatGroupAssetInternal = _format_asset_class("daast", group=True)
_UrlFormatGroupAssetInternal = _format_asset_class("url", group=True)
_WebhookFormatGroupAssetInternal = _format_asset_class("webhook", group=True)

ImageFormatAsset = _ImageFormatAssetInternal
"""Image asset slot in a creative format (asset_type='image').

Distinct from ``ImageContent`` in ``adcp.types`` (the asset-content type
describing an actual image payload — dimensions, file URL, etc.). This
alias names the slot shape used inside a format definition.
"""

VideoFormatAsset = _VideoFormatAssetInternal
"""Video asset slot in a creative format (asset_type='video').

Distinct from ``VideoContent`` in ``adcp.types`` (the asset-content type
describing an actual video payload — codec, duration, file URL). This
alias names the slot shape used inside a format definition.
"""

AudioFormatAsset = _AudioFormatAssetInternal
"""Audio asset slot in a creative format (asset_type='audio').

Distinct from ``AudioContent`` in ``adcp.types`` (the asset-content type
describing an actual audio payload). This alias names the slot shape
used inside a format definition.
"""

TextFormatAsset = _TextFormatAssetInternal
"""Text asset slot in a creative format (asset_type='text').

Distinct from ``TextContent`` in ``adcp.types``. This alias names the slot
shape used inside a format definition.
"""

MarkdownFormatAsset = _MarkdownFormatAssetInternal
"""Markdown asset slot in a creative format (asset_type='markdown').

Distinct from the asset-content type in ``adcp.types``. This alias names
the slot shape used inside a format definition.
"""

HtmlFormatAsset = _HtmlFormatAssetInternal
"""HTML asset slot in a creative format (asset_type='html').

Distinct from ``HtmlContent`` in ``adcp.types`` (the asset-content type
describing actual HTML payload). This alias names the slot shape used
inside a format definition.
"""

CssFormatAsset = _CssFormatAssetInternal
"""CSS asset slot in a creative format (asset_type='css').

Distinct from ``CssContent`` in ``adcp.types``. This alias names the slot
shape used inside a format definition.
"""

JavascriptFormatAsset = _JavascriptFormatAssetInternal
"""JavaScript asset slot in a creative format (asset_type='javascript').

Distinct from ``JavascriptContent`` in ``adcp.types``. This alias names
the slot shape used inside a format definition.
"""

VastFormatAsset = _VastFormatAssetInternal
"""VAST asset slot in a creative format (asset_type='vast').

Distinct from ``UrlVastAsset`` / ``InlineVastAsset``, which describe how
the VAST content itself is delivered (url vs inline payload). This alias
names the asset slot inside a format definition.
"""

DaastFormatAsset = _DaastFormatAssetInternal
"""DAAST asset slot in a creative format (asset_type='daast').

Distinct from ``UrlDaastAsset`` / ``InlineDaastAsset``, which describe how
the DAAST content itself is delivered (url vs inline payload).
"""

UrlFormatAsset = _UrlFormatAssetInternal
"""URL asset slot in a creative format (asset_type='url').

Distinct from ``UrlContent`` in ``adcp.types``. This alias names the slot
shape used inside a format definition.
"""

WebhookFormatAsset = _WebhookFormatAssetInternal
"""Webhook asset slot in a creative format (asset_type='webhook').

Distinct from ``WebhookContent`` in ``adcp.types``. This alias names the
slot shape used inside a format definition.
"""

BriefFormatAsset = _BriefFormatAssetInternal
"""Brief asset slot in a creative format (asset_type='brief').

Distinct from the brief asset-content type (not currently exported on
the public surface). This alias names the slot shape used inside a
format definition.
"""

CatalogFormatAsset = _CatalogFormatAssetInternal
"""Catalog asset slot in a creative format (asset_type='catalog').

Distinct from the catalog asset-content type (not currently exported on
the public surface). This alias names the slot shape used inside a
format definition.
"""

RepeatableAssetGroup = _RepeatableAssetGroupInternal
"""Repeatable asset group in a creative format (item_type='repeatable_group').

Holds a sequence of group-variant assets (``<Type>FormatGroupAsset``) that
repeat either sequentially (carousels, playlists) or via platform
optimization.
"""

ImageFormatGroupAsset = _ImageFormatGroupAssetInternal
"""Image asset nested in a RepeatableAssetGroup (asset_type='image')."""

VideoFormatGroupAsset = _VideoFormatGroupAssetInternal
"""Video asset nested in a RepeatableAssetGroup (asset_type='video')."""

AudioFormatGroupAsset = _AudioFormatGroupAssetInternal
"""Audio asset nested in a RepeatableAssetGroup (asset_type='audio')."""

TextFormatGroupAsset = _TextFormatGroupAssetInternal
"""Text asset nested in a RepeatableAssetGroup (asset_type='text')."""

MarkdownFormatGroupAsset = _MarkdownFormatGroupAssetInternal
"""Markdown asset nested in a RepeatableAssetGroup (asset_type='markdown')."""

HtmlFormatGroupAsset = _HtmlFormatGroupAssetInternal
"""HTML asset nested in a RepeatableAssetGroup (asset_type='html')."""

CssFormatGroupAsset = _CssFormatGroupAssetInternal
"""CSS asset nested in a RepeatableAssetGroup (asset_type='css')."""

JavascriptFormatGroupAsset = _JavascriptFormatGroupAssetInternal
"""JavaScript asset nested in a RepeatableAssetGroup (asset_type='javascript')."""

VastFormatGroupAsset = _VastFormatGroupAssetInternal
"""VAST asset slot nested in a RepeatableAssetGroup (asset_type='vast')."""

DaastFormatGroupAsset = _DaastFormatGroupAssetInternal
"""DAAST asset slot nested in a RepeatableAssetGroup (asset_type='daast')."""

UrlFormatGroupAsset = _UrlFormatGroupAssetInternal
"""URL asset slot nested in a RepeatableAssetGroup (asset_type='url')."""

WebhookFormatGroupAsset = _WebhookFormatGroupAssetInternal
"""Webhook asset slot nested in a RepeatableAssetGroup (asset_type='webhook')."""

# ============================================================================
# OPEN UNION TYPES — forward-compat fallback arms for Format.assets
# ============================================================================
# AdCP enums grow additively by design. When a new asset_type arrives before
# the SDK is updated, the closed discriminated union in generated_poc raises a
# cascade of ValidationErrors (one per arm per slot) and zeroes out the entire
# list_creative_formats catalog. These open union types add an unknown fallback
# arm so callers receive unrecognized assets as typed-unknown rather than
# losing every format in the response.
#
# Asymmetric strictness (Postel's Law):
#   - Emit path stays strict: request/write types keep closed Literal arms.
#   - Parse path is lenient: response/read types accept novel discriminators
#     via UnknownFormatAsset / UnknownGroupAsset fallback arms.
#
# The callable Discriminator + Tag pattern is the Pydantic v2 ≥2.5 canonical
# approach for open discriminated unions. _apply_forward_compat() in
# _forward_compat.py patches Format.assets and Assets94.assets with these
# types at import time using model_rebuild(force=True).

_KNOWN_INDIVIDUAL_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "image",
        "video",
        "audio",
        "text",
        "markdown",
        "html",
        "css",
        "javascript",
        "vast",
        "daast",
        "url",
        "webhook",
        "brief",
        "catalog",
    }
)

_KNOWN_GROUP_ASSET_TYPES: frozenset[str] = frozenset(
    {
        "image",
        "video",
        "audio",
        "text",
        "markdown",
        "html",
        "css",
        "javascript",
        "vast",
        "daast",
        "url",
        "webhook",
    }
)


def _format_asset_discriminator(v: Any) -> str:
    """Route to the correct Tag for Format.assets callable Discriminator."""
    if isinstance(v, dict):
        item_type: str = v.get("item_type", "individual")
        asset_type: str = v.get("asset_type", "")
    else:
        item_type = getattr(v, "item_type", "individual")
        asset_type = getattr(v, "asset_type", "")
    if item_type == "repeatable_group":
        return "repeatable_group"
    return asset_type if asset_type in _KNOWN_INDIVIDUAL_ASSET_TYPES else "_unknown"


def _group_asset_discriminator(v: Any) -> str:
    """Route to the correct Tag for Assets94.assets callable Discriminator."""
    if isinstance(v, dict):
        asset_type: str = v.get("asset_type", "")
    else:
        asset_type = getattr(v, "asset_type", "")
    return asset_type if asset_type in _KNOWN_GROUP_ASSET_TYPES else "_unknown"


class UnknownFormatAsset(_BaseIndividualAsset):
    """Fallback arm for individual asset_type values not in the SDK's known set.

    When the AdCP protocol adds a new asset_type before the SDK is updated,
    responses containing that type parse successfully as UnknownFormatAsset
    instead of raising ValidationError for the entire list_creative_formats
    response. Structural fields (asset_id, required) are still validated;
    type-specific fields are preserved in __pydantic_extra__.

    Access extra wire fields via ``asset.__pydantic_extra__ or {}``.

    This type is read-path only. Do not use it in creative manifests or
    emit-side requests — the request path keeps strict Literal validation.
    """

    # extra='allow' is intentionally hardcoded, not inherited from the
    # ADCP_STRICT_VALIDATION env-var policy on AdCPBaseModel. The whole
    # purpose of this fallback arm is to preserve unknown fields from the wire
    # rather than drop or reject them — both behaviors defeat the goal.
    model_config = ConfigDict(extra="allow")
    asset_type: str


class UnknownGroupAsset(_BaseGroupAsset):
    """Fallback arm for group asset_type values not in the SDK's known set.

    Same forward-compat guarantee as UnknownFormatAsset but for assets nested
    inside a RepeatableAssetGroup (Assets94.assets). Access extra wire fields
    via ``asset.__pydantic_extra__ or {}``.
    """

    model_config = ConfigDict(extra="allow")
    asset_type: str


FormatAssetUnion = _Annotated[
    _Annotated[_ImageFormatAssetInternal, Tag("image")]
    | _Annotated[_VideoFormatAssetInternal, Tag("video")]
    | _Annotated[_AudioFormatAssetInternal, Tag("audio")]
    | _Annotated[_TextFormatAssetInternal, Tag("text")]
    | _Annotated[_MarkdownFormatAssetInternal, Tag("markdown")]
    | _Annotated[_HtmlFormatAssetInternal, Tag("html")]
    | _Annotated[_CssFormatAssetInternal, Tag("css")]
    | _Annotated[_JavascriptFormatAssetInternal, Tag("javascript")]
    | _Annotated[_VastFormatAssetInternal, Tag("vast")]
    | _Annotated[_DaastFormatAssetInternal, Tag("daast")]
    | _Annotated[_UrlFormatAssetInternal, Tag("url")]
    | _Annotated[_WebhookFormatAssetInternal, Tag("webhook")]
    | _Annotated[_BriefFormatAssetInternal, Tag("brief")]
    | _Annotated[_CatalogFormatAssetInternal, Tag("catalog")]
    | _Annotated[_RepeatableAssetGroupInternal, Tag("repeatable_group")]
    | _Annotated[UnknownFormatAsset, Tag("_unknown")],
    Discriminator(_format_asset_discriminator),
]
"""Open discriminated union for Format.assets.

Replaces the generated closed union to add UnknownFormatAsset as a fallback
arm. Applied to Format.assets via _forward_compat._apply_forward_compat().
"""

GroupFormatAssetUnion = _Annotated[
    _Annotated[_ImageFormatGroupAssetInternal, Tag("image")]
    | _Annotated[_VideoFormatGroupAssetInternal, Tag("video")]
    | _Annotated[_AudioFormatGroupAssetInternal, Tag("audio")]
    | _Annotated[_TextFormatGroupAssetInternal, Tag("text")]
    | _Annotated[_MarkdownFormatGroupAssetInternal, Tag("markdown")]
    | _Annotated[_HtmlFormatGroupAssetInternal, Tag("html")]
    | _Annotated[_CssFormatGroupAssetInternal, Tag("css")]
    | _Annotated[_JavascriptFormatGroupAssetInternal, Tag("javascript")]
    | _Annotated[_VastFormatGroupAssetInternal, Tag("vast")]
    | _Annotated[_DaastFormatGroupAssetInternal, Tag("daast")]
    | _Annotated[_UrlFormatGroupAssetInternal, Tag("url")]
    | _Annotated[_WebhookFormatGroupAssetInternal, Tag("webhook")]
    | _Annotated[UnknownGroupAsset, Tag("_unknown")],
    Discriminator(_group_asset_discriminator),
]
"""Open discriminated union for Assets94.assets (RepeatableAssetGroup slots).

Applied to Assets94.assets via _forward_compat._apply_forward_compat().
"""

# ============================================================================
# CROSS-MODULE NAME COLLISION ALIASES (#911, Step 2)
# ============================================================================
# Several bare type names are defined in more than one generated module
# (snapshotted in scripts/collision_allowlist.json). When adopters write
# ``from adcp.types import Creative`` they silently get whichever module wins
# the consolidate sort order — which is rarely the variant they want and can
# change shape between releases. The build guard (Step 1) makes a NEW collision
# loud; these aliases (Step 2) give adopters an unambiguous name for each
# per-module variant of the high-traffic, adopter-facing collisions named in
# #911.
#
# Naming convention: ``<Context><BaseName>`` where ``<Context>`` is derived
# from the defining module / verb (e.g. ``SyncAccountsAccount`` from
# ``account.sync_accounts_response``, ``DeliveryCreative`` from
# ``creative.get_creative_delivery_response``). This matches the existing
# precedent in this file (``SyncCreativeResult``, ``MediaBuyDeliveryStatus``,
# ``SyncAudiencesAudience``).
#
# Each alias imports the variant directly from its source module, so it resolves
# to the correct per-module class regardless of which one wins the bare-name
# slot in _generated.py. These do NOT remove the underlying generated_poc
# collisions, so the Step 1 guard + collision_allowlist.json stay as-is.
#
# Stability contract: tests/test_collision_aliases.py asserts each alias
# resolves to the class defined in its named module (by __module__), not the
# first-sorted winner.

# Each alias imports its variant directly from the source module named in the
# alias prefix. Grouped by base name in __all__ below; sorted by module here.
# Notable shape differences worth disambiguating:
#   - creative.list_creatives_response.Creative is the full creative record;
#     get_creative_delivery_response.Creative (the bare-name winner) is a lean
#     totals view.
#   - core.notification_config.Authentication makes ``credentials`` optional;
#     the other four Authentication variants require it.
#   - the four ``Unit`` enums measure different dimensions (duration, overlay
#     positioning, real-estate area, vehicle distance). ``adcp.types.DimensionUnit``
#     is a separate, already-unambiguous enum and is not part of this collision.
from adcp.types.generated_poc.account.sync_accounts_response import (
    Account as SyncAccountsAccount,
)
from adcp.types.generated_poc.account.sync_accounts_response import (
    CreditLimit as SyncAccountsCreditLimit,
)
from adcp.types.generated_poc.account.sync_accounts_response import (
    Setup as SyncAccountsSetup,
)
from adcp.types.generated_poc.account.sync_governance_request import (
    Account as SyncGovernanceAccount,
)
from adcp.types.generated_poc.account.sync_governance_request import (
    Authentication as GovernanceAuthentication,
)
from adcp.types.generated_poc.account.sync_governance_request import (
    GovernanceAgent as SyncGovernanceGovernanceAgent,
)
from adcp.types.generated_poc.core.account import (
    Account as CoreAccount,
)
from adcp.types.generated_poc.core.account import (
    CreditLimit as CoreCreditLimit,
)
from adcp.types.generated_poc.core.account import (
    GovernanceAgent as CoreGovernanceAgent,
)
from adcp.types.generated_poc.core.account import (
    Setup as CoreSetup,
)
from adcp.types.generated_poc.core.duration import (
    Unit as DurationUnit,
)
from adcp.types.generated_poc.core.media_buy import (
    MediaBuy as CoreMediaBuy,
)
from adcp.types.generated_poc.core.notification_config import (
    Authentication as NotificationAuthentication,
)
from adcp.types.generated_poc.core.overlay import (
    Unit as OverlayUnit,
)
from adcp.types.generated_poc.core.provenance import (
    DeclaredBy as ProvenanceDeclaredBy,
)
from adcp.types.generated_poc.core.push_notification_config import (
    Authentication as PushNotificationAuthentication,
)
from adcp.types.generated_poc.core.real_estate_item import (
    Unit as RealEstateUnit,
)
from adcp.types.generated_poc.core.reporting_webhook import (
    Authentication as ReportingWebhookAuthentication,
)
from adcp.types.generated_poc.core.tasks_list_request import (
    Sort as TasksListSort,
)
from adcp.types.generated_poc.core.vehicle_item import (
    Unit as VehicleUnit,
)
from adcp.types.generated_poc.core.wholesale_feed_event import (
    Signal as WholesaleFeedSignal,
)
from adcp.types.generated_poc.creative.get_creative_delivery_response import (
    Creative as DeliveryCreative,
)
from adcp.types.generated_poc.creative.list_creatives_request import (
    Sort as ListCreativesSort,
)
from adcp.types.generated_poc.creative.list_creatives_response import (
    Creative as ListCreativesCreative,
)
from adcp.types.generated_poc.creative.sync_creatives_response import (
    Creative as SyncCreativesCreative,
)
from adcp.types.generated_poc.media_buy.build_creative_response import (
    Creative as BuildCreativeCreative,
)
from adcp.types.generated_poc.media_buy.create_media_buy_request import (
    Authentication as CreateMediaBuyAuthentication,
)
from adcp.types.generated_poc.media_buy.get_media_buys_response import (
    MediaBuy as GetMediaBuysMediaBuy,
)
from adcp.types.generated_poc.media_buy.sync_event_sources_response import (
    Setup as SyncEventSourcesSetup,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Account as CapabilitiesAccount,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Creative as CapabilitiesCreative,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    MediaBuy as CapabilitiesMediaBuy,
)
from adcp.types.generated_poc.protocol.list_tasks_request import (
    Sort as ListTasksSort,
)
from adcp.types.generated_poc.signals.get_signals_response import (
    Signal as GetSignalsSignal,
)
from adcp.types.generated_poc.sponsored_intelligence.si_sponsored_context import (
    DeclaredBy as SiSponsoredContextDeclaredBy,
)

# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    # Cross-module name collision aliases (#911, Step 2)
    # Creative
    "DeliveryCreative",
    "ListCreativesCreative",
    "SyncCreativesCreative",
    "BuildCreativeCreative",
    "CapabilitiesCreative",
    # Account
    "CoreAccount",
    "SyncAccountsAccount",
    "SyncGovernanceAccount",
    "CapabilitiesAccount",
    # Authentication
    "PushNotificationAuthentication",
    "NotificationAuthentication",
    "ReportingWebhookAuthentication",
    "GovernanceAuthentication",
    "CreateMediaBuyAuthentication",
    # MediaBuy
    "CoreMediaBuy",
    "GetMediaBuysMediaBuy",
    "CapabilitiesMediaBuy",
    # GovernanceAgent
    "CoreGovernanceAgent",
    "SyncGovernanceGovernanceAgent",
    # CreditLimit
    "CoreCreditLimit",
    "SyncAccountsCreditLimit",
    # Setup
    "CoreSetup",
    "SyncAccountsSetup",
    "SyncEventSourcesSetup",
    # Sort
    "ListCreativesSort",
    "TasksListSort",
    "ListTasksSort",
    # Signal
    "GetSignalsSignal",
    "WholesaleFeedSignal",
    # DeclaredBy
    "ProvenanceDeclaredBy",
    "SiSponsoredContextDeclaredBy",
    # Unit
    "DurationUnit",
    "OverlayUnit",
    "RealEstateUnit",
    "VehicleUnit",
    # Account reference variants
    "AccountReferenceById",
    "AccountReferenceByNaturalKey",
    # Format identifier (canonical core class, AdCP 3.0.1+)
    "FormatId",
    # Canonical-formats v2 surface (AdCP 3.1)
    "CanonicalAssetSource",
    "CanonicalCompositionModel",
    "CanonicalFormatAgentPlacement",
    "CanonicalFormatBase",
    "CanonicalFormatDaastAudio",
    "CanonicalFormatDisplayTag",
    "CanonicalFormatHostedAudio",
    "CanonicalFormatHostedVideo",
    "CanonicalFormatHtml5Banner",
    "CanonicalFormatImage",
    "CanonicalFormatImageCarousel",
    "CanonicalFormatKind",
    "CanonicalFormatNativeInFeed",
    "CanonicalFormatResponsiveCreative",
    "CanonicalFormatSponsoredPlacement",
    "CanonicalFormatVastVideo",
    "CanonicalProjectionReference",
    "CanonicalSlotOverride",
    "PixelTrackerAsset",
    "PixelTrackerEvent",
    "PixelTrackerMethod",
    # Error envelope sub-enums (for SDK advisory construction)
    "Recovery",
    "Source",
    "ProductFormatDeclaration",
    "ProductFormatSellerPreference",
    "V1CanonicalDimensions",
    "V1CanonicalGlobPattern",
    "V1CanonicalMapping",
    "V1CanonicalStructural",
    "V1CanonicalStructuralPattern",
    "V1CanonicalV2Projection",
    "V1V2CanonicalFormatMappingRegistry",
    # Activation key variants
    "SegmentIdActivationKey",
    "KeyValueActivationKey",
    # Activation responses
    "ActivateSignalSuccessResponse",
    "ActivateSignalErrorResponse",
    # Asset type aliases
    "BothPreviewRender",
    "HtmlPreviewRender",
    "InlineDaastAsset",
    "InlineVastAsset",
    "UrlDaastAsset",
    "UrlPreviewRender",
    "UrlVastAsset",
    # Authorized agent variants
    "AuthorizedAgentsByPropertyId",
    "AuthorizedAgentsByPropertyTag",
    "AuthorizedAgentsByInlineProperties",
    "AuthorizedAgentsByPublisherProperties",
    "AuthorizedAgentsBySignalId",
    "AuthorizedAgentsBySignalTag",
    # Authorized agent union
    "AuthorizedAgent",
    # Build creative responses
    "BuildCreativeResponse3",
    "BuildCreativeResponse4",
    "BuildCreativeResponse5",
    "BuildCreativeResponse6",
    "BuildCreativeSuccessResponse",
    "BuildCreativeErrorResponse",
    "BuildCreativeSubmittedResponse",
    # Calibrate content responses
    "CalibrateContentSuccessResponse",
    "CalibrateContentErrorResponse",
    # Content standards responses
    "CreateContentStandardsSuccessResponse",
    "CreateContentStandardsErrorResponse",
    "UpdateContentStandardsSuccessResponse",
    "UpdateContentStandardsErrorResponse",
    "GetContentStandardsSuccessResponse",
    "GetContentStandardsErrorResponse",
    "ListContentStandardsSuccessResponse",
    "ListContentStandardsErrorResponse",
    # Create media buy responses
    "CreateMediaBuyResponse1",
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuyErrorResponse",
    "CreateMediaBuySubmittedResponse",
    # Creative delivery requests
    "GetCreativeDeliveryByMediaBuyRequest",
    "GetCreativeDeliveryByBuyerRefRequest",
    "GetCreativeDeliveryByCreativeRequest",
    # Log event responses
    "LogEventSuccessResponse",
    "LogEventErrorResponse",
    # Media buy artifacts responses
    "GetMediaBuyArtifactsSuccessResponse",
    "GetMediaBuyArtifactsErrorResponse",
    # Performance feedback responses
    "ProvidePerformanceFeedbackSuccessResponse",
    "ProvidePerformanceFeedbackErrorResponse",
    # Preview creative responses
    "PreviewCreativeSingleResponse",
    "PreviewCreativeBatchResponse",
    "PreviewCreativeVariantResponse",
    # Get products request variants
    "GetProductsBriefRequest",
    "GetProductsWholesaleRequest",
    "GetProductsRefineRequest",
    # Get signals request variants
    "GetSignalsDiscoveryRequest",
    "GetSignalsLookupRequest",
    # Get products response variants (async discovery)
    "GetProductsSuccessResponse",
    "GetProductsSubmittedResponse",
    "GetProductsWorkingResponse",
    "GetProductsInputRequiredResponse",
    "GetProductsResponseUnion",
    # Get signals response variants (async discovery)
    "GetSignalsSuccessResponse",
    "GetSignalsSubmittedResponse",
    "GetSignalsWorkingResponse",
    "GetSignalsResponseUnion",
    # Performance feedback request variants
    "ProvidePerformanceFeedbackByMediaBuyRequest",
    "ProvidePerformanceFeedbackByBuyerRefRequest",
    # SI send message request variants
    "SiSendTextMessageRequest",
    "SiSendActionResponseRequest",
    # Sync accounts responses
    "SyncAccountsSuccessResponse",
    "SyncAccountsErrorResponse",
    # Sync creatives responses
    "SyncCreativesSuccessResponse",
    "SyncCreativesErrorResponse",
    "SyncCreativesSubmittedResponse",
    "SyncCreativeResult",
    # Sync catalogs responses
    "SyncCatalogResult",
    "SyncCatalogsSuccessResponse",
    "SyncCatalogsErrorResponse",
    "SyncCatalogsSubmittedResponse",
    # Sync event sources responses
    "SyncEventSourcesSuccessResponse",
    "SyncEventSourcesErrorResponse",
    # Update media buy requests
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
    # Update media buy responses
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuyErrorResponse",
    "UpdateMediaBuyResponse3",
    "UpdateMediaBuySubmittedResponse",
    # Validate content delivery responses
    "ValidateContentDeliverySuccessResponse",
    "ValidateContentDeliveryErrorResponse",
    # Get account financials responses
    "GetAccountFinancialsSuccessResponse",
    "GetAccountFinancialsErrorResponse",
    # Sync audiences responses
    "SyncAudiencesSuccessResponse",
    "SyncAudiencesErrorResponse",
    "SyncAudiencesSubmittedResponse",
    # Get creative features responses
    "GetCreativeFeaturesSuccessResponse",
    "GetCreativeFeaturesErrorResponse",
    # Package type aliases
    "Package",
    # Publisher properties types
    "PropertyId",
    "PropertyTag",
    # Publisher properties variants
    "PublisherPropertiesAll",
    "PublisherPropertiesById",
    "PublisherPropertiesByTag",
    # Publisher properties union
    "PublisherProperties",
    # Deployment variants
    "PlatformDeployment",
    "AgentDeployment",
    # Deployment union
    "Deployment",
    # Destination variants
    "PlatformDestination",
    "AgentDestination",
    # Destination union
    "Destination",
    # Pricing option union
    "PricingOption",
    # Sync audiences input type
    "SyncAudiencesAudience",
    "ConsentBasis",
    # Status (backward compat - delivery status, not invoice status)
    "MediaBuyDeliveryStatus",
    # Catalog field binding semantic alias
    "CatalogGroupBinding",
    # Field enum disambiguation aliases
    "GetProductsField",
    "GetBrandIdentityField",
    # Brand Rights response aliases
    "AcquireRightsAcquiredResponse",
    "AcquireRightsPendingResponse",
    "AcquireRightsRejectedResponse",
    "AcquireRightsErrorResponse",
    "GetBrandIdentitySuccessResponse",
    "GetBrandIdentityErrorResponse",
    "GetRightsSuccessResponse",
    "GetRightsErrorResponse",
    # Compliance Test Controller response aliases
    "ComplyListScenariosResponse",
    "ComplyStateTransitionResponse",
    "ComplySimulationResponse",
    "ComplyErrorResponse",
    # Creative format asset slot aliases (item_type='individual')
    # Forward-compat fallback arms (novel asset_type values parse as these)
    "UnknownFormatAsset",
    "UnknownGroupAsset",
    # Open union types (Format.assets / Assets94.assets after _forward_compat patch)
    "FormatAssetUnion",
    "GroupFormatAssetUnion",
    "ImageFormatAsset",
    "VideoFormatAsset",
    "AudioFormatAsset",
    "TextFormatAsset",
    "MarkdownFormatAsset",
    "HtmlFormatAsset",
    "CssFormatAsset",
    "JavascriptFormatAsset",
    "VastFormatAsset",
    "DaastFormatAsset",
    "UrlFormatAsset",
    "WebhookFormatAsset",
    "BriefFormatAsset",
    "CatalogFormatAsset",
    # Creative format asset slot aliases (repeatable groups)
    "RepeatableAssetGroup",
    "ImageFormatGroupAsset",
    "VideoFormatGroupAsset",
    "AudioFormatGroupAsset",
    "TextFormatGroupAsset",
    "MarkdownFormatGroupAsset",
    "HtmlFormatGroupAsset",
    "CssFormatGroupAsset",
    "JavascriptFormatGroupAsset",
    "VastFormatGroupAsset",
    "DaastFormatGroupAsset",
    "UrlFormatGroupAsset",
    "WebhookFormatGroupAsset",
]


# === Post-hoc XOR enforcement on PublisherPropertySelector{1,3} ===
#
# datamodel-code-generator cannot translate the publisher-property-selector
# JSON Schema's `allOf[not[required[both]]] + anyOf[required[either]]`
# construct into Pydantic field constraints (adcp#4504, tracked as
# adcp-client-python#759). Without this patch, direct instantiation of
# the generated selector classes silently accepts payloads the schema
# rejects:
#
#     PublisherPropertySelector1(selection_type="all")  # would pass — bug
#     PublisherPropertySelector3(publisher_domain="a", publisher_domains=["b"])  # would pass — bug
#
# This block attaches an `@model_validator(mode="after")` to the
# generated classes at import time. Implementation note: the supported
# Pydantic-2 API for adding a validator post-hoc to an existing class
# does not exist; we use `pydantic._internal._decorators.Decorator` —
# private but stable across Pydantic 2.x point releases. A drift test
# (``tests/test_publisher_selector_xor_autoenforce.py``) fails loudly if
# Pydantic ever changes the registration shape so the issue surfaces in
# CI rather than as runtime validation regressions.
#
# Scope:
# - PublisherPropertySelector1 (selection_type="all") — both XORs apply
# - PublisherPropertySelector3 (selection_type="by_tag") — both XORs apply
# - PublisherPropertySelector2 (selection_type="by_id") — no XOR (by_id
#   carries only publisher_domain by spec; publisher_domains is rejected
#   at the JSON-schema level). Left unpatched.
from pydantic._internal._decorators import (  # noqa: E402
    Decorator as _PydanticDecorator,
)
from pydantic._internal._decorators import (  # noqa: E402
    ModelValidatorDecoratorInfo as _ModelValidatorDecoratorInfo,
)

from adcp.types._generated import (  # noqa: E402
    PublisherPropertySelector1 as _Selector1,
)
from adcp.types._generated import (
    PublisherPropertySelector3 as _Selector3,
)


def _selector_xor_validate(self: Any) -> Any:
    """Enforce publisher_domain XOR publisher_domains[] on selector 1 / 3.

    Runs after Pydantic has populated the fields. Defers the full
    diagnostic shape to `validate_publisher_properties_item` for parity
    with the dict-path enforcement; a violation surfaces here as a
    Pydantic `ValidationError` (containing the helper's message) rather
    than as the helper's `ValidationError` directly.
    """
    # Local import — avoids a top-level cycle through adcp.validation
    # back into types.aliases.
    from adcp.validation.legacy import (
        ValidationError as _LegacyValidationError,
    )
    from adcp.validation.legacy import (
        validate_publisher_properties_item as _validate_item,
    )

    try:
        _validate_item(self)
    except _LegacyValidationError as exc:
        raise ValueError(str(exc)) from exc
    return self


def _attach_selector_xor_validator(cls: type) -> None:
    """Inject a model_validator(mode='after') onto an existing Pydantic class.

    The supported decorator path is class-definition-time only; the
    generated selector classes can't carry the validator without
    modifying generated code (forbidden — overwritten on next regen).
    This walks the same `_internal._decorators` machinery the decorator
    syntax uses, then forces a `model_rebuild` so Pydantic re-derives
    its core schema with the new validator included.
    """
    cls._selector_xor_validate = _selector_xor_validate  # type: ignore[attr-defined]
    info = _ModelValidatorDecoratorInfo(mode="after")
    decorator = _PydanticDecorator.build(
        cls,
        cls_var_name="_selector_xor_validate",
        shim=None,
        info=info,
    )
    cls.__pydantic_decorators__.model_validators[  # type: ignore[attr-defined]
        "_selector_xor_validate"
    ] = decorator
    cls.model_rebuild(force=True)  # type: ignore[attr-defined]


_attach_selector_xor_validator(_Selector1)
_attach_selector_xor_validator(_Selector3)
