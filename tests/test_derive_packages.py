"""Tests for framework-level package derivation from proposal allocations.

Covers:

* :func:`derive_packages_from_proposal` — pure-function derivation math.
* Error handling — missing fields surface as ``INVALID_REQUEST``.
* Integration with ``maybe_hydrate_recipes_for_create_media_buy``:
    * Auto-derivation runs when ``req.packages`` is empty.
    * ``ProposalManager.derive_packages`` override is preferred.
    * Failed derivation releases the reservation back to COMMITTED.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    InMemoryProposalStore,
    ProposalCapabilities,
    derive_packages_from_proposal,
)
from adcp.decisioning.context import AuthInfo, RequestContext
from adcp.decisioning.proposal_dispatch import (
    maybe_hydrate_recipes_for_create_media_buy,
)
from adcp.decisioning.registry import ApiKeyCredential
from adcp.decisioning.types import Account


def _utc(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# derive_packages_from_proposal — pure-function tests
# ---------------------------------------------------------------------------


def test_derive_packages_even_split() -> None:
    payload = {
        "allocations": [
            {
                "product_id": "prod_1",
                "allocation_percentage": 60.0,
                "pricing_option_id": "po_a",
            },
            {
                "product_id": "prod_2",
                "allocation_percentage": 40.0,
                "pricing_option_id": "po_b",
            },
        ]
    }
    total_budget = {"amount": 1000.0, "currency": "USD"}
    packages = derive_packages_from_proposal(payload, total_budget)
    assert len(packages) == 2
    assert packages[0].product_id == "prod_1"
    assert packages[0].budget == 600.0
    assert packages[0].pricing_option_id == "po_a"
    assert packages[1].product_id == "prod_2"
    assert packages[1].budget == 400.0
    assert packages[1].pricing_option_id == "po_b"


def test_derive_packages_propagates_allocation_flight_times() -> None:
    """When the seller's allocation carries start_time / end_time,
    the derived package MUST inherit them — they encode per-flight
    scheduling within the proposal (spec)."""
    payload = {
        "allocations": [
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                "pricing_option_id": "po_a",
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-04-30T23:59:59Z",
            }
        ]
    }
    packages = derive_packages_from_proposal(payload, {"amount": 1000.0, "currency": "USD"})
    assert packages[0].start_time is not None
    assert packages[0].end_time is not None


def test_derive_packages_accepts_typed_total_budget() -> None:
    """Typed ``TotalBudget`` from the wire model works the same as a dict."""
    from adcp.types import PackageRequest
    from adcp.types.generated_poc.media_buy.create_media_buy_request import TotalBudget

    payload = {
        "allocations": [
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                "pricing_option_id": "po_a",
            },
        ]
    }
    total_budget = TotalBudget(amount=500.0, currency="USD")
    packages = derive_packages_from_proposal(payload, total_budget)
    assert isinstance(packages[0], PackageRequest)
    assert packages[0].budget == 500.0


def test_derive_packages_missing_total_budget_raises() -> None:
    payload = {"allocations": [{"product_id": "p1", "allocation_percentage": 100.0}]}
    with pytest.raises(AdcpError) as exc:
        derive_packages_from_proposal(payload, None)
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "total_budget"


def test_derive_packages_empty_allocations_raises() -> None:
    with pytest.raises(AdcpError) as exc:
        derive_packages_from_proposal({"allocations": []}, {"amount": 100.0, "currency": "USD"})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "proposal_id"


def test_derive_packages_missing_allocations_key_raises() -> None:
    with pytest.raises(AdcpError) as exc:
        derive_packages_from_proposal({}, {"amount": 100.0, "currency": "USD"})
    assert exc.value.code == "INVALID_REQUEST"


def test_enrich_allocations_picks_single_pricing_option() -> None:
    """When a product has exactly one pricing_options[] entry and the
    allocation omits pricing_option_id, the framework picks the single
    option at proposal-persist time. The downstream derivation then
    sees a fully-populated allocation."""
    from adcp.decisioning.proposal_dispatch import (
        _enrich_allocations_with_pricing_options,
    )

    payload = {
        "allocations": [
            {"product_id": "prod_a", "allocation_percentage": 100.0},
        ]
    }
    products = [
        {
            "product_id": "prod_a",
            "pricing_options": [{"pricing_option_id": "po_only"}],
        },
    ]
    _enrich_allocations_with_pricing_options(payload, products)
    assert payload["allocations"][0]["pricing_option_id"] == "po_only"


def test_enrich_allocations_skips_multi_option_products() -> None:
    """Multi-option products are ambiguous — framework leaves the
    allocation alone so the seller's derive_packages override (or
    proposal-assembly logic) handles selection."""
    from adcp.decisioning.proposal_dispatch import (
        _enrich_allocations_with_pricing_options,
    )

    payload = {
        "allocations": [
            {"product_id": "prod_a", "allocation_percentage": 100.0},
        ]
    }
    products = [
        {
            "product_id": "prod_a",
            "pricing_options": [
                {"pricing_option_id": "po_one"},
                {"pricing_option_id": "po_two"},
            ],
        },
    ]
    _enrich_allocations_with_pricing_options(payload, products)
    assert "pricing_option_id" not in payload["allocations"][0]


def test_derive_packages_missing_pricing_option_raises_seller_error() -> None:
    """ProductAllocation.pricing_option_id is optional on the wire but
    PackageRequest.pricing_option_id is required. When the seller's
    proposal omits it AND the framework can't auto-pick (multiple
    pricing options, no product context), surface as INTERNAL_ERROR —
    not buyer-correctable. The buyer can't fix a seller-side gap."""
    payload = {
        "allocations": [
            {"product_id": "prod_1", "allocation_percentage": 100.0},
        ]
    }
    with pytest.raises(AdcpError) as exc:
        derive_packages_from_proposal(payload, {"amount": 1000.0, "currency": "USD"})
    assert exc.value.code == "INTERNAL_ERROR"
    assert "Seller configuration error" in str(exc.value)


