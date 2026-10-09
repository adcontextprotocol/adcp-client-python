"""Chunked row storage on PostgreSQL: verified reads, fail-closed integrity, dual-path reads.

Rows live as canonical JSONL chunks in the shared ``adcp_reporting_row_*`` tables.
Every read is verified against digests committed in PostgreSQL before a row is
released; legacy inline rows keep reading through the old table.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from adcp.reporting.ledger import (
    PgReportingLedgerStore,
    ReportingControlTotalRecord,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    revision_content_sha256,
)
from adcp.reporting.ledger.row_storage import (
    REPORTING_ROW_STORAGE_DDL_PATH,
    ReportingRowStorageOptionsV1,
    ReportingRowStoreError,
)
from adcp.reporting.ledger.store import LedgerConflictError

from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)


def typed_revision(
    obligation: ReportingObligationRecord,
    rows: list[dict[str, Any]],
    *,
    suffix: str,
    supersedes: str | None = None,
) -> ReportingRevisionRecord:
    total = str(sum(int(row["impressions"]) for row in rows))
    evidence = (ReportingControlTotalRecord("impressions", total, "integer"),)
    revision_id = f"rpr_{obligation.account_id}_{suffix}"
    return ReportingRevisionRecord(
        reporting_revision_id=revision_id,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality="snapshot",
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=len(rows),
            control_totals=(("impressions", total),),
            reporting_rows=rows,
            control_total_evidence=evidence,
        ),
        row_count=len(rows),
        control_totals=(("impressions", total),),
        managed_control_totals=evidence,
        observed_at=obligation.period.end,
        data_through=obligation.period.end,
        created_at=obligation.period.end,
        supersedes_reporting_revision_id=supersedes,
    )


def many_rows(count: int, media_buy_id: str = "mb_acct_a") -> list[dict[str, Any]]:
    return [
        {"media_buy_id": media_buy_id, "day": index, "impressions": index % 7 + 1}
        for index in range(count)
    ]


async def walk(
    store: PgReportingLedgerStore, account_id: str, revision_id: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    while True:
        page = await store.read_revision_rows(
            account_id=account_id, reporting_revision_id=revision_id, cursor=cursor, limit=limit
        )
        pages += 1
        rows.extend(page.rows)
        if not page.has_more:
            return rows, pages
        cursor = page.cursor


async def seeded(store: PgReportingLedgerStore) -> ReportingObligationRecord:
    config = configuration()
    await store.put_configuration(config)
    return await store.commit_obligation(obligation_for(config))


async def fetch_all(
    pool: Any, query: str, params: tuple[Any, ...] | None = None
) -> list[tuple[Any, ...]]:
    async with pool.connection() as connection:
        return list(await (await connection.execute(query, params)).fetchall())


async def test_create_schema_installs_row_storage_idempotently() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        first = await fetch_all(
            pool, "SELECT installation_id::text FROM adcp_persistence_installation"
        )
        await store.create_schema()
        again = PgReportingLedgerStore(pool=pool, row_storage=True)
        await again.create_schema()
        assert (
            await fetch_all(pool, "SELECT installation_id::text FROM adcp_persistence_installation")
            == first
        )
        bindings = await fetch_all(
            pool, "SELECT row_binding_id, kind, provider, state FROM adcp_reporting_row_bindings"
        )
        assert bindings == [("postgres", "postgres", "postgres", "active")]


async def test_a_store_without_the_option_never_creates_the_tables() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        assert await fetch_all(
            pool,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = current_schema()"
            " AND table_name LIKE 'adcp\\_reporting\\_row\\_%'",
        ) == [(0,)]
        obligation = await seeded(store)
        revision, rows = revision_for(obligation)
        await store.commit_revision(revision, rows)
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revision_rows") == [(1,)]


async def test_chunks_replace_inline_rows_and_serve_verified_pages() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(1234)
        revision = typed_revision(obligation, rows, suffix="big")
        await store.commit_revision(revision, rows)

        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revision_rows") == [(0,)]
        (row_set,) = await fetch_all(
            pool,
            "SELECT digest_profile, content_sha256, row_count, chunk_count, rows_state,"
            " row_binding_id FROM adcp_reporting_row_sets",
        )
        assert row_set == (
            "revision_envelope_v1",
            revision.revision_content_sha256,
            1234,
            1,
            "live",
            "postgres",
        )
        (chunk,) = await fetch_all(
            pool, "SELECT row_count, jsonb_array_length(segments) FROM adcp_reporting_row_chunks"
        )
        assert chunk == (1234, 3)

        for limit in (1, 100, 499, 500):
            walked, _ = await walk(
                store, obligation.account_id, revision.reporting_revision_id, limit
            )
            assert walked == rows
        first = await store.read_revision_rows(
            account_id=obligation.account_id,
            reporting_revision_id=revision.reporting_revision_id,
            limit=10,
        )
        assert first.total_count == 1234 and first.has_more and len(first.rows) == 10
        # The stored bytes still reproduce the revision binding.
        assert (
            revision_content_sha256(
                reporting_revision_id=revision.reporting_revision_id,
                row_count=len(rows),
                control_totals=revision.control_totals,
                reporting_rows=walked,
                control_total_evidence=revision.managed_control_totals,
            )
            == revision.revision_content_sha256
        )


async def test_a_zero_row_revision_has_a_row_set_and_no_chunks() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        revision = typed_revision(obligation, [], suffix="empty")
        await store.commit_revision(revision, [])
        assert await fetch_all(
            pool, "SELECT row_count, chunk_count FROM adcp_reporting_row_sets"
        ) == [(0, 0)]
        page = await store.read_revision_rows(
            account_id=obligation.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert (page.rows, page.total_count, page.has_more) == ((), 0, False)


async def test_a_retained_pair_hashed_revision_stores_rows_only_and_verifies_its_pair_binding() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        revision, rows = revision_for(obligation)
        assert revision.managed_control_totals is None
        await store.commit_revision(revision, rows)
        (row_set,) = await fetch_all(
            pool, "SELECT digest_profile, content_sha256 FROM adcp_reporting_row_sets"
        )
        assert row_set[0] == "rows_v1"
        assert row_set[1] != revision.revision_content_sha256
        walked, _ = await walk(store, obligation.account_id, revision.reporting_revision_id, 500)
        assert walked == rows

        forged = replace(revision, reporting_revision_id="rpr_forged")
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_revision(forged, rows)
        assert conflict.value.code == "REVISION_CONTENT_MISMATCH"


async def test_rows_that_do_not_reproduce_the_binding_are_refused_before_any_write() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        revision = typed_revision(obligation, rows, suffix="tampered")
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_revision(revision, rows[:2] + [{**rows[2], "impressions": 99}])
        assert conflict.value.code in {"REVISION_CONTENT_MISMATCH", "ROW_COUNT_MISMATCH"}
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_sets") == [(0,)]
        assert await fetch_all(pool, "SELECT count(*) FROM reporting_revisions") == [(0,)]


async def test_non_portable_rows_are_refused() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = [{"media_buy_id": "mb", "impressions": 1, "ratio": float("inf")}]
        revision, _ = revision_for(obligation, suffix="inf")
        with pytest.raises(Exception):
            await store.commit_revision(replace(revision, row_count=1), rows)
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_sets") == [(0,)]


async def test_replaying_a_committed_revision_does_not_write_rows_again() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(5)
        revision = typed_revision(obligation, rows, suffix="replay")
        await store.commit_revision(revision, rows)
        await store.commit_revision(revision, rows)
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_sets") == [(1,)]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_chunks") == [(1,)]


async def test_identical_chunks_in_one_obligation_share_one_body() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(20)
        first = typed_revision(obligation, rows, suffix="a")
        second = typed_revision(
            obligation, rows, suffix="b", supersedes=first.reporting_revision_id
        )
        await store.commit_revision(first, rows)
        await store.commit_revision(second, rows)
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_row_chunks") == [(2,)]
        assert await fetch_all(pool, "SELECT count(*) FROM adcp_reporting_chunk_bodies") == [(1,)]
        for revision in (first, second):
            walked, _ = await walk(
                store, obligation.account_id, revision.reporting_revision_id, 500
            )
            assert walked == rows


async def test_legacy_inline_and_chunked_revisions_read_side_by_side() -> None:
    async with isolated_reporting_pool() as pool:
        legacy_store = PgReportingLedgerStore(pool=pool)
        await legacy_store.create_schema()
        obligation = await seeded(legacy_store)
        inline, inline_rows = revision_for(obligation)
        await legacy_store.commit_revision(inline, inline_rows)

        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        rows = many_rows(30)
        chunked = typed_revision(
            obligation, rows, suffix="chunked", supersedes=inline.reporting_revision_id
        )
        await store.commit_revision(chunked, rows)

        # The reader works on both forms, with or without the write option.
        for reader in (store, legacy_store):
            walked, _ = await walk(reader, obligation.account_id, inline.reporting_revision_id, 500)
            assert walked == inline_rows
            walked, _ = await walk(reader, obligation.account_id, chunked.reporting_revision_id, 7)
            assert walked == rows


async def disable_triggers_and(pool: Any, *statements: tuple[str, tuple[Any, ...]]) -> None:
    async with pool.connection() as connection, connection.transaction():
        for table in (
            "adcp_reporting_row_sets",
            "adcp_reporting_row_chunks",
            "adcp_reporting_chunk_bodies",
        ):
            await connection.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")  # noqa: S608
        for statement, params in statements:
            await connection.execute(statement, params)
        for table in (
            "adcp_reporting_row_sets",
            "adcp_reporting_row_chunks",
            "adcp_reporting_chunk_bodies",
        ):
            await connection.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")  # noqa: S608


async def test_reads_fail_closed_on_tampered_manifests_lost_bodies_and_expired_rows() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(600)
        revision = typed_revision(obligation, rows, suffix="victim")
        await store.commit_revision(revision, rows)
        read = lambda: store.read_revision_rows(  # noqa: E731
            account_id=obligation.account_id,
            reporting_revision_id=revision.reporting_revision_id,
            limit=100,
        )
        await read()

        # A database editor changes a recorded segment digest: the manifest no
        # longer matches the digest committed on the row set.
        await disable_triggers_and(
            pool,
            (
                "UPDATE adcp_reporting_row_chunks SET segments = jsonb_set(segments, '{0,sha256}',"
                " to_jsonb(%s::text))",
                ("f" * 64,),
            ),
        )
        with pytest.raises(ReportingRowStoreError) as failure:
            await read()
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"
        # Restore, then lose the body: unavailable, never an empty or partial page.
        async with pool.connection() as connection, connection.transaction():
            await connection.execute("ALTER TABLE adcp_reporting_row_chunks DISABLE TRIGGER USER")
            await connection.execute("DELETE FROM adcp_reporting_row_chunks")
            await connection.execute("ALTER TABLE adcp_reporting_row_chunks ENABLE TRIGGER USER")
        with pytest.raises(ReportingRowStoreError) as failure:
            await read()
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"


async def test_a_lost_body_is_unavailable_and_expired_rows_look_unknown() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="lost")
        await store.commit_revision(revision, rows)

        async def read() -> Any:
            return await store.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )

        await disable_triggers_and(pool, ("DELETE FROM adcp_reporting_chunk_bodies", ()))
        with pytest.raises(ReportingRowStoreError) as failure:
            await read()
        assert failure.value.code == "ROWS_UNAVAILABLE"

        await disable_triggers_and(
            pool, ("UPDATE adcp_reporting_row_sets SET rows_state = 'pruned'", ())
        )
        with pytest.raises(LedgerConflictError) as unknown:
            await read()
        assert unknown.value.code == "REVISION_NOT_FOUND"
        await disable_triggers_and(
            pool, ("UPDATE adcp_reporting_row_sets SET rows_state = 'unavailable'", ())
        )
        with pytest.raises(ReportingRowStoreError) as lost:
            await read()
        assert lost.value.code == "ROWS_UNAVAILABLE"


async def test_a_row_set_naming_other_content_than_its_header_is_never_released() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(10)
        revision = typed_revision(obligation, rows, suffix="header")
        await store.commit_revision(revision, rows)
        await disable_triggers_and(
            pool, ("UPDATE adcp_reporting_row_sets SET content_sha256 = %s", ("e" * 64,))
        )
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"


async def test_a_row_set_is_scoped_to_its_account() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        revision = typed_revision(obligation, rows, suffix="scoped")
        await store.commit_revision(revision, rows)
        with pytest.raises(LedgerConflictError) as unknown:
            await store.read_revision_rows(
                account_id="acct_other", reporting_revision_id=revision.reporting_revision_id
            )
        assert unknown.value.code == "REVISION_NOT_FOUND"


async def test_a_binding_registered_by_another_deployment_namespace_is_refused() -> None:
    async with isolated_reporting_pool() as pool:
        first = PgReportingLedgerStore(
            pool=pool, row_storage=ReportingRowStorageOptionsV1(deployment_namespace="prod-us")
        )
        await first.create_schema()
        second = PgReportingLedgerStore(
            pool=pool, row_storage=ReportingRowStorageOptionsV1(deployment_namespace="prod-eu")
        )
        with pytest.raises(ReportingRowStoreError) as failure:
            await second.ready_row_storage()
        assert failure.value.code == "UNSAFE_BINDING"


async def test_the_schema_catalog_is_verified_and_drift_is_refused() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE adcp_reporting_row_sets DROP CONSTRAINT"
                " adcp_reporting_row_sets_rows_state_check"
            )
        with pytest.raises(ReportingRowStoreError) as failure:
            await PgReportingLedgerStore(pool=pool, row_storage=True).ready_row_storage()
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert "adcp_reporting_row_sets" in str(failure.value)


async def test_a_row_set_table_with_another_sdks_core_foreign_key_is_refused() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        async with pool.connection() as connection:
            await connection.execute(
                "CREATE TABLE adcp_reporting_obligations (obligation_id TEXT PRIMARY KEY)"
            )
            await connection.execute(
                "ALTER TABLE adcp_reporting_row_sets ADD CONSTRAINT js_core_fk"
                " FOREIGN KEY (obligation_id) REFERENCES adcp_reporting_obligations (obligation_id)"
            )
        with pytest.raises(ReportingRowStoreError) as failure:
            await PgReportingLedgerStore(pool=pool, row_storage=True).ready_row_storage()
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert "js_core_fk" in str(failure.value)


async def test_the_database_enforces_immutability_and_body_digests() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(3)
        revision = typed_revision(obligation, rows, suffix="locked")
        await store.commit_revision(revision, rows)
        from psycopg import errors

        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation, match="binding identity is immutable"):
                await connection.execute("UPDATE adcp_reporting_row_bindings SET provider = 'gcs'")
        async with pool.connection() as connection:
            await connection.execute("UPDATE adcp_reporting_row_bindings SET state = 'read_only'")
        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation, match="row set content is immutable"):
                await connection.execute("UPDATE adcp_reporting_row_sets SET row_count = 99")
        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation, match="row chunk content is immutable"):
                await connection.execute("UPDATE adcp_reporting_row_chunks SET byte_count = 1")
        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation, match="chunk bodies are immutable"):
                await connection.execute("UPDATE adcp_reporting_chunk_bodies SET body = body")
        async with pool.connection() as connection:
            with pytest.raises(errors.CheckViolation):
                await connection.execute(
                    "INSERT INTO adcp_reporting_chunk_bodies (account_id, obligation_id, sha256, body)"
                    " VALUES ('a', 'o', %s, %s)",
                    ("c" * 64, b"{}\n"),
                )
        async with pool.connection() as connection:
            with pytest.raises(errors.ForeignKeyViolation):
                await connection.execute(
                    "INSERT INTO adcp_reporting_row_chunks (row_set_id, chunk_index, account_id,"
                    " obligation_id, first_ordinal, row_count, byte_count, sha256, segments)"
                    " VALUES (%s, 5, 'acct_other', %s, 0, 1, 3, %s, '[]')",
                    (revision.reporting_revision_id, obligation.reporting_obligation_id, "d" * 64),
                )


def test_the_migration_names_the_shared_tables() -> None:
    ddl = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    for table in (
        "adcp_persistence_installation",
        "adcp_reporting_row_bindings",
        "adcp_reporting_row_sets",
        "adcp_reporting_row_chunks",
        "adcp_reporting_chunk_bodies",
        "adcp_reporting_row_write_intents",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "REFERENCES adcp_reporting_obligations" not in ddl


async def test_rows_with_fractional_and_exponent_numbers_round_trip_and_verify() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = [
            {"media_buy_id": "mb", "impressions": 1, "ratio": 0.30000000000000004, "tiny": 1e-7},
            {"media_buy_id": "mb", "impressions": 2, "ratio": -12.5, "wide": 1.5e-300},
        ]
        revision = typed_revision(obligation, rows, suffix="floats")
        await store.commit_revision(revision, rows)
        walked, _ = await walk(store, obligation.account_id, revision.reporting_revision_id, 500)
        assert walked == rows
        assert (
            revision_content_sha256(
                reporting_revision_id=revision.reporting_revision_id,
                row_count=2,
                control_totals=revision.control_totals,
                reporting_rows=walked,
                control_total_evidence=revision.managed_control_totals,
            )
            == revision.revision_content_sha256
        )


async def test_the_packaged_catalog_manifest_matches_a_clean_migration() -> None:
    from adcp.reporting.ledger.row_storage import REPORTING_ROW_STORAGE_TABLES
    from adcp.reporting.outbox._schema import _catalog_objects

    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute(REPORTING_ROW_STORAGE_DDL_PATH.read_text())
            installed = await _catalog_objects(
                connection,
                table_names=REPORTING_ROW_STORAGE_TABLES,
                function_identity=("adcp_reporting_row_storage_immutable", ""),
                name_prefix="adcp_",
            )
    from importlib.resources import files

    packaged = json.loads(
        files("adcp.reporting.ledger").joinpath("required_row_storage_schema.json").read_text()
    )
    assert installed == packaged, "run scripts/generate_row_storage_catalog.py"


async def test_a_full_verified_read_proves_the_binding_under_the_algorithm_it_was_written_with() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=True)
        await store.create_schema()
        obligation = await seeded(store)
        rows = many_rows(1100)
        typed = typed_revision(obligation, rows, suffix="typed")
        await store.commit_revision(typed, rows)
        pair, pair_rows = revision_for(obligation, suffix="pair")
        pair = replace(pair, supersedes_reporting_revision_id=typed.reporting_revision_id)
        await store.commit_revision(pair, pair_rows)

        verified = await store.read_verified_revision_rows(
            account_id=obligation.account_id, reporting_revision_id=typed.reporting_revision_id
        )
        assert list(verified) == rows
        verified = await store.read_verified_revision_rows(
            account_id=obligation.account_id, reporting_revision_id=pair.reporting_revision_id
        )
        assert list(verified) == pair_rows
        with pytest.raises(ReportingRowStoreError) as capped:
            await store.read_verified_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=typed.reporting_revision_id,
                max_bytes=10,
            )
        assert capped.value.code == "INVALID_INPUT"

        # A header that no longer names the stored bytes is never released.
        async with pool.connection() as connection:
            await connection.execute("ALTER TABLE reporting_revisions DISABLE TRIGGER USER")
            await connection.execute(
                'UPDATE reporting_revisions SET control_totals = \'[["impressions", "1"]]\'::jsonb'
                " WHERE reporting_revision_id = %s",
                (pair.reporting_revision_id,),
            )
            await connection.execute("ALTER TABLE reporting_revisions ENABLE TRIGGER USER")
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_verified_revision_rows(
                account_id=obligation.account_id, reporting_revision_id=pair.reporting_revision_id
            )
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"


async def test_a_full_verified_read_checks_legacy_inline_rows_too() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        obligation = await seeded(store)
        revision, rows = revision_for(obligation)
        await store.commit_revision(revision, rows)
        verified = await store.read_verified_revision_rows(
            account_id=obligation.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert list(verified) == rows
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_revision_rows SET row_payload = '{\"impressions\": 6}'::jsonb"
            )
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.read_verified_revision_rows(
                account_id=obligation.account_id,
                reporting_revision_id=revision.reporting_revision_id,
            )
        assert failure.value.code == "ROWS_INTEGRITY_FAILED"
