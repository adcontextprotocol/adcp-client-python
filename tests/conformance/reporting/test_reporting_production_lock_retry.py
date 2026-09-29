"""Real PostgreSQL contention, rollback, fenced reacquisition, and retry limits."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from adcp.reporting.ledger import (
    LedgerConflictError,
    PgReportingLedgerStore,
    ProducerOfferings,
    ReportingProducer,
)
from adcp.reporting.materializer import PgReportingMaterializerStore

from ._generation_support import START, UncalledSource, configuration, isolated_reporting_pool
from ._production_support import production_harness
from .test_reporting_production_lock_order import source_turn
from .test_reporting_publication_time import public_outcome, setup


def producer(store):
    # No elapsed source period: every observable write is real lease bookkeeping.
    return ReportingProducer(
        source=UncalledSource(), store=store, offerings=ProducerOfferings(), clock=lambda: START
    )


@asynccontextmanager
async def lock_limit(pool, monkeypatch, value):
    checkout = pool.connection

    @asynccontextmanager
    async def connection(*args, **kwargs):
        async with checkout(*args, **kwargs) as connection:
            previous = (await (await connection.execute("SHOW lock_timeout")).fetchone())[0]
            await connection.execute("SELECT set_config('lock_timeout',%s,false)", (value,))
            try:
                yield connection
            finally:
                assert (await (await connection.execute("SHOW lock_timeout")).fetchone()) == (
                    value,
                )
                await connection.execute("SELECT set_config('lock_timeout',%s,false)", (previous,))

    with monkeypatch.context() as patch:
        patch.setattr(pool, "connection", connection)
        yield


async def wait_for_blocker(connection, worker, blocked_by):
    for _ in range(300):
        pid = worker()
        if pid is not None:
            row = await (
                await connection.execute("SELECT %s=ANY(pg_blocking_pids(%s))", (blocked_by, pid))
            ).fetchone()
            if row[0]:
                return
        await asyncio.sleep(0.01)
    pytest.fail("the real PostgreSQL operation did not reach its expected lock wait")


@pytest.mark.parametrize("configured, effective", [("0", "5s"), ("10s", "5s"), ("5ms", "5ms")])
async def test_transaction_local_lock_cap_covers_advisory_and_skip_locked(
    configured, effective, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        seen = []
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            if isinstance(query, str) and (
                query.startswith("SELECT pg_advisory_xact_lock(hashtext(%s))")
                or query.startswith("UPDATE reporting_configurations SET lease_worker_id")
            ):
                value = await (await execute(connection, "SHOW lock_timeout")).fetchone()
                seen.append(("SKIP LOCKED" in query, value[0]))
            return await execute(connection, query, *args, **kwargs)

        async with lock_limit(pool, monkeypatch, configured):
            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", observe)
                await store.put_configuration(configuration())
                lease = await store.lease_period_close(worker_id="cap", now=START, lease_seconds=60)
                assert lease is not None
                await store.release_period_close(lease, worker_id="cap")
        assert (False, effective) in seen and (True, effective) in seen
        assert all(value == effective for _, value in seen)


@pytest.mark.parametrize("held", ["account", "relation"])
async def test_default_lock_wait_is_bounded_and_rolls_back(held):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        config = configuration()
        await store.put_configuration(config)
        async with pool.connection() as blocker, blocker.transaction():
            if held == "account":
                await blocker.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting:' || %s))",
                    (config.account_id,),
                )
                operation = store.put_configuration(config)
            else:
                # SKIP LOCKED cannot skip this relation lock, even for sampling.
                await blocker.execute(
                    "LOCK TABLE reporting_configurations IN ACCESS EXCLUSIVE MODE"
                )
                operation = store.lease_period_close(
                    worker_id="blocked", now=START, lease_seconds=60
                )
            with pytest.raises(psycopg.errors.LockNotAvailable):
                await asyncio.wait_for(operation, 8)
        assert await store.list_configurations(account_id=config.account_id) == (config,)
        turn = await asyncio.wait_for(producer(store).run_worker(), 3)
        assert turn.leased is not None
        async with pool.connection() as connection:
            assert await (
                await connection.execute(
                    "SELECT lease_worker_id,lease_expires_at FROM reporting_configurations"
                )
            ).fetchone() == (None, None)


@pytest.mark.parametrize("caller_transaction, attempts", [(False, 3), (True, 1)])
async def test_lock_timeout_retries_are_bounded_and_outer_transaction_stays_owned(
    caller_transaction, attempts, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        await store.put_configuration(configuration())
        failures = []
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            try:
                return await execute(connection, query, *args, **kwargs)
            except psycopg.errors.LockNotAvailable:
                failures.append(connection.info.backend_pid)
                raise

        async with pool.connection() as blocker, blocker.transaction():
            await blocker.execute("LOCK TABLE reporting_configurations IN ACCESS EXCLUSIVE MODE")
            async with lock_limit(pool, monkeypatch, "30ms"):
                with monkeypatch.context() as patch:
                    patch.setattr(psycopg.AsyncConnection, "execute", observe)
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        if caller_transaction:

                            async def run_in_owned_transaction():
                                async with store.transaction():
                                    await producer(store).run_worker()

                            # Python 3.10 wait_for creates a child Task. The
                            # transaction and worker must share that Task.
                            await asyncio.wait_for(run_in_owned_transaction(), 3)
                        else:
                            await asyncio.wait_for(producer(store).run_worker(), 3)
        assert len(failures) == attempts
        assert (await producer(store).run_worker()).leased is not None


@pytest.mark.parametrize("competitor_wins", [False, True])
async def test_real_deadlock_rolls_back_before_retry_and_cannot_steal_a_new_lease(
    competitor_wins, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        config = configuration()
        await store.put_configuration(config)
        first = await store.lease_period_close(worker_id="seed-rank", now=START, lease_seconds=60)
        assert first is not None
        await store.release_period_close(first, worker_id="seed-rank")
        async with pool.connection() as connection:
            original_rank = await (
                await connection.execute("SELECT * FROM adcp_reporting_configuration_lease_turns")
            ).fetchall()
        locked, cycle, retrying, resume = (asyncio.Event() for _ in range(4))
        pids, failures = {}, []
        worker = None
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            if asyncio.current_task() is worker:
                pids["worker"] = connection.info.backend_pid
                if isinstance(query, str) and query.startswith("SELECT c.account_id") and failures:
                    retrying.set()
                    await asyncio.wait_for(resume.wait(), 5)
                if isinstance(query, str) and query.startswith(
                    "INSERT INTO adcp_reporting_configuration_lease_turns"
                ):
                    # Select the worker as the real server-detected victim; the
                    # blocker keeps PostgreSQL's normal, longer detection timer.
                    await execute(connection, "SET LOCAL deadlock_timeout = '50ms'")
            try:
                return await execute(connection, query, *args, **kwargs)
            except psycopg.errors.DeadlockDetected:
                failures.append(connection.info.backend_pid)
                raise

        async def conflicting_writer():
            async with pool.connection() as connection, connection.transaction():
                pids["blocker"] = connection.info.backend_pid
                await connection.execute(
                    "SELECT 1 FROM adcp_reporting_configuration_lease_turns FOR UPDATE"
                )
                locked.set()
                await asyncio.wait_for(cycle.wait(), 5)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting:' || %s))",
                    (config.account_id,),
                )

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", observe)
            blocker = asyncio.create_task(conflicting_writer())
            winner = None
            try:
                await asyncio.wait_for(locked.wait(), 5)
                worker = asyncio.create_task(producer(store).run_worker())
                async with pool.connection() as observer:
                    await wait_for_blocker(observer, lambda: pids.get("worker"), pids["blocker"])
                victim = pids["worker"]
                cycle.set()
                await asyncio.wait_for(blocker, 5)
                await asyncio.wait_for(retrying.wait(), 5)
                assert failures == [victim]
                async with pool.connection() as connection:
                    # The cancelled acquisition committed neither lease nor rank.
                    assert await (
                        await connection.execute(
                            "SELECT lease_worker_id,lease_expires_at FROM reporting_configurations"
                        )
                    ).fetchone() == (None, None)
                    assert (
                        await (
                            await connection.execute(
                                "SELECT * FROM adcp_reporting_configuration_lease_turns"
                            )
                        ).fetchall()
                        == original_rank
                    )
                if competitor_wins:
                    winner = await store.lease_period_close(
                        worker_id="winner", now=START, lease_seconds=60
                    )
                    assert winner is not None
                resume.set()
                turn = await asyncio.wait_for(worker, 5)
                assert (turn.leased is None) == competitor_wins
                if winner is not None:
                    async with pool.connection() as connection:
                        assert await (
                            await connection.execute(
                                "SELECT lease_worker_id,lease_expires_at"
                                " FROM reporting_configurations"
                            )
                        ).fetchone() == ("winner", winner.lease_expires_at)
                    await store.release_period_close(winner, worker_id="winner")
            finally:
                cycle.set()
                resume.set()
                tasks = [task for task in (blocker, worker) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        assert (await producer(store).run_worker()).leased is not None


async def test_production_lease_skips_a_locked_configuration_without_advancing_its_rank(tmp_path):
    async with production_harness("postgres", tmp_path / "destination.sqlite", periods=1) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        async with h.pool.connection() as blocker, blocker.transaction():
            before = await (
                await blocker.execute("SELECT * FROM adcp_reporting_configuration_lease_turns")
            ).fetchall()
            await blocker.execute("SELECT 1 FROM reporting_configurations FOR UPDATE")
            turn = await asyncio.wait_for(source_turn(h.production), 3)
            assert turn.leased is None
            assert (
                await (
                    await blocker.execute("SELECT * FROM adcp_reporting_configuration_lease_turns")
                ).fetchall()
                == before
            )
        assert (await asyncio.wait_for(source_turn(h.production), 5)).leased is not None


async def test_cancelled_work_transaction_releases_and_reacquires_before_publication(
    tmp_path, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        config, source, reader, _, factory = await setup(store, tmp_path / "source")
        failed, unlock, locked = (asyncio.Event() for _ in range(3))
        failures, leases = [], []
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            try:
                result = await execute(connection, query, *args, **kwargs)
            except psycopg.errors.LockNotAvailable:
                failures.append(connection.info.backend_pid)
                failed.set()
                raise
            if isinstance(query, str) and query.startswith(
                "UPDATE reporting_configurations SET lease_worker_id = %s"
            ):
                leases.append(connection.info.backend_pid)
            return result

        async def blocking_writer():
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "LOCK TABLE reporting_obligations IN ACCESS EXCLUSIVE MODE"
                )
                locked.set()
                await asyncio.wait_for(unlock.wait(), 5)

        async with lock_limit(pool, monkeypatch, "100ms"):
            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", observe)
                blocker = asyncio.create_task(blocking_writer())
                worker = None
                try:
                    await asyncio.wait_for(locked.wait(), 5)
                    worker = asyncio.create_task(factory().run_worker())
                    await asyncio.wait_for(failed.wait(), 5)
                    unlock.set()
                    await asyncio.wait_for(blocker, 5)
                    turn = await asyncio.wait_for(worker, 5)
                finally:
                    unlock.set()
                    tasks = [task for task in (blocker, worker) if task is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        assert len(failures) == 1 and len(leases) == 2
        assert len(turn.obligations_committed) == len(turn.revisions_committed) == 1
        assert len(source.executions) == reader.calls == 1
        outcome = await public_outcome(store, config)
        assert outcome.definitive
        assert len(outcome.ledger.revisions) == 1
        assert not (await factory().run_worker()).revisions_committed
        assert len(source.executions) == 1
        assert (await public_outcome(store, config)).ledger.revisions == outcome.ledger.revisions


@pytest.mark.parametrize("kind", ["conflict", "unknown", "cancel", "release_exhausted"])
async def test_non_lock_failure_is_not_retried_or_masked_by_release_contention(kind, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        await store.put_configuration(configuration())
        worker = producer(store)
        error = {
            "conflict": LedgerConflictError("REVISION_IMMUTABLE", "changed content"),
            "unknown": RuntimeError("not a retryable database failure"),
            "cancel": asyncio.CancelledError(),
            "release_exhausted": None,
        }[kind]
        calls, releases = [], []
        release = store.release_period_close

        async def fail_work(*args, **kwargs):
            calls.append(1)
            if error is not None:
                raise error

        async def fail_release(lease, **kwargs):
            releases.append(lease)
            raise psycopg.errors.LockNotAvailable()

        with monkeypatch.context() as patch:
            patch.setattr(worker, "_close_elapsed_periods", fail_work)
            patch.setattr(store, "release_period_close", fail_release)
            with pytest.raises(
                type(error) if error is not None else psycopg.errors.LockNotAvailable
            ) as raised:
                await worker.run_worker()
            if error is not None:
                assert raised.value is error
        assert len(calls) == 1 and len(releases) == 3
        assert len(set(releases)) == 1
        await release(releases[0], worker_id=worker._worker_id)
