"""Async (handoff) discovery for get_products / get_signals — issue #924.

Tests the typed-API + dispatch contract for promoting ``get_products`` and
``get_signals`` to long-running tasks via ``ctx.handoff_to_task(fn)``, plus
the four rejection guards that keep async discovery spec-conformant.

Tests exercise the public API (the ``PlatformHandler`` shim and the
``adcp.types`` / ``adcp.decisioning`` exports) and validate against the real
generated Pydantic models — never against handler internals.

Reference: JS adcp-client#2170 (rc8 async discovery parity).
"""

from __future__ import annotations

import asyncio
import typing
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.types import Account
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-discovery-")
    yield pool
    pool.shutdown(wait=True)


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry | None = None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=registry or InMemoryTaskRegistry(),
    )


class _UnresolvedAccounts:
    """AccountStore that resolves every request to the ``'<unset>'``
    sentinel id — models the accountless/derived-miss case for guard (c)."""

    def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
        del ref, auth_info
        return Account(id="<unset>")


# ---------------------------------------------------------------------------
# Response-union membership (typed API surface)
# ---------------------------------------------------------------------------


def test_get_products_union_includes_all_three_async_arms() -> None:
    """``GetProductsResponseUnion`` carries the sync success arm plus the
    submitted / working / input_required async arms the rc.9 spec ships."""
    from adcp.types import (
        GetProductsInputRequiredResponse,
        GetProductsResponse,
        GetProductsResponseUnion,
        GetProductsSubmittedResponse,
        GetProductsWorkingResponse,
    )

    arms = set(typing.get_args(GetProductsResponseUnion))
    assert GetProductsResponse in arms
    assert GetProductsSubmittedResponse in arms
    assert GetProductsWorkingResponse in arms
    assert GetProductsInputRequiredResponse in arms


def test_get_signals_union_has_submitted_working_but_not_input_required() -> None:
    """``get_signals`` ships ONLY submitted + working — no input_required
    arm (signal discovery cannot pause to solicit buyer clarification)."""
    from adcp.types import (
        GetSignalsResponse,
        GetSignalsResponseUnion,
        GetSignalsSubmittedResponse,
        GetSignalsWorkingResponse,
    )

    arms = set(typing.get_args(GetSignalsResponseUnion))
    assert GetSignalsResponse in arms
    assert GetSignalsSubmittedResponse in arms
    assert GetSignalsWorkingResponse in arms
    # No input_required arm exists for signals at all.
    import adcp.types as adcp_types

    assert not hasattr(adcp_types, "GetSignalsInputRequiredResponse")
    assert not hasattr(adcp_types, "GetSignalsInputRequired")
    arm_names = {a.__name__ for a in arms}
    assert not any("InputRequired" in n for n in arm_names)


def test_input_required_arm_exists_for_products_only() -> None:
    """get_products has an input_required arm; get_signals does not."""
    from adcp.types import GetProductsInputRequiredResponse

    # Reason enum on the products input_required arm is real.
    assert "reason" in GetProductsInputRequiredResponse.model_fields


def test_submitted_arms_validate_the_wire_submitted_envelope() -> None:
    """The submitted arm classes accept the {task_id, status='submitted'}
    wire shape via real .model_validate()."""
    from adcp.types import GetProductsSubmittedResponse, GetSignalsSubmittedResponse

    p = GetProductsSubmittedResponse.model_validate({"status": "submitted", "task_id": "task_p1"})
    assert p.status == "submitted"
    assert p.task_id == "task_p1"

    s = GetSignalsSubmittedResponse.model_validate({"status": "submitted", "task_id": "task_s1"})
    assert s.status == "submitted"
    assert s.task_id == "task_s1"


def test_discovery_result_alias_mirrors_sales_result() -> None:
    """DiscoveryResult[T] has the same arm structure as SalesResult[T]."""
    from adcp.decisioning import DiscoveryResult, SalesResult

    # Both are TypeAliasType — same underlying union string.
    assert DiscoveryResult.__value__ == SalesResult.__value__


