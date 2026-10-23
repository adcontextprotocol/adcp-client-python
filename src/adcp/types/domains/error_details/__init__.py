"""Types the AdCP ``error_details`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.error_details import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.error_details.<schema>``.
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "error_details.accessibility_violation": (
        "AccessibilityViolationDetails",
        "FailureKind",
        "FailureKind1",
        "Violation",
    ),
    "error_details.account_moved": ("AccountMovedDetails",),
    "error_details.account_setup_required": ("AccountSetupRequiredDetails",),
    "error_details.action_not_allowed": ("ActionNotAllowedDetails",),
    "error_details.agent_permission_denied": ("AgentPermissionDeniedDetails",),
    "error_details.audience_too_small": ("AudienceTooSmallDetails",),
    "error_details.authorization_required": ("AuthorizationRequiredDetails",),
    "error_details.billing_not_permitted_for_agent": ("BillingNotPermittedForAgentDetails",),
    "error_details.billing_not_supported": ("BillingNotSupportedDetails",),
    "error_details.budget_too_low": ("BudgetTooLowDetails",),
    "error_details.conflict": ("ConflictDetails",),
    "error_details.creative_rejected": ("CreativeRejectedDetails",),
    "error_details.creative_representation_unresolved": (
        "CreativeRepresentationUnresolvedDetails",
    ),
    "error_details.creative_revision_content_mismatch": ("CreativeRevisionContentMismatchDetails",),
    "error_details.execution_requirement_unmet": (
        "ExecutionRequirementUnmetDetails",
        "Reason",
        "UnmetRequirement",
    ),
    "error_details.governance_agent_not_accepted": (
        "GovernanceAgentNotAcceptedDetails",
        "GovernanceAgentNotAcceptedDetails1",
        "GovernanceAgentNotAcceptedDetails2",
    ),
    "error_details.macro_resolution_failed": ("MacroResolutionFailedDetails",),
    "error_details.policy_violation": (
        "Origin",
        "PolicyViolationDetails",
    ),
    "error_details.rate_limited": ("RateLimitedDetails",),
    "error_details.requote_required": (
        "EnvelopeField",
        "EnvelopeField1",
        "EnvelopeField1Item",
        "RequoteRequiredDetails",
    ),
    "error_details.stale_response": (
        "OriginalError",
        "StaleResponseDetails",
        "Upstream",
    ),
    "error_details.unsupported_refinement_dimension": (
        "SupportedDimension",
        "UnsupportedRefinementDimensionDetails",
    ),
    "error_details.vast_version_mismatch": (
        "DocumentRole",
        "MismatchReason",
        "VastVersionMismatchDetails",
        "VastVersionMismatchDetails1",
        "VastVersionMismatchDetails2",
        "VastVersionMismatchDetails3",
    ),
    "error_details.vendor_error_codes": (
        "Codes",
        "Recovery",
        "VendorErrorCodeRegistry",
        "Vendors",
    ),
    "error_details.version_unsupported": (
        "SupportedMajor",
        "SupportedVersion",
        "VersionUnsupportedDetails",
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
