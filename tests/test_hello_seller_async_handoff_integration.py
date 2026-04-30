"""Hybrid + AdcpError integration tests for
``examples/hello_seller_async_handoff.py``.

Exercises the three return shapes of ``create_media_buy`` in a single
test surface:

1. Sync success (medium budget) → typed response
2. AdcpError raise (sub-floor budget) → correctable rejection envelope
3. TaskHandoff (large budget) → wire Submitted envelope, then
   asynchronous registry persistence of the terminal artifact

Per dispatch design D13 — vertical-slice example tests as
first-class deliverables.
"""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# examples/ is not a package — add to sys.path.
_EXAMPLES = str(Path(__file__).parent.parent / "examples")
if _EXAMPLES not in sys.path:
    sys.path.insert(0, _EXAMPLES)

import hello_seller_async_handoff as _hybrid  # noqa: E402

from adcp.decisioning import (  # noqa: E402
    AdcpError,
    InMemoryTaskRegistry,
)
from adcp.decisioning.handler import PlatformHandler  # noqa: E402
from adcp.server.base import ToolContext  # noqa: E402


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-int-handoff-")
    yield pool
    pool.shutdown(wait=True)


@pytest.fixture
def registry() -> InMemoryTaskRegistry:
    return InMemoryTaskRegistry()


@pytest.fixture
def handler(executor: ThreadPoolExecutor, registry: InMemoryTaskRegistry) -> PlatformHandler:
    return PlatformHandler(
        _hybrid.HelloSellerHybrid(),
        executor=executor,
        registry=registry,
    )


def _build_request(*, total_budget: float, idem_suffix: str):
    """Build a valid CreateMediaBuyRequest with the given budget. The
    hybrid platform branches on ``total_budget`` to pick sync vs.
    handoff vs. AdcpError. The wire shape uses ``total_budget`` as
    a typed object ``{currency, amount}``."""
    from adcp.types import CreateMediaBuyRequest

    return CreateMediaBuyRequest(
        account={"account_id": "buyer-1"},
        brand={"domain": "buyer.example.com"},
        idempotency_key=f"idem_handoff_test_{idem_suffix}_aaaa",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-31T23:59:59Z",
        total_budget={"currency": "USD", "amount": total_budget},
        packages=[
            {
                "product_id": "display-rotation",
                "pricing_option_id": "po-cpm-default",
                "budget": total_budget,
            },
        ],
    )


# ---- Arm 1: sync success ----


@pytest.mark.asyncio
async def test_create_media_buy_medium_budget_returns_sync_success(
    handler: PlatformHandler,
) -> None:
    """Budget between the floor and the HITL threshold goes through
    sync — typed response, no task_id, status=active."""
    req = _build_request(total_budget=5000.0, idem_suffix="medium")
    resp = await handler.create_media_buy(req, ToolContext())
    assert isinstance(resp, dict)
    # Sync arm: real media_buy_id, no task_id.
    assert resp["media_buy_id"].startswith("mb_sync_")
    assert "task_id" not in resp
    assert resp["status"] == "active"


# ---- Arm 2: AdcpError correctable rejection ----


@pytest.mark.asyncio
async def test_create_media_buy_below_floor_raises_adcp_error(
    handler: PlatformHandler,
) -> None:
    """Budget below the seller's floor → AdcpError correctable. The
    framework propagates verbatim (not wrapped to INTERNAL_ERROR);
    wire ``adcp_error`` envelope contains code + recovery + field +
    suggestion."""
    req = _build_request(total_budget=0.10, idem_suffix="cheap")
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(req, ToolContext())

    err = exc_info.value
    assert err.code == "BUDGET_TOO_LOW"
    assert err.recovery == "correctable"
    assert err.field == "total_budget"
    assert err.suggestion is not None
    assert "0.50" in err.suggestion or "0.5" in err.suggestion

    # to_wire() projection includes every populated field — adopters
    # / middleware that surface this to buyers see the full envelope.
    wire = err.to_wire()
    assert wire["code"] == "BUDGET_TOO_LOW"
    assert wire["recovery"] == "correctable"
    assert wire["field"] == "total_budget"
    assert "suggestion" in wire


# ---- Arm 3: TaskHandoff lifecycle ----


