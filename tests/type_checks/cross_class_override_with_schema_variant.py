"""Adopter pattern: cross-class entity override via :data:`SchemaVariant`.

Critical Pattern #2 — when an adopter subclasses a generated response
type and substitutes a **shape-compatible but distinct** entity class
for the parent's declared element type, mypy's Liskov check rejects
the assignment because the two classes are siblings, not parent-child.
The historical workaround was ``# type: ignore[assignment]``; this
fixture proves that :data:`adcp.types.SchemaVariant` plus the mypy
plugin (``adcp.types.mypy_plugin``, registered in this repo's
``pyproject.toml``) eliminates the ignore.

Every override below must pass ``mypy --strict`` with **zero** ``#
type: ignore`` lines. The cases mirror the salesagent ignores listed
in #710 — different parent types, different child types, all
shape-compatible-but-not-subclasses.

Run from the repo root::

    mypy --strict tests/type_checks/

If this file regresses (mypy reports ``[assignment]`` here), the
plugin is misregistered or the marker semantics have drifted —
investigate :mod:`adcp.types.mypy_plugin` before relaxing the test.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel

from adcp.types import SchemaVariant

# --- Case 1: distinct delivery-view class for media buys -----------------


class MediaBuy(BaseModel):
    """Library wire type."""

    media_buy_id: str
    status: str = "active"


class MediaBuyDeliveryData(BaseModel):
    """Adopter delivery-context view — same shape, different class.

    Mirrors the salesagent ``MediaBuyDeliveryData`` case from #710. The
    delivery context adds local fields (impressions, spend) that don't
    belong on the canonical ``MediaBuy``."""

    media_buy_id: str
    status: str = "active"
    impressions_delivered: int = 0
    spend_to_date_cents: int = 0


class LibraryGetMediaBuysResponse(BaseModel):
    media_buys: list[MediaBuy] = []


class GetMediaBuysResponse(LibraryGetMediaBuysResponse):
    # ``SchemaVariant`` marks the substitution as intentional. No
    # ``# type: ignore[assignment]`` — the bundled mypy plugin
    # rewrites the override annotation to ``Any`` so the LSP check
    # passes.
    media_buys: SchemaVariant[list[MediaBuyDeliveryData]]


# --- Case 2: same-name local class (salesagent's Creative pattern) -------


class LibraryCreative(BaseModel):
    creative_id: str
    name: str = ""


class Creative(BaseModel):
    """Adopter ``Creative`` — same name as library type but a different
    class (carries local DB columns). The parent's field declares
    ``list[LibraryCreative]``; the adopter wants
    ``list[Creative]`` (local) without an ignore.
    """

    creative_id: str
    name: str = ""
    internal_state: str = "active"


class LibraryListCreativesResponse(BaseModel):
    creatives: list[LibraryCreative] = []


class ListCreativesResponse(LibraryListCreativesResponse):
    creatives: SchemaVariant[list[Creative]]


# --- Case 3: inclusion vs exclusion variants of the same shape -----------


class GeoCountry(BaseModel):
    """Inclusion variant — what the spec uses for the include list."""

    iso_code: str


class GeoCountriesExcludeItem(BaseModel):
    """Exclusion variant — distinct named type with the same shape.

    Same problem as cases 1 and 2: the spec models inclusion and
    exclusion as separate classes; adopters substitute the inclusion
    type into an exclusion-typed parent field (or vice versa)
    because the shape is identical and conversion between them is a
    cast at the boundary."""

    iso_code: str


class LibraryAudienceFilters(BaseModel):
    excluded_countries: list[GeoCountriesExcludeItem] = []


class AdopterAudienceFilters(LibraryAudienceFilters):
    excluded_countries: SchemaVariant[list[GeoCountry]]


# --- Case 4: non-list container — Optional[Sibling] ---------------------
#
# Proves the marker isn't limited to ``list[Sibling]``. Same Liskov
# violation at the type level on a different shape; same fix.


class LibraryQuerySummary(BaseModel):
    query_id: str


class QuerySummary(BaseModel):
    """Adopter scalar entity — same name as library, distinct class.
    Mirrors the salesagent ``creative.py:677`` case."""

    query_id: str
    internal_cache_key: str = ""


class LibraryGetSignalsResponse(BaseModel):
    summary: LibraryQuerySummary | None = None


class GetSignalsResponse(LibraryGetSignalsResponse):
    summary: SchemaVariant[QuerySummary | None]


# --- Case 5: dict[str, Sibling] container -------------------------------


class LibraryFeatureFlag(BaseModel):
    flag_id: str


class FeatureFlag(BaseModel):
    flag_id: str
    rollout_pct: int = 0


class LibraryFeatureBag(BaseModel):
    flags_by_name: dict[str, LibraryFeatureFlag] = {}


class AdopterFeatureBag(LibraryFeatureBag):
    flags_by_name: SchemaVariant[dict[str, FeatureFlag]]


# --- Inside-the-override: cast() to recover precise inference -----------


def consume_with_precise_inference(resp: GetMediaBuysResponse) -> int:
    """Mypy sees ``resp.media_buys`` as ``Any`` because of the
    SchemaVariant rewrite. To call entity-specific methods with full
    inference, cast inside the override site. This is the documented
    tradeoff — the marker buys override-compat at the cost of
    inside-the-override inference.
    """
    total = 0
    for delivery in cast(list[MediaBuyDeliveryData], resp.media_buys):
        total += delivery.impressions_delivered
    return total


# --- Construction proves the runtime side -------------------------------


_r1 = GetMediaBuysResponse(
    media_buys=[MediaBuyDeliveryData(media_buy_id="mb_1", impressions_delivered=100)]
)
_r2 = ListCreativesResponse(creatives=[Creative(creative_id="c_1", internal_state="paused")])
_r3 = AdopterAudienceFilters(excluded_countries=[GeoCountry(iso_code="US")])
_r4 = GetSignalsResponse(summary=QuerySummary(query_id="q1", internal_cache_key="x"))
_r5 = AdopterFeatureBag(flags_by_name={"beta": FeatureFlag(flag_id="beta", rollout_pct=10)})

# All five constructions type-check; the runtime side is exercised by
# tests/test_schema_variant.py. This file's contract is purely the
# mypy --strict pass.
_ = _r1, _r2, _r3, _r4, _r5
