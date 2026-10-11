"""Whole-period retirement: ordered, resumable, held by unread consumers, tombstoned."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore, ReportingRowStoreError
from adcp.reporting.ledger.change_feed import encode_reporting_change_cursor_v1
from adcp.reporting.ledger.store import LedgerConflictError

from ._generation_support import configuration, isolated_reporting_pool, obligation_for
from .test_reporting_row_retention_pg import age
from .test_reporting_row_storage_object_pg import lease, list_objects, options
from .test_reporting_row_storage_pg import fetch_all, many_rows, seeded, typed_revision


async def count(pool: Any, table: str) -> int:
    return (await fetch_all(pool, f"SELECT count(*) FROM {table}"))[0][0]  # noqa: S608


async def committed_period(store: PgReportingLedgerStore, pool: Any, revisions: int = 2) -> Any:
    obligation = await seeded(store)
    await lease(pool)
    previous = None
    for index in range(revisions):
        rows = many_rows(10 + index)
        revision = typed_revision(obligation, rows, suffix=f"r{index}", supersedes=previous)
        await store.stage_revision_rows(revision, rows)
        await store.commit_revision(revision, rows)
        previous = revision.reporting_revision_id
    return obligation


async def test_an_expired_period_is_retired_in_three_ordered_phases(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path), change_feed=True)
        await store.create_schema()
        await committed_period(store, pool)
        assert len(list_objects(tmp_path / "rows")) == 2

        # Inside the window nothing happens.
        young = await store.retire_expired_periods()
        assert (young.retired, young.protected, young.failed) == (0, (), ())

        await age(pool, days=500)
        result = await store.retire_expired_periods()
        assert (result.retired, result.protected, result.failed) == (1, (), ())

        assert list_objects(tmp_path / "rows") == []
        for table in (
            "reporting_obligations",
            "reporting_revisions",
            "adcp_reporting_row_sets",
            "adcp_reporting_row_chunks",
            "adcp_reporting_chunk_bodies",
        ):
            assert await count(pool, table) == 0, table
        (tombstone,) = await fetch_all(
            pool,
            "SELECT state, final_revision_id, revision_count, adjustment_count,"
            " final_row_manifest_sha256 IS NOT NULL, retired_at IS NOT NULL"
            " FROM adcp_reporting_period_tombstones",
        )
        assert tombstone == ("retired", "rpr_acct_a_r1", 2, 0, True, True)
        kinds = [r.kind for r in (await store.changes_after(account_id="acct_a")).records]
        assert kinds == ["obligation", "revision", "revision", "retirement"]

        # A retired period is gone from reads and is never resurrected by a replay.
        with pytest.raises(LedgerConflictError) as unknown:
            await store.read_revision_rows(
                account_id="acct_a", reporting_revision_id="rpr_acct_a_r1"
            )
        assert unknown.value.code == "REVISION_NOT_FOUND"
        with pytest.raises(LedgerConflictError) as replay:
            await store.commit_obligation(obligation_for(configuration()))
        assert replay.value.code == "PERIOD_RETIRED"
        assert (await store.retire_expired_periods()).retired == 0


async def test_the_window_is_anchored_at_the_newest_record_and_may_be_lengthened(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        await store.commit_revision(typed_revision(obligation, rows, suffix="a"), rows)
        await age(pool, days=500)
        assert (await store.retire_expired_periods(ledger_record_retention_days=3650)).retired == 0
        # A newer revision moves the anchor: the period is not retired until it ages too.
        newest = typed_revision(obligation, rows, suffix="b", supersedes="rpr_acct_a_a")
        await store.commit_revision(newest, rows)
        assert (await store.retire_expired_periods()).retired == 0
        await age(pool, days=500)
        assert (await store.retire_expired_periods()).retired == 1
        for bad in (0, 10_001):
            with pytest.raises(ValueError):
                await store.retire_expired_periods(limit=bad)
        with pytest.raises(ValueError):
            await store.retire_expired_periods(ledger_record_retention_days=0)


async def test_an_unread_feed_consumer_holds_the_period_until_it_passes_or_goes_silent(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True, change_feed=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        await store.commit_revision(typed_revision(obligation, rows, suffix="a"), rows)
        await age(pool, days=500)
        await store.save_feed_consumer_cursor(
            "sink", encode_reporting_change_cursor_v1(account="acct_a", seq="0")
        )
        held = await store.retire_expired_periods()
        assert (held.retired, held.failed) == (0, ())
        assert await count(pool, "reporting_obligations") == 1

        # Silent past max_feed_hold_days: the hold releases and retention proceeds.
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE adcp_reporting_feed_consumers SET updated_at = updated_at - INTERVAL '10 days'"
            )
        assert (await store.retire_expired_periods(max_feed_hold_days=7)).retired == 1

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True, change_feed=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        await store.commit_revision(typed_revision(obligation, rows, suffix="a"), rows)
        await age(pool, days=500)
        everything = await store.changes_after(account_id="acct_a")
        await store.save_feed_consumer_cursor("sink", everything.cursor)
        assert (await store.retire_expired_periods()).retired == 1


async def test_a_failed_external_delete_leaves_the_period_retiring_and_resumes(
    tmp_path: Path,
) -> None:
    async with isolated_reporting_pool() as pool:
        configured = options(tmp_path)
        store = PgReportingLedgerStore(pool=pool, row_storage=configured, change_feed=True)
        await store.create_schema()
        await committed_period(store, pool, revisions=1)
        await age(pool, days=500)

        without = PgReportingLedgerStore(
            pool=pool, row_storage=type(configured)(deployment_namespace="test"), change_feed=True
        )
        stuck = await without.retire_expired_periods()
        assert stuck.retired == 0 and stuck.causes[0][1] == "STATE_UNAVAILABLE"
        assert await fetch_all(pool, "SELECT state FROM adcp_reporting_period_tombstones") == [
            ("retiring",)
        ]
        assert await fetch_all(pool, "SELECT rows_state FROM adcp_reporting_row_sets") == [
            ("pruning",)
        ]
        assert await count(pool, "reporting_obligations") == 1
        assert len(list_objects(tmp_path / "rows")) == 1

        resumed = await store.retire_expired_periods()
        assert resumed.retired == 1
        assert list_objects(tmp_path / "rows") == []
        assert await count(pool, "reporting_obligations") == 0


async def test_a_period_with_protected_records_loses_its_row_bytes_but_keeps_its_records() -> None:
    from tests.test_reporting_settling import _capabilities, _harness, _revisions

    async with isolated_reporting_pool() as pool:
        producer, store, _, _ = await _harness(
            _capabilities(restatement_window="P3D"),
            store_factory=lambda clock: PgReportingLedgerStore(
                pool=pool, clock=clock, row_storage=True, change_feed=True
            ),
        )
        await producer.run_worker()
        (revision,) = await _revisions(store)
        await age(pool, days=500)
        result = await store.retire_expired_periods()
        # Provisional observations are append-only by trigger: all records or none.
        assert result.retired == 0 and result.protected == (revision.reporting_obligation_id,)
        assert await count(pool, "reporting_revisions") == 1
        assert await count(pool, "adcp_reporting_chunk_bodies") == 0
        assert await fetch_all(pool, "SELECT state FROM adcp_reporting_period_tombstones") == [
            ("retiring",)
        ]
        assert await fetch_all(pool, "SELECT rows_state FROM adcp_reporting_row_sets") == [
            ("pruned",)
        ]
        with pytest.raises(LedgerConflictError) as expired:
            await store.read_revision_rows(
                account_id=revision.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
        assert expired.value.code == "REVISION_NOT_FOUND"


async def test_retirement_needs_the_tombstone_table() -> None:
    async with isolated_reporting_pool() as pool:
        plain = PgReportingLedgerStore(pool=pool)
        await plain.create_schema()
        with pytest.raises(ReportingRowStoreError) as failure:
            await plain.retire_expired_periods()
        assert failure.value.code == "STATE_UNAVAILABLE"


async def test_a_retiring_period_refuses_new_revisions(tmp_path: Path) -> None:
    async with isolated_reporting_pool() as pool:
        configured = options(tmp_path)
        store = PgReportingLedgerStore(pool=pool, row_storage=configured, change_feed=True)
        await store.create_schema()
        obligation = await committed_period(store, pool, revisions=1)
        await age(pool, days=500)
        without = PgReportingLedgerStore(
            pool=pool, row_storage=type(configured)(deployment_namespace="test"), change_feed=True
        )
        await without.retire_expired_periods()  # stuck in phase 2: the tombstone is 'retiring'
        late_rows = many_rows(10)
        late = typed_revision(obligation, late_rows, suffix="late")
        with pytest.raises(LedgerConflictError) as refused:
            await store.commit_revision(late, late_rows)
        assert refused.value.code == "PERIOD_RETIRED"


async def test_a_period_that_stopped_qualifying_is_not_retired(tmp_path: Path) -> None:
    from adcp.reporting.ledger.period_retirement import _retire_period
    from adcp.reporting.ledger.row_retention import NoLongerEligibleError

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options(tmp_path), change_feed=True)
        await store.create_schema()
        obligation = await committed_period(store, pool, revisions=1)
        # Young records: a candidate list read earlier is stale by the time the lock is held.
        with pytest.raises(NoLongerEligibleError):
            await _retire_period(
                store,
                obligation.reporting_obligation_id,
                obligation.account_id,
                True,
                (None, 7, ""),
            )
        assert await count(pool, "adcp_reporting_period_tombstones") == 0
        assert await count(pool, "reporting_obligations") == 1


async def test_a_change_feed_host_without_row_storage_retires_periods() -> None:
    """``adcp_reporting_row_sets`` exists only with row storage; retirement must not need it."""
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, change_feed=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="inline")
        await store.commit_revision(revision, rows)
        await age(pool, days=500)

        result = await store.retire_expired_periods()
        assert (result.retired, result.protected, result.failed) == (1, (), ())
        for table in ("reporting_obligations", "reporting_revisions", "reporting_revision_rows"):
            assert await count(pool, table) == 0, table
        assert await fetch_all(
            pool,
            "SELECT state, final_revision_id, final_row_manifest_sha256, revision_count"
            " FROM adcp_reporting_period_tombstones",
        ) == [("retired", revision.reporting_revision_id, None, 1)]
