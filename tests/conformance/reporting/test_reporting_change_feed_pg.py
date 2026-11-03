"""The shared host change feed: ordering, visibility, consumers, pruning and cursor expiry."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from adcp_reporting_storage.ledger.change_feed import (
    ReportingChangeCursorExpiredError,
    decode_reporting_change_cursor_v1,
    encode_reporting_change_cursor_v1,
    is_legacy_account_change_cursor,
)

from ._generation_support import configuration, isolated_reporting_pool, obligation_for
from .test_reporting_row_storage_pg import fetch_all, many_rows, typed_revision


def b64(body: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode()


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
    # A seq-only account cursor is what an older SDK saved; it is still accepted.
    js_account = (
        base64.urlsafe_b64encode(b'{"v":1,"account":"acct_a","seq":"9"}').rstrip(b"=").decode()
    )
    assert encode_reporting_change_cursor_v1(account="acct_a", seq="9") == js_account
    assert is_legacy_account_change_cursor(decode_reporting_change_cursor_v1(js_account, "acct_a"))
    # The current account cursor carries the xid of the last change, as the JavaScript SDK writes it.
    js_account_xid = (
        base64.urlsafe_b64encode(b'{"v":1,"account":"acct_a","seq":"9","xid":"42"}')
        .rstrip(b"=")
        .decode()
    )
    assert encode_reporting_change_cursor_v1(account="acct_a", seq="9", xid="42") == js_account_xid
    decoded_account = decode_reporting_change_cursor_v1(js_account_xid, "acct_a")
    assert decoded_account["xid"] == "42"
    assert not is_legacy_account_change_cursor(decoded_account)
    for bad, account in (
        ("not base64!", None),
        (js_account, "acct_b"),
        (js_account, None),
        (js_deployment, "acct_a"),
        (base64.urlsafe_b64encode(b'{"v":2,"account":null,"seq":"1","xid":"1"}').decode(), None),
        (base64.urlsafe_b64encode(b'{"v":1,"account":null,"seq":"x","xid":"1"}').decode(), None),
        # A deployment cursor always needs an xid; values the database cast rejects fail here.
        (base64.urlsafe_b64encode(b'{"v":1,"account":null,"seq":"1"}').decode(), None),
        (b64({"v": 1, "account": "a", "seq": "9223372036854775808"}), "a"),
        (b64({"v": 1, "account": "a", "seq": "1", "xid": "18446744073709551616"}), "a"),
        (b64({"v": 1, "account": None, "seq": "1", "xid": "99999999999999999999"}), None),
        (b64({"v": 1, "account": "a", "seq": ""}), "a"),
        (b64({"v": 1, "account": "a", "seq": "1", "xid": "x"}), "a"),
        (b64({"v": 1, "account": "a", "seq": "1", "xid": None}), "a"),
        (b64({"v": 1, "account": "a", "seq": "\u0663"}), "a"),  # a non-ASCII digit
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
            ("acct_a", encode_reporting_change_cursor_v1(account="acct_a", seq="94", xid=xid)),
            # A seq-only account cursor maps to a position below every later change.
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


# -- account feed ordering by (xid, seq), ported from the JavaScript SDK (adcp-client #3289) ----

ACCOUNT = "acct_hol_feed"
INSERT_CHANGE = (
    "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id, obligation_id)"
    " VALUES (%s, 'revision', %s, 'robl_hol_feed')"
)


def ids(page: Any) -> list[str]:
    return [record.record_id for record in page.records]


async def committed(pool: Any, record_id: str, account: str = ACCOUNT) -> None:
    async with pool.connection() as connection:
        await connection.execute(INSERT_CHANGE, (account, record_id))


async def insert_at(
    pool: Any, account: str, record_id: str, xid: int, *, age_days: int = 0
) -> None:
    """A committed change with a chosen (historical, so always below the snapshot's xmin)
    transaction id, and optionally an old ``recorded_at``, for ordering and pruning cases that
    need exact values."""
    async with pool.connection() as connection:
        await connection.execute(
            "INSERT INTO adcp_reporting_changes (xid, account_id, record_kind, record_id,"
            " obligation_id, recorded_at) VALUES (%s::text::xid8, %s, 'revision', %s,"
            " 'robl_hol_feed', clock_timestamp() - (%s::integer * INTERVAL '1 day'))",
            (str(xid), account, record_id, age_days),
        )


async def seq_of(pool: Any, record_id: str) -> str:
    rows = await fetch_all(
        pool, "SELECT seq::text FROM adcp_reporting_changes WHERE record_id = %s", (record_id,)
    )
    return str(rows[0][0])


async def remaining(pool: Any, account: str) -> list[str]:
    rows = await fetch_all(
        pool,
        "SELECT record_id FROM adcp_reporting_changes WHERE account_id = %s ORDER BY seq",
        (account,),
    )
    return [row[0] for row in rows]


def legacy_cursor(account: str, seq: str) -> str:
    """The cursor an older SDK saved: a ``seq`` and no ``xid``."""
    return encode_reporting_change_cursor_v1(account=account, seq=seq)


async def until(read: Any, expected: list[str]) -> Any:
    """Other tests share the cluster, so an unrelated older transaction can briefly hold a page
    back: positive reads poll. The assertions that a page is held back do not need it."""
    for _ in range(200):
        page = await read()
        if ids(page) == expected:
            return page
        await asyncio.sleep(0.05)
    return page


async def test_a_lower_seq_that_commits_after_a_higher_one_is_never_passed_over() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id=ACCOUNT)
        slow = await pool.getconn()
        try:
            await slow.execute(INSERT_CHANGE, (ACCOUNT, "rrev_low"))  # the lower seq, commits last
            await committed(pool, "rrev_high")  # a higher seq, committed already
            during = await store.changes_after(account_id=ACCOUNT, cursor=start.cursor)
            assert ids(during) == [], "the page stops before the unfinished transaction"
            assert during.cursor == start.cursor, "the cursor does not move past it"
            await slow.commit()
        finally:
            await pool.putconn(slow)
        after = await until(
            lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
            ["rrev_low", "rrev_high"],
        )
        assert ids(after) == ["rrev_low", "rrev_high"]


async def test_a_lower_xid_with_a_higher_seq_is_returned_first_and_the_held_back_one_after_it() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id=ACCOUNT)
        early = await pool.getconn()
        blocker = await pool.getconn()
        try:
            # xid order: early < blocker < the change inserted next.
            await early.execute("SELECT pg_current_xact_id()")
            await blocker.execute("SELECT pg_current_xact_id()")
            await committed(pool, "rrev_first")  # lower seq, but a transaction after `blocker`
            await early.execute(
                INSERT_CHANGE, (ACCOUNT, "rrev_second")
            )  # higher seq, xid < blocker
            await early.commit()
            # `blocker` is the oldest transaction in flight, so rrev_first (a later xid) is held
            # back while rrev_second sits below it. The feed orders by (xid, seq), so rrev_second
            # comes first and the cursor stops at it, before rrev_first.
            during = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
                ["rrev_second"],
            )
            assert ids(during) == ["rrev_second"]
            assert during.cursor != start.cursor
            await blocker.commit()
            rest = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=during.cursor),
                ["rrev_first"],
            )
            assert ids(rest) == ["rrev_first"], "the held-back change is delivered once, after it"
        finally:
            await pool.putconn(early)
            await pool.putconn(blocker)
        everything = await until(
            lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
            ["rrev_second", "rrev_first"],
        )
        assert ids(everything) == ["rrev_second", "rrev_first"], "a fresh reader sees the order"


async def test_a_lower_seq_with_a_higher_xid_that_commits_late_is_delivered_after_the_cursor() -> (
    None
):
    """No account lock is taken anywhere here: two writers under different lock keys."""
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id=ACCOUNT)
        quick = await pool.getconn()
        slow = await pool.getconn()
        try:
            await quick.execute("SELECT pg_current_xact_id()")  # xid Q
            await slow.execute("SELECT pg_current_xact_id()")  # xid S > Q
            await slow.execute(INSERT_CHANGE, (ACCOUNT, "rrev_late"))  # lower seq, higher xid
            await quick.execute(INSERT_CHANGE, (ACCOUNT, "rrev_quick"))  # higher seq, lower xid
            await quick.commit()
            during = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
                ["rrev_quick"],
            )
            assert ids(during) == ["rrev_quick"], "the late transaction is still open"
            await slow.commit()
            rest = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=during.cursor),
                ["rrev_late"],
            )
            assert ids(rest) == ["rrev_late"]
            assert (
                ids(await store.changes_after(account_id=ACCOUNT, cursor=rest.cursor)) == []
            ), "and nothing is delivered twice"
        finally:
            await pool.putconn(quick)
            await pool.putconn(slow)


async def test_an_open_transaction_on_another_account_holds_a_later_change_back() -> None:
    other = "acct_hol_feed_other"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id=ACCOUNT)
        opened = await pool.getconn()
        try:
            await opened.execute(INSERT_CHANGE, (other, "rrev_other_inflight"))  # in flight
            await committed(pool, "rrev_after_other")
            # xmin is cluster-wide, so a later xid is held back whichever account the open one wrote.
            assert ids(await store.changes_after(account_id=ACCOUNT, cursor=start.cursor)) == []
            await opened.commit()
        finally:
            await pool.putconn(opened)
        after = await until(
            lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
            ["rrev_after_other"],
        )
        assert ids(after) == ["rrev_after_other"]


async def test_a_committed_prefix_is_returned_and_the_cursor_stops_before_the_first_in_flight() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        start = await store.changes_after(account_id=ACCOUNT)
        await committed(pool, "rrev_prefix_a")
        opened = await pool.getconn()
        try:
            await opened.execute(INSERT_CHANGE, (ACCOUNT, "rrev_prefix_b"))  # in flight
            await committed(pool, "rrev_prefix_c")  # committed after it
            during = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=start.cursor),
                ["rrev_prefix_a"],
            )
            assert ids(during) == ["rrev_prefix_a"], "c is not handed out past b"
            await opened.commit()
            rest = await until(
                lambda: store.changes_after(account_id=ACCOUNT, cursor=during.cursor),
                ["rrev_prefix_b", "rrev_prefix_c"],
            )
            assert ids(rest) == ["rrev_prefix_b", "rrev_prefix_c"]
        finally:
            await pool.putconn(opened)


async def test_the_account_cursor_carries_the_xid_of_the_last_change_and_is_bound_to_its_account() -> (
    None
):
    scoped = "acct_hol_feed_cursor"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "rrev_cursor_a", 20)
        page = await store.changes_after(account_id=scoped)
        decoded = decode_reporting_change_cursor_v1(page.cursor, scoped)
        assert decoded["xid"] == "20"
        assert not is_legacy_account_change_cursor(decoded)
        with pytest.raises(ValueError):
            decode_reporting_change_cursor_v1(page.cursor, "acct_hol_feed_elsewhere")
        with pytest.raises(ValueError, match="does not belong to this feed"):
            await store.changes_after(cursor=page.cursor)


async def test_cursor_values_the_database_would_reject_fail_as_value_error() -> None:
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        for body, account in (
            ({"v": 1, "account": "a", "seq": "9223372036854775808"}, "a"),
            ({"v": 1, "account": "a", "seq": "1", "xid": "18446744073709551616"}, "a"),
            ({"v": 1, "account": None, "seq": "1", "xid": "99999999999999999999"}, None),
            ({"v": 1, "account": None, "seq": "1"}, None),
            ({"v": 1, "account": "a", "seq": ""}, "a"),
            ({"v": 1, "account": "a", "seq": "1", "xid": "x"}, "a"),
        ):
            with pytest.raises(ValueError):
                await store.changes_after(account_id=account, cursor=b64(body))
        decode_reporting_change_cursor_v1(
            b64(
                {
                    "v": 1,
                    "account": "a",
                    "seq": "9223372036854775807",
                    "xid": "18446744073709551615",
                }
            ),
            "a",
        )


# -- account cursors issued before the (xid, seq) order --------------------------------------


async def test_a_seq_only_cursor_maps_to_a_position_that_skips_nothing_and_upgrades() -> None:
    scoped = "acct_hol_legacy_map"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        # r2 took a higher seq but a lower xid than r1: the late commit the old order could skip.
        await insert_at(pool, scoped, "r0", 5)  # read long ago, older than any unread change
        await insert_at(pool, scoped, "r1", 20)
        await insert_at(pool, scoped, "r2", 10)
        await insert_at(pool, scoped, "r3", 30)
        page = await store.changes_after(
            account_id=scoped, cursor=legacy_cursor(scoped, await seq_of(pool, "r1"))
        )
        # r2 is delivered; r1 is redelivered because its xid is above r2's; r0 is not, so the
        # position is not simply xid 0.
        assert ids(page) == ["r2", "r1", "r3"]
        upgraded = decode_reporting_change_cursor_v1(page.cursor, scoped)
        assert upgraded["xid"] == "30"
        assert ids(await store.changes_after(account_id=scoped, cursor=page.cursor)) == []

        # A seq-only cursor at the end of the feed has nothing unread: it maps to the snapshot's
        # xmin, and an empty page still upgrades the cursor.
        end = await store.changes_after(
            account_id=scoped, cursor=legacy_cursor(scoped, await seq_of(pool, "r3"))
        )
        assert ids(end) == []
        mapped = decode_reporting_change_cursor_v1(end.cursor, scoped)
        assert mapped["seq"] == await seq_of(pool, "r3")
        assert int(mapped["xid"]) > 30
        assert not is_legacy_account_change_cursor(mapped)


async def test_a_caught_up_seq_only_cursor_does_not_skip_a_change_from_an_in_flight_transaction() -> (
    None
):
    scoped = "acct_hol_legacy_inflight"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "caught_up", 10)
        head = await seq_of(pool, "caught_up")
        opened = await pool.getconn()
        try:
            await opened.execute(INSERT_CHANGE, (scoped, "inflight"))  # at or above xmin
            during = await store.changes_after(
                account_id=scoped, cursor=legacy_cursor(scoped, head)
            )
            assert ids(during) == []
            await opened.commit()
            rest = await until(
                lambda: store.changes_after(account_id=scoped, cursor=during.cursor), ["inflight"]
            )
            assert ids(rest) == ["inflight"], "the mapped position is below the open transaction"
        finally:
            await pool.putconn(opened)


async def test_a_seq_only_cursor_expires_when_any_change_with_a_higher_seq_was_pruned() -> None:
    scoped = "acct_hol_legacy_pruned"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        # Pruned: (xid 20, seq s1) and (xid 10, seq s2). The highest (xid, seq) is s1's, but s2 > s1.
        await insert_at(pool, scoped, "p1", 20, age_days=40)
        await insert_at(pool, scoped, "p2", 10, age_days=40)
        await insert_at(pool, scoped, "p3", 30)
        s1, s2 = await seq_of(pool, "p1"), await seq_of(pool, "p2")
        assert await store.prune_changes(change_retention_days=30) >= 2

        async def expired(cursor: str) -> bool:
            try:
                await store.changes_after(account_id=scoped, cursor=cursor)
            except ReportingChangeCursorExpiredError as error:
                assert error.code == "CURSOR_EXPIRED"
                return True
            return False

        # p2 was pruned after seq s1: a tuple compare would miss it.
        assert await expired(legacy_cursor(scoped, s1)) is True
        assert await expired(legacy_cursor(scoped, "0")) is True
        assert await expired(legacy_cursor(scoped, s2)) is False
        # A cursor at the horizon has passed everything pruned; one below it in (xid, seq) order
        # may have missed a pruned change.
        at_horizon = encode_reporting_change_cursor_v1(account=scoped, seq=s1, xid="20")
        assert await expired(at_horizon) is False
        below = encode_reporting_change_cursor_v1(account=scoped, seq=s2, xid="10")
        assert await expired(below) is True
        assert ids(await store.changes_after(account_id=scoped)) == ["p3"], "no cursor: horizon"


async def test_a_seq_only_consumer_registration_holds_by_seq_until_it_saves_a_new_cursor() -> None:
    scoped = "acct_hol_legacy_consumer"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "c1", 10, age_days=40)
        await insert_at(pool, scoped, "c2", 20, age_days=40)
        await insert_at(pool, scoped, "c3", 30, age_days=40)
        s1 = await seq_of(pool, "c1")
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_feed_consumers"
                " (consumer_name, account_id, cursor_xid, cursor_seq)"
                " VALUES ('legacy-sink', %s, NULL, %s::bigint)",
                (scoped, s1),
            )

        async def registered() -> tuple[Any, ...]:
            return (
                await fetch_all(
                    pool,
                    "SELECT cursor_xid::text, cursor_seq::text FROM adcp_reporting_feed_consumers"
                    " WHERE consumer_name = 'legacy-sink'",
                )
            )[0]

        await store.prune_changes(change_retention_days=30)
        assert await remaining(pool, scoped) == ["c2", "c3"], "c1 was passed; c2 and c3 are held"
        # Saving a seq-only cursor registers a mapped position, below every unread change.
        assert await store.save_feed_consumer_cursor("legacy-sink", legacy_cursor(scoped, s1))
        assert (await registered())[0] == "20", "the oldest unread change is c2, at xid 20"
        # A cursor in the new format replaces it, forward only.
        page = await store.changes_after(account_id=scoped, cursor=legacy_cursor(scoped, s1))
        assert ids(page) == ["c2", "c3"]
        assert await store.save_feed_consumer_cursor("legacy-sink", page.cursor) is True
        assert await registered() == ("30", await seq_of(pool, "c3"))


# -- pruning in (xid, seq) order -------------------------------------------------------------


async def test_an_account_consumer_holds_every_change_after_its_cursor_in_xid_seq_order() -> None:
    scoped = "acct_hol_prune_order"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "o1", 10, age_days=40)
        await insert_at(pool, scoped, "o2", 30, age_days=40)
        await insert_at(pool, scoped, "o3", 20, age_days=40)  # higher seq than o2, lower xid
        o1, o3 = await seq_of(pool, "o1"), await seq_of(pool, "o3")
        cursor = encode_reporting_change_cursor_v1(account=scoped, seq=o3, xid="20")
        await store.save_feed_consumer_cursor("prune-order", cursor)
        await store.prune_changes(change_retention_days=30)
        assert await remaining(pool, scoped) == ["o2"], "o2 follows the cursor in xid order"
        horizon = await fetch_all(
            pool,
            "SELECT pruned_through_xid::text, pruned_through_xid_seq::text,"
            " pruned_through_seq::text FROM adcp_reporting_change_account_horizon"
            " WHERE account_id = %s",
            (scoped,),
        )
        assert horizon == [("20", o3, o3)]
        # The consumer reads on from where it was and is not told it missed anything.
        assert ids(await store.changes_after(account_id=scoped, cursor=cursor)) == ["o2"]
        # A consumer that had only passed o1 has missed o3 (pruned): expired.
        behind = encode_reporting_change_cursor_v1(account=scoped, seq=o1, xid="10")
        with pytest.raises(ReportingChangeCursorExpiredError):
            await store.changes_after(account_id=scoped, cursor=behind)


async def test_the_horizon_only_moves_forward_and_another_accounts_consumer_holds_nothing() -> None:
    scoped = "acct_hol_prune_forward"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "f1", 50, age_days=40)
        await store.save_feed_consumer_cursor(
            "prune-elsewhere",
            encode_reporting_change_cursor_v1(account="acct_hol_prune_elsewhere", seq="1", xid="1"),
        )
        await store.prune_changes(change_retention_days=30)
        assert await remaining(pool, scoped) == []

        async def horizon_xid() -> str:
            rows = await fetch_all(
                pool,
                "SELECT pruned_through_xid::text FROM adcp_reporting_change_account_horizon"
                " WHERE account_id = %s",
                (scoped,),
            )
            return str(rows[0][0])

        assert await horizon_xid() == "50"
        await insert_at(pool, scoped, "f2", 40, age_days=40)  # lower xid than the horizon
        await store.prune_changes(change_retention_days=30)
        assert await horizon_xid() == "50", "a lower (xid, seq) never lowers the horizon"


async def test_a_pruning_batch_cut_by_its_limit_leaves_nothing_retained_below_the_horizon() -> None:
    scoped = "acct_hol_prune_batch"
    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        await insert_at(pool, scoped, "a", 20, age_days=40)
        await insert_at(pool, scoped, "b", 30, age_days=40)
        await insert_at(pool, scoped, "c", 10, age_days=40)  # the highest seq, the lowest xid
        await store.prune_changes(change_retention_days=30, limit=2)
        kept = await remaining(pool, scoped)
        assert kept == ["b"], "victims are taken in (xid, seq) order: c then a"
        assert (
            ids(await store.changes_after(account_id=scoped)) == kept
        ), "a cold start loses nothing"


async def test_period_retirement_holds_a_period_in_xid_seq_order_for_account_consumers() -> None:
    from adcp_reporting_storage.ledger.change_feed import feed_consumer_behind_change

    async with isolated_reporting_pool() as pool:
        store = await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (xid, account_id, record_kind, record_id,"
                " obligation_id) VALUES ('30'::xid8, 'acct_a', 'revision', 'k1', 'o'),"
                " ('20'::xid8, 'acct_a', 'revision', 'k2', 'o')"
            )
        k1, k2 = await seq_of(pool, "k1"), await seq_of(pool, "k2")
        sql = (
            "SELECT change.record_id FROM adcp_reporting_changes change"
            " JOIN adcp_reporting_feed_consumers consumer ON TRUE WHERE "
            + feed_consumer_behind_change("consumer", "change")
            + " ORDER BY change.xid, change.seq"
        )
        # The consumer passed (xid 20, seq k2): k1 (xid 30, a lower seq) is still ahead of it.
        await store.save_feed_consumer_cursor(
            "hold", encode_reporting_change_cursor_v1(account="acct_a", seq=k2, xid="20")
        )
        assert [row[0] for row in await fetch_all(pool, sql)] == ["k1"]
        # A seq-only registration holds by seq alone.
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_feed_consumers SET cursor_xid = NULL, cursor_seq = %s::bigint",
                (k1,),
            )
        assert [row[0] for row in await fetch_all(pool, sql)] == ["k2"]


# -- the migration -----------------------------------------------------------------------------

#: The change tables as the previous release created them.
PREVIOUS_CHANGES_DDL = """
CREATE TABLE adcp_reporting_changes (
  seq BIGSERIAL PRIMARY KEY, xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
  account_id TEXT NOT NULL,
  record_kind TEXT NOT NULL
    CHECK (record_kind IN ('obligation', 'revision', 'adjustment', 'retirement')),
  record_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
CREATE INDEX adcp_reporting_changes_account ON adcp_reporting_changes (account_id, seq);
CREATE TABLE adcp_reporting_change_account_horizon (
  account_id TEXT PRIMARY KEY, pruned_through_seq BIGINT NOT NULL);
INSERT INTO adcp_reporting_change_account_horizon VALUES ('acct_upgrade', 77);
CREATE TABLE adcp_reporting_feed_consumers (
  consumer_name TEXT PRIMARY KEY CHECK (consumer_name ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  account_id TEXT, cursor_xid xid8, cursor_seq BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK ((account_id IS NULL) = (cursor_xid IS NOT NULL)));
INSERT INTO adcp_reporting_feed_consumers (consumer_name, account_id, cursor_xid, cursor_seq)
  VALUES ('old-account-sink', 'acct_upgrade', NULL, 5);
"""


async def test_concurrent_appliers_of_the_changes_migration_all_succeed() -> None:
    from adcp_reporting_storage.ledger.change_feed import REPORTING_CHANGES_DDL_PATH

    async with isolated_reporting_pool() as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()  # the ledger the index needs
        ddl = REPORTING_CHANGES_DDL_PATH.read_text()

        async def apply() -> None:
            async with pool.connection() as connection:
                await connection.execute(ddl)

        results = await asyncio.gather(*(apply() for _ in range(8)), return_exceptions=True)
        assert [r for r in results if isinstance(r, BaseException)] == []


async def test_reapplying_the_changes_migration_upgrades_an_older_install_in_place() -> None:
    from adcp_reporting_storage.ledger.change_feed import REPORTING_CHANGES_DDL_PATH
    from psycopg.errors import CheckViolation

    async with isolated_reporting_pool() as pool:
        await PgReportingLedgerStore(pool=pool).create_schema()
        async with pool.connection() as connection:
            await connection.execute(PREVIOUS_CHANGES_DDL)
        with pytest.raises(CheckViolation):
            async with pool.connection() as connection:
                await connection.execute(
                    "INSERT INTO adcp_reporting_feed_consumers"
                    " (consumer_name, account_id, cursor_xid, cursor_seq)"
                    " VALUES ('old-deployment-sink', NULL, NULL, 5)"
                )
        ddl = REPORTING_CHANGES_DDL_PATH.read_text()
        for _ in range(2):  # idempotent
            async with pool.connection() as connection:
                await connection.execute(ddl)

        assert await fetch_all(
            pool,
            "SELECT account_id, pruned_through_seq::text, pruned_through_xid::text,"
            " pruned_through_xid_seq::text FROM adcp_reporting_change_account_horizon",
        ) == [("acct_upgrade", "77", "0", "0")], "the existing seq horizon is kept"
        assert await fetch_all(
            pool,
            "SELECT consumer_name, cursor_xid::text, cursor_seq::text FROM"
            " adcp_reporting_feed_consumers",
        ) == [("old-account-sink", None, "5")], "a seq-only account consumer is kept"
        # The deployment feed still needs an xid; the old check is gone, the new one stays.
        with pytest.raises(CheckViolation):
            async with pool.connection() as connection:
                await connection.execute(
                    "INSERT INTO adcp_reporting_feed_consumers"
                    " (consumer_name, account_id, cursor_xid, cursor_seq)"
                    " VALUES ('bad-deployment', NULL, NULL, 1)"
                )
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_feed_consumers"
                " (consumer_name, account_id, cursor_xid, cursor_seq)"
                " VALUES ('new-account-sink', 'acct_upgrade', '9'::xid8, 1)"
            )
        checks = await fetch_all(
            pool,
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'adcp_reporting_feed_consumers'::regclass AND contype = 'c'"
            " ORDER BY conname",
        )
        assert [name for name, _ in checks if "account_id IS NULL) =" in _] == []
        assert [name for name, _ in checks].count("adcp_reporting_feed_consumers_cursor_scope") == 1
        indexes = {
            row[0]
            for row in await fetch_all(
                pool,
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                " AND tablename = 'adcp_reporting_changes'",
            )
        }
        assert "adcp_reporting_changes_account_xid" in indexes
        assert "adcp_reporting_changes_account" in indexes, "re-applying never drops an index"


async def test_the_account_feed_query_is_served_by_the_account_xid_seq_index_in_order() -> None:
    from adcp_reporting_storage.ledger.change_feed import _page_query

    # Autocommit: rows written inside one open transaction carry that transaction's own xid, at or
    # above the snapshot's xmin, and the planner would see a histogram nothing satisfies.
    async with isolated_reporting_pool(autocommit=True) as pool:
        await feed_store(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id,"
                " obligation_id) SELECT 'acct_filler_' || (n % 2000), 'revision',"
                " 'rrev_filler_' || n, 'robl_filler' FROM generate_series(1, 100000) AS n"
            )
            # Enough history for the account that a page of 500 is a small slice of it.
            await connection.execute(
                "INSERT INTO adcp_reporting_changes (account_id, record_kind, record_id,"
                " obligation_id) SELECT %s, 'revision', 'rrev_history_' || n, 'robl_history'"
                " FROM generate_series(1, 3000) AS n",
                (ACCOUNT,),
            )
            await connection.execute("ANALYZE adcp_reporting_changes")
            # The very query the store runs for an account page, planned with its parameters.
            plan = "\n".join(
                row[0]
                for row in await (
                    await connection.execute(
                        "EXPLAIN " + _page_query(account=True),
                        (ACCOUNT, "0", 0, None, None, 500),
                    )
                ).fetchall()
            )
        assert "adcp_reporting_changes_account_xid" in plan, plan
        assert "Sort" not in plan, "the index already yields (xid, seq) order"
