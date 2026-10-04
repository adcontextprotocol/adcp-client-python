"""Legacy/canonical creative format compatibility helper behaviour."""

from __future__ import annotations

import pytest

from adcp.canonical_formats import (
    CANONICAL_CREATIVE_AGENT_URL,
    format_is_supported,
    formats_are_equivalent,
    upgrade_legacy_format_id,
)
from adcp.canonical_formats.projection import (
    migrated_format_option_id,
    project_legacy_format_id,
)
from adcp.types import FormatCard
from adcp.types.legacy import LegacyFormatId as FormatId


def test_upgrade_legacy_display_size_to_parameterized_canonical_format_id() -> None:
    upgraded = upgrade_legacy_format_id("display_300x250")

    assert upgraded == FormatId(
        agent_url=CANONICAL_CREATIVE_AGENT_URL,
        id="display_image",
        width=300,
        height=250,
    )


def test_formats_are_equivalent_matches_legacy_display_against_structured_canonical() -> None:
    structured = {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_image",
        "width": 300,
        "height": 250,
    }

    assert formats_are_equivalent("display_300x250", structured)


def test_formats_are_equivalent_rejects_conflicting_dimensions() -> None:
    assert not formats_are_equivalent(
        "display_300x250",
        {
            "agent_url": CANONICAL_CREATIVE_AGENT_URL,
            "id": "display_image",
            "width": 728,
            "height": 90,
        },
    )


def test_format_is_supported_rejects_under_specified_request_for_fixed_product() -> None:
    requested = {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_image",
    }
    supported = {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_image",
        "width": 300,
        "height": 250,
    }

    assert formats_are_equivalent(requested, supported)
    assert not format_is_supported(requested, supported)


def test_format_is_supported_allows_specific_request_for_broad_product() -> None:
    requested = "display_300x250"
    supported = {
        "agent_url": CANONICAL_CREATIVE_AGENT_URL,
        "id": "display_image",
    }

    assert format_is_supported(requested, supported)


def test_canonical_format_helpers_canonicalize_agent_url_case_and_default_port() -> None:
    seller = {
        "agent_url": "https://Creative.AdContextProtocol.org:443/",
        "id": "display_image",
        "width": 300,
        "height": 250,
    }

    assert formats_are_equivalent("display_300x250", seller)


def test_canonical_format_helpers_keep_path_trailing_slash_significant() -> None:
    without_slash = {
        "agent_url": "https://seller.example/formats",
        "id": "native_card",
    }
    with_slash = {
        "agent_url": "https://seller.example/formats/",
        "id": "native_card",
    }

    assert not formats_are_equivalent(without_slash, with_slash)


def test_upgrade_legacy_display_size_does_not_rewrite_seller_owned_namespace() -> None:
    seller_owned = FormatId(agent_url="https://seller.example/formats", id="display_300x250")

    upgraded = upgrade_legacy_format_id(seller_owned)

    assert upgraded == seller_owned
    assert not formats_are_equivalent(
        seller_owned,
        {
            "agent_url": CANONICAL_CREATIVE_AGENT_URL,
            "id": "display_image",
            "width": 300,
            "height": 250,
        },
    )


def _card(**overrides: object) -> FormatCard:
    """Return a public model whose ``format_id`` is a generated reference."""
    return FormatCard.model_validate(
        {
            "format_id": {
                "agent_url": CANONICAL_CREATIVE_AGENT_URL,
                "id": "display_image",
                "width": 300,
                "height": 250,
                **overrides,
            },
            "manifest": {},
        }
    )


def test_format_is_supported_accepts_a_reference_taken_off_a_model_field() -> None:
    requested = _card().format_id

    assert format_is_supported(requested, "display_300x250")
    assert format_is_supported("display_300x250", requested)
    assert format_is_supported(requested, requested)


def test_format_helpers_accept_the_dump_of_a_reference_taken_off_a_model_field() -> None:
    dumped = _card().format_id.model_dump()

    assert formats_are_equivalent(dumped, "display_300x250")
    assert format_is_supported(dumped, dumped)


def test_upgrade_legacy_format_id_accepts_a_reference_taken_off_a_model_field() -> None:
    reference = _card(id="display_300x250", width=None, height=None).format_id

    upgraded = upgrade_legacy_format_id(reference)

    assert upgraded.id == "display_image"
    assert (upgraded.width, upgraded.height) == (300, 250)


def test_format_is_supported_names_the_parameter_that_carried_the_bad_value() -> None:
    with pytest.raises(TypeError, match=r"^requested must be a string"):
        format_is_supported(object(), "display_300x250")

    with pytest.raises(TypeError, match=r"^supported must be a string"):
        format_is_supported("display_300x250", object())


def test_upgrade_legacy_format_id_names_its_own_parameter() -> None:
    with pytest.raises(TypeError, match=r"^value must be a string"):
        upgrade_legacy_format_id(object())


def test_format_id_refusal_reports_the_type_it_received() -> None:
    with pytest.raises(TypeError, match=r"got list$"):
        format_is_supported("display_300x250", ["display_300x250"])


def test_projection_accepts_a_reference_taken_off_a_model_field() -> None:
    reference = _card().format_id

    assert migrated_format_option_id(reference) == migrated_format_option_id(reference.model_dump())

    projected = project_legacy_format_id(reference, product_id="p1", field="format_ids[0]")

    assert projected.diagnostic is None
    assert projected.declaration is not None
