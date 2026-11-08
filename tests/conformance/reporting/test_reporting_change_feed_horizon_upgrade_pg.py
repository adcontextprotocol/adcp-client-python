"""An account horizon a release without the ``(xid, seq)`` account feed pruned past is raised.

adcp-reporting-storage 0.1.0b5 hands out account cursors that carry ``xid`` and keeps the
account prune horizon in ``(xid, seq)``. 0.2.0b0 to 0.2.0b2 prune by ``seq`` and advance only
``pruned_through_seq``, so on a ledger 0.1.0b5 wrote, a cursor 0.1.0b5 handed out passed the
stale ``(xid, seq)`` check and silently skipped what 0.2.x pruned. The first ``create_schema``
of this release on such a ledger raises every account horizon to the deployment horizon, which
every release keeps in ``(xid, seq)``, or just below the account's oldest retained change when
that is lower; a ledger upgraded straight from 0.1.0b5 keeps its own.
The 0.2.x prune is staged with the statements that release runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from adcp_reporting_storage.ledger.change_feed import (
    ReportingChangeCursorExpiredError,
    encode_reporting_change_cursor_v1,
)

from ._generation_support import isolated_reporting_pool

ACCOUNT = "acct_interlude"
OTHER = "acct_other"
INSERT_CHANGE = (
    "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id, obligation_id)"
    " VALUES (%s, 'revision', %s, 'robl_interlude')"
)
MARKER = "adcp: account horizon maintained in (xid, seq)"


async def feed_store(pool: Any) -> PgReportingLedgerStore:
    store = PgReportingLedgerStore(pool=pool, change_feed=True)
    await store.create_schema()
    return store


async def insert(pool: Any, account: str, *record_ids: str) -> None:
    for record_id in record_ids:  # one transaction each, so each change has its own xid
        async with pool.connection() as connection:
            await connection.execute(INSERT_CHANGE, (account, record_id))


async def age(pool: Any, *record_ids: str) -> None:
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE adcp_reporting_changes SET recorded_at = recorded_at - INTERVAL '40 days'"
            " WHERE record_id = ANY(%s)",
            (list(record_ids),),
        )


async def cursor_at(pool: Any, account: str, record_id: str) -> str:
    """The account cursor 0.1.0b5 handed out after ``record_id``."""
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT seq::text, xid::text FROM adcp_reporting_changes WHERE record_id = %s",
                (record_id,),
            )
        ).fetchone()
    return encode_reporting_change_cursor_v1(account=account, seq=row[0], xid=row[1])


async def prune_like_0_2(pool: Any, *record_ids: str) -> None:
    """What 0.2.0b0-0.2.0b2's prune writes: the deployment horizon and pruned_through_seq."""
    async with pool.connection() as connection, connection.transaction():
        victims = await (
            await connection.execute(
                "DELETE FROM adcp_reporting_changes WHERE record_id = ANY(%s)"
                " RETURNING seq::text, xid::text, account_id",
                (list(record_ids),),
            )
        ).fetchall()
        highest = max(victims, key=lambda row: (int(row[1]), int(row[0])))
        await connection.execute(
            "UPDATE adcp_reporting_change_horizon"
            " SET (pruned_through_xid, pruned_through_seq) = (%s::text::xid8, %s::bigint)"
            " WHERE singleton AND (%s::text::xid8, %s::bigint) >"
            "       (pruned_through_xid, pruned_through_seq)",
            (highest[1], highest[0], highest[1], highest[0]),
        )
        per_account: dict[str, int] = {}
        for seq, _, account in victims:
            per_account[account] = max(per_account.get(account, 0), int(seq))
        for account, seq in per_account.items():
            await connection.execute(
                "INSERT INTO adcp_reporting_change_account_horizon (account_id, pruned_through_seq)"
                " VALUES (%s, %s::bigint) ON CONFLICT (account_id) DO UPDATE"
                "   SET pruned_through_seq = GREATEST("
                "       adcp_reporting_change_account_horizon.pruned_through_seq,"
                "       EXCLUDED.pruned_through_seq)",
                (account, seq),
            )


async def forget_this_release(pool: Any, *, keep_migrations: bool) -> None:
    """The ledger as 0.1.0b5 (and, with ``keep_migrations``, then 0.2.x) left it."""
    async with pool.connection() as connection:
        await connection.execute("COMMENT ON TABLE adcp_reporting_change_account_horizon IS NULL")
        if not keep_migrations:
            await connection.execute("DELETE FROM adcp_schema_migrations")


async def account_horizon(pool: Any, account: str) -> tuple[str, str, str]:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT pruned_through_xid::text, pruned_through_xid_seq::text,"
                " pruned_through_seq::text FROM adcp_reporting_change_account_horizon"
                " WHERE account_id = %s",
                (account,),
            )
        ).fetchone()
    return row


async def deployment_horizon(pool: Any) -> tuple[str, str]:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT pruned_through_xid::text, pruned_through_seq::text"
                " FROM adcp_reporting_change_horizon WHERE singleton"
            )
        ).fetchone()
    return row


async def marker(pool: Any) -> str | None:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT obj_description('adcp_reporting_change_account_horizon'::regclass,"
                " 'pg_class')"
            )
        ).fetchone()
    return row[0]


async def read(store: PgReportingLedgerStore, cursor: str | None) -> list[str]:
    """Poll: other transactions on the cluster can briefly hold a page back."""
    ids: list[str] = []
    for _ in range(200):
        page = await store.changes_after(account_id=ACCOUNT, cursor=cursor)
        ids = [record.record_id for record in page.records]
        if ids:
            break
        await asyncio.sleep(0.05)
    return ids


