"""The sync empty-legacy replacement runs inside the caller's transaction."""

from __future__ import annotations

import pytest

from adcp.reporting.migration import (
    ReportingOwnershipMigrationError,
    replace_empty_legacy_reporting_sync,
)

from ._empty_ledger_support import (
    DRIVERS,
    Background,
    connect,
    database_url,
    install_legacy,
    isolated_schema,
    require_driver,
    session,
    surface,
    wait_until_blocked,
)


def test_autocommit_connection_commits_its_own_transaction():
    require_driver("psycopg")
    with isolated_schema() as schema, connect(schema, autocommit=True) as connection:
        install_legacy(connection)
        connection.execute(
            "CREATE SEQUENCE reporting_unused_sequence; "
            "CREATE FUNCTION reporting_unused_function() RETURNS integer "
            "LANGUAGE SQL AS 'SELECT 1'; CREATE TABLE application_data(id integer); "
            "INSERT INTO application_data VALUES (42)"
        )
        replace_empty_legacy_reporting_sync(connection)
        with connect(schema) as other:
            assert surface(other) == 0
            assert other.execute("SELECT * FROM application_data").fetchall() == [(42,)]
            assert other.execute(
                "SELECT to_regprocedure('reporting_unused_function()')"
            ).fetchone() == (None,)
        with pytest.raises(ReportingOwnershipMigrationError, match="unmigrated"):
            replace_empty_legacy_reporting_sync(connection)


@pytest.mark.parametrize("driver", DRIVERS)
def test_replacement_rolls_back_with_the_outer_transaction(driver):
    require_driver(driver)
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute("CREATE TABLE application_data(id integer)")
            legacy = surface(setup)
        with session(schema, driver) as caller:
            # The migration framework's own write, e.g. Alembic's version bump.
            caller.run("INSERT INTO application_data VALUES (1)")
            replace_empty_legacy_reporting_sync(caller.bind)
            assert caller.run("SELECT to_regclass('reporting_configurations')") == [(None,)]
            caller.rollback()
        with connect(schema) as check:
            assert surface(check) == legacy
            assert check.execute("SELECT * FROM application_data").fetchall() == []


@pytest.mark.parametrize("driver", DRIVERS)
def test_replacement_commits_with_the_outer_transaction(driver):
    require_driver(driver)
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute("CREATE TABLE application_data(id integer)")
        with session(schema, driver) as caller:
            caller.run("INSERT INTO application_data VALUES (1)")
            replace_empty_legacy_reporting_sync(caller.bind)
            with connect(schema) as concurrent:
                # Nothing is visible, or committed, before the caller commits.
                assert surface(concurrent) > 0
            caller.commit()
        with connect(schema) as check:
            assert surface(check) == 0
            assert check.execute("SELECT * FROM application_data").fetchall() == [(1,)]


@pytest.mark.parametrize("driver", DRIVERS)
def test_refusal_rolls_back_only_its_savepoint(driver):
    require_driver(driver)
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute(
                "CREATE TABLE application_data(id integer); "
                "CREATE TABLE reporting_extra_evidence(id integer); "
                "INSERT INTO reporting_extra_evidence VALUES (7)"
            )
            legacy = surface(setup)
        with session(schema, driver) as caller:
            caller.run("INSERT INTO application_data VALUES (1)")
            with pytest.raises(ReportingOwnershipMigrationError, match="contains rows"):
                replace_empty_legacy_reporting_sync(caller.bind)
            # The savepoint's schema and table locks went with it.
            assert caller.run(
                "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid()"
                " AND (locktype='advisory' OR mode='AccessExclusiveLock')"
            ) == [(0,)]
            # The caller's transaction is still usable and keeps its own work.
            caller.run("INSERT INTO application_data VALUES (2)")
            caller.commit()
        with connect(schema) as check:
            assert surface(check) == legacy
            assert check.execute("SELECT * FROM application_data ORDER BY id").fetchall() == [
                (1,),
                (2,),
            ]
            assert check.execute("SELECT * FROM reporting_extra_evidence").fetchall() == [(7,)]


