"""Types declared by the AdCP ``core/targeting`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.targeting import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.targeting import (
    AgeRestriction,
    AudienceExclude,
    AudienceInclude,
    AxeExcludeSegment,
    AxeIncludeSegment,
    Browser,
    BrowserExclude,
    CollectionListExclude,
    DaypartTargets,
    Demographics,
    DevicePlatform,
    DevicePlatformExclude,
    DeviceType,
    DeviceTypeExclude,
    GeoCountries,
    GeoCountriesExclude,
    GeoCountriesExcludeItem,
    GeoCountry,
    GeoMetrosExclude,
    GeoMetrosExcludeItem,
    GeoPlaces,
    GeoPlacesExclude,
    GeoPostalAreas,
    GeoPostalAreasExclude,
    GeoProximity,
    GeoProximityItem,
    GeoProximityItem2,
    GeoRegion,
    GeoRegions,
    GeoRegionsExclude,
    GeoRegionsExcludeItem,
    Geometry,
    Geometry3,
    KeywordTarget,
    PlacementSelection,
    PropertyListExclude,
    Radius,
    StoreCatchment,
    StoreCatchments,
    TargetingOverlay,
    TravelTime,
    Type,
)

# Explicit exports
__all__ = [
    "AgeRestriction",
    "AudienceExclude",
    "AudienceInclude",
    "AxeExcludeSegment",
    "AxeIncludeSegment",
    "Browser",
    "BrowserExclude",
    "CollectionListExclude",
    "DaypartTargets",
    "Demographics",
    "DevicePlatform",
    "DevicePlatformExclude",
    "DeviceType",
    "DeviceTypeExclude",
    "GeoCountries",
    "GeoCountriesExclude",
    "GeoCountriesExcludeItem",
    "GeoCountry",
    "GeoMetrosExclude",
    "GeoMetrosExcludeItem",
    "GeoPlaces",
    "GeoPlacesExclude",
    "GeoPostalAreas",
    "GeoPostalAreasExclude",
    "GeoProximity",
    "GeoProximityItem",
    "GeoProximityItem2",
    "GeoRegion",
    "GeoRegions",
    "GeoRegionsExclude",
    "GeoRegionsExcludeItem",
    "Geometry",
    "Geometry3",
    "KeywordTarget",
    "PlacementSelection",
    "PropertyListExclude",
    "Radius",
    "StoreCatchment",
    "StoreCatchments",
    "TargetingOverlay",
    "TravelTime",
    "Type",
]
