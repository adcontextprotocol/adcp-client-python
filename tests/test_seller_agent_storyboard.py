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
    _sa.open_impairments.clear()
    _sa.accounts.clear()
    _sa.proposals.clear()
    _sa.plans.clear()
    _sa.seeded_creative_formats.clear()
    _sa.legacy_routes_by_option_id.clear()
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


def _image_option(
    option_id: str,
    *,
    legacy_id: str = "display_300x250",
    legacy_owner: str = _sa.LEGACY_FORMAT_OWNER,
) -> dict[str, Any]:
    """Canonical declaration with an explicit, compatibility-only legacy ref."""
    return {
        "format_option_id": option_id,
        "format_kind": "image",
        "params": {"sizes": [{"width": 300, "height": 250}]},
        "v1_format_ref": [{"agent_url": legacy_owner, "id": legacy_id}],
    }


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
    assert isinstance(seeded["format_options"], list)
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
        "format_options": [
            _image_option("custom-format", legacy_id="custom_format", legacy_owner="http://x")
        ],
        "pricing_options": [{"pricing_option_id": "po-1", "pricing_model": "cpm"}],
        "reporting_capabilities": {"available_metrics": ["impressions"]},
        "delivery_measurement": {"provider": "moat"},
        "publisher_properties": [{"publisher_domain": "example.com"}],
    }
    await store.seed_product(fixture=fixture, product_id="outdoor_display_q2")

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "outdoor_display_q2")
    assert seeded["name"] == "Q2 Outdoor Custom"
    assert seeded["delivery_type"] == "guaranteed"
    assert seeded["format_options"] == fixture["format_options"]
    assert seeded["delivery_measurement"] == {"provider": "moat"}


@pytest.mark.asyncio
async def test_seed_product_minimal_fixture_satisfies_schema_requirements() -> None:
    """A bare ``seed_product(product_id=...)`` must produce a product
    that satisfies the spec's get-products-response.json schema.

    Regression for storyboard CI failures where ``publisher_properties``
    defaulted to an empty list (violates ``minItems: 1``),
    ``available_reporting_frequencies`` was empty (same), and
    canonical ``format_options`` were absent.
    """
    store = _store()
    await store.seed_product(product_id="schema_check_minimal")

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "schema_check_minimal")

    # publisher_properties: minItems 1
    assert (
        len(seeded["publisher_properties"]) >= 1
    ), f"publisher_properties must be non-empty; got {seeded['publisher_properties']}"

    # Canonical format declarations are non-empty and self-describing.
    assert len(seeded["format_options"]) >= 1
    for fmt in seeded["format_options"]:
        assert "format_option_id" in fmt
        assert "format_kind" in fmt
        assert "params" in fmt

    # reporting_capabilities.available_reporting_frequencies: minItems 1
    rc = seeded["reporting_capabilities"]
    assert (
        len(rc["available_reporting_frequencies"]) >= 1
    ), f"available_reporting_frequencies must be non-empty; got {rc}"

    assert seeded["format_options"], "format_options must be non-empty"


@pytest.mark.asyncio
async def test_get_products_prioritizes_seeded_product_that_matches_brief() -> None:
    """Storyboard steps read the product they just seeded from
    ``/products/0`` when the brief names that fixture. Keep the
    default catalog order for unrelated follow-on storyboards.
    """
    store = _store()
    seller = _seller()

    await store.seed_product(product_id="available_actions_display")

    resp = await seller.get_products({"brief": "available actions display package"})
    assert resp["products"][0]["product_id"] == "available_actions_display"

    unrelated_resp = await seller.get_products({"brief": "Display inventory Q3 flight"})
    assert unrelated_resp["products"][0]["product_id"] == _INITIAL_PRODUCTS[0]["product_id"]


@pytest.mark.asyncio
async def test_get_products_uses_canonical_models_and_preserves_legacy_delivery() -> None:
    from adcp.canonical_formats import project_canonical_response_to_legacy

    response = await _seller().get_products({})

    assert "format_ids" not in response["products"][0]
    assert "v1_format_ref" not in response["products"][0]["format_options"][0]

    projected = project_canonical_response_to_legacy(response)
    assert projected["products"][0]["format_ids"] == _INITIAL_PRODUCTS[0]["format_ids"]


