"""An empty pre-ownership ledger can be replaced without an archive."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pytest

from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.reporting.migration import (
    ReportingOwnershipMigrationError,
    replace_empty_legacy_reporting,
)

from ._generation_support import isolated_reporting_pool, require_rolling_database


async def install_legacy(connection):
    for name in (
        "reporting_ledger.sql",
        "reporting_ledger_obligation_currency.sql",
        "reporting_ledger_reconciliation.sql",
    ):
        await connection.execute(
            (Path(__file__).parents[2] / "fixtures/reporting_ownership" / name).read_text()
        )


async def test_empty_legacy_replacement_allows_owned_schema_without_archive():
    require_rolling_database()
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            await connection.execute(
                "CREATE SEQUENCE reporting_unused_sequence; "
                "CREATE FUNCTION reporting_unused_function() RETURNS integer "
                "LANGUAGE SQL AS 'SELECT 1'; CREATE TABLE application_data(id integer); "
                "INSERT INTO application_data VALUES (42)"
            )
            await replace_empty_legacy_reporting(connection)
            assert (
                await (await connection.execute("SELECT * FROM application_data")).fetchone()
            ) == (42,)
            assert (
                await (
                    await connection.execute(
                        "SELECT to_regclass('reporting_configurations'), "
                        "to_regclass('reporting_unused_sequence'), "
                        "to_regprocedure('reporting_unused_function()')"
                    )
                ).fetchone()
            ) == (None, None, None)
            store = PgReportingLedgerStore(pool=pool)
            await store.create_schema()
            assert ("consumer_id",) in await (
                await connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='reporting_configurations'"
                )
            ).fetchall()
            assert await store.list_all_configurations() == ()
            assert (
                await (
                    await connection.execute("SELECT * FROM adcp_reporting_ownership_archives")
                ).fetchall()
            ) == []
            with pytest.raises(ReportingOwnershipMigrationError, match="unmigrated"):
                await replace_empty_legacy_reporting(connection)


@pytest.mark.parametrize("table", ["reporting_extra_evidence", "adcp_reporting_extra_evidence"])
async def test_populated_legacy_table_refuses_replacement_atomically(table):
    require_rolling_database()
    from psycopg import sql

    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            await connection.execute(
                sql.SQL(
                    "CREATE TABLE {}(body bytea); INSERT INTO {} VALUES (decode('00ff','hex'))"
                ).format(sql.Identifier(table), sql.Identifier(table))
            )
            with pytest.raises(ReportingOwnershipMigrationError, match="contains rows"):
                await replace_empty_legacy_reporting(connection)
            assert (
                await (
                    await connection.execute(
                        sql.SQL("SELECT body FROM {}").format(sql.Identifier(table))
                    )
                ).fetchone()
            ) == (b"\x00\xff",)
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_configurations')")
                ).fetchone()
            )[0] is not None


async def test_sql_body_functions_on_legacy_tables_are_replaced():
    require_rolling_database()
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            # A SQL-standard body depends on the table itself, not its row type.
            await connection.execute(
                "CREATE FUNCTION reporting_configuration_count() RETURNS bigint"
                " LANGUAGE SQL BEGIN ATOMIC SELECT count(*) FROM reporting_configurations; END"
            )
            await replace_empty_legacy_reporting(connection)
            assert (
                await (
                    await connection.execute(
                        "SELECT to_regclass('reporting_configurations'),"
                        " to_regprocedure('reporting_configuration_count()')"
                    )
                ).fetchone()
            ) == (None, None)


@pytest.mark.parametrize("dependency", ["view", "sequence", "function", "partition"])
async def test_external_dependencies_roll_back_replacement(dependency):
    require_rolling_database()
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            if dependency == "view":
                await connection.execute(
                    "CREATE VIEW application_view AS SELECT account_id"
                    " FROM reporting_configurations"
                )
            elif dependency == "sequence":
                await connection.execute(
                    "CREATE SEQUENCE reporting_application_seq; CREATE TABLE application_data "
                    "(id integer DEFAULT nextval('reporting_application_seq'))"
                )
            elif dependency == "function":
                await connection.execute(
                    "CREATE FUNCTION reporting_application_value() RETURNS integer "
                    "LANGUAGE SQL AS 'SELECT 1'; CREATE VIEW application_view AS "
                    "SELECT reporting_application_value()"
                )
            else:
                await connection.execute(
                    "CREATE TABLE reporting_parent(id integer) PARTITION BY RANGE(id); "
                    "CREATE TABLE application_partition PARTITION OF reporting_parent "
                    "FOR VALUES FROM (0) TO (10)"
                )
            with pytest.raises(ReportingOwnershipMigrationError, match="external"):
                await replace_empty_legacy_reporting(connection)
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_configurations')")
                ).fetchone()
            )[0] is not None


async def test_replacement_waits_for_writer_then_refuses_committed_rows():
    require_rolling_database()
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection, pool.connection() as writer:
            await install_legacy(connection)
            await connection.execute("CREATE TABLE reporting_late_evidence(id integer)")
            async with writer.transaction():
                await writer.execute("INSERT INTO reporting_late_evidence VALUES (1)")
                task = asyncio.create_task(replace_empty_legacy_reporting(connection))
                try:
                    # Confirm the migration reached a blocked table lock, rather
                    # than using elapsed time as evidence that it checks safely.
                    for _ in range(100):
                        async with pool.connection() as observer:
                            waiting = await (
                                await observer.execute(
                                    "SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted",
                                    (connection.info.backend_pid,),
                                )
                            ).fetchone()
                        if waiting:
                            break
                        await asyncio.sleep(0.01)
                    assert waiting
                    assert not task.done()
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            with pytest.raises(ReportingOwnershipMigrationError, match="contains rows"):
                await asyncio.wait_for(task, timeout=5)
            assert (
                await (
                    await connection.execute("SELECT id FROM reporting_late_evidence")
                ).fetchone()
            ) == (1,)


async def test_row_security_cannot_hide_populated_legacy_tables():
    require_rolling_database()
    from psycopg import sql

    role = "adcp_empty_ledger_" + secrets.token_hex(6)
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
            await connection.execute(
                "CREATE TABLE reporting_hidden_rows(id integer); "
                "INSERT INTO reporting_hidden_rows VALUES (1); "
                "ALTER TABLE reporting_hidden_rows ENABLE ROW LEVEL SECURITY; "
                "CREATE POLICY hide_rows ON reporting_hidden_rows USING (false)"
            )
            await connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            try:
                await connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                await connection.execute(
                    sql.SQL("GRANT ALL ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    )
                )
                await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                assert (
                    await (
                        await connection.execute("SELECT * FROM reporting_hidden_rows")
                    ).fetchall()
                ) == []
                with pytest.raises(ReportingOwnershipMigrationError, match="row security"):
                    await replace_empty_legacy_reporting(connection)
            finally:
                await connection.execute("RESET ROLE")
                await connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            assert (
                await (await connection.execute("SELECT * FROM reporting_hidden_rows")).fetchone()
            ) == (1,)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
async def test_snapshot_isolation_refuses_replacement(isolation):
    require_rolling_database()
    from psycopg import sql

    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            await install_legacy(connection)
            async with connection.transaction():
                await connection.execute(
                    sql.SQL("SET TRANSACTION ISOLATION LEVEL {}").format(sql.SQL(isolation))
                )
                with pytest.raises(ReportingOwnershipMigrationError, match="READ COMMITTED"):
                    await replace_empty_legacy_reporting(connection)
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_configurations')")
                ).fetchone()
            )[0] is not None
