"""Unit tests for adcp.decisioning.task_registry.

Covers:

* :class:`TaskRegistry` Protocol structural matching
* :class:`InMemoryTaskRegistry` lifecycle:
    - issue() returns unique task_id; row stored in 'submitted'
    - update_progress transitions submitted → working on first call
    - update_progress is no-op state-transition on subsequent calls
    - update_progress on unknown task_id silently no-ops (per Protocol
      contract — registry transients must not abort handoff)
    - complete() transitions to 'completed' with result; idempotent on
      equal result; raises on different result
    - fail() transitions to 'failed' with error; idempotent on equal
      error; raises on different error
    - get() returns the dict; cross-tenant probe returns None
    - concurrent issue() yields unique task_ids
* :class:`TaskHandoffContext` ergonomics:
    - update() routes to registry.update_progress; swallows transient errors
    - heartbeat() is a v6.0 no-op

The hostile-probe regression is in
``test_decisioning_task_registry_cross_tenant.py`` (separate file per
the dispatch design's file plan — covers the security boundary
explicitly).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from adcp.decisioning.task_registry import (
    InMemoryTaskRegistry,
    TaskHandoffContext,
    TaskRegistry,
    _noop_heartbeat,
)

# ---- Protocol structural matching ----


def test_in_memory_task_registry_satisfies_protocol() -> None:
    """``InMemoryTaskRegistry`` matches the ``TaskRegistry`` Protocol
    structurally — adopters writing custom registries don't need to
    inherit, just implement the methods."""
    assert isinstance(InMemoryTaskRegistry(), TaskRegistry)


def test_custom_registry_satisfies_protocol_via_duck_typing() -> None:
    """Adopter-written class with the right methods + ``is_durable``
    class attr matches without inheritance."""

    class _Stub:
        is_durable = True  # custom durable impl

        async def issue(self, *, account_id: str, task_type: str) -> str:
            return "task_x"

        async def update_progress(self, task_id: str, progress: dict[str, Any]) -> None:
            pass

        async def complete(self, task_id: str, result: dict[str, Any]) -> None:
            pass

        async def fail(self, task_id: str, error: dict[str, Any]) -> None:
            pass

        async def get(
            self,
            task_id: str,
            *,
            expected_account_id: str | None = None,
        ) -> dict[str, Any] | None:
            return None

    assert isinstance(_Stub(), TaskRegistry)


def test_in_memory_task_registry_is_not_durable() -> None:
    """``InMemoryTaskRegistry.is_durable`` is False — production-mode
    gate refuses by default. Subclasses for instrumentation inherit
    this."""
    assert InMemoryTaskRegistry.is_durable is False
    assert InMemoryTaskRegistry().is_durable is False

    class _InstrumentedSubclass(InMemoryTaskRegistry):
        pass

    assert _InstrumentedSubclass.is_durable is False


# ---- InMemoryTaskRegistry — issue + initial state ----


@pytest.mark.asyncio
async def test_issue_returns_unique_task_id() -> None:
    """Each ``issue()`` allocates a fresh UUID-based id and persists
    a 'submitted' row."""
    reg = InMemoryTaskRegistry()
    a = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    b = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    assert a != b
    assert a.startswith("task_")
    assert b.startswith("task_")


@pytest.mark.asyncio
async def test_issue_initial_state_is_submitted() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "submitted"
    assert rec["task_type"] == "create_media_buy"
    assert rec["account_id"] == "acct_a"
    assert rec["progress"] is None
    assert rec["result"] is None
    assert rec["error"] is None


@pytest.mark.asyncio
async def test_concurrent_issue_yields_unique_ids() -> None:
    """Concurrent calls under the asyncio.Lock all get distinct ids;
    no collision regression."""
    reg = InMemoryTaskRegistry()
    ids = await asyncio.gather(
        *[reg.issue(account_id="acct_a", task_type="create_media_buy") for _ in range(20)]
    )
    assert len(set(ids)) == 20


# ---- update_progress lifecycle ----


@pytest.mark.asyncio
async def test_update_progress_transitions_to_working_on_first_call() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.update_progress(tid, {"step": 1, "message": "validating"})
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "working"
    assert rec["progress"] == {"step": 1, "message": "validating"}


@pytest.mark.asyncio
async def test_update_progress_subsequent_calls_dont_change_state() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.update_progress(tid, {"step": 1})
    await reg.update_progress(tid, {"step": 2})
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "working"
    assert rec["progress"] == {"step": 2}


@pytest.mark.asyncio
async def test_update_progress_unknown_task_is_silent_noop() -> None:
    """Per Protocol contract: registry transients must not abort the
    handoff. Unknown task_id → silent return."""
    reg = InMemoryTaskRegistry()
    # Should NOT raise.
    await reg.update_progress("nonexistent", {"step": 1})


# ---- complete ----


@pytest.mark.asyncio
async def test_complete_transitions_to_completed() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.complete(tid, {"media_buy_id": "mb_1"})
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"] == {"media_buy_id": "mb_1"}


@pytest.mark.asyncio
async def test_complete_is_idempotent_on_equal_result() -> None:
    """Repeated complete() with the same result is a no-op — safe for
    retries on transient post-completion failures."""
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.complete(tid, {"media_buy_id": "mb_1"})
    await reg.complete(tid, {"media_buy_id": "mb_1"})  # idempotent
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"


@pytest.mark.asyncio
async def test_complete_with_different_result_raises() -> None:
    """Re-completion with a different result is a programmer error,
    not silent overwrite."""
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.complete(tid, {"media_buy_id": "mb_1"})
    with pytest.raises(ValueError, match="already completed"):
        await reg.complete(tid, {"media_buy_id": "mb_2"})


@pytest.mark.asyncio
async def test_complete_unknown_task_raises() -> None:
    reg = InMemoryTaskRegistry()
    with pytest.raises(ValueError, match="not found"):
        await reg.complete("nonexistent", {"x": 1})


# ---- fail ----


@pytest.mark.asyncio
async def test_fail_transitions_to_failed() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    err = {
        "code": "BUDGET_TOO_LOW",
        "message": "Below floor",
        "recovery": "correctable",
    }
    await reg.fail(tid, err)
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"] == err


@pytest.mark.asyncio
async def test_fail_is_idempotent_on_equal_error() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    err = {"code": "BUDGET_TOO_LOW", "message": "Below floor"}
    await reg.fail(tid, err)
    await reg.fail(tid, err)  # idempotent
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"


@pytest.mark.asyncio
async def test_fail_with_different_error_raises() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    await reg.fail(tid, {"code": "BUDGET_TOO_LOW"})
    with pytest.raises(ValueError, match="already failed"):
        await reg.fail(tid, {"code": "POLICY_VIOLATION"})


@pytest.mark.asyncio
async def test_fail_unknown_task_raises() -> None:
    reg = InMemoryTaskRegistry()
    with pytest.raises(ValueError, match="not found"):
        await reg.fail("nonexistent", {"code": "BUDGET_TOO_LOW"})


# ---- get ----


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_id() -> None:
    reg = InMemoryTaskRegistry()
    assert await reg.get("nonexistent") is None
    assert await reg.get("nonexistent", expected_account_id="acct_a") is None


@pytest.mark.asyncio
async def test_get_without_expected_account_returns_record() -> None:
    """Unscoped get (e.g., admin tooling) returns the record without
    cross-tenant filtering."""
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    rec = await reg.get(tid)  # no expected_account_id
    assert rec is not None
    assert rec["account_id"] == "acct_a"


# ---- TaskHandoffContext ----


@pytest.mark.asyncio
async def test_handoff_context_update_routes_to_registry() -> None:
    reg = InMemoryTaskRegistry()
    tid = await reg.issue(account_id="acct_a", task_type="create_media_buy")
    handoff_ctx = TaskHandoffContext(id=tid, _registry=reg)
    await handoff_ctx.update({"step": 1})
    rec = await reg.get(tid, expected_account_id="acct_a")
    assert rec is not None
    assert rec["progress"] == {"step": 1}


@pytest.mark.asyncio
async def test_handoff_context_update_swallows_registry_errors(caplog) -> None:
    """A transient registry write failure must not abort the handoff
    fn. ``update`` swallows; the buyer-facing impact is a missed
    progress event, not a failed task. Round-4 review: the swallow
    now logs at WARNING with traceback so transient failures aren't
    silently invisible to operators."""
    failing_registry = AsyncMock(spec=TaskRegistry)
    failing_registry.update_progress.side_effect = RuntimeError("DB down")
    handoff_ctx = TaskHandoffContext(id="task_x", _registry=failing_registry)
    import logging

    with caplog.at_level(logging.WARNING):
        # Must NOT raise.
        await handoff_ctx.update({"step": 1})
    failing_registry.update_progress.assert_called_once_with("task_x", {"step": 1})
    # Round-4 review: swallow now logs WARNING with traceback.
    assert any(
        "task_x" in r.message and "registry transient" in r.message for r in caplog.records
    ), "TaskHandoffContext.update suppression must log WARNING with task_id"


@pytest.mark.asyncio
async def test_handoff_context_heartbeat_is_noop() -> None:
    """v6.0 ships heartbeat as a no-op — adopters can call it for
    future-proofing without effect today."""
    reg = InMemoryTaskRegistry()
    handoff_ctx = TaskHandoffContext(id="task_x", _registry=reg)
    # Just verify it returns without error.
    await handoff_ctx.heartbeat()


@pytest.mark.asyncio
async def test_noop_heartbeat_is_awaitable() -> None:
    """Module-level _noop_heartbeat is an awaitable; importable for
    custom test harnesses."""
    await _noop_heartbeat()
