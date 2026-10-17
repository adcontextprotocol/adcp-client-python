"""Regression tests for the pinned AdCP 3.3 identity preview schemas and types."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator, FormatChecker
from pydantic import ValidationError

from adcp.server.base import ToolContext
from adcp.server.principal import (
    InMemoryPrincipalRecordStore,
    PrincipalIdentity,
    PrincipalService,
)
from adcp.signing import TrustRecordError, validate_trust_record
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

SCHEMAS = Path(str(files("adcp.signing").joinpath("_identity_schemas")))
ORIGIN = "https://buyer.platform.com"
IDENTITY = PrincipalIdentity(ORIGIN, "buyer_agent")


def bundle() -> dict[str, Any]:
    return json.loads((SCHEMAS / "bundle.json").read_text())


def errors(value: Any, schema_path: str) -> list[str]:
    key = schema_path.removesuffix(".json").replace("/", "_").replace("-", "_")
    definitions = bundle()["definitions"]
    schema = {"$ref": f"#/definitions/{key}", "definitions": definitions}
    validator = Draft7Validator(schema, format_checker=FormatChecker())
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
    definitions = bundle()["definitions"]
    assert definitions["enums_principal_relationship_status"]["enum"] == [
        "pending_approval",
        "active",
        "suspended",
        "blocked",
    ]
    assert definitions["core_principal_relationship"]["properties"]["status"] == {
        "$ref": "#/definitions/enums_principal_relationship_status",
        "description": "Current state of the relationship.",
    }
    assert definitions["core_principal_relationship"]["x-added-in"] == "3.3.0"


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


@pytest.mark.parametrize("version", ["3.3", "3.3-beta.1", "3.3.1", "3.4", "4.0"])
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
    definitions = bundle()["definitions"]
    assert "enums_buyer_onboarding_mode" not in definitions
    assert definitions["enums_agent_onboarding_mode"]["enum"] == [
        "open",
        "application",
        "invite_only",
    ]
    mode_ref = {"$ref": "#/definitions/enums_agent_onboarding_mode"}
    assert definitions["core_buyer_onboarding_posture"]["properties"]["mode"] == mode_ref
    assert definitions["core_agent_onboarding_tier"]["properties"]["mode"] == mode_ref
    assert errors({"mode": "considers_unsolicited"}, "core/buyer-onboarding-posture.json")
    assert BuyerOnboardingPosture(mode="invite_only").mode is AgentOnboardingMode.invite_only


@pytest.mark.parametrize("record_level", [False, True])
def test_agent_identity_requirement_kind_rejected(record_level):
    kinds = bundle()["definitions"]["enums_agent_onboarding_requirement_kind"]["enum"]
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
    definitions = bundle()["definitions"]
    for key in ("core_agent_onboarding", "core_buyer_onboarding_posture"):
        assert "documentation_url" not in definitions[key]["properties"]
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
    properties = bundle()["definitions"]["core_agent_onboarding_requirement"]["properties"]
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


# provenance ---------------------------------------------------------------


def test_upstream_lists_exactly_the_pinned_schema_files():
    upstream = json.loads((SCHEMAS / "UPSTREAM.json").read_text())
    present = {
        path.relative_to(SCHEMAS).as_posix()
        for path in SCHEMAS.rglob("*.json")
        if path.name not in {"UPSTREAM.json", "bundle.json"}
    }
    assert set(upstream["sources"]) == present
    for source in upstream["sources"].values():
        assert len(source["commit"]) == 40
        int(source["commit"], 16)
    assert set(upstream["composition"]) <= present
