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
    "ActivateSignalResponse1",
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
    # Defined under ``not TYPE_CHECKING`` so type checkers see the surface only
    # via the explicit ``TYPE_CHECKING`` re-export block below — a typo'd import
    # is flagged rather than silently typed as ``object``. Runtime stays lazy.

    def __getattr__(name: str) -> object:
        """Resolve a name from :mod:`adcp.types` (PEP 562), caching the result."""
        if name in __all__:
            import adcp.types

            value = getattr(adcp.types, name)
            globals()[name] = value
            return value
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __dir__() -> list[str]:
        return sorted(__all__)


if TYPE_CHECKING:
    # Eager re-export so type checkers and IDEs see the surface; resolved
    # lazily through ``__getattr__`` at runtime.
    from adcp.types import (  # noqa: F401
        ActivateSignalErrorResponse,
        ActivateSignalRequest,
        ActivateSignalResponse,
        ActivateSignalResponse1,
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
