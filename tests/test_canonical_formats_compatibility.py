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
from adcp.types.canonical_creative import sanitize_canonical_schema
from adcp.types.legacy import LegacyFormatId as FormatId
from adcp.validation.schema_loader import get_named_schema_document


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


def _schema_parameters() -> frozenset[str]:
    """The parameters ``core/format-id.json`` declares beyond the identity pair.

    Read from the bundled schema rather than from the generated model, so the
    guard below fails when a helper stops comparing a parameter the *spec*
    declares — not merely when it disagrees with the model it was written
    against.
    """
    document = get_named_schema_document("core/format-id.json")
    assert document is not None, "core/format-id.json is absent from the bundled schema tree"
    return frozenset(document["properties"]) - frozenset(document["required"])


#: One pair of conflicting, dependency-legal values per declared parameter.
#: ``pixel_ratio`` needs ``width``/``height`` present, which ``_BASE`` supplies.
_CONFLICTING_VALUES: dict[str, tuple[object, object]] = {
    "width": (300, 728),
    "height": (250, 90),
    "duration_ms": (15000, 30000),
    "pixel_ratio": (2, 3),
}

_BASE = {
    "agent_url": CANONICAL_CREATIVE_AGENT_URL,
    "id": "display_image",
    "width": 300,
    "height": 250,
}


def test_the_conflict_table_covers_every_parameter_the_schema_declares() -> None:
    """Keeps the parametrized guard below honest as the schema grows.

    A new parameter in ``core/format-id.json`` fails here until it is given a
    conflicting pair, which then puts it under the comparison guard.
    """
    assert set(_CONFLICTING_VALUES) == set(_schema_parameters())


@pytest.mark.parametrize("parameter", sorted(_CONFLICTING_VALUES))
def test_the_helpers_refuse_a_conflict_in_every_declared_parameter(parameter: str) -> None:
    """Two references that disagree about any declared parameter differ.

    The comparison walks the parameters the model declares, so a parameter
    added to the schema is compared without being named here.
    """
    supported_value, requested_value = _CONFLICTING_VALUES[parameter]
    supported = {**_BASE, parameter: supported_value}
    requested = {**_BASE, parameter: requested_value}

    assert not formats_are_equivalent(requested, supported)
    assert not format_is_supported(requested, supported)


def test_format_is_supported_refuses_a_request_that_omits_a_required_pixel_ratio() -> None:
    """A 2x product is not served by a creative that never claims a density.

    ``core/format-id.json`` makes the omitted form the 1x variant ("Omit for
    the backward-compatible 1x variant"), so silence here is a 1x asset being
    delivered into a 2x slot, not an unspecified parameter.
    """
    supported = {**_BASE, "pixel_ratio": 2}

    assert not formats_are_equivalent(_BASE, supported)
    assert not format_is_supported(_BASE, supported)


def test_format_is_supported_accepts_the_omitted_form_of_a_1x_product() -> None:
    """The mirror of the case above, and the reason it is not a plain equality.

    A seller that spells ``pixel_ratio: 1`` publishes the same render as one
    that omits it, so a request omitting the field must still be served.
    """
    assert format_is_supported(_BASE, {**_BASE, "pixel_ratio": 1})


def test_upgrade_legacy_format_id_keeps_every_parameter_it_was_given() -> None:
    """The legacy-size upgrade reparameterizes the ID; it drops no parameter."""
    legacy = FormatId(
        agent_url=CANONICAL_CREATIVE_AGENT_URL,
        id="display_300x250",
        width=300,
        height=250,
        duration_ms=15000,
        pixel_ratio=2,
    )

    upgraded = upgrade_legacy_format_id(legacy)

    assert upgraded.id == "display_image"
    assert (upgraded.width, upgraded.height) == (300, 250)
    assert upgraded.duration_ms == 15000
    assert upgraded.pixel_ratio == 2


#: ``(supported pixel_ratio, requested pixel_ratio, served)`` for two otherwise
#: identical 300x250 references; ``None`` omits the field. The omitted form is
#: the 1x variant on *both* sides, so the table is symmetric in its verdicts.
_PIXEL_RATIO_MATRIX: list[tuple[float | None, float | None, bool]] = [
    (None, None, True),
    (None, 1, True),
    (None, 2, False),
    (1, None, True),
    (2, None, False),
    (1, 1, True),
    (2, 2, True),
    (1, 2, False),
    (2, 1, False),
]


def _with_pixel_ratio(pixel_ratio: float | None) -> dict[str, object]:
    return _BASE if pixel_ratio is None else {**_BASE, "pixel_ratio": pixel_ratio}


@pytest.mark.parametrize(("supported", "requested", "served"), _PIXEL_RATIO_MATRIX)
def test_format_is_supported_reads_an_omitted_pixel_ratio_as_1x_on_both_sides(
    supported: float | None, requested: float | None, served: bool
) -> None:
    """A supported 300x250 that omits ``pixel_ratio`` is the 1x variant.

    ``core/format-id.json`` describes the omitted field as "the
    backward-compatible 1x variant" without naming a side, so a product that
    omits it serves only 1x — it refuses a 2x creative exactly as a 2x product
    refuses a creative that omits the field.

    ``formats_are_equivalent`` reads the omission the same way, so for two
    references of the same size it agrees with gating in both directions.
    """
    requested_ref = _with_pixel_ratio(requested)
    supported_ref = _with_pixel_ratio(supported)

    assert format_is_supported(requested_ref, supported_ref) is served
    assert formats_are_equivalent(requested_ref, supported_ref) is served
    assert formats_are_equivalent(supported_ref, requested_ref) is served


@pytest.mark.parametrize("requested", [None, 1, 2])
def test_a_template_supported_format_accepts_any_pixel_ratio(requested: float | None) -> None:
    """The implied 1x needs dimensions, because ``pixel_ratio`` requires them.

    A bare ``display_image`` names no 300x250 variant to be 1x of, so it stays
    the broad template it was and serves a request at any density.
    """
    template = {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display_image"}

    assert format_is_supported(_with_pixel_ratio(requested), template)
    assert formats_are_equivalent(_with_pixel_ratio(requested), template)


def test_a_legacy_size_supported_format_is_the_1x_variant() -> None:
    """A legacy ``display_300x250`` upgrades to a dimensioned, 1x reference."""
    legacy = {"agent_url": CANONICAL_CREATIVE_AGENT_URL, "id": "display_300x250"}

    assert format_is_supported(_BASE, legacy)
    assert not format_is_supported({**_BASE, "pixel_ratio": 2}, legacy)


def test_schema_sanitizer_recognises_an_untitled_reference_carrying_pixel_ratio() -> None:
    """The legacy-tuple shape check knows every parameter the model declares.

    An untitled object holding only format-reference properties is a legacy
    reference, so its ``agent_url`` is stripped — including when it carries
    ``pixel_ratio``, which a hand-written key list left out.
    """
    reference = {
        "type": "object",
        "properties": {key: {} for key in ("agent_url", "id", "width", "height", "pixel_ratio")},
    }

    sanitized = sanitize_canonical_schema({"type": "object", "properties": {"ref": reference}})

    assert "agent_url" not in sanitized["properties"]["ref"]["properties"]
