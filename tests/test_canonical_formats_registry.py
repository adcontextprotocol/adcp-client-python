"""v1↔v2 mapping registry — loader + glob/structural matchers."""

from __future__ import annotations

import pytest

from adcp.canonical_formats import (
    glob_match,
    load_default_registry,
    structural_match,
)
from adcp.types import V1V2CanonicalFormatMappingRegistry

# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


def test_loader_returns_typed_registry() -> None:
    registry = load_default_registry()
    assert isinstance(registry, V1V2CanonicalFormatMappingRegistry)
    assert registry.version  # semver string
    assert registry.mappings  # non-empty


def test_loader_is_cached() -> None:
    """Cached so repeated lookups don't re-read + re-parse the JSON."""
    a = load_default_registry()
    b = load_default_registry()
    assert a is b


def test_initial_registry_has_seven_pure_structural_entries() -> None:
    """3.1 ships 7 pure-structural fallback entries per the registry docstring.
    A change to this count is a vocabulary-governance event."""
    registry = load_default_registry()
    assert len(registry.mappings) == 7
    # Every initial entry is structural — no literal globs as of 3.1.
    for mapping in registry.mappings:
        # ``v1_pattern`` is the discriminated union; the structural branch
        # exposes ``.structural``, the glob branch exposes ``.format_id_glob``.
        assert hasattr(
            mapping.v1_pattern, "structural"
        ), f"3.1 baseline expected pure-structural; got {mapping.v1_pattern!r}"


# ---------------------------------------------------------------------------
# glob_match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,value,expected",
    [
        ("*", "anything", True),
        ("iab_mrec_300x250", "iab_mrec_300x250", True),
        ("iab_mrec_300x250", "iab_mrec_728x90", False),
        ("iab_leaderboard_*", "iab_leaderboard_728x90", True),
        ("iab_leaderboard_*", "iab_mrec_300x250", False),
        ("meta_*_reels", "meta_video_reels", True),
        ("meta_*_reels", "meta_image_reels", True),
        ("meta_*_reels", "meta_reels", False),
    ],
)
def test_glob_match_handles_wildcards(pattern: str, value: str, expected: bool) -> None:
    assert glob_match(value, pattern) is expected


def test_glob_match_treats_regex_metachars_as_literal() -> None:
    """Pattern language is glob, not regex — ``.`` matches a literal dot."""
    assert glob_match("display.300", "display.300") is True
    assert glob_match("displayX300", "display.300") is False


# ---------------------------------------------------------------------------
# structural_match
# ---------------------------------------------------------------------------


def test_structural_match_vast_42_against_vast_4_plus_pattern() -> None:
    registry = load_default_registry()
    # Entry 0 in the test fixture is the VAST ≥4.0 entry.
    pattern = registry.mappings[0].v1_pattern.structural

    assert structural_match(
        asset_types=["vast"],
        vast_versions=["4.2"],
        pattern=pattern,
    )


def test_structural_match_vast_30_misses_vast_4_plus_pattern() -> None:
    registry = load_default_registry()
    pattern = registry.mappings[0].v1_pattern.structural

    assert not structural_match(
        asset_types=["vast"],
        vast_versions=["3.0"],
        pattern=pattern,
    )


def test_structural_match_vast_30_hits_legacy_pattern() -> None:
    registry = load_default_registry()
    # Entry 1 is the legacy VAST 3.x / 2.x entry.
    pattern = registry.mappings[1].v1_pattern.structural

    assert structural_match(
        asset_types=["vast"],
        vast_versions=["3.0"],
        pattern=pattern,
    )
    assert structural_match(
        asset_types=["vast"],
        vast_versions=["2.0"],
        pattern=pattern,
    )


def test_structural_match_misses_when_asset_type_absent() -> None:
    registry = load_default_registry()
    # Entry 3 is the zip → html5 entry.
    pattern = registry.mappings[3].v1_pattern.structural

    assert not structural_match(asset_types=["url"], pattern=pattern)
    assert structural_match(asset_types=["zip"], pattern=pattern)


def test_structural_match_with_extra_asset_types_still_matches() -> None:
    """The pattern's asset_types is a *subset* requirement — adding more
    asset_types in the v1 format doesn't disqualify the match."""
    registry = load_default_registry()
    # Entry 4 is the video → video_hosted entry.
    pattern = registry.mappings[4].v1_pattern.structural

    assert structural_match(asset_types=["video", "url"], pattern=pattern)


def test_structural_match_empty_pattern_matches_anything() -> None:
    """An empty pattern declares no constraints; everything matches."""
    assert structural_match(asset_types=[], pattern=None)
    assert structural_match(asset_types=["whatever"], pattern={})
