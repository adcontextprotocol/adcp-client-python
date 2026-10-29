"""The AdCP structured error-details models, with their field types.

``Error.details`` is typed ``dict``, so nothing constrains what a raise site
puts in it. One model per ``error-details/*.json`` schema encodes the required
keys, and this module exports every one of them together with the transitive
closure of their field types — the enums and nested models their annotations
reference — so a seller constructs the payload with typed values:

    from adcp.types.error_details import SupportedVersion, VersionUnsupportedDetails

    VersionUnsupportedDetails(
        adcp_version="3.2",
        supported_versions=[SupportedVersion("3.1"), SupportedVersion("3.2")],
    )

A nested name that two error-details schemas both define carries the
defining file in its name (``ScopeFromRateLimited``) instead of a bare one.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-29 01:57:27 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.domains.core.account_candidate import AccountCandidate
from adcp.types.domains.core.brand_key import BrandKey
from adcp.types.domains.core.canonical_account_ref import (
    CanonicalAccountReference1,
    CanonicalAccountReference2,
)
from adcp.types.domains.core.creative_review_sla import CreativeReviewSla
from adcp.types.domains.core.downstream_connection_requirement import (
    ConnectionType,
    DownstreamConnectionRequirement,
    ResourceRef,
    Scope as ScopeFromDownstreamConnectionRequirement,
    Status as StatusFromDownstreamConnectionRequirement,
)
from adcp.types.domains.core.ext import ExtensionObject
from adcp.types.domains.core.format_option_ref import FormatOptionReference1, FormatOptionReference2
from adcp.types.domains.core.macro_encoding import Kind, MacroEncoding
from adcp.types.domains.core.macro_resolution_result import (
    MacroResolutionResult,
    Status as StatusFromMacroResolutionResult,
    UnavailableBehavior,
)
from adcp.types.domains.core.media_buy_available_action import MediaBuyAvailableAction, Task
from adcp.types.domains.core.operator_unit import OperatorUnit
from adcp.types.domains.core.product_execution_requirement import (
    Connection,
    ProductExecutionRequirement1,
    ProductExecutionRequirement2,
    ProductExecutionRequirement3,
)
from adcp.types.domains.core.representation_rejection import Code, RepresentationRejection
from adcp.types.domains.core.sla_window import Clock, SlaWindow
from adcp.types.domains.enums.action_not_allowed_reason import ActionNotAllowedReason
from adcp.types.domains.enums.adjustment_kind import PriceAdjustmentKind
from adcp.types.domains.enums.billing_party import BillingParty
from adcp.types.domains.enums.catalog_type import CatalogType
from adcp.types.domains.enums.creative_merge_rejection_reason import CreativeMergeRejectionReason
from adcp.types.domains.enums.definition_pin_ref_kind import DefinitionPinReferenceKind
from adcp.types.domains.enums.event_type import EventType
from adcp.types.domains.enums.macro_dialect import MacroDialectFamily
from adcp.types.domains.enums.macro_mapping_status import MacroMappingStatus
from adcp.types.domains.enums.macro_processing_operation import MacroProcessingOperation
from adcp.types.domains.enums.macro_resolution_reason import MacroResolutionReason
from adcp.types.domains.enums.macro_resolver import MacroProcessingActor
from adcp.types.domains.enums.media_buy_action_mode import MediaBuyActionMode
from adcp.types.domains.enums.media_buy_valid_action import MediaBuyValidAction
from adcp.types.domains.enums.seller_policy_decline_reason import SellerPolicyDeclineReason
from adcp.types.domains.enums.universal_macro import UniversalMacro, UniversalMacro3
from adcp.types.domains.enums.vast_version import VastVersion
from adcp.types.domains.error_details.accessibility_violation import (
    AccessibilityViolationDetails,
    FailureKind,
    FailureKind1,
    Violation,
)
from adcp.types.domains.error_details.account_ambiguous import AccountAmbiguousDetails
from adcp.types.domains.error_details.account_moved import AccountMovedDetails
from adcp.types.domains.error_details.account_setup_required import AccountSetupRequiredDetails
from adcp.types.domains.error_details.action_not_allowed import ActionNotAllowedDetails
from adcp.types.domains.error_details.agent_permission_denied import (
    AgentPermissionDeniedDetails,
    Reason as ReasonFromAgentPermissionDenied,
)
from adcp.types.domains.error_details.audience_too_small import AudienceTooSmallDetails
from adcp.types.domains.error_details.authorization_required import AuthorizationRequiredDetails
from adcp.types.domains.error_details.billing_not_permitted_for_agent import (
    BillingNotPermittedForAgentDetails,
)
from adcp.types.domains.error_details.billing_not_supported import (
    BillingNotSupportedDetails,
    Scope as ScopeFromBillingNotSupported,
)
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
    Reason as ReasonFromExecutionRequirementUnmet,
    UnmetRequirement,
)
from adcp.types.domains.error_details.governance_agent_not_accepted import (
    GovernanceAgentNotAcceptedDetails,
    GovernanceAgentNotAcceptedDetails1,
    GovernanceAgentNotAcceptedDetails2,
)
from adcp.types.domains.error_details.macro_resolution_failed import MacroResolutionFailedDetails
from adcp.types.domains.error_details.policy_violation import Origin, PolicyViolationDetails
from adcp.types.domains.error_details.price_adjustments_mismatch import (
    PriceAdjustmentsMismatchDetails,
)
from adcp.types.domains.error_details.rate_limited import (
    RateLimitedDetails,
    Scope as ScopeFromRateLimited,
)
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
    SupportedDimension as SupportedDimensionFromUnsupportedRefinementDimension,
    UnsupportedRefinementDimensionDetails,
)
from adcp.types.domains.error_details.unsupported_targeting_dimension import (
    SupportedDimension as SupportedDimensionFromUnsupportedTargetingDimension,
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
from adcp.types.domains.governance.accepted_governance_agents import (
    AcceptedGovernanceAgents,
    AnyOf,
    AnyOf1,
    AnyOf2,
    VerificationMode,
)
from adcp.types.domains.pricing_options.price_breakdown import Adjustment, PriceBreakdown

# Explicit exports
__all__ = [
    "AcceptedGovernanceAgents",
    "AccessibilityViolationDetails",
    "AccountAmbiguousDetails",
    "AccountCandidate",
    "AccountMovedDetails",
    "AccountSetupRequiredDetails",
    "ActionNotAllowedDetails",
    "ActionNotAllowedReason",
    "Adjustment",
    "AgentPermissionDeniedDetails",
    "AnyOf",
    "AnyOf1",
    "AnyOf2",
    "AudienceTooSmallDetails",
    "AuthorizationRequiredDetails",
    "BillingNotPermittedForAgentDetails",
    "BillingNotSupportedDetails",
    "BillingParty",
    "BrandKey",
    "BudgetTooLowDetails",
    "CanonicalAccountReference1",
    "CanonicalAccountReference2",
    "CatalogType",
    "Clock",
    "Code",
    "Codes",
    "ConflictDetails",
    "Connection",
    "ConnectionType",
    "CreativeMergeRejectedDetails",
    "CreativeMergeRejectionReason",
    "CreativeRejectedDetails",
    "CreativeRepresentationUnresolvedDetails",
    "CreativeReviewSla",
    "CreativeRevisionContentMismatchDetails",
    "DefinitionPinReferenceKind",
    "DocumentRole",
    "DownstreamConnectionRequirement",
    "EnvelopeField",
    "EnvelopeField1",
    "EnvelopeField1Item",
    "EventType",
    "ExecutionRequirementUnmetDetails",
    "ExtensionObject",
    "FailureKind",
    "FailureKind1",
    "FormatOptionReference1",
    "FormatOptionReference2",
    "GovernanceAgentNotAcceptedDetails",
    "GovernanceAgentNotAcceptedDetails1",
    "GovernanceAgentNotAcceptedDetails2",
    "Kind",
    "MacroDialectFamily",
    "MacroEncoding",
    "MacroMappingStatus",
    "MacroProcessingActor",
    "MacroProcessingOperation",
    "MacroResolutionFailedDetails",
    "MacroResolutionReason",
    "MacroResolutionResult",
    "MediaBuyActionMode",
    "MediaBuyAvailableAction",
    "MediaBuyValidAction",
    "MismatchReason",
    "OperatorUnit",
    "Origin",
    "OriginalError",
    "PolicyViolationDetails",
    "PriceAdjustmentKind",
    "PriceAdjustmentsMismatchDetails",
    "PriceBreakdown",
    "ProductExecutionRequirement1",
    "ProductExecutionRequirement2",
    "ProductExecutionRequirement3",
    "RateLimitedDetails",
    "ReasonFromAgentPermissionDenied",
    "ReasonFromExecutionRequirementUnmet",
    "Recovery",
    "ReferenceDefinitionChangedDetails",
    "Rejection",
    "RepresentationRejection",
    "RequoteRequiredDetails",
    "ResourceRef",
    "ScopeFromBillingNotSupported",
    "ScopeFromDownstreamConnectionRequirement",
    "ScopeFromRateLimited",
    "SellerPolicyDeclineReason",
    "SlaWindow",
    "StaleResponseDetails",
    "StatusFromDownstreamConnectionRequirement",
    "StatusFromMacroResolutionResult",
    "SupportedDimensionFromUnsupportedRefinementDimension",
    "SupportedDimensionFromUnsupportedTargetingDimension",
    "SupportedMajor",
    "SupportedVersion",
    "Task",
    "UnavailableBehavior",
    "UniversalMacro",
    "UniversalMacro3",
    "UnmetRequirement",
    "UnsupportedRefinementDimensionDetails",
    "UnsupportedTargetingDimensionDetails",
    "Upstream",
    "VastVersion",
    "VastVersionMismatchDetails",
    "VastVersionMismatchDetails1",
    "VastVersionMismatchDetails2",
    "VastVersionMismatchDetails3",
    "VendorErrorCodeRegistry",
    "Vendors",
    "VerificationMode",
    "VersionUnsupportedDetails",
    "Violation",
]
