"""Types declared by the AdCP ``core/signal_definition_enrichment`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.signal_definition_enrichment import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.signal_definition_enrichment import (
    AiActRiskClass,
    Art9Basis,
    Channel,
    Country,
    DataSource,
    DataSubjectRights,
    MatchKey,
    Method,
    Methodology,
    Modeling,
    Onboarder,
    ParentMatchBehavior,
    PreOnboardingPrecisionLevel,
    RefreshCadence,
    Right,
    SeedSource,
    SignalDefinitionEnrichment,
    Taxonomy,
    TrainingDataJurisdiction,
    Type,
    Value,
    ValueMapping,
)

# Explicit exports
__all__ = [
    "AiActRiskClass",
    "Art9Basis",
    "Channel",
    "Country",
    "DataSource",
    "DataSubjectRights",
    "MatchKey",
    "Method",
    "Methodology",
    "Modeling",
    "Onboarder",
    "ParentMatchBehavior",
    "PreOnboardingPrecisionLevel",
    "RefreshCadence",
    "Right",
    "SeedSource",
    "SignalDefinitionEnrichment",
    "Taxonomy",
    "TrainingDataJurisdiction",
    "Type",
    "Value",
    "ValueMapping",
]
