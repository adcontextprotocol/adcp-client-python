"""The production worker schedules retention: sweeps, period retirement and change pruning."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from adcp.reporting.production.service import ReportingProductionSupport


class Store:
    def __init__(self, *, rows: bool, feed: bool, fail: str | None = None) -> None:
        self._row_storage = object() if rows else None
        self._change_feed = feed
        self.calls: list[str] = []
        self.fail = fail
        self.report: Any = None

    async def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError("secret bucket gs://x token=SECRET")

    async def sweep_row_write_intents(self, **_: Any) -> Any:
        await self._record("sweep")
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
    return instance


async def test_maintenance_runs_only_what_the_store_opted_into() -> None:
    cases = [
        (dict(rows=True, feed=True), ["sweep", "retire", "prune"]),
        (dict(rows=True, feed=False), ["sweep", "retire"]),
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
    assert store.calls == ["sweep", "retire", "prune"]
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
    assert store.calls == ["sweep", "retire", "prune"]
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
    assert [r.operation for r in records] == ["intent_sweep", "period_retirement"]
    assert all("secret" not in r.getMessage() and r.exc_info is None for r in records)
    store.report = SimpleNamespace(failed=())
    caplog.clear()
    worker = service(store)
    await worker._maintain()
    assert not [
        r for r in caplog.records if getattr(r, "code", "") == "REPORTING_MAINTENANCE_FAILED"
    ]
