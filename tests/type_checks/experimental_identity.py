"""Draft response readers retain their field types under mypy and pyright."""

from __future__ import annotations

from typing import Literal

from typing_extensions import assert_type

from adcp.server.signed_requests import RequestSignatureVerification
from adcp.signing import (
    DiscoveredSignerKeys,
    ResolvedSignerKey,
    SignerKeyResolver,
    WbaSignerKeyResolver,
)
from adcp.types.domains.core.principal_changed_webhook import Reason
from adcp.types.domains.protocol.get_principal_response import PrincipalReadFailedResult
from adcp.types.experimental_identity import (
    AccountCapabilities,
    AgentOnboardingRequirement,
    AgentOnboardingRequirementKind,
    GetAdcpCapabilitiesResponse,
    GetPrincipalResponse,
    PrincipalChangedWebhook,
    PrincipalCurrentResult,
    PrincipalRecognizedResult,
    PrincipalRelationship,
    PrincipalRelationshipStatus,
    PrincipalUnconfiguredResult,
    RequestSigning,
)


def principal_relationship(response: GetPrincipalResponse) -> PrincipalRelationship | None:
    assert_type(
        response.result,
        PrincipalUnconfiguredResult
        | PrincipalRecognizedResult
        | PrincipalCurrentResult
        | PrincipalReadFailedResult,
    )
    if isinstance(response.result, PrincipalReadFailedResult):
        return None
    assert_type(response.result.relationship, PrincipalRelationship | None)
    return response.result.relationship


def relationship_pending(relationship: PrincipalRelationship) -> bool:
    assert_type(relationship.status, PrincipalRelationshipStatus)
    return relationship.status is PrincipalRelationshipStatus.pending_approval


def onboarding_message(response: GetAdcpCapabilitiesResponse) -> str | None:
    assert_type(response.account, AccountCapabilities | None)
    assert_type(response.request_signing, RequestSigning | None)
    if response.request_signing is not None:
        assert_type(response.request_signing.profiles, list[str] | None)
    if response.account is None or response.account.agent_onboarding is None:
        return None
    return response.account.agent_onboarding.message


def requirement_kind(requirement: AgentOnboardingRequirement) -> str:
    assert_type(requirement.kind, AgentOnboardingRequirementKind | str)
    return requirement.kind


def webhook_reason(event: PrincipalChangedWebhook) -> str:
    assert_type(event.reason, Reason | Literal["relationship_changed", "relationship_expiring"])
    return event.reason


class LegacyKeyResolver:
    """Existing adopter resolvers need only the original keyid argument."""

    async def __call__(self, keyid: str) -> ResolvedSignerKey | None:
        return None


def legacy_resolver_configuration() -> RequestSignatureVerification:
    legacy: SignerKeyResolver = LegacyKeyResolver()
    discovered = DiscoveredSignerKeys(legacy_resolver=legacy)
    origin_aware: WbaSignerKeyResolver = discovered
    assert isinstance(origin_aware, WbaSignerKeyResolver)
    return RequestSignatureVerification(signer_keys=legacy)
