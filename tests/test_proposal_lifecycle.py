"""Tests for proposal_lifecycle helpers — D4 / D7 framework intercepts.

Covers:

* enforce_proposal_expiry (D7) — three failure modes:
    - missing record → PROPOSAL_NOT_FOUND, recovery=terminal
    - cross-tenant probe → PROPOSAL_NOT_FOUND (not the raw record)
    - state != COMMITTED → PROPOSAL_NOT_COMMITTED, recovery=correctable
    - now > expires_at + grace → PROPOSAL_EXPIRED, recovery=terminal
    - within grace → returns the record
* validate_capability_overlap (D4) — pre-adapter rejection produces
  INVALID_REQUEST with the right field path:
    - pricing_models gate
    - targeting_dimensions gate
    - delivery_types gate
    - signal_types gate
    - None axis is no-op (legacy / open posture)
    - frozenset() (empty) means deny-all
* validate_overlap_subset_of_wire (D4 round-4) — adopter declaring
  overlap.pricing_models > wire pricing options raises INTERNAL_ERROR.
* detect_finalize_action — extracts (index, proposal_id, ask) from refine[]
  with action='finalize' on scope='proposal'; returns None otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from adcp.decisioning import (
    AdcpError,
    CapabilityOverlap,
    InMemoryProposalStore,
    Recipe,
)
from adcp.decisioning.proposal_lifecycle import (
    detect_finalize_action,
    enforce_proposal_expiry,
    validate_capability_overlap,
    validate_overlap_subset_of_wire,
)


def _utc(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


class _DemoRecipe(Recipe):
    recipe_kind: str = "demo"
    line_item_id: str = "li_demo"


# ---------------------------------------------------------------------------
# enforce_proposal_expiry (D7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_unknown_proposal_raises_not_found() -> None:
    store = InMemoryProposalStore()
    with pytest.raises(AdcpError) as exc:
        await enforce_proposal_expiry(
            "p-missing",
            proposal_store=store,
            expected_account_id="acct_a",
        )
    assert exc.value.code == "PROPOSAL_NOT_FOUND"
    assert exc.value.recovery == "terminal"
    assert exc.value.field == "proposal_id"


@pytest.mark.asyncio
async def test_expiry_cross_tenant_returns_not_found() -> None:
    """Cross-tenant probes squash into PROPOSAL_NOT_FOUND — same error
    as missing-id so principal-enumeration via id probing fails."""
    store = InMemoryProposalStore()
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit("p1", expires_at=_utc("2099-01-02T00:00:00"), proposal_payload={})
    with pytest.raises(AdcpError) as exc:
        await enforce_proposal_expiry(
            "p1",
            proposal_store=store,
            expected_account_id="acct_OTHER",
        )
    assert exc.value.code == "PROPOSAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_expiry_draft_state_raises_not_committed() -> None:
    store = InMemoryProposalStore()
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    with pytest.raises(AdcpError) as exc:
        await enforce_proposal_expiry(
            "p1",
            proposal_store=store,
            expected_account_id="acct_a",
        )
    assert exc.value.code == "PROPOSAL_NOT_COMMITTED"
    assert exc.value.recovery == "correctable"


@pytest.mark.asyncio
async def test_expiry_past_deadline_raises_expired() -> None:
    """Pin the store's clock so its eviction logic doesn't garbage-collect
    the record before the lifecycle helper checks expiry. The store's
    clock is independent of the lifecycle's ``now`` parameter — they
    test different concerns."""
    fixed = _utc("2099-06-01T00:00:00")
    store = InMemoryProposalStore(clock=lambda: fixed)
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = _utc("2099-06-01T01:00:00")  # 1 hour in the store's "future"
    await store.commit("p1", expires_at=expires, proposal_payload={})

    # Lifecycle checks against ``now`` 1 hour past expires — the
    # adopter's grace is 0 → PROPOSAL_EXPIRED.
    now = expires + timedelta(hours=1)
    with pytest.raises(AdcpError) as exc:
        await enforce_proposal_expiry(
            "p1",
            proposal_store=store,
            expected_account_id="acct_a",
            grace_seconds=0,
            now=now,
        )
    assert exc.value.code == "PROPOSAL_EXPIRED"
    assert exc.value.recovery == "terminal"


@pytest.mark.asyncio
async def test_expiry_within_grace_returns_record() -> None:
    """Adopter sets a 5-minute grace; now is 4 minutes past expires →
    expiry validation succeeds and returns the record."""
    store = InMemoryProposalStore()
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = _utc("2099-01-01T00:00:00")
    await store.commit("p1", expires_at=expires, proposal_payload={})
    now = expires + timedelta(minutes=4)
    record = await enforce_proposal_expiry(
        "p1",
        proposal_store=store,
        expected_account_id="acct_a",
        grace_seconds=300,  # 5 min
        now=now,
    )
    assert record.proposal_id == "p1"


@pytest.mark.asyncio
async def test_expiry_inside_window_returns_record() -> None:
    store = InMemoryProposalStore()
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = _utc("2099-12-31T00:00:00")
    await store.commit("p1", expires_at=expires, proposal_payload={"committed": True})
    record = await enforce_proposal_expiry(
        "p1",
        proposal_store=store,
        expected_account_id="acct_a",
    )
    assert record.expires_at == expires


# ---------------------------------------------------------------------------
# validate_capability_overlap (D4)
# ---------------------------------------------------------------------------


def _make_package(
    *,
    product_id: str,
    pricing_model: str | None = None,
    targeting_overlay: dict[str, Any] | None = None,
    delivery_type: str | None = None,
    signal_type: str | None = None,
) -> Any:
    """Build a mock package object the validator inspects via getattr."""
    pkg = MagicMock(spec=[])
    pkg.product_id = product_id
    pkg._resolved_pricing_model = pricing_model
    pkg.targeting_overlay = targeting_overlay
    pkg._resolved_delivery_type = delivery_type
    pkg.signal_type = signal_type
    return pkg


def test_overlap_validation_pricing_model_rejected() -> None:
    """Buyer asks for cpcv but the recipe overlap allows only cpm."""
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(pricing_models=frozenset({"cpm"})),
    )
    pkg = _make_package(product_id="prod_1", pricing_model="cpcv")
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "packages[0].pricing_option_id"


def test_overlap_validation_pricing_model_accepted() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(pricing_models=frozenset({"cpm"})),
    )
    pkg = _make_package(product_id="prod_1", pricing_model="cpm")
    validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})


def test_overlap_validation_targeting_dimension_rejected() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(
            targeting_dimensions=frozenset({"geo", "device_type"}),
        ),
    )
    pkg = _make_package(
        product_id="prod_1",
        targeting_overlay={"geo": ["US"], "language": ["en"]},
    )
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "packages[0].targeting_overlay"


def test_overlap_validation_delivery_type_rejected() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(
            delivery_types=frozenset({"guaranteed"}),
        ),
    )
    pkg = _make_package(product_id="prod_1", delivery_type="non_guaranteed")
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "packages[0].delivery_type"


def test_overlap_validation_signal_type_rejected() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(
            signal_types=frozenset({"audience"}),
        ),
    )
    pkg = _make_package(product_id="prod_1", signal_type="contextual")
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "packages[0].signal_type"


def test_overlap_validation_empty_frozenset_means_deny_all() -> None:
    """frozenset() (empty) is enforced as deny-all — distinguishes from
    None (no constraint)."""
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(signal_types=frozenset()),
    )
    pkg = _make_package(product_id="prod_1", signal_type="audience")
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})
    assert exc.value.code == "INVALID_REQUEST"


def test_overlap_validation_none_axis_is_noop() -> None:
    """None on an axis means "no framework gate" — the v1 legacy posture.
    The buyer can pick anything on that axis."""
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(
            pricing_models=None,  # no gate
            targeting_dimensions=frozenset({"geo"}),
        ),
    )
    pkg = _make_package(
        product_id="prod_1",
        pricing_model="anything-the-buyer-wants",
        targeting_overlay={"geo": ["US"]},
    )
    validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})


def test_overlap_validation_no_recipe_is_noop() -> None:
    """Packages whose product_id is not in recipes (e.g. legacy buys
    without a proposal lifecycle) skip the gate entirely."""
    pkg = _make_package(product_id="prod_unknown", pricing_model="anything")
    validate_capability_overlap(packages=[pkg], recipes={})


def test_overlap_validation_recipe_without_overlap_is_noop() -> None:
    """recipe.capability_overlap=None — v1.5 back-compat. Adopter
    didn't declare an overlap on this recipe; framework doesn't gate."""
    recipe = _DemoRecipe(capability_overlap=None)
    pkg = _make_package(product_id="prod_1", pricing_model="anything")
    validate_capability_overlap(packages=[pkg], recipes={"prod_1": recipe})


def test_overlap_validation_field_path_prefix_for_update_path() -> None:
    """update_media_buy callers pass a different field-path prefix to
    match their wire shape (the wire envelope is different from
    create_media_buy's packages[])."""
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(pricing_models=frozenset({"cpm"})),
    )
    pkg = _make_package(product_id="prod_1", pricing_model="cpcv")
    with pytest.raises(AdcpError) as exc:
        validate_capability_overlap(
            packages=[pkg],
            recipes={"prod_1": recipe},
            field_path_prefix="patch.packages",
        )
    assert exc.value.field == "patch.packages[0].pricing_option_id"


