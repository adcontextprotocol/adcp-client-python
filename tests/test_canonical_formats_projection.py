"""v2 → v1 projection — resolution-order behaviour.

Walks every branch of the resolution order documented at
:mod:`adcp.canonical_formats.projection`. Each test maps to a numbered
step in that contract so a future refactor that breaks one step shows
up here with a clear pointer to the spec rule it violated.
"""

from __future__ import annotations

import pytest

from adcp.canonical_formats import (
    V1_TRANSLATABLE,
    project_declaration_to_v1,
    project_product_to_v1,
)
from adcp.canonical_formats.advisory import SDK_ID
from adcp.types import CanonicalFormatKind, FormatId, ProductFormatDeclaration


def _ref(id_: str = "display_300x250_image") -> FormatId:
    return FormatId(
        agent_url="https://creative.adcontextprotocol.org",
        id=id_,
    )


# ---------------------------------------------------------------------------
# Step 1 — explicit v1-unreachability is silent (no refs, no advisories)
# ---------------------------------------------------------------------------


def test_canonical_formats_only_emits_no_refs_and_no_advisory() -> None:
    """Step 1: when the seller has opted out of v1 projection, project to nothing.

    ``canonical_formats_only=True`` is mutually exclusive with
    ``v1_format_ref[]`` at construction (enforced by the hand-rolled
    declaration model), so the fixture here cannot also carry refs.
    """
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"sizes": [{"width": 300, "height": 250}]},
        canonical_formats_only=True,
    )

    result = project_declaration_to_v1(decl)

    assert result.format_ids == []
    assert result.advisories == []


def test_custom_without_refs_is_silent() -> None:
    """``custom`` is in the not-v1-translatable set; without seller refs → silent."""
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.custom,
        params={},
        format_shape="multi_placement_takeover",
    )

    result = project_declaration_to_v1(decl)

    assert result.format_ids == []
    assert result.advisories == []


def test_custom_with_v1_format_ref_emits_refs() -> None:
    """``custom`` MAY carry seller-asserted v1 refs; step 2 flow applies."""
    refs = [_ref("acme_homepage_takeover")]
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.custom,
        params={},
        format_shape="multi_placement_takeover",
        v1_format_ref=refs,
    )

    result = project_declaration_to_v1(decl)

    assert result.format_ids == refs
    assert result.advisories == []


# ---------------------------------------------------------------------------
# Step 2 — v1_format_ref present → emit, check multi-size fan-out
# ---------------------------------------------------------------------------


def test_seller_asserted_v1_ref_emits_refs_with_no_advisory() -> None:
    refs = [_ref("display_300x250_image"), _ref("display_728x90_image")]
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"sizes": [{"width": 300, "height": 250}, {"width": 728, "height": 90}]},
        v1_format_ref=refs,
    )

    result = project_declaration_to_v1(decl)

    assert result.format_ids == refs
    assert result.advisories == []


def test_multi_size_lossy_fan_out_emits_lossy_advisory() -> None:
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={
            "sizes": [
                {"width": 300, "height": 250},
                {"width": 728, "height": 90},
                {"width": 970, "height": 250},
            ],
        },
        v1_format_ref=[_ref()],
    )

    result = project_declaration_to_v1(decl, field_path="products[0].format_options[2]")

    assert len(result.format_ids) == 1  # the partial coverage still ships
    assert len(result.advisories) == 1
    advisory = result.advisories[0]
    assert advisory.code == "FORMAT_DECLARATION_V1_LOSSY_MULTI_SIZE"
    assert advisory.source.value == "sdk"
    assert advisory.sdk_id == SDK_ID
    assert advisory.field == "products[0].format_options[2]"
    assert advisory.details == {
        "format_kind": "image",
        "v1_format_ref_count": 1,
        "sizes_count": 3,
    }


def test_single_size_with_single_ref_emits_no_lossy_advisory() -> None:
    """1 ref for 1 size is not lossy; ref-for-no-sizes is also not lossy."""
    decl = ProductFormatDeclaration(
        format_kind=CanonicalFormatKind.image,
        params={"sizes": [{"width": 300, "height": 250}]},
        v1_format_ref=[_ref()],
    )

    result = project_declaration_to_v1(decl)

    assert len(result.format_ids) == 1
    assert result.advisories == []


# ---------------------------------------------------------------------------
# Step 3 — canonical with v1_translatable=False → silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        CanonicalFormatKind.agent_placement,
        CanonicalFormatKind.sponsored_placement,
        CanonicalFormatKind.responsive_creative,
        CanonicalFormatKind.image_carousel,
        CanonicalFormatKind.custom,
    ],
)
def test_non_translatable_canonicals_are_silent_with_no_ref(kind: CanonicalFormatKind) -> None:
    """Per the registry's "Direction of truth" — these canonicals never have
    a v1 form regardless of registry coverage; surfacing AMBIGUOUS would
    spam the wire."""
    decl = ProductFormatDeclaration(format_kind=kind, params={})

    result = project_declaration_to_v1(decl)

    assert result.format_ids == []
    assert result.advisories == []


def test_v1_translatable_table_matches_kind_enum() -> None:
    """The V1_TRANSLATABLE map must cover every CanonicalFormatKind value;
    a new kind added upstream without a table entry would default to True
    and emit AMBIGUOUS for a structurally v1-unreachable canonical."""
    missing = [k for k in CanonicalFormatKind if k not in V1_TRANSLATABLE]
    assert not missing, f"V1_TRANSLATABLE missing entries: {missing}"


