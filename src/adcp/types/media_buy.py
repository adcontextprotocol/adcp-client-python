"""AdCP media buy types — curated partial surface.

Media-buy lifecycle types — create / update / get media buys, packages,
delivery, pacing, budget, targeting overlays, and media-buy status.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a media buy integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.media_buy import CreateMediaBuyRequest
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "ListProductsRequest",
    "ListProductsResponse",
    "RequestProposalsRequest",
    "RequestProposalsResponse",
    "RefineProposalsRequest",
    "RefineProposalsResponse",
    "DeclineProposalsRequest",
    "DeclineProposalsResponse",
    "BuyProductsRequest",
    "BuyProductsResponse",
    "AcceptProposalRequest",
    "AcceptProposalResponse",
    "AcceptedLoss",
    "AcceptanceContext",
    "AcceptancePolicyCatalog",
    "AcceptancePolicyDiscovery",
    "AcceptancePolicyProfile",
    "AcceptancePolicyProfileId",
    "AcceptancePolicyProfileIds",
    "AcceptancePolicyRequirement",
    "AcceptancePolicyRule",
    "RegistryAcceptancePolicyProfileReference",
    "CompatibilityPurchaseCoordinatorInput",
    "ControlMediaBuyRequest",
    "ControlMediaBuyResponse",
    "CreateMediaBuyRequest",
    "CreateMediaBuyResponse",
    "CreateMediaBuySuccessResponse",
    "CreateMediaBuySubmittedResponse",
    "CreateMediaBuyErrorResponse",
    "UpdateMediaBuyRequest",
    "UpdateMediaBuyResponse",
    "UpdateMediaBuySuccessResponse",
    "UpdateMediaBuySubmittedResponse",
    "UpdateMediaBuyErrorResponse",
    "UpdateMediaBuyPackagesRequest",
    "UpdateMediaBuyPropertiesRequest",
    "GetMediaBuysRequest",
    "GetMediaBuysResponse",
    "GetMediaBuyDeliveryRequest",
    "GetMediaBuyDeliveryResponse",
    "GetMediaBuyArtifactsRequest",
    "GetMediaBuyArtifactsResponse",
    "MediaBuy",
    "MediaBuyPackage",
    "MediaBuyDelivery",
    "MediaBuyDeliveryStatus",
    "MediaBuyStatus",
    "MediaBuyFeatures",
    "Package",
    "PackageRequest",
    "PackageUpdate",
    "AssignedPackage",
    "Pacing",
    "Overlay",
    "TargetingOverlay",
    "FrequencyCap",
    "FrequencyCapScope",
    "DaypartTarget",
    "OptimizationGoal",
    "DeliveryMetrics",
    "DeliveryStatus",
    "DeliveryType",
    "DailyBreakdownItem",
    "ByPackageItem",
    "Totals",
    "AggregatedTotals",
    "Proposal",
    "Results",
]


if not TYPE_CHECKING:
    # Lazy runtime resolution (shared with the other partial modules). Defined
    # under ``not TYPE_CHECKING`` so type checkers see the surface only via the
    # explicit ``TYPE_CHECKING`` re-export block below — a typo'd import is
    # flagged rather than silently typed as ``object``.
    from adcp.types._partial import lazy_partial_surface

    __getattr__, __dir__ = lazy_partial_surface(__name__, __all__, globals())


if TYPE_CHECKING:
    # Eager re-export so type checkers and IDEs see the surface; resolved
    # lazily through ``__getattr__`` at runtime.
    from adcp.types import (  # noqa: F401
        AcceptanceContext,
        AcceptancePolicyCatalog,
        AcceptancePolicyDiscovery,
        AcceptancePolicyProfile,
        AcceptancePolicyProfileId,
        AcceptancePolicyProfileIds,
        AcceptancePolicyRequirement,
        AcceptancePolicyRule,
        AcceptedLoss,
        AcceptProposalRequest,
        AcceptProposalResponse,
        AggregatedTotals,
        AssignedPackage,
        BuyProductsRequest,
        BuyProductsResponse,
        ByPackageItem,
        CompatibilityPurchaseCoordinatorInput,
        ControlMediaBuyRequest,
        ControlMediaBuyResponse,
        CreateMediaBuyErrorResponse,
        CreateMediaBuyRequest,
        CreateMediaBuyResponse,
        CreateMediaBuySubmittedResponse,
        CreateMediaBuySuccessResponse,
        DailyBreakdownItem,
        DaypartTarget,
        DeclineProposalsRequest,
        DeclineProposalsResponse,
        DeliveryMetrics,
        DeliveryStatus,
        DeliveryType,
        FrequencyCap,
        FrequencyCapScope,
        GetMediaBuyArtifactsRequest,
        GetMediaBuyArtifactsResponse,
        GetMediaBuyDeliveryRequest,
        GetMediaBuyDeliveryResponse,
        GetMediaBuysRequest,
        GetMediaBuysResponse,
        ListProductsRequest,
        ListProductsResponse,
        MediaBuy,
        MediaBuyDelivery,
        MediaBuyDeliveryStatus,
        MediaBuyFeatures,
        MediaBuyPackage,
        MediaBuyStatus,
        OptimizationGoal,
        Overlay,
        Pacing,
        Package,
        PackageRequest,
        PackageUpdate,
        Proposal,
        RefineProposalsRequest,
        RefineProposalsResponse,
        RegistryAcceptancePolicyProfileReference,
        RequestProposalsRequest,
        RequestProposalsResponse,
        Results,
        TargetingOverlay,
        Totals,
        UpdateMediaBuyErrorResponse,
        UpdateMediaBuyPackagesRequest,
        UpdateMediaBuyPropertiesRequest,
        UpdateMediaBuyRequest,
        UpdateMediaBuyResponse,
        UpdateMediaBuySubmittedResponse,
        UpdateMediaBuySuccessResponse,
    )
