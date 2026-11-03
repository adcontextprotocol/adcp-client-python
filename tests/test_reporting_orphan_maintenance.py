"""Scheduling the orphan collector: options, the store option and the cycle state.

Ported from the JavaScript SDK (adcp-client #3291 and #3593, ``reporting-maintenance-warnings`` and
``reliable-reporting-service``). No database: the collector itself is replaced.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from adcp_reporting_storage.ledger import orphan_collector
from adcp_reporting_storage.ledger.orphan_collector import (
    ReportingOrphanBindingResultV1,
    ReportingOrphanCollectionOptionsV1,
    ReportingOrphanCollectionResultV1,
    ReportingOrphanCollectionStateV1,
    run_orphan_collection_pass,
)
from adcp_reporting_storage.ledger.orphan_fence import ReportingOrphanFenceRefusalV1


def options(**overrides: Any) -> ReportingOrphanCollectionOptionsV1:
    return ReportingOrphanCollectionOptionsV1(
        **{"acknowledge_exclusive_namespace": True, **overrides}
    )


def test_deleting_needs_the_acknowledgement_and_every_bound_is_checked() -> None:
    with pytest.raises(ValueError, match="acknowledge_exclusive_namespace"):
        ReportingOrphanCollectionOptionsV1()
    assert ReportingOrphanCollectionOptionsV1(dry_run=True).dry_run is True
    assert options().interval_seconds == 86_400 and options().min_age_seconds == 7 * 86_400
    assert options(min_age_seconds=86_400).min_age_seconds == 86_400
    for bad in (
        {"min_age_seconds": 86_399},
        {"min_age_seconds": 0},
        {"min_age_seconds": 1.5},
        {"limit": 0},
        {"limit": -1},
        {"limit": 1.5},
        {"interval_seconds": 0},
        {"interval_seconds": True},
        {"dry_run": "yes"},
        {"include_read_only": 1},
        {"acknowledge_exclusive_namespace": "true"},
    ):
        with pytest.raises(ValueError):
            options(**bad)


def test_the_store_option_needs_row_storage_and_adds_the_maintenance_operation() -> None:
    pytest.importorskip("psycopg_pool")
    pool = object()
    with pytest.raises(ValueError, match="requires row_storage"):
        PgReportingLedgerStore(pool=pool, orphan_collection=options())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PgReportingLedgerStore(
            pool=pool,  # type: ignore[arg-type]
            row_storage=True,
            orphan_collection=True,  # type: ignore[arg-type]
        )
    plain = PgReportingLedgerStore(pool=pool, row_storage=True)  # type: ignore[arg-type]
    assert "orphan_collection" not in [name for name, _ in plain.maintenance_operations()]
    scheduled = PgReportingLedgerStore(
        pool=pool,  # type: ignore[arg-type]
        row_storage=True,
        orphan_collection=options(),
    )
    names = [name for name, _ in scheduled.maintenance_operations()]
    assert names[-1] == "orphan_collection" and "intent_sweep" in names


def result(**changes: Any) -> ReportingOrphanCollectionResultV1:
    return replace(ReportingOrphanCollectionResultV1(dry_run=False), **changes)


def binding(binding_id: str, status: Any = "complete", **changes: Any) -> Any:
    return ReportingOrphanBindingResultV1(binding_id, status, **changes)


class Recorder:
    def __init__(self, *outcomes: ReportingOrphanCollectionResultV1) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, store: Any, **kwargs: Any) -> ReportingOrphanCollectionResultV1:
        self.calls.append(kwargs)
        return self.outcomes.pop(0)


async def test_a_pass_forwards_its_options_replaces_the_saved_state_and_rests_after_a_clean_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(
        result(
            cursors={"objects": "page-2"},
            completed=("legacy",),
            bindings=(binding("objects", "partial"),),
        ),
        result(cursors={}, completed=("objects",), bindings=(binding("objects"),)),
    )
    monkeypatch.setattr(orphan_collector, "collect_orphaned_row_objects", recorder)
    configured = options(
        dry_run=False,
        limit=50,
        min_age_seconds=3 * 86_400,
        include_read_only=True,
        interval_seconds=600,
    )
    state = ReportingOrphanCollectionStateV1(cursors={"stale": "x"}, completed=["done"])
    await run_orphan_collection_pass(object(), configured, state)  # type: ignore[arg-type]
    assert recorder.calls[0] == {
        "dry_run": False,
        "acknowledge_exclusive_namespace": True,
        "min_age_seconds": 3 * 86_400,
        "limit": 50,
        "include_read_only": True,
        "cursors": {"stale": "x"},
        "skip_bindings": ["done"],
    }
    # An unfinished listing carries on next pass, remembering what already finished.
    assert state.cursors == {"objects": "page-2"}
    assert state.completed == ["done", "legacy"] and state.next_cycle_at is None
    await run_orphan_collection_pass(object(), configured, state)  # type: ignore[arg-type]
    assert recorder.calls[1]["cursors"] == {"objects": "page-2"}
    assert state.cursors == {} and state.completed == []
    assert state.next_cycle_at is not None
    assert 590 < state.next_cycle_at - time.monotonic() <= 600, "a clean cycle rests the interval"
    # Between full listings the pass does nothing, and does not call the collector.
    idle = await run_orphan_collection_pass(object(), configured, state)  # type: ignore[arg-type]
    assert idle.listed == 0 and idle.dry_run is False and len(recorder.calls) == 2


async def test_a_binding_that_finished_is_not_listed_again_while_another_needs_more_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(
        result(
            cursors={"big": "t1"},
            completed=("small",),
            bindings=(binding("small"), binding("big", "partial")),
        ),
        result(cursors={"big": "t2"}, completed=(), bindings=(binding("big", "partial"),)),
        result(cursors={}, completed=("big",), bindings=(binding("big"),)),
    )
    monkeypatch.setattr(orphan_collector, "collect_orphaned_row_objects", recorder)
    state = ReportingOrphanCollectionStateV1()
    for _ in range(3):
        await run_orphan_collection_pass(object(), options(), state)  # type: ignore[arg-type]
    assert [call["skip_bindings"] for call in recorder.calls] == [[], ["small"], ["small"]]
    assert state.completed == [] and state.cursors == {} and state.next_cycle_at is not None


async def test_a_failed_binding_keeps_the_task_retrying_every_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = result(
        cursors={}, failed=1, bindings=(binding("objects", "failed", error="PROVIDER_UNAVAILABLE"),)
    )
    recorder = Recorder(failing, failing)
    monkeypatch.setattr(orphan_collector, "collect_orphaned_row_objects", recorder)
    state = ReportingOrphanCollectionStateV1()
    for _ in range(2):
        await run_orphan_collection_pass(object(), options(), state)  # type: ignore[arg-type]
        assert state.next_cycle_at is None, "a failure does not rest the interval"
    assert len(recorder.calls) == 2


async def test_a_fenced_binding_rests_at_most_an_hour_whatever_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = ReportingOrphanFenceRefusalV1("fence_advanced", "take over", 5, 3)
    fenced = result(fenced=("objects",), bindings=(binding("objects", "fenced", fence=refusal),))
    recorder = Recorder(fenced, fenced)
    monkeypatch.setattr(orphan_collector, "collect_orphaned_row_objects", recorder)
    state = ReportingOrphanCollectionStateV1()
    await run_orphan_collection_pass(object(), options(interval_seconds=86_400), state)  # type: ignore[arg-type]
    assert state.next_cycle_at is not None
    assert 3_590 < state.next_cycle_at - time.monotonic() <= 3_600
    # A short interval is kept as it is.
    short = ReportingOrphanCollectionStateV1()
    await run_orphan_collection_pass(object(), options(interval_seconds=120), short)  # type: ignore[arg-type]
    assert short.next_cycle_at is not None
    assert 110 < short.next_cycle_at - time.monotonic() <= 120


async def test_a_rejected_collector_propagates_so_the_worker_counts_it_as_a_failed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(store: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(orphan_collector, "collect_orphaned_row_objects", explode)
    state = ReportingOrphanCollectionStateV1(cursors={"a": "b"})
    with pytest.raises(RuntimeError):
        await run_orphan_collection_pass(object(), options(), state)  # type: ignore[arg-type]
    assert state.cursors == {"a": "b"} and state.next_cycle_at is None
