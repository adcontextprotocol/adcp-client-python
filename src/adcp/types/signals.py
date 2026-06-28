"""AdCP signals types — curated partial surface.

Signal types — discovery / activation, signal targeting, and audiences.

A stable, narrow alternative to importing the whole :mod:`adcp.types`
namespace. Every name here is also exported from :mod:`adcp.types`; this
module simply groups the ones a signals integration reaches for, and never
exposes the internal generated layer.

This module is for curation and discoverability, not a separate
performance tier: importing it is cheap, but the first access to *any* AdCP
type (here or via :mod:`adcp.types` / :mod:`adcp`) realizes the full generated
Pydantic graph — there is no per-domain graph. Use it for a smaller, focused
import surface.

    from adcp.types.signals import GetSignalsRequest
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "GetSignalsRequest",
    "GetSignalsResponse",
    "GetSignalsResponseUnion",
    "GetSignalsSuccessResponse",
    "GetSignalsWorkingResponse",
    "GetSignalsSubmittedResponse",
    "GetSignalsDiscoveryRequest",
    "GetSignalsLookupRequest",
    "GetSignalsSignal",
    "ActivateSignalRequest",
    "ActivateSignalResponse",
    "ActivateSignalSuccessResponse",
    "ActivateSignalErrorResponse",
    "Signal",
    "SignalListing",
    "SignalRef",
    "SignalFilters",
    "SignalDefinitionEnrichment",
    "SignalTargeting",
    "SignalTargetingExpression",
    "SignalTargetingRules",
    "SignalPricingOption",
    "SignalAvailabilityType",
    "SignalCatalogType",
    "PackageSignalTargeting",
    "PackageSignalTargetingGroup",
    "PackageSignalTargetingGroups",
    "ProductSignalTargetingOption",
    "SegmentIdActivationKey",
    "KeyValueActivationKey",
    "AudienceSource",
    "SyncAudiencesRequest",
    "SyncAudiencesResponse",
    "SyncAudiencesSuccessResponse",
    "SyncAudiencesSubmittedResponse",
    "SyncAudiencesErrorResponse",
    "SyncAudiencesAudience",
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
        ActivateSignalErrorResponse,
        ActivateSignalRequest,
        ActivateSignalResponse,
        ActivateSignalSuccessResponse,
        AudienceSource,
        GetSignalsDiscoveryRequest,
        GetSignalsLookupRequest,
        GetSignalsRequest,
        GetSignalsResponse,
        GetSignalsResponseUnion,
        GetSignalsSignal,
        GetSignalsSubmittedResponse,
        GetSignalsSuccessResponse,
        GetSignalsWorkingResponse,
        KeyValueActivationKey,
        PackageSignalTargeting,
        PackageSignalTargetingGroup,
        PackageSignalTargetingGroups,
        ProductSignalTargetingOption,
        SegmentIdActivationKey,
        Signal,
        SignalAvailabilityType,
        SignalCatalogType,
        SignalDefinitionEnrichment,
        SignalFilters,
        SignalListing,
        SignalPricingOption,
        SignalRef,
        SignalTargeting,
        SignalTargetingExpression,
        SignalTargetingRules,
        SyncAudiencesAudience,
        SyncAudiencesErrorResponse,
        SyncAudiencesRequest,
        SyncAudiencesResponse,
        SyncAudiencesSubmittedResponse,
        SyncAudiencesSuccessResponse,
    )
