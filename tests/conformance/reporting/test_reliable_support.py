"""The downstream harness controls reporting time, interleaving and retry bounds."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from adcp.reporting.conformance import (
    ReportingSourceConformanceError,
    run_reporting_source_replay_conformance,
    validate_reporting_source_execution,
)
from adcp.reporting.fixtures import SNAPSHOT_OFFERING_ID
from adcp.reporting.ledger import InMemoryReportingLedgerStore

from ._generation_support import configuration as ledger_configuration
from ._generation_support import obligation_for, revision_for
from ._reliable_support import (
    ManualClock,
    ReliableHarness,
    ScriptedSource,
    complete_fetch,
    configuration,
    drain_until_idle,
)


async def test_core_change_appends_and_snapshot_use_the_injected_clock(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    config = ledger_configuration()
    await h.store.put_configuration(config)
    obligation = obligation_for(config)
    first_at = h.clock()
    await h.store.commit_obligation(obligation)
    second_at = h.clock.advance(timedelta(minutes=7))
    revision, rows = revision_for(obligation)
    await h.store.commit_revision(revision, rows)
    if isinstance(h.store, InMemoryReportingLedgerStore):
        # _append previously ignored its clock even though snapshots used it.
        timestamps = [entry[4] for entry in h.store._changes]
    else:
        assert h.blobs.pool is not None
        async with h.blobs.pool.connection() as connection:
            retained = await (
                await connection.execute(
                    "SELECT committed_at FROM reporting_ledger_changes"
                    " WHERE account_id=%s ORDER BY seq",
                    (config.account_id,),
                )
            ).fetchall()
        timestamps = [entry[0] for entry in retained]
    assert timestamps == [first_at, second_at]
    snapshot = await h.store.open_snapshot(
        account_id=config.account_id, filters_fingerprint="fixture"
    )
    assert snapshot.ledger_as_of == second_at
    await h.store.commit_revision(revision, rows)
    replay = await h.store.open_snapshot(
        account_id=config.account_id, filters_fingerprint="fixture"
    )
    assert replay == snapshot


async def test_drain_has_a_finite_turn_budget_for_a_source_that_stays_unready(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    script = ScriptedSource(eur=[None, None, None])
    await h.store.put_configuration(configuration("eur"))
    started = h.clock()
    with pytest.raises(AssertionError, match="within 3 turns"):
        await drain_until_idle(h.producer(h.source(script.async_fetch)), h.clock, max_turns=3)
    assert len(script.requests) == 3
    assert h.clock() == started + timedelta(milliseconds=3)
    assert len({request.identity.source_execution_key for request in script.requests}) == 1
    assert {request.currency for request in script.requests} == {"EUR"}


async def test_source_conformance_uses_manual_deadlines_for_execution_and_object_reads(
    reliable: ReliableHarness,
) -> None:
    h = reliable
    script = ScriptedSource(eur=[complete_fetch])
    source = h.source(script.sync)
    producer = h.producer(source)
    config = configuration("eur")
    await h.store.put_configuration(config)
    (obligation,) = await producer.close_elapsed_periods(config)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=h.clock())
    manifest = await run_reporting_source_replay_conformance(
        executor=source, request=request, object_reader=h.staging, clock=h.clock
    )
    assert manifest.observed_at == h.clock()
    assert len(script.requests) == 1
    result = await source.execute(request, cancel=asyncio.Event())
    h.clock.advance(request.deadline_at - h.clock())
    with pytest.raises(ReportingSourceConformanceError, match="exceeded its deadline"):
        await validate_reporting_source_execution(
            capabilities=source.capabilities,
            request=request,
            result=result,
            object_reader=h.staging,
            clock=h.clock,
        )
    with pytest.raises(ReportingSourceConformanceError, match="exceeded its deadline"):
        await run_reporting_source_replay_conformance(
            executor=source, request=request, object_reader=h.staging, clock=h.clock
        )
    assert len(script.requests) == 1


@pytest.mark.parametrize("component", ["destination", "receiver"])
async def test_deterministic_stores_replay_after_commit_failure_and_keep_accounts_separate(
    reliable: ReliableHarness, component: str
) -> None:
    h = reliable
    store = getattr(h, component)
    h.failures.at(f"{component}.after", OSError("lost acknowledgement"))
    with pytest.raises(OSError, match="lost acknowledgement"):
        await store.write("eur", "colliding-revision", b'{"currency":"EUR"}\n')
    digest = await store.write("eur", "colliding-revision", b'{"currency":"EUR"}\n')
    assert len(digest) == 64
    with pytest.raises(ValueError, match="different bytes"):
        await store.write("eur", "colliding-revision", b'{"currency":"USD"}\n')
    await store.write("usd", "colliding-revision", b'{"currency":"USD"}\n')
    await h.restart()
    store = getattr(h, component)
    assert await store.read("eur", "colliding-revision") == b'{"currency":"EUR"}\n'
    assert await store.read("usd", "colliding-revision") == b'{"currency":"USD"}\n'
    assert await store.read("outsider", "colliding-revision") is None
    replayed_digest = await store.write("eur", "colliding-revision", b'{"currency":"EUR"}\n')
    assert replayed_digest == digest


def test_manual_clock_requires_aware_monotonic_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        ManualClock(datetime(2026, 9, 1))
    clock = ManualClock()
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
