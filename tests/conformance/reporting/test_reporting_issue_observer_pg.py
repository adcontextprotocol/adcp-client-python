"""The issue observer through ``ReliableReportingService`` on PostgreSQL (#1258).

The same flows as the memory reference (``tests/test_reporting_issue_observer.py``)
over ``postgres_storage``: the issue opens on ``sync_reporting_status``, escalates
on the worker turn when the boundary passes, resolves on agreement, and a
failing observer is reported and retried with the same ``event_id``. Two
services on one ledger (two replicas) deliver each event once.

Requires ``ADCP_PG_TEST_URL``; skips otherwise.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from adcp.reporting.service import ReliableReportingService
from adcp.reporting.testing import DeterministicReportingClock
from tests.test_reliable_reporting_service import NOW
from tests.test_reporting_issue_observer import (
    ESCALATION,
    Observed,
    published_period,
    run_failing_observer_flow,
    run_lifecycle_flow,
    service_options,
    unreadable,
)

from ._generation_support import isolated_reporting_pool


async def _service(pool, clock, observed) -> ReliableReportingService:
    from adcp.storage.postgres import postgres_storage

    return ReliableReportingService(
        storage=postgres_storage(pool=pool, clock=clock), **service_options(clock, observed)
    )


async def test_the_lifecycle_reaches_the_observer_once_per_transition_on_postgres() -> None:
    from adcp_reporting_storage.migrations import migrate

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        clock = DeterministicReportingClock(NOW)
        observed = Observed()
        service = await _service(pool, clock, observed)
        try:
            await run_lifecycle_flow(service, clock, observed)
        finally:
            await service.close()


async def test_a_failing_observer_is_retried_with_the_same_event_id_on_postgres() -> None:
    from adcp_reporting_storage.migrations import migrate

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        clock = DeterministicReportingClock(NOW)
        observed = Observed()
        service = await _service(pool, clock, observed)
        try:
            await run_failing_observer_flow(service, clock, observed)
        finally:
            await service.close()


async def test_two_replicas_deliver_each_event_once() -> None:
    from adcp_reporting_storage.migrations import migrate

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        clock = DeterministicReportingClock(NOW)
        observed = Observed()
        first = await _service(pool, clock, observed)
        second = await _service(pool, clock, observed)
        try:
            period, revision = await published_period(first)
            clock.advance(timedelta(minutes=1))
            await first.sync_reporting_status({"statuses": [unreadable(period, revision, clock())]})
            clock.advance(ESCALATION.consumer_mismatch_escalation)
            await asyncio.gather(first.run_worker(), second.run_worker())
            await asyncio.gather(first.run_worker(), second.run_worker())
            assert observed.kinds() == ["opened", "escalated"]
            assert observed.errors == []
        finally:
            await first.close()
            await second.close()


async def test_the_service_refuses_to_start_before_migration_0008() -> None:
    import pytest
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    from adcp.reporting.service import ReliableReportingConfigurationError

    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = DeterministicReportingClock(NOW)
        observed = Observed()
        options = service_options(clock, observed)
        service = ReliableReportingService(
            store=PgReportingLedgerStore(pool=pool, clock=clock), **options
        )
        with pytest.raises(ReliableReportingConfigurationError, match="python -m adcp migrate"):
            await service.initialize()
