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

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 04:47:28 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.applicable_package_id import ApplicablePackageId
from adcp.types.generated_poc.core.brand_id import BrandId
from adcp.types.generated_poc.core.brand_key import BrandKey, Country
from adcp.types.generated_poc.core.canonical_account_ref import (
    CanonicalAccountReference,
    CanonicalAccountReference1,
    CanonicalAccountReference2,
)
from adcp.types.generated_poc.core.creative_revision_id import CreativeRevisionId
from adcp.types.generated_poc.core.downstream_connection_requirement import (
    ConnectionType,
    DownstreamConnectionRequirement,
    RequiredForItem as RequiredForItemFromDownstreamConnectionRequirement,
    ResourceRef,
    Scope as ScopeFromDownstreamConnectionRequirement,
    Status as StatusFromDownstreamConnectionRequirement,
)
from adcp.types.generated_poc.core.ext import ExtensionObject
from adcp.types.generated_poc.core.format_option_ref import (
    FormatOptionReference,
    FormatOptionReference1,
    FormatOptionReference2,
)
from adcp.types.generated_poc.core.macro_encoding import Kind, MacroEncoding
from adcp.types.generated_poc.core.macro_resolution_result import (
    MacroResolutionResult,
    Status as StatusFromMacroResolutionResult,
    UnavailableBehavior,
)
from adcp.types.generated_poc.core.media_buy_available_action import MediaBuyAvailableAction, Task
from adcp.types.generated_poc.core.media_buy_change_term_id import MediaBuyChangeTermId
from adcp.types.generated_poc.core.media_buy_legacy_terms_ref import MediaBuyTermsReference
from adcp.types.generated_poc.core.operator_unit import OperatorUnit
from adcp.types.generated_poc.core.product_execution_requirement import (
    Connection,
    ProductExecutionRequirement,
    ProductExecutionRequirement1,
    ProductExecutionRequirement2,
    ProductExecutionRequirement3,
    RequiredForItem as RequiredForItemFromProductExecutionRequirement,
)
from adcp.types.generated_poc.core.representation_rejection import Code, RepresentationRejection
from adcp.types.generated_poc.core.sla_window import SlaWindow
from adcp.types.generated_poc.enums.action_not_allowed_reason import ActionNotAllowedReason
from adcp.types.generated_poc.enums.billing_party import BillingParty
from adcp.types.generated_poc.enums.catalog_type import CatalogType
from adcp.types.generated_poc.enums.event_type import EventType
from adcp.types.generated_poc.enums.macro_dialect import MacroDialectFamily
from adcp.types.generated_poc.enums.macro_mapping_status import MacroMappingStatus
from adcp.types.generated_poc.enums.macro_processing_operation import MacroProcessingOperation
from adcp.types.generated_poc.enums.macro_resolution_reason import MacroResolutionReason
from adcp.types.generated_poc.enums.macro_resolver import MacroProcessingActor
from adcp.types.generated_poc.enums.media_buy_action_mode import MediaBuyActionMode
from adcp.types.generated_poc.enums.media_buy_valid_action import MediaBuyValidAction
from adcp.types.generated_poc.enums.seller_policy_decline_reason import SellerPolicyDeclineReason
from adcp.types.generated_poc.enums.universal_macro import UniversalMacro
from adcp.types.generated_poc.enums.vast_version import VastVersion
from adcp.types.generated_poc.error_details.accessibility_violation import (
    AccessibilityViolationDetails,
    FailureKind,
    FailureKind1,
    Violation,
)
from adcp.types.generated_poc.error_details.account_moved import AccountMovedDetails
from adcp.types.generated_poc.error_details.account_setup_required import (
    AccountSetupRequiredDetails,
)
from adcp.types.generated_poc.error_details.action_not_allowed import ActionNotAllowedDetails
from adcp.types.generated_poc.error_details.agent_permission_denied import (
    AgentPermissionDeniedDetails,
)
from adcp.types.generated_poc.error_details.audience_too_small import AudienceTooSmallDetails
from adcp.types.generated_poc.error_details.authorization_required import (
    AuthorizationRequiredDetails,
)
from adcp.types.generated_poc.error_details.billing_not_permitted_for_agent import (
    BillingNotPermittedForAgentDetails,
)
from adcp.types.generated_poc.error_details.billing_not_supported import (
    BillingNotSupportedDetails,
    Scope as ScopeFromBillingNotSupported,
)
from adcp.types.generated_poc.error_details.budget_too_low import BudgetTooLowDetails
from adcp.types.generated_poc.error_details.conflict import ConflictDetails
from adcp.types.generated_poc.error_details.creative_rejected import CreativeRejectedDetails
from adcp.types.generated_poc.error_details.creative_representation_unresolved import (
    CreativeRepresentationUnresolvedDetails,
)
from adcp.types.generated_poc.error_details.creative_revision_content_mismatch import (
    CreativeRevisionContentMismatchDetails,
)
from adcp.types.generated_poc.error_details.execution_requirement_unmet import (
    ExecutionRequirementUnmetDetails,
    Reason,
    UnmetRequirement,
)
from adcp.types.generated_poc.error_details.governance_agent_not_accepted import (
    GovernanceAgentNotAcceptedDetails,
    GovernanceAgentNotAcceptedDetails1,
    GovernanceAgentNotAcceptedDetails2,
)
from adcp.types.generated_poc.error_details.macro_resolution_failed import (
    MacroResolutionFailedDetails,
)
from adcp.types.generated_poc.error_details.policy_violation import Origin, PolicyViolationDetails
from adcp.types.generated_poc.error_details.rate_limited import (
    RateLimitedDetails,
    Scope as ScopeFromRateLimited,
)
from adcp.types.generated_poc.error_details.requote_required import (
    EnvelopeField,
    EnvelopeField1,
    EnvelopeField1Item,
    RequoteRequiredDetails,
)
from adcp.types.generated_poc.error_details.stale_response import (
    OriginalError,
    StaleResponseDetails,
    Upstream,
)
from adcp.types.generated_poc.error_details.unsupported_refinement_dimension import (
    SupportedDimension,
    UnsupportedRefinementDimensionDetails,
)
from adcp.types.generated_poc.error_details.vast_version_mismatch import (
    DocumentRole,
    MismatchReason,
    ObservedDocumentVastVersion,
    VastVersionMismatchDetails,
    VastVersionMismatchDetails1,
    VastVersionMismatchDetails2,
    VastVersionMismatchDetails3,
)
from adcp.types.generated_poc.error_details.vendor_error_codes import (
    Codes,
    Recovery,
    VendorErrorCodeRegistry,
    Vendors,
)
from adcp.types.generated_poc.error_details.version_unsupported import (
    SupportedMajor,
    SupportedVersion,
    VersionUnsupportedDetails,
)
from adcp.types.generated_poc.governance.accepted_governance_agents import (
    AcceptedGovernanceAgents,
    AnyOf,
    AnyOf1,
    AnyOf2,
    VerificationMode,
)

