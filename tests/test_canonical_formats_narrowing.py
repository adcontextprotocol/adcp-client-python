"""``FORMAT_DECLARATION_DIVERGENT`` narrowing check.

Compares v2 ``params`` against v1 ``requirements`` and verifies the
divergence-detection logic covers the four divergence kinds the
schema-conformance check needs: exceeds-max, below-min, not-subset,
not-equal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adcp.canonical_formats import check_narrows, narrowing_advisory
from adcp.canonical_formats.advisory import SDK_ID
from adcp.types import CanonicalFormatKind, Format

# Unmodified protocol v3.2.7 static/test-vectors/v1-canonical-mapping.json.
_NARROWING_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/canonical-narrowing-vectors-3.2.7.json").read_text()
)["narrowing_vectors"]


@pytest.mark.parametrize("vector", _NARROWING_VECTORS, ids=lambda vector: vector["id"])
def test_published_narrowing_vector(vector: dict) -> None:
    requirements = {}
    for field, constraint in vector["v1_baseline"].items():
        if constraint["kind"] == "exact":
            requirements[field] = constraint["value"]
        elif constraint["kind"] == "set":
            requirements[field] = constraint["values"]
        elif field.endswith("_range"):
            requirements[field] = [constraint["min"], constraint["max"]]
        else:
            requirements[f"min_{field}"] = constraint["min"]
            requirements[f"max_{field}"] = constraint["max"]
    divergences = check_narrows(vector["v2_params"], requirements)
    assert (not divergences) is vector["expected"]["narrows"]
    if divergences:
        fields = {d.field.removeprefix("min_").removeprefix("max_") for d in divergences}
        assert vector["expected"]["conflict"] in fields


@pytest.mark.parametrize(
    ("params", "requirements", "kind"),
    [
        (
            {"duration_ms_range": [5000, 120000]},
            {"min_duration_ms": 3000, "max_duration_ms": 90000},
            "exceeds_max",
        ),
        ({"duration_ms_range": [1000, 30000]}, {"duration_ms_range": [3000, 90000]}, "below_min"),
        ({"duration_ms_exact": 120000}, {"duration_ms_range": [3000, 90000]}, "exceeds_max"),
        (
            {"duration_ms_exact": 120000},
            {"min_duration_ms": 3000, "max_duration_ms": 90000},
            "exceeds_max",
        ),
        ({"min_width": 200, "max_width": 400}, {"width": 300}, "exceeds_max"),
        ({"min_width": 200, "max_width": 400}, {"width": 300}, "below_min"),
        ({"height": 240}, {"height": 250}, "not_equal"),
        (
            {"duration_ms_range": [None, 120000]},
            {"duration_ms_range": [3000, 90000]},
            "exceeds_max",
        ),
        ({"duration_ms_range": [None, 30000]}, {"duration_ms_range": [3000, 90000]}, "below_min"),
        ({"duration_ms_range": [5000, None]}, {"duration_ms_range": [3000, 90000]}, "exceeds_max"),
        (
            {"duration_ms_exact": 120000, "duration_ms_range": [5000, 60000]},
            {"duration_ms_range": [3000, 90000]},
            "exceeds_max",
        ),
    ],
)
def test_range_and_exact_constraint_conflicts(params: dict, requirements: dict, kind: str) -> None:
    assert any(d.kind == kind for d in check_narrows(params, requirements))


@pytest.mark.parametrize(
    ("params", "requirements"),
    [
        ({"duration_ms_range": [3000, 90000]}, {"min_duration_ms": 3000, "max_duration_ms": 90000}),
        ({"duration_ms_exact": 5000}, {"duration_ms_range": [3000, 90000]}),
        ({"width": 300}, {"width": 300}),
        ({"min_width": 300, "max_width": 300}, {"width": 300}),
        ({"duration_ms_range": [None, 30000]}, {"duration_ms_range": [None, 90000]}),
        ({"duration_ms_range": [5000, None]}, {"duration_ms_range": [3000, None]}),
        (
            {"duration_ms_exact": 5000, "duration_ms_range": [1000, 120000]},
            {"duration_ms_range": [3000, 90000]},
        ),
    ],
)
def test_equal_or_contained_constraints_narrow(params: dict, requirements: dict) -> None:
    assert check_narrows(params, requirements) == []


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
    assert divs[0].kind == "below_min"
    assert divs[0].cap == 300
    assert divs[0].value == 100


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
    assert divs[0].field == "max_file_size_kb"
    assert divs[0].kind == "exceeds_max"
    assert divs[0].cap == 200
    assert divs[0].value == 500
    # Wire-shape projection preserves the original key vocabulary.
    assert divs[0].to_dict() == {
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
    assert divs[0].kind == "exceeds_max"
    assert divs[0].cap == 300
    assert divs[0].value == 500


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
    assert divs[0].kind == "not_equal"
    assert divs[0].cap is True
    assert divs[0].value is False
    # Wire-shape uses ``v1_value`` / ``v2_value`` for not_equal.
    assert divs[0].to_dict()["v1_value"] is True
    assert divs[0].to_dict()["v2_value"] is False


# ---------------------------------------------------------------------------
# narrowing_advisory — emits the wire-shape Error
# ---------------------------------------------------------------------------


def test_advisory_is_none_when_narrows() -> None:
    d = Format(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 100},
    )
    assert (
        narrowing_advisory(d, v1_requirements={"max_file_size_kb": 200}, v1_format_id="x") is None
    )


def test_advisory_emitted_on_divergence() -> None:
    d = Format(
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

    d = Format(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 500},
    )
    a = narrowing_advisory(d, v1_requirements=_FakeReq(), v1_format_id="x")
    assert a is not None


# ---------------------------------------------------------------------------
# Expanded narrowing field coverage (PR #845 expert review NORMATIVE fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "max_file_size_mb",
        "max_bitrate_kbps",
        "max_wrapper_depth",
        "max_cpu_load_percent",
        "max_response_time_ms",
    ],
)
def test_new_max_field_coverage(field: str) -> None:
    divs = check_narrows({field: 500}, {field: 100})
    assert any(d.field == field and d.kind == "exceeds_max" for d in divs)


def test_new_min_bitrate_field_coverage() -> None:
    divs = check_narrows({"min_bitrate_kbps": 100}, {"min_bitrate_kbps": 500})
    assert any(d.field == "min_bitrate_kbps" and d.kind == "below_min" for d in divs)


@pytest.mark.parametrize("field", ["vast_version", "daast_version"])
def test_singular_version_exact_check(field: str) -> None:
    """Singular ``vast_version`` / ``daast_version`` are exact-equal scalars
    on the canonical schemas; the plural ``vast_versions`` is the enum subset path."""
    divs = check_narrows({field: "4.2"}, {field: "3.0"})
    assert any(d.field == field and d.kind == "not_equal" for d in divs)


# ---------------------------------------------------------------------------
# bool / int subclass guard (PR #845 security review)
# ---------------------------------------------------------------------------


def test_bool_value_is_not_treated_as_numeric_for_caps() -> None:
    """``True == 1`` in Python but a seller declaring ``max_width: True``
    is malformed input — the narrowing check must NOT compare it as 1."""
    assert check_narrows({"max_width": 5}, {"max_width": True}) == []
    assert check_narrows({"max_width": True}, {"max_width": 5}) == []


# ---------------------------------------------------------------------------
# Unhashable element tolerance + echo capping (PR #845 review)
# ---------------------------------------------------------------------------


def test_subset_check_tolerates_unhashable_elements() -> None:
    v1 = {"allowed_card_media_asset_types": [{"type": "image"}, {"type": "video"}]}
    assert check_narrows({"allowed_card_media_asset_types": [{"type": "image"}]}, v1) == []
    divs = check_narrows(
        {"allowed_card_media_asset_types": [{"type": "image"}, {"type": "audio"}]},
        v1,
    )
    assert len(divs) == 1
    assert divs[0].kind == "not_subset"


def test_advisory_caps_echoed_set_length() -> None:
    big_v1 = {"image_formats": [f"fmt_{i}" for i in range(200)]}
    d = Format(
        format_kind=CanonicalFormatKind.image,
        params={"image_formats": [f"bad_{i}" for i in range(200)]},
    )
    a = narrowing_advisory(d, v1_requirements=big_v1, v1_format_id="x")
    assert a is not None
    div = a.details["divergences"][0]
    assert len(div["v1_allowed"]) == 33
    assert len(div["v2_declared"]) == 33
    assert str(div["v1_allowed"][-1]).startswith("…")
    assert str(div["v2_declared"][-1]).startswith("…")


def test_narrowing_advisory_scrubs_v1_format_id() -> None:
    d = Format(
        format_kind=CanonicalFormatKind.image,
        params={"max_file_size_kb": 500},
    )
    a = narrowing_advisory(
        d,
        v1_requirements={"max_file_size_kb": 200},
        v1_format_id="format\nFAKE LINE\nimage",
    )
    assert a is not None
    echoed = a.details["v1_format_id"]
    assert "\n" not in echoed
    assert "\\u000a" in echoed
