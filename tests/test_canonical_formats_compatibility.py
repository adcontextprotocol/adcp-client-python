"""Legacy/canonical creative format compatibility helper behaviour."""

from __future__ import annotations

from adcp.canonical_formats import (
    CANONICAL_CREATIVE_AGENT_URL,
    format_is_supported,
    formats_are_equivalent,
    upgrade_legacy_format_id,
)
from adcp.types import FormatId


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