# ---------------------------------------------------------------------------
# validate_overlap_subset_of_wire (D4 round-4)
# ---------------------------------------------------------------------------


def _make_product(
    *,
    product_id: str,
    pricing_models: list[str],
    delivery_type: str | None = None,
) -> Any:
    """Mock wire Product for the subset check."""
    product = MagicMock(spec=[])
    product.product_id = product_id
    product.pricing_options = [MagicMock(pricing_model=p) for p in pricing_models]
    product.delivery_type = delivery_type
    return product


def test_overlap_subset_check_accepts_strict_subset() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(pricing_models=frozenset({"cpm"})),
    )
    product = _make_product(product_id="prod_1", pricing_models=["cpm", "cpcv"])
    validate_overlap_subset_of_wire(
        recipes={"prod_1": recipe},
        products=[product],
    )


def test_overlap_subset_check_rejects_overlap_exceeds_wire() -> None:
    """Adopter declares overlap.pricing_models={cpm, cpcv} but the wire
    only advertises cpm — drift / config bug. Framework raises
    INTERNAL_ERROR (adopter, not buyer)."""
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(pricing_models=frozenset({"cpm", "cpcv"})),
    )
    product = _make_product(product_id="prod_1", pricing_models=["cpm"])
    with pytest.raises(AdcpError) as exc:
        validate_overlap_subset_of_wire(
            recipes={"prod_1": recipe},
            products=[product],
        )
    assert exc.value.code == "INTERNAL_ERROR"


