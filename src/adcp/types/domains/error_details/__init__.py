"""Types the AdCP ``error_details`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.error_details import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.error_details.<schema>``.
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
    from adcp.types.domains.error_details.accessibility_violation import (
        AccessibilityViolationDetails,
        FailureKind,
        FailureKind1,
        Violation,
    )
    from adcp.types.domains.error_details.account_moved import AccountMovedDetails
    from adcp.types.domains.error_details.account_setup_required import AccountSetupRequiredDetails
    from adcp.types.domains.error_details.action_not_allowed import ActionNotAllowedDetails
    from adcp.types.domains.error_details.agent_permission_denied import (
        AgentPermissionDeniedDetails,
    )
    from adcp.types.domains.error_details.audience_too_small import AudienceTooSmallDetails
    from adcp.types.domains.error_details.authorization_required import AuthorizationRequiredDetails
    from adcp.types.domains.error_details.billing_not_permitted_for_agent import (
        BillingNotPermittedForAgentDetails,
    )
    from adcp.types.domains.error_details.billing_not_supported import BillingNotSupportedDetails
    from adcp.types.domains.error_details.budget_too_low import BudgetTooLowDetails
    from adcp.types.domains.error_details.conflict import ConflictDetails
    from adcp.types.domains.error_details.creative_rejected import CreativeRejectedDetails
    from adcp.types.domains.error_details.creative_representation_unresolved import (
        CreativeRepresentationUnresolvedDetails,
    )
    from adcp.types.domains.error_details.creative_revision_content_mismatch import (
        CreativeRevisionContentMismatchDetails,
    )
    from adcp.types.domains.error_details.execution_requirement_unmet import (
        ExecutionRequirementUnmetDetails,
        Reason,
        UnmetRequirement,
    )
    from adcp.types.domains.error_details.governance_agent_not_accepted import (
        GovernanceAgentNotAcceptedDetails,
        GovernanceAgentNotAcceptedDetails1,
        GovernanceAgentNotAcceptedDetails2,
    )
    from adcp.types.domains.error_details.macro_resolution_failed import (
        MacroResolutionFailedDetails,
    )
    from adcp.types.domains.error_details.policy_violation import Origin, PolicyViolationDetails
    from adcp.types.domains.error_details.rate_limited import RateLimitedDetails
    from adcp.types.domains.error_details.requote_required import (
        EnvelopeField,
        EnvelopeField1,
        EnvelopeField1Item,
        RequoteRequiredDetails,
    )
    from adcp.types.domains.error_details.stale_response import (
        OriginalError,
        StaleResponseDetails,
        Upstream,
    )
    from adcp.types.domains.error_details.unsupported_refinement_dimension import (
        SupportedDimension,
        UnsupportedRefinementDimensionDetails,
    )
    from adcp.types.domains.error_details.vast_version_mismatch import (
        DocumentRole,
        MismatchReason,
        VastVersionMismatchDetails,
        VastVersionMismatchDetails1,
        VastVersionMismatchDetails2,
        VastVersionMismatchDetails3,
    )
    from adcp.types.domains.error_details.vendor_error_codes import (
        Codes,
        Recovery,
        VendorErrorCodeRegistry,
        Vendors,
    )
    from adcp.types.domains.error_details.version_unsupported import (
        SupportedMajor,
        SupportedVersion,
        VersionUnsupportedDetails,
    )

# Explicit exports
__all__ = [
    "AccessibilityViolationDetails",
    "AccountMovedDetails",
    "AccountSetupRequiredDetails",
    "ActionNotAllowedDetails",
    "AgentPermissionDeniedDetails",
    "AudienceTooSmallDetails",
    "AuthorizationRequiredDetails",
    "BillingNotPermittedForAgentDetails",
    "BillingNotSupportedDetails",
    "BudgetTooLowDetails",
    "Codes",
    "ConflictDetails",
    "CreativeRejectedDetails",
    "CreativeRepresentationUnresolvedDetails",
    "CreativeRevisionContentMismatchDetails",
    "DocumentRole",
    "EnvelopeField",
    "EnvelopeField1",
    "EnvelopeField1Item",
    "ExecutionRequirementUnmetDetails",
    "FailureKind",
    "FailureKind1",
    "GovernanceAgentNotAcceptedDetails",
    "GovernanceAgentNotAcceptedDetails1",
    "GovernanceAgentNotAcceptedDetails2",
    "MacroResolutionFailedDetails",
    "MismatchReason",
    "Origin",
    "OriginalError",
    "PolicyViolationDetails",
    "RateLimitedDetails",
    "Reason",
    "Recovery",
    "RequoteRequiredDetails",
    "StaleResponseDetails",
    "SupportedDimension",
    "SupportedMajor",
    "SupportedVersion",
    "UnmetRequirement",
    "UnsupportedRefinementDimensionDetails",
    "Upstream",
    "VastVersionMismatchDetails",
    "VastVersionMismatchDetails1",
    "VastVersionMismatchDetails2",
    "VastVersionMismatchDetails3",
    "VendorErrorCodeRegistry",
    "Vendors",
    "VersionUnsupportedDetails",
    "Violation",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "AccessibilityViolationDetails": "adcp.types.domains.error_details.accessibility_violation",
    "AccountMovedDetails": "adcp.types.domains.error_details.account_moved",
    "AccountSetupRequiredDetails": "adcp.types.domains.error_details.account_setup_required",
    "ActionNotAllowedDetails": "adcp.types.domains.error_details.action_not_allowed",
    "AgentPermissionDeniedDetails": "adcp.types.domains.error_details.agent_permission_denied",
    "AudienceTooSmallDetails": "adcp.types.domains.error_details.audience_too_small",
    "AuthorizationRequiredDetails": "adcp.types.domains.error_details.authorization_required",
    "BillingNotPermittedForAgentDetails": "adcp.types.domains.error_details.billing_not_permitted_for_agent",
    "BillingNotSupportedDetails": "adcp.types.domains.error_details.billing_not_supported",
    "BudgetTooLowDetails": "adcp.types.domains.error_details.budget_too_low",
    "Codes": "adcp.types.domains.error_details.vendor_error_codes",
    "ConflictDetails": "adcp.types.domains.error_details.conflict",
    "CreativeRejectedDetails": "adcp.types.domains.error_details.creative_rejected",
    "CreativeRepresentationUnresolvedDetails": "adcp.types.domains.error_details.creative_representation_unresolved",
    "CreativeRevisionContentMismatchDetails": "adcp.types.domains.error_details.creative_revision_content_mismatch",
    "DocumentRole": "adcp.types.domains.error_details.vast_version_mismatch",
    "EnvelopeField": "adcp.types.domains.error_details.requote_required",
    "EnvelopeField1": "adcp.types.domains.error_details.requote_required",
    "EnvelopeField1Item": "adcp.types.domains.error_details.requote_required",
    "ExecutionRequirementUnmetDetails": "adcp.types.domains.error_details.execution_requirement_unmet",
    "FailureKind": "adcp.types.domains.error_details.accessibility_violation",
    "FailureKind1": "adcp.types.domains.error_details.accessibility_violation",
    "GovernanceAgentNotAcceptedDetails": "adcp.types.domains.error_details.governance_agent_not_accepted",
    "GovernanceAgentNotAcceptedDetails1": "adcp.types.domains.error_details.governance_agent_not_accepted",
    "GovernanceAgentNotAcceptedDetails2": "adcp.types.domains.error_details.governance_agent_not_accepted",
    "MacroResolutionFailedDetails": "adcp.types.domains.error_details.macro_resolution_failed",
    "MismatchReason": "adcp.types.domains.error_details.vast_version_mismatch",
    "Origin": "adcp.types.domains.error_details.policy_violation",
    "OriginalError": "adcp.types.domains.error_details.stale_response",
    "PolicyViolationDetails": "adcp.types.domains.error_details.policy_violation",
    "RateLimitedDetails": "adcp.types.domains.error_details.rate_limited",
    "Reason": "adcp.types.domains.error_details.execution_requirement_unmet",
    "Recovery": "adcp.types.domains.error_details.vendor_error_codes",
    "RequoteRequiredDetails": "adcp.types.domains.error_details.requote_required",
    "StaleResponseDetails": "adcp.types.domains.error_details.stale_response",
    "SupportedDimension": "adcp.types.domains.error_details.unsupported_refinement_dimension",
    "SupportedMajor": "adcp.types.domains.error_details.version_unsupported",
    "SupportedVersion": "adcp.types.domains.error_details.version_unsupported",
    "UnmetRequirement": "adcp.types.domains.error_details.execution_requirement_unmet",
    "UnsupportedRefinementDimensionDetails": "adcp.types.domains.error_details.unsupported_refinement_dimension",
    "Upstream": "adcp.types.domains.error_details.stale_response",
    "VastVersionMismatchDetails": "adcp.types.domains.error_details.vast_version_mismatch",
    "VastVersionMismatchDetails1": "adcp.types.domains.error_details.vast_version_mismatch",
    "VastVersionMismatchDetails2": "adcp.types.domains.error_details.vast_version_mismatch",
    "VastVersionMismatchDetails3": "adcp.types.domains.error_details.vast_version_mismatch",
    "VendorErrorCodeRegistry": "adcp.types.domains.error_details.vendor_error_codes",
    "Vendors": "adcp.types.domains.error_details.vendor_error_codes",
    "VersionUnsupportedDetails": "adcp.types.domains.error_details.version_unsupported",
    "Violation": "adcp.types.domains.error_details.accessibility_violation",
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
