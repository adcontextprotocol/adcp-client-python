"""Regression tests for the AdCP 3.3 identity schemas and their generated types."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from adcp.server.base import ToolContext
from adcp.server.principal import (
    InMemoryPrincipalRecordStore,
    PrincipalIdentity,
    PrincipalService,
)
from adcp.signing import (
    TrustRecordError,
    TrustRecordEvaluator,
    TrustResponse,
    validate_trust_record,
)
from adcp.types.experimental_identity import (
    AgentOnboarding,
    AgentOnboardingMode,
    AgentOnboardingRequirement,
    AgentOnboardingRequirementKind,
    BuyerOnboardingPosture,
    GetPrincipalResponse,
    PrincipalRelationship,
    PrincipalRelationshipStatus,
    TrustRecord,
)
from adcp.validation.schema_loader import get_named_schema_document, get_named_validator

ORIGIN = "https://buyer.platform.com"
IDENTITY = PrincipalIdentity(ORIGIN, "buyer_agent")


def schema(schema_path: str) -> dict[str, Any]:
    document = get_named_schema_document(schema_path)
    assert document is not None, f"the pinned bundle does not declare {schema_path}"
    return document


def errors(value: Any, schema_path: str) -> list[str]:
    validator = get_named_validator(schema_path)
    assert validator is not None, f"the pinned bundle does not declare {schema_path}"
    return [error.message for error in validator.iter_errors(value)]


def trust_record(posture: dict[str, Any], *, record_level: bool) -> dict[str, Any]:
    agent: dict[str, Any] = {"url": ORIGIN, "roles": ["adcp:buying"]}
    document: dict[str, Any] = {"agents": [agent]}
    if record_level:
        document["profiles"] = {"adcp": {"onboarding": posture}}
    else:
        agent["profiles"] = {"adcp": {"onboarding": posture}}
    return document


# relationship.status -------------------------------------------------------


def test_relationship_status_is_its_own_enum_with_pending_approval():
    assert schema("enums/principal-relationship-status.json")["enum"] == [
        "pending_approval",
        "active",
        "suspended",
        "blocked",
    ]
    relationship = schema("core/principal-relationship.json")
    status = relationship["properties"]["status"]
    assert status["$ref"].endswith("enums/principal-relationship-status.json")
    assert status["description"] == "Current state of the relationship."
    assert relationship["x-added-in"] == "3.3.0"


def test_pending_approval_accepted_and_pending_rejected():
    assert errors({"status": "pending_approval"}, "core/principal-relationship.json") == []
    assert errors({"status": "pending"}, "core/principal-relationship.json")

    relationship = PrincipalRelationship.model_validate({"status": "pending_approval"})
    assert relationship.status is PrincipalRelationshipStatus.pending_approval
    with pytest.raises(ValidationError):
        PrincipalRelationship.model_validate({"status": "pending"})
    with pytest.raises(ValidationError):
        GetPrincipalResponse.model_validate(
            {"result": {"kind": "unconfigured", "relationship": {"status": "pending"}}}
        )


# get_principal version gate -----------------------------------------------


def service(calls: list[str]) -> PrincipalService:
    async def relationship(identity: PrincipalIdentity) -> dict[str, Any]:
        calls.append(identity.subject)
        return {"status": "pending_approval", "setup": {"message": "Apply"}}

    return PrincipalService(InMemoryPrincipalRecordStore(), relationship_resolver=relationship)


@pytest.mark.parametrize("version", ["3.3", "3.3-beta.2", "3.3.1", "3.4", "4.0"])
async def test_relationship_present_when_served_at_3_3_or_later(version):
    calls: list[str] = []
    response = await service(calls).get_principal(
        IDENTITY, context=ToolContext(resolved_adcp_version=version)
    )
    assert isinstance(response, GetPrincipalResponse)
    body = response.model_dump(mode="json", exclude_none=True)
    assert body["result"]["relationship"]["status"] == "pending_approval"
    assert calls == [ORIGIN]


@pytest.mark.parametrize("version", ["3.2", "3.0", "3.2-rc.1", "2.5", None, "not-a-version"])
async def test_relationship_omitted_when_served_below_3_3_or_unknown(version):
    calls: list[str] = []
    response = await service(calls).get_principal(
        IDENTITY, context=ToolContext(resolved_adcp_version=version)
    )
    assert response.model_dump(mode="json", exclude_none=True)["result"] == {"kind": "unconfigured"}
    assert calls == []


async def test_relationship_omitted_without_context():
    calls: list[str] = []
    response = await service(calls).get_principal(IDENTITY)
    assert "relationship" not in response.model_dump(mode="json", exclude_none=True)["result"]
    assert calls == []


# buyer onboarding posture in trust.json -------------------------------------


@pytest.mark.parametrize("record_level", [False, True])
def test_profiles_adcp_onboarding_validates_on_record_and_agent(record_level):
    posture = {
        "mode": "open",
        "requirements": [{"kind": "sandbox"}, {"kind": "storyboard", "id": "media_buy_seller"}],
        "url": "https://platform.com/sellers",
        "message": "Sellers meeting these requirements are considered.",
    }
    document = trust_record(posture, record_level=record_level)
    assert errors(document, "trust/v1/trust.json") == []
    validate_trust_record(document, operator_domain="platform.com")

    parsed = TrustRecord.model_validate(document).root
    profiles = parsed.profiles if record_level else parsed.agents[0].profiles
    assert profiles is not None and profiles.adcp is not None
    assert profiles.adcp.onboarding is not None
    assert profiles.adcp.onboarding.mode is AgentOnboardingMode.open


def test_buyer_and_seller_postures_share_one_mode_enum():
    assert get_named_schema_document("enums/buyer-onboarding-mode.json") is None
    assert schema("enums/agent-onboarding-mode.json")["enum"] == [
        "open",
        "application",
        "invite_only",
    ]
    for path in ("core/buyer-onboarding-posture.json", "core/agent-onboarding-tier.json"):
        mode_ref = schema(path)["properties"]["mode"]["$ref"]
        assert mode_ref.endswith("enums/agent-onboarding-mode.json"), (path, mode_ref)
    assert errors({"mode": "considers_unsolicited"}, "core/buyer-onboarding-posture.json")
    assert BuyerOnboardingPosture(mode="invite_only").mode is AgentOnboardingMode.invite_only


@pytest.mark.parametrize("record_level", [False, True])
def test_agent_identity_requirement_kind_rejected(record_level):
    kinds = schema("enums/agent-onboarding-requirement-kind.json")["enum"]
    assert "agent_identity" not in kinds
    posture = {"mode": "application", "requirements": [{"kind": "agent_identity"}]}
    assert errors(posture, "core/buyer-onboarding-posture.json")
    assert errors(
        {"sandbox": {"mode": "application", "requirements": [{"kind": "agent_identity"}]}},
        "core/agent-onboarding.json",
    )
    with pytest.raises(TrustRecordError):
        validate_trust_record(
            trust_record(posture, record_level=record_level), operator_domain="platform.com"
        )
    # Readers keep the retired kind as an opaque string, never as a known enum member.
    requirement = AgentOnboardingRequirement.model_validate({"kind": "agent_identity"})
    assert not isinstance(requirement.kind, AgentOnboardingRequirementKind)
    assert requirement.kind == "agent_identity"


def test_documentation_url_is_no_longer_a_declared_field():
    for path in ("core/agent-onboarding.json", "core/buyer-onboarding-posture.json"):
        assert "documentation_url" not in schema(path)["properties"]
    assert "documentation_url" not in AgentOnboarding.model_fields
    assert "documentation_url" not in BuyerOnboardingPosture.model_fields
    # The postures stay open to extension (additionalProperties: true upstream), so a
    # stray documentation_url is an untyped extension value, not a validated URI.
    posture = {"mode": "application", "documentation_url": "not a uri"}
    assert errors(posture, "core/buyer-onboarding-posture.json") == []
    assert (
        errors(
            {"sandbox": {"mode": "open"}, "documentation_url": "not a uri"},
            "core/agent-onboarding.json",
        )
        == []
    )


def test_aao_verified_uses_verified_specialisms():
    requirement = {
        "kind": "aao_verified",
        "role": "media-buy",
        "verification_modes": ["storyboard"],
        "verified_specialisms": ["sales-guaranteed"],
        "adcp_version": "3.3",
    }
    properties = schema("core/agent-onboarding-requirement.json")["properties"]
    assert "verified_specialisms" in properties
    assert "specialisms" not in properties
    assert errors(requirement, "core/agent-onboarding-requirement.json") == []
    assert errors(
        {**requirement, "verified_specialisms": []}, "core/agent-onboarding-requirement.json"
    )
    document = trust_record({"mode": "open", "requirements": [requirement]}, record_level=False)
    validate_trust_record(document, operator_domain="platform.com")

    parsed = AgentOnboardingRequirement.model_validate(requirement)
    assert parsed.kind is AgentOnboardingRequirementKind.aao_verified
    assert parsed.verified_specialisms == ["sales-guaranteed"]


@pytest.mark.parametrize(
    "requirement",
    [
        {"kind": "aao_verified"},
        {"kind": "aao_verified", "verified_specialisms": ["sales-guaranteed"]},
        {"kind": "storyboard"},
        {"kind": "experimental_feature"},
    ],
)
async def test_requirement_kind_conditionals_reject_missing_role_or_id(requirement):
    # adcp main adds if/then conditionals: aao_verified requires role;
    # storyboard and experimental_feature require id.
    assert errors(requirement, "core/agent-onboarding-requirement.json")
    posture = {"mode": "open", "requirements": [requirement]}
    assert errors(posture, "core/buyer-onboarding-posture.json")
    assert errors({"sandbox": posture}, "core/agent-onboarding.json")
    document = trust_record(posture, record_level=False)
    with pytest.raises(TrustRecordError):
        validate_trust_record(document, operator_domain="platform.com")

    # The reader keeps only unknown kinds; a known kind that breaks its
    # conditional is a malformed record, as for any other schema violation.
    async def fetch(url: str, *, allow_private: bool = False) -> TrustResponse:
        return TrustResponse(json.dumps(document).encode())

    evaluator = TrustRecordEvaluator(fetcher=fetch, clock=lambda: 1000)
    with pytest.raises(TrustRecordError):
        await evaluator.evaluate(ORIGIN, role="adcp:buying")


@pytest.mark.parametrize(
    "requirement",
    [
        {"kind": "aao_verified", "role": "media-buy"},
        {"kind": "storyboard", "id": "media_buy_seller"},
        {"kind": "experimental_feature", "id": "identity.trust_json"},
        {"kind": "sandbox"},
        {"kind": "grant"},
    ],
)
def test_requirement_kind_conditionals_accept_complete_requirements(requirement):
    assert errors(requirement, "core/agent-onboarding-requirement.json") == []