@pytest.mark.asyncio
async def test_create_media_buy_large_budget_returns_submitted_envelope(
    handler: PlatformHandler,
    registry: InMemoryTaskRegistry,
) -> None:
    """Budget above the HITL threshold → ctx.handoff_to_task. The
    framework returns the wire Submitted envelope SYNCHRONOUSLY,
    persists the task in 'submitted' state, runs the handoff fn in
    the background, then transitions to 'completed' with the
    terminal artifact."""
    req = _build_request(total_budget=100_000.0, idem_suffix="enterprise")
    resp = await handler.create_media_buy(req, ToolContext())

    # Sync return is the Submitted envelope per
    # ``schemas/cache/core/protocol-envelope.json`` — {task_id, status}
    # only. ``task_type`` is registry-internal (tasks/get reads it but
    # the wire never carries it).
    assert isinstance(resp, dict)
    assert resp["status"] == "submitted"
    assert "task_type" not in resp
    task_id = resp["task_id"]
    assert task_id.startswith("task_")

    # The handoff fn runs in the background; wait for it to complete.
    # The hybrid example's _async_trafficker_review awaits 50ms.
    deadline = asyncio.get_running_loop().time() + 2.0
    final_state = "submitted"
    while asyncio.get_running_loop().time() < deadline:
        rec = await registry.get(task_id, expected_account_id="hello-hybrid:anonymous")
        if rec is not None and rec["state"] in {"completed", "failed"}:
            final_state = rec["state"]
            break
        await asyncio.sleep(0.05)

    assert final_state == "completed", f"Expected handoff fn to complete; got state={final_state}"
    rec = await registry.get(task_id, expected_account_id="hello-hybrid:anonymous")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"]["media_buy_id"].startswith("mb_reviewed_")
    # DON'T cross-leak the framework's task_id namespace into the
    # adopter's media_buy_id namespace — buyers reading the response
    # shouldn't see a raw task UUID embedded in media_buy_id
    # (round-4 reviewer P1).
    assert task_id not in rec["result"]["media_buy_id"]
    assert rec["result"]["status"] == "active"


@pytest.mark.asyncio
async def test_handoff_progress_updates_visible_via_registry(
    handler: PlatformHandler,
    registry: InMemoryTaskRegistry,
) -> None:
    """The handoff fn calls ``task_ctx.update(progress)``; buyers
    polling tasks/get see the latest progress payload while the task
    is in 'working' state. Verifies the update_progress wiring
    end-to-end (registry write + state transition)."""
    req = _build_request(total_budget=200_000.0, idem_suffix="progress")
    resp = await handler.create_media_buy(req, ToolContext())
    task_id = resp["task_id"]

    # Give the background fn a moment to fire its first update().
    # The first update transitions submitted → working.
    deadline = asyncio.get_running_loop().time() + 2.0
    final_state = None
    while asyncio.get_running_loop().time() < deadline:
        rec = await registry.get(task_id, expected_account_id="hello-hybrid:anonymous")
        if rec is not None and rec["state"] == "completed":
            final_state = rec["state"]
            # The example's last update wrote "trafficker approved";
            # registry stores the LATEST progress payload.
            assert rec["progress"] == {"step": "trafficker approved"}
            break
        await asyncio.sleep(0.02)

    assert final_state == "completed"


# ---- Arm 3 fail path: handoff fn raises AdcpError ----


@pytest.mark.asyncio
async def test_handoff_fn_adcp_error_persists_via_registry_fail(
    handler: PlatformHandler,
    registry: InMemoryTaskRegistry,
) -> None:
    """When the handoff fn itself raises AdcpError, the framework
    routes to registry.fail(task_id, err.to_wire()). tasks/get
    returns the failure envelope verbatim. Tested by stitching a
    custom hybrid platform whose handoff fn rejects."""
    from adcp.decisioning import (
        DecisioningCapabilities,
        DecisioningPlatform,
        SingletonAccounts,
    )

    async def _rejecting_handoff(task_ctx):
        raise AdcpError(
            "POLICY_VIOLATION",
            message="trafficker rejected after review",
            recovery="terminal",
            details={"reviewer": "trafficker-1"},
        )

    class _RejectingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=[])
        accounts = SingletonAccounts(account_id="reject-test")

        async def create_media_buy(self, req, ctx):
            return ctx.handoff_to_task(_rejecting_handoff)

    rejecting_handler = PlatformHandler(
        _RejectingPlatform(),
        executor=handler._executor,
        registry=registry,
    )
    req = _build_request(total_budget=5000.0, idem_suffix="reject")
    resp = await rejecting_handler.create_media_buy(req, ToolContext())
    task_id = resp["task_id"]

    deadline = asyncio.get_running_loop().time() + 2.0
    rec = None
    while asyncio.get_running_loop().time() < deadline:
        rec = await registry.get(task_id, expected_account_id="reject-test:anonymous")
        if rec is not None and rec["state"] == "failed":
            break
        await asyncio.sleep(0.02)

    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"]["code"] == "POLICY_VIOLATION"
    assert rec["error"]["recovery"] == "terminal"
    assert rec["error"]["details"]["reviewer"] == "trafficker-1"
