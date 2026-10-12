"""The shared host change feed: ordering, visibility, consumers, pruning and cursor expiry."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.ledger.change_feed import (
    ReportingChangeCursorExpiredError,
    decode_reporting_change_cursor_v1,
    encode_reporting_change_cursor_v1,
)

from ._generation_support import configuration, isolated_reporting_pool, obligation_for
from .test_reporting_row_storage_pg import fetch_all, many_rows, typed_revision


async def feed_store(pool: Any) -> PgReportingLedgerStore:
    store = PgReportingLedgerStore(pool=pool, change_feed=True)
    await store.create_schema()
    return store


async def seed(store: PgReportingLedgerStore, account: str = "acct_a") -> Any:
    config = configuration(account)
    await store.put_configuration(config)
    return await store.commit_obligation(obligation_for(config))


def test_cursors_use_the_format_the_javascript_sdk_reads_and_writes() -> None:
    js_deployment = (
        base64.urlsafe_b64encode(b'{"v":1,"account":null,"seq":"7","xid":"42"}')
        .rstrip(b"=")
        .decode()
    )
    assert encode_reporting_change_cursor_v1(account=None, seq="7", xid="42") == js_deployment
    assert decode_reporting_change_cursor_v1(js_deployment, None) == {
        "v": 1,
        "account": None,
        "seq": "7",
        "xid": "42",
    }
    js_account = (
        base64.urlsafe_b64encode(b'{"v":1,"account":"acct_a","seq":"9"}').rstrip(b"=").decode()
    )
    assert encode_reporting_change_cursor_v1(account="acct_a", seq="9") == js_account
    for bad, account in (
        ("not base64!", None),
        (js_account, "acct_b"),
        (js_account, None),
        (js_deployment, "acct_a"),
        (base64.urlsafe_b64encode(b'{"v":2,"account":null,"seq":"1","xid":"1"}').decode(), None),
        (base64.urlsafe_b64encode(b'{"v":1,"account":null,"seq":"x","xid":"1"}').decode(), None),
    ):
        with pytest.raises(ValueError):
            decode_reporting_change_cursor_v1(bad, account)


async def test_each_committed_record_appends_one_change_in_account_order() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        obligation = await seed(store)
        rows = many_rows(3)
        first = typed_revision(obligation, rows, suffix="a")
        second = typed_revision(
            obligation, rows, suffix="b", supersedes=first.reporting_revision_id
        )
        await store.commit_revision(first, rows)
        await store.commit_revision(first, rows)  # a replay appends nothing
        await store.commit_revision(second, rows)

        page = await store.changes_after(account_id="acct_a")
        assert [(r.kind, r.record_id, r.reporting_obligation_id) for r in page.records] == [
            ("obligation", obligation.reporting_obligation_id, obligation.reporting_obligation_id),
            ("revision", first.reporting_revision_id, obligation.reporting_obligation_id),
            ("revision", second.reporting_revision_id, obligation.reporting_obligation_id),
        ]
        assert all(r.account_id == "acct_a" and r.recorded_at.endswith("Z") for r in page.records)

        # A cursor resumes after exactly what it returned, and an idle poll keeps it.
        assert (await store.changes_after(account_id="acct_a", cursor=page.cursor)).records == ()
        again = await store.changes_after(account_id="acct_a", cursor=page.cursor)
        assert again.cursor == page.cursor
        third = typed_revision(
            obligation, rows, suffix="c", supersedes=second.reporting_revision_id
        )
        await store.commit_revision(third, rows)
        later = await store.changes_after(account_id="acct_a", cursor=page.cursor)
        assert [r.record_id for r in later.records] == [third.reporting_revision_id]

        only = await store.changes_after(account_id="acct_a", kinds=["obligation"])
        assert [r.kind for r in only.records] == ["obligation"]
        small = await store.changes_after(account_id="acct_a", limit=2)
        rest = await store.changes_after(account_id="acct_a", cursor=small.cursor, limit=10)
        assert len(small.records) == 2 and len(rest.records) == 2
        with pytest.raises(ValueError):
            await store.changes_after(
                account_id="acct_a",
                cursor=encode_reporting_change_cursor_v1(account="acct_b", seq="1"),
            )
        with pytest.raises(ValueError):
            await store.changes_after(limit=0)


async def test_accounts_have_independent_feeds_and_a_store_without_the_option_writes_none() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await seed(store, "acct_a")
        await seed(store, "acct_b")
        assert {r.account_id for r in (await store.changes_after(account_id="acct_a")).records} == {
            "acct_a"
        }
        assert len((await store.changes_after()).records) == 2
    async with isolated_reporting_pool() as pool:
        plain = PgReportingLedgerStore(pool=pool)
        await plain.create_schema()
        await seed(plain)
        assert await fetch_all(
            pool, "SELECT to_regclass(current_schema() || '.adcp_reporting_changes') IS NULL"
        ) == [(True,)]
        with pytest.raises(ValueError):
            await plain.changes_after()


async def test_the_deployment_feed_never_skips_a_slow_commit() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        obligation = await seed(store)
        baseline = await store.changes_after()
        assert len(baseline.records) == 1

        # A transaction takes a lower xid, stays open, and commits after a later record.
        slow = await pool.getconn()
        try:
            await slow.execute(
                "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id, obligation_id)"
                " VALUES ('acct_slow', 'revision', 'rpr_slow', 'robl_slow')"
            )
            rows = many_rows(2)
            await store.commit_revision(typed_revision(obligation, rows, suffix="fast"), rows)
            # The later commit is invisible while the lower transaction is in flight.
            assert (await store.changes_after(cursor=baseline.cursor)).records == ()
            # Account readers use the same guard, so ordering never relies on the account lock:
            # they wait for the in-flight transaction too, and lose nothing by it.
            during = await store.changes_after(account_id="acct_a")
            assert all(r.record_id != "rpr_acct_a_fast" for r in during.records)
            await slow.commit()
        finally:
            await pool.putconn(slow)
        resumed = await store.changes_after(cursor=baseline.cursor)
        assert [r.record_id for r in resumed.records] == ["rpr_slow", "rpr_acct_a_fast"]


async def test_consumers_hold_back_pruning_and_old_cursors_expire() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        obligation = await seed(store)
        rows = many_rows(2)
        first = typed_revision(obligation, rows, suffix="a")
        await store.commit_revision(first, rows)
        start = await store.changes_after(account_id="acct_a", limit=1)  # after the obligation
        everything = await store.changes_after(account_id="acct_a")
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_changes SET recorded_at = recorded_at - INTERVAL '40 days'"
            )

        # A live consumer that has passed only the first change holds back the second.
        await store.save_feed_consumer_cursor("sink", start.cursor)
        assert await store.prune_changes() == 1
        assert [r.kind for r in (await store.changes_after(account_id="acct_a")).records] == [
            "revision"
        ]
        # The consumer's own cursor is still valid; an older one is not.
        assert (await store.changes_after(account_id="acct_a", cursor=start.cursor)).records
        with pytest.raises(ReportingChangeCursorExpiredError) as expired:
            await store.changes_after(
                account_id="acct_a",
                cursor=encode_reporting_change_cursor_v1(account="acct_a", seq="0"),
            )
        assert expired.value.code == "CURSOR_EXPIRED"

        # Once it passes everything, retention proceeds.
        await store.save_feed_consumer_cursor("sink", everything.cursor)
        assert await store.prune_changes() == 1
        assert (await store.changes_after(account_id="acct_a")).records == ()

        # A silent consumer stops holding after max_feed_hold_days.
        second = typed_revision(
            obligation, rows, suffix="b", supersedes=first.reporting_revision_id
        )
        await store.commit_revision(second, rows)
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_changes SET recorded_at = recorded_at - INTERVAL '40 days'"
            )
            await connection.execute(
                "UPDATE adcp_reporting_feed_consumers SET updated_at = updated_at - INTERVAL '10 days'"
            )
        assert await store.prune_changes(max_feed_hold_days=7) == 1


async def test_deployment_consumers_and_the_deployment_horizon() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await seed(store, "acct_a")
        await seed(store, "acct_b")
        before = await store.changes_after()
        await store.save_feed_consumer_cursor(
            "global", encode_reporting_change_cursor_v1(account=None, seq="0", xid="0")
        )
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_changes SET recorded_at = recorded_at - INTERVAL '40 days'"
            )
        # The deployment consumer has passed nothing, so nothing is pruned.
        assert await store.prune_changes() == 0
        await store.save_feed_consumer_cursor("global", before.cursor)
        assert await store.prune_changes() == 2
        with pytest.raises(ReportingChangeCursorExpiredError):
            await store.changes_after(
                cursor=encode_reporting_change_cursor_v1(account=None, seq="0", xid="0")
            )
        assert (await store.changes_after(cursor=before.cursor)).records == ()
        for bad in ("", "x" * 200):
            with pytest.raises(ValueError):
                await store.save_feed_consumer_cursor("global", bad)
        with pytest.raises(ValueError):
            await store.save_feed_consumer_cursor("bad name", before.cursor)


async def test_pages_cross_99_to_100_in_numeric_order_on_both_feeds() -> None:
    """``seq::text`` is named ``seq``; a bare ORDER BY would sort 100 before 95 as text."""
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (seq, account_id, record_kind, record_id,"
                " obligation_id) SELECT n, 'acct_a', 'revision', 'rev_' || n, 'obl_a'"
                " FROM generate_series(1, 130) AS n"
            )
        (xid,) = (await fetch_all(pool, "SELECT xid::text FROM adcp_reporting_changes LIMIT 1"))[0]

        for account, start in (
            ("acct_a", encode_reporting_change_cursor_v1(account="acct_a", seq="94")),
            (None, encode_reporting_change_cursor_v1(account=None, seq="94", xid=xid)),
        ):
            cursor = start
            seen: list[int] = []
            for _ in range(8):
                page = await store.changes_after(account_id=account, cursor=cursor, limit=5)
                seen.extend(int(r.record_id.removeprefix("rev_")) for r in page.records)
                cursor = page.cursor
            assert seen == list(range(95, 131)), (account, seen)


# -- account feed ordering, ported from the JavaScript SDK (adcp-client #3228) ---------------

INSERT_CHANGE = (
    "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id, obligation_id)"
    " VALUES ('acct_hol_feed', 'revision', %s, 'robl_hol_feed')"
)


def ids(page: Any) -> list[str]:
    return [record.record_id for record in page.records]


async def committed(pool: Any, record_id: str) -> None:
    async with pool.connection() as connection:
        await connection.execute(INSERT_CHANGE, (record_id,))


async def until(read: Any, expected: list[str]) -> Any:
    """Other tests share the cluster, so an unrelated older transaction can briefly hold a page
    back: positive reads poll. The assertions that a page is held back do not need it."""
    import asyncio

    for _ in range(200):
        page = await read()
        if ids(page) == expected:
            return page
        await asyncio.sleep(0.05)
    return page


async def test_a_lower_seq_that_commits_after_a_higher_one_is_never_passed_over() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id="acct_hol_feed")
        slow = await pool.getconn()
        try:
            await slow.execute(INSERT_CHANGE, ("rrev_low",))  # the lower seq, commits last
            await committed(pool, "rrev_high")  # a higher seq, committed already
            during = await store.changes_after(account_id="acct_hol_feed", cursor=start.cursor)
            assert ids(during) == [], "the page stops before the unfinished transaction"
            assert during.cursor == start.cursor, "the cursor does not move past it"
            await slow.commit()
        finally:
            await pool.putconn(slow)
        after = await until(
            lambda: store.changes_after(account_id="acct_hol_feed", cursor=start.cursor),
            ["rrev_low", "rrev_high"],
        )
        assert ids(after) == ["rrev_low", "rrev_high"]


async def test_a_held_back_change_is_never_skipped_when_a_later_one_passes_a_plain_xid_filter() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id="acct_hol_feed")
        early = await pool.getconn()
        blocker = await pool.getconn()
        try:
            # xid order: early < blocker < the change inserted next.
            await early.execute("SELECT pg_current_xact_id()")
            await blocker.execute("SELECT pg_current_xact_id()")
            await committed(pool, "rrev_first")  # lower seq, but a transaction after `blocker`
            await early.execute(INSERT_CHANGE, ("rrev_second",))  # higher seq, xid before `blocker`
            await early.commit()
            # `blocker` is the oldest transaction in flight, so rrev_first is held back while
            # rrev_second sits below it. Filtering on xid alone would return rrev_second and a
            # cursor past rrev_first, which would then never be seen.
            during = await store.changes_after(account_id="acct_hol_feed", cursor=start.cursor)
            assert ids(during) == []
            assert during.cursor == start.cursor, "the cursor does not move past the held change"
            await blocker.commit()
        finally:
            await pool.putconn(early)
            await pool.putconn(blocker)
        after = await until(
            lambda: store.changes_after(account_id="acct_hol_feed", cursor=start.cursor),
            ["rrev_first", "rrev_second"],
        )
        assert ids(after) == ["rrev_first", "rrev_second"]


async def test_a_committed_prefix_is_returned_and_the_cursor_stops_before_the_first_in_flight() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id="acct_hol_feed")
        await committed(pool, "rrev_prefix_a")
        opened = await pool.getconn()
        try:
            await opened.execute(INSERT_CHANGE, ("rrev_prefix_b",))  # in flight
            await committed(pool, "rrev_prefix_c")  # committed after it
            during = await until(
                lambda: store.changes_after(account_id="acct_hol_feed", cursor=start.cursor),
                ["rrev_prefix_a"],
            )
            assert ids(during) == ["rrev_prefix_a"], "truncated, not filtered"
            await opened.commit()
            rest = await until(
                lambda: store.changes_after(account_id="acct_hol_feed", cursor=during.cursor),
                ["rrev_prefix_b", "rrev_prefix_c"],
            )
            assert ids(rest) == ["rrev_prefix_b", "rrev_prefix_c"]
        finally:
            await pool.putconn(opened)


async def test_the_account_feed_query_is_served_by_the_account_seq_index() -> None:
    from adcp.reporting.ledger.change_feed import _page_query

    async with isolated_reporting_pool() as pool:
        await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id,"
                " obligation_id) SELECT 'acct_filler_' || n, 'revision', 'rrev_filler_' || n,"
                " 'robl_filler' FROM generate_series(1, 2000) AS n"
            )
            await connection.execute("ANALYZE adcp_reporting_changes")
            # The very query the store runs for an account page, planned with its parameters.
            plan = await (
                await connection.execute(
                    "EXPLAIN " + _page_query(account=True),
                    ("acct_hol_feed", 0, None, None, 500),
                )
            ).fetchall()
        assert "adcp_reporting_changes_account" in "\n".join(row[0] for row in plan)
