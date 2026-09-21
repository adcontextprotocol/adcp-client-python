"""Public current stores share account-before-row lease order on upgraded schemas."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from adcp.reporting.feed import PgReportingFeedStore
from adcp.reporting.ledger import PgReportingLedgerStore, PgReportingReconciliationStore
from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.projection import PgReportingProjectionStore
from adcp.reporting.receipts import PgReportingReceiptStore

from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
)


@pytest.mark.parametrize(
    "store_type",
    [
        PgReportingLedgerStore,
        PgReportingReconciliationStore,
        PgReportingMaterializerStore,
        PgReportingReceiptStore,
        PgReportingFeedStore,
        PgReportingProjectionStore,
    ],
)
@pytest.mark.parametrize("operation", ["acquire", "release"])
async def test_public_stores_lease_and_configuration_writer_keep_consistent_order(
    store_type, operation, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = store_type(pool=pool)
        await store.create_schema()
        # Base/reconciliation writers can coexist with a materializer that
        # installed its trigger. All current classes must obey the same order.
        await PgReportingMaterializerStore(pool=pool).create_schema()
        first = configuration()
        sibling = replace(first, delivery_config_id="zz-sibling")
        await store.put_configuration(first)
        await store.put_configuration(sibling)
        held = None
        if operation == "release":
            held = await store.lease_period_close(worker_id="lease", now=NOW, lease_seconds=60)
            assert held is not None and held.generation_key == first.generation_key
        entered, resume = asyncio.Event(), asyncio.Event()
        lease_task = None
        pid = None
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            nonlocal pid
            if asyncio.current_task() is lease_task:
                pid = connection.info.backend_pid
            return await execute(connection, query, *args, **kwargs)

        async def writer():
            # One supported transaction groups two configuration writes. Pause
            # after the first public call, while it owns the account and only
            # the sibling row. No production method or SQL result is replaced.
            async with store.transaction():
                await store.put_configuration(sibling)
                entered.set()
                await asyncio.wait_for(resume.wait(), 5)
                await store.put_configuration(first)

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", observe)
            writer_task = asyncio.create_task(writer())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                lease_task = asyncio.create_task(
                    store.lease_period_close(worker_id="lease", now=NOW, lease_seconds=60)
                    if operation == "acquire"
                    else store.release_period_close(held, worker_id="lease")
                )
                # The fixed acquisition skips a busy account. Release may wait
                # for its account lock, but must not own the configuration row
                # while doing so. The predecessor blocks here in its trigger.
                async with pool.connection() as observer:
                    for _ in range(250):
                        if lease_task.done():
                            break
                        if pid is not None:
                            waiting = await (
                                await observer.execute(
                                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s"
                                    " AND locktype='advisory' AND NOT granted)",
                                    (pid,),
                                )
                            ).fetchone()
                            if waiting[0]:
                                break
                        await asyncio.sleep(0.01)
                    else:
                        pytest.fail("lease operation did not reach its lock boundary")
                resume.set()
                results = await asyncio.wait_for(
                    asyncio.gather(writer_task, lease_task, return_exceptions=True), 5
                )
                assert not any(isinstance(result, BaseException) for result in results), [
                    type(result).__name__ for result in results
                ]
                if operation == "release":
                    assert results[1] is None
                elif results[1] is not None:
                    await store.release_period_close(results[1], worker_id="lease")
            finally:
                resume.set()
                tasks = [task for task in (writer_task, lease_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        # Both sides complete, then a real subsequent lease/release advances.
        lease = await asyncio.wait_for(
            store.lease_period_close(worker_id="after", now=NOW, lease_seconds=60), 3
        )
        assert lease is not None
        await asyncio.wait_for(store.release_period_close(lease, worker_id="after"), 3)
        async with pool.connection() as connection:
            assert await (
                await connection.execute(
                    "SELECT count(*) FROM reporting_configurations WHERE lease_worker_id IS NOT"
                    " NULL"
                )
            ).fetchone() == (0,)
        assert await store.list_configurations(account_id=first.account_id) == (first, sibling)


@pytest.mark.parametrize("held_lock", ["account", "row"])
async def test_shared_lease_sampling_reaches_past_a_busy_prefix(held_lock):
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        configs = [replace(configuration(), delivery_config_id=f"busy-{i:02d}") for i in range(33)]
        for config in configs:
            await store.put_configuration(config)
        free = configuration("zz-open")
        await store.put_configuration(free)
        async with pool.connection() as blocker, blocker.transaction():
            if held_lock == "account":
                await blocker.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('adcp.reporting:' || %s))",
                    (configs[0].account_id,),
                )
            else:
                await blocker.execute(
                    "SELECT 1 FROM reporting_configurations WHERE account_id=%s FOR UPDATE",
                    (configs[0].account_id,),
                )
            assert (
                await asyncio.wait_for(
                    store.lease_period_close(worker_id="probe", now=NOW, lease_seconds=60), 3
                )
                is None
            )
            lease = await asyncio.wait_for(
                store.lease_period_close(worker_id="probe", now=NOW, lease_seconds=60), 3
            )
            assert lease is not None and lease.generation_key == free.generation_key
            await store.release_period_close(lease, worker_id="probe")
        lease = await store.lease_period_close(worker_id="resumed", now=NOW, lease_seconds=60)
        assert lease is not None and lease.account_id == configs[0].account_id
        await store.release_period_close(lease, worker_id="resumed")


async def test_sampled_configuration_cannot_steal_a_lease_committed_before_account_lock(
    monkeypatch,
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        config = configuration()
        await store.put_configuration(config)
        sampled, resume = asyncio.Event(), asyncio.Event()
        first = None
        execute = psycopg.AsyncConnection.execute

        async def pause_sample(connection, query, *args, **kwargs):
            result = await execute(connection, query, *args, **kwargs)
            if (
                asyncio.current_task() is first
                and not sampled.is_set()
                and isinstance(query, str)
                and query.startswith("SELECT c.account_id")
            ):
                sampled.set()
                await asyncio.wait_for(resume.wait(), 5)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", pause_sample)
            first = asyncio.create_task(
                store.lease_period_close(worker_id="stale-sample", now=NOW, lease_seconds=60)
            )
            try:
                await asyncio.wait_for(sampled.wait(), 5)
                winner = await store.lease_period_close(
                    worker_id="winner", now=NOW, lease_seconds=60
                )
                assert winner is not None
                async with pool.connection() as connection:
                    rank = await (
                        await connection.execute(
                            "SELECT lease_turn FROM adcp_reporting_configuration_lease_turns"
                        )
                    ).fetchall()
                resume.set()
                assert await asyncio.wait_for(first, 3) is None
                async with pool.connection() as connection:
                    assert await (
                        await connection.execute(
                            "SELECT lease_worker_id,lease_expires_at FROM reporting_configurations"
                        )
                    ).fetchone() == ("winner", winner.lease_expires_at)
                    assert (
                        await (
                            await connection.execute(
                                "SELECT lease_turn FROM adcp_reporting_configuration_lease_turns"
                            )
                        ).fetchall()
                        == rank
                    )
                await store.release_period_close(winner, worker_id="winner")
            finally:
                resume.set()
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome, previous", [("timeout", "5ms"), ("timeout", "5s"), ("cancel", "5s"), ("grant", "5s")]
)
async def test_bounded_account_wait_preserves_caller_transaction_and_timeout(
    outcome, previous, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        config = configuration()
        await store.put_configuration(config)
        # Widen only the barrier-controlled grant/cancel test's private budget;
        # actual default-budget expiry is exercised by the timeout cells.
        if outcome != "timeout":
            monkeypatch.setitem(
                store.lease_period_close.__func__.__globals__, "_LEASE_ACCOUNT_WAIT_SECONDS", 2
            )
        task = None
        pid = None
        limits = []
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            nonlocal pid
            if asyncio.current_task() is task:
                pid = connection.info.backend_pid
                if isinstance(query, str) and "set_config('lock_timeout'" in query:
                    parameters = args[0] if args else kwargs.get("params")
                    if parameters and parameters[0].endswith("ms"):
                        limits.append(int(parameters[0][:-2]))
            return await execute(connection, query, *args, **kwargs)

        checkout = pool.connection
        restored = []

        @asynccontextmanager
        async def checked_checkout(*args, **kwargs):
            async with checkout(*args, **kwargs) as connection:
                original = (await (await connection.execute("SHOW lock_timeout")).fetchone())[0]
                await connection.execute("SELECT set_config('lock_timeout',%s,false)", (previous,))
                try:
                    yield connection
                finally:
                    value = (await (await connection.execute("SHOW lock_timeout")).fetchone())[0]
                    restored.append(value)
                    assert value == previous
                    assert await (await connection.execute("SELECT 1")).fetchone() == (1,)
                    await connection.execute(
                        "SELECT set_config('lock_timeout',%s,false)", (original,)
                    )

        monkeypatch.setattr(pool, "connection", checked_checkout)

        async def caller():
            cancelled = False
            try:
                leased = await store.lease_period_close(
                    worker_id="bounded", now=NOW, lease_seconds=60
                )
            except asyncio.CancelledError:
                cancelled = True
                leased = None
            if leased is not None:
                await store.release_period_close(leased, worker_id="bounded")
            return cancelled, leased

        async with pool.connection() as holder:
            await holder.execute(
                "SELECT pg_advisory_lock(hashtext('adcp.reporting:' || %s))", (config.account_id,)
            )
            locked = True
            with monkeypatch.context() as patch:
                patch.setattr(psycopg.AsyncConnection, "execute", observe)
                try:
                    task = asyncio.create_task(caller())
                    async with pool.connection() as observer:
                        for _ in range(300):
                            if task.done():
                                break
                            if pid is not None:
                                waiting = await (
                                    await observer.execute(
                                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND"
                                        " locktype='advisory' AND NOT granted)",
                                        (pid,),
                                    )
                                ).fetchone()
                                if waiting[0]:
                                    break
                            await asyncio.sleep(0.005)
                        else:
                            pytest.fail("bounded lease did not reach its lock boundary")
                    if outcome == "cancel":
                        assert not task.done()
                        task.cancel()
                    elif outcome == "grant":
                        assert not task.done()
                        await holder.execute(
                            "SELECT pg_advisory_unlock(hashtext('adcp.reporting:' || %s))",
                            (config.account_id,),
                        )
                        locked = False
                    cancelled, leased = await asyncio.wait_for(task, 3)
                    assert cancelled == (outcome == "cancel")
                    assert (leased is not None) == (outcome == "grant")
                    assert limits
                    if previous == "5ms":
                        assert max(limits) <= 5
                finally:
                    if locked:
                        await holder.execute(
                            "SELECT pg_advisory_unlock(hashtext('adcp.reporting:' || %s))",
                            (config.account_id,),
                        )
                    if task is not None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        # Cancellation/timeout left neither a lease nor an unusable connection.
        leased = await store.lease_period_close(worker_id="after", now=NOW, lease_seconds=60)
        assert leased is not None
        await store.release_period_close(leased, worker_id="after")


@pytest.mark.parametrize("caller_transaction", [False, True])
async def test_lease_never_waits_for_another_account_after_acquiring_one(
    caller_transaction, monkeypatch
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingMaterializerStore(pool=pool)
        other = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        a, b = configuration("acct_a"), configuration("acct_b")
        await store.put_configuration(a)
        await store.put_configuration(b)
        # An incorrect implementation must not be rescued by a lock timeout
        # shorter than PostgreSQL's deadlock detector during this control.
        monkeypatch.setitem(
            store.lease_period_close.__func__.__globals__, "_LEASE_ACCOUNT_WAIT_SECONDS", 5
        )
        sampled, resume_sample, acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()
        writer_locked, resume_writer, proceed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        caller_locked, start_lease, decided, finish = (asyncio.Event() for _ in range(4))
        leaser = writer_task = None
        winner = None
        pids = {}
        queued = []
        execute = psycopg.AsyncConnection.execute

        async def observe(connection, query, *args, **kwargs):
            task = asyncio.current_task()
            if task is leaser:
                pids["lease"] = connection.info.backend_pid
            elif task is writer_task:
                pids["writer"] = connection.info.backend_pid
            if (
                task is leaser
                and isinstance(query, str)
                and query.startswith("SELECT pg_advisory_xact_lock(hashtext('adcp.reporting:'")
            ):
                queued.append(connection.info.backend_pid)
            result = await execute(connection, query, *args, **kwargs)
            if task is leaser and not caller_transaction and isinstance(query, str):
                if query.startswith("SELECT c.account_id") and not sampled.is_set():
                    sampled.set()
                    await asyncio.wait_for(resume_sample.wait(), 5)
                if query.startswith("SELECT pg_try_advisory_xact_lock") and not acquired.is_set():
                    acquired.set()
                    await asyncio.wait_for(proceed.wait(), 5)
            return result

        async def writer():
            async with other.transaction():
                await other.put_configuration(a if caller_transaction else b)
                writer_locked.set()
                await asyncio.wait_for(resume_writer.wait(), 5)
                await other.put_configuration(b if caller_transaction else a)

        async def caller():
            async with store.transaction():
                await store.put_configuration(b)
                caller_locked.set()
                await asyncio.wait_for(start_lease.wait(), 5)
                leased = await store.lease_period_close(
                    worker_id="caller", now=NOW, lease_seconds=60
                )
                if leased is not None:
                    await store.release_period_close(leased, worker_id="caller")
                decided.set()
                await asyncio.wait_for(finish.wait(), 5)
            return leased

        async def wait_for_waiter(role, task):
            async with pool.connection() as observer:
                while not task.done():
                    if role in pids:
                        row = await (
                            await observer.execute(
                                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND"
                                " locktype='advisory' AND NOT granted)",
                                (pids[role],),
                            )
                        ).fetchone()
                        if row[0]:
                            return
                    if role == "lease" and decided.is_set():
                        return
                    await asyncio.sleep(0.005)

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", observe)
            try:
                if caller_transaction:
                    leaser = asyncio.create_task(caller())
                    await asyncio.wait_for(caller_locked.wait(), 5)
                    writer_task = asyncio.create_task(writer())
                    await asyncio.wait_for(writer_locked.wait(), 5)
                    start_lease.set()
                    await asyncio.wait_for(wait_for_waiter("lease", leaser), 3)
                    resume_writer.set()
                    finish.set()
                else:
                    leaser = asyncio.create_task(
                        store.lease_period_close(worker_id="stale", now=NOW, lease_seconds=60)
                    )
                    await asyncio.wait_for(sampled.wait(), 5)
                    winner = await other.lease_period_close(
                        worker_id="winner", now=NOW, lease_seconds=60
                    )
                    assert winner is not None and winner.account_id == a.account_id
                    writer_task = asyncio.create_task(writer())
                    await asyncio.wait_for(writer_locked.wait(), 5)
                    resume_sample.set()
                    await asyncio.wait_for(acquired.wait(), 5)
                    # Leaser now holds A; the real writer holds B and wants A.
                    # Its stale A sample cannot acquire the already-live lease.
                    resume_writer.set()
                    await asyncio.wait_for(wait_for_waiter("writer", writer_task), 3)
                    proceed.set()
                results = await asyncio.wait_for(
                    asyncio.gather(leaser, writer_task, return_exceptions=True), 5
                )
                assert not any(isinstance(result, BaseException) for result in results), [
                    type(result).__name__ for result in results
                ]
                assert not queued  # No new blocking edge while an account is held.
                if not caller_transaction:
                    assert results[0] is None
            finally:
                for event in (resume_sample, proceed, resume_writer, start_lease, finish):
                    event.set()
                tasks = [task for task in (leaser, writer_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if winner is not None:
                    await other.release_period_close(winner, worker_id="winner")
        lease = await store.lease_period_close(worker_id="after", now=NOW, lease_seconds=60)
        assert lease is not None
        await store.release_period_close(lease, worker_id="after")
