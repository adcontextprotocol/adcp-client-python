"""End-to-end integration tests for the v1.5 proposal lifecycle.

Exercises the full dispatch pipeline through :class:`PlatformHandler`:
typed wire request → handler shim → :func:`maybe_intercept_finalize` /
:func:`maybe_hydrate_recipes_for_create_media_buy` /
:func:`maybe_hydrate_recipes_for_media_buy_id` →
:class:`ProposalManager` / :class:`DecisioningPlatform` →
:class:`InMemoryProposalStore` reads/writes → typed wire response.

No MCP server transport — we exercise the framework wiring directly.

Test surface mirrors the storyboard ``proposal_finalize.yaml``:

1. brief → manager returns proposals + recipes; framework persists drafts.
2. refine → manager returns refined proposals; framework overwrites drafts.
3. finalize → framework intercepts before refine_products; calls
   ``finalize_proposal``; commits via ``proposal_store.commit``.
4. create_media_buy(proposal_id=...) → framework hydrates ``ctx.recipes``,
   validates expiry + capability overlap, dispatches to platform,
   marks proposal consumed.
5. update_media_buy(media_buy_id) → framework hydrates ``ctx.recipes``
   from the consumed proposal via reverse-index.
6. get_media_buy_delivery → same hydration path.

Plus capability-overlap rejection, expiry boundaries, and crash-resume
(simulating framework restart between create_media_buy and
update_media_buy).
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

# Add examples dir to path so the storyboard adopter modules import.
_EXAMPLES = Path(__file__).parent.parent / "examples"
if str(_EXAMPLES.parent) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES.parent))

from adcp.decisioning import (  # noqa: E402
    AdcpError,
    InMemoryProposalStore,
    InMemoryTaskRegistry,
)
from adcp.decisioning.handler import PlatformHandler  # noqa: E402
from adcp.decisioning.proposal_store import ProposalState  # noqa: E402
from adcp.server.base import ToolContext  # noqa: E402
from examples.sales_proposal_mode_seller.src.app import build_router  # noqa: E402
from examples.sales_proposal_mode_seller.src.proposal_manager import (  # noqa: E402
    PROPOSAL_ID,
)

_BRAND = {"domain": "acmeoutdoor.example"}
_ACCOUNT = {"account_id": "acct_demo"}
_CMB_IDEM = "test-cmb-prop-"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-e2e-proposal-")
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def router() -> Any:
    return build_router()


@pytest.fixture
def store(router: Any) -> InMemoryProposalStore:
    return router.proposal_store_for_tenant("default")


@pytest.fixture
def handler(executor: ThreadPoolExecutor, router: Any) -> PlatformHandler:
    return PlatformHandler(
        router,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )


# ---------------------------------------------------------------------------
# Phase 1: brief → proposals + recipes persisted as drafts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brief_persists_drafts_to_store(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import GetProductsRequest

    resp = await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="anything"),
        ToolContext(),
    )

    response_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")
    proposals = response_dict["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == PROPOSAL_ID
    assert proposals[0]["proposal_status"] == "draft"

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT
    assert "ctv-premium-q2" in record.recipes
    assert "display-run-q2" in record.recipes


# ---------------------------------------------------------------------------
# Phase 2: refine → draft overwritten in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refine_overwrites_draft(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )

    refine_req = GetProductsRequest.model_validate(
        {
            "buying_mode": "refine",
            "refine": [
                {
                    "scope": "proposal",
                    "proposal_id": PROPOSAL_ID,
                    "ask": "Shift to CTV.",
                },
                {"scope": "request", "ask": "Frequency cap 3 per day."},
            ],
        }
    )
    resp = await handler.get_products(refine_req, ToolContext())
    response_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")
    assert response_dict["proposals"][0]["proposal_id"] == PROPOSAL_ID
    assert response_dict["proposals"][0]["proposal_status"] == "draft"
    assert len(response_dict["refinement_applied"]) == 2

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT


# ---------------------------------------------------------------------------
# Phase 3: finalize → framework intercepts; manager.finalize_proposal commits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_commits_proposal(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )

    finalize_req = GetProductsRequest.model_validate(
        {
            "buying_mode": "refine",
            "refine": [
                {
                    "scope": "proposal",
                    "proposal_id": PROPOSAL_ID,
                    "action": "finalize",
                },
            ],
        }
    )
    resp = await handler.get_products(finalize_req, ToolContext())
    response_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")

    committed = response_dict["proposals"][0]
    assert committed["proposal_status"] == "committed"
    assert committed["proposal_id"] == PROPOSAL_ID
    assert "expires_at" in committed
    assert response_dict["refinement_applied"][0]["status"] == "applied"
    assert response_dict["refinement_applied"][0]["scope"] == "proposal"

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.COMMITTED
    assert record.expires_at is not None


# ---------------------------------------------------------------------------
# Phase 4: create_media_buy(proposal_id) → recipes hydrated; consume marked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_hydrates_and_consumes(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest, GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    finalize_req = GetProductsRequest.model_validate(
        {
            "buying_mode": "refine",
            "refine": [
                {
                    "scope": "proposal",
                    "proposal_id": PROPOSAL_ID,
                    "action": "finalize",
                }
            ],
        }
    )
    await handler.get_products(finalize_req, ToolContext())

    cmb_req = CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": PROPOSAL_ID,
            "total_budget": {"amount": 50000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": "test-buyer-001",
            "idempotency_key": _CMB_IDEM + "001",
            "brand": _BRAND,
            "account": _ACCOUNT,
        }
    )
    resp = await handler.create_media_buy(cmb_req, ToolContext())
    resp_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")
    assert resp_dict["status"] == "active"
    assert resp_dict["proposal_id"] == PROPOSAL_ID
    media_buy_id = resp_dict["media_buy_id"]
    assert media_buy_id

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == media_buy_id

    reverse_record = await store.get_by_media_buy_id(media_buy_id, expected_account_id="acct_demo")
    assert reverse_record is not None
    assert reverse_record.proposal_id == PROPOSAL_ID


# ---------------------------------------------------------------------------
# Phase 5+6: update_media_buy / get_media_buy_delivery hydrate from reverse-index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_and_delivery_hydrate_from_reverse_index(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import (
        CreateMediaBuyRequest,
        GetMediaBuyDeliveryRequest,
        GetProductsRequest,
        UpdateMediaBuyRequest,
    )

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )
    cmb_resp = await handler.create_media_buy(
        CreateMediaBuyRequest.model_validate(
            {
                "proposal_id": PROPOSAL_ID,
                "total_budget": {"amount": 50000, "currency": "USD"},
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-06-30T23:59:59Z",
                "buyer_ref": "test-buyer-002",
                "idempotency_key": _CMB_IDEM + "update",
                "brand": _BRAND,
                "account": _ACCOUNT,
            }
        ),
        ToolContext(),
    )
    cmb_dict = cmb_resp if isinstance(cmb_resp, dict) else cmb_resp.model_dump(mode="json")
    media_buy_id = cmb_dict["media_buy_id"]

    upd_resp = await handler.update_media_buy(
        UpdateMediaBuyRequest.model_validate(
            {
                "media_buy_id": media_buy_id,
                "account": _ACCOUNT,
                "idempotency_key": _CMB_IDEM + "updateB",
                "total_budget": {"amount": 60000, "currency": "USD"},
            }
        ),
        ToolContext(),
    )
    upd_dict = upd_resp if isinstance(upd_resp, dict) else upd_resp.model_dump(mode="json")
    assert upd_dict["media_buy_id"] == media_buy_id

    delivery = await handler.get_media_buy_delivery(
        GetMediaBuyDeliveryRequest.model_validate(
            {"media_buy_ids": [media_buy_id], "account": _ACCOUNT},
        ),
        ToolContext(),
    )
    delivery_dict = delivery if isinstance(delivery, dict) else delivery.model_dump(mode="json")
    assert len(delivery_dict["media_buy_deliveries"]) == 1


# ---------------------------------------------------------------------------
# Capability-overlap rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_rejects_disallowed_pricing_model(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest, GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )

    cmb_req = CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": PROPOSAL_ID,
            "total_budget": {"amount": 50000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": "test-buyer-rej",
            "idempotency_key": _CMB_IDEM + "reject",
            "brand": _BRAND,
            "account": _ACCOUNT,
            "packages": [
                {
                    "buyer_ref": "pkg-1",
                    "product_id": "ctv-premium-q2",
                    "pricing_option_id": "po-ctv-cpm",
                    "_resolved_pricing_model": "cpcv",
                    "budget": 10000,
                }
            ],
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(cmb_req, ToolContext())
    assert exc_info.value.code == "INVALID_REQUEST"
    assert "packages[0].pricing_option_id" in (exc_info.value.field or "")


# ---------------------------------------------------------------------------
# Expiry — both boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_proposal_rejected(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest
    from examples.sales_proposal_mode_seller.src.recipe import ProposalModeRecipe

    await store.put_draft(
        proposal_id="expired_proposal",
        account_id="acct_demo",
        publisher_id="default",
        recipes={
            "ctv-premium-q2": ProposalModeRecipe(
                line_item_template_id="lit_test",
                floor_cpm=15.0,
            ),
        },
        proposal_payload={"proposal_id": "expired_proposal", "proposal_status": "draft"},
    )
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    await store.commit(
        "expired_proposal",
        expires_at=past,
        proposal_payload={"proposal_id": "expired_proposal", "proposal_status": "committed"},
        expected_account_id="acct_demo",
        expected_publisher_id="default",
    )

    cmb_req = CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": "expired_proposal",
            "total_budget": {"amount": 1000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": "test-buyer-exp",
            "idempotency_key": _CMB_IDEM + "expire",
            "brand": _BRAND,
            "account": _ACCOUNT,
        }
    )
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(cmb_req, ToolContext())
    assert exc_info.value.code == "PROPOSAL_EXPIRED"


@pytest.mark.asyncio
async def test_within_grace_window_accepted(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest
    from examples.sales_proposal_mode_seller.src.recipe import ProposalModeRecipe

    await store.put_draft(
        proposal_id="grace_proposal",
        account_id="acct_demo",
        publisher_id="default",
        recipes={
            "ctv-premium-q2": ProposalModeRecipe(
                line_item_template_id="lit_test",
                floor_cpm=15.0,
            ),
        },
        proposal_payload={"proposal_id": "grace_proposal", "proposal_status": "draft"},
    )
    near = datetime.now(timezone.utc) - timedelta(seconds=30)
    await store.commit(
        "grace_proposal",
        expires_at=near,
        proposal_payload={"proposal_id": "grace_proposal", "proposal_status": "committed"},
        expected_account_id="acct_demo",
        expected_publisher_id="default",
    )

    cmb_req = CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": "grace_proposal",
            "total_budget": {"amount": 1000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": "test-buyer-grace",
            "idempotency_key": _CMB_IDEM + "graceA",
            "brand": _BRAND,
            "account": _ACCOUNT,
        }
    )
    resp = await handler.create_media_buy(cmb_req, ToolContext())
    resp_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")
    assert resp_dict["status"] == "active"


# ---------------------------------------------------------------------------
# Crash / restart safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_between_finalize_and_create_media_buy(
    router: Any,
    executor: ThreadPoolExecutor,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest, GetProductsRequest

    handler1 = PlatformHandler(
        router,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    await handler1.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler1.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )
    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.COMMITTED

    handler2 = PlatformHandler(
        router,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    cmb_req = CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": PROPOSAL_ID,
            "total_budget": {"amount": 50000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": "test-buyer-restart",
            "idempotency_key": _CMB_IDEM + "restart",
            "brand": _BRAND,
            "account": _ACCOUNT,
        }
    )
    resp = await handler2.create_media_buy(cmb_req, ToolContext())
    resp_dict = resp if isinstance(resp, dict) else resp.model_dump(mode="json")
    assert resp_dict["status"] == "active"


# ---------------------------------------------------------------------------
# Wire-overlap subset validation (§ D4 round-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlap_subset_of_wire_drift_raises_internal_error(
    handler: PlatformHandler,
) -> None:
    from typing import Literal

    from adcp.decisioning import CapabilityOverlap, Recipe
    from adcp.decisioning.proposal_lifecycle import validate_overlap_subset_of_wire

    class DriftRecipe(Recipe):
        recipe_kind: Literal["drift"] = "drift"
        capability_overlap: CapabilityOverlap | None = CapabilityOverlap(
            pricing_models=frozenset({"cpcv"}),
        )

    products = [
        {
            "product_id": "p1",
            "pricing_options": [
                {"pricing_option_id": "po-1", "pricing_model": "cpm"},
            ],
        }
    ]
    with pytest.raises(AdcpError) as exc_info:
        validate_overlap_subset_of_wire(
            recipes={"p1": DriftRecipe()},
            products=products,
        )
    assert exc_info.value.code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Lifecycle log records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_logs_emitted(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
    caplog: Any,
) -> None:
    import logging

    from adcp.types import CreateMediaBuyRequest, GetProductsRequest

    caplog.set_level(logging.INFO, logger="adcp.decisioning.proposal_lifecycle")
    caplog.set_level(logging.INFO, logger="adcp.decisioning.proposal_dispatch")

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )
    await handler.create_media_buy(
        CreateMediaBuyRequest.model_validate(
            {
                "proposal_id": PROPOSAL_ID,
                "total_budget": {"amount": 50000, "currency": "USD"},
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-06-30T23:59:59Z",
                "buyer_ref": "test-buyer-logs",
                "idempotency_key": _CMB_IDEM + "logsabc",
                "brand": _BRAND,
                "account": _ACCOUNT,
            }
        ),
        ToolContext(),
    )

    messages = [r.message for r in caplog.records]
    assert "proposal.draft_persisted" in messages
    assert "proposal.finalized" in messages
    assert "proposal.consumed" in messages


# ---------------------------------------------------------------------------
# Phase 11: TaskHandoff finalize — HITL slow path (D2)
# ---------------------------------------------------------------------------


def _build_handoff_router(handoff_fn: Any) -> Any:
    from adcp.decisioning.types import TaskHandoff
    from examples.sales_proposal_mode_seller.src.proposal_manager import (
        ProposalModeProposalManager,
    )

    class _HandoffManager(ProposalModeProposalManager):
        async def finalize_proposal(self, req: Any, ctx: Any) -> Any:  # type: ignore[override]
            del req, ctx
            return TaskHandoff(handoff_fn)

    router = build_router()
    router._proposal_managers = {"default": _HandoffManager()}  # noqa: SLF001
    return router


@pytest.fixture
def registry() -> InMemoryTaskRegistry:
    return InMemoryTaskRegistry()


def _build_handler(
    router: Any, executor: ThreadPoolExecutor, registry: InMemoryTaskRegistry
) -> PlatformHandler:
    return PlatformHandler(router, executor=executor, registry=registry)


async def _seed_draft(handler: PlatformHandler) -> None:
    from adcp.types import GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )


def _finalize_request() -> Any:
    from adcp.types import GetProductsRequest

    return GetProductsRequest.model_validate(
        {
            "buying_mode": "refine",
            "refine": [
                {
                    "scope": "proposal",
                    "proposal_id": PROPOSAL_ID,
                    "action": "finalize",
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_finalize_handoff_returns_submitted_envelope(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    from adcp.decisioning.proposal_manager import FinalizeProposalSuccess

    finish = asyncio.Event()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    async def _handoff_body(task_ctx: Any) -> FinalizeProposalSuccess:
        del task_ctx
        await finish.wait()
        return FinalizeProposalSuccess(
            proposal={
                "proposal_id": PROPOSAL_ID,
                "proposal_status": "committed",
                "expires_at": expires_at.isoformat(),
            },
            expires_at=expires_at,
        )

    router = _build_handoff_router(_handoff_body)
    handler = _build_handler(router, executor, registry)
    store = router.proposal_store_for_tenant("default")

    await _seed_draft(handler)
    response = await handler.get_products(_finalize_request(), ToolContext())
    response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")

    assert response_dict["status"] == "submitted"
    assert "task_id" in response_dict
    task_id = response_dict["task_id"]

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT

    finish.set()
    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.COMMITTED
    assert record.expires_at == expires_at

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "completed"


@pytest.mark.asyncio
async def test_finalize_handoff_emits_handoff_path_log(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
    caplog: Any,
) -> None:
    import logging

    from adcp.decisioning.proposal_manager import FinalizeProposalSuccess

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    async def _handoff_body(task_ctx: Any) -> FinalizeProposalSuccess:
        del task_ctx
        return FinalizeProposalSuccess(
            proposal={"proposal_id": PROPOSAL_ID, "proposal_status": "committed"},
            expires_at=expires_at,
        )

    caplog.set_level(logging.INFO, logger="adcp.decisioning.proposal_lifecycle")
    router = _build_handoff_router(_handoff_body)
    handler = _build_handler(router, executor, registry)

    await _seed_draft(handler)
    await handler.get_products(_finalize_request(), ToolContext())
    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    handoff_records = [
        r
        for r in caplog.records
        if r.message == "proposal.finalized" and getattr(r, "path", None) == "handoff"
    ]
    assert len(handoff_records) == 1


@pytest.mark.asyncio
async def test_finalize_handoff_fn_raises_keeps_proposal_draft(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    async def _handoff_body(task_ctx: Any) -> Any:
        del task_ctx
        raise AdcpError(
            "GOVERNANCE_DENIED",
            message="Brand-safety reviewer rejected the inventory hold.",
            recovery="terminal",
        )

    router = _build_handoff_router(_handoff_body)
    handler = _build_handler(router, executor, registry)
    store = router.proposal_store_for_tenant("default")

    await _seed_draft(handler)
    response = await handler.get_products(_finalize_request(), ToolContext())
    response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")
    task_id = response_dict["task_id"]

    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT
    assert record.expires_at is None

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "failed"
    assert task_record["error"]["code"] == "GOVERNANCE_DENIED"


@pytest.mark.asyncio
async def test_finalize_handoff_fn_wrong_return_type_keeps_proposal_draft(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    async def _handoff_body(task_ctx: Any) -> dict:
        del task_ctx
        return {"proposal_id": PROPOSAL_ID, "proposal_status": "committed"}

    router = _build_handoff_router(_handoff_body)
    handler = _build_handler(router, executor, registry)
    store = router.proposal_store_for_tenant("default")

    await _seed_draft(handler)
    response = await handler.get_products(_finalize_request(), ToolContext())
    response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")
    task_id = response_dict["task_id"]

    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "failed"
    assert task_record["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_finalize_handoff_commit_failure_keeps_proposal_draft(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    from adcp.decisioning.proposal_manager import FinalizeProposalSuccess

    async def _handoff_body(task_ctx: Any) -> FinalizeProposalSuccess:
        del task_ctx
        return FinalizeProposalSuccess(
            proposal={"proposal_id": PROPOSAL_ID, "proposal_status": "committed"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )

    router = _build_handoff_router(_handoff_body)
    handler = _build_handler(router, executor, registry)
    store = router.proposal_store_for_tenant("default")

    await _seed_draft(handler)

    original_commit = store.commit

    async def _failing_commit(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("simulated DB failure during commit")

    store.commit = _failing_commit  # type: ignore[method-assign]
    try:
        response = await handler.get_products(_finalize_request(), ToolContext())
        response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")
        task_id = response_dict["task_id"]
        await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)
    finally:
        store.commit = original_commit  # type: ignore[method-assign]

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.DRAFT

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "failed"
    assert task_record["error"]["code"] == "INTERNAL_ERROR"


async def _drain_background_tasks() -> None:
    from adcp.decisioning.dispatch import _BACKGROUND_HANDOFF_TASKS

    while _BACKGROUND_HANDOFF_TASKS:
        pending = list(_BACKGROUND_HANDOFF_TASKS)
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Phase 12: TOCTOU consumption race + adapter-failure rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_concurrent_proposal_id_only_one_succeeds(
    handler: PlatformHandler,
    store: InMemoryProposalStore,
) -> None:
    from adcp.types import CreateMediaBuyRequest, GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )

    def _build_req(suffix: str) -> CreateMediaBuyRequest:
        return CreateMediaBuyRequest.model_validate(
            {
                "proposal_id": PROPOSAL_ID,
                "total_budget": {"amount": 50000, "currency": "USD"},
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-06-30T23:59:59Z",
                "buyer_ref": f"buyer-race-{suffix}",
                "idempotency_key": _CMB_IDEM + f"race{suffix}",
                "brand": _BRAND,
                "account": _ACCOUNT,
            }
        )

    async def _call(suffix: str) -> tuple[str, Any]:
        try:
            result = await handler.create_media_buy(_build_req(suffix), ToolContext())
            return ("ok", result)
        except AdcpError as exc:
            return (exc.code, exc)

    a, b = await asyncio.gather(_call("a"), _call("b"))
    statuses = sorted([a[0], b[0]])
    assert statuses == ["PROPOSAL_NOT_COMMITTED", "ok"]

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.CONSUMED


@pytest.mark.asyncio
async def test_create_media_buy_adapter_failure_rolls_back_reservation(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    from examples.sales_proposal_mode_seller.src.app import build_router
    from examples.sales_proposal_mode_seller.src.platform import (
        ProposalModeDecisioningPlatform,
    )

    router = build_router()
    store = router.proposal_store_for_tenant("default")
    handler = _build_handler(router, executor, registry)

    target_platform = router._platforms["default"]  # noqa: SLF001
    assert isinstance(target_platform, ProposalModeDecisioningPlatform)
    original = target_platform.create_media_buy

    async def _failing_create(req: Any, ctx: Any) -> Any:
        del req, ctx
        raise AdcpError(
            "AGENT_TIMEOUT",
            message="upstream took too long",
            recovery="transient",
        )

    target_platform.create_media_buy = _failing_create  # type: ignore[method-assign]

    try:
        from adcp.types import CreateMediaBuyRequest, GetProductsRequest

        await handler.get_products(
            GetProductsRequest(buying_mode="brief", brief="initial"),
            ToolContext(),
        )
        await handler.get_products(
            GetProductsRequest.model_validate(
                {
                    "buying_mode": "refine",
                    "refine": [
                        {
                            "scope": "proposal",
                            "proposal_id": PROPOSAL_ID,
                            "action": "finalize",
                        }
                    ],
                }
            ),
            ToolContext(),
        )

        with pytest.raises(AdcpError) as exc:
            await handler.create_media_buy(
                CreateMediaBuyRequest.model_validate(
                    {
                        "proposal_id": PROPOSAL_ID,
                        "total_budget": {"amount": 50000, "currency": "USD"},
                        "start_time": "2026-04-01T00:00:00Z",
                        "end_time": "2026-06-30T23:59:59Z",
                        "buyer_ref": "buyer-rollback",
                        "idempotency_key": _CMB_IDEM + "rollback",
                        "brand": _BRAND,
                        "account": _ACCOUNT,
                    }
                ),
                ToolContext(),
            )
        assert exc.value.code == "AGENT_TIMEOUT"

        record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
        assert record is not None
        assert record.state == ProposalState.COMMITTED
    finally:
        target_platform.create_media_buy = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Phase 13: TaskHandoff create_media_buy — HITL accept-proposal path
# ---------------------------------------------------------------------------


def _build_handoff_create_media_buy_router(handoff_fn: Any) -> Any:
    from adcp.decisioning.types import TaskHandoff
    from examples.sales_proposal_mode_seller.src.platform import (
        ProposalModeDecisioningPlatform,
    )

    router = build_router()

    class _HandoffPlatform(ProposalModeDecisioningPlatform):
        async def create_media_buy(self, req: Any, ctx: Any) -> Any:  # type: ignore[override]
            del req, ctx
            return TaskHandoff(handoff_fn)

    router._platforms = {"default": _HandoffPlatform()}  # noqa: SLF001
    return router


def _build_create_media_buy_request(suffix: str) -> Any:
    from adcp.types import CreateMediaBuyRequest

    return CreateMediaBuyRequest.model_validate(
        {
            "proposal_id": PROPOSAL_ID,
            "total_budget": {"amount": 50000, "currency": "USD"},
            "start_time": "2026-04-01T00:00:00Z",
            "end_time": "2026-06-30T23:59:59Z",
            "buyer_ref": f"buyer-handoff-cmb-{suffix}",
            "idempotency_key": _CMB_IDEM + f"hcmb{suffix}",
            "brand": _BRAND,
            "account": _ACCOUNT,
        }
    )


async def _seed_committed_proposal(handler: PlatformHandler) -> None:
    from adcp.types import GetProductsRequest

    await handler.get_products(
        GetProductsRequest(buying_mode="brief", brief="initial"),
        ToolContext(),
    )
    await handler.get_products(
        GetProductsRequest.model_validate(
            {
                "buying_mode": "refine",
                "refine": [
                    {
                        "scope": "proposal",
                        "proposal_id": PROPOSAL_ID,
                        "action": "finalize",
                    }
                ],
            }
        ),
        ToolContext(),
    )


@pytest.mark.asyncio
async def test_create_media_buy_handoff_finalizes_consumption_on_completion(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    finish = asyncio.Event()

    async def _handoff_body(task_ctx: Any) -> Any:
        del task_ctx
        await finish.wait()
        return {"media_buy_id": "mb_handoff_001", "status": "active"}

    router = _build_handoff_create_media_buy_router(_handoff_body)
    store = router.proposal_store_for_tenant("default")
    handler = _build_handler(router, executor, registry)

    await _seed_committed_proposal(handler)
    response = await handler.create_media_buy(
        _build_create_media_buy_request("happy"), ToolContext()
    )
    response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")

    assert response_dict["status"] == "submitted"
    task_id = response_dict["task_id"]

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.CONSUMING

    finish.set()
    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == "mb_handoff_001"

    by_buy = await store.get_by_media_buy_id("mb_handoff_001", expected_account_id="acct_demo")
    assert by_buy is not None
    assert by_buy.proposal_id == PROPOSAL_ID

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "completed"


@pytest.mark.asyncio
async def test_create_media_buy_handoff_fn_raises_releases_reservation(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    async def _handoff_body(task_ctx: Any) -> Any:
        del task_ctx
        raise AdcpError(
            "GOVERNANCE_DENIED",
            message="Trafficker rejected the inventory hold.",
            recovery="terminal",
        )

    router = _build_handoff_create_media_buy_router(_handoff_body)
    store = router.proposal_store_for_tenant("default")
    handler = _build_handler(router, executor, registry)

    await _seed_committed_proposal(handler)
    response = await handler.create_media_buy(
        _build_create_media_buy_request("denied"), ToolContext()
    )
    response_dict = response if isinstance(response, dict) else response.model_dump(mode="json")
    task_id = response_dict["task_id"]

    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)

    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.COMMITTED

    task_record = await registry.get(task_id, expected_account_id="acct_demo")
    assert task_record is not None
    assert task_record["state"] == "failed"
    assert task_record["error"]["code"] == "GOVERNANCE_DENIED"


@pytest.mark.asyncio
async def test_create_media_buy_handoff_buyer_can_retry_after_release(
    executor: ThreadPoolExecutor,
    registry: InMemoryTaskRegistry,
) -> None:
    fail_first = {"count": 0}

    async def _flaky_handoff(task_ctx: Any) -> Any:
        del task_ctx
        fail_first["count"] += 1
        if fail_first["count"] == 1:
            raise AdcpError(
                "AGENT_TIMEOUT",
                message="upstream slow on first attempt",
                recovery="transient",
            )
        return {"media_buy_id": "mb_retry_002", "status": "active"}

    router = _build_handoff_create_media_buy_router(_flaky_handoff)
    store = router.proposal_store_for_tenant("default")
    handler = _build_handler(router, executor, registry)

    await _seed_committed_proposal(handler)

    await handler.create_media_buy(_build_create_media_buy_request("first"), ToolContext())
    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)
    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.COMMITTED

    await handler.create_media_buy(_build_create_media_buy_request("second"), ToolContext())
    await asyncio.wait_for(_drain_background_tasks(), timeout=2.0)
    record = await store.get(PROPOSAL_ID, expected_account_id="acct_demo")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == "mb_retry_002"