async def test_a_new_install_records_that_it_maintains_the_account_horizon() -> None:
    async with isolated_reporting_pool() as pool:
        await feed_store(pool)
        assert await marker(pool) == MARKER


async def test_a_0_2_prune_after_0_1_0b5_expires_the_cursors_it_could_have_skipped() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert(pool, ACCOUNT, "rrev_1", "rrev_2", "rrev_3", "rrev_4")
        # 0.1.0b5 prunes rrev_1 and records the account horizon in (xid, seq).
        handed_out = await cursor_at(pool, ACCOUNT, "rrev_1")
        await age(pool, "rrev_1")
        assert await store.prune_changes() == 1
        assert await read(store, handed_out) == ["rrev_2", "rrev_3", "rrev_4"]
        stale = await account_horizon(pool, ACCOUNT)

        # 0.2.x then prunes rrev_2 and rrev_3 without touching the xid columns.
        await forget_this_release(pool, keep_migrations=True)
        await prune_like_0_2(pool, "rrev_2", "rrev_3")
        assert (await account_horizon(pool, ACCOUNT))[:2] == stale[:2]
        # The hazard: on the stale horizon the old cursor reads on and skips both changes.
        assert await read(store, handed_out) == ["rrev_4"]

        await store.create_schema()  # this release starts on the ledger
        xid, xid_seq, _ = await account_horizon(pool, ACCOUNT)
        assert (xid, xid_seq) == await deployment_horizon(pool)
        assert await marker(pool) == MARKER
        with pytest.raises(ReportingChangeCursorExpiredError):
            await store.changes_after(account_id=ACCOUNT, cursor=handed_out)
        # A cursor at the last pruned change, and a consumer starting over, read on.
        at_horizon = encode_reporting_change_cursor_v1(account=ACCOUNT, seq=xid_seq, xid=xid)
        assert await read(store, at_horizon) == ["rrev_4"]
        assert await read(store, None) == ["rrev_4"]

        # Once recorded, the step never runs again: a later horizon is this release's own.
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_change_horizon SET pruned_through_seq ="
                " pruned_through_seq + 1000 WHERE singleton"
            )
        await store.create_schema()
        assert (await account_horizon(pool, ACCOUNT))[:2] == (xid, xid_seq)


async def test_the_raised_horizon_never_passes_a_retained_change() -> None:
    """When 0.2.x pruned another account past this one's retained changes, they still read."""
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert(pool, ACCOUNT, "rrev_1", "rrev_2", "rrev_3")
        await insert(pool, OTHER, "rrev_o1")
        handed_out = await cursor_at(pool, ACCOUNT, "rrev_1")
        await forget_this_release(pool, keep_migrations=True)
        await prune_like_0_2(pool, "rrev_1", "rrev_2", "rrev_o1")

        await store.create_schema()
        xid, xid_seq, _ = await account_horizon(pool, ACCOUNT)
        async with pool.connection() as connection:
            oldest = await (
                await connection.execute(
                    "SELECT xid::text, (seq - 1)::text FROM adcp_reporting_changes"
                    " WHERE record_id = 'rrev_3'"
                )
            ).fetchone()
        assert (xid, xid_seq) == tuple(oldest)
        deployment = await deployment_horizon(pool)
        assert (int(xid), int(xid_seq)) < (int(deployment[0]), int(deployment[1]))
        with pytest.raises(ReportingChangeCursorExpiredError):
            await store.changes_after(account_id=ACCOUNT, cursor=handed_out)
        assert await read(store, None) == ["rrev_3"]


async def test_a_ledger_upgraded_straight_from_0_1_0b5_keeps_its_account_horizon() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert(pool, ACCOUNT, "rrev_a1", "rrev_a2")
        await insert(pool, OTHER, "rrev_b1", "rrev_b2")
        handed_out = await cursor_at(pool, ACCOUNT, "rrev_a1")
        await age(pool, "rrev_a1")
        assert await store.prune_changes() == 1
        await age(pool, "rrev_b1")
        assert await store.prune_changes() == 1  # the deployment horizon moves past ACCOUNT's
        kept = await account_horizon(pool, ACCOUNT)
        deployment = await deployment_horizon(pool)
        assert (int(kept[0]), int(kept[1])) < (int(deployment[0]), int(deployment[1]))

        # 0.1.0b5 records no schema version; this release's first boot records 0001 itself.
        await forget_this_release(pool, keep_migrations=False)
        await store.create_schema()
        assert await account_horizon(pool, ACCOUNT) == kept
        assert await marker(pool) == MARKER
        assert await read(store, handed_out) == ["rrev_a2"]


async def test_an_install_from_before_the_xid_account_feed_keeps_the_seq_horizon() -> None:
    """Its account cursors carry no xid; seq-only cursors are checked by pruned_through_seq."""
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert(pool, ACCOUNT, "rrev_1", "rrev_2")
        await insert(pool, OTHER, "rrev_o1", "rrev_o2")
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE adcp_reporting_change_account_horizon"
                " DROP COLUMN pruned_through_xid, DROP COLUMN pruned_through_xid_seq"
            )
        await forget_this_release(pool, keep_migrations=True)
        await prune_like_0_2(pool, "rrev_1", "rrev_o1")
        await store.create_schema()
        xid, xid_seq, seq = await account_horizon(pool, ACCOUNT)
        assert (xid, xid_seq) == ("0", "0") and seq != "0"
        legacy = encode_reporting_change_cursor_v1(account=ACCOUNT, seq="0", xid=None)
        with pytest.raises(ReportingChangeCursorExpiredError):
            await store.changes_after(account_id=ACCOUNT, cursor=legacy)
        assert await read(store, None) == ["rrev_2"]
