"""Legacy inline rows migrate to verified chunks, or are quarantined and never rewritten."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting.ledger import (
    PgReportingLedgerStore,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    ReportingRowStoreError,
)
from adcp.reporting.ledger.store import LedgerConflictError

from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from .test_reporting_row_storage_pg import fetch_all, many_rows, typed_revision, walk


async def seed_legacy(
    pool: Any,
) -> tuple[
    PgReportingLedgerStore,
    ReportingObligationRecord,
    list[tuple[ReportingRevisionRecord, list[dict[str, Any]]]],
]:
    """A ledger written by a binary that predates chunked rows."""
    legacy = PgReportingLedgerStore(pool=pool)
    await legacy.create_schema()
    config = configuration()
    await legacy.put_configuration(config)
    obligation = await legacy.commit_obligation(obligation_for(config))
    committed: list[tuple[ReportingRevisionRecord, list[dict[str, Any]]]] = []
    previous: str | None = None
    for suffix, rows in (
        ("a_pairs", None),
        ("b_typed", many_rows(1234)),
        ("c_empty", []),
        ("d_typed", many_rows(7)),
    ):
        if rows is None:
            revision, rows = revision_for(obligation, suffix=suffix)
            revision = replace(revision, supersedes_reporting_revision_id=previous)
        else:
            revision = typed_revision(obligation, rows, suffix=suffix, supersedes=previous)
        await legacy.commit_revision(revision, rows)
        committed.append((revision, rows))
        previous = revision.reporting_revision_id
    return legacy, obligation, committed


async def test_migration_moves_every_revision_into_verified_chunks_in_resumable_batches() -> None:
    async with isolated_reporting_pool() as pool:
        _, obligation, committed = await seed_legacy(pool)
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()

        cursor = None
        batches = []
        while True:
            batch = await store.migrate_inline_rows(limit=3, cursor=cursor)
            batches.append(batch)
            assert batch.quarantined == ()
            cursor = batch.cursor
            if cursor is None:
                break
        assert [(b.examined, b.migrated) for b in batches] == [(3, 3), (1, 1)]

        profiles = dict(
            await fetch_all(pool, "SELECT row_set_id, digest_profile FROM adcp_reporting_row_sets")
        )
        assert profiles == {
            "rpr_acct_a_a_pairs": "rows_v1",
            "rpr_acct_a_b_typed": "revision_envelope_v1",
            "rpr_acct_a_c_empty": "revision_envelope_v1",
            "rpr_acct_a_d_typed": "revision_envelope_v1",
        }
        for revision, rows in committed:
            walked, _ = await walk(
                store, obligation.account_id, revision.reporting_revision_id, 500
            )
            assert walked == rows
            verified = await store.read_verified_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
            assert list(verified) == rows
        # Migration never deletes inline rows; retirement is a separate decision.
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revision_rows") == [
            (1 + 1234 + 0 + 7,)
        ]
        # Idempotent.
        again = await store.migrate_inline_rows(limit=100)
        assert (again.examined, again.migrated, again.cursor) == (0, 0, None)


async def test_a_revision_whose_rows_do_not_reproduce_its_binding_is_quarantined_never_rewritten() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        legacy, obligation, committed = await seed_legacy(pool)
        victim = committed[1][0]
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_revision_rows SET row_payload = jsonb_set(row_payload, '{impressions}',"
                " '99'::jsonb) WHERE reporting_revision_id = %s AND ordinal = 5",
                (victim.reporting_revision_id,),
            )
        before = await fetch_all(
            pool,
            "SELECT reporting_revision_id, ordinal, row_payload::text FROM reporting_revision_rows"
            " ORDER BY 1, 2",
        )
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        batch = await store.migrate_inline_rows()
        assert batch.quarantined == (victim.reporting_revision_id,)
        assert batch.migrated == 3 and batch.cursor is None

        assert await fetch_all(
            pool,
            "SELECT reporting_revision_id, reason FROM adcp_reporting_row_quarantine",
        ) == [(victim.reporting_revision_id, "BINDING_MISMATCH")]
        assert await fetch_all(
            pool,
            "SELECT count(*) FROM adcp_reporting_row_sets WHERE row_set_id = %s",
            (victim.reporting_revision_id,),
        ) == [(0,)]
        assert (
            await fetch_all(
                pool,
                "SELECT reporting_revision_id, ordinal, row_payload::text FROM reporting_revision_rows"
                " ORDER BY 1, 2",
            )
            == before
        )
        # Never served, by either read path, with the operator code.
        for reader in (store.read_revision_rows, store.read_verified_revision_rows):
            with pytest.raises(ReportingRowStoreError) as failure:
                await reader(
                    account_id=obligation.account_id,
                    reporting_revision_id=victim.reporting_revision_id,
                )
            assert failure.value.code == "ROWS_INTEGRITY_FAILED"
        # A later pass does not re-examine it, and its neighbours still read.
        assert (await store.migrate_inline_rows()).examined == 0
        walked, _ = await walk(
            store, obligation.account_id, committed[3][0].reporting_revision_id, 500
        )
        assert walked == committed[3][1]
        # The quarantine record is immutable.
        from psycopg import errors

        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation, match="immutable"):
                await connection.execute(
                    "UPDATE adcp_reporting_row_quarantine SET reason = 'ROWS_NOT_PORTABLE'"
                )


async def test_a_gap_in_the_inline_ordinals_is_quarantined() -> None:
    async with isolated_reporting_pool() as pool:
        _, _, committed = await seed_legacy(pool)
        victim = committed[3][0]
        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM reporting_revision_rows WHERE reporting_revision_id = %s AND ordinal = 3",
                (victim.reporting_revision_id,),
            )
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        batch = await store.migrate_inline_rows()
        assert batch.quarantined == (victim.reporting_revision_id,)
        assert await fetch_all(pool, "SELECT reason FROM adcp_reporting_row_quarantine") == [
            ("ROW_SEQUENCE_INVALID",)
        ]


async def test_a_revision_with_a_gap_is_never_served_as_a_short_page_before_migration() -> None:
    async with isolated_reporting_pool() as pool:
        _, obligation, committed = await seed_legacy(pool)
        victim = committed[3][0]
        async with pool.connection() as connection:
            await connection.execute(
                "DELETE FROM reporting_revision_rows WHERE reporting_revision_id = %s AND ordinal = 3",
                (victim.reporting_revision_id,),
            )
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_revision_rows(
                account_id=obligation.account_id, reporting_revision_id=victim.reporting_revision_id
            )
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"


async def test_retirement_waits_for_every_revision_then_renames_the_legacy_table() -> None:
    async with isolated_reporting_pool() as pool:
        legacy, obligation, committed = await seed_legacy(pool)
        victim = committed[1][0]
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_revision_rows SET row_payload = '{}'::jsonb"
                " WHERE reporting_revision_id = %s AND ordinal = 0",
                (victim.reporting_revision_id,),
            )
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()

        with pytest.raises(LedgerConflictError) as early:
            await store.retire_legacy_inline_rows()
        assert early.value.code == "ROWS_MIGRATION_INCOMPLETE"

        await store.migrate_inline_rows()
        assert await store.retire_legacy_inline_rows() is True
        assert await store.retire_legacy_inline_rows() is False

        tables = {
            row[0]
            for row in await fetch_all(
                pool,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()",
            )
        }
        assert "reporting_revision_rows" not in tables
        assert "adcp_reporting_legacy_revision_rows_v0" in tables
        # Nothing was truncated: the archive still holds every inline row, quarantined ones included.
        assert await fetch_all(
            pool, "SELECT count(*) FROM adcp_reporting_legacy_revision_rows_v0"
        ) == [(1 + 1234 + 0 + 7,)]

        # A binary that predates chunked rows fails closed instead of writing rows nobody reads.
        from psycopg import errors

        async with pool.connection() as connection:
            with pytest.raises(errors.UndefinedTable):
                await connection.execute(
                    "INSERT INTO reporting_revision_rows VALUES ('x', 0, '{}'::jsonb)"
                )
        async with pool.connection() as connection:
            with pytest.raises(errors.UndefinedTable):
                await connection.execute("SELECT * FROM reporting_revision_rows")

        # The new reader keeps serving migrated revisions, refuses the quarantined one,
        # and a later boot does not resurrect the retired table.
        await PgReportingLedgerStore(pool=pool, row_storage=True).create_schema()
        assert "reporting_revision_rows" not in {
            row[0]
            for row in await fetch_all(
                pool,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()",
            )
        }
        walked, _ = await walk(
            store, obligation.account_id, committed[3][0].reporting_revision_id, 500
        )
        assert walked == committed[3][1]
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_revision_rows(
                account_id=obligation.account_id, reporting_revision_id=victim.reporting_revision_id
            )
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"
        # New revisions keep committing as chunks.
        fresh_rows = many_rows(4)
        fresh = typed_revision(
            obligation,
            fresh_rows,
            suffix="e_fresh",
            supersedes=committed[3][0].reporting_revision_id,
        )
        await store.commit_revision(fresh, fresh_rows)
        walked, _ = await walk(store, obligation.account_id, fresh.reporting_revision_id, 500)
        assert walked == fresh_rows


async def test_a_revision_with_no_chunks_after_retirement_is_unavailable_not_empty() -> None:
    async with isolated_reporting_pool() as pool:
        _, obligation, committed = await seed_legacy(pool)
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        await store.migrate_inline_rows()
        await store.retire_legacy_inline_rows()
        async with pool.connection() as connection:
            for table in ("adcp_reporting_row_chunks", "adcp_reporting_row_sets"):
                await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")  # noqa: S608
            await connection.execute("DELETE FROM adcp_reporting_row_chunks")
            await connection.execute("DELETE FROM adcp_reporting_row_sets")
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=committed[1][0].reporting_revision_id,
            )
        assert failure.value.code == "ROWS_UNAVAILABLE"


async def test_migration_requires_row_storage_and_a_valid_batch_size() -> None:
    async with isolated_reporting_pool() as pool:
        plain = PgReportingLedgerStore(pool=pool)
        await plain.create_schema()
        with pytest.raises(ReportingRowStoreError):
            await plain.migrate_inline_rows()
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        for bad in (0, 10_001):
            with pytest.raises(ValueError):
                await store.migrate_inline_rows(limit=bad)
        with pytest.raises(ValueError):
            await store.migrate_inline_rows(cursor="not-a-cursor")


async def test_the_installed_schema_stays_ready_after_the_legacy_table_is_retired() -> None:
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox._schema import validate_schema

    async with isolated_reporting_pool() as pool:
        await seed_legacy(pool)
        store = PgReportingLedgerStore(pool=pool, row_storage=True, notifications=True)
        await store.create_schema()
        await store.migrate_inline_rows()
        async with pool.connection() as connection:
            await validate_schema(connection)
        await store.retire_legacy_inline_rows()
        async with pool.connection() as connection:
            await validate_schema(connection)
        # A table that is merely missing, not retired, is still refused.
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE adcp_reporting_legacy_revision_rows_v0 RENAME TO somewhere_else"
            )
            with pytest.raises(ReportingNotificationError):
                await validate_schema(connection)