def test_overlap_subset_check_no_overlap_is_noop() -> None:
    """Recipe without capability_overlap → no subset check needed."""
    recipe = _DemoRecipe(capability_overlap=None)
    product = _make_product(product_id="prod_1", pricing_models=["cpm"])
    validate_overlap_subset_of_wire(
        recipes={"prod_1": recipe},
        products=[product],
    )


def test_overlap_subset_check_delivery_type_drift_rejected() -> None:
    recipe = _DemoRecipe(
        capability_overlap=CapabilityOverlap(
            delivery_types=frozenset({"guaranteed", "non_guaranteed"}),
        ),
    )
    product = _make_product(product_id="prod_1", pricing_models=["cpm"], delivery_type="guaranteed")
    with pytest.raises(AdcpError) as exc:
        validate_overlap_subset_of_wire(
            recipes={"prod_1": recipe},
            products=[product],
        )
    assert exc.value.code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# detect_finalize_action
# ---------------------------------------------------------------------------


def test_detect_finalize_action_finds_proposal_finalize_entry() -> None:
    """Refine entry with scope='proposal' and action='finalize' is
    surfaced as (index, proposal_id, ask). Index lets the framework
    emit indexed wire-field paths on rejection."""
    req = MagicMock(spec=["refine"])
    entry_root = MagicMock(spec=[])
    entry_root.scope = "proposal"
    entry_root.action = "finalize"
    entry_root.proposal_id = "p_42"
    entry_root.ask = "lock pricing"
    entry = MagicMock(spec=["root"])
    entry.root = entry_root
    req.refine = [entry]

    result = detect_finalize_action(req)
    assert result == (0, "p_42", "lock pricing")


def test_detect_finalize_action_no_finalize_returns_none() -> None:
    """Refine entries without action='finalize' return None."""
    req = MagicMock(spec=["refine"])
    entry_root = MagicMock(spec=[])
    entry_root.scope = "request"
    entry_root.action = None
    entry_root.proposal_id = None
    entry = MagicMock(spec=["root"])
    entry.root = entry_root
    req.refine = [entry]
    assert detect_finalize_action(req) is None


def test_detect_finalize_action_empty_refine_returns_none() -> None:
    req = MagicMock(spec=["refine"])
    req.refine = None
    assert detect_finalize_action(req) is None
    req.refine = []
    assert detect_finalize_action(req) is None


def test_detect_finalize_action_picks_first_finalize() -> None:
    """If multiple finalize entries exist, only the first is processed.
    v1.5 doesn't support multi-finalize."""
    req = MagicMock(spec=["refine"])
    entry1_root = MagicMock(spec=[])
    entry1_root.scope = "proposal"
    entry1_root.action = "finalize"
    entry1_root.proposal_id = "p_first"
    entry1_root.ask = None
    entry2_root = MagicMock(spec=[])
    entry2_root.scope = "proposal"
    entry2_root.action = "finalize"
    entry2_root.proposal_id = "p_second"
    entry2_root.ask = None
    e1 = MagicMock(spec=["root"])
    e1.root = entry1_root
    e2 = MagicMock(spec=["root"])
    e2.root = entry2_root
    req.refine = [e1, e2]
    assert detect_finalize_action(req) == (0, "p_first", None)
