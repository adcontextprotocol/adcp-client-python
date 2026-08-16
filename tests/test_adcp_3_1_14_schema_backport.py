"""Regression coverage for enum-reference fixes in AdCP 3.1.14."""

from adcp.types import AdcpProtocol, CatalogType, PropertyType
from adcp.types.generated_poc.core.registry_event import BadgeRole
from adcp.types.generated_poc.formats.canonical.sponsored_placement import (
    CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven,
)
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import TrustedMatch


def test_registry_badge_role_uses_canonical_protocol_enum() -> None:
    role = BadgeRole.model_validate("measurement")

    assert role.root is AdcpProtocol.measurement
    assert role.model_dump(mode="json") == "measurement"


def test_sponsored_placement_accepts_promotion_catalog_type() -> None:
    declaration = CanonicalFormatSponsoredPlacementRetailMediaCatalogDriven(
        supported_catalog_types=["promotion"]
    )

    assert declaration.supported_catalog_types == [CatalogType.promotion]


def test_trusted_match_accepts_linear_tv_surface() -> None:
    capability = TrustedMatch(surfaces=["linear_tv"])

    assert capability.surfaces == [PropertyType.linear_tv]
