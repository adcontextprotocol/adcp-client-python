"""``row_binding_id`` is bounded at 255 characters in the database too (adcp-client-python #1498).

``tests/fixtures/reporting-row-storage-pre-binding-bounds.sql`` is the row-storage DDL as released
before the bound moved from 128 to 255 (an unnamed ``CHECK`` PostgreSQL named after the column).
Re-applying the current DDL drops it and adds ``adcp_reporting_row_bindings_row_binding_id_bounds``,
the constraint a fresh install carries inline and the JavaScript SDK names the same. Ported from
adcp-client #3595.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore, ReportingRowStorageOptionsV1
from adcp_reporting_storage.ledger.row_storage import (
    REPORTING_ROW_STORAGE_DDL_PATH,
    REPORTING_ROW_STORAGE_TABLES,
    ReportingObjectRowBindingV1,
    ReportingPostgresRowBindingV1,
)
from adcp_reporting_storage.ledger.row_storage_object import (
    create_filesystem_reporting_row_object_provider_v1,
)
from adcp_reporting_storage.outbox._schema import _catalog_objects

from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError

from ._generation_support import isolated_reporting_pool
from .test_reporting_row_storage_pg import fetch_all, many_rows, seeded, typed_revision, walk

OLD_DDL = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "reporting-row-storage-pre-binding-bounds.sql"
)
CONSTRAINT = "adcp_reporting_row_bindings_row_binding_id_bounds"
LEGACY_CONSTRAINT = "adcp_reporting_row_bindings_row_binding_id_check"
SHA = "a" * 64


async def insert_binding(pool: Any, binding_id: str) -> None:
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(
            "INSERT INTO adcp_reporting_row_bindings (row_binding_id, kind, provider,"
            " identity_config, identity_sha256, namespace_key)"
            " VALUES (%s, 'postgres', 'postgres', '{}', %s, %s)",
            (binding_id, SHA, "b" * 64),
        )


async def binding_constraints(pool: Any) -> list[tuple[Any, ...]]:
    return await fetch_all(
        pool,
        "SELECT conname, oid::text, pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'adcp_reporting_row_bindings'::regclass AND contype = 'c'"
        " AND pg_get_constraintdef(oid) LIKE '%%row_binding_id%%' ORDER BY conname",
    )


async def catalog(pool: Any) -> dict[str, Any]:
    async with pool.connection() as connection:
        return await _catalog_objects(
            connection,
            table_names=REPORTING_ROW_STORAGE_TABLES,
            function_identity=("adcp_reporting_row_storage_immutable", ""),
            name_prefix="adcp_",
        )


async def apply(pool: Any, ddl: str) -> None:
    async with pool.connection() as connection, connection.transaction():
        await connection.execute(ddl)


async def test_a_fresh_install_accepts_255_and_refuses_the_rest_under_one_named_constraint() -> (
    None
):
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool() as pool:
        await apply(pool, REPORTING_ROW_STORAGE_DDL_PATH.read_text())
        assert [row[0] for row in await binding_constraints(pool)] == [CONSTRAINT]
        assert "{1,255}" in (await binding_constraints(pool))[0][2]
        await insert_binding(pool, "a" * 255)
        await insert_binding(pool, "a" * 128)
        for bad in ("a" * 256, "", "has space", "trailing\n", "аcyrillic", "x" * 5000):
            with pytest.raises(psycopg.errors.CheckViolation):
                await insert_binding(pool, bad)


async def test_a_database_with_the_original_128_limit_is_widened_in_place_and_idempotently() -> (
    None
):
    psycopg = pytest.importorskip("psycopg")
    current = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as old, isolated_reporting_pool() as fresh:
        await apply(old, OLD_DDL.read_text())
        assert [row[0] for row in await binding_constraints(old)] == [LEGACY_CONSTRAINT]
        await insert_binding(old, "k" * 128)
        with pytest.raises(psycopg.errors.CheckViolation):
            await insert_binding(old, "k" * 129)

        await apply(old, current)
        upgraded = await binding_constraints(old)
        assert [row[0] for row in upgraded] == [CONSTRAINT], "the original CHECK is gone"
        await insert_binding(old, "k" * 255)
        with pytest.raises(psycopg.errors.CheckViolation):
            await insert_binding(old, "k" * 256)
        assert await fetch_all(old, "SELECT count(*) FROM adcp_reporting_row_bindings") == [
            (2,)
        ], "no recorded binding was touched"

        # Re-applying changes nothing: the same constraint object survives.
        await apply(old, current)
        await apply(old, current)
        assert await binding_constraints(old) == upgraded

        # The upgraded database is indistinguishable from a fresh install.
        await apply(fresh, current)
        assert await catalog(old) == await catalog(fresh)


async def test_a_current_schema_is_not_rewritten_by_the_guard() -> None:
    current = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as pool:
        await apply(pool, current)
        before = await binding_constraints(pool)
        await apply(pool, current)
        assert await binding_constraints(pool) == before


async def test_the_guard_leaves_a_host_customised_constraint_alone() -> None:
    """Only the ``{1,128}`` shape this SDK shipped is replaced; anything else is the host's."""
    psycopg = pytest.importorskip("psycopg")
    current = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
    async with isolated_reporting_pool() as pool:
        await apply(pool, OLD_DDL.read_text())
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(
                f"ALTER TABLE adcp_reporting_row_bindings DROP CONSTRAINT {LEGACY_CONSTRAINT}"
            )
            await connection.execute(
                "ALTER TABLE adcp_reporting_row_bindings ADD CONSTRAINT host_row_binding_id_64"
                " CHECK (row_binding_id ~ '^[A-Za-z0-9_.:-]{1,64}$')"
            )
        await apply(pool, current)
        assert [row[0] for row in await binding_constraints(pool)] == [
            CONSTRAINT,
            "host_row_binding_id_64",
        ]
        with pytest.raises(psycopg.errors.CheckViolation):
            await insert_binding(pool, "h" * 65)


