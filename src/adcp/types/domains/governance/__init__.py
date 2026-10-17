"""Types the AdCP ``governance`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.governance import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.governance.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
    from adcp.types.domains.governance.accepted_governance_agents import (
        AcceptedGovernanceAgents,
        AnyOf,
        AnyOf1,
        AnyOf2,
        VerificationMode,
    )
    from adcp.types.domains.governance.attribute_definition import (
        AttributeDefinition,
        RegulatoryBasi,
    )
    from adcp.types.domains.governance.audience_constraints import AudienceConstraints
    from adcp.types.domains.governance.check_governance_request import (
        AudienceDistribution,
        AudienceDistribution1,
        Baseline,
        CheckGovernanceRequest,
        CheckGovernanceRequest1,
        CheckGovernanceRequest2,
        CheckGovernanceRequest3,
        DeliveryMetrics,
        DeliveryMetrics2,
        ExecutionCommitment,
        Pacing,
        ProposedCommitment,
        RuntimeAttestation1,
        Subject,
        Subject4,
        Subject6,
        Subject8,
        Subject9,
    )
    from adcp.types.domains.governance.check_governance_response import (
        ActionBinding,
        CheckGovernanceResponse,
        Condition,
        RuntimeAttestationEvaluation,
    )
    from adcp.types.domains.governance.get_plan_audit_logs_request import GetPlanAuditLogsRequest
    from adcp.types.domains.governance.get_plan_audit_logs_response import (
        ChannelAllocation,
        DeliveryReportingPeriod,
        DriftMetrics,
        Entry,
        Escalation,
        EscalationRateTrend,
        GetPlanAuditLogsResponse,
        GovernedAction,
        Statuses,
        Summary,
        Thresholds,
        Type,
    )
    from adcp.types.domains.governance.policy_category_definition import (
        Facet,
        PolicyCategoryDefinition,
        RegulatoryFramework,
    )
    from adcp.types.domains.governance.policy_entry import Exemplar, Exemplars, Issuer, PolicyEntry
    from adcp.types.domains.governance.policy_ref import PolicyReference
    from adcp.types.domains.governance.report_plan_adjustment_request import (
        Action,
        Decision,
        ReportPlanAdjustmentRequest,
    )
    from adcp.types.domains.governance.report_plan_adjustment_response import (
        ReportPlanAdjustmentResponse,
    )
    from adcp.types.domains.governance.report_plan_outcome_request import (
        Package,
        ReportPlanOutcomeRequest,
        SellerResponse,
    )
    from adcp.types.domains.governance.report_plan_outcome_response import (
        OutcomeState,
        ReportPlanOutcomeResponse,
    )
    from adcp.types.domains.governance.reported_outcome_error import (
        BoundedObject,
        BoundedScalar,
        BoundedScalar1,
        BoundedValue,
        BoundedValue1,
        BoundedValueLevel2,
        BoundedValueLevel21,
        BoundedValueLevel3,
        BoundedValueLevel31,
        ClassificationSource,
        Recovery,
        ReportedOutcomeError,
    )
    from adcp.types.domains.governance.sync_plans_request import (
        Allocations,
        Budget2,
        BudgetLimit,
        Channels,
        Delegation,
        Flight,
        MixTargets,
        Portfolio,
        SyncPlansRequest,
        TotalBudgetCap,
    )
    from adcp.types.domains.governance.sync_plans_response import (
        Category,
        ResolvedPolicy,
        Status51,
        SyncPlansResponse,
    )

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "governance.accepted_governance_agents": (
        "AcceptedGovernanceAgents",
        "AnyOf",
        "AnyOf1",
        "AnyOf2",
        "VerificationMode",
    ),
    "governance.attribute_definition": (
        "AttributeDefinition",
        "RegulatoryBasi",
    ),
    "governance.audience_constraints": ("AudienceConstraints",),
    "governance.check_governance_request": (
        "AudienceDistribution",
        "AudienceDistribution1",
        "Baseline",
        "CheckGovernanceRequest",
        "CheckGovernanceRequest1",
        "CheckGovernanceRequest2",
        "CheckGovernanceRequest3",
        "DeliveryMetrics",
        "DeliveryMetrics2",
        "ExecutionCommitment",
        "Pacing",
        "ProposedCommitment",
        "RuntimeAttestation1",
        "Subject",
        "Subject4",
        "Subject6",
        "Subject8",
        "Subject9",
    ),
    "governance.check_governance_response": (
        "ActionBinding",
        "CheckGovernanceResponse",
        "Condition",
        "RuntimeAttestationEvaluation",
    ),
    "governance.get_plan_audit_logs_request": ("GetPlanAuditLogsRequest",),
    "governance.get_plan_audit_logs_response": (
        "ChannelAllocation",
        "DeliveryReportingPeriod",
        "DriftMetrics",
        "Entry",
        "Escalation",
        "EscalationRateTrend",
        "GetPlanAuditLogsResponse",
        "GovernedAction",
        "Statuses",
        "Summary",
        "Thresholds",
        "Type",
    ),
    "governance.policy_category_definition": (
        "Facet",
        "PolicyCategoryDefinition",
        "RegulatoryFramework",
    ),
    "governance.policy_entry": (
        "Exemplar",
        "Exemplars",
        "Issuer",
        "PolicyEntry",
    ),
    "governance.policy_ref": ("PolicyReference",),
    "governance.report_plan_adjustment_request": (
        "Action",
        "Decision",
        "ReportPlanAdjustmentRequest",
    ),
    "governance.report_plan_adjustment_response": ("ReportPlanAdjustmentResponse",),
    "governance.report_plan_outcome_request": (
        "Package",
        "ReportPlanOutcomeRequest",
        "SellerResponse",
    ),
    "governance.report_plan_outcome_response": (
        "OutcomeState",
        "ReportPlanOutcomeResponse",
    ),
    "governance.reported_outcome_error": (
        "BoundedObject",
        "BoundedScalar",
        "BoundedScalar1",
        "BoundedValue",
        "BoundedValue1",
        "BoundedValueLevel2",
        "BoundedValueLevel21",
        "BoundedValueLevel3",
        "BoundedValueLevel31",
        "ClassificationSource",
        "Recovery",
        "ReportedOutcomeError",
    ),
    "governance.sync_plans_request": (
        "Allocations",
        "Budget2",
        "BudgetLimit",
        "Channels",
        "Delegation",
        "Flight",
        "MixTargets",
        "Portfolio",
        "SyncPlansRequest",
        "TotalBudgetCap",
    ),
    "governance.sync_plans_response": (
        "Category",
        "ResolvedPolicy",
        "Status51",
        "SyncPlansResponse",
    ),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


# Explicit exports
__all__ = [
    "AcceptedGovernanceAgents",
    "Action",
    "ActionBinding",
    "Allocations",
    "AnyOf",
    "AnyOf1",
    "AnyOf2",
    "AttributeDefinition",
    "AudienceConstraints",
    "AudienceDistribution",
    "AudienceDistribution1",
    "Baseline",
    "BoundedObject",
    "BoundedScalar",
    "BoundedScalar1",
    "BoundedValue",
    "BoundedValue1",
    "BoundedValueLevel2",
    "BoundedValueLevel21",
    "BoundedValueLevel3",
    "BoundedValueLevel31",
    "Budget2",
    "BudgetLimit",
    "Category",
    "ChannelAllocation",
    "Channels",
    "CheckGovernanceRequest",
    "CheckGovernanceRequest1",
    "CheckGovernanceRequest2",
    "CheckGovernanceRequest3",
    "CheckGovernanceResponse",
    "ClassificationSource",
    "Condition",
    "Decision",
    "Delegation",
    "DeliveryMetrics",
    "DeliveryMetrics2",
    "DeliveryReportingPeriod",
    "DriftMetrics",
    "Entry",
    "Escalation",
    "EscalationRateTrend",
    "ExecutionCommitment",
    "Exemplar",
    "Exemplars",
    "Facet",
    "Flight",
    "GetPlanAuditLogsRequest",
    "GetPlanAuditLogsResponse",
    "GovernedAction",
    "Issuer",
    "MixTargets",
    "OutcomeState",
    "Pacing",
    "Package",
    "PolicyCategoryDefinition",
    "PolicyEntry",
    "PolicyReference",
    "Portfolio",
    "ProposedCommitment",
    "Recovery",
    "RegulatoryBasi",
    "RegulatoryFramework",
    "ReportPlanAdjustmentRequest",
    "ReportPlanAdjustmentResponse",
    "ReportPlanOutcomeRequest",
    "ReportPlanOutcomeResponse",
    "ReportedOutcomeError",
    "ResolvedPolicy",
    "RuntimeAttestation1",
    "RuntimeAttestationEvaluation",
    "SellerResponse",
    "Status51",
    "Statuses",
    "Subject",
    "Subject4",
    "Subject6",
    "Subject8",
    "Subject9",
    "Summary",
    "SyncPlansRequest",
    "SyncPlansResponse",
    "Thresholds",
    "TotalBudgetCap",
    "Type",
    "VerificationMode",
]
