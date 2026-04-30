"""Tests for the 5 storyboard fixture-gap fixes in examples/seller_agent.py.

Covers:
  - seed_product populates schema-required field defaults for minimal fixtures
  - create_media_buy returns TERMS_REJECTED for aggressive measurement_terms
  - create_media_buy round-trips targeting_overlay / property_list through storage
  - get_media_buys returns persisted targeting_overlay
  - update_media_buy applies targeting_overlay and property_list deltas
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

# examples/ is not a package; add it to the path once at import time.
_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

import seller_agent as _sa  # noqa: E402 (path manipulation above is intentional)

# ---------------------------------------------------------------------------
# Fixture: reset module-level globals before every test so tests are isolated.
# ---------------------------------------------------------------------------

# Snapshot taken at import time — before any test mutates the module state.
_INITIAL_PRODUCTS: list[dict[str, Any]] = deepcopy(_sa.PRODUCTS)


@pytest.fixture(autouse=True)
def _reset_seller_state() -> Any:
    """Reset all mutable module globals to their initial state before each test."""
    _sa.PRODUCTS.clear()
    _sa.PRODUCTS.extend(deepcopy(_INITIAL_PRODUCTS))
    _sa.media_buys.clear()
    _sa.creatives.clear()
    _sa.accounts.clear()
    _sa.proposals.clear()
    _sa.plans.clear()
    _sa.seeded_creative_formats.clear()
    _sa.pending_directives.clear()
    _sa.pending_task_completions.clear()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seller() -> _sa.DemoSeller:
    return _sa.DemoSeller()


def _store() -> _sa.DemoStore:
    return _sa.DemoStore()


# ---------------------------------------------------------------------------
# seed_product — required field defaults (failures 1 & 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_product_minimal_fixture_adds_required_fields() -> None:
    """seed_product fills in required fields when the fixture omits them."""
    store = _store()
    result = await store.seed_product(product_id="outdoor_display_q2")
    assert result["product_id"] == "outdoor_display_q2"

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "outdoor_display_q2")
    assert "name" in seeded
    assert isinstance(seeded["format_ids"], list)
    assert isinstance(seeded["pricing_options"], list)
    assert "reporting_capabilities" in seeded
    assert "delivery_measurement" in seeded


@pytest.mark.asyncio
async def test_seed_product_fixture_fields_not_overwritten() -> None:
    """Fixture values must not be overwritten by the setdefault calls."""
    store = _store()
    fixture = {
        "name": "Q2 Outdoor Custom",
        "delivery_type": "guaranteed",
        "format_ids": [{"agent_url": "http://x", "id": "custom_format"}],
        "pricing_options": [{"pricing_option_id": "po-1", "pricing_model": "cpm"}],
        "reporting_capabilities": {"available_metrics": ["impressions"]},
        "delivery_measurement": {"provider": "moat"},
        "publisher_properties": [{"publisher_domain": "example.com"}],
    }
    await store.seed_product(fixture=fixture, product_id="outdoor_display_q2")

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "outdoor_display_q2")
    assert seeded["name"] == "Q2 Outdoor Custom"
    assert seeded["delivery_type"] == "guaranteed"
    assert seeded["format_ids"] == [{"agent_url": "http://x", "id": "custom_format"}]
    assert seeded["delivery_measurement"] == {"provider": "moat"}


@pytest.mark.asyncio
async def test_seed_product_minimal_fixture_satisfies_schema_requirements() -> None:
    """A bare ``seed_product(product_id=...)`` must produce a product
    that satisfies the spec's get-products-response.json schema.

    Regression for storyboard CI failures where ``publisher_properties``
    defaulted to an empty list (violates ``minItems: 1``),
    ``available_reporting_frequencies`` was empty (same), and
    ``format_ids`` items were missing ``agent_url``.
    """
    store = _store()
    await store.seed_product(product_id="schema_check_minimal")

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "schema_check_minimal")

    # publisher_properties: minItems 1
    assert (
        len(seeded["publisher_properties"]) >= 1
    ), f"publisher_properties must be non-empty; got {seeded['publisher_properties']}"

    # format_ids: minItems 1, each item requires {agent_url, id}
    assert len(seeded["format_ids"]) >= 1
    for fmt in seeded["format_ids"]:
        assert "agent_url" in fmt, f"format_ids item missing agent_url: {fmt}"
        assert "id" in fmt, f"format_ids item missing id: {fmt}"

    # reporting_capabilities.available_reporting_frequencies: minItems 1
    rc = seeded["reporting_capabilities"]
    assert (
        len(rc["available_reporting_frequencies"]) >= 1
    ), f"available_reporting_frequencies must be non-empty; got {rc}"


@pytest.mark.asyncio
async def test_seed_product_normalizes_format_ids_missing_agent_url() -> None:
    """Storyboard fixtures commonly send ``format_ids: [{"id": "..."}]``
    — the bare id without the canonical ``agent_url``. The schema
    requires both fields, so seed_product fills in the local AGENT_URL
    for any caller-supplied format_ids item missing it. Existing
    agent_url values are preserved (regression for the
    fixture-fields-not-overwritten test).
    """
    store = _store()
    await store.seed_product(
        fixture={"format_ids": [{"id": "video_15s"}, {"id": "display_300x250"}]},
        product_id="agent_url_normalize_test",
    )

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_normalize_test")
    for fmt in seeded["format_ids"]:
        assert (
            fmt["agent_url"] == _sa.AGENT_URL
        ), f"agent_url should be filled in from local AGENT_URL: {fmt}"

    # Existing agent_url MUST be preserved.
    await store.seed_product(
        fixture={"format_ids": [{"agent_url": "https://other.example/", "id": "x"}]},
        product_id="agent_url_preserve_test",
    )
    preserved = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_preserve_test")
    assert preserved["format_ids"][0]["agent_url"] == "https://other.example/"


@pytest.mark.asyncio
async def test_seed_product_format_ids_edge_cases() -> None:
    """Format-id normalization edge cases — empty-string agent_url and
    explicit ``None`` are both treated as missing (filled in with local
    AGENT_URL); non-dict items pass through unchanged so weird storyboard
    fixtures don't crash the seller."""
    store = _store()

    # Empty string should be treated as missing.
    await store.seed_product(
        fixture={"format_ids": [{"agent_url": "", "id": "x"}]},
        product_id="agent_url_empty_string",
    )
    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_empty_string")
    assert seeded["format_ids"][0]["agent_url"] == _sa.AGENT_URL

    # Explicit None should be treated as missing.
    await store.seed_product(
        fixture={"format_ids": [{"agent_url": None, "id": "x"}]},
        product_id="agent_url_explicit_none",
    )
    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_explicit_none")
    assert seeded["format_ids"][0]["agent_url"] == _sa.AGENT_URL

    # Non-dict items pass through unchanged — defensive against
    # malformed fixtures (storyboard runner downstream of us).
    await store.seed_product(
        fixture={"format_ids": ["just-a-string", {"id": "real", "agent_url": "http://x"}]},
        product_id="agent_url_mixed_shapes",
    )
    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_mixed_shapes")
    assert seeded["format_ids"][0] == "just-a-string"
    assert seeded["format_ids"][1]["agent_url"] == "http://x"