@pytest.mark.asyncio
async def test_list_creatives_preserves_explicit_legacy_tuple_for_delivery() -> None:
    from adcp.canonical_formats import project_canonical_response_to_legacy

    original_ref = {"agent_url": "https://formats.example/mcp", "id": "custom-display"}
    _sa.creatives["creative-1"] = {
        "creative_id": "creative-1",
        "name": "Custom display",
        "status": "approved",
        "format_id": original_ref,
    }

    response = await _seller().list_creatives({})
    canonical = response["creatives"][0]
    assert "format_id" not in canonical
    assert canonical["format_kind"] == "image"
    assert canonical["format_option_ref"]["format_option_id"].startswith("migrated_")

    projected = project_canonical_response_to_legacy(response)
    assert projected["creatives"][0]["format_id"] == original_ref


@pytest.mark.asyncio
async def test_capabilities_select_legacy_storyboard_wire_dialect() -> None:
    response = await _seller().get_adcp_capabilities({})
    assert response["media_buy"]["features"]["canonical_creatives"] is False


@pytest.mark.asyncio
async def test_seed_product_preserves_canonical_format_options() -> None:
    """A seeded canonical declaration remains the source of truth."""
    store = _store()
    options = [
        _image_option("video", legacy_id="video_15s"),
        _image_option("display", legacy_id="display_300x250"),
    ]
    await store.seed_product(
        fixture={"format_options": options},
        product_id="format_options_repair",
    )

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "format_options_repair")
    assert seeded["product_id"] == "format_options_repair"
    assert seeded["format_options"] == options
    assert [option["v1_format_ref"][0]["id"] for option in seeded["format_options"]] == [
        "video_15s",
        "display_300x250",
    ]


@pytest.mark.asyncio
async def test_available_actions_are_resolved_persisted_and_enforced() -> None:
    store = _store()
    seller = _seller()
    await store.seed_product(
        fixture={
            "name": "Available Actions Display Package",
            "delivery_type": "guaranteed",
            "format_options": [_image_option("available-actions-display")],
            "allowed_actions": [
                {
                    "action": "increase_budget",
                    "modes": ["self_serve"],
                    "sla": {"response_max": "PT5M", "completion_max": "PT1H"},
                },
                {
                    "action": "extend_flight",
                    "modes": ["requires_proposal"],
                    "sla": {"response_max": "PT4H", "completion_max": "P2D"},
                    "terms_ref": "terms://available-actions/extension",
                },
                {
                    "action": "cancel",
                    "modes": ["requires_approval"],
                    "sla": {"response_max": "PT1H", "completion_max": "P1D"},
                },
                {
                    "action": "decrease_budget",
                    "modes": ["self_serve"],
                    "allowed_statuses": ["active"],
                },
            ],
        },
        product_id="available_actions_display",
    )
    await store.seed_pricing_option(
        fixture={
            "pricing_model": "cpm",
            "currency": "USD",
            "fixed_price": 8.0,
        },
        product_id="available_actions_display",
        pricing_option_id="available_actions_cpm",
    )

    create_resp = await seller.create_media_buy(
        {
            "packages": [
                {
                    "product_id": "available_actions_display",
                    "pricing_option_id": "available_actions_cpm",
                    "budget": 10000,
                }
            ]
        }
    )
    assert [a["action"] for a in create_resp["available_actions"]] == [
        "increase_budget",
        "extend_flight",
        "cancel",
    ]
    assert create_resp["available_actions"][0]["mode"] == "self_serve"
    assert create_resp["available_actions"][0]["sla"]["response_max"] == "PT5M"
    assert create_resp["available_actions"][1]["mode"] == "requires_proposal"
    assert create_resp["available_actions"][1]["terms_ref"] == "terms://available-actions/extension"
    assert create_resp["available_actions"][2]["mode"] == "requires_approval"

    media_buy_id = create_resp["media_buy_id"]
    package_id = create_resp["packages"][0]["package_id"]
    read_resp = await seller.get_media_buys({"media_buy_ids": [media_buy_id]})
    assert read_resp["media_buys"][0]["available_actions"] == create_resp["available_actions"]
    assert read_resp["media_buys"][0]["valid_actions"]

    update_resp = await seller.update_media_buy(
        {
            "media_buy_id": media_buy_id,
            "packages": [{"package_id": package_id, "budget": 12000}],
        }
    )
    assert update_resp["media_buy_id"] == media_buy_id
    assert update_resp["affected_packages"][0]["package_id"] == package_id
    assert update_resp["affected_packages"][0]["budget"] == 12000
    assert update_resp["available_actions"][0]["action"] == "increase_budget"

    extend_resp = await seller.update_media_buy(
        {
            "media_buy_id": media_buy_id,
            "end_time": "2027-08-31T23:59:59Z",
        }
    )
    assert extend_resp["errors"][0]["code"] == "ACTION_NOT_ALLOWED"
    assert extend_resp["errors"][0]["recovery"] == "correctable"
    assert extend_resp["errors"][0]["details"]["attempted_action"] == "extend_flight"
    assert extend_resp["errors"][0]["details"]["reason"] == "mode_mismatch"
    assert (
        extend_resp["errors"][0]["details"]["currently_available_actions"][1]["mode"]
        == "requires_proposal"
    )

    decrease_resp = await seller.update_media_buy(
        {
            "media_buy_id": media_buy_id,
            "packages": [{"package_id": package_id, "budget": 11000}],
        }
    )
    assert decrease_resp["errors"][0]["details"]["attempted_action"] == "decrease_budget"
    assert decrease_resp["errors"][0]["details"]["reason"] == "wrong_status"

    pause_resp = await seller.update_media_buy({"media_buy_id": media_buy_id, "paused": True})
    assert pause_resp["errors"][0]["recovery"] == "terminal"
    assert pause_resp["errors"][0]["details"]["attempted_action"] == "pause"
    assert pause_resp["errors"][0]["details"]["reason"] == "not_supported_on_product"


