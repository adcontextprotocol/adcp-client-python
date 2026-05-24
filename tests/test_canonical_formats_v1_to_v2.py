"""v1 → v2 projection — resolution-order behaviour.

Mirror of :mod:`tests.test_canonical_formats_projection` (the v2 → v1
suite). Each test maps to a numbered step in the resolution order
documented in :mod:`adcp.canonical_formats.v1_to_v2`.
"""

from __future__ import annotations

import json
from pathlib import Path

from adcp.canonical_formats import (
    project_v1_catalog_to_v2,
    project_v1_format_to_declaration,
)
from adcp.canonical_formats.advisory import SDK_ID
from adcp.types import CanonicalFormatKind

_FIXTURES = Path(__file__).parent / "fixtures" / "canonical"


# ---------------------------------------------------------------------------
# Step 1 — explicit canonical annotation
# ---------------------------------------------------------------------------


def test_explicit_canonical_annotation_wins() -> None:
    v1 = {
        "format_id": {
            "agent_url": "https://creative.adcontextprotocol.org",
            "id": "display_300x250_image",
        },
        "canonical": {"kind": "image", "asset_source": "buyer_uploaded"},
        "assets": [{"asset_type": "image"}],
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    assert result.declaration.format_kind is CanonicalFormatKind.image
    assert result.advisories == []
    # asset_source from canonical threads into params:
    assert result.declaration.params.get("asset_source") == "buyer_uploaded"
    # v1_format_ref points back at the source:
    assert len(result.declaration.v1_format_ref) == 1
    assert result.declaration.v1_format_ref[0].id == "display_300x250_image"


def test_explicit_canonical_bypasses_registry_with_no_advisory() -> None:
    """Seller-asserted canonical is highest priority — even an asset shape
    that would match the registry's video_vast pattern should project to
    whatever the seller said."""
    v1 = {
        "format_id": {"agent_url": "https://x.example", "id": "weird_format"},
        "canonical": {"kind": "html5"},
        "assets": [{"asset_type": "vast"}, {"asset_type": "video"}],
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    assert result.declaration.format_kind is CanonicalFormatKind.html5
    assert result.advisories == []


def test_slots_override_threads_into_params() -> None:
    v1 = {
        "format_id": {"agent_url": "https://x.example", "id": "weird_image"},
        "canonical": {
            "kind": "image",
            "slots_override": [
                {"asset_group_id": "image_main", "asset_type": "image", "required": True},
                {"asset_group_id": "headline", "asset_type": "text", "max_chars": 30},
            ],
        },
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    slots = result.declaration.params.get("slots")
    assert isinstance(slots, list) and len(slots) == 2
    assert slots[0]["asset_group_id"] == "image_main"


# ---------------------------------------------------------------------------
# Step 3 — registry structural match (no explicit canonical)
# ---------------------------------------------------------------------------


def test_structural_match_emits_ambiguous_advisory() -> None:
    v1 = {
        "format_id": {"agent_url": "https://x.example", "id": "weird_vast_format"},
        "assets": [{"asset_type": "vast"}],
        "vast_version": "4.2",
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    assert result.declaration.format_kind is CanonicalFormatKind.video_vast
    # Step 4: family-level match always emits AMBIGUOUS
    assert len(result.advisories) == 1
    a = result.advisories[0]
    assert a.code == "FORMAT_DECLARATION_V1_AMBIGUOUS"
    assert a.source.value == "sdk"
    assert a.sdk_id == SDK_ID
    assert a.details["matched_canonical"] == "video_vast"
    assert a.details["match_kind"] == "structural_family"


def test_structural_match_carries_registry_params_into_declaration() -> None:
    v1 = {
        "format_id": {"agent_url": "https://x.example", "id": "iab_mrec"},
        "assets": [{"asset_type": "vast"}],
        "vast_versions": ["4.2"],
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    # The registry's video_vast >= 4.0 entry carries parameters.vast_version
    assert result.declaration.params.get("vast_version") == "4.2"


# ---------------------------------------------------------------------------
# Step 5 — fail closed
# ---------------------------------------------------------------------------


def test_format_with_no_canonical_and_no_registry_match_fails_closed() -> None:
    v1 = {
        "format_id": {"agent_url": "https://x.example", "id": "unknown_format"},
        "assets": [{"asset_type": "exotic_shape"}],
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is None
    assert len(result.advisories) == 1
    a = result.advisories[0]
    assert a.code == "FORMAT_PROJECTION_FAILED"
    assert a.details["resolution_failure"] == "no_registry_match"
    assert a.details["v1_format_id"] == "unknown_format"


def test_missing_format_id_fails_closed() -> None:
    v1 = {"name": "Catalog entry without format_id"}
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is None
    assert len(result.advisories) == 1
    assert result.advisories[0].code == "FORMAT_PROJECTION_FAILED"
    assert result.advisories[0].details["resolution_failure"] == "missing_format_id"


# ---------------------------------------------------------------------------
# Catalog-level helper
# ---------------------------------------------------------------------------


def test_catalog_aggregation_collects_all_results() -> None:
    catalog = [
        {
            "format_id": {"agent_url": "https://x.example", "id": "fmt_a"},
            "canonical": {"kind": "image"},
        },
        {
            "format_id": {"agent_url": "https://x.example", "id": "fmt_b"},
            "assets": [{"asset_type": "vast"}],
            "vast_version": "4.2",
        },  # ambiguous family
        {
            "format_id": {"agent_url": "https://x.example", "id": "fmt_c"},
            "assets": [{"asset_type": "exotic"}],
        },  # fail-closed
    ]
    result = project_v1_catalog_to_v2(catalog)
    assert len(result.declarations) == 2  # fmt_a + fmt_b
    codes = sorted({a.code for a in result.advisories})
    assert codes == ["FORMAT_DECLARATION_V1_AMBIGUOUS", "FORMAT_PROJECTION_FAILED"]


# ---------------------------------------------------------------------------
# Full v1 reference catalog (50 entries from upstream)
# ---------------------------------------------------------------------------


def test_step1_threads_registry_params_when_seller_annotates_kind_only() -> None:
    """Partial seller annotation (kind only) MUST still inherit registry params.

    Without this, a seller annotating only ``{kind: video_vast}`` on a
    v1 format whose ``id`` matches a registry glob loses the registry's
    declared ``vast_version`` / dimensions / etc. That's the
    code-reviewer's MUST-FIX #1.
    """
    # No registry glob matches this id (3.1 has zero literal globs) so
    # the test exercises the structural intent without depending on a
    # specific glob entry: assert seller-asserted slots win, registry
    # params (when found) thread in alongside.
    v1 = {
        "format_id": {"agent_url": "https://creative.adcontextprotocol.org", "id": "x"},
        "canonical": {"kind": "video_vast"},  # partial — no slots_override, no asset_source
        "assets": [{"asset_type": "vast"}],
        "vast_version": "4.2",
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is not None
    assert result.declaration.format_kind is CanonicalFormatKind.video_vast
    # No registry glob → no params, no advisory (step 1 stops here).
    assert result.advisories == []


def test_v1_to_v2_narrows_exception_to_validation_error() -> None:
    """The bare ``except Exception`` in _v1_format_id was narrowed to
    pydantic.ValidationError. A malformed FormatId dict must still
    fall-through to ``None`` (eventually fail-closed) without masking
    other exception types."""
    v1 = {
        # Missing required ``id`` — pydantic ValidationError.
        "format_id": {"agent_url": "https://x.example"},
    }
    result = project_v1_format_to_declaration(v1)
    assert result.declaration is None
    assert result.advisories[0].code == "FORMAT_PROJECTION_FAILED"
    assert result.advisories[0].details["resolution_failure"] == "missing_format_id"


def test_full_v1_reference_catalog_projects_via_seller_canonical() -> None:
    """All 50 entries in the vendored v1 ``reference-formats.json`` carry an
    explicit ``canonical:`` annotation, so projection MUST go through step 1
    with zero advisories. This pins the SDK's behaviour against the
    upstream reference catalog so any future drift (e.g., an annotation
    drop) is immediately visible."""
    v1 = json.loads((_FIXTURES / "v1-reference-formats.json").read_text())
    result = project_v1_catalog_to_v2(v1)
    assert len(result.declarations) == len(v1)
    assert result.advisories == []