def test_derive_packages_missing_product_id_raises() -> None:
    payload = {
        "allocations": [
            {"allocation_percentage": 100.0, "pricing_option_id": "po"},
        ]
    }
    with pytest.raises(AdcpError) as exc:
        derive_packages_from_proposal(payload, {"amount": 1000.0, "currency": "USD"})
    assert exc.value.code == "INVALID_REQUEST"
    assert "product_id" in str(exc.value)


# ---------------------------------------------------------------------------
# Integration — auto-derivation in maybe_hydrate_recipes_for_create_media_buy
# ---------------------------------------------------------------------------


class _DerivingManager:
    """Bare-minimum manager that opts into framework derivation via
    ``derive_packages_from_allocations``. Tests covering the built-in
    helper path use this; tests covering the override path subclass it
    to add a ``derive_packages`` method.
    """

    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
        derive_packages_from_allocations=True,
    )


class _Platform:
    """Minimal platform exposing per-tenant store / manager hooks.

    Mirrors the duck-typed router introspection in
    :func:`_resolve_manager_and_store`.
    """

    def __init__(
        self,
        store: InMemoryProposalStore,
        manager: Any | None = None,
    ) -> None:
        self._store = store
        # Default to a manager that opts into derivation so the bulk of
        # tests exercise the auto-injection path. Tests covering the
        # "off" semantics pass ``manager=None`` or a manager without
        # the flag.
        self._manager = manager if manager is not None else _DerivingManager()

    def proposal_store_for_tenant(self, tenant_id: str) -> InMemoryProposalStore:
        del tenant_id
        return self._store

    def proposal_manager_for_tenant(self, tenant_id: str) -> Any | None:
        del tenant_id
        return self._manager


def _ctx(account_id: str = "acct_a", tenant_id: str = "t1") -> RequestContext[Any]:
    """Build a RequestContext with the tenant_id metadata the dispatcher
    looks up."""
    return RequestContext(
        account=Account(id=account_id, metadata={"tenant_id": tenant_id}),
        auth_info=AuthInfo(
            kind="api_key",
            key_id="kid_1",
            principal="agent.example.com",
            credential=ApiKeyCredential(kind="api_key", key_id="kid_1"),
        ),
    )


async def _seed_committed_proposal(
    store: InMemoryProposalStore,
    *,
    proposal_id: str = "p1",
    account_id: str = "acct_a",
    publisher_id: str | None = "t1",
    allocations: list[dict[str, Any]] | None = None,
) -> None:
    payload = {"proposal_id": proposal_id, "proposal_status": "committed"}
    if allocations is not None:
        payload["allocations"] = allocations
    await store.put_draft(
        proposal_id=proposal_id,
        account_id=account_id,
        publisher_id=publisher_id,
        recipes={},
        proposal_payload=payload,
    )
    await store.commit(
        proposal_id,
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload=payload,
        expected_account_id=account_id,
        expected_publisher_id=publisher_id,
    )


class _CreateMediaBuyParams:
    """Minimal duck-typed stand-in for ``CreateMediaBuyRequest``.

    The auto-derivation block mutates ``packages``; we verify the
    assignment happens. Using a plain class keeps the test independent
    of the full Pydantic model (which carries many required fields like
    ``brand`` / ``idempotency_key`` we don't need to exercise the
    derivation path).
    """

    def __init__(
        self,
        proposal_id: str,
        total_budget: Any | None,
        packages: list[Any] | None = None,
    ) -> None:
        self.proposal_id = proposal_id
        self.total_budget = total_budget
        self.packages = packages


@pytest.mark.asyncio
async def test_derivation_off_by_default_no_manager() -> None:
    """Without a manager wired, the framework does not auto-derive —
    preserves pre-#727 semantics for adopters who haven't opted in."""

    class _BareLookup:
        def __init__(self, store: InMemoryProposalStore) -> None:
            self._store = store

        def proposal_store_for_tenant(self, _tid: str) -> InMemoryProposalStore:
            return self._store

        def proposal_manager_for_tenant(self, _tid: str) -> Any | None:
            return None

    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                "pricing_option_id": "po_a",
            }
        ],
    )
    platform = _BareLookup(store)
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=None,
    )
    await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    # Derivation did NOT run.
    assert params.packages is None


