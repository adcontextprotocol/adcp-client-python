"""Opt-in names for the experimental AdCP 3.3 agent-identity surface.

Every model here is the generated 3.3 type under the name this module has
exported since the identity preview (#1502). The preview vendored the identity
schemas from adcp main while the wire pin was 3.2; the 3.3.0-beta.0 bundle
declares the same schemas, so the private preview tree and its generator are
retired and nothing is composed locally any more.

Seller and buyer onboarding postures share one mode enum, AgentOnboardingMode
(open, application, invite_only). The buyer posture is published only in
trust.json, at profiles.adcp.onboarding on the record or on an agent entry.
relationship on get_principal is 3.3-only; its status is
PrincipalRelationshipStatus.

Validate trust records with adcp.signing.validate_trust_record: origin
ownership and one-agent-per-origin are behavioral constraints, beyond the
generated types.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel

from adcp.types.base import inherit_field
from adcp.types.domains.core.agent_onboarding import (
    AgentOnboardingPosture as _AgentOnboardingPosture,
)
from adcp.types.domains.core.agent_onboarding_requirement import (
    AgentOnboardingRequirement as _AgentOnboardingRequirement,
)
from adcp.types.domains.core.agent_onboarding_tier import (
    AgentOnboardingTier as _AgentOnboardingTier,
)
from adcp.types.domains.core.buyer_onboarding_posture import (
    BuyerOnboardingPosture as _BuyerOnboardingPosture,
)
from adcp.types.domains.core.principal_changed_webhook import PrincipalChangedWebhook
from adcp.types.domains.core.principal_relationship import PrincipalRelationship
from adcp.types.domains.enums.agent_onboarding_mode import AgentOnboardingMode
from adcp.types.domains.enums.agent_onboarding_requirement_kind import (
    AgentOnboardingRequirementKind,
)
from adcp.types.domains.enums.billing_party import BillingParty
from adcp.types.domains.enums.principal_relationship_status import PrincipalRelationshipStatus
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    Account as _Account,
)
from adcp.types.domains.protocol.get_adcp_capabilities_response import (
    GetAdcpCapabilitiesResponse as _GetAdcpCapabilitiesResponse,
)
from adcp.types.domains.protocol.get_adcp_capabilities_response import RequestSigning
from adcp.types.domains.protocol.get_principal_response import (
    GetPrincipalResponse,
    PrincipalCurrentResult,
    PrincipalReadFailedResult,
    PrincipalRecognizedResult,
    PrincipalUnconfiguredResult,
)
from adcp.types.domains.trust.v1.trust import Agent as TrustAgent
from adcp.types.domains.trust.v1.trust import Grant as TrustGrant
from adcp.types.domains.trust.v1.trust import TrustJson

# ``enums/agent-onboarding-requirement-kind.json`` is ``x-extensible``: a
# seller may advertise a requirement kind this SDK does not know, and a reader
# keeps such advisory requirements rather than refusing the whole posture
# (``adcp.signing.trust``). The generated enum is closed, so the capability
# readers below redeclare ``kind`` open, as the identity preview did, and each
# redeclaration inherits its generated field (#1620).
_OpenRequirementKind = Annotated[
    AgentOnboardingRequirementKind | str, Field(union_mode="left_to_right")
]
_requirement_kind = inherit_field(_AgentOnboardingRequirement, "kind", required=True)
_tier_requirements = inherit_field(_AgentOnboardingTier, "requirements")
_buyer_requirements = inherit_field(_BuyerOnboardingPosture, "requirements")
_posture_sandbox = inherit_field(_AgentOnboardingPosture, "sandbox")
_posture_billing = inherit_field(_AgentOnboardingPosture, "billing")
_account_agent_onboarding = inherit_field(_Account, "agent_onboarding")
_capabilities_account = inherit_field(_GetAdcpCapabilitiesResponse, "account")


class AgentOnboardingRequirement(_AgentOnboardingRequirement):
    """An onboarding requirement whose ``kind`` keeps an unknown extensible value."""

    kind: _OpenRequirementKind = _requirement_kind  # type: ignore[assignment]


class AgentOnboardingTier(_AgentOnboardingTier):
    requirements: list[AgentOnboardingRequirement] | None = _tier_requirements  # type: ignore[assignment]


class BuyerOnboardingPosture(_BuyerOnboardingPosture):
    requirements: list[AgentOnboardingRequirement] | None = _buyer_requirements  # type: ignore[assignment]


class AgentOnboarding(_AgentOnboardingPosture):
    sandbox: AgentOnboardingTier | None = (
        _posture_sandbox  # pyright: ignore[reportIncompatibleVariableOverride]
    )
    billing: dict[BillingParty, AgentOnboardingTier] | None = _posture_billing  # type: ignore[assignment]


class AccountCapabilities(_Account):
    agent_onboarding: AgentOnboarding | None = (
        _account_agent_onboarding  # pyright: ignore[reportIncompatibleVariableOverride]
    )


class GetAdcpCapabilitiesResponse(_GetAdcpCapabilitiesResponse):
    account: AccountCapabilities | None = (
        _capabilities_account  # pyright: ignore[reportIncompatibleVariableOverride]
    )


class TrustRecord(RootModel[TrustJson]):
    """A parsed trust.json: a hosted pointer or an organization's record."""


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
    "PrincipalReadFailedResult",
    "PrincipalRecognizedResult",
    "PrincipalRelationship",
    "PrincipalRelationshipStatus",
    "PrincipalUnconfiguredResult",
    "RequestSigning",
    "TrustAgent",
    "TrustGrant",
    "TrustRecord",
]