async def test_the_guard_does_nothing_before_the_bindings_table_exists() -> None:
    async with isolated_reporting_pool() as pool:
        ddl = REPORTING_ROW_STORAGE_DDL_PATH.read_text()
        start = ddl.index("DO $binding_id_bound$")
        block = ddl[start : ddl.index("$binding_id_bound$;", start) + len("$binding_id_bound$;")]
        await apply(pool, block)


async def test_a_binding_registered_against_the_original_limit_fails_closed_until_the_ddl_runs(
    tmp_path: Path,
) -> None:
    """The startup catalog check names the missing bound; it never reaches a bare CHECK failure."""
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(
            pool=pool,
            row_storage=ReportingRowStorageOptionsV1(
                deployment_namespace="test",
                bindings={"x" * 200: ReportingPostgresRowBindingV1()},
            ),
        )
        await apply(pool, _ledger_ddl())
        await apply(pool, OLD_DDL.read_text())
        with pytest.raises(ReportingRowStoreError) as failure:
            await store.ready_row_storage()
        assert failure.value.code == "STATE_UNAVAILABLE"
        assert CONSTRAINT in str(failure.value)

        await store.create_schema()  # applies the current row-storage DDL in place
        await store.ready_row_storage()
        assert await fetch_all(
            pool, "SELECT length(row_binding_id) FROM adcp_reporting_row_bindings"
        ) == [(200,)]


def _ledger_ddl() -> str:
    from adcp_reporting_storage.ledger.pg import _DDL_PATH

    return _DDL_PATH.read_text()


async def test_a_255_character_binding_id_and_credential_ref_write_and_read_back_a_revision(
    tmp_path: Path,
) -> None:
    binding_id = "r" * 255
    root = tmp_path / "rows"
    provider = create_filesystem_reporting_row_object_provider_v1(roots={"rows": root})
    options = ReportingRowStorageOptionsV1(
        deployment_namespace="test",
        bindings={
            binding_id: ReportingObjectRowBindingV1(
                provider="filesystem",
                location={"root": "rows"},
                prefix="adcp-rows",
                credential_ref="c" * 255,
            ),
        },
        providers={"filesystem": provider},
    )
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, row_storage=options)
        await store.create_schema()
        obligation = await seeded(store)
        async with pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_configurations SET lease_worker_id = 'worker-1',"
                " lease_expires_at = clock_timestamp() + INTERVAL '10 minutes'"
            )
        rows = many_rows(50)
        revision = typed_revision(obligation, rows, suffix="long")
        assert await store.stage_revision_rows(revision, rows) is True
        await store.commit_revision(revision, rows)

        assert await fetch_all(
            pool, "SELECT row_binding_id, operational_config FROM adcp_reporting_row_bindings"
        ) == [(binding_id, {"compression": "gzip", "credentialRef": "c" * 255})]
        walked, _ = await walk(store, obligation.account_id, revision.reporting_revision_id, 20)
        assert walked == rows
