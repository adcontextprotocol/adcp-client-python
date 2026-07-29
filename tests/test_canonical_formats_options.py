"""Closed-set ``format_options[]`` validator behaviour.

Sellers MUST reject a ``create_media_buy`` whose creative manifest
targets a ``format_kind`` outside the product's published
``format_options[]``. These tests exercise the pre-call guard.
"""

from __future__ import annotations

import pytest

from adcp.canonical_formats import (
    FormatKindNotInClosedSetError,
    find_declaration_by_kind,
    validate_format_kind_in_options,
)
from adcp.types import CanonicalFormatKind, ProductFormatDeclaration


def _decl(
    kind: CanonicalFormatKind,
    *,
    capability_id: str | None = None,
) -> ProductFormatDeclaration:
    return ProductFormatDeclaration(
        format_kind=kind,
        params={},
        capability_id=capability_id,
    )


# ---------------------------------------------------------------------------
# validate_format_kind_in_options
# ---------------------------------------------------------------------------


def test_validator_accepts_kind_in_closed_set() -> None:
    options = [_decl(CanonicalFormatKind.image), _decl(CanonicalFormatKind.video_vast)]
    # Both string and enum forms accepted.
    validate_format_kind_in_options("image", options)
    validate_format_kind_in_options(CanonicalFormatKind.video_vast, options)


def test_validator_rejects_kind_outside_closed_set() -> None:
    options = [_decl(CanonicalFormatKind.image)]
    with pytest.raises(FormatKindNotInClosedSetError) as exc:
        validate_format_kind_in_options("audio_daast", options)

    assert exc.value.format_kind == "audio_daast"
    assert exc.value.accepted_kinds == ["image"]


def test_validator_rejection_message_mentions_kind_and_accepted_set() -> None:
    options = [_decl(CanonicalFormatKind.image), _decl(CanonicalFormatKind.html5)]
    with pytest.raises(FormatKindNotInClosedSetError) as exc:
        validate_format_kind_in_options("video_vast", options)

    msg = str(exc.value)
    assert "video_vast" in msg
    assert "image" in msg
    assert "html5" in msg


def test_validator_against_empty_closed_set_rejects_everything() -> None:
    with pytest.raises(FormatKindNotInClosedSetError) as exc:
        validate_format_kind_in_options("image", [])
    assert exc.value.accepted_kinds == []


# ---------------------------------------------------------------------------
# find_declaration_by_kind
# ---------------------------------------------------------------------------


def test_lookup_returns_matching_declaration() -> None:
    image_a = _decl(CanonicalFormatKind.image, capability_id="cap_a")
    options = [image_a, _decl(CanonicalFormatKind.video_vast)]

    assert find_declaration_by_kind("image", options) is image_a
    assert find_declaration_by_kind(CanonicalFormatKind.video_vast, options) is options[1]


def test_lookup_returns_none_when_no_match() -> None:
    options = [_decl(CanonicalFormatKind.image)]
    assert find_declaration_by_kind("audio_daast", options) is None


def test_lookup_disambiguates_with_format_option_id() -> None:
    """Two image declarations on the same product MUST be disambiguated by
    ``capability_id`` per the ProductFormatDeclaration contract."""
    image_a = _decl(CanonicalFormatKind.image, capability_id="cap_a")
    image_b = _decl(CanonicalFormatKind.image, capability_id="cap_b")
    options = [image_a, image_b]

    assert find_declaration_by_kind("image", options, format_option_id="cap_a") is image_a
    assert find_declaration_by_kind("image", options, format_option_id="cap_b") is image_b
    assert find_declaration_by_kind("image", options, format_option_id="cap_c") is None


def test_lookup_without_capability_id_returns_first_kind_match() -> None:
    """When ``capability_id`` is omitted and multiple kinds match, the first
    in declaration order wins — same precedence the registry uses elsewhere."""
    image_a = _decl(CanonicalFormatKind.image, capability_id="cap_a")
    image_b = _decl(CanonicalFormatKind.image, capability_id="cap_b")
    options = [image_a, image_b]

    assert find_declaration_by_kind("image", options) is image_a