# Explicit exports
__all__ = [
    "AcceptedGovernanceAgents",
    "AccessibilityViolationDetails",
    "AccountMovedDetails",
    "AccountSetupRequiredDetails",
    "ActionNotAllowedDetails",
    "ActionNotAllowedReason",
    "AgentPermissionDeniedDetails",
    "AnyOf",
    "AnyOf1",
    "AnyOf2",
    "ApplicablePackageId",
    "AudienceTooSmallDetails",
    "AuthorizationRequiredDetails",
    "BillingNotPermittedForAgentDetails",
    "BillingNotSupportedDetails",
    "BillingParty",
    "BrandId",
    "BrandKey",
    "BudgetTooLowDetails",
    "CanonicalAccountReference",
    "CanonicalAccountReference1",
    "CanonicalAccountReference2",
    "CatalogType",
    "Code",
    "Codes",
    "ConflictDetails",
    "Connection",
    "ConnectionType",
    "Country",
    "CreativeRejectedDetails",
    "CreativeRepresentationUnresolvedDetails",
    "CreativeRevisionContentMismatchDetails",
    "CreativeRevisionId",
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
    "FormatOptionReference",
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
    "MediaBuyChangeTermId",
    "MediaBuyTermsReference",
    "MediaBuyValidAction",
    "MismatchReason",
    "ObservedDocumentVastVersion",
    "OperatorUnit",
    "Origin",
    "OriginalError",
    "PolicyViolationDetails",
    "ProductExecutionRequirement",
    "ProductExecutionRequirement1",
    "ProductExecutionRequirement2",
    "ProductExecutionRequirement3",
    "RateLimitedDetails",
    "Reason",
    "Recovery",
    "RepresentationRejection",
    "RequiredForItemFromDownstreamConnectionRequirement",
    "RequiredForItemFromProductExecutionRequirement",
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
    "SupportedDimension",
    "SupportedMajor",
    "SupportedVersion",
    "Task",
    "UnavailableBehavior",
    "UniversalMacro",
    "UnmetRequirement",
    "UnsupportedRefinementDimensionDetails",
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
