"""Atomic direct/hopwise installs, historical upgrades, and complete readiness."""

from __future__ import annotations

import asyncio
from importlib.resources import files

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import PgReportingOutbox, ReportingNotificationError
from adcp.reporting.outbox._schema import SCHEMA_CONTRACT, schema_contract, validate_schema

from ._generation_support import NOW, isolated_reporting_pool, revision_for
from ._reconciliation_support import scenario
from ._reliable_support import Barrier
from .test_reporting_reconciliation_migration import FIXTURES

RESOURCES = files("adcp.reporting.ledger")
CHAIN = (
    "reporting_ledger.sql",
    "reporting_ledger_account_generations.sql",
    "reporting_ledger_obligation_currency.sql",
    "reporting_ledger_reconciliation.sql",
    "reporting_notification_outbox.sql",
)


async def foundation(pool):
    async with pool.connection() as conn, conn.transaction():
        for name in CHAIN[:-1]:
            await conn.execute(RESOURCES.joinpath(name).read_text())


async def retained_physical_rows(pool):
    """Include MVCC identity: no migration rewrite/backfill of retained evidence."""
    from psycopg import sql

    tables = (
        "reporting_configurations",
        "reporting_obligations",
        "reporting_revisions",
        "reporting_revision_rows",
        "reporting_adjustments",
        "reporting_consumer_statuses",
        "reporting_issue_lifecycle",
        "reporting_ledger_changes",
        "reporting_reconciliation_records",
        "reporting_reconciliation_changes",
        "reporting_reconciliation_heads",
        "reporting_receipt_heads",
    )
    result = {}
    async with pool.connection() as conn:
        for table in tables:
            result[table] = await (
                await conn.execute(
                    sql.SQL(
                        "SELECT t.ctid::text, t.xmin::text, to_jsonb(t) FROM {} t ORDER BY t.ctid"
                    ).format(sql.Identifier(table))
                )
            ).fetchall()
    return result


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("installation", ["direct", "full_chain"])
async def test_populated_pre_outbox_upgrade_never_rewrites_or_backfills(autocommit, installation):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await foundation(pool)
        old = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        s = await scenario(old)
        await old.commit_materialization(s.outcome)
        await old.record_revision_receipt(s.receipt)
        await old.ensure_issue_opened(
            account_id="acct_a", consumer_id="buyer", issue_key="opaque-legacy", observed_at=NOW
        )
        before = await retained_physical_rows(pool)
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW, notifications=True)
        if installation == "direct":
            async with pool.connection() as conn:
                await conn.execute(RESOURCES.joinpath(CHAIN[-1]).read_text())
        else:
            await store.create_schema()
        assert await retained_physical_rows(pool) == before
        outbox = PgReportingOutbox(pool=pool, clock=lambda: NOW)
        assert await outbox.list_events(account_id="acct_a") == ()
        assert await outbox.read_status_dirty(account_id="acct_a") == ()
        async with pool.connection() as conn:
            await validate_schema(conn)
        # Repeated installation neither rebuilds constraints nor changes MVCC rows.
        await store.create_schema()
        assert await retained_physical_rows(pool) == before
        revision, rows = revision_for(s.obligation, suffix="post-upgrade")
        await store.commit_revision(revision, rows)
        restarted = PgReportingOutbox(pool=pool, clock=lambda: NOW)
        assert len(await restarted.list_events(account_id="acct_a")) == 1


@pytest.mark.parametrize(
    "source",
    ["reporting_ledger_beta15.sql", "reporting_ledger_1169.sql", "reporting_ledger_1171.sql"],
)
@pytest.mark.parametrize("hopwise", [False, True])
async def test_direct_and_hopwise_historical_schema_chain(source, hopwise):
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pool.connection() as conn:
            await conn.execute((FIXTURES / source).read_text())
            await conn.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            if hopwise:
                for name in CHAIN[1:]:
                    await conn.execute(RESOURCES.joinpath(name).read_text())
        await PgReportingReconciliationStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            assert await schema_contract(conn) == SCHEMA_CONTRACT
        assert await PgReportingOutbox(pool=pool).list_events(account_id="acct_a") == ()


@pytest.mark.parametrize("autocommit", [False, True])
async def test_concurrent_repeated_install_from_independent_pools(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(
            pool.conninfo, kwargs=pool.kwargs, min_size=2, max_size=6, open=False
        ) as other:
            await other.wait(timeout=10)
            gate = asyncio.Event()

            async def install(selected):
                await gate.wait()
                await PgReportingReconciliationStore(pool=selected).create_schema()

            tasks = [asyncio.create_task(install(selected)) for selected in (pool, other) * 3]
            gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 20)
            async with other.connection() as conn:
                await validate_schema(conn)
            await PgReportingReconciliationStore(pool=other).create_schema()


