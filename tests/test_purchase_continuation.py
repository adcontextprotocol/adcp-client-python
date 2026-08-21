"""AdCP 3.2 legacy purchase continuation conformance tests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adcp.compat import (
    CompatibilityContinuationError,
    CompatibilityContinuationErrorCode,
    InMemoryCompatibilityContinuationStore,
    LegacyPurchaseCoordinator,
    PendingTaskResolution,
    ReconciliationResult,
    SqliteCompatibilityContinuationStore,
)
from adcp.types import CompatibilityPurchaseCoordinatorInput
from adcp.types.core import TaskResult, TaskStatus
from adcp.validation import get_bundle_adcp_version, validate_response

_VECTORS = (
    Path(__file__).parent
    / "conformance"
    / "vectors"
    / "products-only-brief-compatibility"
    / "vectors.json"
)
_NOW = datetime(2098, 1, 1, tzinfo=timezone.utc)
_TOKEN_KEY = b"test-only-continuation-token-key-32-bytes-minimum"


def _cases() -> list[dict[str, Any]]:
    return json.loads(_VECTORS.read_text())["cases"]


def test_all_signed_compact_projection_vectors_validate_against_beta4() -> None:
    vectors = json.loads(_VECTORS.read_text())
    projections = [case["compact_projection"] for case in vectors["cases"]]
    projections += [case["compact_projection"] for case in vectors["listed_purchase_cases"]]
    for projection in projections:
        outcome = validate_response("request_proposals", projection, version="3.2-beta.4")
        assert outcome.valid, outcome.issues


def _coordinator(store: Any, executor: Any, *, reconciler: Any = None) -> LegacyPurchaseCoordinator:
    return LegacyPurchaseCoordinator(
        store=store,
        executor=executor,
        reconciler=reconciler,
        token_derivation_key=_TOKEN_KEY,
        allow_non_durable_store=not store.is_durable,
        clock=lambda: _NOW,
    )


def _success_result(source_version: str, media_buy_id: str) -> dict[str, Any]:
    if source_version.startswith("2.5."):
        return {"media_buy_id": media_buy_id, "buyer_ref": "buyer-ref", "packages": []}
    if source_version.startswith("3.1."):
        return {
            "media_buy_id": media_buy_id,
            "confirmed_at": "2098-01-01T00:00:00Z",
            "revision": 1,
            "packages": [],
        }
    return {"media_buy_id": media_buy_id, "packages": []}


def _success_for(ctx: Any, media_buy_id: str) -> dict[str, Any]:
    return _success_result(ctx.source_adcp_version, media_buy_id)


async def _issue(
    coordinator: LegacyPurchaseCoordinator,
    case: dict[str, Any],
    *,
    principal: str = "principal-acme",
    target: str = "seller-session-acme",
) -> None:
    continuation = case["compact_projection"]["purchase_continuation"]
    token = await coordinator.issue_legacy_create_continuation(
        principal_id=principal,
        issuance_idempotency_key=f"discovery-{case['source_version']}",
        account=case["continuation_input"]["account"],
        source_adcp_version=case["source_version"],
        expires_at=datetime.fromisoformat(
            continuation["continuation_expires_at"].replace("Z", "+00:00")
        ),
        observed_request=case["legacy_request"],
        observed_response=case["legacy_response"],
        product_ids=continuation["product_ids"],
        buyer_visible_products=case["compact_projection"]["products"],
        losses=continuation["losses"],
        target_binding=target,
        mutation_idempotency_guaranteed=not case["source_version"].startswith("2.5."),
    )
    case["continuation_input"]["continuation_token"] = token


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["source_version"])
async def test_upstream_vectors_execute_and_replay(case: dict[str, Any]) -> None:
    calls: list[Any] = []

    async def execute(ctx: Any) -> dict[str, Any]:
        calls.append(ctx)
        return _success_for(ctx, f"mb-{ctx.selected_product_ids[0]}")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)

    first = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    replay = await coordinator.continue_legacy_purchase(
        copy.deepcopy(case["continuation_input"]),
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    assert first == replay
    assert len(calls) == 1
    assert calls[0].source_adcp_version == case["source_version"]


@pytest.mark.asyncio
async def test_concurrent_exact_retries_execute_once() -> None:
    case = _cases()[2]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _success_for(ctx, "mb-once")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    first = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            copy.deepcopy(case["continuation_input"]),
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert calls == 1
    assert {type(value) for value in outcomes} == {dict, CompatibilityContinuationError}
    error = next(value for value in outcomes if isinstance(value, CompatibilityContinuationError))
    assert error.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION


@pytest.mark.asyncio
async def test_in_flight_not_applied_reconciliation_cannot_reopen_live_executor() -> None:
    case = _cases()[2]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _success_for(ctx, "mb-once")

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.not_applied()

    coordinator = _coordinator(
        InMemoryCompatibilityContinuationStore(), execute, reconciler=reconcile
    )
    await _issue(coordinator, case)
    first = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await entered.wait()
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    release.set()
    assert await first == _success_result(case["source_version"], "mb-once")
    assert exc.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION
    assert calls == 1


@pytest.mark.asyncio
async def test_different_idempotency_keys_cannot_double_claim() -> None:
    case = _cases()[1]
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, "mb-once")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    other = copy.deepcopy(case["continuation_input"])
    other["idempotency_key"] = "9eea24eb-9594-4705-a6c3-f0913031dd26"
    outcomes = await asyncio.gather(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        ),
        coordinator.continue_legacy_purchase(
            other,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        ),
        return_exceptions=True,
    )

    assert calls == 1
    errors = [value for value in outcomes if isinstance(value, CompatibilityContinuationError)]
    assert len(errors) == 1
    assert errors[0].code == CompatibilityContinuationErrorCode.ALREADY_CLAIMED


@pytest.mark.asyncio
async def test_exception_is_ambiguous_and_never_blindly_retried() -> None:
    case = _cases()[2]
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TimeoutError("response lost after possible commit")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    for _ in range(2):
        with pytest.raises(CompatibilityContinuationError) as exc:
            await coordinator.continue_legacy_purchase(
                case["continuation_input"],
                principal_id="principal-acme",
                target_binding="seller-session-acme",
            )
        assert exc.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION
    assert calls == 1


@pytest.mark.asyncio
async def test_authoritative_reconciliation_replays_applied_result() -> None:
    case = _cases()[2]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TimeoutError

    store = InMemoryCompatibilityContinuationStore()
    coordinator = _coordinator(store, execute)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError):
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.applied(
            _success_result(case["source_version"], "mb-reconciled")
        )

    recovered = _coordinator(store, execute, reconciler=reconcile)
    result = await recovered.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    replay = await recovered.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == replay == _success_result(case["source_version"], "mb-reconciled")
    assert calls == 1


@pytest.mark.asyncio
async def test_authoritatively_not_applied_resumes_ambiguous_operation() -> None:
    case = _cases()[2]
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError
        return _success_for(ctx, "mb-resumed")

    store = InMemoryCompatibilityContinuationStore()
    initial = _coordinator(store, execute)
    await _issue(initial, case)
    with pytest.raises(CompatibilityContinuationError) as initial_error:
        await initial.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.not_applied()

    recovered = _coordinator(store, execute, reconciler=reconcile)
    snapshot = await recovered.get_legacy_purchase_operation(
        initial_error.value.details["operation_id"],
        principal_id="principal-acme",
    )
    result = await recovered.recover_legacy_purchase(
        snapshot,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == _success_result(case["source_version"], "mb-resumed")
    assert calls == 2


@pytest.mark.asyncio
async def test_public_recovery_fences_in_flight_with_revision_cas() -> None:
    case = copy.deepcopy(_cases()[2])
    store = InMemoryCompatibilityContinuationStore()

    async def execute(ctx: Any) -> dict[str, Any]:
        return _success_for(ctx, "mb-recovered-after-crash")

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.not_applied()

    coordinator = _coordinator(store, execute, reconciler=reconcile)
    await _issue(coordinator, case)
    token = case["continuation_input"]["continuation_token"]
    execution_input = {
        key: copy.deepcopy(value)
        for key, value in case["continuation_input"].items()
        if key != "continuation_token"
    }
    claimed = await store.claim(
        hashlib.sha256(token.encode()).hexdigest(),
        principal_id="principal-acme",
        idempotency_key=case["continuation_input"]["idempotency_key"],
        payload_hash="simulated-hard-loss-payload",
        execution_input=execution_input,
        now=_NOW,
    )
    in_flight = await store.mark_in_flight(claimed)
    snapshot = await coordinator.get_legacy_purchase_operation(
        in_flight.operation_id, principal_id="principal-acme"
    )
    result = await coordinator.recover_legacy_purchase(
        snapshot,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == _success_result(case["source_version"], "mb-recovered-after-crash")
    with pytest.raises(CompatibilityContinuationError) as stale:
        await coordinator.recover_legacy_purchase(
            snapshot,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert stale.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT


@pytest.mark.asyncio
async def test_sqlite_store_survives_restart_and_replays(tmp_path: Path) -> None:
    case = _cases()[1]
    database = tmp_path / "continuations.sqlite3"
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, "mb-durable")

    first = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    await _issue(first, case)
    result = await first.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    async def must_not_execute(_ctx: Any) -> dict[str, Any]:
        raise AssertionError("durable replay called the seller")

    restarted = _coordinator(SqliteCompatibilityContinuationStore(database), must_not_execute)
    replay = await restarted.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == replay == _success_result(case["source_version"], "mb-durable")
    assert calls == 1


@pytest.mark.asyncio
async def test_sqlite_stores_share_one_atomic_claim(tmp_path: Path) -> None:
    case = _cases()[2]
    database = tmp_path / "continuations.sqlite3"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return _success_for(ctx, "mb-sqlite-once")

    first = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    second = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    await _issue(first, case)
    first_task = asyncio.create_task(
        first.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await entered.wait()
    other = copy.deepcopy(case["continuation_input"])
    other["idempotency_key"] = "40be257f-8c2e-434f-bd30-e549266bd5c9"
    with pytest.raises(CompatibilityContinuationError) as exc:
        await second.continue_legacy_purchase(
            other,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    release.set()
    assert await first_task == _success_result(case["source_version"], "mb-sqlite-once")
    assert exc.value.code == CompatibilityContinuationErrorCode.ALREADY_CLAIMED
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_leaves_operation_ambiguous() -> None:
    case = _cases()[1]
    entered = asyncio.Event()
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()
        return {}

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    task = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_after_reservation_commit_never_calls_executor() -> None:
    case = _cases()[2]

    class DelayedReservationStore(InMemoryCompatibilityContinuationStore):
        def __init__(self) -> None:
            super().__init__()
            self.committed = asyncio.Event()
            self.release = asyncio.Event()
            self.reserved_operation: Any = None

        async def mark_in_flight(self, operation: Any) -> Any:
            reserved = await super().mark_in_flight(operation)
            self.reserved_operation = reserved
            self.committed.set()
            await self.release.wait()
            return reserved

    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    store = DelayedReservationStore()
    coordinator = _coordinator(store, execute)
    await _issue(coordinator, case)
    task = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await store.committed.wait()
    task.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation = await store.get_operation(
        store.reserved_operation.operation_id, principal_id="principal-acme"
    )
    assert operation is not None
    assert operation.state.value == "ambiguous"
    assert calls == 0


@pytest.mark.asyncio
async def test_invalid_executor_result_is_not_persisted_as_success() -> None:
    case = _cases()[2]
    coordinator = _coordinator(
        InMemoryCompatibilityContinuationStore(), lambda _ctx: {"media_buy_id": "invalid"}
    )
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE
    assert exc.value.details["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "submitted", "task_id": "task-123"},
        {"status": "working", "task_id": "task-working", "percentage": 25},
        {
            "status": "input-required",
            "task_id": "task-input",
            "reason": "APPROVAL_REQUIRED",
            "errors": [{"code": "APPROVAL_REQUIRED", "message": "Approve purchase"}],
        },
        {"errors": [{"code": "BUDGET_TOO_LOW", "message": "Increase budget"}]},
        TaskResult(
            status=TaskStatus.SUBMITTED,
            submitted={
                "webhook_url": "https://buyer.example/webhook",
                "operation_id": "task-wrapper-123",
            },
        ),
    ],
)
async def test_non_success_legacy_results_are_validated_and_replayed(result: Any) -> None:
    case = _cases()[2]
    calls = 0

    def execute(_ctx: Any) -> Any:
        nonlocal calls
        calls += 1
        return result

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    first = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    replay = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert first == replay
    assert calls == 1


@pytest.mark.asyncio
async def test_pending_result_can_be_polled_to_completion_with_revision_fencing() -> None:
    case = _cases()[2]
    store = InMemoryCompatibilityContinuationStore()
    pending = {"status": "submitted", "task_id": "task-123"}
    resolutions = iter(
        [
            {"status": "working", "task_id": "task-123", "percentage": 75},
            _success_result(case["source_version"], "mb-pending-complete"),
        ]
    )

    async def resolve(_ctx: Any, _operation: Any) -> PendingTaskResolution:
        return PendingTaskResolution("task-123", next(resolutions))

    coordinator = LegacyPurchaseCoordinator(
        store=store,
        executor=lambda _ctx: pending,
        pending_poller=resolve,
        token_derivation_key=_TOKEN_KEY,
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    await _issue(coordinator, case)
    assert (
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
        == pending
    )
    first_snapshot = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )

    working = await coordinator.refresh_pending_legacy_purchase(
        first_snapshot,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert working["status"] == "working"
    second_snapshot = await coordinator.get_legacy_purchase_operation(
        first_snapshot.operation_id, principal_id="principal-acme"
    )
    assert second_snapshot.revision == first_snapshot.revision + 1
    with pytest.raises(CompatibilityContinuationError) as stale:
        await coordinator.refresh_pending_legacy_purchase(
            first_snapshot,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert stale.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT

    completed = await coordinator.refresh_pending_legacy_purchase(
        second_snapshot,
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert completed == _success_result(case["source_version"], "mb-pending-complete")
    assert (
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
        == completed
    )


@pytest.mark.asyncio
async def test_pending_refresh_rejects_task_identity_substitution() -> None:
    case = _cases()[2]
    store = InMemoryCompatibilityContinuationStore()
    coordinator = LegacyPurchaseCoordinator(
        store=store,
        executor=lambda _ctx: {"status": "submitted", "task_id": "task-original"},
        pending_poller=lambda _ctx, _operation: PendingTaskResolution(
            "task-substituted",
            {"status": "working", "task_id": "task-substituted", "percentage": 10},
        ),
        token_derivation_key=_TOKEN_KEY,
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    snapshot = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.refresh_pending_legacy_purchase(
            snapshot,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE
    current = await coordinator.get_legacy_purchase_operation(
        snapshot.operation_id, principal_id="principal-acme"
    )
    assert current == snapshot


@pytest.mark.asyncio
async def test_pending_refresh_binds_terminal_result_to_original_task() -> None:
    case = _cases()[2]
    store = InMemoryCompatibilityContinuationStore()
    coordinator = LegacyPurchaseCoordinator(
        store=store,
        executor=lambda _ctx: {"status": "submitted", "task_id": "task-original"},
        pending_poller=lambda _ctx, _operation: PendingTaskResolution(
            "task-other", _success_result(case["source_version"], "mb-wrong-task")
        ),
        token_derivation_key=_TOKEN_KEY,
        allow_non_durable_store=True,
        clock=lambda: _NOW,
    )
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    snapshot = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.refresh_pending_legacy_purchase(
            snapshot,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE
    assert (
        await coordinator.get_legacy_purchase_operation(
            snapshot.operation_id, principal_id="principal-acme"
        )
        == snapshot
    )


@pytest.mark.asyncio
async def test_pending_refresh_requires_configured_poller() -> None:
    case = _cases()[2]
    store = InMemoryCompatibilityContinuationStore()
    coordinator = _coordinator(
        store, lambda _ctx: {"status": "submitted", "task_id": "task-unresolved"}
    )
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    snapshot = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.refresh_pending_legacy_purchase(
            snapshot,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.PENDING_RESOLUTION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["source_version"])
@pytest.mark.parametrize("arm", ["submitted", "errors"])
async def test_completed_task_result_accepts_any_valid_legacy_arm(
    case: dict[str, Any], arm: str
) -> None:
    data = (
        {"status": "submitted", "task_id": "task-completed-wrapper"}
        if arm == "submitted"
        else {"errors": [{"code": "INVALID_REQUEST", "message": "Rejected"}]}
    )
    calls = 0

    def execute(_ctx: Any) -> TaskResult[Any]:
        nonlocal calls
        calls += 1
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=arm != "errors",
            data=data,
        )

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    first = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    replay = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert first == replay == data
    assert calls == 1


@pytest.mark.asyncio
async def test_task_result_status_must_match_validated_payload_arm() -> None:
    case = _cases()[2]
    invalid = TaskResult(
        status=TaskStatus.FAILED,
        success=False,
        data=_success_result(case["source_version"], "mb-stale-success"),
    )
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: invalid)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE


@pytest.mark.asyncio
async def test_submitted_task_result_requires_task_identity() -> None:
    case = _cases()[2]
    coordinator = _coordinator(
        InMemoryCompatibilityContinuationStore(),
        lambda _ctx: TaskResult(status=TaskStatus.SUBMITTED),
    )
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_LEGACY_RESPONSE


@pytest.mark.asyncio
async def test_executor_result_rejects_signed_url_before_persistence() -> None:
    case = _cases()[2]
    store = InMemoryCompatibilityContinuationStore()
    coordinator = _coordinator(
        store,
        lambda _ctx: {
            "status": "submitted",
            "task_id": "task-sensitive",
            "message": "https://seller.example/task?token=secret",
        },
    )
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.PERSISTENCE_POLICY
    operation = await coordinator.get_legacy_purchase_operation_by_idempotency_key(
        case["continuation_input"]["idempotency_key"], principal_id="principal-acme"
    )
    assert operation.state.value == "ambiguous"
    assert operation.result is None


@pytest.mark.asyncio
async def test_synchronous_executor_runs_off_event_loop_thread() -> None:
    case = _cases()[2]
    event_loop_thread = threading.get_ident()
    executor_thread: int | None = None

    def execute(ctx: Any) -> dict[str, Any]:
        nonlocal executor_thread
        executor_thread = threading.get_ident()
        return _success_for(ctx, "mb-threaded")

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert executor_thread is not None
    assert executor_thread != event_loop_thread


@pytest.mark.asyncio
async def test_buyer_visible_pricing_projection_is_authoritative() -> None:
    case = copy.deepcopy(_cases()[2])
    hidden = copy.deepcopy(case["legacy_response"]["products"][0]["pricing_options"][0])
    hidden["pricing_option_id"] = "hidden-option"
    case["legacy_response"]["products"][0]["pricing_options"].append(hidden)
    case["continuation_input"]["legacy_create_request"]["packages"][0][
        "pricing_option_id"
    ] = "hidden-option"
    calls = 0

    def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH
    assert calls == 0


@pytest.mark.asyncio
async def test_buyer_visible_pricing_terms_must_match_observed_terms() -> None:
    case = copy.deepcopy(_cases()[2])
    case["compact_projection"]["products"][0]["pricing_options"][0]["fixed_price"] = 1
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH


@pytest.mark.asyncio
async def test_buyer_visible_pricing_cannot_omit_observed_commercial_terms() -> None:
    case = copy.deepcopy(_cases()[2])
    del case["compact_projection"]["products"][0]["pricing_options"][0]["fixed_price"]
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projected_value", "observed_value"),
    [(1, 2), (True, None)],
)
async def test_buyer_visible_pricing_extensions_fail_closed(
    projected_value: Any, observed_value: Any
) -> None:
    case = copy.deepcopy(_cases()[2])
    projected = case["compact_projection"]["products"][0]["pricing_options"][0]
    observed = case["legacy_response"]["products"][0]["pricing_options"][0]
    key = "x-billing-multiplier" if observed_value is not None else "max_bid"
    projected[key] = projected_value
    if observed_value is not None:
        observed[key] = observed_value
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH


@pytest.mark.asyncio
async def test_31_pricing_does_not_apply_25_field_normalization() -> None:
    case = copy.deepcopy(_cases()[2])
    observed = case["legacy_response"]["products"][0]["pricing_options"][0]
    observed["rate"] = 999
    observed["is_fixed"] = True
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH


@pytest.mark.asyncio
async def test_31_without_replay_guarantee_requires_mutation_loss() -> None:
    case = copy.deepcopy(_cases()[2])
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    continuation = case["compact_projection"]["purchase_continuation"]
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.issue_legacy_create_continuation(
            principal_id="principal-acme",
            issuance_idempotency_key="invalid-projected-pricing",
            account=case["continuation_input"]["account"],
            source_adcp_version=case["source_version"],
            expires_at=datetime.fromisoformat(
                continuation["continuation_expires_at"].replace("Z", "+00:00")
            ),
            observed_request=case["legacy_request"],
            observed_response=case["legacy_response"],
            product_ids=continuation["product_ids"],
            buyer_visible_products=case["compact_projection"]["products"],
            losses=continuation["losses"],
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_INPUT


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["false", 1, None])
async def test_replay_guarantee_requires_strict_boolean(invalid: Any) -> None:
    case = copy.deepcopy(_cases()[2])
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    continuation = case["compact_projection"]["purchase_continuation"]
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.issue_legacy_create_continuation(
            principal_id="principal-acme",
            issuance_idempotency_key="invalid-fixed-pricing",
            account=case["continuation_input"]["account"],
            source_adcp_version=case["source_version"],
            expires_at=datetime.fromisoformat(
                continuation["continuation_expires_at"].replace("Z", "+00:00")
            ),
            observed_request=case["legacy_request"],
            observed_response=case["legacy_response"],
            product_ids=continuation["product_ids"],
            buyer_visible_products=case["compact_projection"]["products"],
            losses=continuation["losses"],
            target_binding="seller-session-acme",
            mutation_idempotency_guaranteed=invalid,
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_INPUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda payload: payload["account"].update(account_id="other-account"),
            CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        ),
        (
            lambda payload: payload["accepted_losses"].append(
                "mutation_idempotency_not_guaranteed"
            ),
            CompatibilityContinuationErrorCode.LOSS_MISMATCH,
        ),
        (
            lambda payload: payload["selected_product_ids"].__setitem__(0, "substitute"),
            CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        ),
        (
            lambda payload: payload["legacy_create_request"]["packages"][0].__setitem__(
                "product_id", "substitute"
            ),
            CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        ),
        (
            lambda payload: payload["legacy_create_request"]["packages"][0].__setitem__(
                "pricing_option_id", "unobserved-option"
            ),
            CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        ),
        (
            lambda payload: payload["legacy_create_request"]["account"].update(
                account_id="other-account"
            ),
            CompatibilityContinuationErrorCode.BINDING_MISMATCH,
        ),
    ],
)
async def test_all_substitution_and_consent_mismatches_fail_before_call(
    mutation: Any, code: CompatibilityContinuationErrorCode
) -> None:
    case = _cases()[2]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    bad = copy.deepcopy(case["continuation_input"])
    mutation(bad)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            bad,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == code
    assert calls == 0


@pytest.mark.asyncio
async def test_principal_and_target_rebinding_are_rejected_before_call() -> None:
    case = _cases()[0]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), execute)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as principal_error:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="other-principal",
            target_binding="seller-session-acme",
        )
    assert principal_error.value.code == CompatibilityContinuationErrorCode.NOT_FOUND

    with pytest.raises(CompatibilityContinuationError) as target_error:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="other-seller-session",
        )
    assert target_error.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH
    assert calls == 0


@pytest.mark.asyncio
async def test_issuance_rejects_observed_discovery_account_rebinding() -> None:
    case = copy.deepcopy(_cases()[2])
    case["legacy_request"]["account"]["account_id"] = "other-account"
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.BINDING_MISMATCH


@pytest.mark.asyncio
async def test_issuance_rejects_source_patch_without_exact_bundled_schema() -> None:
    case = copy.deepcopy(_cases()[1])
    case["source_version"] = "3.0.17"
    coordinator = _coordinator(InMemoryCompatibilityContinuationStore(), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_INPUT
    assert "bundled source schema release" in str(exc.value)


@pytest.mark.asyncio
async def test_identical_issuance_is_idempotent_across_sqlite_restart(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"
    first = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(first, case)
    first_token = case["continuation_input"]["continuation_token"]

    restarted = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(restarted, case)
    assert case["continuation_input"]["continuation_token"] == first_token
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_pre_fingerprint_authorization_blocks_duplicate_reissuance(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(database, clock=lambda: mutable_now)
    first = _coordinator(store, lambda _ctx: {})
    await _issue(first, case)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute(
            "UPDATE adcp_compat_continuations "
            "SET token_hash = ?, issuance_fingerprint = NULL, issuance_binding_hash = NULL",
            ("f" * 64,),
        )

    restarted = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(restarted, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1

    # The old row has no stable issuance fingerprint from which to build a
    # compact tombstone, so automatic cleanup must retain its full replay fence.
    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(mutable_now) == 0
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_reused_issuance_key_with_changed_discovery_conflicts(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"
    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(coordinator, case)
    changed = copy.deepcopy(case)
    changed["legacy_request"]["brief"] = "A materially different brief."
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, changed)
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_purged_issuance_key_with_changed_bindings_remains_retired(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3", clock=lambda: mutable_now
    )
    coordinator = _coordinator(store, lambda ctx: _success_for(ctx, "mb-before-purge"))
    await _issue(coordinator, case)
    old_token = case["continuation_input"]["continuation_token"]
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(mutable_now) == 1

    changed = copy.deepcopy(case)
    changed["legacy_request"]["brief"] = "A new discovery with changed authorization bindings."
    with pytest.raises(CompatibilityContinuationError) as retired:
        await _issue(coordinator, changed)
    assert retired.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT
    assert changed["continuation_input"]["continuation_token"] == old_token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://creative.example/render?X-Amz-Signature=secret",
        "https://creative.example/render?a=1;X-Amz-Signature=secret",
        "https://creative.example/render?a=1%3BX-Amz-Signature=secret",
    ],
)
async def test_issuance_rejects_presigned_url_before_persistence(tmp_path: Path, url: str) -> None:
    case = copy.deepcopy(_cases()[2])
    case["legacy_response"]["products"][0]["format_ids"][0]["agent_url"] = url
    coordinator = _coordinator(
        SqliteCompatibilityContinuationStore(tmp_path / "continuations.sqlite3"),
        lambda _ctx: {},
    )
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.PERSISTENCE_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "apiKey",
        "clientSecret",
        "auth_token",
        "key",
        "signature",
        "accessKeyId",
        "jwt",
        "authorizationCode",
        "APIKey",
        "sellerAccessKEYID",
        "credentialValue",
        "accessTokenValue",
        "accessTokenDataValue",
    ],
)
async def test_issuance_rejects_credential_aliases_before_persistence(
    tmp_path: Path, field: str
) -> None:
    case = copy.deepcopy(_cases()[2])
    case["legacy_response"][field] = "must-not-persist"
    database = tmp_path / "continuations.sqlite3"
    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.PERSISTENCE_POLICY
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_redemption_rejects_push_notification_config_before_claim() -> None:
    case = copy.deepcopy(_cases()[2])
    store = InMemoryCompatibilityContinuationStore()
    coordinator = _coordinator(store, lambda _ctx: {})
    await _issue(coordinator, case)
    case["continuation_input"]["legacy_create_request"]["push_notification_config"] = {
        "url": "https://buyer.example/events",
    }
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.PERSISTENCE_POLICY
    assert not store._operations


@pytest.mark.asyncio
async def test_expiry_is_rejected_before_claim_or_call() -> None:
    case = _cases()[1]
    mutable_now = _NOW
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    store = InMemoryCompatibilityContinuationStore()
    coordinator = LegacyPurchaseCoordinator(
        store=store,
        executor=execute,
        allow_non_durable_store=True,
        clock=lambda: mutable_now,
    )
    await _issue(coordinator, case)
    mutable_now = datetime(2100, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.EXPIRED
    assert calls == 0


@pytest.mark.asyncio
async def test_store_refreshes_expiry_clock_after_atomic_lock(tmp_path: Path) -> None:
    """A stale coordinator timestamp cannot redeem an already-expired token."""
    case = _cases()[1]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3",
        clock=lambda: datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    coordinator = _coordinator(store, execute)
    await _issue(coordinator, case)

    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    assert exc.value.code == CompatibilityContinuationErrorCode.EXPIRED
    assert calls == 0


@pytest.mark.asyncio
async def test_completed_operation_replays_after_token_expiry() -> None:
    case = _cases()[1]
    mutable_now = _NOW
    calls = 0

    async def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, "mb-before-expiry")

    coordinator = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute,
        allow_non_durable_store=True,
        clock=lambda: mutable_now,
    )
    await _issue(coordinator, case)
    first = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    mutable_now = datetime(2100, 1, 1, tzinfo=timezone.utc)
    replay = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert first == replay == _success_result(case["source_version"], "mb-before-expiry")
    assert calls == 1


def test_production_default_rejects_in_memory_store() -> None:
    with pytest.raises(ValueError, match="durable store"):
        LegacyPurchaseCoordinator(
            store=InMemoryCompatibilityContinuationStore(),
            executor=lambda _ctx: {},
        )


def test_durable_coordinator_requires_stable_token_derivation_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token_derivation_key"):
        LegacyPurchaseCoordinator(
            store=SqliteCompatibilityContinuationStore(tmp_path / "continuations.sqlite3"),
            executor=lambda _ctx: {},
        )

    with pytest.raises(TypeError, match="bytes-like"):
        LegacyPurchaseCoordinator(
            store=SqliteCompatibilityContinuationStore(tmp_path / "integer-key.sqlite3"),
            executor=lambda _ctx: {},
            token_derivation_key=32,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="high-entropy"):
        LegacyPurchaseCoordinator(
            store=SqliteCompatibilityContinuationStore(tmp_path / "zero-key.sqlite3"),
            executor=lambda _ctx: {},
            token_derivation_key=b"\x00" * 32,
        )


def test_sqlite_store_rejects_memory_database() -> None:
    with pytest.raises(ValueError, match="file-backed"):
        SqliteCompatibilityContinuationStore(":memory:")


def test_sqlite_store_validates_positive_quotas(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_records"):
        SqliteCompatibilityContinuationStore(tmp_path / "continuations.sqlite3", max_records=0)


@pytest.mark.asyncio
async def test_sqlite_record_quota_rolls_back_claim_and_can_be_retried(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"
    constrained = _coordinator(
        SqliteCompatibilityContinuationStore(database, max_records=1), lambda _ctx: {}
    )
    await _issue(constrained, case)
    with pytest.raises(CompatibilityContinuationError) as exc:
        await constrained.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED

    calls = 0

    def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, "mb-after-capacity")

    reopened = _coordinator(SqliteCompatibilityContinuationStore(database, max_records=2), execute)
    result = await reopened.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == _success_result(case["source_version"], "mb-after-capacity")
    assert calls == 1


@pytest.mark.asyncio
async def test_sqlite_principal_quota_does_not_consume_other_principal_capacity(
    tmp_path: Path,
) -> None:
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3",
        max_records=10,
        max_records_per_principal=1,
    )
    coordinator = _coordinator(store, lambda _ctx: {})
    first = copy.deepcopy(_cases()[1])
    await _issue(coordinator, first, principal="principal-one")

    same_principal = copy.deepcopy(_cases()[2])
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, same_principal, principal="principal-one")
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED

    other_principal = copy.deepcopy(_cases()[2])
    await _issue(coordinator, other_principal, principal="principal-two")
    with closing(sqlite3.connect(store.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_sqlite_principal_byte_quota_counts_target_binding(tmp_path: Path) -> None:
    database = tmp_path / "continuations.sqlite3"
    store = SqliteCompatibilityContinuationStore(
        database,
        max_bytes=1_000_000,
        max_bytes_per_principal=20_000,
    )
    coordinator = _coordinator(store, lambda _ctx: {})
    oversized = copy.deepcopy(_cases()[2])

    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(
            coordinator,
            oversized,
            principal="principal-noisy",
            target="x" * 50_000,
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED

    ordinary = copy.deepcopy(_cases()[2])
    await _issue(coordinator, ordinary, principal="principal-other")
    with sqlite3.connect(database) as conn:
        principals = conn.execute("SELECT principal_id FROM adcp_compat_continuations").fetchall()
    assert principals == [("principal-other",)]


@pytest.mark.asyncio
async def test_sqlite_payload_quota_rejects_large_discovery(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    case["legacy_response"]["products"][0]["description"] = "x" * 5_000
    coordinator = _coordinator(
        SqliteCompatibilityContinuationStore(
            tmp_path / "continuations.sqlite3", max_payload_bytes=1_000
        ),
        lambda _ctx: {},
    )
    with pytest.raises(CompatibilityContinuationError) as exc:
        await _issue(coordinator, case)
    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_sqlite_reserves_terminal_payload_before_executor_runs(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    calls = 0

    def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    coordinator = _coordinator(
        SqliteCompatibilityContinuationStore(
            tmp_path / "continuations.sqlite3",
            max_bytes=100_000,
            max_payload_bytes=1_000_000,
        ),
        execute,
    )
    await _issue(coordinator, case)

    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    assert exc.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED
    assert calls == 0


@pytest.mark.asyncio
async def test_sqlite_quota_cannot_strand_terminal_write_after_execution(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(ctx: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return _success_for(ctx, "mb-reserved")

    store = SqliteCompatibilityContinuationStore(tmp_path / "continuations.sqlite3")
    coordinator = _coordinator(store, execute)
    await _issue(coordinator, case)
    purchase = asyncio.create_task(
        coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    )
    await entered.wait()

    # Simulate ledger capacity being tightened or exhausted after the seller
    # mutation starts. Its pre-reserved terminal write must still commit.
    store.max_bytes = 1
    store.max_bytes_per_principal = 1
    store.max_payload_bytes = 1
    release.set()

    assert await purchase == _success_result(case["source_version"], "mb-reserved")


@pytest.mark.asyncio
async def test_sqlite_persists_reservation_across_worker_quota_configs(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"

    async def execute(_ctx: Any) -> dict[str, Any]:
        raise TimeoutError

    initial = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    await _issue(initial, case)
    with pytest.raises(CompatibilityContinuationError) as ambiguous:
        await initial.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert ambiguous.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.applied(
            _success_result(case["source_version"], "mb-cross-worker")
        )

    constrained_store = SqliteCompatibilityContinuationStore(
        database,
        max_bytes=100_000,
        max_payload_bytes=10_000,
    )
    recovered = _coordinator(constrained_store, execute, reconciler=reconcile)

    # The second worker must account for the first worker's durable 1 MiB
    # reservation rather than substituting its own one-byte payload setting.
    other = copy.deepcopy(_cases()[1])
    with pytest.raises(CompatibilityContinuationError) as quota:
        await _issue(
            recovered,
            other,
            principal="principal-other",
            target="seller-session-other",
        )
    assert quota.value.code == CompatibilityContinuationErrorCode.STORE_QUOTA_EXCEEDED

    # Its smaller current setting also cannot invalidate the existing
    # operation's already-reserved terminal result.
    result = await recovered.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == _success_result(case["source_version"], "mb-cross-worker")


def test_sqlite_store_creates_private_ledger_and_rejects_loose_existing_file(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private.sqlite3"
    SqliteCompatibilityContinuationStore(private)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600

    loose = tmp_path / "loose.sqlite3"
    loose.touch(mode=0o644)
    with pytest.raises(PermissionError, match="restrict it to 0o600"):
        SqliteCompatibilityContinuationStore(loose)


def test_concurrent_first_startup_safely_creates_missing_parent(tmp_path: Path) -> None:
    database = tmp_path / "new" / "nested" / "continuations.sqlite3"
    barrier = threading.Barrier(2)

    def construct() -> SqliteCompatibilityContinuationStore:
        barrier.wait()
        return SqliteCompatibilityContinuationStore(database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        stores = list(executor.map(lambda _index: construct(), range(2)))
    assert len(stores) == 2


def test_sqlite_store_creates_private_parent_directory(tmp_path: Path) -> None:
    parent = tmp_path / "private-ledger"
    SqliteCompatibilityContinuationStore(parent / "continuations.sqlite3")
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_sqlite_store_rejects_writable_parent_and_ancestor(tmp_path: Path) -> None:
    writable_parent = tmp_path / "writable-parent"
    writable_parent.mkdir(mode=0o700)
    writable_parent.chmod(0o770)
    try:
        with pytest.raises(PermissionError, match="remove group/world write access"):
            SqliteCompatibilityContinuationStore(writable_parent / "continuations.sqlite3")
    finally:
        writable_parent.chmod(0o700)

    writable_ancestor = tmp_path / "writable-ancestor"
    writable_ancestor.mkdir(mode=0o700)
    secure_parent = writable_ancestor / "secure-parent"
    secure_parent.mkdir(mode=0o700)
    writable_ancestor.chmod(0o777)
    try:
        with pytest.raises(PermissionError, match="unsafe writable mode"):
            SqliteCompatibilityContinuationStore(secure_parent / "continuations.sqlite3")
    finally:
        writable_ancestor.chmod(0o700)


def test_sqlite_store_rechecks_parent_before_every_open(tmp_path: Path) -> None:
    parent = tmp_path / "ledger-parent"
    parent.mkdir(mode=0o700)
    store = SqliteCompatibilityContinuationStore(parent / "continuations.sqlite3")
    parent.chmod(0o770)
    try:
        with pytest.raises(PermissionError, match="remove group/world write access"):
            store._connect()
    finally:
        parent.chmod(0o700)


def test_sqlite_store_rejects_symlink_path_components(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(PermissionError, match="real directory"):
        SqliteCompatibilityContinuationStore(linked_parent / "continuations.sqlite3")


def test_sqlite_store_forces_private_wal_sidecars_under_permissive_umask(
    tmp_path: Path,
) -> None:
    database = tmp_path / "continuations.sqlite3"
    previous_umask = os.umask(0)
    try:
        store = SqliteCompatibilityContinuationStore(database)
        with closing(store._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO adcp_compat_continuations ("
                "token_hash, principal_id, account_identity, source_adcp_version, "
                "expires_at, observed_request_json, observed_response_json, "
                "observed_payload_hash, product_ids_json, losses_json, target_binding, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "hash",
                    "principal",
                    "account",
                    "2.5",
                    "2100-01-01T00:00:00Z",
                    "{}",
                    "{}",
                    "payload-hash",
                    "[]",
                    "[]",
                    "binding",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                assert sidecar.exists()
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        os.umask(previous_umask)


def test_sqlite_store_rejects_loose_existing_sidecar(tmp_path: Path) -> None:
    database = tmp_path / "continuations.sqlite3"
    SqliteCompatibilityContinuationStore(database)
    wal = Path(f"{database}-wal")
    wal.touch()
    wal.chmod(0o644)

    with pytest.raises(PermissionError, match="SQLite sidecar.*restrict it to 0o600"):
        SqliteCompatibilityContinuationStore(database)


def test_sqlite_store_handles_concurrent_sidecar_create_remove_cycles(
    tmp_path: Path,
) -> None:
    store = SqliteCompatibilityContinuationStore(tmp_path / "continuations.sqlite3")

    def repeatedly_connect(_worker: int) -> None:
        for _ in range(20):
            with closing(store._connect()) as conn:
                conn.execute("SELECT 1").fetchone()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(repeatedly_connect, range(8)))


def test_sqlite_store_serializes_concurrent_timestamp_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "old-ledger.sqlite3"
    SqliteCompatibilityContinuationStore(database)
    with sqlite3.connect(database) as conn:
        for table in ("adcp_compat_continuations", "adcp_compat_operations"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN created_at")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN updated_at")

    original = SqliteCompatibilityContinuationStore._ensure_timestamp_columns
    starters_ready = threading.Barrier(2)

    def synchronized_migration(
        self: SqliteCompatibilityContinuationStore, conn: sqlite3.Connection
    ) -> None:
        starters_ready.wait(timeout=5)
        original(self, conn)

    monkeypatch.setattr(
        SqliteCompatibilityContinuationStore,
        "_ensure_timestamp_columns",
        synchronized_migration,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        stores = list(executor.map(SqliteCompatibilityContinuationStore, [database] * 2))

    assert len(stores) == 2
    with sqlite3.connect(database) as conn:
        for table in ("adcp_compat_continuations", "adcp_compat_operations"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert {"created_at", "updated_at"}.issubset(columns)


@pytest.mark.asyncio
async def test_migration_audits_and_rejects_existing_secret_payload(tmp_path: Path) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "continuations.sqlite3"
    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(coordinator, case)
    with closing(sqlite3.connect(database)) as conn, conn:
        raw = conn.execute(
            "SELECT observed_response_json FROM adcp_compat_continuations"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["clientSecret"] = "legacy-secret"
        conn.execute(
            "UPDATE adcp_compat_continuations SET observed_response_json = ?",
            (json.dumps(payload),),
        )
        conn.execute("DELETE FROM adcp_compat_metadata WHERE key = 'persistence_policy_version'")

    with pytest.raises(CompatibilityContinuationError) as exc:
        SqliteCompatibilityContinuationStore(database)
    assert exc.value.code == CompatibilityContinuationErrorCode.PERSISTENCE_POLICY


@pytest.mark.asyncio
async def test_sqlite_migrates_origin_ledger_and_adopts_exact_retry_input(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[0])
    database = tmp_path / "old-ledger.sqlite3"
    store = SqliteCompatibilityContinuationStore(database)

    async def uncertain(_ctx: Any) -> dict[str, Any]:
        raise TimeoutError

    coordinator = _coordinator(store, uncertain)
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as initial:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE adcp_compat_continuations DROP COLUMN projected_products_json")
        conn.execute(
            "ALTER TABLE adcp_compat_continuations DROP COLUMN mutation_idempotency_guaranteed"
        )
        conn.execute("ALTER TABLE adcp_compat_operations DROP COLUMN revision")
        conn.execute("ALTER TABLE adcp_compat_operations DROP COLUMN execution_input_json")

    migrated_store = SqliteCompatibilityContinuationStore(database)
    migrated = _coordinator(migrated_store, uncertain)
    pre_adoption = await migrated.get_legacy_purchase_operation(
        initial.value.details["operation_id"], principal_id="principal-acme"
    )
    with pytest.raises(CompatibilityContinuationError) as missing_snapshot:
        await migrated.recover_legacy_purchase(
            pre_adoption,
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert missing_snapshot.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT
    with pytest.raises(CompatibilityContinuationError) as replay:
        await migrated.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert replay.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION
    operation = await migrated.get_legacy_purchase_operation(
        initial.value.details["operation_id"], principal_id="principal-acme"
    )
    assert operation.execution_input["legacy_create_request"]
    assert operation.revision == 2
    with sqlite3.connect(database) as conn:
        projected = conn.execute(
            "SELECT projected_products_json FROM adcp_compat_continuations"
        ).fetchone()[0]
    assert projected is None


@pytest.mark.asyncio
async def test_migrated_unclaimed_token_cannot_redeem_unbound_hidden_option(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    hidden = copy.deepcopy(case["legacy_response"]["products"][0]["pricing_options"][0])
    hidden["pricing_option_id"] = "seller-only-option"
    case["legacy_response"]["products"][0]["pricing_options"].append(hidden)
    database = tmp_path / "old-unclaimed-ledger.sqlite3"
    calls = 0

    def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    await _issue(coordinator, case)
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE adcp_compat_continuations DROP COLUMN projected_products_json")
    restarted = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    case["continuation_input"]["legacy_create_request"]["packages"][0][
        "pricing_option_id"
    ] = "seller-only-option"
    with pytest.raises(CompatibilityContinuationError) as exc:
        await restarted.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_INPUT
    assert calls == 0


@pytest.mark.asyncio
async def test_migrated_31_ledger_without_bound_replay_guarantee_fails_closed(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "old-31-ledger.sqlite3"
    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(coordinator, case)
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE adcp_compat_continuations SET mutation_idempotency_guaranteed = 0")
    with pytest.raises(CompatibilityContinuationError) as exc:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert exc.value.code == CompatibilityContinuationErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_migrated_31_terminal_result_still_replays_without_mutation(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[2])
    database = tmp_path / "old-31-terminal-ledger.sqlite3"
    calls = 0

    def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, "mb-before-migration")

    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    await _issue(coordinator, case)
    expected = await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE adcp_compat_continuations DROP COLUMN projected_products_json")
        conn.execute(
            "ALTER TABLE adcp_compat_continuations DROP COLUMN mutation_idempotency_guaranteed"
        )
    restarted = _coordinator(SqliteCompatibilityContinuationStore(database), execute)
    replay = await restarted.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert replay == expected
    assert calls == 1


@pytest.mark.asyncio
async def test_sqlite_store_never_persists_raw_bearer_token(tmp_path: Path) -> None:
    case = _cases()[1]
    database = tmp_path / "continuations.sqlite3"
    coordinator = _coordinator(SqliteCompatibilityContinuationStore(database), lambda _ctx: {})
    await _issue(coordinator, case)
    raw_token = case["continuation_input"]["continuation_token"].encode()

    stored_bytes = b"".join(
        path.read_bytes()
        for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
        if path.exists()
    )
    assert raw_token not in stored_bytes


@pytest.mark.asyncio
async def test_sqlite_cleanup_preserves_replay_fence_until_expiry_and_tombstones_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "continuations.sqlite3"
    mutable_now = _NOW
    calls = 0

    def execute(ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _success_for(ctx, f"mb-{calls}")

    store = SqliteCompatibilityContinuationStore(database, clock=lambda: mutable_now)
    coordinator = _coordinator(store, execute)
    case = copy.deepcopy(_cases()[2])
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    mutable_now = _NOW + timedelta(days=2)
    assert await store.purge_resolved_before(mutable_now) == 0

    # An exact issuance retry still resolves to the claimed continuation, so a
    # new purchase idempotency key cannot execute a second buy.
    case["continuation_input"]["idempotency_key"] = "b15ac836-a49e-4e59-bb49-df24dc2cc339"
    await _issue(coordinator, case)
    with pytest.raises(CompatibilityContinuationError) as claimed:
        await coordinator.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )
    assert claimed.value.code == CompatibilityContinuationErrorCode.ALREADY_CLAIMED
    assert calls == 1

    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(mutable_now) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_operations").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM adcp_compat_issuance_tombstones").fetchone()[0] == 1
        )

    with pytest.raises(CompatibilityContinuationError) as retired:
        await _issue(coordinator, case)
    assert retired.value.code == CompatibilityContinuationErrorCode.STORE_CONFLICT
    assert calls == 1


@pytest.mark.asyncio
async def test_sqlite_replay_fence_triggers_fail_closed_for_older_workers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "continuations.sqlite3"
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(database, clock=lambda: mutable_now)
    coordinator = _coordinator(store, lambda ctx: _success_for(ctx, "mb-trigger-fence"))
    case = copy.deepcopy(_cases()[2])
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        continuation = dict(conn.execute("SELECT * FROM adcp_compat_continuations").fetchone())

        # Simulate the cleanup SQL from a process that predates tombstones.
        conn.execute("BEGIN")
        conn.execute("DELETE FROM adcp_compat_operations")
        with pytest.raises(sqlite3.IntegrityError, match="requires issuance tombstone"):
            conn.execute("DELETE FROM adcp_compat_continuations")
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1

    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(mutable_now) == 1

    columns = tuple(continuation)
    placeholders = ", ".join("?" for _ in columns)
    with sqlite3.connect(database) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="issuance identity is retired"):
            conn.execute(
                f"INSERT INTO adcp_compat_continuations ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(continuation[column] for column in columns),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE adcp_compat_issuance_tombstones SET retired_at = retired_at")
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            conn.execute("DELETE FROM adcp_compat_issuance_tombstones")
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM adcp_compat_issuance_tombstones").fetchone()[0] == 1
        )


@pytest.mark.asyncio
async def test_sqlite_replay_fence_migration_is_atomic_against_older_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "continuations.sqlite3"
    store = SqliteCompatibilityContinuationStore(database, clock=lambda: _NOW)
    coordinator = _coordinator(store, lambda ctx: _success_for(ctx, "mb-before-upgrade"))
    case = copy.deepcopy(_cases()[2])
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    # Recreate the on-disk shape visible immediately before this migration.
    with sqlite3.connect(database) as conn:
        trigger_names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND name LIKE 'adcp_compat_%_guard'"
            )
        ]
        for trigger_name in trigger_names:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')
        conn.execute("DROP TABLE adcp_compat_issuance_tombstones")

    migration_has_write_lock = threading.Event()
    allow_migration = threading.Event()
    older_cleanup_started = threading.Event()
    original_connect = SqliteCompatibilityContinuationStore._connect

    def connect_with_migration_pause(
        self: SqliteCompatibilityContinuationStore,
    ) -> sqlite3.Connection:
        conn = original_connect(self)

        def trace(statement: str) -> None:
            if "CREATE TABLE IF NOT EXISTS adcp_compat_issuance_tombstones" in statement:
                migration_has_write_lock.set()
                allow_migration.wait(timeout=10)

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(
        SqliteCompatibilityContinuationStore,
        "_connect",
        connect_with_migration_pause,
    )

    def run_older_cleanup() -> str:
        with sqlite3.connect(database, timeout=10) as conn:
            conn.set_trace_callback(
                lambda statement: (
                    older_cleanup_started.set()
                    if statement.strip().upper() == "BEGIN IMMEDIATE"
                    else None
                )
            )
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM adcp_compat_operations")
                conn.execute("DELETE FROM adcp_compat_continuations")
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                return str(exc)
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        startup = executor.submit(SqliteCompatibilityContinuationStore, database)
        assert migration_has_write_lock.wait(timeout=5)
        cleanup = executor.submit(run_older_cleanup)
        assert older_cleanup_started.wait(timeout=5)
        try:
            assert not cleanup.done()
        finally:
            allow_migration.set()
        startup.result(timeout=10)
        assert "requires issuance tombstone" in cleanup.result(timeout=10)

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_continuations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM adcp_compat_operations").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM adcp_compat_issuance_tombstones").fetchone()[0] == 0
        )


@pytest.mark.asyncio
async def test_sqlite_cleanup_retains_unresolved_operations(tmp_path: Path) -> None:
    database = tmp_path / "continuations.sqlite3"
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(database, clock=lambda: mutable_now)

    completed_case = copy.deepcopy(_cases()[1])
    completed = _coordinator(store, lambda ctx: _success_for(ctx, "mb-complete"))
    await _issue(completed, completed_case)
    await completed.continue_legacy_purchase(
        completed_case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    ambiguous_case = copy.deepcopy(_cases()[2])

    async def uncertain(_ctx: Any) -> dict[str, Any]:
        raise TimeoutError

    ambiguous = _coordinator(store, uncertain)
    await _issue(ambiguous, ambiguous_case)
    with pytest.raises(CompatibilityContinuationError):
        await ambiguous.continue_legacy_purchase(
            ambiguous_case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(_NOW + timedelta(days=1)) == 1
    with sqlite3.connect(database) as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM adcp_compat_operations")]
    assert states == ["ambiguous"]


@pytest.mark.asyncio
async def test_sqlite_cleanup_compares_fractional_timestamps_chronologically(
    tmp_path: Path,
) -> None:
    case = copy.deepcopy(_cases()[1])
    updated_at = _NOW + timedelta(microseconds=500_000)
    mutable_now = updated_at
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3", clock=lambda: mutable_now
    )
    coordinator = _coordinator(store, lambda ctx: _success_for(ctx, "mb-fractional"))
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(_NOW) == 0
    assert await store.purge_resolved_before(_NOW + timedelta(seconds=1)) == 1


@pytest.mark.asyncio
async def test_sqlite_cleanup_does_not_depend_on_sqlite_datetime_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = copy.deepcopy(_cases()[1])
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3", clock=lambda: mutable_now
    )
    coordinator = _coordinator(store, lambda ctx: _success_for(ctx, "mb-portable-cleanup"))
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    original_connect = store._connect

    def connect_without_datetime_functions() -> sqlite3.Connection:
        conn = original_connect()

        def deny_datetime_functions(
            action: int,
            _arg1: str | None,
            arg2: str | None,
            _database: str | None,
            _source: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_FUNCTION and arg2 in {"julianday", "strftime"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_datetime_functions)
        return conn

    monkeypatch.setattr(store, "_connect", connect_without_datetime_functions)

    mutable_now = datetime(2099, 1, 2, tzinfo=timezone.utc)
    assert await store.purge_resolved_before(_NOW + timedelta(seconds=1)) == 1


def test_generated_legacy_purchase_input_rejects_object_loss_arm() -> None:
    payload = copy.deepcopy(_cases()[2]["continuation_input"])
    payload["accepted_losses"] = {}
    with pytest.raises(ValidationError):
        CompatibilityPurchaseCoordinatorInput.model_validate(payload)


@pytest.mark.parametrize(
    "accepted_losses",
    [
        ["feed_version_not_atomic", "feed_version_not_atomic"],
        ["feed_version_not_atomic", "mutation_idempotency_not_guaranteed"],
        ["pricing_version_not_atomic", "mutation_idempotency_not_guaranteed"],
    ],
)
def test_generated_legacy_purchase_input_enforces_loss_array_constraints(
    accepted_losses: list[str],
) -> None:
    payload = copy.deepcopy(_cases()[2]["continuation_input"])
    payload["accepted_losses"] = accepted_losses
    with pytest.raises(ValidationError):
        CompatibilityPurchaseCoordinatorInput.model_validate(payload)


def test_generated_legacy_purchase_input_emits_loss_array_constraints() -> None:
    accepted_losses = CompatibilityPurchaseCoordinatorInput.model_json_schema()["properties"][
        "accepted_losses"
    ]
    assert accepted_losses["uniqueItems"] is True
    assert accepted_losses["allOf"] == [
        {"contains": {"const": "feed_version_not_atomic"}},
        {"contains": {"const": "pricing_version_not_atomic"}},
    ]


def test_exact_legacy_source_bundle_versions_are_exposed() -> None:
    assert get_bundle_adcp_version(version="2.5.3") == "2.5.3"
    assert get_bundle_adcp_version(version="3.0.18") == "3.0.18"
    assert get_bundle_adcp_version(version="3.1.15") == "3.1.15"


def test_vector_source_release_versions_are_exact_patch_versions() -> None:
    assert {case["source_version"] for case in _cases()} == {
        "2.5.3",
        "3.0.18",
        "3.1.15",
    }


def test_expiry_fixture_is_future_relative_to_test_clock() -> None:
    # Keep the fixed test clock explicitly below the signed vector expiry.
    assert _NOW + timedelta(days=1) < datetime(2099, 1, 1, tzinfo=timezone.utc)