# ---------------------------------------------------------------------------
# to_wire_error
# ---------------------------------------------------------------------------


def test_to_wire_error_produces_unsupported_feature() -> None:
    err = FormatKindNotInClosedSetError("audio_daast", ["image", "video_vast"])
    wire = err.to_wire_error()

    assert wire.code == "UNSUPPORTED_FEATURE"
    assert wire.field == "manifest.format_kind"
    assert wire.details == {
        "rejected_value": "audio_daast",
        "accepted_values": ["image", "video_vast"],  # sorted, dedup'd
    }


def test_to_wire_error_field_override() -> None:
    err = FormatKindNotInClosedSetError("image", ["video_vast"])
    wire = err.to_wire_error(field="packages[0].manifest.format_kind")
    assert wire.field == "packages[0].manifest.format_kind"


def test_to_wire_error_accepted_values_dedup_and_sort() -> None:
    err = FormatKindNotInClosedSetError("custom", ["image", "image", "audio_daast"])
    wire = err.to_wire_error()
    assert wire.details["accepted_values"] == ["audio_daast", "image"]


# ---------------------------------------------------------------------------
# find_declaration_by_v1_format_id (seller-side v1 inbound lookup)
# ---------------------------------------------------------------------------


def test_v1_inbound_lookup_finds_declaration_by_ref() -> None:
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    ref = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_300x250_image",
    )
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image, params={}, v1_format_ref=[ref]
    )

    found = find_declaration_by_v1_format_id(ref, [decl])
    assert found is decl


def test_v1_inbound_lookup_misses_when_no_ref_matches() -> None:
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        v1_format_ref=[
            FormatId(
                agent_url="https://creative.adcontextprotocol.org",
                id="display_300x250_image",
            ),
        ],
    )
    wrong = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_728x90_image",
    )

    assert find_declaration_by_v1_format_id(wrong, [decl]) is None


def test_v1_inbound_lookup_distinguishes_by_agent_url() -> None:
    """Same ``id`` on a different ``agent_url`` is a different format identity."""
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        v1_format_ref=[
            FormatId(
                agent_url="https://creative.adcontextprotocol.org",
                id="display_300x250_image",
            ),
        ],
    )
    other_seller = FormatId(
        agent_url="https://other.example",
        id="display_300x250_image",
    )

    assert find_declaration_by_v1_format_id(other_seller, [decl]) is None


def test_v1_inbound_lookup_canonicalises_agent_url_host_case() -> None:
    """RFC 3986 §6 host-casefolding: ``Creative.X`` must match ``creative.x``.

    Without canonicalization a seller publishing
    ``https://Creative.AdContextProtocol.org`` would silently miss-match
    a buyer's ``https://creative.adcontextprotocol.org`` and the SDK
    would return a wrongful ``UNSUPPORTED_FEATURE``.
    """
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    seller_decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        v1_format_ref=[
            FormatId(
                agent_url="https://Creative.AdContextProtocol.org",
                id="display_300x250_image",
            ),
        ],
    )
    buyer_ref = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_300x250_image",
    )

    assert find_declaration_by_v1_format_id(buyer_ref, [seller_decl]) is seller_decl


def test_v1_inbound_lookup_canonicalises_default_port() -> None:
    """Default-port stripping: ``https://x.example:443`` matches ``https://x.example``."""
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    seller_decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={},
        v1_format_ref=[
            FormatId(
                agent_url="https://creative.adcontextprotocol.org:443",
                id="display_300x250_image",
            ),
        ],
    )
    buyer_ref = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_300x250_image",
    )

    assert find_declaration_by_v1_format_id(buyer_ref, [seller_decl]) is seller_decl


def test_v1_inbound_lookup_with_no_refs_returns_none() -> None:
    from adcp.canonical_formats import find_declaration_by_v1_format_id
    from adcp.types.legacy import LegacyFormatId as FormatId

    decl = ProductFormatDeclaration(format_kind=CanonicalFormatKind.image, params={})
    ref = FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_300x250_image",
    )

    assert find_declaration_by_v1_format_id(ref, [decl]) is None