@pytest.mark.asyncio
async def test_seed_product_is_findable_by_create_media_buy() -> None:
    """After seed_product, create_media_buy must NOT return PRODUCT_NOT_FOUND."""
    store = _store()
    seller = _seller()

    await store.seed_product(
        fixture={
            "pricing_options": [{"pricing_option_id": "po-q2", "pricing_model": "cpm"}],
        },
        product_id="outdoor_display_q2",
    )

    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "outdoor_display_q2",
                    "pricing_option_id": "po-q2",
                    "budget": 5000,
                }
            ]
        }
    )
    assert resp.get("media_buy_id") is not None, f"Expected media buy, got: {resp}"


# ---------------------------------------------------------------------------
# measurement_terms rejection (failure 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_accepts_third_party_vendor() -> None:
    """Vendor identity is buyer's choice; storyboard
    measurement_terms_rejected/create_media_buy_relaxed_terms expects
    acceptance of `vendor.domain` like 'videoamp.example' as long as
    variance + window are workable."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {
                        "billing_measurement": {
                            "vendor": {"domain": "videoamp.example"},
                            "max_variance_percent": 10,
                            "measurement_window": "c7",
                        },
                    },
                }
            ],
        }
    )
    assert (
        resp.get("media_buy_id") is not None
    ), f"Third-party vendor with workable terms should be accepted, got: {resp}"


@pytest.mark.asyncio
async def test_create_media_buy_terms_rejected_aggressive_variance() -> None:
    """max_variance_percent < 5 is unworkable — buyer dictating tighter
    tolerance than the seller's internal counter can promise."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {
                        "billing_measurement": {"max_variance_percent": 0.5},
                    },
                }
            ],
        }
    )
    errors = resp.get("errors", [])
    rejected = next((e for e in errors if e.get("code") == "TERMS_REJECTED"), None)
    assert rejected is not None, f"Expected TERMS_REJECTED, got: {resp}"
    assert rejected["recovery"] == "correctable"
    assert rejected.get("field") == "measurement_terms"


