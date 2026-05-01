"""Tests for ``handoff_to_workflow`` — the externally-completed task primitive.

Distinct from ``handoff_to_task`` (framework-managed background work).
``handoff_to_workflow`` is for adopter-owned external workflows
(human queue review, batch jobs, Airflow DAGs, ML pipelines, scheduled
cron) that complete on their own schedule via direct calls to
``registry.complete()`` / ``registry.fail()``.

Test surfaces:

* Wire-shape parity with TaskHandoff (Submitted envelope identical).
* Sync + async enqueue fns supported.
* Registry rollback on enqueue exception (no orphan task_id reaches
  the buyer).
* No background coroutine runs in the framework.
* External completion via ``registry.complete()`` transitions state
  correctly; buyers polling ``tasks/get`` see the terminal artifact.
* Cross-tenant probe semantics survive the workflow lifecycle.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
    WorkflowHandoff,
)
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
    _project_workflow_handoff,
)
from adcp.decisioning.types import Account, AdcpError
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-workflow-")
    yield pool
    pool.shutdown(wait=True)


class _ProductsRequest(BaseModel):
    pass


# ---- Public API + marker shape ----


def test_handoff_to_workflow_returns_workflow_marker() -> None:
    """``ctx.handoff_to_workflow(fn)`` returns a :class:`WorkflowHandoff`
    marker — distinct from :class:`TaskHandoff`."""
    from adcp.decisioning.context import RequestContext

    ctx = RequestContext()
    marker = ctx.handoff_to_workflow(lambda task_ctx: None)
    assert type(marker) is WorkflowHandoff


def test_workflow_handoff_dispatch_uses_type_identity_not_isinstance() -> None:
    """``is_workflow_handoff`` matches by type identity. Adopter
    subclasses don't trigger the workflow path — they fall through to
    sync return (silent — adopter contract)."""
    from adcp.decisioning.types import is_workflow_handoff

    assert is_workflow_handoff(WorkflowHandoff(lambda task_ctx: None))

    class _Subclass(WorkflowHandoff):
        pass

    assert not is_workflow_handoff(_Subclass(lambda task_ctx: None))


# ---- Wire-shape parity ----


@pytest.mark.asyncio
async def test_workflow_handoff_returns_submitted_envelope(
    executor: ThreadPoolExecutor,
) -> None:
    """The wire envelope is the EXACT same shape as TaskHandoff:
    ``{task_id, status: 'submitted'}``. Buyers can't tell which
    path the seller took."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    enqueue_called = False

    def _enqueue(task_ctx):
        nonlocal enqueue_called
        enqueue_called = True

    handoff = WorkflowHandoff(_enqueue)
    envelope = await _project_workflow_handoff(
        handoff,
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    assert set(envelope.keys()) == {"task_id", "status"}
    assert envelope["status"] == "submitted"
    assert envelope["task_id"].startswith("task_")
    assert enqueue_called is True


@pytest.mark.asyncio
async def test_workflow_handoff_persists_submitted_state_in_registry(
    executor: ThreadPoolExecutor,
) -> None:
    """After projection, the registry holds a record in
    ``submitted`` state — the adopter's external workflow drives
    the eventual transition to completed/failed."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    handoff = WorkflowHandoff(lambda task_ctx: None)
    envelope = await _project_workflow_handoff(
        handoff,
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "submitted"
    assert rec["task_type"] == "create_media_buy"
    # No background work ran — no progress or terminal artifact.
    assert rec["progress"] is None
    assert rec["result"] is None
    assert rec["error"] is None


# ---- Sync + async enqueue ----


@pytest.mark.asyncio
async def test_workflow_handoff_supports_async_enqueue(
    executor: ThreadPoolExecutor,
) -> None:
    """Async enqueue fn — framework awaits it inline, doesn't dispatch
    to a background task."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    enqueued_task_ids: list[str] = []

    async def _async_enqueue(task_ctx):
        # Could await something here (DB write, queue publish).
        await asyncio.sleep(0)
        enqueued_task_ids.append(task_ctx.id)

    envelope = await _project_workflow_handoff(
        WorkflowHandoff(_async_enqueue),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    assert enqueued_task_ids == [envelope["task_id"]]


@pytest.mark.asyncio
async def test_workflow_handoff_supports_sync_enqueue(
    executor: ThreadPoolExecutor,
) -> None:
    """Sync enqueue fn — framework runs on the executor with a
    contextvars snapshot."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    enqueued: list[str] = []

    def _sync_enqueue(task_ctx):
        enqueued.append(task_ctx.id)

    envelope = await _project_workflow_handoff(
        WorkflowHandoff(_sync_enqueue),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    assert enqueued == [envelope["task_id"]]


# ---- Rollback on enqueue exception ----


@pytest.mark.asyncio
async def test_workflow_handoff_rolls_back_registry_on_sync_enqueue_failure(
    executor: ThreadPoolExecutor,
) -> None:
    """If the sync enqueue fn raises, the framework discards the
    just-allocated task_id from the registry. The buyer never sees
    a Submitted envelope referencing an orphan id."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    captured_task_id: list[str] = []

    def _failing_enqueue(task_ctx):
        captured_task_id.append(task_ctx.id)
        raise RuntimeError("trafficker queue down")

    with pytest.raises(RuntimeError, match="trafficker queue down"):
        await _project_workflow_handoff(
            WorkflowHandoff(_failing_enqueue),
            ctx,
            method_name="create_media_buy",
            registry=registry,
            executor=executor,
        )

    # The framework allocated a task_id, called enqueue (which
    # raised), and discarded the id. Registry has nothing.
    assert len(captured_task_id) == 1
    rec = await registry.get(captured_task_id[0])
    assert rec is None


@pytest.mark.asyncio
async def test_workflow_handoff_rolls_back_registry_on_async_enqueue_failure(
    executor: ThreadPoolExecutor,
) -> None:
    """Same rollback semantics for async enqueue."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    captured_task_id: list[str] = []

    async def _failing_enqueue(task_ctx):
        captured_task_id.append(task_ctx.id)
        await asyncio.sleep(0)
        raise RuntimeError("airflow trigger failed")

    with pytest.raises(RuntimeError, match="airflow trigger failed"):
        await _project_workflow_handoff(
            WorkflowHandoff(_failing_enqueue),
            ctx,
            method_name="create_media_buy",
            registry=registry,
            executor=executor,
        )
    rec = await registry.get(captured_task_id[0])
    assert rec is None


# ---- External completion via registry ----


@pytest.mark.asyncio
async def test_external_workflow_completion_transitions_state(
    executor: ThreadPoolExecutor,
) -> None:
    """The adopter's external workflow calls
    ``registry.complete(task_id, result)`` — buyer polling
    ``tasks/get`` then sees the terminal artifact. End-to-end
    integration of the workflow handoff lifecycle."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    enqueued_task_ids: list[str] = []

    envelope = await _project_workflow_handoff(
        WorkflowHandoff(lambda task_ctx: enqueued_task_ids.append(task_ctx.id)),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    task_id = envelope["task_id"]

    # Adopter's external workflow does work, then completes the task.
    await registry.complete(
        task_id,
        {"media_buy_id": "mb_after_human_review", "status": "active"},
    )

    rec = await registry.get(task_id, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"] == {
        "media_buy_id": "mb_after_human_review",
        "status": "active",
    }


@pytest.mark.asyncio
async def test_external_workflow_failure_via_registry_fail(
    executor: ThreadPoolExecutor,
) -> None:
    """Adopter's external workflow can fail the task via
    ``registry.fail(task_id, error)`` — same path the
    TaskHandoff projector uses for adopter-raised errors."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    envelope = await _project_workflow_handoff(
        WorkflowHandoff(lambda task_ctx: None),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    task_id = envelope["task_id"]

    error_payload = AdcpError(
        "POLICY_VIOLATION",
        message="Trafficker rejected: brand mismatch",
        recovery="correctable",
    ).to_wire()
    await registry.fail(task_id, error_payload)

    rec = await registry.get(task_id, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"]["code"] == "POLICY_VIOLATION"


# ---- Integration via _invoke_platform_method ----


@pytest.mark.asyncio
async def test_invoke_platform_method_routes_workflow_handoff(
    executor: ThreadPoolExecutor,
) -> None:
    """End-to-end: a platform method returning ``ctx.handoff_to_workflow(fn)``
    flows through ``_invoke_platform_method`` and produces the
    Submitted envelope without the caller knowing it was a workflow
    handoff. Same dispatch surface as TaskHandoff."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    enqueued: list[str] = []

    def _enqueue(task_ctx):
        enqueued.append(task_ctx.id)

    class _WorkflowPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        def create_media_buy(self, req, ctx):
            return ctx.handoff_to_workflow(_enqueue)

    result = await _invoke_platform_method(
        _WorkflowPlatform(),
        "create_media_buy",
        _ProductsRequest(),
        ctx,
        executor=executor,
        registry=registry,
    )
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert "task_type" not in result
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_workflow_handoff_does_not_run_background_task(
    executor: ThreadPoolExecutor,
) -> None:
    """Critical distinction from TaskHandoff: NO background coroutine
    runs. After projection, the registry stays in ``submitted`` state
    — there's no work to do until the adopter's external workflow
    calls registry.complete().

    Sleep briefly to give any (incorrectly-scheduled) background work
    a chance to run, then assert the state is still submitted."""
    from adcp.decisioning.dispatch import _BACKGROUND_HANDOFF_TASKS

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    initial_bg_tasks = len(_BACKGROUND_HANDOFF_TASKS)

    envelope = await _project_workflow_handoff(
        WorkflowHandoff(lambda task_ctx: None),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    # Yield to give any (incorrectly-scheduled) background work a
    # chance to run.
    await asyncio.sleep(0.05)

    # No background handoff tasks were spawned for this workflow path.
    assert len(_BACKGROUND_HANDOFF_TASKS) == initial_bg_tasks
    # State stays submitted — no fn ran to completion in the framework.
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "submitted"


# ---- Public exports ----


def test_workflow_handoff_publicly_exported() -> None:
    """``WorkflowHandoff`` is on ``adcp.decisioning.__all__`` so
    adopters import from the canonical public surface."""
    import adcp.decisioning as dx

    assert "WorkflowHandoff" in dx.__all__
    assert dx.WorkflowHandoff is WorkflowHandoff


def test_request_context_exposes_handoff_to_workflow() -> None:
    """``RequestContext.handoff_to_workflow`` is the adopter-facing
    seam. Pinned alongside the existing ``handoff_to_task`` so a
    future refactor doesn't drop the workflow primitive."""
    from adcp.decisioning import RequestContext

    assert hasattr(RequestContext, "handoff_to_task")
    assert hasattr(RequestContext, "handoff_to_workflow")
