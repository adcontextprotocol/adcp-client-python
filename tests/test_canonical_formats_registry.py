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


def test_loader_returns_equal_content_each_call() -> None:
    """Repeated loads return semantically-equal copies (cache is an internal
    detail; multi-tenant isolation requires fresh instances)."""
    a = load_default_registry()
    b = load_default_registry()
    assert a == b


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


# ---------------------------------------------------------------------------
# Registry cache isolation + load-error wrapping
# ---------------------------------------------------------------------------


def test_loader_returns_fresh_deep_copy_per_call() -> None:
    """Multi-tenant callers must not be able to poison each other's registry view."""
    a = load_default_registry()
    b = load_default_registry()
    assert a is not b
    assert a.mappings is not b.mappings
    assert a.mappings[0] is not b.mappings[0]


def test_loader_caller_mutation_does_not_poison_subsequent_loads() -> None:
    a = load_default_registry()
    original_count = len(a.mappings)
    a.mappings.clear()

    b = load_default_registry()
    assert len(b.mappings) == original_count


def test_registry_load_error_is_raised_on_malformed_bundle(monkeypatch) -> None:
    """Wrap JSONDecodeError with a contextual ``RegistryLoadError``."""
    from adcp.canonical_formats import RegistryLoadError
    from adcp.canonical_formats import registry as registry_mod

    registry_mod._load_registry_uncopied.cache_clear()
    monkeypatch.setattr(registry_mod, "_read_registry_json", lambda: "this is not json {")

    try:
        with pytest.raises(RegistryLoadError) as exc:
            registry_mod._load_registry_uncopied()
        assert "invalid JSON" in str(exc.value)
        assert "ADCP_VERSION=" in str(exc.value)
    finally:
        registry_mod._load_registry_uncopied.cache_clear()


# ---------------------------------------------------------------------------
# Version-operator DSL
# ---------------------------------------------------------------------------


def test_versions_overlap_supports_strict_inequalities() -> None:
    """Operators ``<``, ``>``, ``!=``, ``==`` recognised alongside ``<=``, ``>=``."""
    from adcp.canonical_formats.registry import _versions_overlap

    assert _versions_overlap("4.2", [">4.0"])
    assert not _versions_overlap("4.0", [">4.0"])
    assert _versions_overlap("3.0", ["<4.0"])
    assert not _versions_overlap("4.0", ["<4.0"])
    assert _versions_overlap("4.2", ["!=3.0"])
    assert not _versions_overlap("3.0", ["!=3.0"])
    assert _versions_overlap("4.2", ["==4.2"])


def test_versions_overlap_fails_loud_on_unrecognised_operator() -> None:
    """A typo like ``~>4.0`` would silently never match — fail loudly instead."""
    from adcp.canonical_formats.registry import _versions_overlap

    with pytest.raises(ValueError) as exc:
        _versions_overlap("4.2", ["~>4.0"])
    assert "Unrecognised version-constraint operator" in str(exc.value)