@pytest.mark.asyncio
async def test_derivation_off_by_default_when_flag_false() -> None:
    """A manager without the opt-in flag and without a derive_packages
    override does not trigger derivation."""

    class _OffManager:
        capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                "pricing_option_id": "po_a",
            }
        ],
    )
    platform = _Platform(store, manager=_OffManager())
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=None,
    )
    await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert params.packages is None


@pytest.mark.asyncio
async def test_auto_derives_packages_from_allocations() -> None:
    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            {
                "product_id": "prod_1",
                "allocation_percentage": 70.0,
                "pricing_option_id": "po_a",
            },
            {
                "product_id": "prod_2",
                "allocation_percentage": 30.0,
                "pricing_option_id": "po_b",
            },
        ],
    )
    platform = _Platform(store)
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=None,
    )
    record = await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert record is not None
    assert params.packages is not None
    assert len(params.packages) == 2
    assert params.packages[0].product_id == "prod_1"
    assert params.packages[0].budget == 700.0
    assert params.packages[1].budget == 300.0


@pytest.mark.asyncio
async def test_existing_packages_are_not_overridden() -> None:
    """Buyer-supplied packages take precedence — no derivation runs."""
    from adcp.types import PackageRequest

    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                "pricing_option_id": "po_a",
            }
        ],
    )
    platform = _Platform(store)
    explicit = [PackageRequest(product_id="other", budget=42.0, pricing_option_id="po_x")]
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=list(explicit),
    )
    await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert params.packages is not None
    assert len(params.packages) == 1
    assert params.packages[0].product_id == "other"
    assert params.packages[0].budget == 42.0


@pytest.mark.asyncio
async def test_proposal_without_allocations_is_a_noop() -> None:
    """Legacy proposals without ``allocations[]`` skip derivation; the
    buyer's empty packages stay empty (adapter handles)."""
    store = InMemoryProposalStore()
    await _seed_committed_proposal(store, allocations=None)
    platform = _Platform(store)
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget=None,
        packages=None,
    )
    record = await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert record is not None
    assert params.packages is None  # untouched


@pytest.mark.asyncio
async def test_derivation_failure_releases_reservation() -> None:
    """Derivation raises (e.g. missing pricing_option_id) → the proposal
    must roll back to COMMITTED so the buyer can retry."""
    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            # Missing pricing_option_id — triggers INVALID_REQUEST.
            {"product_id": "prod_1", "allocation_percentage": 100.0},
        ],
    )
    platform = _Platform(store)
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=None,
    )
    with pytest.raises(AdcpError) as exc:
        await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert exc.value.code == "INTERNAL_ERROR"
    # Reservation released back to COMMITTED.
    from adcp.decisioning.proposal_store import ProposalState

    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None
    assert record.state == ProposalState.COMMITTED


# ---------------------------------------------------------------------------
# Override hook — ProposalManager.derive_packages
# ---------------------------------------------------------------------------


class _CustomDerivationManager:
    """Manager declaring a ``derive_packages`` override that uses
    auction-style ``bid_price`` instead of even-percentage split."""

    capabilities = ProposalCapabilities(sales_specialism="sales-non-guaranteed")

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def derive_packages(
        self,
        *,
        proposal_payload: Mapping[str, Any],
        total_budget: Any,
        recipes: Mapping[str, Any],
    ) -> list[Any]:
        from adcp.types import PackageRequest

        self.calls.append({"proposal_payload": dict(proposal_payload)})
        del recipes
        budget = total_budget["amount"] if total_budget else 100.0
        return [
            PackageRequest(
                product_id=str(a["product_id"]),
                budget=budget,  # custom: full budget per product, not split
                pricing_option_id="po_custom",
                bid_price=5.0,
            )
            for a in proposal_payload["allocations"]
        ]


@pytest.mark.asyncio
async def test_manager_derive_packages_override_is_preferred() -> None:
    """When the manager declares ``derive_packages``, the framework
    dispatches there instead of the built-in helper."""
    store = InMemoryProposalStore()
    await _seed_committed_proposal(
        store,
        allocations=[
            {
                "product_id": "prod_1",
                "allocation_percentage": 100.0,
                # Note: no pricing_option_id — would fail the default
                # helper but the override supplies one.
            },
        ],
    )
    manager = _CustomDerivationManager()
    platform = _Platform(store, manager=manager)
    params = _CreateMediaBuyParams(
        proposal_id="p1",
        total_budget={"amount": 1000.0, "currency": "USD"},
        packages=None,
    )
    await maybe_hydrate_recipes_for_create_media_buy(platform, params, _ctx())
    assert len(manager.calls) == 1
    assert params.packages is not None
    assert len(params.packages) == 1
    assert params.packages[0].budget == 1000.0
    assert params.packages[0].pricing_option_id == "po_custom"
    assert params.packages[0].bid_price == 5.0
