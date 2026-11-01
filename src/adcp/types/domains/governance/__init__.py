"""Types the AdCP ``governance`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.governance import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.governance.<schema>``.
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
        Period,
        Portfolio,
        SyncPlansRequest,
        TotalBudgetCap,
    )
    from adcp.types.domains.governance.sync_plans_response import (
        Category,
        ResolvedPolicy,
        Status97,
        SyncPlansResponse,
    )

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
    "Period",
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
    "Status97",
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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "AcceptedGovernanceAgents": "adcp.types.domains.governance.accepted_governance_agents",
    "Action": "adcp.types.domains.governance.report_plan_adjustment_request",
    "ActionBinding": "adcp.types.domains.governance.check_governance_response",
    "Allocations": "adcp.types.domains.governance.sync_plans_request",
    "AnyOf": "adcp.types.domains.governance.accepted_governance_agents",
    "AnyOf1": "adcp.types.domains.governance.accepted_governance_agents",
    "AnyOf2": "adcp.types.domains.governance.accepted_governance_agents",
    "AttributeDefinition": "adcp.types.domains.governance.attribute_definition",
    "AudienceConstraints": "adcp.types.domains.governance.audience_constraints",
    "AudienceDistribution": "adcp.types.domains.governance.check_governance_request",
    "AudienceDistribution1": "adcp.types.domains.governance.check_governance_request",
    "Baseline": "adcp.types.domains.governance.check_governance_request",
    "BoundedObject": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedScalar": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedScalar1": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValue": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValue1": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValueLevel2": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValueLevel21": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValueLevel3": "adcp.types.domains.governance.reported_outcome_error",
    "BoundedValueLevel31": "adcp.types.domains.governance.reported_outcome_error",
    "Budget2": "adcp.types.domains.governance.sync_plans_request",
    "BudgetLimit": "adcp.types.domains.governance.sync_plans_request",
    "Category": "adcp.types.domains.governance.sync_plans_response",
    "ChannelAllocation": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "Channels": "adcp.types.domains.governance.sync_plans_request",
    "CheckGovernanceRequest": "adcp.types.domains.governance.check_governance_request",
    "CheckGovernanceRequest1": "adcp.types.domains.governance.check_governance_request",
    "CheckGovernanceRequest2": "adcp.types.domains.governance.check_governance_request",
    "CheckGovernanceRequest3": "adcp.types.domains.governance.check_governance_request",
    "CheckGovernanceResponse": "adcp.types.domains.governance.check_governance_response",
    "ClassificationSource": "adcp.types.domains.governance.reported_outcome_error",
    "Condition": "adcp.types.domains.governance.check_governance_response",
    "Decision": "adcp.types.domains.governance.report_plan_adjustment_request",
    "Delegation": "adcp.types.domains.governance.sync_plans_request",
    "DeliveryMetrics": "adcp.types.domains.governance.check_governance_request",
    "DeliveryMetrics2": "adcp.types.domains.governance.check_governance_request",
    "DeliveryReportingPeriod": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "DriftMetrics": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "Entry": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "Escalation": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "EscalationRateTrend": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "ExecutionCommitment": "adcp.types.domains.governance.check_governance_request",
    "Exemplar": "adcp.types.domains.governance.policy_entry",
    "Exemplars": "adcp.types.domains.governance.policy_entry",
    "Facet": "adcp.types.domains.governance.policy_category_definition",
    "Flight": "adcp.types.domains.governance.sync_plans_request",
    "GetPlanAuditLogsRequest": "adcp.types.domains.governance.get_plan_audit_logs_request",
    "GetPlanAuditLogsResponse": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "GovernedAction": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "Issuer": "adcp.types.domains.governance.policy_entry",
    "MixTargets": "adcp.types.domains.governance.sync_plans_request",
    "OutcomeState": "adcp.types.domains.governance.report_plan_outcome_response",
    "Pacing": "adcp.types.domains.governance.check_governance_request",
    "Package": "adcp.types.domains.governance.report_plan_outcome_request",
    "Period": "adcp.types.domains.governance.sync_plans_request",
    "PolicyCategoryDefinition": "adcp.types.domains.governance.policy_category_definition",
    "PolicyEntry": "adcp.types.domains.governance.policy_entry",
    "PolicyReference": "adcp.types.domains.governance.policy_ref",
    "Portfolio": "adcp.types.domains.governance.sync_plans_request",
    "ProposedCommitment": "adcp.types.domains.governance.check_governance_request",
    "Recovery": "adcp.types.domains.governance.reported_outcome_error",
    "RegulatoryBasi": "adcp.types.domains.governance.attribute_definition",
    "RegulatoryFramework": "adcp.types.domains.governance.policy_category_definition",
    "ReportPlanAdjustmentRequest": "adcp.types.domains.governance.report_plan_adjustment_request",
    "ReportPlanAdjustmentResponse": "adcp.types.domains.governance.report_plan_adjustment_response",
    "ReportPlanOutcomeRequest": "adcp.types.domains.governance.report_plan_outcome_request",
    "ReportPlanOutcomeResponse": "adcp.types.domains.governance.report_plan_outcome_response",
    "ReportedOutcomeError": "adcp.types.domains.governance.reported_outcome_error",
    "ResolvedPolicy": "adcp.types.domains.governance.sync_plans_response",
    "RuntimeAttestation1": "adcp.types.domains.governance.check_governance_request",
    "RuntimeAttestationEvaluation": "adcp.types.domains.governance.check_governance_response",
    "SellerResponse": "adcp.types.domains.governance.report_plan_outcome_request",
    "Status97": "adcp.types.domains.governance.sync_plans_response",
    "Statuses": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "Subject": "adcp.types.domains.governance.check_governance_request",
    "Subject4": "adcp.types.domains.governance.check_governance_request",
    "Subject6": "adcp.types.domains.governance.check_governance_request",
    "Subject8": "adcp.types.domains.governance.check_governance_request",
    "Subject9": "adcp.types.domains.governance.check_governance_request",
    "Summary": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "SyncPlansRequest": "adcp.types.domains.governance.sync_plans_request",
    "SyncPlansResponse": "adcp.types.domains.governance.sync_plans_response",
    "Thresholds": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "TotalBudgetCap": "adcp.types.domains.governance.sync_plans_request",
    "Type": "adcp.types.domains.governance.get_plan_audit_logs_response",
    "VerificationMode": "adcp.types.domains.governance.accepted_governance_agents",
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
