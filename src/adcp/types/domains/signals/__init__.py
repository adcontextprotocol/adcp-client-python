"""Types the AdCP ``signals`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.signals import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.signals.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:49:37 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types.domains.signals.activate_signal_request import Action, ActivateSignalRequest
    from adcp.types.domains.signals.activate_signal_response import (
        ActivateSignalResponse,
        ActivateSignalResponse1,
        ActivateSignalResponse2,
    )
    from adcp.types.domains.signals.get_signals_async_response_submitted import GetSignalsSubmitted
    from adcp.types.domains.signals.get_signals_async_response_working import GetSignalsWorking
    from adcp.types.domains.signals.get_signals_request import (
        DiscoveryMode,
        Field1,
        GetSignalsRequest,
    )
    from adcp.types.domains.signals.get_signals_response import (
        AiActRiskClass,
        Art9Basis,
        CacheScope,
        Channel,
        DataSource,
        DataSubjectRights,
        GetSignalsResponse,
        IncompleteItem,
        MatchKey,
        Method,
        Methodology,
        Modeling,
        Onboarder,
        ParentMatchBehavior,
        PreOnboardingPrecisionLevel,
        Range,
        RefreshCadence,
        Right,
        Scope,
        SeedSource,
        Signal,
        Taxonomy,
        TrainingDataJurisdiction,
        Type,
        Value,
        ValueMapping,
    )

# Explicit exports
__all__ = [
    "Action",
    "ActivateSignalRequest",
    "ActivateSignalResponse",
    "ActivateSignalResponse1",
    "ActivateSignalResponse2",
    "AiActRiskClass",
    "Art9Basis",
    "CacheScope",
    "Channel",
    "DataSource",
    "DataSubjectRights",
    "DiscoveryMode",
    "Field1",
    "GetSignalsRequest",
    "GetSignalsResponse",
    "GetSignalsSubmitted",
    "GetSignalsWorking",
    "IncompleteItem",
    "MatchKey",
    "Method",
    "Methodology",
    "Modeling",
    "Onboarder",
    "ParentMatchBehavior",
    "PreOnboardingPrecisionLevel",
    "Range",
    "RefreshCadence",
    "Right",
    "Scope",
    "SeedSource",
    "Signal",
    "Taxonomy",
    "TrainingDataJurisdiction",
    "Type",
    "Value",
    "ValueMapping",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Action": "adcp.types.domains.signals.activate_signal_request",
    "ActivateSignalRequest": "adcp.types.domains.signals.activate_signal_request",
    "ActivateSignalResponse": "adcp.types.domains.signals.activate_signal_response",
    "ActivateSignalResponse1": "adcp.types.domains.signals.activate_signal_response",
    "ActivateSignalResponse2": "adcp.types.domains.signals.activate_signal_response",
    "AiActRiskClass": "adcp.types.domains.signals.get_signals_response",
    "Art9Basis": "adcp.types.domains.signals.get_signals_response",
    "CacheScope": "adcp.types.domains.signals.get_signals_response",
    "Channel": "adcp.types.domains.signals.get_signals_response",
    "DataSource": "adcp.types.domains.signals.get_signals_response",
    "DataSubjectRights": "adcp.types.domains.signals.get_signals_response",
    "DiscoveryMode": "adcp.types.domains.signals.get_signals_request",
    "Field1": "adcp.types.domains.signals.get_signals_request",
    "GetSignalsRequest": "adcp.types.domains.signals.get_signals_request",
    "GetSignalsResponse": "adcp.types.domains.signals.get_signals_response",
    "GetSignalsSubmitted": "adcp.types.domains.signals.get_signals_async_response_submitted",
    "GetSignalsWorking": "adcp.types.domains.signals.get_signals_async_response_working",
    "IncompleteItem": "adcp.types.domains.signals.get_signals_response",
    "MatchKey": "adcp.types.domains.signals.get_signals_response",
    "Method": "adcp.types.domains.signals.get_signals_response",
    "Methodology": "adcp.types.domains.signals.get_signals_response",
    "Modeling": "adcp.types.domains.signals.get_signals_response",
    "Onboarder": "adcp.types.domains.signals.get_signals_response",
    "ParentMatchBehavior": "adcp.types.domains.signals.get_signals_response",
    "PreOnboardingPrecisionLevel": "adcp.types.domains.signals.get_signals_response",
    "Range": "adcp.types.domains.signals.get_signals_response",
    "RefreshCadence": "adcp.types.domains.signals.get_signals_response",
    "Right": "adcp.types.domains.signals.get_signals_response",
    "Scope": "adcp.types.domains.signals.get_signals_response",
    "SeedSource": "adcp.types.domains.signals.get_signals_response",
    "Signal": "adcp.types.domains.signals.get_signals_response",
    "Taxonomy": "adcp.types.domains.signals.get_signals_response",
    "TrainingDataJurisdiction": "adcp.types.domains.signals.get_signals_response",
    "Type": "adcp.types.domains.signals.get_signals_response",
    "Value": "adcp.types.domains.signals.get_signals_response",
    "ValueMapping": "adcp.types.domains.signals.get_signals_response",
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
