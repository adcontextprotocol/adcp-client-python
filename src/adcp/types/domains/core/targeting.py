"""Types declared by the AdCP ``core/targeting`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.targeting import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-04 01:19:07 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.targeting import (
    AgeRestriction,
    GeoCountriesExcludeItem,
    GeoCountry,
    GeoMetrosExcludeItem,
    GeoProximityItem,
    GeoRegion,
    GeoRegionsExcludeItem,
    Geometry,
    KeywordTarget,
    Radius,
    StoreCatchment,
    TargetingOverlay,
    TravelTime,
    Type,
)

# Explicit exports
__all__ = [
    "AgeRestriction",
    "GeoCountriesExcludeItem",
    "GeoCountry",
    "GeoMetrosExcludeItem",
    "GeoProximityItem",
    "GeoRegion",
    "GeoRegionsExcludeItem",
    "Geometry",
    "KeywordTarget",
    "Radius",
    "StoreCatchment",
    "TargetingOverlay",
    "TravelTime",
    "Type",
]