# ---------------------------------------------------------------------------
# Async handoff — submitted envelope on submit, terminal result on completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_handoff_returns_submitted_envelope(executor) -> None:
    """Adopter returns ctx.handoff_to_task(fn) → handler returns the wire
    Submitted envelope ({task_id, status}), NOT a GetProductsResponse."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    async def _curate(task_ctx):
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    handler = _make_handler(_Platform(), executor)
    result = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="ctv inventory"),
        ToolContext(),
    )
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert result["task_id"].startswith("task_")
    assert set(result.keys()) == {"task_id", "status"}


@pytest.mark.asyncio
async def test_get_products_handoff_completion_lands_in_registry(executor) -> None:
    """The background task completes the registry row with task_type
    'get_products' and the terminal GetProductsResponse artifact (the
    shape a buyer reads via tasks/get)."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    completed = asyncio.Event()

    async def _curate(task_ctx):
        completed.set()
        # Empty products list is wire-valid; the terminal artifact shape is
        # what matters for the registry-completion assertion.
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    registry = InMemoryTaskRegistry()
    handler = _make_handler(_Platform(), executor, registry=registry)
    envelope = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="ctv inventory"),
        ToolContext(),
    )
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    rec = await registry.get(envelope["task_id"])
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["task_type"] == "get_products"
    assert "products" in rec["result"]


@pytest.mark.asyncio
async def test_get_signals_handoff_returns_submitted_envelope(executor) -> None:
    from adcp.types import GetSignalsRequest, GetSignalsResponse

    async def _discover(task_ctx):
        return GetSignalsResponse(signals=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return ctx.handoff_to_task(_discover)

    handler = _make_handler(_Platform(), executor)
    result = await handler.get_signals(
        GetSignalsRequest(discovery_mode="brief", signal_spec="auto intenders"),
        ToolContext(),
    )
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert result["task_id"].startswith("task_")


@pytest.mark.asyncio
async def test_get_signals_handoff_completion_lands_in_registry(executor) -> None:
    from adcp.types import GetSignalsRequest, GetSignalsResponse

    completed = asyncio.Event()

    async def _discover(task_ctx):
        completed.set()
        return GetSignalsResponse(signals=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return ctx.handoff_to_task(_discover)

    registry = InMemoryTaskRegistry()
    handler = _make_handler(_Platform(), executor, registry=registry)
    envelope = await handler.get_signals(
        GetSignalsRequest(discovery_mode="brief", signal_spec="auto intenders"),
        ToolContext(),
    )
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    rec = await registry.get(envelope["task_id"])
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["task_type"] == "get_signals"


# ---------------------------------------------------------------------------
# Sync path unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_sync_path_no_task_id(executor) -> None:
    """A sync GetProductsResponse return has no task_id and is returned as
    the typed model (no behavior change)."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    resp = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="x"),
        ToolContext(),
    )
    assert isinstance(resp, GetProductsResponse)
    assert not hasattr(resp, "task_id") or resp.task_id is None  # type: ignore[attr-defined]
    assert resp.products == []


@pytest.mark.asyncio
async def test_get_signals_sync_path_no_task_id(executor) -> None:
    from adcp.types import GetSignalsRequest, GetSignalsResponse

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return GetSignalsResponse(signals=[])

    handler = _make_handler(_Platform(), executor)
    resp = await handler.get_signals(
        GetSignalsRequest(discovery_mode="brief", signal_spec="x"),
        ToolContext(),
    )
    assert isinstance(resp, GetSignalsResponse)


class _RecordingStore:
    """Minimal ProposalStore that records put_draft calls (including the
    full persisted proposal_payload + recipes, so a regression test can
    pin that pagination / fields projection on the wire response never
    strips data from the stored draft)."""

    def __init__(self) -> None:
        self.put_calls: list[dict[str, Any]] = []

    async def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        recipes: Any,
        proposal_payload: Any,
    ) -> None:
        self.put_calls.append(
            {
                "proposal_id": proposal_id,
                "account_id": account_id,
                "recipes": recipes,
                "proposal_payload": proposal_payload,
            }
        )


class _StoreBackedAccounts:
    """Resolves a tenant-scoped account carrying ``tenant_id`` in metadata
    so the proposal-store resolver finds the wired store."""

    def resolve(self, ref: Any = None, auth_info: Any = None) -> Account[Any]:
        del ref, auth_info
        return Account(id="seller:acct_1", metadata={"tenant_id": "default"})


def _store_backed_platform(store: _RecordingStore, handoff: bool):
    from adcp.types import GetProductsResponse

    _proposals = [
        {
            "proposal_id": "prop_1",
            "name": "plan",
            "allocations": [{"product_id": "p1", "allocation_percentage": 100}],
        }
    ]
    _products: list[Any] = []

    async def _curate(task_ctx):
        return GetProductsResponse(products=_products, proposals=_proposals)

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = _StoreBackedAccounts()

        def proposal_store_for_tenant(self, tenant_id: str):
            return store

        async def get_products(self, req, ctx):
            if handoff:
                return ctx.handoff_to_task(_curate)
            return GetProductsResponse(products=_products, proposals=_proposals)

    return _Platform()


@pytest.mark.asyncio
async def test_get_products_sync_draft_persist_still_fires(executor) -> None:
    """The persist-draft terminal side-effect runs on the sync completion
    path (threaded as on_complete) — proposals reach the wired store."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    store = _RecordingStore()
    handler = _make_handler(_store_backed_platform(store, handoff=False), executor)
    resp = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="x"),
        ToolContext(),
    )
    assert isinstance(resp, GetProductsResponse)
    assert [c["proposal_id"] for c in store.put_calls] == ["prop_1"]


