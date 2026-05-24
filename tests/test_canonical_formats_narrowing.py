"""``FORMAT_DECLARATION_DIVERGENT`` narrowing check.

Compares v2 ``params`` against v1 ``requirements`` and verifies the
divergence-detection logic covers the four divergence kinds the
schema-conformance check needs: exceeds-max, below-min, not-subset,
not-equal.
"""

from __future__ import annotations

import pytest

from adcp.canonical_formats import check_narrows, narrowing_advisory
from adcp.canonical_formats.advisory import SDK_ID
from adcp.types import CanonicalFormatKind, ProductFormatDeclaration

# ---------------------------------------------------------------------------
# check_narrows — no divergence cases
# ---------------------------------------------------------------------------


def test_v2_within_v1_caps_does_not_diverge() -> None:
    assert (
        check_narrows(
            {"max_file_size_kb": 100, "image_formats": ["jpg"]},
            {"max_file_size_kb": 200, "image_formats": ["jpg", "png"]},
        )
        == []
    )


def test_v2_below_v1_minimum_does_diverge() -> None:
    divs = check_narrows({"min_width": 100}, {"min_width": 300})
    assert len(divs) == 1
    assert divs[0]["kind"] == "below_min"
    assert divs[0]["v1_min"] == 300
    assert divs[0]["v2_value"] == 100


def test_v2_silently_omitting_field_is_not_divergence() -> None:
    """v2 omitting a field that v1 declared is "narrows into unconstrained
    space" — NOT a divergence per the schema."""
    assert check_narrows({}, {"max_file_size_kb": 200}) == []


def test_v1_omitting_field_is_not_divergence() -> None:
    """v1 not declaring a constraint v2 declares is also not a divergence."""
    assert check_narrows({"max_file_size_kb": 100}, {}) == []


# ---------------------------------------------------------------------------
# check_narrows — exceeds_max
# ---------------------------------------------------------------------------


def test_exceeds_max_on_named_cap() -> None:
    divs = check_narrows({"max_file_size_kb": 500}, {"max_file_size_kb": 200})
    assert len(divs) == 1
    assert divs[0] == {
        "field": "max_file_size_kb",
        "kind": "exceeds_max",
        "v1_max": 200,
        "v2_value": 500,
    }


def test_exceeds_max_on_value_being_capped() -> None:
    """v1 says ``max_width=300``, v2 declares ``width=500`` — that's a divergence
    (v2 width is being capped by v1.max_width and exceeds it)."""
    divs = check_narrows({"width": 500}, {"max_width": 300})
    assert len(divs) == 1
    assert divs[0]["kind"] == "exceeds_max"
    assert divs[0]["v1_max"] == 300
    assert divs[0]["v2_value"] == 500


# ---------------------------------------------------------------------------
# check_narrows — not_subset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "v2,v1,is_div",
    [
        # subset → no divergence
        (["jpg"], ["jpg", "png"], False),
        (["jpg", "png"], ["jpg", "png"], False),
        # not subset → divergence
        (["jpg", "tiff"], ["jpg", "png"], True),
        (["webp"], ["jpg", "png"], True),
    ],
)
def test_image_formats_subset_check(v2: list[str], v1: list[str], is_div: bool) -> None:
    divs = check_narrows({"image_formats": v2}, {"image_formats": v1})
    assert bool(divs) is is_div


# ---------------------------------------------------------------------------
# check_narrows — not_equal
# ---------------------------------------------------------------------------


def test_exact_field_disagreement_is_divergence() -> None:
    divs = check_narrows({"ssl_required": False}, {"ssl_required": True})
    assert len(divs) == 1
    assert divs[0]["kind"] == "not_equal"
    assert divs[0]["v1_value"] is True
    assert divs[0]["v2_value"] is False


# ---------------------------------------------------------------------------
# narrowing_advisory — emits the wire-shape Error
# ---------------------------------------------------------------------------


def test_advisory_is_none_when_narrows() -> None:
    d = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 100},
    )
    assert (
        narrowing_advisory(d, v1_requirements={"max_file_size_kb": 200}, v1_format_id="x") is None
    )


def test_advisory_emitted_on_divergence() -> None:
    d = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 500, "image_formats": ["tiff"]},
    )
    a = narrowing_advisory(
        d,
        v1_requirements={"max_file_size_kb": 200, "image_formats": ["jpg", "png"]},
        v1_format_id="display_300x250_image",
        field_path="products[0].format_options[0]",
    )
    assert a is not None
    assert a.code == "FORMAT_DECLARATION_DIVERGENT"
    assert a.source.value == "sdk"
    assert a.sdk_id == SDK_ID
    assert a.field == "products[0].format_options[0]"
    assert a.details["format_kind"] == "image"
    assert a.details["v1_format_id"] == "display_300x250_image"
    divs = a.details["divergences"]
    assert {d["field"] for d in divs} == {"max_file_size_kb", "image_formats"}


def test_advisory_tolerates_pydantic_input() -> None:
    """``v1_requirements`` may be a Pydantic model; ``check_narrows`` walks it."""

    class _FakeReq:
        def model_dump(self, exclude_none: bool = False) -> dict:
            return {"max_file_size_kb": 200}

    d = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 500},
    )
    a = narrowing_advisory(d, v1_requirements=_FakeReq(), v1_format_id="x")
    assert a is not None
