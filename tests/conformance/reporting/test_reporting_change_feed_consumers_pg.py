"""Feed consumer checkpoints are forward-only within a scope (adcp-client-python #1509).

Ported from the JavaScript SDK (adcp-client #3197): a delayed or repeated acknowledgment
must not move a consumer, or its retention hold, backward. Account consumers compare
``(xid, seq)`` like deployment consumers (adcp-client #3289, adcp-client-python #1825).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from adcp_reporting_storage.ledger.change_feed import encode_reporting_change_cursor_v1

from ._generation_support import isolated_reporting_pool
from .test_reporting_row_storage_pg import fetch_all


async def feed_store(pool: Any) -> PgReportingLedgerStore:
    store = PgReportingLedgerStore(pool=pool, change_feed=True)
    await store.create_schema()
    return store


def account_cursor(seq: int, account: str = "acct_a", xid: int | None = None) -> str:
    """A new-format account cursor; ``xid`` defaults to ``seq`` so the two orders agree."""
    return encode_reporting_change_cursor_v1(
        account=account, seq=str(seq), xid=str(seq if xid is None else xid)
    )


def legacy_account_cursor(seq: int, account: str = "acct_a") -> str:
    """The cursor an older SDK saved: a ``seq`` and no ``xid``."""
    return encode_reporting_change_cursor_v1(account=account, seq=str(seq))


def deployment_cursor(xid: int, seq: int) -> str:
    return encode_reporting_change_cursor_v1(account=None, seq=str(seq), xid=str(xid))


async def saved(pool: Any, name: str) -> tuple[Any, ...] | None:
    rows = await fetch_all(
        pool,
        "SELECT account_id, cursor_xid::text, cursor_seq FROM adcp_reporting_feed_consumers"
        " WHERE consumer_name = %s",
        (name,),
    )
    return rows[0] if rows else None


async def age(pool: Any, name: str) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE adcp_reporting_feed_consumers SET updated_at = updated_at - INTERVAL '1 day'"
            " WHERE consumer_name = %s",
            (name,),
        )


async def updated_at_age_seconds(pool: Any, name: str) -> float:
    rows = await fetch_all(
        pool,
        "SELECT EXTRACT(EPOCH FROM clock_timestamp() - updated_at) FROM"
        " adcp_reporting_feed_consumers WHERE consumer_name = %s",
        (name,),
    )
    return float(rows[0][0])


async def test_an_account_consumer_never_moves_backward_unless_asked_to() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        assert await store.save_feed_consumer_cursor("sink", account_cursor(10)) is True
        assert await store.save_feed_consumer_cursor("sink", account_cursor(20)) is True
        assert await saved(pool, "sink") == ("acct_a", "20", 20)

        # A delayed acknowledgment is ignored and says so.
        applied = await store.save_feed_consumer_cursor("sink", account_cursor(15))
        assert await saved(pool, "sink") == ("acct_a", "20", 20)
        assert applied is False

        # A deliberate replay rewinds, with or without the refresh option.
        assert await store.save_feed_consumer_cursor("sink", account_cursor(5), rewind=True) is True
        assert await saved(pool, "sink") == ("acct_a", "5", 5)
        assert (
            await store.save_feed_consumer_cursor(
                "sink", account_cursor(3), rewind=True, refresh_unchanged=False
            )
            is True
        )
        assert await saved(pool, "sink") == ("acct_a", "3", 3)

        # Compared numerically, not as text: 100 is ahead of 95.
        await store.save_feed_consumer_cursor("sink", account_cursor(95))
        assert await store.save_feed_consumer_cursor("sink", account_cursor(100)) is True
        assert await store.save_feed_consumer_cursor("sink", account_cursor(95)) is False
        assert await saved(pool, "sink") == ("acct_a", "100", 100)


async def test_a_repeated_acknowledgment_renews_the_hold_unless_told_not_to() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await store.save_feed_consumer_cursor("sink", account_cursor(7))

        await age(pool, "sink")
        assert (
            await store.save_feed_consumer_cursor(
                "sink", account_cursor(7), refresh_unchanged=False
            )
            is False
        )
        assert await updated_at_age_seconds(pool, "sink") > 86_000, "the hold was not renewed"

        assert await store.save_feed_consumer_cursor("sink", account_cursor(7)) is True
        assert await updated_at_age_seconds(pool, "sink") < 60, "a heartbeat renews the hold"

        # A stale acknowledgment never renews it, whatever the option says.
        await age(pool, "sink")
        assert await store.save_feed_consumer_cursor("sink", account_cursor(6)) is False
        assert await updated_at_age_seconds(pool, "sink") > 86_000


async def test_a_deployment_consumer_compares_xid_then_seq() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(50, 9)) is True

        # A later transaction with a lower seq is ahead in (xid, seq) order; an earlier
        # transaction with a higher seq is behind.
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(60, 2)) is True
        assert await saved(pool, "global") == (None, "60", 2)
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(55, 99)) is False
        assert await saved(pool, "global") == (None, "60", 2)

        # Same transaction: seq decides.
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(60, 3)) is True
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(60, 1)) is False
        assert await saved(pool, "global") == (None, "60", 3)

        # xid compares numerically past ten digits as well.
        assert (
            await store.save_feed_consumer_cursor("global", deployment_cursor(10_000_000_000, 1))
            is True
        )
        assert await store.save_feed_consumer_cursor("global", deployment_cursor(9, 99)) is False

        assert (
            await store.save_feed_consumer_cursor("global", deployment_cursor(60, 3), rewind=True)
            is True
        )
        assert await saved(pool, "global") == (None, "60", 3)


async def test_a_change_of_feed_scope_replaces_the_position() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await store.save_feed_consumer_cursor("sink", account_cursor(500, "acct_a"))
        # Positions in different feeds are not comparable, so a scope change always applies.
        assert await store.save_feed_consumer_cursor("sink", account_cursor(1, "acct_b")) is True
        assert await saved(pool, "sink") == ("acct_b", "1", 1)
        assert await store.save_feed_consumer_cursor("sink", deployment_cursor(5, 1)) is True
        assert await saved(pool, "sink") == (None, "5", 1)
        assert await store.save_feed_consumer_cursor("sink", account_cursor(900, "acct_a")) is True
        assert await saved(pool, "sink") == ("acct_a", "900", 900)


async def test_concurrent_writers_cannot_interleave_a_stale_position_over_a_newer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale acknowledgment waits on the newer acknowledgment's uncommitted update.

    Merely queuing both writers behind a third transaction does not order their writes.
    Hold the real newer write before commit, then prove the stale write is blocked by it.
    The conditional upsert must re-evaluate against the committed row and skip the stale write.
    """
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await store.save_feed_consumer_cursor("sink", account_cursor(1))

        newer: asyncio.Task[bool] | None = None
        stale: asyncio.Task[bool] | None = None
        newer_written = asyncio.Event()
        stale_connected = asyncio.Event()
        commit_newer = asyncio.Event()
        pids: dict[str, int] = {}
        checkout = pool.connection

        @asynccontextmanager
        async def connection(*args: Any, **kwargs: Any):
            async with checkout(*args, **kwargs) as current:
                task = asyncio.current_task()
                if task is newer:
                    pids["newer"] = current.info.backend_pid
                elif task is stale:
                    pids["stale"] = current.info.backend_pid
                    stale_connected.set()
                yield current
                if task is newer:
                    newer_written.set()
                    await commit_newer.wait()

        with monkeypatch.context() as patch:
            patch.setattr(pool, "connection", connection)
            try:
                newer = asyncio.create_task(
                    store.save_feed_consumer_cursor("sink", account_cursor(9))
                )
                await asyncio.wait_for(newer_written.wait(), timeout=5)
                stale = asyncio.create_task(
                    store.save_feed_consumer_cursor("sink", account_cursor(5))
                )
                await asyncio.wait_for(stale_connected.wait(), timeout=5)
                await blocked_writer(pool, pids["stale"], pids["newer"])
                assert not stale.done(), "the stale writer must overlap the uncommitted newer write"
                commit_newer.set()
                results = tuple(await asyncio.wait_for(asyncio.gather(newer, stale), timeout=10))
            finally:
                commit_newer.set()
                tasks = [task for task in (newer, stale) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        assert await saved(pool, "sink") == ("acct_a", "9", 9), "a stale write landed last"
        assert results == (True, False)


async def blocked_writer(pool: Any, writer_pid: int, blocker_pid: int) -> None:
    for _ in range(400):
        rows = await fetch_all(
            pool,
            "SELECT %s = ANY(pg_blocking_pids(%s))",
            (blocker_pid, writer_pid),
        )
        if rows[0][0]:
            return
        await asyncio.sleep(0.025)
    pytest.fail("the stale writer never blocked on the newer writer's transaction")


async def test_remove_feed_consumer_releases_the_hold() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        assert await store.remove_feed_consumer("sink") is False
        await store.save_feed_consumer_cursor("sink", account_cursor(4))
        assert await store.remove_feed_consumer("sink") is True
        assert await saved(pool, "sink") is None
        assert await store.remove_feed_consumer("sink") is False
        # Saving under the same name registers it again, at the position given.
        assert await store.save_feed_consumer_cursor("sink", account_cursor(2)) is True
        assert await saved(pool, "sink") == ("acct_a", "2", 2)
        with pytest.raises(ValueError):
            await store.remove_feed_consumer("bad name")


async def test_an_account_consumer_compares_xid_then_seq() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        assert await store.save_feed_consumer_cursor("sink", account_cursor(9, xid=50)) is True
        # A later transaction with a lower seq is ahead in (xid, seq) order; an earlier
        # transaction with a higher seq is behind.
        assert await store.save_feed_consumer_cursor("sink", account_cursor(2, xid=60)) is True
        assert await saved(pool, "sink") == ("acct_a", "60", 2)
        assert await store.save_feed_consumer_cursor("sink", account_cursor(99, xid=55)) is False
        assert await saved(pool, "sink") == ("acct_a", "60", 2)
        # Same transaction: seq decides.
        assert await store.save_feed_consumer_cursor("sink", account_cursor(3, xid=60)) is True
        assert await store.save_feed_consumer_cursor("sink", account_cursor(1, xid=60)) is False
        assert await saved(pool, "sink") == ("acct_a", "60", 3)


async def test_a_null_xid_consumer_row_is_replaced_by_a_new_format_cursor_unless_its_seq_is_behind() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_feed_consumers"
                " (consumer_name, account_id, cursor_xid, cursor_seq)"
                " VALUES ('old', 'acct_a', NULL, 9000)"
            )
        # A consumer row saved before the upgrade holds by seq; a cursor behind it is ignored.
        assert await store.save_feed_consumer_cursor("old", account_cursor(5, xid=7)) is False
        assert await saved(pool, "old") == ("acct_a", None, 9000)
        assert await store.save_feed_consumer_cursor("old", account_cursor(9000, xid=7)) is True
        assert await saved(pool, "old") == ("acct_a", "7", 9000)
        assert await store.save_feed_consumer_cursor("old", account_cursor(5, xid=7)) is False
        assert (
            await store.save_feed_consumer_cursor("old", account_cursor(5, xid=7), rewind=True)
            is True
        )