@pytest.mark.asyncio
async def test_seed_product_preserves_legacy_refs_on_canonical_options() -> None:
    """Compatibility refs remain attached to their canonical declarations."""
    store = _store()
    options = [
        _image_option("video-option", legacy_id="video_15s"),
        _image_option("display-option", legacy_id="display_300x250"),
    ]
    await store.seed_product(
        fixture={"format_options": options},
        product_id="agent_url_normalize_test",
    )

    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_normalize_test")
    assert seeded["format_options"] == options

    # An explicitly different owner is never reverse-guessed or overwritten.
    external = [_image_option("external", legacy_id="x", legacy_owner="https://other.example/")]
    await store.seed_product(
        fixture={"format_options": external},
        product_id="agent_url_preserve_test",
    )
    preserved = next(p for p in _sa.PRODUCTS if p["product_id"] == "agent_url_preserve_test")
    assert preserved["format_options"] == external


@pytest.mark.asyncio
async def test_seed_product_canonical_format_option_params_round_trip() -> None:
    """Open canonical params survive seeding without legacy identity inference."""
    store = _store()
    option = _image_option("rich-option")
    option["params"]["image_formats"] = ["png", "webp"]
    await store.seed_product(
        fixture={"format_options": [option]},
        product_id="canonical_params_round_trip",
    )
    seeded = next(p for p in _sa.PRODUCTS if p["product_id"] == "canonical_params_round_trip")
    assert seeded["format_options"] == [option]


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


@pytest.mark.asyncio
async def test_create_media_buy_round_trips_context_fields() -> None:
    """rc4 storyboards assert buyer correlation context survives create/read."""
    seller = _seller()
    media_buy_context = {"correlation_id": "media_buy_seller--create_media_buy"}
    package_context = {"buyer_ref": "pending-creatives-line-001"}

    create_resp = await seller.create_media_buy(
        {
            "context": media_buy_context,
            "packages": [
                {
                    "product_id": "premium-homepage",
                    "pricing_option_id": "po-cpm-homepage",
                    "budget": 10000,
                    "context": package_context,
                }
            ],
        }
    )
    assert create_resp["packages"][0]["context"] == package_context

    get_resp = await seller.get_media_buys({"media_buy_ids": [create_resp["media_buy_id"]]})
    media_buy = get_resp["media_buys"][0]
    assert media_buy["context"] == media_buy_context
    assert media_buy["packages"][0]["context"] == package_context


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
    assert update_resp["affected_packages"][0]["targeting_overlay"] == overlay
    assert update_resp["affected_packages"][0]["creative_assignments"] == assignments
    assert update_resp["affected_packages"][0]["creatives"] == creatives

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


