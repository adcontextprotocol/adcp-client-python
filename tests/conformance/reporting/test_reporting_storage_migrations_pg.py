"""Versioned migrations and ``postgres_storage`` against real PostgreSQL (AdCP 3.3)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from adcp.reporting._errors import ReliableReportingConfigurationError
from tests.conformance.reporting._generation_support import isolated_reporting_pool


@pytest.mark.asyncio
async def test_migrate_applies_every_version_once_and_status_reads_it() -> None:
    from adcp_reporting_storage.migrations import (
        describe,
        migrate,
        migration_status,
        migrations,
        pending_migrations,
    )

    async with isolated_reporting_pool(autocommit=True) as pool:
        assert [item.version for item in await pending_migrations(pool)] == [
            item.version for item in migrations()
        ]
        planned = await migrate(pool, dry_run=True)
        assert planned and await pending_migrations(pool) == planned  # a dry run changes nothing
        applied = await migrate(pool)
        assert [item.version for item in applied] == [item.version for item in migrations()]
        assert await pending_migrations(pool) == []
        assert await migrate(pool) == []  # idempotent
        lines = describe(await migration_status(pool))
        assert all(": applied " in line for line in lines)
        assert lines[1].endswith("[stopped-worker upgrade]")


@pytest.mark.asyncio
async def test_storage_refuses_a_schema_that_is_behind_and_starts_once_migrated() -> None:
    from adcp_reporting_storage.migrations import migrate

    from adcp.storage.postgres import postgres_storage

    async with isolated_reporting_pool(autocommit=True) as pool:
        storage = postgres_storage(pool=pool)
        assert storage.durable
        assert storage.check_schema is not None
        with pytest.raises(ReliableReportingConfigurationError) as refused:
            await storage.check_schema()
        error = refused.value
        assert error.code == "REPORTING_SCHEMA_BEHIND" and error.recovery == "correctable"
        assert "pending: 0001, 0002, 0003, 0004, 0005, 0006" in str(error)
        assert "python -m adcp migrate" in str(error)
        assert "0002, 0006 are stopped-worker upgrades" in str(error)
        await migrate(pool)
        await storage.check_schema()


@pytest.mark.asyncio
async def test_the_subscription_stores_list_principals_and_declare_durability() -> None:
    from adcp_reporting_storage.migrations import migrate

    from adcp.storage.postgres import postgres_storage

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        storage = postgres_storage(pool=pool)
        state = storage.subscriptions
        assert state.is_durable and storage.principals.is_durable and storage.ledger.is_durable
        assert await state.list_principals() == []
        assert await state.compare_and_swap("principal-b", None, {"document_version": 1})
        assert await state.compare_and_swap("principal-a", None, {"document_version": 1})
        assert await state.list_principals() == ["principal-a", "principal-b"]


@pytest.mark.asyncio
async def test_the_cli_migrates_and_reports_status(capsys) -> None:
    import os

    from adcp_reporting_storage.migrations.cli import _run

    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as connection:
            cursor = await connection.execute("SELECT current_schema()")
            schema = (await cursor.fetchone())[0]
        base = os.environ["ADCP_PG_TEST_URL"]
        dsn = f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema}"
        assert await _run(dsn, status=True, dry_run=False) == 0
        status = capsys.readouterr().out
        assert "0002 reporting_subscription_generations: pending  [stopped-worker upgrade" in status
        assert "6 pending" in status
        assert await _run(dsn, status=False, dry_run=False) == 0
        assert "applied 0002 reporting_subscription_generations (stopped-worker upgrade)" in (
            capsys.readouterr().out
        )
        assert await _run(dsn, status=True, dry_run=False) == 0
        assert capsys.readouterr().out.strip().endswith("up to date")


@pytest.mark.asyncio
async def test_a_managed_service_runs_on_postgres_storage() -> None:
    from datetime import timedelta

    from adcp_reporting_storage.migrations import migrate

    from adcp.reporting import PrincipalRef, SubscriptionOptions
    from adcp.reporting.service import ReliableReportingService
    from adcp.reporting.testing import DeterministicReportingClock
    from adcp.storage.postgres import postgres_storage
    from tests.test_reliable_reporting_service import DEFINITION
    from tests.test_reporting_subscription_managed import (
        AGENCY,
        OFFERING,
        START,
        SUBSCRIPTION,
        Seller,
        _ByBuySource,
        _context,
        _ctx,
    )

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        clock = DeterministicReportingClock(START)
        service = ReliableReportingService(
            storage=postgres_storage(pool=pool, clock=clock),
            account_context=_context,
            clock=clock,
            read_jitter_window=timedelta(0),
            subscriptions=SubscriptionOptions(definitions={OFFERING["offering_id"]: DEFINITION}),
        )
        service.sources.register("gam", _ByBuySource(), capability_offerings=[OFFERING])
        platform = service.install(Seller())
        try:
            response = await platform.sync_principal(
                {
                    "idempotency_key": "pg-storage-key-000001",
                    "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
                },
                _ctx(),
            )
            assert response.result.kind == "applied"
            clock.set(START + timedelta(hours=3, minutes=30))
            turn = await service.run_worker()
            assert turn.subscription_errors == {}
            assert turn.subscription_obligations_written == 2
            engine = service._managed.engine
            assert await engine.list_subscription_principals() == [PrincipalRef(None, AGENCY)]
        finally:
            await service.close()


def test_no_storage_existence_check_resolves_through_the_search_path() -> None:
    """#1838: ``to_regclass('name')`` finds a table in any schema on the path.

    An existence check that does would read, skip or alter another schema's
    tables; qualify it with ``quote_ident(current_schema())``.
    """
    import re
    from pathlib import Path

    import adcp_reporting_storage

    root = Path(adcp_reporting_storage.__file__).parent
    unqualified = re.compile(r"to_regclass\(\s*'")
    found = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted(root.rglob("*"))
        if path.suffix in {".py", ".sql"}
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if unqualified.search(line)
    ]
    assert found == []


async def _schema(pool) -> str:
    async with pool.connection() as connection:
        return (await (await connection.execute("SELECT current_schema()")).fetchone())[0]


async def _catalog(pool, schema: str) -> list[tuple]:
    """Columns, constraints and recorded migrations of ``schema``."""
    async with pool.connection() as connection:
        columns = await (
            await connection.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = %s ORDER BY 1, 2",
                (schema,),
            )
        ).fetchall()
        constraints = await (
            await connection.execute(
                "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid)"
                " FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
                " WHERE n.nspname = %s ORDER BY 1, 2",
                (schema,),
            )
        ).fetchall()
        recorded = await (
            await connection.execute("SELECT version FROM adcp_schema_migrations ORDER BY 1")
        ).fetchall()
    return [*columns, *constraints, *recorded]


@asynccontextmanager
async def _search_path(*schemas: str):
    import os

    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        os.environ["ADCP_PG_TEST_URL"],
        kwargs={"options": f"-csearch_path={','.join(schemas)}", "autocommit": True},
        min_size=1,
        max_size=2,
        open=False,
    ) as pool:
        await pool.wait(timeout=10)
        yield pool


@pytest.mark.asyncio
async def test_migrate_upgrades_its_own_schema_beside_a_migrated_one() -> None:
    """#1838: a migrated schema later on the search_path is not this schema's record."""
    from adcp_reporting_storage.migrations import migrate, migrations, pending_migrations

    every = [item.version for item in migrations()]
    async with (
        isolated_reporting_pool(autocommit=True) as other,
        isolated_reporting_pool(autocommit=True) as own,
    ):
        await migrate(other)
        other_schema, own_schema = await _schema(other), await _schema(own)
        before = await _catalog(other, other_schema)
        async with _search_path(own_schema, other_schema) as shared:
            assert [item.version for item in await pending_migrations(shared)] == every
            assert [item.version for item in await migrate(shared)] == every
            assert await pending_migrations(shared) == []
        assert await pending_migrations(own) == []
        assert await _catalog(own, own_schema) == before  # the same schema, built in place
        assert await _catalog(other, other_schema) == before


@pytest.mark.asyncio
async def test_create_schema_builds_its_own_schema_beside_a_migrated_one() -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate, pending_migrations

    async with (
        isolated_reporting_pool(autocommit=True) as other,
        isolated_reporting_pool(autocommit=True) as own,
    ):
        await migrate(other)
        other_schema, own_schema = await _schema(other), await _schema(own)
        before = await _catalog(other, other_schema)
        async with _search_path(own_schema, other_schema) as shared:
            await PgReportingLedgerStore(pool=shared).create_schema()
        # create_schema() records its own 0001-0003; the rest stay for ``migrate``.
        assert [item.version for item in await pending_migrations(own)] == ["0004", "0005", "0006"]
        assert await _catalog(other, other_schema) == before
