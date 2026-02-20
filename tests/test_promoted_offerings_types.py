"""Tests for PromotedOfferingsRequirement enum and PromotedOfferingsAssetRequirements model.

These types allow formats to declare what content must be present in a promoted
offerings asset (e.g., brand logos, colors, SI agent URL).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp import PromotedOfferingsAssetRequirements, PromotedOfferingsRequirement


class TestPromotedOfferingsRequirementEnum:
    """Tests for PromotedOfferingsRequirement enum."""

    def test_importable_from_adcp_types(self):
        """Should be importable from adcp.types."""
        from adcp.types import PromotedOfferingsRequirement as T

        assert T is PromotedOfferingsRequirement

    def test_has_all_spec_values(self):
        """Should have all values defined in promoted-offerings-requirement.json schema."""
        expected_values = {
            "si_agent_url",
            "offerings",
            "brand.logos",
            "brand.colors",
            "brand.tone",
            "brand.assets",
            "brand.product_catalog",
        }
        actual_values = {member.value for member in PromotedOfferingsRequirement}
        assert actual_values == expected_values

    def test_enum_member_values(self):
        """Python member names map to dot-notation wire values per the spec."""
        assert PromotedOfferingsRequirement.si_agent_url.value == "si_agent_url"
        assert PromotedOfferingsRequirement.offerings.value == "offerings"
        assert PromotedOfferingsRequirement.brand_logos.value == "brand.logos"
        assert PromotedOfferingsRequirement.brand_colors.value == "brand.colors"
        assert PromotedOfferingsRequirement.brand_tone.value == "brand.tone"
        assert PromotedOfferingsRequirement.brand_assets.value == "brand.assets"
        assert PromotedOfferingsRequirement.brand_product_catalog.value == "brand.product_catalog"


class TestPromotedOfferingsAssetRequirementsModel:
    """Tests for PromotedOfferingsAssetRequirements model."""

    def test_importable_from_adcp_types(self):
        """Should be importable from adcp.types."""
        from adcp.types import PromotedOfferingsAssetRequirements as T

        assert T is PromotedOfferingsAssetRequirements

    def test_requires_defaults_to_none(self):
        """requires field is optional per the schema (no 'required' array)."""
        req = PromotedOfferingsAssetRequirements()
        assert req.requires is None

    def test_accepts_enum_values_in_requires(self):
        """requires field accepts a list of PromotedOfferingsRequirement enum values."""
        req = PromotedOfferingsAssetRequirements(
            requires=[
                PromotedOfferingsRequirement.brand_logos,
                PromotedOfferingsRequirement.brand_colors,
            ]
        )
        assert req.requires is not None
        assert len(req.requires) == 2
        assert PromotedOfferingsRequirement.brand_logos in req.requires
        assert PromotedOfferingsRequirement.brand_colors in req.requires

    def test_accepts_string_values_in_requires(self):
        """requires field coerces dot-notation strings to enum members.

        Pydantic v2 coerces string inputs to plain Enum members by value,
        which is how JSON wire data is parsed into typed models.
        """
        req = PromotedOfferingsAssetRequirements(requires=["brand.logos", "si_agent_url"])
        assert req.requires is not None
        assert PromotedOfferingsRequirement.brand_logos in req.requires
        assert PromotedOfferingsRequirement.si_agent_url in req.requires

    def test_validates_from_json_dict(self):
        """Should validate correctly from a JSON-like dict matching the spec."""
        data = {"requires": ["brand.logos", "brand.tone", "offerings"]}
        req = PromotedOfferingsAssetRequirements.model_validate(data)
        assert req.requires is not None
        assert len(req.requires) == 3
        assert PromotedOfferingsRequirement.brand_logos in req.requires
        assert PromotedOfferingsRequirement.brand_tone in req.requires
        assert PromotedOfferingsRequirement.offerings in req.requires

    def test_serializes_to_spec_json(self):
        """Should serialize requires array with dot-notation string values for wire format."""
        req = PromotedOfferingsAssetRequirements(
            requires=[PromotedOfferingsRequirement.brand_logos]
        )
        data = req.model_dump(mode="json", exclude_none=True)
        assert data == {"requires": ["brand.logos"]}

    def test_requires_min_one_item(self):
        """requires array must have at least one item when present (minItems: 1 in schema)."""
        with pytest.raises(ValidationError):
            PromotedOfferingsAssetRequirements(requires=[])

    def test_all_requirement_values_accepted(self):
        """All PromotedOfferingsRequirement values are accepted in requires list."""
        all_requirements = list(PromotedOfferingsRequirement)
        req = PromotedOfferingsAssetRequirements(requires=all_requirements)
        assert req.requires is not None
        assert len(req.requires) == len(all_requirements)

    def test_si_agent_url_only_requirement(self):
        """A format can require only an SI agent URL — the condition for conversational formats."""
        req = PromotedOfferingsAssetRequirements(
            requires=[PromotedOfferingsRequirement.si_agent_url]
        )
        assert req.requires == [PromotedOfferingsRequirement.si_agent_url]
        data = req.model_dump(mode="json", exclude_none=True)
        assert data == {"requires": ["si_agent_url"]}

    def test_si_agent_with_offerings_requirement(self):
        """A format can require both SI agent URL and offerings for conversational experiences."""
        req = PromotedOfferingsAssetRequirements(
            requires=[
                PromotedOfferingsRequirement.si_agent_url,
                PromotedOfferingsRequirement.offerings,
            ]
        )
        assert req.requires is not None
        assert PromotedOfferingsRequirement.si_agent_url in req.requires
        assert PromotedOfferingsRequirement.offerings in req.requires

    def test_extra_fields_preserved(self):
        """Unknown fields are preserved (additionalProperties: true in schema)."""
        data = {"requires": ["brand.logos"], "custom_field": "custom_value"}
        req = PromotedOfferingsAssetRequirements.model_validate(data)
        dumped = req.model_dump(mode="json", exclude_none=True)
        assert dumped["custom_field"] == "custom_value"
        assert dumped["requires"] == ["brand.logos"]
