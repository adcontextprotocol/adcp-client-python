"""AdCP 3.2 legacy purchase continuation conformance tests."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
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
    ReconciliationResult,
    SqliteCompatibilityContinuationStore,
)
from adcp.types import CompatibilityPurchaseCoordinatorInput
from adcp.validation import get_bundle_adcp_version, validate_response

_VECTORS = (
    Path(__file__).parent
    / "conformance"
    / "vectors"
    / "products-only-brief-compatibility"
    / "vectors.json"
)
_NOW = datetime(2098, 1, 1, tzinfo=timezone.utc)


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
        allow_non_durable_store=not store.is_durable,
        clock=lambda: _NOW,
    )


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
        account=case["continuation_input"]["account"],
        source_adcp_version=case["source_version"],
        expires_at=datetime.fromisoformat(
            continuation["continuation_expires_at"].replace("Z", "+00:00")
        ),
        observed_request=case["legacy_request"],
        observed_response=case["legacy_response"],
        product_ids=continuation["product_ids"],
        losses=continuation["losses"],
        target_binding=target,
    )
    case["continuation_input"]["continuation_token"] = token


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["source_version"])
async def test_upstream_vectors_execute_and_replay(case: dict[str, Any]) -> None:
    calls: list[Any] = []

    async def execute(ctx: Any) -> dict[str, Any]:
        calls.append(ctx)
        return {"media_buy_id": f"mb-{ctx.selected_product_ids[0]}"}

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

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"media_buy_id": "mb-once"}

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

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"media_buy_id": "mb-once"}

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
    assert await first == {"media_buy_id": "mb-once"}
    assert exc.value.code == CompatibilityContinuationErrorCode.AMBIGUOUS_MUTATION
    assert calls == 1


@pytest.mark.asyncio
async def test_different_idempotency_keys_cannot_double_claim() -> None:
    case = _cases()[1]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"media_buy_id": "mb-once"}

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

    async def execute(_ctx: Any) -> dict[str, Any]:
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
        return ReconciliationResult.applied({"media_buy_id": "mb-reconciled"})

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
    assert result == replay == {"media_buy_id": "mb-reconciled"}
    assert calls == 1


@pytest.mark.asyncio
async def test_authoritatively_not_applied_resumes_ambiguous_operation() -> None:
    case = _cases()[2]
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError
        return {"media_buy_id": "mb-resumed"}

    store = InMemoryCompatibilityContinuationStore()
    initial = _coordinator(store, execute)
    await _issue(initial, case)
    with pytest.raises(CompatibilityContinuationError):
        await initial.continue_legacy_purchase(
            case["continuation_input"],
            principal_id="principal-acme",
            target_binding="seller-session-acme",
        )

    async def reconcile(_ctx: Any, _operation: Any) -> ReconciliationResult:
        return ReconciliationResult.not_applied()

    recovered = _coordinator(store, execute, reconciler=reconcile)
    result = await recovered.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )
    assert result == {"media_buy_id": "mb-resumed"}
    assert calls == 2


@pytest.mark.asyncio
async def test_sqlite_store_survives_restart_and_replays(tmp_path: Path) -> None:
    case = _cases()[1]
    database = tmp_path / "continuations.sqlite3"
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"media_buy_id": "mb-durable"}

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
    assert result == replay == {"media_buy_id": "mb-durable"}
    assert calls == 1


@pytest.mark.asyncio
async def test_sqlite_stores_share_one_atomic_claim(tmp_path: Path) -> None:
    case = _cases()[2]
    database = tmp_path / "continuations.sqlite3"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"media_buy_id": "mb-sqlite-once"}

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
    assert await first_task == {"media_buy_id": "mb-sqlite-once"}
    assert exc.value.code == CompatibilityContinuationErrorCode.ALREADY_CLAIMED
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_leaves_operation_ambiguous() -> None:
    case = _cases()[1]
    entered = asyncio.Event()
    calls = 0

    async def execute(_ctx: Any) -> dict[str, Any]:
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

    async def execute(_ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"media_buy_id": "mb-before-expiry"}

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
    assert first == replay == {"media_buy_id": "mb-before-expiry"}
    assert calls == 1


def test_production_default_rejects_in_memory_store() -> None:
    with pytest.raises(ValueError, match="durable store"):
        LegacyPurchaseCoordinator(
            store=InMemoryCompatibilityContinuationStore(),
            executor=lambda _ctx: {},
        )


def test_sqlite_store_rejects_memory_database() -> None:
    with pytest.raises(ValueError, match="file-backed"):
        SqliteCompatibilityContinuationStore(":memory:")


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
async def test_sqlite_cleanup_retains_unresolved_operations(tmp_path: Path) -> None:
    database = tmp_path / "continuations.sqlite3"
    mutable_now = _NOW
    store = SqliteCompatibilityContinuationStore(database, clock=lambda: mutable_now)

    completed_case = copy.deepcopy(_cases()[1])
    completed = _coordinator(store, lambda _ctx: {"media_buy_id": "mb-complete"})
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

    mutable_now = _NOW + timedelta(days=2)
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
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3", clock=lambda: updated_at
    )
    coordinator = _coordinator(store, lambda _ctx: {"media_buy_id": "mb-fractional"})
    await _issue(coordinator, case)
    await coordinator.continue_legacy_purchase(
        case["continuation_input"],
        principal_id="principal-acme",
        target_binding="seller-session-acme",
    )

    assert await store.purge_resolved_before(_NOW) == 0
    assert await store.purge_resolved_before(_NOW + timedelta(seconds=1)) == 1


@pytest.mark.asyncio
async def test_sqlite_cleanup_does_not_depend_on_sqlite_datetime_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = copy.deepcopy(_cases()[1])
    store = SqliteCompatibilityContinuationStore(
        tmp_path / "continuations.sqlite3", clock=lambda: _NOW
    )
    coordinator = _coordinator(store, lambda _ctx: {"media_buy_id": "mb-portable-cleanup"})
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
