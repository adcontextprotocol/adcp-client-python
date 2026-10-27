"""Types the AdCP ``error_details`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.error_details import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.error_details.<schema>``.
Nothing here is renamed.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-26 23:56:31 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.domains.error_details.accessibility_violation import (
    AccessibilityViolationDetails,
    FailureKind,
    FailureKind1,
    Violation,
)
from adcp.types.domains.error_details.account_moved import AccountMovedDetails
from adcp.types.domains.error_details.account_setup_required import AccountSetupRequiredDetails
from adcp.types.domains.error_details.action_not_allowed import ActionNotAllowedDetails
from adcp.types.domains.error_details.agent_permission_denied import AgentPermissionDeniedDetails
from adcp.types.domains.error_details.audience_too_small import AudienceTooSmallDetails
from adcp.types.domains.error_details.authorization_required import AuthorizationRequiredDetails
from adcp.types.domains.error_details.billing_not_permitted_for_agent import (
    BillingNotPermittedForAgentDetails,
)
from adcp.types.domains.error_details.billing_not_supported import BillingNotSupportedDetails
from adcp.types.domains.error_details.budget_too_low import BudgetTooLowDetails
from adcp.types.domains.error_details.conflict import ConflictDetails
from adcp.types.domains.error_details.creative_merge_rejected import (
    CreativeMergeRejectedDetails,
    Rejection,
)
from adcp.types.domains.error_details.creative_rejected import CreativeRejectedDetails
from adcp.types.domains.error_details.creative_representation_unresolved import (
    CreativeRepresentationUnresolvedDetails,
)
from adcp.types.domains.error_details.creative_revision_content_mismatch import (
    CreativeRevisionContentMismatchDetails,
)
from adcp.types.domains.error_details.execution_requirement_unmet import (
    ExecutionRequirementUnmetDetails,
    UnmetRequirement,
)
from adcp.types.domains.error_details.governance_agent_not_accepted import (
    GovernanceAgentNotAcceptedDetails,
    GovernanceAgentNotAcceptedDetails1,
    GovernanceAgentNotAcceptedDetails2,
)
from adcp.types.domains.error_details.macro_resolution_failed import MacroResolutionFailedDetails
from adcp.types.domains.error_details.policy_violation import Origin, PolicyViolationDetails
from adcp.types.domains.error_details.rate_limited import RateLimitedDetails
from adcp.types.domains.error_details.reference_definition_changed import (
    ReferenceDefinitionChangedDetails,
)
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
    UnsupportedRefinementDimensionDetails,
)
from adcp.types.domains.error_details.unsupported_targeting_dimension import (
    UnsupportedTargetingDimensionDetails,
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
    "CreativeMergeRejectedDetails",
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
    "Recovery",
    "ReferenceDefinitionChangedDetails",
    "Rejection",
    "RequoteRequiredDetails",
    "StaleResponseDetails",
    "SupportedMajor",
    "SupportedVersion",
    "UnmetRequirement",
    "UnsupportedRefinementDimensionDetails",
    "UnsupportedTargetingDimensionDetails",
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