def test_health_fields_dedupes_impairments_and_preserves_observed_at() -> None:
    """One rejected creative can impair multiple package references, but it
    should still project as one impairment with stable package scope and
    transition time.
    """
    _sa.creatives["cr-1"] = {
        "creative_id": "cr-1",
        "status": "rejected",
        "status_changed_at": "2026-05-23T10:00:00Z",
    }
    mb = {
        "packages": [
            {
                "package_id": "pkg-1",
                "creative_assignments": [{"creative_id": "cr-1"}],
                "creatives": [{"creative_id": "cr-1"}],
            },
            {
                "package_id": "pkg-2",
                "creative_assignments": [{"creative_id": "cr-1"}],
            },
        ]
    }

    health = _sa._health_fields_for_media_buy("mb-1", mb)

    assert health["health"] == "impaired"
    assert len(health["impairments"]) == 1
    impairment = health["impairments"][0]
    assert impairment["resource_id"] == "cr-1"
    assert impairment["package_ids"] == ["pkg-1", "pkg-2"]
    assert impairment["observed_at"] == "2026-05-23T10:00:00Z"


def test_health_fields_excludes_packages_with_approved_replacements() -> None:
    _sa.creatives["cr-rejected"] = {
        "creative_id": "cr-rejected",
        "status": "rejected",
        "status_changed_at": "2026-05-23T10:00:00Z",
    }
    _sa.creatives["cr-approved"] = {
        "creative_id": "cr-approved",
        "status": "approved",
        "status_changed_at": "2026-05-23T10:01:00Z",
    }
    mb = {
        "packages": [
            {
                "package_id": "pkg-serviceable",
                "creative_assignments": [
                    {"creative_id": "cr-rejected"},
                    {"creative_id": "cr-approved"},
                ],
            },
            {
                "package_id": "pkg-blocked",
                "creative_assignments": [{"creative_id": "cr-rejected"}],
            },
        ]
    }

    health = _sa._health_fields_for_media_buy("mb-1", mb)

    assert health["health"] == "impaired"
    assert len(health["impairments"]) == 1
    assert health["impairments"][0]["package_ids"] == ["pkg-blocked"]


def test_health_fields_tracks_impairment_lifecycle() -> None:
    _sa.creatives["cr-1"] = {
        "creative_id": "cr-1",
        "status": "rejected",
        "status_changed_at": "2026-05-23T10:00:00Z",
    }
    mb = {
        "packages": [
            {
                "package_id": "pkg-1",
                "creative_assignments": [{"creative_id": "cr-1"}],
            }
        ]
    }

    first = _sa._health_fields_for_media_buy("mb-1", mb)["impairments"][0]
    _sa.creatives["cr-1"]["status_changed_at"] = "2026-05-23T10:05:00Z"
    second = _sa._health_fields_for_media_buy("mb-1", mb)["impairments"][0]
    assert second["impairment_id"] == first["impairment_id"]
    assert second["observed_at"] == first["observed_at"]

    _sa.creatives["cr-1"]["status"] = "approved"
    recovered = _sa._health_fields_for_media_buy("mb-1", mb)
    assert recovered == {"health": "ok", "impairments": []}

    _sa.creatives["cr-1"]["status"] = "rejected"
    _sa.creatives["cr-1"]["status_changed_at"] = "2026-05-23T10:10:00Z"
    reopened = _sa._health_fields_for_media_buy("mb-1", mb)["impairments"][0]
    assert reopened["impairment_id"] != first["impairment_id"]
    assert reopened["observed_at"] == "2026-05-23T10:10:00Z"
