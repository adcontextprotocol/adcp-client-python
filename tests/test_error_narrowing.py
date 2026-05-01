"""narrow_union_errors — discriminated-union ValidationError narrowing.

Stability AI Emma backend test (verdict 5/10) flagged that constructing
a ``CreativeManifest`` whose asset is missing a required field (e.g.
``ImageAsset.width``) produced a 60-line pydantic ValidationError
listing every variant of the asset content union (13+ variants). The
user's actual mistake (one missing field) was buried.

This file pins the post-fix behavior:

* End-to-end: the framework's typed-dispatch path runs the
  ``CreativeManifest`` construction through ``narrow_union_errors``
  and surfaces only the variant the user matched.
* Algorithm-level: ``narrow_union_errors`` correctly identifies the
  discriminator-matched variant, the fewest-error fallback, and the
  pass-through case for non-union errors.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.types.error_narrowing import narrow_union_errors

# ---- Pass-through cases ----


def test_pass_through_empty_errors() -> None:
    """Empty input → empty output. No surprises on no-op input."""
    assert narrow_union_errors([]) == []


def test_pass_through_non_union_errors() -> None:
    """Errors with no variant in their loc (typical scalar field
    validation) pass through unchanged."""
    errors: list[dict[str, Any]] = [
        {"type": "missing", "loc": ("brief",), "msg": "Field required"},
        {"type": "string_type", "loc": ("po_number",), "msg": "Input should be a valid string"},
    ]
    assert narrow_union_errors(errors) == errors


# ---- Variant detection ----


def test_narrow_picks_discriminator_matched_variant() -> None:
    """When one variant has only field-level errors and the others
    have ``literal_error`` on what looks like the discriminator,
    keep ONLY the matched variant's errors. This is the
    Stability/AudioStack 60-line-dump fix."""
    errors: list[dict[str, Any]] = [
        # Matched variant — missing fields, no literal_error.
        {
            "type": "missing",
            "loc": ("assets", "hero", "ImageAsset", "width"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "ImageAsset", "height"),
            "msg": "Field required",
        },
        # Non-matching variants — discriminator-only failure.
        {
            "type": "literal_error",
            "loc": ("assets", "hero", "VideoAsset", "asset_type"),
            "msg": "Input should be 'video'",
        },
        {
            "type": "literal_error",
            "loc": ("assets", "hero", "AudioAsset", "asset_type"),
            "msg": "Input should be 'audio'",
        },
    ]
    narrowed = narrow_union_errors(errors)
    assert len(narrowed) == 2, f"Expected 2 ImageAsset errors, got {len(narrowed)}: {narrowed}"
    assert all("ImageAsset" in err["loc"] for err in narrowed)


def test_narrow_picks_fewest_errors_when_no_discriminator_winner() -> None:
    """When NO variant has a clean discriminator match (e.g. the user
    provided an invalid discriminator value, so every variant has a
    ``literal_error`` on the discriminator field), pick the variant
    with the fewest non-discriminator errors as the closest-fit
    guess."""
    errors: list[dict[str, Any]] = [
        # ImageAsset has 2 missing fields + literal mismatch.
        {
            "type": "missing",
            "loc": ("assets", "hero", "ImageAsset", "width"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "ImageAsset", "height"),
            "msg": "Field required",
        },
        {
            "type": "literal_error",
            "loc": ("assets", "hero", "ImageAsset", "asset_type"),
            "msg": "Input should be 'image'",
        },
        # VideoAsset has 4 missing fields + literal mismatch.
        {
            "type": "missing",
            "loc": ("assets", "hero", "VideoAsset", "width"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "VideoAsset", "height"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "VideoAsset", "duration_ms"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "VideoAsset", "codec"),
            "msg": "Field required",
        },
        {
            "type": "literal_error",
            "loc": ("assets", "hero", "VideoAsset", "asset_type"),
            "msg": "Input should be 'video'",
        },
    ]
    narrowed = narrow_union_errors(errors)
    # Both variants have literal_error → no discriminator winner →
    # fewest-non-literal-errors heuristic. ImageAsset: 2 non-literal,
    # VideoAsset: 4 non-literal. ImageAsset wins.
    image_errors = [e for e in narrowed if "ImageAsset" in e["loc"]]
    video_errors = [e for e in narrowed if "VideoAsset" in e["loc"]]
    assert image_errors and not video_errors


def test_narrow_falls_back_when_variants_tie() -> None:
    """When multiple variants tie on error count, the function returns
    all errors rather than guessing — surfaces the ambiguity to the
    adopter who can disambiguate via the discriminator."""
    errors: list[dict[str, Any]] = [
        {
            "type": "missing",
            "loc": ("assets", "hero", "ImageAsset", "width"),
            "msg": "Field required",
        },
        {
            "type": "missing",
            "loc": ("assets", "hero", "VideoAsset", "duration_ms"),
            "msg": "Field required",
        },
    ]
    # Both variants have 1 non-literal error. Tie → keep both.
    narrowed = narrow_union_errors(errors)
    assert len(narrowed) == 2


# ---- End-to-end: CreativeManifest with missing field ----


def test_e2e_creative_manifest_missing_width_height_narrows_to_image_asset() -> None:
    """End-to-end regression for the exact Stability AI report case.

    Before the narrowing: 26 ValidationErrors covering every variant
    of the asset content union. After: just the 2 ImageAsset.width /
    ImageAsset.height errors the adopter cares about."""
    from pydantic import ValidationError

    from adcp.types import CreativeManifest, FormatReferenceStructuredObject

    try:
        CreativeManifest(
            creative_id="cr-1",
            format_id=FormatReferenceStructuredObject(agent_url="https://x", id="img"),
            assets={
                "hero": {
                    "asset_role": "hero",
                    "asset_type": "image",
                    "url": "https://x.png",
                }
            },
        )
    except ValidationError as exc:
        narrowed = narrow_union_errors(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
        # Should be ~2 errors (ImageAsset's width + height), not ~26.
        assert (
            len(narrowed) <= 4
        ), f"narrow_union_errors didn't narrow: {len(narrowed)} errors remain"
        assert all("ImageAsset" in err["loc"] for err in narrowed), (
            "narrowed result should be ImageAsset-only; got: " f"{[err['loc'] for err in narrowed]}"
        )
        missing_fields = {err["loc"][-1] for err in narrowed if err["type"] == "missing"}
        assert "width" in missing_fields and "height" in missing_fields
    else:
        pytest.fail(
            "CreativeManifest accepted invalid asset (missing width/height); "
            "regression in upstream pydantic discriminated-union behavior"
        )
