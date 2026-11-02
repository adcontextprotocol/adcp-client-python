"""Types the AdCP ``pricing_options`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.pricing_options import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.pricing_options.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.pricing_options.cpa_option import CpaPricingOption
    from adcp.types.domains.pricing_options.cpc_option import CpcPricingOption
    from adcp.types.domains.pricing_options.cpcv_option import CpcvPricingOption
    from adcp.types.domains.pricing_options.cpm_option import CpmPricingOption
    from adcp.types.domains.pricing_options.cpp_option import CppPricingOption
    from adcp.types.domains.pricing_options.cpv_option import (
        CpvPricingOption,
        ViewThreshold,
        ViewThreshold1,
    )
    from adcp.types.domains.pricing_options.flat_rate_option import FlatRatePricingOption
    from adcp.types.domains.pricing_options.price_adjustments import (
        Kind,
        PriceAdjustments,
        PriceAdjustments1,
        PriceAdjustments2,
        PriceAdjustments3,
    )
    from adcp.types.domains.pricing_options.price_breakdown import Adjustment, PriceBreakdown
    from adcp.types.domains.pricing_options.price_guidance import PriceGuidance
    from adcp.types.domains.pricing_options.revenue_share_option import RevenueSharePricingOption
    from adcp.types.domains.pricing_options.time_option import TimeBasedPricingOption, TimeUnit
    from adcp.types.domains.pricing_options.vcpm_option import VcpmPricingOption

# Explicit exports
__all__ = [
    "Adjustment",
    "CpaPricingOption",
    "CpcPricingOption",
    "CpcvPricingOption",
    "CpmPricingOption",
    "CppPricingOption",
    "CpvPricingOption",
    "FlatRatePricingOption",
    "Kind",
    "PriceAdjustments",
    "PriceAdjustments1",
    "PriceAdjustments2",
    "PriceAdjustments3",
    "PriceBreakdown",
    "PriceGuidance",
    "RevenueSharePricingOption",
    "TimeBasedPricingOption",
    "TimeUnit",
    "VcpmPricingOption",
    "ViewThreshold",
    "ViewThreshold1",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Adjustment": "adcp.types.domains.pricing_options.price_breakdown",
    "CpaPricingOption": "adcp.types.domains.pricing_options.cpa_option",
    "CpcPricingOption": "adcp.types.domains.pricing_options.cpc_option",
    "CpcvPricingOption": "adcp.types.domains.pricing_options.cpcv_option",
    "CpmPricingOption": "adcp.types.domains.pricing_options.cpm_option",
    "CppPricingOption": "adcp.types.domains.pricing_options.cpp_option",
    "CpvPricingOption": "adcp.types.domains.pricing_options.cpv_option",
    "FlatRatePricingOption": "adcp.types.domains.pricing_options.flat_rate_option",
    "Kind": "adcp.types.domains.pricing_options.price_adjustments",
    "PriceAdjustments": "adcp.types.domains.pricing_options.price_adjustments",
    "PriceAdjustments1": "adcp.types.domains.pricing_options.price_adjustments",
    "PriceAdjustments2": "adcp.types.domains.pricing_options.price_adjustments",
    "PriceAdjustments3": "adcp.types.domains.pricing_options.price_adjustments",
    "PriceBreakdown": "adcp.types.domains.pricing_options.price_breakdown",
    "PriceGuidance": "adcp.types.domains.pricing_options.price_guidance",
    "RevenueSharePricingOption": "adcp.types.domains.pricing_options.revenue_share_option",
    "TimeBasedPricingOption": "adcp.types.domains.pricing_options.time_option",
    "TimeUnit": "adcp.types.domains.pricing_options.time_option",
    "VcpmPricingOption": "adcp.types.domains.pricing_options.vcpm_option",
    "ViewThreshold": "adcp.types.domains.pricing_options.cpv_option",
    "ViewThreshold1": "adcp.types.domains.pricing_options.cpv_option",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