@pytest.mark.asyncio
async def test_get_products_handoff_draft_persist_runs_on_completion(executor) -> None:
    """The persist-draft side-effect runs on the handoff COMPLETION path —
    threaded as on_complete so it fires when the bg task lands, not at
    submit time."""
    from adcp.types import GetProductsRequest

    store = _RecordingStore()
    registry = InMemoryTaskRegistry()
    platform = _store_backed_platform(store, handoff=True)
    handler = _make_handler(platform, executor, registry=registry)
    envelope = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="x"),
        ToolContext(),
    )
    # At submit time the side-effect has NOT run yet.
    assert isinstance(envelope, dict) and envelope["status"] == "submitted"
    # Drain the background task.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if store.put_calls:
            break
    assert [c["proposal_id"] for c in store.put_calls] == [
        "prop_1"
    ], "persist-draft on_complete hook did not fire on the handoff completion path"
    rec = await registry.get(envelope["task_id"])
    assert rec is not None and rec["state"] == "completed"


# ---------------------------------------------------------------------------
# Guard (a): wholesale + push_notification_config — pre-dispatch reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_wholesale_push_rejected_predispatch(executor) -> None:
    from adcp.types import GetProductsRequest

    call_count = {"n": 0}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            call_count["n"] += 1
            raise AssertionError("platform method must not be invoked")

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(
                buying_mode="wholesale",
                push_notification_config={"url": "https://buyer.example.com/wh"},
            ),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "push_notification_config"
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_get_signals_wholesale_push_rejected_predispatch(executor) -> None:
    from adcp.types import GetSignalsRequest

    call_count = {"n": 0}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            call_count["n"] += 1
            raise AssertionError("platform method must not be invoked")

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_signals(
            GetSignalsRequest(
                discovery_mode="wholesale",
                push_notification_config={"url": "https://buyer.example.com/wh"},
            ),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "push_notification_config"
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# Guard (b): wholesale + adopter handoff — post-dispatch reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_wholesale_handoff_rejected(executor) -> None:
    from adcp.types import GetProductsRequest

    async def _curate(task_ctx):
        return {"products": []}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="wholesale"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "buying_mode"


@pytest.mark.asyncio
async def test_get_signals_wholesale_handoff_rejected(executor) -> None:
    from adcp.types import GetSignalsRequest

    async def _discover(task_ctx):
        return {"signals": []}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return ctx.handoff_to_task(_discover)

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_signals(
            GetSignalsRequest(discovery_mode="wholesale"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "discovery_mode"


# ---------------------------------------------------------------------------
# Guard (c): async + unresolved account — field='account'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_push_unresolved_account_rejected(executor) -> None:
    """brief + push_notification_config against an unresolved (sentinel)
    account is rejected with field='account' before dispatch."""
    from adcp.types import GetProductsRequest

    call_count = {"n": 0}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = _UnresolvedAccounts()

        async def get_products(self, req, ctx):
            call_count["n"] += 1
            from adcp.types import GetProductsResponse

            return GetProductsResponse(products=[])

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(
                buying_mode="brief",
                brief="x",
                push_notification_config={"url": "https://buyer.example.com/wh"},
            ),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "account"
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_get_signals_push_unresolved_account_rejected(executor) -> None:
    from adcp.types import GetSignalsRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = _UnresolvedAccounts()

        async def get_signals(self, req, ctx):
            from adcp.types import GetSignalsResponse

            return GetSignalsResponse(signals=[])

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_signals(
            GetSignalsRequest(
                discovery_mode="brief",
                signal_spec="x",
                push_notification_config={"url": "https://buyer.example.com/wh"},
            ),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"
    assert exc.value.field == "account"


# ---------------------------------------------------------------------------
# Guard (d): hand-rolled submitted dict from the sync arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_hand_rolled_submitted_rejected(executor) -> None:
    """Adopter returns a literal {'status':'submitted', ...} with extra
    keys (bypassing ctx.handoff_to_task) → guiding INVALID_REQUEST."""
    from adcp.types import GetProductsRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return {"status": "submitted", "task_id": "hand_rolled_1", "products": []}

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="x"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"


@pytest.mark.asyncio
async def test_get_signals_hand_rolled_submitted_rejected(executor) -> None:
    from adcp.types import GetSignalsRequest

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return {"status": "submitted", "task_id": "hand_rolled_2", "signals": []}

    handler = _make_handler(_Platform(), executor)
    with pytest.raises(AdcpError) as exc:
        await handler.get_signals(
            GetSignalsRequest(discovery_mode="brief", signal_spec="x"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.recovery == "correctable"


# ---------------------------------------------------------------------------
# task-type enum + wire validation
# ---------------------------------------------------------------------------


def test_task_type_enum_includes_discovery_verbs() -> None:
    """The generated TaskType enum (which validates tasks_get / list_tasks /
    webhook task_type) carries both discovery verbs."""
    from adcp.types.generated_poc.enums.task_type import TaskType

    values = {t.value for t in TaskType}
    assert "get_products" in values
    assert "get_signals" in values


def test_wire_validator_accepts_submitted_for_discovery_verbs() -> None:
    """Strict response validation selects the submitted variant and passes
    the {task_id, status='submitted'} envelope for both discovery verbs.

    The rc.9 bundle ships the submitted schema for both verbs, so the
    validator MUST actually select the ``submitted`` variant (not fall back
    to the ``skipped`` no-bundle path) and report it valid. Asserting on the
    variant closes the escape hatch where an absent bundle would silently
    pass the looser ``outcome.valid`` check.
    """
    from adcp.validation.schema_validator import validate_response

    for tool in ("get_products", "get_signals"):
        outcome = validate_response(tool, {"task_id": "task_x", "status": "submitted"})
        assert (
            outcome.valid is True
        ), f"{tool} submitted envelope failed validation: {outcome.issues}"
        assert outcome.variant == "submitted", (
            f"{tool} validated against the {outcome.variant!r} variant, not the "
            "rc.9 submitted schema — the bundle should be present, not skipped"
        )


# ---------------------------------------------------------------------------
# SHOULD-FIX 2: persisted draft retains full product recipe / pricing data
# even when fields= / pagination= shape the wire response
# ---------------------------------------------------------------------------

# A wire-valid Product carrying full pricing — the data the persisted draft
# must retain regardless of the buyer-presentation projection applied to the
# response.
_RICH_PRODUCT: dict[str, Any] = {
    "product_id": "p1",
    "name": "Premium CTV",
    "description": "Premium connected TV inventory",
    "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
    "format_options": [
        {
            "format_option_id": "p1-display",
            "format_kind": "image",
            "params": {"sizes": [{"width": 300, "height": 250}]},
        }
    ],
    "delivery_type": "non_guaranteed",
    "pricing_options": [
        {"pricing_model": "cpm", "pricing_option_id": "po1", "currency": "USD", "rate": 42.5}
    ],
    "reporting_capabilities": {
        "available_reporting_frequencies": ["daily"],
        "expected_delay_minutes": 60,
        "timezone": "UTC",
        "supports_webhooks": False,
        "available_metrics": ["impressions"],
        "date_range_support": "date_range",
    },
}


@pytest.mark.asyncio
async def test_persisted_draft_keeps_full_product_data_under_projection(executor) -> None:
    """A sync get_products with proposals + a recording store, where the
    request applies fields= AND pagination= projections: the persisted draft
    must retain the FULL product recipe / pricing data. The wire-response
    projections shape only what the buyer sees, never the stored draft (the
    persist-draft on_complete hook fires with the raw adapter result before
    any projection). Pins the intended raw-result persistence behavior."""
    from adcp.types import GetProductsRequest, GetProductsResponse, Product

    rich_products = [Product.model_validate(_RICH_PRODUCT)]
    proposals = [
        {
            "proposal_id": "prop_rich",
            "name": "plan",
            "allocations": [{"product_id": "p1", "allocation_percentage": 100}],
        }
    ]

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(auto_paginate=True)
        accounts = _StoreBackedAccounts()

        def proposal_store_for_tenant(self, tenant_id: str):
            return store

        async def get_products(self, req, ctx):
            return GetProductsResponse(products=rich_products, proposals=proposals)

    store = _RecordingStore()
    handler = _make_handler(_Platform(), executor)
    # fields= drops product fields from the wire response; pagination=
    # paginates it. Neither must touch the stored draft.
    resp = await handler.get_products(
        GetProductsRequest(
            buying_mode="brief",
            brief="x",
            fields=["product_id"],
            pagination={"max_results": 1},
        ),
        ToolContext(),
    )
    assert isinstance(resp, GetProductsResponse)
    # Exactly one draft persisted.
    assert [c["proposal_id"] for c in store.put_calls] == ["prop_rich"]
    persisted = store.put_calls[0]["proposal_payload"]
    # The persisted proposal retains its allocations (recipe linkage).
    assert persisted["allocations"][0]["product_id"] == "p1"
    # The enriched pricing_option_id is carried onto the allocation (the
    # single-option product makes the choice unambiguous) — proves the
    # persist hook saw the full product pricing, not a stripped wire shape.
    assert persisted["allocations"][0].get("pricing_option_id") == "po1"


# ---------------------------------------------------------------------------
# MUST-FIX 2: no-push handoff against an unresolved account is owned by
# compose_caller_identity (fails closed at _build_ctx, before any task row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_handoff_unresolved_account_no_push_rejected(executor) -> None:
    """A no-push handoff against an unresolved (sentinel) account is rejected
    BEFORE any task row is minted. compose_caller_identity fails closed
    inside _build_ctx (terminal INVALID_REQUEST) before the platform method
    runs, so the registry never issues a task the buyer could not reach."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    async def _curate(task_ctx):
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = _UnresolvedAccounts()

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    registry = InMemoryTaskRegistry()
    handler = _make_handler(_Platform(), executor, registry=registry)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            # NO push_notification_config — the handoff arm of guard (c).
            GetProductsRequest(buying_mode="brief", brief="x"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    # No task row was ever issued — the rejection happened before dispatch.
    assert registry._records == {}


# ---------------------------------------------------------------------------
# SHOULD-FIX 1: a rejected wholesale handoff leaves NO registry side effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wholesale_handoff_rejected_leaves_no_registry_row(executor) -> None:
    """An adopter that hands off on a wholesale get_products is rejected the
    instant dispatch detects the TaskHandoff — BEFORE a registry row is
    minted, the background coroutine launched, or a draft persisted. Asserts
    the registry has no row after the rejection."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    ran = {"bg": False}

    async def _curate(task_ctx):
        ran["bg"] = True
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    registry = InMemoryTaskRegistry()
    handler = _make_handler(_Platform(), executor, registry=registry)
    with pytest.raises(AdcpError) as exc:
        await handler.get_products(
            GetProductsRequest(buying_mode="wholesale"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "buying_mode"
    # Give any (erroneously launched) bg task a chance to run.
    await asyncio.sleep(0.05)
    # No registry row minted, and the handoff coroutine never ran.
    assert registry._records == {}
    assert ran["bg"] is False


@pytest.mark.asyncio
async def test_get_signals_wholesale_handoff_rejected_leaves_no_registry_row(executor) -> None:
    from adcp.types import GetSignalsRequest, GetSignalsResponse

    ran = {"bg": False}

    async def _discover(task_ctx):
        ran["bg"] = True
        return GetSignalsResponse(signals=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return ctx.handoff_to_task(_discover)

    registry = InMemoryTaskRegistry()
    handler = _make_handler(_Platform(), executor, registry=registry)
    with pytest.raises(AdcpError) as exc:
        await handler.get_signals(
            GetSignalsRequest(discovery_mode="wholesale", signal_spec="x"),
            ToolContext(),
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.field == "discovery_mode"
    await asyncio.sleep(0.05)
    assert registry._records == {}
    assert ran["bg"] is False


# ---------------------------------------------------------------------------
# Async-completion webhooks: terminal completion / failure delivered exactly
# once from the background path when push_notification_config is present
# ---------------------------------------------------------------------------


def _push_handler(platform: DecisioningPlatform, executor: ThreadPoolExecutor):
    """Handler wired with a recording AsyncMock webhook sender."""
    from unittest.mock import AsyncMock

    sender = AsyncMock()
    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        webhook_sender=sender,
    )
    return handler, sender


async def _drain_until_webhook(sender, *, attempts: int = 40) -> None:
    for _ in range(attempts):
        await asyncio.sleep(0.02)
        if sender.send_mcp.await_count:
            return


@pytest.mark.asyncio
async def test_get_products_handoff_push_emits_one_completed_webhook(executor) -> None:
    """get_products handoff + push_notification_config → exactly one
    'completed' webhook from the background path, with operation_id echoed,
    the registry task_id, and the terminal result in the payload."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    async def _curate(task_ctx):
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    handler, sender = _push_handler(_Platform(), executor)
    envelope = await handler.get_products(
        GetProductsRequest(
            buying_mode="brief",
            brief="ctv",
            push_notification_config={
                "url": "https://buyer.example.com/wh",
                "operation_id": "op-products-123",
                "token": "tok-products-abcdefghij",
            },
        ),
        ToolContext(),
    )
    assert envelope["status"] == "submitted"
    await _drain_until_webhook(sender)
    sender.send_mcp.assert_awaited_once()
    kw = sender.send_mcp.await_args.kwargs
    assert kw["status"] == "completed"
    assert kw["task_type"] == "get_products"
    assert kw["task_id"] == envelope["task_id"]
    assert kw["operation_id"] == "op-products-123"
    assert kw["token"] == "tok-products-abcdefghij"
    assert "products" in kw["result"]


@pytest.mark.asyncio
async def test_get_signals_handoff_push_emits_one_completed_webhook(executor) -> None:
    from adcp.types import GetSignalsRequest, GetSignalsResponse

    async def _discover(task_ctx):
        return GetSignalsResponse(signals=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="signals-seller")

        async def get_signals(self, req, ctx):
            return ctx.handoff_to_task(_discover)

    handler, sender = _push_handler(_Platform(), executor)
    envelope = await handler.get_signals(
        GetSignalsRequest(
            discovery_mode="brief",
            signal_spec="auto intenders",
            push_notification_config={
                "url": "https://buyer.example.com/wh",
                "operation_id": "op-signals-456",
            },
        ),
        ToolContext(),
    )
    assert envelope["status"] == "submitted"
    await _drain_until_webhook(sender)
    sender.send_mcp.assert_awaited_once()
    kw = sender.send_mcp.await_args.kwargs
    assert kw["status"] == "completed"
    assert kw["task_type"] == "get_signals"
    assert kw["task_id"] == envelope["task_id"]
    assert kw["operation_id"] == "op-signals-456"


@pytest.mark.asyncio
async def test_create_media_buy_handoff_push_emits_one_completed_webhook(executor) -> None:
    """create_media_buy handoff + push → exactly one 'completed' terminal
    webhook from the background path (the framework-wide async-completion
    webhook now also covers create_media_buy)."""
    from adcp.types import CreateMediaBuyRequest, CreateMediaBuySuccessResponse

    async def _review(task_ctx):
        return CreateMediaBuySuccessResponse(
            media_buy_id="mb_async_1",
            confirmed_at="2026-05-01T00:00:00Z",
            revision=1,
            packages=[],
            status="active",
        )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="seller")

        def create_media_buy(self, req, ctx):
            return ctx.handoff_to_task(_review)

    handler, sender = _push_handler(_Platform(), executor)
    envelope = await handler.create_media_buy(
        CreateMediaBuyRequest(
            account={"account_id": "acct_a"},
            brand={"domain": "example.com"},
            idempotency_key="idem_async_create_1",
            start_time="2026-05-01T00:00:00Z",
            end_time="2026-05-31T23:59:59Z",
            push_notification_config={
                "url": "https://buyer.example.com/wh",
                "operation_id": "op-cmb-789",
            },
        ),
        ToolContext(),
    )
    assert isinstance(envelope, dict) and envelope["status"] == "submitted"
    await _drain_until_webhook(sender)
    sender.send_mcp.assert_awaited_once()
    kw = sender.send_mcp.await_args.kwargs
    assert kw["status"] == "completed"
    assert kw["task_type"] == "create_media_buy"
    assert kw["task_id"] == envelope["task_id"]
    assert kw["operation_id"] == "op-cmb-789"
    assert kw["result"]["media_buy_id"] == "mb_async_1"


@pytest.mark.asyncio
async def test_get_products_handoff_failure_emits_one_failed_webhook(executor) -> None:
    """When the handoff fn raises, the background path delivers exactly one
    'failed' webhook carrying the structured error, with operation_id echoed
    and the registry task_id."""
    from adcp.types import GetProductsRequest

    async def _curate(task_ctx):
        raise AdcpError(
            "INTERNAL_ERROR",
            message="curation backend unavailable",
            recovery="transient",
        )

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    handler, sender = _push_handler(_Platform(), executor)
    envelope = await handler.get_products(
        GetProductsRequest(
            buying_mode="brief",
            brief="ctv",
            push_notification_config={
                "url": "https://buyer.example.com/wh",
                "operation_id": "op-fail-111",
            },
        ),
        ToolContext(),
    )
    assert envelope["status"] == "submitted"
    await _drain_until_webhook(sender)
    sender.send_mcp.assert_awaited_once()
    kw = sender.send_mcp.await_args.kwargs
    assert kw["status"] == "failed"
    assert kw["task_type"] == "get_products"
    assert kw["task_id"] == envelope["task_id"]
    assert kw["operation_id"] == "op-fail-111"
    # The structured error wire dict rides on the payload result.
    assert kw["result"] is not None


@pytest.mark.asyncio
async def test_get_products_handoff_no_push_no_webhook(executor) -> None:
    """No push_notification_config → NO webhook fires on the handoff
    completion path. The buyer polls tasks/get; the polling-only path is
    unchanged."""
    from adcp.types import GetProductsRequest, GetProductsResponse

    completed = asyncio.Event()

    async def _curate(task_ctx):
        completed.set()
        return GetProductsResponse(products=[])

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def get_products(self, req, ctx):
            return ctx.handoff_to_task(_curate)

    handler, sender = _push_handler(_Platform(), executor)
    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="ctv"),
        ToolContext(),
    )
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    sender.send_mcp.assert_not_called()
