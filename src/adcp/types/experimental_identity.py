"""Opt-in types for pinned AdCP 3.3 identity drafts, independent of the wire pin.

The schemas are pinned to open spec branches (see
adcp/signing/_identity_schemas/UPSTREAM.json). Seller and buyer onboarding
postures share one mode enum, AgentOnboardingMode (open, application,
invite_only). The buyer posture is published only in trust.json, at
profiles.adcp.onboarding on the record or on an agent entry. relationship on
get_principal is 3.3-only; its status is PrincipalRelationshipStatus.

Regenerate the private models with scripts/generate_identity_preview.py.
Validate trust records with adcp.signing.validate_trust_record: origin ownership
and one-agent-per-origin are behavioral constraints, beyond generated types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from adcp.types._identity_preview import (
    Agent as TrustAgent,
)
from adcp.types._identity_preview import (
    CoreAgentOnboarding as AgentOnboarding,
)
from adcp.types._identity_preview import (
    CoreAgentOnboardingRequirement as AgentOnboardingRequirement,
)
from adcp.types._identity_preview import (
    CoreAgentOnboardingTier as AgentOnboardingTier,
)
from adcp.types._identity_preview import (
    CoreBuyerOnboardingPosture as BuyerOnboardingPosture,
)
from adcp.types._identity_preview import (
    CorePrincipalRelationship as PrincipalRelationship,
)
from adcp.types._identity_preview import (
    EnumsAgentOnboardingMode as AgentOnboardingMode,
)
from adcp.types._identity_preview import (
    EnumsAgentOnboardingRequirementKind as AgentOnboardingRequirementKind,
)
from adcp.types._identity_preview import (
    EnumsPrincipalRelationshipStatus as PrincipalRelationshipStatus,
)
from adcp.types._identity_preview import (
    Grant as TrustGrant,
)
from adcp.types._identity_preview import (
    TrustV1Trust as TrustRecord,
)
from adcp.types.domains.core.principal_changed_webhook import (
    PrincipalChangedWebhook as _PrincipalChangedWebhook,
)
from adcp.types.domains.core.principal_changed_webhook import (
    Reason as _PrincipalChangedReason,
)
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    Account as _Account,
)
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    GetAdcpCapabilitiesResponse as _GetAdcpCapabilitiesResponse,
)
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    RequestSigning as _RequestSigning,
)
from adcp.types.domains.protocol.get_principal_response import (
    GetPrincipalResponse as _GetPrincipalResponse,
)
from adcp.types.domains.protocol.get_principal_response import (
    PrincipalCurrentResult as _PrincipalCurrentResult,
)
from adcp.types.domains.protocol.get_principal_response import (
    PrincipalReadFailedResult,
)
from adcp.types.domains.protocol.get_principal_response import (
    PrincipalRecognizedResult as _PrincipalRecognizedResult,
)
from adcp.types.domains.protocol.get_principal_response import (
    PrincipalUnconfiguredResult as _PrincipalUnconfiguredResult,
)


class AccountCapabilities(_Account):
    agent_onboarding: AgentOnboarding | None = None


class RequestSigning(_RequestSigning):
    """Draft accepted transport profiles; omission means the legacy profile only."""

    profiles: list[str] | None = None


class GetAdcpCapabilitiesResponse(_GetAdcpCapabilitiesResponse):
    # Pyright treats mutable Pydantic fields as invariant even for subclasses.
    account: AccountCapabilities | None = (  # pyright: ignore[reportIncompatibleVariableOverride]
        None
    )
    request_signing: (  # pyright: ignore[reportIncompatibleVariableOverride]
        RequestSigning | None
    ) = None


class PrincipalUnconfiguredResult(_PrincipalUnconfiguredResult):
    relationship: PrincipalRelationship | None = None


class PrincipalRecognizedResult(_PrincipalRecognizedResult):
    relationship: PrincipalRelationship | None = None


class PrincipalCurrentResult(_PrincipalCurrentResult):
    relationship: PrincipalRelationship | None = None


class GetPrincipalResponse(_GetPrincipalResponse):
    result: (  # pyright: ignore[reportIncompatibleVariableOverride]
        PrincipalUnconfiguredResult
        | PrincipalRecognizedResult
        | PrincipalCurrentResult
        | PrincipalReadFailedResult
    )


class PrincipalChangedWebhook(_PrincipalChangedWebhook):
    # Draft webhook reasons intentionally expand the stable response enum.
    reason: _PrincipalChangedReason | Literal["relationship_changed", "relationship_expiring"]  # type: ignore[assignment]


class GrantAbsentDetails(BaseModel):
    scope: Literal["agent"] = "agent"
    reason: Literal["grant_absent"] = "grant_absent"


__all__ = [
    "AccountCapabilities",
    "AgentOnboarding",
    "AgentOnboardingMode",
    "AgentOnboardingRequirement",
    "AgentOnboardingRequirementKind",
    "AgentOnboardingTier",
    "BuyerOnboardingPosture",
    "GetAdcpCapabilitiesResponse",
    "GetPrincipalResponse",
    "GrantAbsentDetails",
    "PrincipalChangedWebhook",
    "PrincipalCurrentResult",
    "PrincipalRecognizedResult",
    "PrincipalRelationship",
    "PrincipalRelationshipStatus",
    "PrincipalUnconfiguredResult",
    "RequestSigning",
    "TrustAgent",
    "TrustGrant",
    "TrustRecord",
]
