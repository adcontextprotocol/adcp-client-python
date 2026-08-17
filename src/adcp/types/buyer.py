"""AdCP buyer types — curated partial surface.

Buy-side (DSP / agency) surface — product discovery, briefs / refine,
pricing, media buys the buyer sends, performance feedback, brand rights.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a buyer integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.buyer import GetProductsRequest
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
    "ControlMediaBuyRequest",
    "ControlMediaBuyResponse",
    "GetProductsRequest",
    "GetProductsResponse",
    "GetProductsResponseUnion",
    "GetProductsSuccessResponse",
    "GetProductsWorkingResponse",
    "GetProductsSubmittedResponse",
    "GetProductsInputRequiredResponse",
    "GetProductsBriefRequest",
    "GetProductsRefineRequest",
    "GetProductsWholesaleRequest",
    "Product",
    "ProductCard",
    "ProductCardDetailed",
    "ProductFilters",
    "Refine",
    "RefinementApplied",
    "CreateMediaBuyRequest",
    "UpdateMediaBuyRequest",
    "PackageRequest",
    "TargetingOverlay",
    "ProvidePerformanceFeedbackRequest",
    "ProvidePerformanceFeedbackResponse",
    "ProvidePerformanceFeedbackByMediaBuyRequest",
    "ProvidePerformanceFeedbackByBuyerRefRequest",
    "PerformanceFeedback",
    "FeedbackSource",
    "PricingOption",
    "PriceGuidance",
    "PricingModel",
    "PricingCurrency",
    "CpmPricingOption",
    "CpcPricingOption",
    "CpaPricingOption",
    "FlatRatePricingOption",
    "AcquireRightsRequest",
    "GetRightsRequest",
    "GetBrandIdentityRequest",
    "VerifyBrandClaimsRequest",
    "BrandReference",
    "BrandSource",
    "RightsTerms",
    "RightsPricingOption",
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
        AcceptProposalRequest,
        AcceptProposalResponse,
        AcquireRightsRequest,
        BrandReference,
        BrandSource,
        BuyProductsRequest,
        BuyProductsResponse,
        ControlMediaBuyRequest,
        ControlMediaBuyResponse,
        CpaPricingOption,
        CpcPricingOption,
        CpmPricingOption,
        CreateMediaBuyRequest,
        DeclineProposalsRequest,
        DeclineProposalsResponse,
        FeedbackSource,
        FlatRatePricingOption,
        GetBrandIdentityRequest,
        GetProductsBriefRequest,
        GetProductsInputRequiredResponse,
        GetProductsRefineRequest,
        GetProductsRequest,
        GetProductsResponse,
        GetProductsResponseUnion,
        GetProductsSubmittedResponse,
        GetProductsSuccessResponse,
        GetProductsWholesaleRequest,
        GetProductsWorkingResponse,
        GetRightsRequest,
        ListProductsRequest,
        ListProductsResponse,
        PackageRequest,
        PerformanceFeedback,
        PriceGuidance,
        PricingCurrency,
        PricingModel,
        PricingOption,
        Product,
        ProductCard,
        ProductCardDetailed,
        ProductFilters,
        ProvidePerformanceFeedbackByBuyerRefRequest,
        ProvidePerformanceFeedbackByMediaBuyRequest,
        ProvidePerformanceFeedbackRequest,
        ProvidePerformanceFeedbackResponse,
        Refine,
        RefinementApplied,
        RefineProposalsRequest,
        RefineProposalsResponse,
        RequestProposalsRequest,
        RequestProposalsResponse,
        RightsPricingOption,
        RightsTerms,
        TargetingOverlay,
        UpdateMediaBuyRequest,
        VerifyBrandClaimsRequest,
    )
