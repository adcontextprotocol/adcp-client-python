"""Canonical-to-legacy routes are scoped and collision-safe."""

from __future__ import annotations

import pytest

import adcp
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionError,
    project_canonical_response_to_legacy,
)


def _publisher_declaration(publisher_domain: str, legacy_owner: str, legacy_id: str) -> adcp.Format:
    return adcp.Format(
        format_option_id="shared-option",
        publisher_domain=publisher_domain,
        format_kind="image",
        params={"width": 300, "height": 250},
        v1_format_ref=[{"agent_url": legacy_owner, "id": legacy_id}],
    )


def _creative(creative_id: str, publisher_domain: str) -> dict[str, object]:
    return {
        "creative_id": creative_id,
        "format_kind": "image",
        "format_option_ref": {
            "scope": "publisher",
            "publisher_domain": publisher_domain,
            "format_option_id": "shared-option",
        },
    }


def test_same_option_id_is_resolved_independently_for_two_publishers() -> None:
    one = _publisher_declaration("one.example", "https://formats.one.example/mcp", "one-banner")
    two = _publisher_declaration("two.example", "https://formats.two.example/mcp", "two-banner")

    wire = project_canonical_response_to_legacy(
        {
            "creatives": [
                _creative("creative-one", "one.example"),
                _creative("creative-two", "two.example"),
            ]
        },
        sources=[one, two],
    )

    assert wire["creatives"][0]["format_id"] == {
        "agent_url": "https://formats.one.example/mcp",
        "id": "one-banner",
    }
    assert wire["creatives"][1]["format_id"] == {
        "agent_url": "https://formats.two.example/mcp",
        "id": "two-banner",
    }


def test_conflicting_routes_for_same_publisher_and_option_fail_closed() -> None:
    first = _publisher_declaration(
        "publisher.example", "https://formats.publisher.example/mcp", "first-banner"
    )
    conflicting = _publisher_declaration(
        "publisher.example", "https://formats.publisher.example/mcp", "second-banner"
    )

    with pytest.raises(CanonicalFormatLegacyResolutionError, match="conflicting declarations"):
        project_canonical_response_to_legacy(
            {"creatives": [_creative("creative-one", "publisher.example")]},
            sources=[first, conflicting],
        )


def test_product_scope_does_not_fall_back_to_another_product() -> None:
    declaration = adcp.Format(
        format_option_id="shared-option",
        format_kind="image",
        params={"width": 300, "height": 250},
        v1_format_ref=[{"agent_url": "https://formats.publisher.example/mcp", "id": "banner"}],
    )

    with pytest.raises(CanonicalFormatLegacyResolutionError, match="no discovered declaration"):
        project_canonical_response_to_legacy(
            {
                "products": [{"product_id": "product-one", "format_options": [declaration]}],
                "packages": [
                    {
                        "product_id": "product-two",
                        "format_option_refs": [
                            {"scope": "product", "format_option_id": "shared-option"}
                        ],
                    }
                ],
            }
        )