# ---------------------------------------------------------------------------
# Step 4 — translatable canonical, no v1_format_ref → AMBIGUOUS advisory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        CanonicalFormatKind.image,
        CanonicalFormatKind.html5,
        CanonicalFormatKind.display_tag,
        CanonicalFormatKind.video_hosted,
        CanonicalFormatKind.video_vast,
        CanonicalFormatKind.audio_hosted,
        CanonicalFormatKind.audio_daast,
        CanonicalFormatKind.native_in_feed,
    ],
)
def test_translatable_canonical_without_v1_ref_emits_ambiguous(
    kind: CanonicalFormatKind,
) -> None:
    decl = ProductFormatDeclaration(format_kind=kind, params={})

    result = project_declaration_to_v1(decl, product_id="prod_xyz")

    assert result.format_ids == []
    assert len(result.advisories) == 1
    advisory = result.advisories[0]
    assert advisory.code == "FORMAT_DECLARATION_V1_AMBIGUOUS"
    assert advisory.source.value == "sdk"
    assert advisory.sdk_id == SDK_ID
    assert advisory.details["format_kind"] == kind.value
    assert advisory.details["product_id"] == "prod_xyz"
    assert advisory.details["reason"] == "no_v1_format_ref"


# ---------------------------------------------------------------------------
# project_product_to_v1 — fan-out across format_options[]
# ---------------------------------------------------------------------------


class _StubProduct:
    """Duck-typed stand-in — projection helper reads ``format_options`` and ``product_id``."""

    def __init__(
        self,
        format_options: list[ProductFormatDeclaration],
        product_id: str | None = None,
    ) -> None:
        self.format_options = format_options
        self.product_id = product_id


def test_project_product_aggregates_per_declaration_results() -> None:
    product = _StubProduct(
        format_options=[
            ProductFormatDeclaration(
                format_kind=CanonicalFormatKind.image,
                params={},
                v1_format_ref=[_ref("display_300x250_image")],
            ),
            ProductFormatDeclaration(
                format_kind=CanonicalFormatKind.video_vast,
                params={},
            ),
            ProductFormatDeclaration(
                format_kind=CanonicalFormatKind.agent_placement,
                params={},
            ),
        ],
        product_id="prod_alpha",
    )

    result = project_product_to_v1(product, product_index=0)

    # 1 ref from the first declaration, 0 from the others.
    assert len(result.format_ids) == 1
    # 1 AMBIGUOUS advisory from the video_vast declaration; agent_placement silent.
    assert len(result.advisories) == 1
    assert result.advisories[0].code == "FORMAT_DECLARATION_V1_AMBIGUOUS"
    # Field path is indexed against the parent product when product_index given.
    assert result.advisories[0].field == "products[0].format_options[1]"
    # Advisory carries product_id for downstream correlation.
    assert result.advisories[0].details["product_id"] == "prod_alpha"


def test_project_product_without_product_index_omits_products_prefix() -> None:
    product = _StubProduct(
        format_options=[
            ProductFormatDeclaration(format_kind=CanonicalFormatKind.video_vast, params={}),
        ],
    )

    result = project_product_to_v1(product)

    assert result.advisories[0].field == "format_options[0]"


def test_project_product_handles_missing_format_options() -> None:
    """A product with no format_options[] (3.0-era product) projects to empty
    refs / advisories — no AttributeError, no spurious advisory."""

    class _BareProduct:
        product_id = "prod_bare"

    result = project_product_to_v1(_BareProduct())
    assert result.format_ids == []
    assert result.advisories == []


def test_advisory_product_id_is_truncated() -> None:
    """Seller-controlled identifiers are capped before echoing into advisory details.

    Mitigates log-injection / response-spoofing via multi-hop ``errors[]``.
    """
    long_id = "x" * 300
    decl = ProductFormatDeclaration(format_kind=CanonicalFormatKind.video_vast, params={})

    result = project_declaration_to_v1(decl, product_id=long_id)

    echoed = result.advisories[0].details["product_id"]
    assert echoed != long_id, "long product_id must not echo verbatim"
    assert echoed.endswith("…[truncated]")
    # The literal cap (128) plus the truncation marker.
    assert echoed.startswith("x" * 128)


def test_advisory_product_id_scrubs_newlines() -> None:
    """Newline injection attempts are escaped so operator log emitters
    don't see forged lines from seller-controlled identifiers."""
    decl = ProductFormatDeclaration(format_kind=CanonicalFormatKind.video_vast, params={})

    result = project_declaration_to_v1(decl, product_id="prod_alpha\nFAKE LOG LINE\nprod_omega")

    echoed = result.advisories[0].details["product_id"]
    assert "\n" not in echoed
    assert "\\u000a" in echoed


def test_advisory_product_id_scrubs_ansi_escape() -> None:
    """ANSI ``\\x1b[`` escape sequences are neutralised before echoing."""
    decl = ProductFormatDeclaration(format_kind=CanonicalFormatKind.video_vast, params={})

    result = project_declaration_to_v1(decl, product_id="prod\x1b[31mRED\x1b[0m")

    echoed = result.advisories[0].details["product_id"]
    assert "\x1b" not in echoed
    assert "\\u001b" in echoed


def test_advisory_product_id_scrubs_unicode_line_separator() -> None:
    """Unicode LS/PS line separators escape too — they break naive line splitters."""
    decl = ProductFormatDeclaration(format_kind=CanonicalFormatKind.video_vast, params={})

    result = project_declaration_to_v1(decl, product_id="prod injected")

    echoed = result.advisories[0].details["product_id"]
    assert " " not in echoed
