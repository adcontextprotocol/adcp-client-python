"""An empty caller-owned ledger can be removed so an 8.x image can start again."""

from __future__ import annotations

import asyncio

import pytest

from adcp.reporting.migration import (
    ReportingOwnershipMigrationError,
    replace_empty_legacy_reporting,
    replace_empty_owned_reporting,
    replace_empty_owned_reporting_sync,
)

from ._empty_ledger_support import (
    DRIVERS,
    FUNCTIONS,
    Background,
    connect,
    create_owned_schema,
    database_url,
    install_legacy,
    install_owned,
    isolated_schema,
    require_driver,
    session,
    surface,
    wait_until_blocked,
)
from ._generation_support import isolated_reporting_pool, require_rolling_database


@pytest.mark.parametrize("installed", ["create_schema", "every packaged migration"])
def test_rollback_removes_the_owned_surface_and_8x_ddl_installs(installed):
    require_driver("psycopg")
    with isolated_schema() as schema:
        if installed == "create_schema":
            create_owned_schema(schema, change_feed=True, row_storage=True)
        with connect(schema, autocommit=True) as connection:
            if installed != "create_schema":
                install_owned(connection)
            connection.execute(
                "CREATE TABLE application_data(id integer); "
                "INSERT INTO application_data VALUES (42)"
            )
            assert surface(connection) > 0
            # Installation and startup write configuration rows, not reporting state.
            assert connection.execute(
                "SELECT count(*) FROM adcp_reporting_change_horizon"
            ).fetchone() == (1,)
            if installed == "create_schema":
                assert connection.execute(
                    "SELECT count(*) > 0 FROM adcp_reporting_row_bindings"
                ).fetchone() == (True,)
            replace_empty_owned_reporting_sync(connection)
            assert surface(connection) == 0
            assert connection.execute(FUNCTIONS).fetchone() == (0,)
            assert connection.execute("SELECT * FROM application_data").fetchall() == [(42,)]
            # The deployment-wide row-storage identity is not reporting state.
            assert connection.execute(
                "SELECT to_regclass('adcp_persistence_installation') IS NOT NULL"
            ).fetchone() == (True,)
            # The 8.x image creates its own ledger from scratch, and SDK 9 can
            # replace that empty ledger again when the upgrade is retried.
            install_legacy(connection)
        asyncio.run(_replace_legacy(schema))
        create_owned_schema(schema)
        with connect(schema, autocommit=True) as connection:
            replace_empty_owned_reporting_sync(connection)
            assert surface(connection) == 0
            with pytest.raises(ReportingOwnershipMigrationError, match="caller-owned"):
                replace_empty_owned_reporting_sync(connection)


async def _replace_legacy(schema: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        database_url(), autocommit=True, options=f"-csearch_path={schema}"
    ) as connection:
        await replace_empty_legacy_reporting(connection)


def test_functions_depending_on_owned_tables_are_removed():
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema, autocommit=True) as connection:
            connection.execute(
                "CREATE FUNCTION reporting_configuration_count() RETURNS bigint"
                " LANGUAGE SQL BEGIN ATOMIC SELECT count(*) FROM reporting_configurations; END;"
                " CREATE FUNCTION reporting_configuration_ids(c reporting_configurations[])"
                " RETURNS integer LANGUAGE SQL AS 'SELECT 1'"
            )
            replace_empty_owned_reporting_sync(connection)
            assert surface(connection) == 0
            assert connection.execute(FUNCTIONS).fetchone() == (0,)


@pytest.mark.parametrize("driver", DRIVERS)
def test_rollback_is_atomic_with_the_outer_transaction(driver):
    require_driver(driver)
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema, autocommit=True) as setup:
            setup.execute("CREATE TABLE application_data(id integer)")
            owned = surface(setup)
        with session(schema, driver) as caller:
            caller.run("INSERT INTO application_data VALUES (1)")
            replace_empty_owned_reporting_sync(caller.bind)
            assert caller.run("SELECT to_regclass('reporting_configurations')") == [(None,)]
            caller.rollback()
        with connect(schema) as check:
            assert surface(check) == owned
            assert check.execute("SELECT * FROM application_data").fetchall() == []
        with session(schema, driver) as caller:
            caller.run("INSERT INTO application_data VALUES (2)")
            replace_empty_owned_reporting_sync(caller.bind)
            caller.commit()
        with connect(schema) as check:
            assert surface(check) == 0
            assert check.execute("SELECT * FROM application_data").fetchall() == [(2,)]


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("INSERT INTO reporting_extra_evidence VALUES (1)", "contains rows"),
        ("INSERT INTO adcp_reporting_extra_evidence VALUES (1)", "contains rows"),
        (
            "UPDATE adcp_reporting_change_horizon SET pruned_through_seq = 5",
            "contains rows",
        ),
        (
            "INSERT INTO adcp_reporting_ownership_archives"
            " VALUES ('adcp_reporting_quarantine_release')",
            "quarantine archive",
        ),
    ],
)
def test_retained_state_refuses_rollback_atomically(state, message):
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema, change_feed=True)
        with connect(schema, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE reporting_extra_evidence(id integer); "
                "CREATE TABLE adcp_reporting_extra_evidence(id integer)"
            )
            connection.execute(state)
            owned = surface(connection)
            functions = connection.execute(FUNCTIONS).fetchone()
            with pytest.raises(ReportingOwnershipMigrationError, match=message):
                replace_empty_owned_reporting_sync(connection)
            assert surface(connection) == owned
            assert connection.execute(FUNCTIONS).fetchone() == functions


