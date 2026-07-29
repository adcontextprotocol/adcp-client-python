"""Round-trip projection tests against the upstream reference fixtures.

Vendored from ``adcontextprotocol/adcp`` at
``static/examples/products/canonical/`` (14 fixtures) and
``server/src/creative-agent/reference-formats.json`` (50 v1 catalog
entries).

Two round-trip shapes are exercised:

1. **v2 → v1 → declaration-by-id** — Each v2 product fixture's
   ``format_options[]`` carries seller-asserted ``v1_format_ref[]``.
   Walking the product through :func:`project_product_to_v1` produces
   ``format_ids[]``. Each emitted id MUST round-trip back to the
   originating declaration via :func:`find_declaration_by_v1_format_id`.

2. **v1 catalog → v2 declarations** — The vendored
   ``v1-reference-formats.json`` has all 50 entries carrying explicit
   ``canonical:`` annotations. :func:`project_v1_catalog_to_v2` MUST
   project every entry with zero advisories (seller-asserted path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adcp.canonical_formats import (
    find_declaration_by_v1_format_id,
    project_product_to_v1,
    project_v1_catalog_to_v2,
)
from adcp.types import (
    CanonicalFormatKind,
    ProductFormatDeclaration,
)
from adcp.types.legacy import LegacyFormatId as FormatId

_FIXTURES = Path(__file__).parent / "fixtures" / "canonical"

# The 14 v2 product fixtures vendored from
# adcontextprotocol/adcp@main/static/examples/products/canonical/.
_PRODUCT_FIXTURES: tuple[str, ...] = (
    "amazon_sponsored_products.json",
    "chatgpt_brand_mention.json",
    "gam_3p_display_tag.json",
    "google_performance_max.json",
    "meta_carousel.json",
    "meta_reels_us.json",
    "nytimes_homepage_html5.json",
    "nytimes_homepage_mrec.json",
    "nytimes_homepage_takeover_custom.json",
    "taboola_content_recommendation.json",
    "the_daily_30s_host_read.json",
    "triton_daast_audio_30s.json",
    "veo_generative_video_15s.json",
    "youtube_vast_preroll.json",
)


def _load_product(name: str) -> dict[str, Any]:
    """Load a vendored v2 product fixture as a raw dict.

    Kept as a dict (rather than ``Product.model_validate``) because the
    fixtures occasionally carry fields the generated Pydantic ``Product``
    rejects strictly (the fixtures are demo data, not always
    schema-conformant on every optional field). The projection helpers
    duck-type on ``format_options`` + ``product_id`` so the round-trip
    works directly on dicts.
    """
    return json.loads((_FIXTURES / name).read_text())


def _load_declarations(raw_product: dict[str, Any]) -> list[ProductFormatDeclaration]:
    """Pull the typed ``format_options[]`` out of a raw-dict product."""
    return [
        ProductFormatDeclaration.model_validate(opt)
        for opt in raw_product.get("format_options", [])
    ]


class _DuckProduct:
    """Duck-typed product wrapper for :func:`project_product_to_v1`."""

    def __init__(self, raw: dict[str, Any], declarations: list[ProductFormatDeclaration]) -> None:
        self.product_id = raw.get("product_id")
        self.format_options = declarations


# ---------------------------------------------------------------------------
# v2 product fixtures — round-trip via v1 inbound lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", _PRODUCT_FIXTURES)
def test_v2_product_v1_outbound_round_trip(fixture_name: str) -> None:
    """For each v2 product fixture:

    * Run :func:`project_product_to_v1` to emit the v1 ``format_ids[]``.
    * The emitted list MUST be the union of every declaration's
      ``v1_format_ref[]`` (preserving order).
    * Every emitted ``format_id`` MUST resolve back to a declaration via
      :func:`find_declaration_by_v1_format_id`.
    * Any advisories emitted MUST be advisories the resolution order
      legitimately produces (``LOSSY_MULTI_SIZE`` /
      ``DECLARATION_V1_AMBIGUOUS``) — no ``FORMAT_PROJECTION_FAILED``
      and no codes outside the canonical set.
    """
    raw = _load_product(fixture_name)
    declarations = _load_declarations(raw)
    if not declarations:
        pytest.skip("fixture has empty format_options")
    product = _DuckProduct(raw, declarations)

    result = project_product_to_v1(product, product_index=0)

    # Outbound refs = union of seller-asserted refs across declarations.
    expected_refs: list[FormatId] = []
    for d in declarations:
        if d.canonical_formats_only:
            continue
        if d.legacy_format_refs:
            expected_refs.extend(d.legacy_format_refs)
    assert result.format_ids == expected_refs, (
        f"{fixture_name}: outbound format_ids didn't equal the union of "
        f"declaration v1_format_ref[]"
    )

    # Every emitted ref MUST round-trip back to a declaration.
    for ref in result.format_ids:
        found = find_declaration_by_v1_format_id(ref, declarations)
        assert found is not None, (
            f"{fixture_name}: emitted format_id {ref.id!r} did not "
            f"resolve back to any declaration via find_declaration_by_v1_format_id"
        )

    # Advisories MUST be from the canonical set.
    allowed_codes = {
        "FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE",
        "FORMAT_DECLARATION_V1_AMBIGUOUS",
    }
    for a in result.advisories:
        assert a.code in allowed_codes, f"{fixture_name}: unexpected advisory code {a.code!r}"


@pytest.mark.parametrize("fixture_name", _PRODUCT_FIXTURES)
def test_v2_product_declarations_are_constructable(fixture_name: str) -> None:
    """Every v2 product fixture's ``format_options[]`` MUST be parseable as
    typed :class:`ProductFormatDeclaration` instances.

    A regression on the hand-rolled declaration model (e.g., a new
    required field, a tightening of the credential-shaped key guard)
    would show up here against real-world seller catalogs.
    """
    raw = _load_product(fixture_name)
    declarations = _load_declarations(raw)
    # At least one declaration per product (otherwise the fixture is degenerate).
    assert declarations
    # Every declaration carries a valid kind from the canonical enum.
    for d in declarations:
        assert isinstance(d.format_kind, CanonicalFormatKind)


# ---------------------------------------------------------------------------
# v1 reference catalog — projection round-trip
# ---------------------------------------------------------------------------


def test_v1_reference_catalog_projects_cleanly() -> None:
    """Every v1 catalog entry MUST project via the seller-asserted
    ``canonical:`` annotation path (resolution-order step 1), with
    zero advisories — the catalog is the canonical reference and a
    drift here means upstream changed the contract."""
    v1 = json.loads((_FIXTURES / "v1-reference-formats.json").read_text())
    result = project_v1_catalog_to_v2(v1)
    assert len(result.declarations) == len(v1)
    assert result.advisories == []


def test_v1_reference_catalog_projection_round_trips_format_ids() -> None:
    """For each projected v1 catalog entry, the resulting v2 declaration's
    ``v1_format_ref[0]`` MUST equal the source v1 ``format_id`` (the
    projection threads the source ref back into the declaration so
    v2→v1 lookup on the produced declaration finds the source format)."""
    v1 = json.loads((_FIXTURES / "v1-reference-formats.json").read_text())
    result = project_v1_catalog_to_v2(v1)
    for source, declaration in zip(v1, result.declarations):
        assert len(declaration.legacy_format_refs) == 1
        ref = declaration.legacy_format_refs[0]
        # Compare on (agent_url, id) — Pydantic AnyUrl may add a trailing
        # slash so compare the path-stripped + id form.
        assert ref.id == source["format_id"]["id"]


def test_v1_reference_catalog_covers_eight_canonical_kinds() -> None:
    """Pin the catalog's canonical-kind coverage so a regression that drops
    a kind from the reference catalog (or upstream's tagging mistake)
    surfaces immediately."""
    v1 = json.loads((_FIXTURES / "v1-reference-formats.json").read_text())
    result = project_v1_catalog_to_v2(v1)
    kinds = {d.format_kind for d in result.declarations}
    expected = {
        CanonicalFormatKind.image,
        CanonicalFormatKind.html5,
        CanonicalFormatKind.display_tag,
        CanonicalFormatKind.video_hosted,
        CanonicalFormatKind.video_vast,
        CanonicalFormatKind.audio_hosted,
        CanonicalFormatKind.sponsored_placement,
        CanonicalFormatKind.agent_placement,
    }
    assert kinds == expected