@pytest.mark.asyncio
async def test_create_media_buy_terms_rejected_aggressive_window() -> None:
    """measurement_window outside (c3, c7) is unworkable for the demo seller."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {
                        "billing_measurement": {"measurement_window": "c30"},
                    },
                }
            ],
        }
    )
    errors = resp.get("errors", [])
    rejected = next((e for e in errors if e.get("code") == "TERMS_REJECTED"), None)
    assert rejected is not None, f"Expected TERMS_REJECTED, got: {resp}"
    assert rejected["recovery"] == "correctable"
    assert rejected.get("field") == "measurement_terms"


@pytest.mark.asyncio
async def test_create_media_buy_accepts_workable_terms() -> None:
    """variance >= 5 and window in (c3, c7) is the runner's 'relaxed terms' shape."""
    seller = _seller()
    for window in ("c3", "c7"):
        resp = await seller.create_media_buy(
            {
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "pricing_option_id": "po-cpm-homepage",
                        "budget": 10000,
                        "measurement_terms": {
                            "billing_measurement": {
                                "max_variance_percent": 5,
                                "measurement_window": window,
                            },
                        },
                    }
                ],
            }
        )
        assert (
            resp.get("media_buy_id") is not None
        ), f"Workable terms with window={window!r} should be accepted, got: {resp}"


# ---------------------------------------------------------------------------
# targeting_overlay round-trip on create (failures 4 & 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_round_trips_targeting_overlay() -> None:
    """targeting_overlay must survive create and appear in get_media_buys."""
    seller = _seller()
    overlay = {"property_list": {"list_id": "acme_outdoor_allowlist_v1", "match": "any"}}
    create_resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "run-of-site",
                    "pricing_option_id": "po-cpm-ros",
                    "budget": 2000,
                    "targeting_overlay": overlay,
                }
            ]
        }
    )
    mb_id = create_resp.get("media_buy_id")
    assert mb_id is not None, f"Expected media buy id, got: {create_resp}"

    get_resp = await seller.get_media_buys({"media_buy_ids": [mb_id]})
    mb_list = get_resp.get("media_buys", [])
    assert mb_list, "Expected at least one media buy in response"
    packages = mb_list[0].get("packages", [])
    assert packages, "Expected packages in media buy"
    assert (
        packages[0].get("targeting_overlay") == overlay
    ), f"targeting_overlay not round-tripped: {packages[0]}"


# ---------------------------------------------------------------------------
# targeting_overlay round-trip on update (failure 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_media_buy_persists_targeting_overlay() -> None:
    """update_media_buy must apply targeting_overlay AND
    creative_assignments AND creatives deltas to the stored package
    state — all three are persisted by the seller and round-tripped
    through get_media_buys."""
    seller = _seller()

    # Create without any overlay or creatives
    create_resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "run-of-site",
                    "pricing_option_id": "po-cpm-ros",
                    "budget": 1500,
                }
            ]
        }
    )
    mb_id = create_resp.get("media_buy_id")
    assert mb_id is not None
    pkg_id = _sa.media_buys[mb_id]["packages"][0]["package_id"]

    # Update all three persisted fields in one call.
    overlay = {"property_list": {"list_id": "acme_outdoor_no_match_v1", "match": "none"}}
    assignments = [{"creative_id": "cr-1", "weighting": {"type": "even"}}]
    creatives = [{"creative_id": "cr-1", "name": "test"}]
    update_resp = await seller.update_media_buy(
        {
            "media_buy_id": mb_id,
            "packages": [
                {
                    "package_id": pkg_id,
                    "targeting_overlay": overlay,
                    "creative_assignments": assignments,
                    "creatives": creatives,
                }
            ],
        }
    )
    assert update_resp.get("media_buy_id") == mb_id, f"Update failed: {update_resp}"

    # All three fields must be persisted on the package — round-tripping through
    # get_media_buys is the storyboard contract for delivery_reporting + inventory.
    persisted = _sa.media_buys[mb_id]["packages"][0]
    assert persisted.get("targeting_overlay") == overlay
    assert persisted.get("creative_assignments") == assignments
    assert persisted.get("creatives") == creatives

    # Verify the get_media_buys response also surfaces the persisted fields.
    get_resp = await seller.get_media_buys({"media_buy_ids": [mb_id]})
    pkg = get_resp["media_buys"][0]["packages"][0]
    assert pkg.get("targeting_overlay") == overlay
    assert pkg.get("creative_assignments") == assignments
    assert pkg.get("creatives") == creatives


@pytest.mark.asyncio
async def test_create_media_buy_handles_non_dict_measurement_terms() -> None:
    """Defensive coercion — fixtures occasionally send measurement_terms
    as a non-dict (string / list / None). The seller must NOT crash with
    AttributeError; treat as "no terms supplied" and accept the package."""
    seller = _seller()
    for bogus in ("a-string", 123, None, ["a", "list"]):
        resp = await seller.create_media_buy(
            {
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "pricing_option_id": "po-cpm-homepage",
                        "budget": 10000,
                        "measurement_terms": bogus,
                    }
                ],
            }
        )
        assert (
            resp.get("media_buy_id") is not None
        ), f"Bogus measurement_terms={bogus!r} should be ignored, got: {resp}"
