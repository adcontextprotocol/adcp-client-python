"""Connection ownership, atomic fanout and source/lifecycle failure boundaries."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import (
    StatusProjectionInput,
    mismatch_key,
    project_status_scope,
)
from adcp.reporting.outbox import (
    PgStatusNotificationStore,
    ReportingStatusScope,
    ReportingStatusSweeper,
)

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from ._reliable_support import NotificationHarness, SimulatedCrash
from .test_reporting_notification_outbox import statement

status_harness = _contract.status_harness


async def test_pool_of_one_uses_one_backend_and_no_nested_acquisition(monkeypatch):
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    queries = []

    class InstrumentedConnection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            if isinstance(query, str):
                queries.append((self.info.backend_pid, query))
            return await super().execute(query, params, **kwargs)

    async with isolated_reporting_pool(autocommit=True) as owner:
        async with AsyncConnectionPool(
            owner.conninfo,
            kwargs=owner.kwargs,
            min_size=1,
            max_size=1,
            connection_class=InstrumentedConnection,
            open=False,
        ) as pool:
            ledger = PgReportingLedgerStore(pool=pool, notifications=True)  # Database time.
            status = PgStatusNotificationStore(ledger)
            await status.create_schema()
            acquired = set()
            get, put = pool.getconn, pool.putconn

            async def getconn(*args, **kwargs):
                task = asyncio.current_task()
                assert task not in acquired, "nested connection acquisition while account locked"
                acquired.add(task)
                return await get(*args, **kwargs)

            async def putconn(connection):
                acquired.remove(asyncio.current_task())
                return await put(connection)

            monkeypatch.setattr(pool, "getconn", getconn)
            monkeypatch.setattr(pool, "putconn", putconn)
            config = configuration()
            await asyncio.wait_for(ledger.put_configuration(config), 5)
            obligation = await ledger.commit_obligation(obligation_for(config))
            revision, rows = revision_for(obligation)
            await ledger.commit_revision(revision, rows)
            await status.baseline(account_id="acct_a")
            queries.clear()
            await ledger.record_consumer_status(
                replace(statement(obligation), consumer_status="unreadable")
            )
            snapshot = await ledger.read_status_snapshot(account_id="acct_a")
            await asyncio.wait_for(status.project_one(account_id="acct_a"), 5)
            handler = ReportingStatusHandler(ledger, consumer_status_enabled=True)
            response = handler.render_snapshot(
                {}, caller=ReportingStatusCaller("acct_a", "buyer"), snapshot=snapshot
            )
            scope = ReportingStatusScope(
                "acct_a", config.generation_key, consumer_id="buyer", feed_purpose="analytics"
            )
            projected = project_status_scope(StatusProjectionInput(snapshot, scope))
            checkpoint = next(
                c for c in await status.checkpoints(account_id="acct_a") if c.scope == scope
            )
            assert response["health"] == projected.health == checkpoint.snapshot["health"]
            assert (
                response["issues"]
                == checkpoint.snapshot["issues"]
                == projected.canonical()["issues"]
            )
            assert len({pid for pid, _ in queries}) == 1
            sql = [query for _, query in queries]
            lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
            cursor = next(
                i
                for i, q in enumerate(sql)
                if "reporting_status_accounts" in q and "FOR UPDATE" in q
            )
            scopes = next(
                i
                for i, q in enumerate(sql)
                if "reporting_status_scope_checkpoints" in q and "FOR UPDATE" in q
            )
            evidence = next(i for i, q in enumerate(sql) if "FROM reporting_consumer_statuses" in q)
            assert lock < cursor < scopes < evidence
            assert any("clock_timestamp()" in q for q in sql)
            assert not acquired


async def test_consumer_status_pre_lifecycle_crash_rolls_back_both_and_self_dirty_is_suppressed(
    status_harness, monkeypatch
):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    outbox = NotificationHarness(h.reliable).outbox
    before = await outbox.read_status_dirty(account_id="acct_a")
    import adcp.reporting.ledger.status_snapshot as snapshots

    def crash_memory(*args, **kwargs):
        raise SimulatedCrash("consumer_status_pre_lifecycle")

    async def crash_pg(*args, **kwargs):
        raise SimulatedCrash("consumer_status_pre_lifecycle")

    record = replace(
        statement(obligation),
        consumer_status="unreadable",
        reporting_revision_id=revision.reporting_revision_id,
    )
    with monkeypatch.context() as patch:
        patch.setattr(snapshots, "apply_memory_intents", crash_memory)
        patch.setattr(snapshots, "apply_intents_on", crash_pg)
        with pytest.raises(SimulatedCrash):
            await h.ledger.record_consumer_status_with_lifecycle(record)
    assert not await h.ledger.list_consumer_statuses(account_id="acct_a", consumer_id="buyer")
    assert await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(record)) is None
    assert await outbox.read_status_dirty(account_id="acct_a") == before
    await h.ledger.record_consumer_status_with_lifecycle(record)
    after = await outbox.read_status_dirty(account_id="acct_a")
    assert len(after) == len(before) + 1  # Lifecycle application creates no self-dirty row.
    await h.drain()
    assert await outbox.read_status_dirty(account_id="acct_a") == after
    assert not (await h.status.project_one(account_id="acct_a")).did_work


async def test_fanout_failure_cannot_advance_half_a_source_transaction(status_harness, monkeypatch):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    for consumer in ("buyer", "auditor"):
        await h.ledger.record_consumer_status(
            replace(
                statement(obligation, consumer),
                consumer_status="received",
                reporting_revision_id=revision.reporting_revision_id,
                observed_revision_content_sha256=revision.revision_content_sha256,
            )
        )
    await h.status.baseline(account_id="acct_a")
    before = await h.status.checkpoints(account_id="acct_a")
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    from adcp.reporting.outbox import status_memory, status_pg

    called = 0
    original = status_pg.advance_checkpoint

    def fail(*args, **kwargs):
        nonlocal called
        called += 1
        if called == 3:
            raise SimulatedCrash("after_event_insert_pre_commit")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(status_pg, "advance_checkpoint", fail)
        patch.setattr(status_memory, "advance_checkpoint", fail)
        with pytest.raises(SimulatedCrash):
            await h.status.project_one(account_id="acct_a")
    assert await h.status.checkpoints(account_id="acct_a") == before
    assert not await h.status.outbox.list_events(account_id="acct_a")
    cls = type(h.status)
    await h.reliable.restart()
    h.status = cls(h.ledger)
    assert (await h.status.project_one(account_id="acct_a")).events == 6
    assert not (await h.status.project_one(account_id="acct_a")).did_work
    assert {c.generation for c in await h.status.checkpoints(account_id="acct_a")} == {1}


async def test_expiry_during_checkpoint_event_work_rolls_back_the_entire_turn(
    status_harness, monkeypatch
):
    h = status_harness
    obligation, _, _ = await h.seed()
    h.clock.now = obligation.period.expected_at - timedelta(seconds=1)
    await h.status.baseline(account_id="acct_a")
    h.clock.now = obligation.period.expected_at
    lease = await h.status.claim_due(account_id="acct_a", lease_seconds=30)
    assert lease is not None
    from adcp.reporting.outbox import status_memory, status_pg

    original = status_pg.advance_checkpoint

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        h.clock.now = lease.expires_at
        return result

    with monkeypatch.context() as patch:
        patch.setattr(status_pg, "advance_checkpoint", expire)
        patch.setattr(status_memory, "advance_checkpoint", expire)
        assert not (await h.status.complete_due(lease)).did_work
    assert not await h.status.outbox.list_events(account_id="acct_a")
    assert all(c.generation == 0 for c in await h.status.checkpoints(account_id="acct_a"))
    assert (await ReportingStatusSweeper(h.status).run_once(account_id="acct_a")).events == 2
    assert not (await ReportingStatusSweeper(h.status).run_once(account_id="acct_a")).did_work


async def test_database_clock_exact_expiry_reclaims_but_never_acknowledges():
    """Capture one real database instant into state and the production predicate.

    The connection instrumentation makes equality deterministic without replacing
    the clock with ManualClock or relying on a scheduling delay. The production
    claim and ACK statements retain their actual <= and > comparisons.
    """
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from .test_reporting_status_process_matrix import database_seed

    fence = None
    captured = []

    class ExactBoundaryConnection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            nonlocal fence
            if fence is not None and query == "SELECT clock_timestamp()":
                lease, fence = fence, None
                cursor = await super().execute(
                    "WITH moment AS MATERIALIZED (SELECT clock_timestamp() AS at),"
                    " fenced AS (UPDATE reporting_status_scope_checkpoints SET"
                    " lease_expires_at=moment.at FROM moment WHERE account_id=%s"
                    " AND consumer_namespace=%s AND delivery_config_id=%s AND version=%s"
                    " AND scope_kind=%s AND obligation_namespace=%s AND lease_token=%s"
                    " RETURNING lease_expires_at) SELECT lease_expires_at FROM fenced",
                    (*lease.scope.checkpoint_key, lease.token),
                )
                row = await cursor.fetchone()
                assert row is not None
                captured.append(row[0])

                class ClockResult:
                    async def fetchone(self):
                        return row

                return ClockResult()
            return await super().execute(query, params, **kwargs)

    async with isolated_reporting_pool(autocommit=True) as owner:
        await database_seed(owner, "zero_recovery")
        async with AsyncConnectionPool(
            owner.conninfo,
            kwargs=owner.kwargs,
            min_size=1,
            max_size=1,
            connection_class=ExactBoundaryConnection,
            open=False,
        ) as pool:
            ledger = PgReportingLedgerStore(pool=pool, notifications=True)
            status = PgStatusNotificationStore(ledger)
            first = await status.claim_due(account_id="acct_a", lease_seconds=60)
            assert first is not None
            fence = first
            reclaimed = await status.claim_due(account_id="acct_a", lease_seconds=60)
            assert reclaimed is not None
            assert reclaimed.scope == first.scope and reclaimed.token != first.token
            assert not await status.release_due(first)
            fence = reclaimed
            assert not await status.release_due(reclaimed)
            checkpoint = next(
                c
                for c in await status.checkpoints(account_id="acct_a")
                if c.scope == reclaimed.scope
            )
            assert checkpoint.lease_token == reclaimed.token
            assert checkpoint.lease_expires_at == captured[-1]
            assert len(captured) == 2 and captured[0] <= captured[1]
            assert (await ReportingStatusSweeper(status).run_once(account_id="acct_a")).events == 2
            assert not (await ReportingStatusSweeper(status).run_once(account_id="acct_a")).did_work
