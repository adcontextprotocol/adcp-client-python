"""Public models compose with generated capability models without dict conversion."""

from __future__ import annotations

import inspect
from typing import Any, get_args

import pytest
from pydantic import BaseModel, ValidationError

import adcp.types as public_types
import adcp.types.capabilities as capability_types
from adcp.decisioning.capabilities import MediaBuy, Portfolio
from adcp.types import AcceptancePolicyDiscovery, PrimaryCountry, PublisherDomain
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    AcceptancePolicyDiscovery as BundledAcceptancePolicyDiscovery,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    PrimaryCountry as BundledPrimaryCountry,
)
from adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response import (
    PublisherDomain as BundledPublisherDomain,
)


def _model_references(annotation: Any) -> list[type[BaseModel]]:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in get_args(annotation) for model in _model_references(arg)]


def test_public_models_compose_with_capability_models() -> None:
    discovery = AcceptancePolicyDiscovery(
        catalog_url="https://example.com/acceptance-policy-catalog.json",
        catalog_digest=f"sha256:{'a' * 64}",
    )
    media_buy = MediaBuy(acceptance_policy_discovery=discovery)
    assert media_buy.acceptance_policy_discovery is discovery

    publisher_domain = PublisherDomain("publisher.example")
    primary_country = PrimaryCountry("US")
    portfolio = Portfolio(
        publisher_domains=[publisher_domain],
        primary_countries=[primary_country],
    )
    assert portfolio.publisher_domains[0] is publisher_domain
    assert portfolio.primary_countries is not None
    assert portfolio.primary_countries[0] is primary_country


def test_capability_dicts_parse_to_canonical_public_models() -> None:
    media_buy = MediaBuy.model_validate(
        {
            "acceptance_policy_discovery": {
                "catalog_url": "https://example.com/acceptance-policy-catalog.json",
                "catalog_digest": f"sha256:{'b' * 64}",
            }
        }
    )
    assert isinstance(media_buy.acceptance_policy_discovery, AcceptancePolicyDiscovery)

    portfolio = Portfolio.model_validate(
        {"publisher_domains": ["publisher.example"], "primary_countries": ["US"]}
    )
    assert isinstance(portfolio.publisher_domains[0], PublisherDomain)
    assert portfolio.primary_countries is not None
    assert isinstance(portfolio.primary_countries[0], PrimaryCountry)


def test_composability_patch_preserves_generated_field_constraints() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Portfolio(publisher_domains=[])


@pytest.mark.parametrize(
    ("canonical_model", "bundled_model"),
    [
        (AcceptancePolicyDiscovery, BundledAcceptancePolicyDiscovery),
        (PrimaryCountry, BundledPrimaryCountry),
        (PublisherDomain, BundledPublisherDomain),
    ],
)
def test_composed_model_classes_have_equivalent_wire_schemas(
    canonical_model: type[BaseModel], bundled_model: type[BaseModel]
) -> None:
    """A schema drift must be reviewed before an identity patch remains valid."""
    assert canonical_model is not bundled_model
    assert canonical_model.model_json_schema() == bundled_model.model_json_schema()


def test_capability_fields_have_no_unreviewed_public_model_collisions() -> None:
    """Every same-named public/bundled class split must be reviewed explicitly."""
    # These are intentionally different concepts that happen to share a schema
    # name. They must not be replaced by the public class of the same name.
    semantic_name_collisions = {
        ("CapabilitiesMediaBuy", "content_standards", "ContentStandards"),
        ("CapabilitiesMediaBuy", "performance_feedback", "PerformanceFeedback"),
        ("Execution", "trusted_match", "TrustedMatch"),
    }
    actual_collisions: set[tuple[str, str, str]] = set()

    for export_name in capability_types.__all__:
        capability_model = getattr(capability_types, export_name)
        if not inspect.isclass(capability_model) or not issubclass(capability_model, BaseModel):
            continue
        for field_name, field in capability_model.model_fields.items():
            for nested_model in _model_references(field.annotation):
                public_model = getattr(public_types, nested_model.__name__, None)
                if (
                    inspect.isclass(public_model)
                    and issubclass(public_model, BaseModel)
                    and nested_model is not public_model
                ):
                    actual_collisions.add((export_name, field_name, nested_model.__name__))

    assert actual_collisions == semantic_name_collisions
