"""The production worker schedules retention: sweeps, period retirement and change pruning."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from adcp.reporting.production.service import ReportingProductionSupport


class Store:
    """A store listing the retention passes it opted into, as the shipped stores do."""

    def __init__(self, *, rows: bool, feed: bool, fail: str | None = None) -> None:
        self.rows, self.feed = rows, feed
        self.calls: list[str] = []
        self.fail = fail
        self.report: Any = None

    def maintenance_operations(self) -> tuple[tuple[str, Any], ...]:
        operations: list[tuple[str, Any]] = []
        if self.rows:
            operations.append(("intent_sweep", lambda: self.sweep_row_write_intents(limit=100)))
            operations.append(("row_pruning", lambda: self.prune_expired_row_sets(limit=20)))
        if self.rows or self.feed:
            operations.append(("period_retirement", lambda: self.retire_expired_periods(limit=20)))
        if self.feed:
            operations.append(("change_pruning", lambda: self.prune_changes()))
        return tuple(operations)

    async def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("secret bucket gs://x token=SECRET")

    async def sweep_row_write_intents(self, **_: Any) -> Any:
        await self._record("sweep")
        return self.report

    async def prune_expired_row_sets(self, **_: Any) -> Any:
        await self._record("prune_rows")
        return self.report

    async def retire_expired_periods(self, **_: Any) -> Any:
        await self._record("retire")
        return self.report

    async def prune_changes(self, **_: Any) -> None:
        await self._record("prune")


def service(store: Any, seconds: float | None = 300.0) -> ReportingProductionSupport:
    instance = object.__new__(ReportingProductionSupport)
    instance.store = store
    instance.maintenance_seconds = seconds
    instance._next_maintenance = 0.0
    instance.maintenance_report = {}
    return instance


def test_postgres_store_lists_the_passes_for_its_features() -> None:
    """The selection lives in the store; the worker runs whatever it lists."""
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    cases = [
        (
            dict(row_storage=True, change_feed=True),
            ["intent_sweep", "row_pruning", "period_retirement", "change_pruning"],
        ),
        (
            dict(row_storage=True, change_feed=False),
            ["intent_sweep", "row_pruning", "period_retirement"],
        ),
        (dict(row_storage=False, change_feed=True), ["period_retirement", "change_pruning"]),
        (dict(row_storage=False, change_feed=False), []),
    ]
    for options, expected in cases:
        store = PgReportingLedgerStore(pool=object(), **options)  # type: ignore[arg-type]
        assert [name for name, _ in store.maintenance_operations()] == expected, options


async def test_maintenance_runs_only_what_the_store_opted_into() -> None:
    cases = [
        (dict(rows=True, feed=True), ["sweep", "prune_rows", "retire", "prune"]),
        (dict(rows=True, feed=False), ["sweep", "prune_rows", "retire"]),
        (dict(rows=False, feed=True), ["retire", "prune"]),
        (dict(rows=False, feed=False), []),
    ]
    for options, expected in cases:
        store = Store(**options)
        await service(store)._maintain()
        assert store.calls == expected, options


async def test_maintenance_waits_for_its_interval_and_can_be_disabled() -> None:
    store = Store(rows=True, feed=True)
    worker = service(store, seconds=3600)
    await worker._maintain()
    await worker._maintain()
    assert store.calls == ["sweep", "prune_rows", "retire", "prune"]
    disabled = Store(rows=True, feed=True)
    await service(disabled, seconds=None)._maintain()
    assert disabled.calls == []


async def test_a_failing_operation_never_stops_the_worker_or_leaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store(rows=True, feed=True, fail="sweep")
    with caplog.at_level(logging.WARNING, logger="adcp.reporting.production"):
        await service(store)._maintain()
    # The other operations still ran, and only the closed operation name is recorded.
    assert store.calls == ["sweep", "prune_rows", "retire", "prune"]
    (record,) = [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_FAILED"
    ]
    assert record.operation == "intent_sweep"
    assert "SECRET" not in record.getMessage() and record.exc_info is None


async def test_per_item_failures_in_a_result_are_logged_by_operation_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    store = Store(rows=True, feed=False)
    store.report = SimpleNamespace(failed=("rpo_secret_account",))
    with caplog.at_level(logging.WARNING, logger="adcp.reporting.production"):
        await service(store)._maintain()
    records = [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_FAILED"
    ]
    assert [r.operation for r in records] == [
        "intent_sweep",
        "row_pruning",
        "period_retirement",
    ]
    assert all("secret" not in r.getMessage() and r.exc_info is None for r in records)
    store.report = SimpleNamespace(failed=())
    caplog.clear()
    worker = service(store)
    await worker._maintain()
    assert not [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_FAILED"
    ]


async def test_protected_held_parked_and_skipped_work_is_logged_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    store = Store(rows=True, feed=True)
    store.report = SimpleNamespace(
        retired=2,
        failed=(),
        protected=("rpo_a", "rpo_b"),
        held=3,
        deferred=1,
        skipped_unconfigured=4,
        stuck=(("rpo_stuck", 6, "STATE_UNAVAILABLE"),),
        overdue=0,
    )
    worker = service(store)
    with caplog.at_level(logging.INFO, logger="adcp.reporting.production"):
        await worker._maintain()
    pending = [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_PENDING"
    ]
    assert {r.operation for r in pending} == {
        "intent_sweep",
        "row_pruning",
        "period_retirement",
    }
    one = pending[0]
    # Integer counters only: no obligation or account identifiers reach the log.
    assert (one.protected, one.held, one.deferred) == (2, 3, 1)
    assert one.skipped_unconfigured == 4
    assert all("rpo_" not in r.getMessage() for r in pending)
    # The same counters are available to health checks and dashboards.
    assert worker.maintenance_report["period_retirement"]["protected"] == 2
    assert worker.maintenance_report["period_retirement"]["stuck"] == 1
    assert worker.maintenance_report["intent_sweep"]["skipped_unconfigured"] == 4
    assert not [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_FAILED"
    ]


async def test_work_nobody_is_handling_is_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    from types import SimpleNamespace

    store = Store(rows=True, feed=False)
    store.report = SimpleNamespace(failed=(), overdue=7, stuck=(("rpo_a", 5, "X"),))
    with caplog.at_level(logging.INFO, logger="adcp.reporting.production"):
        await service(store)._maintain()
    stalled = [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_STALLED"
    ]
    assert stalled and all(r.levelno == logging.WARNING and r.overdue == 7 for r in stalled)
    assert all(r.stuck == 1 for r in stalled)


async def test_a_quiet_pass_logs_nothing_and_still_records_its_counters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    store = Store(rows=True, feed=True)
    store.report = SimpleNamespace(retired=1, failed=(), protected=(), held=0)
    worker = service(store)
    with caplog.at_level(logging.INFO, logger="adcp.reporting.production"):
        await worker._maintain()
    assert not [r for r in caplog.records if getattr(r, "code", "").startswith("REPORTING_MAINT")]
    assert worker.maintenance_report["period_retirement"] == {
        "retired": 1,
        "failed": 0,
        "protected": 0,
        "held": 0,
    }
