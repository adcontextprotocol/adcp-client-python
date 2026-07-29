"""Public ``adcp.canonical_formats.fixtures`` module.

Adopters writing their own canonical-formats consumers reuse the
vendored reference fixtures via this module rather than re-vendoring
upstream. Tests pin that the public surface stays stable.
"""

from __future__ import annotations

import pytest

from adcp.canonical_formats import fixtures as cf_fixtures


def test_reference_product_names_lists_14_fixtures() -> None:
    """The 14 v2 product fixtures vendored from upstream are pinned."""
    assert len(cf_fixtures.REFERENCE_PRODUCT_NAMES) == 14


def test_every_reference_product_loads_as_a_dict() -> None:
    for name in cf_fixtures.REFERENCE_PRODUCT_NAMES:
        product = cf_fixtures.load_reference_product(name)
        assert isinstance(product, dict)
        assert "product_id" in product
        assert "format_options" in product


def test_load_reference_product_accepts_json_suffix() -> None:
    bare = cf_fixtures.load_reference_product("meta_reels_us")
    suffixed = cf_fixtures.load_reference_product("meta_reels_us.json")
    # Same cached instance — confirms the suffix-strip happens before the lru_cache key.
    assert bare is suffixed


def test_unknown_product_raises() -> None:
    with pytest.raises(ValueError) as exc:
        cf_fixtures.load_reference_product("does_not_exist")
    assert "does_not_exist" in str(exc.value)


def test_load_v1_reference_catalog_returns_rc3_55_entries() -> None:
    catalog = cf_fixtures.load_v1_reference_catalog()
    assert isinstance(catalog, list)
    assert len(catalog) == 55
    # Every entry carries a format_id + canonical annotation per the
    # upstream contract.
    for entry in catalog:
        assert "format_id" in entry
        assert "canonical" in entry


def test_load_v1_reference_catalog_is_cached() -> None:
    a = cf_fixtures.load_v1_reference_catalog()
    b = cf_fixtures.load_v1_reference_catalog()
    assert a is b


def test_fixtures_match_in_tree_test_vendoring() -> None:
    """The vendored fixtures under ``src/adcp/canonical_formats/_fixtures/``
    MUST match byte-for-byte the in-tree test vendoring at
    ``tests/fixtures/canonical/`` — both come from the same upstream
    refresh, and a drift means someone re-vendored one but not the
    other."""
    import json
    from pathlib import Path

    test_fixtures = Path(__file__).parent / "fixtures" / "canonical"
    for name in cf_fixtures.REFERENCE_PRODUCT_NAMES:
        public = cf_fixtures.load_reference_product(name)
        test = json.loads((test_fixtures / f"{name}.json").read_text())
        assert public == test, f"{name}: package vs test-tree fixture drift"
