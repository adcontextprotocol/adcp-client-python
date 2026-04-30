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
async def test_create_media_buy_terms_rejected_high_viewability() -> None:
    """Per-package measurement_terms.viewability_threshold > 80 must return TERMS_REJECTED."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {"viewability_threshold": 85},
                }
            ],
        }
    )
    errors = resp.get("errors", [])
    assert any(e.get("code") == "TERMS_REJECTED" for e in errors), (
        f"Expected TERMS_REJECTED in errors, got: {resp}"
    )
    # TERMS_REJECTED is a negotiation error; recovery must be correctable, not terminal.
    rejected = next(e for e in errors if e.get("code") == "TERMS_REJECTED")
    assert rejected.get("recovery") == "correctable", (
        f"Expected recovery=correctable, got: {rejected}"
    )


@pytest.mark.asyncio
async def test_create_media_buy_terms_rejected_at_boundary() -> None:
    """viewability_threshold == 80 is exactly at the limit and must be accepted."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {"viewability_threshold": 80},
                }
            ],
        }
    )
    assert resp.get("media_buy_id") is not None, (
        f"viewability_threshold=80 should be accepted, got: {resp}"
    )


@pytest.mark.asyncio
async def test_create_media_buy_accepts_normal_viewability() -> None:
    """viewability_threshold < 80 must NOT be rejected."""
    seller = _seller()
    resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "measurement_terms": {"viewability_threshold": 70},
                }
            ],
        }
    )
    assert resp.get("media_buy_id") is not None, f"Expected success, got: {resp}"


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
    assert packages[0].get("targeting_overlay") == overlay, (
        f"targeting_overlay not round-tripped: {packages[0]}"
    )


# ---------------------------------------------------------------------------
# targeting_overlay round-trip on update (failure 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_media_buy_persists_targeting_overlay() -> None:
    """update_media_buy must apply targeting_overlay to the stored package."""
    seller = _seller()

    # Create without overlay
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

    # Find the generated package_id
    pkg_id = _sa.media_buys[mb_id]["packages"][0]["package_id"]

    # Update with targeting_overlay
    overlay = {"property_list": {"list_id": "acme_outdoor_no_match_v1", "match": "none"}}
    update_resp = await seller.update_media_buy(
        {
            "media_buy_id": mb_id,
            "packages": [
                {
                    "package_id": pkg_id,
                    "targeting_overlay": overlay,
                }
            ],
        }
    )
    assert update_resp.get("media_buy_id") == mb_id, f"Update failed: {update_resp}"

    # Verify overlay persisted in get_media_buys
    get_resp = await seller.get_media_buys({"media_buy_ids": [mb_id]})
    packages = get_resp["media_buys"][0]["packages"]
    assert packages[0].get("targeting_overlay") == overlay, (
        f"targeting_overlay not persisted after update: {packages[0]}"
    )
