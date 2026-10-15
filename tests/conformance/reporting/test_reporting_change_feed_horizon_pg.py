"""``changes_after`` re-reads the prune horizon after the page (adcp-client-python #1508).

Ported from the JavaScript SDK (adcp-client #3197, ``rejects a prune between its horizon and
page reads``). A prune deletes rows and advances the horizon in one transaction. If it commits
after ``changes_after`` read the horizon but before it read the page, the page lacks the pruned
changes while the first horizon still looks fine, and the returned cursor would silently pass
them. The race is staged with a real second connection that commits a prune between the two
statements of a read that is already in progress.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.ledger.change_feed import (
    ReportingChangeCursorExpiredError,
    encode_reporting_change_cursor_v1,
)

from ._generation_support import isolated_reporting_pool

ACCOUNT = "acct_race"
INSERT_CHANGE = (
    "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id, obligation_id)"
    " VALUES (%s, 'revision', %s, 'robl_race')"
)
PAGE_QUERY_MARKER = "FROM adcp_reporting_changes change"


async def feed_store(pool: Any) -> PgReportingLedgerStore:
    store = PgReportingLedgerStore(pool=pool, change_feed=True)
    await store.create_schema()
    return store


async def seed_changes(pool: Any) -> None:
    """Two changes past retention, then one that is not: a prune deletes only the first two."""
    for record_id in ("rrev_old_1", "rrev_old_2", "rrev_recent"):
        async with pool.connection() as connection:
            await connection.execute(INSERT_CHANGE, (ACCOUNT, record_id))
    async with pool.connection() as connection:
        await connection.execute(
            "UPDATE adcp_reporting_changes SET recorded_at = recorded_at - INTERVAL '40 days'"
            " WHERE record_id IN ('rrev_old_1', 'rrev_old_2')"
        )


class RacingPool:
    """Runs ``race`` once, just before the next change page query of a read in progress.

    Everything else is the real pool and the real connection, so the read's horizon query has
    already executed in its transaction when ``race`` commits on another connection.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self.race: Callable[[], Awaitable[None]] | None = None
        self.fired = False

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as real:
            yield _RacingConnection(real, self)


class _RacingConnection:
    def __init__(self, real: Any, owner: RacingPool) -> None:
        self._real = real
        self._owner = owner

    async def execute(self, query: str, params: Any = None) -> Any:
        race, self._owner.race = self._owner.race, None
        if race is not None and PAGE_QUERY_MARKER in query:
            self._owner.fired = True
            await race()
        elif race is not None:
            self._owner.race = race
        return await self._real.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def feed_cases() -> list[Any]:
    return [
        pytest.param(ACCOUNT, "with-cursor", id="account-with-cursor"),
        pytest.param(ACCOUNT, "no-cursor", id="account-without-cursor"),
        pytest.param(None, "with-cursor", id="deployment-with-cursor"),
        pytest.param(None, "no-cursor", id="deployment-without-cursor"),
    ]


def starting_cursor(account: str | None, mode: str) -> str | None:
    if mode == "no-cursor":
        return None
    if account is not None:
        return encode_reporting_change_cursor_v1(account=account, seq="0")
    return encode_reporting_change_cursor_v1(account=None, seq="0", xid="0")


async def read_recent(store: PgReportingLedgerStore, account: str | None) -> list[str]:
    """Poll: other transactions on the cluster can briefly hold a page back."""
    ids: list[str] = []
    for _ in range(200):
        page = await store.changes_after(account_id=account)
        ids = [record.record_id for record in page.records]
        if ids:
            break
        await asyncio.sleep(0.05)
    return ids


@pytest.mark.parametrize(("account", "mode"), feed_cases())
async def test_a_prune_between_the_horizon_and_page_reads_rejects_the_page(
    account: str | None, mode: str
) -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await seed_changes(pool)
        racing = RacingPool(pool)
        reader = PgReportingLedgerStore(pool=racing, change_feed=True)  # type: ignore[arg-type]

        async def prune_in_another_transaction() -> None:
            assert await store.prune_changes() == 2

        racing.race = prune_in_another_transaction
        # Without the second horizon read this returned ["rrev_recent"] and a cursor at it,
        # skipping the two pruned changes without any signal.
        with pytest.raises(ReportingChangeCursorExpiredError) as expired:
            await reader.changes_after(account_id=account, cursor=starting_cursor(account, mode))
        assert racing.fired, "the prune did not commit between the two reads"
        assert expired.value.code == "CURSOR_EXPIRED"

        # Retrying without a cursor starts from the new, later horizon: the oldest retained
        # change, with nothing skipped.
        assert await read_recent(store, account) == ["rrev_recent"]


@pytest.mark.parametrize(("account", "mode"), feed_cases())
async def test_a_prune_that_commits_before_the_read_starts_is_not_a_race(
    account: str | None, mode: str
) -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await seed_changes(pool)
        assert await store.prune_changes() == 2

        if mode == "with-cursor":
            with pytest.raises(ReportingChangeCursorExpiredError):
                await store.changes_after(account_id=account, cursor=starting_cursor(account, mode))
        else:
            assert await read_recent(store, account) == ["rrev_recent"]


@pytest.mark.parametrize(("account", "mode"), feed_cases())
async def test_a_prune_that_deletes_nothing_does_not_reject_the_page(
    account: str | None, mode: str
) -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        for record_id in ("rrev_a", "rrev_b"):
            async with pool.connection() as connection:
                await connection.execute(INSERT_CHANGE, (ACCOUNT, record_id))
        racing = RacingPool(pool)
        reader = PgReportingLedgerStore(pool=racing, change_feed=True)  # type: ignore[arg-type]

        async def prune_in_another_transaction() -> None:
            assert await store.prune_changes() == 0  # nothing is past retention

        racing.race = prune_in_another_transaction
        page = None
        for _ in range(200):
            racing.race = prune_in_another_transaction
            page = await reader.changes_after(
                account_id=account, cursor=starting_cursor(account, mode)
            )
            if page.records:
                break
            await asyncio.sleep(0.05)
        assert page is not None
        assert [record.record_id for record in page.records] == ["rrev_a", "rrev_b"]