@pytest.mark.parametrize("legacy", [True, False])
def test_rollback_requires_an_owned_ledger(legacy):
    require_driver("psycopg")
    with isolated_schema() as schema, connect(schema, autocommit=True) as connection:
        if legacy:
            install_legacy(connection)
        before = surface(connection)
        with pytest.raises(ReportingOwnershipMigrationError, match="caller-owned"):
            replace_empty_owned_reporting_sync(connection)
        assert surface(connection) == before


def test_external_dependency_refuses_rollback():
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema, autocommit=True) as connection:
            connection.execute(
                "CREATE VIEW application_view AS SELECT account_id FROM reporting_configurations"
            )
            owned = surface(connection)
            with pytest.raises(ReportingOwnershipMigrationError, match="nothing was removed"):
                replace_empty_owned_reporting_sync(connection)
            assert surface(connection) == owned
            assert connection.execute(FUNCTIONS).fetchone()[0] > 0


def test_rollback_waits_for_writer_then_refuses_committed_rows():
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema, autocommit=True) as setup:
            setup.execute("CREATE TABLE reporting_late_evidence(id integer)")
        with connect(schema) as writer, connect(schema) as caller:
            writer.execute("INSERT INTO reporting_late_evidence VALUES (1)")
            rollback = Background(lambda: replace_empty_owned_reporting_sync(caller))
            rollback.start()
            try:
                wait_until_blocked(schema, caller.info.backend_pid, rollback)
            finally:
                writer.commit()
            error = rollback.outcome()
            assert isinstance(error, ReportingOwnershipMigrationError)
            assert "contains rows" in str(error)
            caller.rollback()
        with connect(schema) as check:
            assert check.execute("SELECT id FROM reporting_late_evidence").fetchall() == [(1,)]


def test_rollback_blocks_create_schema_until_it_commits():
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema) as caller:
            replace_empty_owned_reporting_sync(caller)
            # A 9.0 process booting now waits on the schema lock rather than
            # observing, or recreating, a half-removed ledger.
            boot = Background(lambda: create_owned_schema(schema))
            boot.start()
            try:
                with connect(schema, autocommit=True) as observer:
                    for _ in range(1000):
                        waiting = observer.execute(
                            "SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid"
                            " WHERE NOT l.granted AND l.locktype='advisory'"
                            " AND a.pid <> %s AND a.datname=current_database()",
                            (caller.info.backend_pid,),
                        ).fetchone()
                        if waiting:
                            break
                        boot.join(timeout=0.01)
                    assert waiting and boot.is_alive()
            finally:
                caller.commit()
            assert boot.outcome() is None
        with connect(schema) as check:
            # The boot ran after the removal committed, so it built a fresh ledger.
            assert check.execute(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema=current_schema()"
                " AND table_name='reporting_configurations' AND column_name='consumer_id'"
            ).fetchone() == (1,)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_snapshot_isolation_refuses_rollback(isolation):
    require_driver("psycopg")
    with isolated_schema() as schema:
        create_owned_schema(schema)
        with connect(schema) as caller:
            caller.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            with pytest.raises(ReportingOwnershipMigrationError, match="READ COMMITTED"):
                replace_empty_owned_reporting_sync(caller)
            caller.commit()
            assert surface(caller) > 0


async def test_async_rollback_matches_the_sync_guarantees():
    require_rolling_database()
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingLedgerStore(pool=pool, change_feed=True).create_schema()
        async with pool.connection() as connection, pool.connection() as writer:
            await connection.execute("CREATE TABLE reporting_late_evidence(id integer)")
            async with writer.transaction():
                await writer.execute("INSERT INTO reporting_late_evidence VALUES (1)")
                task = asyncio.create_task(replace_empty_owned_reporting(connection))
                try:
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
            await connection.execute("DROP TABLE reporting_late_evidence")
            await replace_empty_owned_reporting(connection)
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_configurations')")
                ).fetchone()
            ) == (None,)
            # Inside a transaction already open on the connection, the async
            # variant runs as a savepoint: the caller's rollback restores it.
            await PgReportingLedgerStore(pool=pool).create_schema()
            async with connection.transaction(force_rollback=True):
                await replace_empty_owned_reporting(connection)
            assert (
                await (
                    await connection.execute("SELECT to_regclass('reporting_configurations')")
                ).fetchone()
            ) != (None,)
