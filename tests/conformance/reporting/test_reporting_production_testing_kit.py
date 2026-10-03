"""Adopters' public kit against actual services and memory/PostgreSQL stores."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting.conformance import ReportingSourceConformanceError
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request
from adcp.reporting.inline_source import (
    InMemorySealStore,
    InMemoryStagingStore,
    ReportingSealStore,
    ReportingStagingStore,
)
from adcp.reporting.ledger import InMemoryReportingLedgerStore, ReportingConfiguration
from adcp.reporting.ledger.store import ReportingLedgerStore, ReportingRowPage
from adcp.reporting.service import (
    ReliableReportingService,
    ReliableReportingShutdownTimeoutError,
    ReliableReportingState,
    ReliableReportingUnavailableError,
    ReportingServiceResource,
)
from adcp.reporting.testing import (
    DeterministicReportingClock,
    FaultInjectingReportingSealStore,
    FaultInjectingReportingStagingStore,
    ReportingFailurePlan,
    ReportingTestBarrier,
    ScriptedReportingAdapter,
    assert_reporting_lifecycle_replay,
    capture_reporting_lifecycle,
    run_reporting_adapter_conformance,
)
from tests.test_reliable_reporting_service import _account_context, _configuration, _rows

from ._generation_support import isolated_reporting_pool

Stores = tuple[ReportingLedgerStore, ReportingStagingStore, ReportingSealStore]


@dataclass
class Backend:
    clock: DeterministicReportingClock
    stores: Callable[[], Stores]

    @property
    def configuration(self) -> ReportingConfiguration:
        boundary = self.clock().replace(minute=0, second=0, microsecond=0)
        return replace(
            _configuration(),
            activated_at=boundary - timedelta(hours=3) + timedelta(minutes=20),
            deactivated_at=boundary - timedelta(hours=1),
        )


@pytest.fixture(params=["memory", "pg-transaction", "pg-autocommit"])
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    if request.param == "memory":
        clock = DeterministicReportingClock(datetime(2026, 11, 1, 3, 10, tzinfo=timezone.utc))
        retained = (
            InMemoryReportingLedgerStore(clock=clock),
            InMemoryStagingStore(),
            InMemorySealStore(),
        )
        yield Backend(clock, lambda: retained)
    else:
        from adcp.reporting.inline_storage import PgReportingSealStore, PgReportingStagingStore
        from adcp.reporting.ledger.pg import PgReportingLedgerStore

        async with isolated_reporting_pool(autocommit=request.param == "pg-autocommit") as pool:
            async with pool.connection() as connection:
                row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
            clock = DeterministicReportingClock(row[0])
            await PgReportingStagingStore(pool=pool).create_schema()
            yield Backend(
                clock,
                lambda: (
                    PgReportingLedgerStore(pool=pool),
                    PgReportingStagingStore(pool=pool),
                    PgReportingSealStore(pool=pool),
                ),
            )
            assert not pool.closed  # service and wrappers borrow the pool


def service_for(
    backend: Backend,
    adapter: ScriptedReportingAdapter,
    failures: ReportingFailurePlan,
    *,
    resources: tuple[ReportingServiceResource, ...] = (),
) -> ReliableReportingService:
    ledger, staging, seals = backend.stores()
    service = ReliableReportingService(
        store=ledger,
        account_context=_account_context,
        clock=backend.clock,
        owned_resources=resources,
    )
    service.sources.register(
        "gam",
        adapter,
        staging=FaultInjectingReportingStagingStore(staging, failures),
        seals=FaultInjectingReportingSealStore(seals, failures),
    )
    return service


def rows() -> list[dict[str, Any]]:
    return [*_rows(7), {**_rows(9)[0], "campaign_id": "campaign-redacted-2"}]


@pytest.mark.parametrize(
    "point",
    [
        "fetch.before",
        "fetch.after",
        "stage.before",
        "stage.after",
        "seal.get.before",
        "seal.get.after",
        "seal.before",
        "seal.after",
        "read.before",
        "read.after",
    ],
)
async def test_public_failure_plan_recovers_original_obligation_after_restart(
    backend: Backend, point: str
) -> None:
    failures = ReportingFailurePlan()
    failures.at(point, OSError("injected boundary failure"))
    first_adapter = ScriptedReportingAdapter(redacted_capabilities(), [rows()], failures=failures)
    service = service_for(backend, first_adapter, failures)
    configuration = backend.configuration
    await service.configure(configuration)
    retry_at = None
    try:
        first = await service.run_worker()
        if point.startswith("fetch."):
            turn = first.configurations[configuration.generation_key]
            assert turn.slices_failed and not turn.revisions_committed
            retry_at = turn.earliest_retry_at
            assert retry_at is not None
        else:
            assert isinstance(first.configuration_errors[configuration.generation_key], OSError)
        assert configuration.deactivated_at is not None
        obligation = await service.store.find_obligation(
            account_id=configuration.account_id,
            consumer_id=configuration.consumer_id,
            delivery_config_id=configuration.delivery_config_id,
            delivery_config_version=configuration.delivery_config_version,
            period_start=configuration.deactivated_at - timedelta(hours=1),
            period_end=configuration.deactivated_at,
        )
        assert obligation is not None
        obligation_id = obligation.reporting_obligation_id
        assert not await service.store.list_revisions(
            account_id=configuration.account_id, reporting_obligation_id=obligation_id
        )
        assert service.ready and service.failure is None
        failures.assert_consumed()
    finally:
        await service.close()
    assert service.state is ReliableReportingState.CLOSED
    # Acknowledgement/read failures after sealing must reuse retained bytes.
    sealed = point in {"seal.after", "read.before", "read.after"}
    restarted_adapter = ScriptedReportingAdapter(
        redacted_capabilities(), [] if sealed else [rows()], failures=failures
    )
    restarted = service_for(backend, restarted_adapter, failures)
    await restarted.configure(configuration)
    try:
        if retry_at is not None:
            backend.clock.set(retry_at)
        recovered = await restarted.run_worker()
        assert not recovered.configuration_errors
        assert len(recovered.configurations[configuration.generation_key].revisions_committed) == 1
        assert len(restarted_adapter.calls) == (0 if sealed else 1)
        snapshot = await capture_reporting_lifecycle(
            restarted.store,
            account_id=configuration.account_id,
            reporting_obligation_id=obligation_id,
            page_size=1,
        )
        assert snapshot.revisions[0].row_count == 2
    finally:
        await restarted.close()
    replay_adapter = ScriptedReportingAdapter(redacted_capabilities())
    replay = service_for(backend, replay_adapter, failures)
    await replay.configure(configuration)
    try:
        await replay.run_worker()
        await assert_reporting_lifecycle_replay(snapshot, replay.store, page_size=1)
        assert not replay_adapter.calls
    finally:
        await replay.close()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_public_barrier_drains_admitted_fetch_before_owned_cleanup(
    backend: Backend, asynchronous: bool
) -> None:
    failures = ReportingFailurePlan()
    barrier = ReportingTestBarrier()
    failures.at("fetch.after", barrier)
    adapter = ScriptedReportingAdapter(
        redacted_capabilities(), [rows()], asynchronous=asynchronous, failures=failures
    )
    closed: list[str] = []
    service = service_for(
        backend,
        adapter,
        failures,
        resources=(ReportingServiceResource(close=lambda: closed.append("owned")),),
    )
    configuration = backend.configuration
    await service.configure(configuration)
    work = asyncio.create_task(service.run_worker())
    try:
        await barrier.wait()
        with pytest.raises(ReliableReportingShutdownTimeoutError):
            await service.close(timeout=0)
        assert service.state is ReliableReportingState.STOPPING
        assert not closed
        with pytest.raises(ReliableReportingUnavailableError):
            await service.configure(replace(configuration, delivery_config_version=2))
        assert not work.done()
    finally:
        barrier.release()
        result = await asyncio.wait_for(work, 10)
        await service.close()
    failures.assert_consumed()
    assert closed == ["owned"]
    assert len(result.configurations[configuration.generation_key].revisions_committed) == 1
    assert service.state is ReliableReportingState.CLOSED
    await service.wait()


async def test_adapter_runner_restarts_over_injected_stores_and_a_past_clock(
    backend: Backend,
) -> None:
    # This request's deadline is in the past relative to wall time, but remains
    # in budget on the supplied clock. Both execution and validation must use it.
    original = redacted_snapshot_request()
    period = original.period.model_copy(
        update={
            "start": datetime(2020, 1, 1, 5, tzinfo=timezone.utc),
            "end": datetime(2020, 1, 2, 5, tzinfo=timezone.utc),
            "source_read_cutoff_at": datetime(2020, 1, 1, 12, tzinfo=timezone.utc),
            "source_local_date": "2020-01-01",
        }
    )
    request = original.model_copy(
        update={
            "period": period,
            "deadline_at": datetime(2020, 1, 1, 13, tzinfo=timezone.utc),
        }
    )
    clock = DeterministicReportingClock(period.source_read_cutoff_at)
    _, staging, seals = backend.stores()
    first = ScriptedReportingAdapter(redacted_capabilities(), [rows()])
    manifest = await run_reporting_adapter_conformance(
        first, request, clock=clock, staging=staging, seals=seals
    )
    _, new_staging, new_seals = backend.stores()
    fresh = ScriptedReportingAdapter(redacted_capabilities())
    assert (
        await run_reporting_adapter_conformance(
            fresh, request, clock=clock, staging=new_staging, seals=new_seals
        )
        == manifest
    )
    assert len(first.calls) == 1
    assert not fresh.calls
    with pytest.raises(ReportingSourceConformanceError, match="deadline"):
        await run_reporting_adapter_conformance(
            fresh, request, clock=lambda: request.deadline_at, staging=new_staging, seals=new_seals
        )


@pytest.mark.parametrize("fault", ["changed-rows", "wrong-revision", "truncated", "cursor-cycle"])
async def test_lifecycle_assertion_rejects_broken_exact_reads(fault: str) -> None:
    class BrokenReadStore(InMemoryReportingLedgerStore):
        broken = False

        async def read_revision_rows(self, **kwargs: Any) -> ReportingRowPage:
            if self.broken and fault == "cursor-cycle":
                kwargs["cursor"] = None
            page = await super().read_revision_rows(**kwargs)
            if not self.broken:
                return page
            if fault == "changed-rows":
                return replace(page, rows=tuple({**row, "impressions": 999} for row in page.rows))
            if fault == "wrong-revision":
                return replace(page, reporting_revision_id="wrong-revision")
            if fault == "truncated":
                return replace(page, rows=(), has_more=False, cursor=None)
            return replace(page, has_more=True, cursor="same-cursor")

    clock = DeterministicReportingClock(datetime(2026, 11, 1, 3, 10, tzinfo=timezone.utc))
    ledger = BrokenReadStore(clock=clock)
    backend = Backend(clock, lambda: (ledger, InMemoryStagingStore(), InMemorySealStore()))
    service = service_for(
        backend, ScriptedReportingAdapter(redacted_capabilities(), [rows()]), ReportingFailurePlan()
    )
    configuration = backend.configuration
    await service.configure(configuration)
    try:
        turn = await service.run_worker()
        (obligation_id,) = turn.configurations[configuration.generation_key].obligations_committed
        snapshot = await capture_reporting_lifecycle(
            ledger,
            account_id=configuration.account_id,
            reporting_obligation_id=obligation_id,
            page_size=1,
        )
        ledger.broken = True
        with pytest.raises(AssertionError):
            await assert_reporting_lifecycle_replay(snapshot, ledger, page_size=1, max_pages=2)
    finally:
        await service.close()


async def test_failure_plan_requires_consumption_and_preserves_cancelled_error() -> None:
    failures = ReportingFailurePlan()
    failures.at("custom.destination.after", None, asyncio.CancelledError())
    with pytest.raises(AssertionError, match="unreached"):
        failures.assert_consumed()
    await failures.hit("custom.destination.after")
    with pytest.raises(asyncio.CancelledError):
        await failures.hit("custom.destination.after")
    failures.assert_consumed()
    assert failures.hits == ("custom.destination.after", "custom.destination.after")
    barrier = ReportingTestBarrier()
    with pytest.raises(RuntimeError, match="event-loop thread"):
        barrier.pause_sync()
