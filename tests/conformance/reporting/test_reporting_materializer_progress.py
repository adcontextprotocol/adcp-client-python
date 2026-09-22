"""Account progress while peers stay due, including bounded busy-account scans."""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager

import pytest

from adcp.reporting.materializer import (
    PgReportingMaterializerStore,
    ReportingMaterializerLease,
    ReportingWriterError,
)

from ._durable_materializer_support import durable_case


@asynccontextmanager
async def progress_pool(*, autocommit=False):
    """Each scenario owns a schema and a size-one pool on the real database."""
    url = os.environ.get("ADCP_PG_TEST_URL")
    if not url:
        pytest.skip("ADCP_PG_TEST_URL not set — requires real PostgreSQL")
    psycopg = pytest.importorskip("psycopg")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    schema = "adcp_materializer_progress_" + secrets.token_hex(6)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(
            psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema))
        )
        try:
            async with psycopg_pool.AsyncConnectionPool(
                url,
                kwargs={
                    "options": f"-csearch_path={schema} -cstatement_timeout=15000",
                    "autocommit": autocommit,
                },
                min_size=1,
                max_size=1,
                open=False,
            ) as pool:
                await pool.wait(timeout=10)
                yield pool, schema
        finally:
            await admin.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


async def wake(pool, account):
    # This is the installed function invoked by real producer/binding triggers.
    # The store-level regression enrolls due accounts, never successful work.
    async with pool.connection() as connection:
        await connection.execute("SELECT reporting_materializer_wake(%s)", (account,))


async def served(pool):
    async with pool.connection() as connection:
        return dict(
            await (
                await connection.execute(
                    "SELECT account_id,served_at::text FROM reporting_materializer_accounts"
                    " ORDER BY served_at,account_id"
                )
            ).fetchall()
        )


@pytest.mark.parametrize(
    "first,late,keep_waking_first,restart_at_enrollment,expected_late",
    [
        ("account-a", "account-z", True, False, 15),
        ("account-z", "account-a", True, False, 15),
        ("account-a", "account-z", False, False, 30),
        ("account-a", "account-z", True, True, 15),
    ],
    ids=["continuous", "reverse-order", "producer-stops-control", "fresh-store-control"],
)
async def test_late_due_account_is_served_while_the_first_stays_continuously_due(
    first, late, keep_waking_first, restart_at_enrollment, expected_late
):
    async with progress_pool() as (pool, _):
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        await wake(pool, first)
        trace = []
        for turn in range(40):
            if turn == 10:
                await wake(pool, late)
                if restart_at_enrollment:
                    store = PgReportingMaterializerStore(pool=pool)
            # Waking from turn zero is essential: a gap before late enrollment
            # could reset the old cursor and hide the regression.
            if keep_waking_first:
                await wake(pool, first)
            if turn >= 10:
                await wake(pool, late)
            before = await served(pool)
            await store.claim_materialization(keys=())
            after = await served(pool)
            moved = [account for account in after if before.get(account) != after[account]]
            assert len(moved) <= 1
            trace.append({"turn": turn, "served": moved})
        late_turns = [row["turn"] for row in trace if late in row["served"]]
        print(
            json.dumps(
                {
                    "late_account_progress": {
                        "first": first,
                        "late": late,
                        "continuous_first": keep_waking_first,
                        "restart_at_enrollment": restart_at_enrollment,
                        "trace": trace,
                        "late_turns": late_turns,
                        "pool_size": 1,
                    }
                }
            ),
            flush=True,
        )
        assert late_turns and late_turns[0] <= 11, "late account was excluded from due sampling"
        assert len(late_turns) == expected_late