async def test_saving_a_seq_only_account_cursor_registers_the_position_changes_after_reads() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (xid, account_id, record_kind, record_id,"
                " obligation_id) VALUES ('10'::xid8, 'acct_a', 'revision', 'r1', 'o'),"
                " ('20'::xid8, 'acct_a', 'revision', 'r2', 'o'),"
                " ('30'::xid8, 'acct_a', 'revision', 'r3', 'o')"
            )
        first_seq = (await fetch_all(pool, "SELECT seq FROM adcp_reporting_changes ORDER BY seq"))[
            0
        ][0]
        # The hold sits below every unread change: the oldest one after the cursor, xid 20.
        assert await store.save_feed_consumer_cursor("sink", legacy_account_cursor(first_seq))
        assert await saved(pool, "sink") == ("acct_a", "20", first_seq)
        page = await store.changes_after(
            account_id="acct_a", cursor=legacy_account_cursor(first_seq)
        )
        assert [r.record_id for r in page.records] == ["r2", "r3"]
        # A new-format cursor replaces it, forward only.
        assert await store.save_feed_consumer_cursor("sink", page.cursor) is True
        row = await saved(pool, "sink")
        assert row is not None and row[1] == "30"
        # A mapped seq-only cursor is forward-only against a new-format registration.
        assert (
            await store.save_feed_consumer_cursor("sink", legacy_account_cursor(first_seq)) is False
        )
