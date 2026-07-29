"""Public access to the vendored canonical-formats reference fixtures.

The SDK ships 14 v2 ``Product`` fixtures and a 50-entry v1 reference
catalog vendored from ``adcontextprotocol/adcp`` upstream. They drive
the SDK's own round-trip tests (see
``tests/test_canonical_formats_roundtrip.py``); adopters writing their
own canonical-formats consumers can reuse them via this module rather
than re-vendoring.

Two access shapes:

* :func:`load_reference_product(name)` returns one v2 product fixture
  as a parsed ``dict[str, Any]``.
* :func:`load_v1_reference_catalog()` returns the 50-entry v1 catalog
  as a ``list[dict[str, Any]]``.

The helper :data:`REFERENCE_PRODUCT_NAMES` lists every available
fixture name so adopters can iterate without hardcoding.

Provenance + refresh procedure are documented at
``tests/fixtures/canonical/VENDOR.md`` in this repo.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

# Canonical filename list — pinned so a future re-vendor that adds/
# removes a fixture surfaces here (the matching round-trip test
# :func:`tests.test_canonical_formats_roundtrip` is parametrised over
# this tuple via fixture discovery).
REFERENCE_PRODUCT_NAMES: tuple[str, ...] = (
    "amazon_sponsored_products",
    "chatgpt_brand_mention",
    "gam_3p_display_tag",
    "google_performance_max",
    "meta_carousel",
    "meta_reels_us",
    "nytimes_homepage_html5",
    "nytimes_homepage_mrec",
    "nytimes_homepage_takeover_custom",
    "taboola_content_recommendation",
    "the_daily_30s_host_read",
    "triton_daast_audio_30s",
    "veo_generative_video_15s",
    "youtube_vast_preroll",
)

_V1_CATALOG_NAME = "v1-reference-formats"
_RC3_CATALOG_ADDITIONS_NAME = "rc3-aao-additions"


class _UnknownFixtureError(ValueError):
    """Raised when :func:`load_reference_product` is asked for a name
    that isn't in :data:`REFERENCE_PRODUCT_NAMES`."""


def _fixture_text(stem: str) -> str:
    """Read a fixture file from the bundled ``_fixtures/`` package data.

    Uses :func:`importlib.resources.files` so the read works from
    wheels, editable installs, and zipapps without filesystem
    assumptions.
    """
    resource = files("adcp.canonical_formats._fixtures") / f"{stem}.json"
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=len(REFERENCE_PRODUCT_NAMES))
def _load_by_stem(stem: str) -> dict[str, Any]:
    """Read + parse a fixture by its stem, cached per-stem."""
    return json.loads(_fixture_text(stem))  # type: ignore[no-any-return]


def load_reference_product(name: str) -> dict[str, Any]:
    """Return one of the 14 vendored v2 ``Product`` fixtures.

    Args:
        name: One of :data:`REFERENCE_PRODUCT_NAMES`. The ``.json``
            suffix is optional — callers can pass either
            ``"meta_reels_us"`` or ``"meta_reels_us.json"``.

    Returns:
        The parsed product as a ``dict[str, Any]``. Returned object
        is cached; callers MUST NOT mutate it. ``copy.deepcopy`` if
        you need a writable copy.

    Raises:
        _UnknownFixtureError: when ``name`` isn't a known fixture.
    """
    stem = name.removesuffix(".json")
    if stem not in REFERENCE_PRODUCT_NAMES:
        raise _UnknownFixtureError(
            f"{name!r} is not a known reference product fixture; "
            f"valid names: {REFERENCE_PRODUCT_NAMES!r}"
        )
    return _load_by_stem(stem)


@lru_cache(maxsize=1)
def load_v1_reference_catalog() -> list[dict[str, Any]]:
    """Return the exact 55-entry TypeScript 13.0.0-rc.3 catalog.

    Every entry carries an explicit ``canonical:`` annotation so the
    SDK's v1 → v2 projection
    (:func:`adcp.canonical_formats.project_v1_format_to_declaration`)
    walks resolution-order step 1 on every entry. Useful for adopter
    tests covering catalog migration flows.

    Returned list is cached; callers MUST NOT mutate it.
    ``copy.deepcopy`` if you need a writable copy.
    """
    raw = json.loads(_fixture_text(_V1_CATALOG_NAME))
    if not isinstance(raw, list):
        # The vendored fixture is a JSON array. Wrap a single-object
        # input defensively so a future vendoring mistake fails fast
        # rather than silently confusing callers.
        return [raw]
    additions = json.loads(_fixture_text(_RC3_CATALOG_ADDITIONS_NAME))
    if not isinstance(additions, list):
        raise ValueError("RC3 AAO catalog additions fixture must be a JSON array")
    catalog: list[dict[str, Any]] = [*raw, *additions]
    return catalog


__all__ = [
    "REFERENCE_PRODUCT_NAMES",
    "load_reference_product",
    "load_v1_reference_catalog",
]