async def test_interrupted_autocommit_install_is_invisible_and_restart_converges():
    async with isolated_reporting_pool(autocommit=True) as pool:
        from psycopg_pool import AsyncConnectionPool

        await foundation(pool)
        gate = Barrier()
        async with AsyncConnectionPool(
            pool.conninfo, kwargs=pool.kwargs, min_size=2, max_size=2, open=False
        ) as observer:
            await observer.wait(timeout=10)

            async def install_then_rollback():
                async with pool.connection() as conn, conn.transaction():
                    await conn.execute(RESOURCES.joinpath(CHAIN[-1]).read_text())
                    await gate.pause()
                    raise OSError("injected precommit interruption")

            task = asyncio.create_task(install_then_rollback())
            await gate.wait()
            async with observer.connection() as conn:
                assert (
                    await (
                        await conn.execute("SELECT to_regclass('reporting_notification_events')")
                    ).fetchone()
                )[0] is None
            gate.release()
            with pytest.raises(OSError):
                await task
            async with observer.connection() as conn:
                assert (
                    await (
                        await conn.execute("SELECT to_regclass('reporting_notification_events')")
                    ).fetchone()
                )[0] is None
            await PgReportingReconciliationStore(pool=observer).create_schema()
            async with observer.connection() as conn:
                await validate_schema(conn)


@pytest.mark.parametrize(
    "damage",
    [
        (
            "ALTER TABLE reporting_notification_events"
            " DISABLE TRIGGER reporting_notification_immutable"
        ),
        "ALTER TABLE reporting_status_dirty ALTER COLUMN cause_generation DROP NOT NULL",
        "DROP INDEX reporting_notification_deliveries_due",
        "ALTER TABLE reporting_notification_deliveries DROP COLUMN body_sha256",
        (
            "ALTER TABLE reporting_notification_expansions"
            " DROP CONSTRAINT reporting_notification_expansions_account_id_consumer_namespa_fkey"
        ),
        (
            "ALTER TABLE reporting_reconciliation_records"
            " DISABLE TRIGGER reporting_reconciliation_guard"
        ),
        (
            "ALTER TABLE reporting_configurations"
            " DROP CONSTRAINT reporting_configurations_pkey CASCADE"
        ),
        (
            "CREATE OR REPLACE FUNCTION reporting_notification_immutable() RETURNS TRIGGER"
            " LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
        ),
        "DROP TABLE reporting_restatement_checkpoints",
        "ALTER TABLE reporting_restatement_checkpoints ALTER COLUMN next_observation DROP NOT NULL",
        "DROP INDEX reporting_restatement_checkpoints_account_idx",
        (
            "ALTER TABLE reporting_restatement_checkpoints"
            " DROP CONSTRAINT reporting_restatement_checkpoints_next_observation_check"
        ),
    ],
)
async def test_readiness_validates_the_installed_chain_not_table_presence(damage):
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingReconciliationStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            # PostgreSQL truncates generated FK names. Resolve the named table's
            # FK in the one vector concerned with composite namespace binding.
            if "DROP CONSTRAINT reporting_notification_expansions_" in damage:
                from psycopg import sql

                name = (
                    await (
                        await conn.execute(
                            "SELECT conname FROM pg_constraint WHERE contype = 'f'"
                            " AND conrelid = 'reporting_notification_expansions'::regclass"
                        )
                    ).fetchone()
                )[0]
                await conn.execute(
                    sql.SQL(
                        "ALTER TABLE reporting_notification_expansions DROP CONSTRAINT {}"
                    ).format(sql.Identifier(name))
                )
            else:
                await conn.execute(damage)
            before = await schema_contract(conn)
            with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
                await validate_schema(conn)
            assert await schema_contract(conn) == before


async def test_malformed_outbox_upgrade_rolls_back_entire_chain():
    async with isolated_reporting_pool(autocommit=True) as pool:
        import psycopg

        async with pool.connection() as conn:
            await conn.execute((FIXTURES / "reporting_ledger_beta15.sql").read_text())
            await conn.execute("CREATE TABLE reporting_notification_events (adopter_marker text)")
        with pytest.raises(psycopg.Error):
            await PgReportingReconciliationStore(pool=pool).create_schema()
        async with pool.connection() as conn:
            assert (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM pg_attribute"
                        " WHERE attrelid = 'reporting_obligations'::regclass"
                        " AND attname = 'currency' AND NOT attisdropped"
                    )
                ).fetchone()
            )[0] == 0


async def test_default_off_upgrade_preserves_adopter_index_and_opt_in_checks_readiness():
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingReconciliationStore(pool=pool)
        await store.create_schema()
        async with pool.connection() as conn:
            await conn.execute(
                "CREATE INDEX adopter_configuration_lookup"
                " ON reporting_configurations (account_id, delivery_config_id)"
            )
            original = (
                await (
                    await conn.execute("SELECT 'adopter_configuration_lookup'::regclass::oid")
                ).fetchone()
            )[0]
        # Existing Core startup retains its compatibility with adopter indexes.
        await store.create_schema()
        async with pool.connection() as conn:
            retained = (
                await (
                    await conn.execute("SELECT 'adopter_configuration_lookup'::regclass::oid")
                ).fetchone()
            )[0]
            assert retained == original
        # Enabling the outbox invokes the full conservative SDK chain check.
        from adcp.reporting.outbox import PgReportingOutbox

        with pytest.raises(ReportingNotificationError, match="notification_schema_unready"):
            await PgReportingOutbox(pool=pool).create_schema()