def test_external_dependency_rolls_back_inside_the_outer_transaction():
    require_driver("psycopg")
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute(
                "CREATE VIEW application_view AS SELECT account_id FROM reporting_configurations"
            )
            legacy = surface(setup)
        with connect(schema) as caller:
            with pytest.raises(ReportingOwnershipMigrationError, match="external dependencies"):
                replace_empty_legacy_reporting_sync(caller)
            assert caller.execute("SELECT count(*) FROM application_view").fetchone() == (0,)
            caller.commit()
            assert surface(caller) == legacy


def test_replacement_waits_for_writer_then_refuses_committed_rows():
    require_driver("psycopg")
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute("CREATE TABLE reporting_late_evidence(id integer)")
        with connect(schema) as writer, connect(schema) as caller:
            writer.execute("INSERT INTO reporting_late_evidence VALUES (1)")
            pid = caller.info.backend_pid
            replacement = Background(lambda: replace_empty_legacy_reporting_sync(caller))
            replacement.start()
            try:
                wait_until_blocked(schema, pid, replacement)
            finally:
                writer.commit()
            error = replacement.outcome()
            assert isinstance(error, ReportingOwnershipMigrationError)
            assert "contains rows" in str(error)
            caller.rollback()
        with connect(schema) as check:
            assert check.execute("SELECT id FROM reporting_late_evidence").fetchall() == [(1,)]


def test_concurrent_replacements_serialize_on_the_schema_lock():
    require_driver("psycopg")
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
        with connect(schema) as first, connect(schema) as second:
            replace_empty_legacy_reporting_sync(first)
            # ``first`` holds the advisory and table locks until it commits.
            replacement = Background(lambda: replace_empty_legacy_reporting_sync(second))
            replacement.start()
            try:
                wait_until_blocked(schema, second.info.backend_pid, replacement)
            finally:
                first.commit()
            error = replacement.outcome()
            assert isinstance(error, ReportingOwnershipMigrationError)
            assert "unmigrated" in str(error)
            second.rollback()
            assert surface(second) == 0


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_snapshot_isolation_refuses_and_keeps_the_outer_transaction(isolation):
    require_driver("psycopg")
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute("CREATE TABLE application_data(id integer)")
            legacy = surface(setup)
        with connect(schema) as caller:
            caller.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            caller.execute("INSERT INTO application_data VALUES (1)")
            with pytest.raises(ReportingOwnershipMigrationError, match="READ COMMITTED"):
                replace_empty_legacy_reporting_sync(caller)
            caller.commit()
            assert surface(caller) == legacy
            assert caller.execute("SELECT * FROM application_data").fetchall() == [(1,)]


def test_async_and_unknown_connections_are_refused():
    psycopg = pytest.importorskip("psycopg")

    class Unknown:
        autocommit = False

    with pytest.raises(TypeError, match="sync psycopg 3 or psycopg2"):
        replace_empty_legacy_reporting_sync(Unknown())
    # An AsyncConnection is never driven synchronously, and no loop is started.
    async_connection = object.__new__(psycopg.AsyncConnection)
    with pytest.raises(TypeError, match="async variant"):
        replace_empty_legacy_reporting_sync(async_connection)


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_autocommit_connection_inside_an_open_transaction_uses_a_savepoint(driver):
    require_driver(driver)
    with isolated_schema() as schema:
        with connect(schema, autocommit=True) as setup:
            install_legacy(setup)
            setup.execute("CREATE TABLE application_data(id integer)")
            legacy = surface(setup)
        if driver == "psycopg":
            with connect(schema, autocommit=True) as caller:
                with pytest.raises(RuntimeError, match="later step"):
                    with caller.transaction():
                        caller.execute("INSERT INTO application_data VALUES (1)")
                        replace_empty_legacy_reporting_sync(caller)
                        raise RuntimeError("a later step failed")
        else:
            import psycopg2

            caller = psycopg2.connect(database_url(), options=f"-csearch_path={schema}")
            try:
                caller.autocommit = True
                with caller.cursor() as cursor:
                    cursor.execute("BEGIN")
                    cursor.execute("INSERT INTO application_data VALUES (1)")
                    replace_empty_legacy_reporting_sync(caller)
                    cursor.execute("ROLLBACK")
            finally:
                caller.close()
        with connect(schema) as check:
            # The helper neither committed the caller's transaction nor its own drop.
            assert surface(check) == legacy
            assert check.execute("SELECT * FROM application_data").fetchall() == []