def observe_samples(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    original = psycopg.AsyncConnection.execute
    samples = []

    async def execute(connection, query, *args, **kwargs):
        result = await original(connection, query, *args, **kwargs)
        if isinstance(query, str) and query.startswith(
            "SELECT account_id,served_at::text FROM reporting_materializer_accounts WHERE due_at"
        ):
            assert query.endswith("LIMIT 16")
            samples.append(result.rowcount)
        return result

    monkeypatch.setattr(psycopg.AsyncConnection, "execute", execute)
    return samples


async def bounded_claim(store, samples, *, keys=()):
    before = len(samples)
    result = await asyncio.wait_for(store.claim_materialization(keys=keys), 10)
    sizes = samples[before:]
    assert 1 <= len(sizes) <= 2 and 0 <= sum(sizes) <= 16, sizes
    return result


@pytest.mark.parametrize("notifications", [False, True])
async def test_more_than_two_busy_pages_progress_then_recover_after_unlock(
    notifications, monkeypatch
):
    async with progress_pool(autocommit=True) as (pool, schema):
        from psycopg import AsyncConnection

        store = PgReportingMaterializerStore(pool=pool, notifications=notifications)
        await store.create_schema()
        busy = [f"busy-{number:02}" for number in range(33)]
        available = "zz-available"
        for account in [*busy, available]:
            await wake(pool, account)
        samples = observe_samples(monkeypatch)
        async with (
            await AsyncConnection.connect(
                os.environ["ADCP_PG_TEST_URL"],
                options=f"-csearch_path={schema} -cstatement_timeout=15000",
                autocommit=True,
            ) as holder,
            holder.transaction(),
        ):
            for account in busy:
                await store._lock_account(holder, account)
            before = await served(pool)
            for _ in range(2):
                assert (await bounded_claim(store, samples)).state == "idle"
                assert await served(pool) == before
            assert samples == [16, 16]
            await bounded_claim(store, samples)
            after = await served(pool)
            assert after[available] != before[available]
            assert {a: after[a] for a in busy} == {a: before[a] for a in busy}
            assert samples[-1] == 2
            # A successful account turn restarts at the oldest durable rank;
            # subsequent busy turns must still walk through all three pages.
            for expected in (16, 16, 1):
                await bounded_claim(store, samples)
                assert samples[-1] == expected
            await bounded_claim(store, samples)
            assert samples[-2:] == [0, 16]
        recovered = set()
        for _ in range(len(busy) + 3):
            before = await served(pool)
            await bounded_claim(store, samples)
            after = await served(pool)
            recovered.update(a for a in busy if after[a] != before[a])
            if recovered == set(busy):
                break
        assert recovered == set(busy)
        assert all(value != "-infinity" for value in (await served(pool)).values())


@pytest.mark.parametrize("notifications", [False, True])
async def test_commit_failure_rolls_back_rank_and_reservation_then_retries(notifications):
    async with progress_pool(autocommit=True) as (pool, _):
        store = PgReportingMaterializerStore(pool=pool, notifications=notifications)
        await store.create_schema()
        case = await durable_case(store)
        before = await served(pool)
        async with pool.connection() as connection:
            # A deferred database error fails COMMIT after all reservation and
            # scheduling statements executed. No SDK operation is replaced.
            await connection.execute(
                "CREATE FUNCTION progress_fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$"
                " BEGIN RAISE EXCEPTION 'private-progress-commit-fault'; END $$"
            )
            await connection.execute(
                "CREATE CONSTRAINT TRIGGER progress_fail_commit"
                " AFTER UPDATE ON reporting_materializer_accounts"
                " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW"
                " EXECUTE FUNCTION progress_fail_commit()"
            )
        try:
            with pytest.raises(ReportingWriterError) as error:
                await store.claim_materialization(keys=case.keys)
            assert error.value.failure.code == "RESOURCE_UNAVAILABLE"
            assert "private-progress" not in str(error.value)
            assert await served(pool) == before
            async with pool.connection() as connection:
                assert await (
                    await connection.execute("SELECT count(*) FROM reporting_materializer_work")
                ).fetchone() == (0,)
        finally:
            async with pool.connection() as connection:
                await connection.execute(
                    "DROP TRIGGER progress_fail_commit ON reporting_materializer_accounts"
                )
                await connection.execute("DROP FUNCTION progress_fail_commit()")
        lease = await case.claim()
        assert isinstance(lease, ReportingMaterializerLease)
        assert lease.attempt.attempt == 1
        assert await store.renew_materialization(lease, lease_seconds=30)
        assert await served(pool) != before


async def test_two_workers_skip_uncommitted_peer_and_reserve_distinct_accounts(monkeypatch):
    async with progress_pool(autocommit=True) as (pool, schema):
        from psycopg import AsyncConnection
        from psycopg_pool import AsyncConnectionPool

        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        first = await durable_case(store, account="account-a")
        second = await durable_case(store, account="account-z")
        async with AsyncConnectionPool(
            os.environ["ADCP_PG_TEST_URL"],
            kwargs={"options": f"-csearch_path={schema}", "autocommit": True},
            min_size=1,
            max_size=1,
            open=False,
        ) as peer_pool:
            await peer_pool.wait(timeout=10)
            peer = PgReportingMaterializerStore(pool=peer_pool)
            holding, release = asyncio.Event(), asyncio.Event()
            original = AsyncConnection.execute
            winner = None

            async def execute(connection, query, *args, **kwargs):
                result = await original(connection, query, *args, **kwargs)
                if (
                    asyncio.current_task() is winner
                    and isinstance(query, str)
                    and query.startswith("UPDATE reporting_materializer_accounts SET served_at=")
                ):
                    # Only pause after the real update, under the real locks.
                    holding.set()
                    await asyncio.wait_for(release.wait(), 10)
                return result

            monkeypatch.setattr(AsyncConnection, "execute", execute)
            winner = asyncio.create_task(store.claim_materialization(keys=first.keys))
            try:
                await asyncio.wait_for(holding.wait(), 10)
                other = await asyncio.wait_for(peer.claim_materialization(keys=second.keys), 10)
                assert isinstance(other, ReportingMaterializerLease)
                assert other.scope.principal.account_id == "account-z"
                release.set()
                selected = await asyncio.wait_for(winner, 10)
            finally:
                release.set()
                if not winner.done():
                    winner.cancel()
                await asyncio.gather(winner, return_exceptions=True)
            assert isinstance(selected, ReportingMaterializerLease)
            assert selected.scope.principal.account_id == "account-a"
            assert selected.request.external_id != other.request.external_id
            await store.authorize_materialization(selected)
            await peer.authorize_materialization(other)
            assert await store.renew_materialization(selected, lease_seconds=30)
            assert await peer.renew_materialization(other, lease_seconds=30)
            for current in (store, peer):
                assert not isinstance(
                    await current.claim_materialization(keys=first.keys), ReportingMaterializerLease
                )
            async with pool.connection() as connection:
                assert await (
                    await connection.execute("SELECT count(*) FROM reporting_materializer_work")
                ).fetchone() == (2,)
