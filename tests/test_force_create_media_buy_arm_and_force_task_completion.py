"""Tests for force_create_media_buy_arm and force_task_completion.

Parity with adcp/server/tests/unit/training-agent-force-create-media-buy-arm.test.ts
and training-agent-force-task-completion.test.ts (adcp#3115, adcp#3194).

Covers: valid registration, INVALID_PARAMS branches, replay idempotency,
diverging-replay INVALID_TRANSITION, cross-account isolation, and
list_scenarios advertisement.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.test_controller import (
    TestControllerError,
    TestControllerStore,
    _handle_test_controller,
)


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover force_* dispatch / replay / isolation, not the
    sandbox-authority gate. Set the legacy env opt-in so the gate admits
    without requiring per-call resolver wiring. The gate's own behavior
    is exercised in ``test_account_mode_gate.py``."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


# ---------------------------------------------------------------------------
# Concrete store implementations for tests
# ---------------------------------------------------------------------------

_ACCOUNT_A = {"id": "acct-a"}
_ACCOUNT_B = {"id": "acct-b"}


class _ArmStore(TestControllerStore):
    """Stores a single pending force_create_media_buy_arm directive per
    account.  A second registration overwrites; it is consumed (cleared)
    by the first create_media_buy call (not tested here — only the
    registration side is in scope for this PR)."""

    def __init__(self) -> None:
        self._directives: dict[str, dict[str, Any]] = {}

    async def force_create_media_buy_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        key = (account or {}).get("id", "__default__")
        directive: dict[str, Any] = {"arm": arm}
        if task_id is not None:
            directive["task_id"] = task_id
        self._directives[key] = directive
        forced: dict[str, Any] = {"arm": arm}
        if task_id is not None:
            forced["task_id"] = task_id
        return {"success": True, "forced": forced}


class _CompletionStore(TestControllerStore):
    """Stores force_task_completion records with cross-account isolation,
    idempotency, and INVALID_TRANSITION on diverging replay."""

    def __init__(self) -> None:
        # {task_id: {"result": ..., "owner": str}}
        self._tasks: dict[str, dict[str, Any]] = {}

    async def force_task_completion(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        owner = (account or {}).get("id", "__default__")
        if task_id in self._tasks:
            stored = self._tasks[task_id]
            if stored["owner"] != owner:
                raise TestControllerError("NOT_FOUND", f"task {task_id!r} not found")
            if stored["result"] == result:
                # Identical-params replay — idempotent
                return {
                    "success": True,
                    "previous_state": "submitted",
                    "current_state": "completed",
                }
            raise TestControllerError(
                "INVALID_TRANSITION",
                f"task {task_id!r} already completed with different result",
                current_state="completed",
            )
        self._tasks[task_id] = {"result": result, "owner": owner}
        return {"success": True, "previous_state": "submitted", "current_state": "completed"}


class _BothStore(_ArmStore, _CompletionStore):
    """Implements both new scenarios for list_scenarios tests."""

    def __init__(self) -> None:
        _ArmStore.__init__(self)
        _CompletionStore.__init__(self)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _arm_req(params: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"scenario": "force_create_media_buy_arm", "params": params}
    if account is not None:
        req["account"] = account
    return req


def _completion_req(
    params: dict[str, Any], account: dict[str, Any] | None = None
) -> dict[str, Any]:
    req: dict[str, Any] = {"scenario": "force_task_completion", "params": params}
    if account is not None:
        req["account"] = account
    return req


# ===========================================================================
# force_create_media_buy_arm
# ===========================================================================


@pytest.mark.asyncio
async def test_arm_submitted_valid_registration() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(
        store,
        _arm_req({"arm": "submitted", "task_id": "task-abc"}, account=_ACCOUNT_A),
    )
    assert resp["success"] is True
    assert resp["forced"]["arm"] == "submitted"
    assert resp["forced"]["task_id"] == "task-abc"


@pytest.mark.asyncio
async def test_arm_input_required_no_task_id() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(
        store,
        _arm_req({"arm": "input-required"}, account=_ACCOUNT_A),
    )
    assert resp["success"] is True
    assert resp["forced"]["arm"] == "input-required"
    assert "task_id" not in resp["forced"]


@pytest.mark.asyncio
async def test_arm_overwrite_before_consumption() -> None:
    """A second registration before consumption overwrites the first."""
    store = _ArmStore()
    await _handle_test_controller(
        store,
        _arm_req({"arm": "submitted", "task_id": "task-first"}, account=_ACCOUNT_A),
    )
    resp = await _handle_test_controller(
        store,
        _arm_req({"arm": "submitted", "task_id": "task-second"}, account=_ACCOUNT_A),
    )
    assert resp["success"] is True
    assert resp["forced"]["task_id"] == "task-second"
    assert store._directives[_ACCOUNT_A["id"]]["task_id"] == "task-second"


@pytest.mark.asyncio
async def test_arm_invalid_params_missing_arm() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(store, _arm_req({}))
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_arm_invalid_params_bad_arm_value() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(store, _arm_req({"arm": "completed"}))
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_arm_task_id_required_when_submitted() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(store, _arm_req({"arm": "submitted"}))
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "task_id" in resp["error_detail"]


@pytest.mark.asyncio
async def test_arm_task_id_too_long() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(
        store, _arm_req({"arm": "submitted", "task_id": "x" * 129})
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "128" in resp["error_detail"]


@pytest.mark.asyncio
async def test_arm_message_too_long() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(
        store,
        _arm_req({"arm": "input-required", "message": "m" * 2001}),
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "2000" in resp["error_detail"]


@pytest.mark.asyncio
async def test_arm_whitespace_task_id_treated_as_missing() -> None:
    """A whitespace-only task_id is stripped to empty, then treated as absent."""
    store = _ArmStore()
    resp = await _handle_test_controller(store, _arm_req({"arm": "submitted", "task_id": "   "}))
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"


@pytest.mark.asyncio
async def test_arm_task_id_stripped_for_input_required() -> None:
    """task_id is silently dropped for arm='input-required'.

    Forced.extra='forbid' in the response schema means a store that echoes
    task_id on input-required would produce an invalid response. The
    dispatcher strips it before calling the store.
    """
    store = _ArmStore()
    resp = await _handle_test_controller(
        store,
        _arm_req({"arm": "input-required", "task_id": "should-be-dropped"}, account=_ACCOUNT_A),
    )
    assert resp["success"] is True
    assert "task_id" not in resp["forced"]


# ===========================================================================
# force_task_completion
# ===========================================================================

_GOOD_RESULT = {"media_buy_id": "mb-1", "packages": []}


@pytest.mark.asyncio
async def test_completion_valid() -> None:
    store = _CompletionStore()
    resp = await _handle_test_controller(
        store,
        _completion_req({"task_id": "task-1", "result": _GOOD_RESULT}, account=_ACCOUNT_A),
    )
    assert resp["success"] is True
    assert resp["previous_state"] == "submitted"
    assert resp["current_state"] == "completed"


@pytest.mark.asyncio
async def test_completion_idempotent_same_params() -> None:
    store = _CompletionStore()
    params = {"task_id": "task-1", "result": _GOOD_RESULT}
    await _handle_test_controller(store, _completion_req(params, account=_ACCOUNT_A))
    resp = await _handle_test_controller(store, _completion_req(params, account=_ACCOUNT_A))
    assert resp["success"] is True
    assert resp["current_state"] == "completed"


@pytest.mark.asyncio
async def test_completion_diverging_replay_invalid_transition() -> None:
    store = _CompletionStore()
    await _handle_test_controller(
        store,
        _completion_req({"task_id": "task-1", "result": _GOOD_RESULT}, account=_ACCOUNT_A),
    )
    different_result = {"media_buy_id": "mb-2", "packages": []}
    resp = await _handle_test_controller(
        store,
        _completion_req({"task_id": "task-1", "result": different_result}, account=_ACCOUNT_A),
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_TRANSITION"
    assert resp.get("current_state") == "completed"


@pytest.mark.asyncio
async def test_completion_cross_account_not_found() -> None:
    store = _CompletionStore()
    await _handle_test_controller(
        store,
        _completion_req({"task_id": "task-1", "result": _GOOD_RESULT}, account=_ACCOUNT_A),
    )
    resp = await _handle_test_controller(
        store,
        _completion_req({"task_id": "task-1", "result": _GOOD_RESULT}, account=_ACCOUNT_B),
    )
    assert resp["success"] is False
    assert resp["error"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_completion_missing_task_id() -> None:
    store = _CompletionStore()
    resp = await _handle_test_controller(store, _completion_req({"result": _GOOD_RESULT}))
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "task_id" in resp["error_detail"]


@pytest.mark.asyncio
async def test_completion_empty_result_object() -> None:
    store = _CompletionStore()
    resp = await _handle_test_controller(
        store, _completion_req({"task_id": "task-1", "result": {}})
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "non-empty" in resp["error_detail"]


@pytest.mark.asyncio
async def test_completion_task_id_too_long() -> None:
    store = _CompletionStore()
    resp = await _handle_test_controller(
        store, _completion_req({"task_id": "t" * 129, "result": _GOOD_RESULT})
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "128" in resp["error_detail"]


@pytest.mark.asyncio
async def test_completion_result_too_large() -> None:
    store = _CompletionStore()
    large_result = {"data": "x" * (256 * 1024 + 1)}
    resp = await _handle_test_controller(
        store, _completion_req({"task_id": "task-1", "result": large_result})
    )
    assert resp["success"] is False
    assert resp["error"] == "INVALID_PARAMS"
    assert "256" in resp["error_detail"]


# ===========================================================================
# list_scenarios advertisement
# ===========================================================================


@pytest.mark.asyncio
async def test_list_scenarios_both_implemented() -> None:
    store = _BothStore()
    resp = await _handle_test_controller(store, {"scenario": "list_scenarios"})
    assert resp["success"] is True
    assert "force_create_media_buy_arm" in resp["scenarios"]
    assert "force_task_completion" in resp["scenarios"]


@pytest.mark.asyncio
async def test_list_scenarios_neither_implemented() -> None:
    store = TestControllerStore()
    resp = await _handle_test_controller(store, {"scenario": "list_scenarios"})
    assert resp["success"] is True
    assert "force_create_media_buy_arm" not in resp["scenarios"]
    assert "force_task_completion" not in resp["scenarios"]


@pytest.mark.asyncio
async def test_list_scenarios_partial_only_arm() -> None:
    store = _ArmStore()
    resp = await _handle_test_controller(store, {"scenario": "list_scenarios"})
    assert resp["success"] is True
    assert "force_create_media_buy_arm" in resp["scenarios"]
    assert "force_task_completion" not in resp["scenarios"]
