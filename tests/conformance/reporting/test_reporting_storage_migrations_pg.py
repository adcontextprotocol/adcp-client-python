"""Versioned migrations and ``postgres_storage`` against real PostgreSQL (AdCP 3.3)."""

from __future__ import annotations

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
        assert "pending: 0001, 0002, 0003, 0004, 0005" in str(error)
        assert "python -m adcp migrate" in str(error)
        assert "0002 is a stopped-worker upgrade" in str(error)
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
        assert "5 pending" in status
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
            assert await engine.list_principals() == [PrincipalRef(None, AGENCY)]
        finally:
            await service.close()
