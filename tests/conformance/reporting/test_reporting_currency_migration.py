"""No invented currency when upgrading literal beta.15 and #1169 ledgers."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adcp.reporting.ledger import (
    ProducerOfferings,
    ReportingProducer,
    ReportingStatusCaller,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore

from ._generation_support import (
    NOW,
    UncalledSource,
    configuration,
    isolated_reporting_pool,
    require_rolling_database,
)
from .test_reporting_generation_migration import _TABLES, _primary_key

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MIGRATION = files("adcp.reporting.ledger").joinpath("reporting_ledger_obligation_currency.sql")
ACCOUNT_MIGRATION = files("adcp.reporting.ledger").joinpath(
    "reporting_ledger_account_generations.sql"
)


def test_1169_schema_fixture_is_the_reviewed_stacked_base() -> None:
    # 91fa2786efdd3117ce2c1a875ede7431767d7e87, byte for byte.
    assert hashlib.sha256((FIXTURES / "reporting_ledger_1169.sql").read_bytes()).hexdigest() == (
        "6617d1c840b50530266f5126ec20f86c4b032735f51ecc78f983db5ac070a911"
    )


async def retained(pool: AsyncConnectionPool) -> dict[str, list[Any]]:
    require_rolling_database()
    from psycopg import sql

    result = {}
    async with pool.connection() as connection:
        for table in _TABLES:
            rows = await (
                await connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY to_jsonb(t)::text").format(
                        sql.Identifier(table)
                    )
                )
            ).fetchall()
            result[table] = [row[0] for row in rows]
    return result


async def raw_upgrade(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(ACCOUNT_MIGRATION.read_text())
            await connection.execute(MIGRATION.read_text())


@pytest.mark.parametrize("schema", ["reporting_ledger_beta15.sql", "reporting_ledger_1169.sql"])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_migration_preserves_evidence_and_quarantines_unknown_currency(
    schema: str,
    autocommit: bool,
) -> None:
    require_rolling_database()
    from psycopg import sql

    from adcp.reporting.migration import (
        ReportingOwnershipMigrationError,
        migrate_legacy_reporting,
    )

    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / schema).read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
        before = await retained(pool)
        primary = await _primary_key(pool)
        with pytest.raises(ReportingOwnershipMigrationError, match="Stop reporting workers"):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await retained(pool) == before
        assert await _primary_key(pool) == primary
        archive = "adcp_reporting_quarantine_currency"
        async with pool.connection() as connection:
            await migrate_legacy_reporting(connection, archive_schema=archive, workers_stopped=True)
            try:
                for table, rows in before.items():
                    archived = await (
                        await connection.execute(
                            sql.SQL(
                                "SELECT to_jsonb(t) FROM {}.{} t ORDER BY to_jsonb(t)::text"
                            ).format(sql.Identifier(archive), sql.Identifier(table))
                        )
                    ).fetchall()
                    assert [r[0] for r in archived] == rows
                store = PgReportingLedgerStore(pool=pool)
                assert (
                    await store.list_configurations(caller=ReportingStatusCaller("acct_a", "buyer"))
                    == ()
                )
            finally:
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
                )


async def test_currency_constraints_and_immutability_cover_direct_sql() -> None:
    async with isolated_reporting_pool() as pool:
        from psycopg.errors import CheckViolation

        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        producer = ReportingProducer(
            store=store,
            source=UncalledSource(),
            offerings=ProducerOfferings(currency="EUR"),
            clock=lambda: NOW,
        )
        config = configuration()
        await store.put_configuration(config)
        (obligation,) = await producer.close_elapsed_periods(config)
        for value in ("USD", None, "eur", "EUR\n", "ÅBC", "US1", "EURO"):
            with pytest.raises(CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(
                        "UPDATE reporting_obligations SET currency = %s", (value,)
                    )
        async with pool.connection() as connection:
            await connection.execute("UPDATE reporting_obligations SET currency = currency")
        assert (
            await store.get_obligation(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            == obligation
        )
        for value in ("eur", "EUR\n", "ÅBC", "US1", "EURO"):
            with pytest.raises(CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(
                        "INSERT INTO reporting_obligations SELECT (jsonb_populate_record("
                        "NULL::reporting_obligations, to_jsonb(o) || jsonb_build_object("
                        "'currency', %s::text, 'account_id', 'invalid', "
                        "'reporting_obligation_id', 'invalid'))).*"
                        " FROM reporting_obligations o",
                        (value,),
                    )


async def test_unexpected_default_rolls_back_without_rewriting_evidence() -> None:
    async with isolated_reporting_pool() as pool:
        from adcp.reporting.migration import ReportingOwnershipMigrationError

        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_beta15.sql").read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            await connection.execute(
                "ALTER TABLE reporting_obligations ADD COLUMN currency TEXT DEFAULT 'USD'"
            )
        before = await retained(pool)
        primary = await _primary_key(pool)
        with pytest.raises(ReportingOwnershipMigrationError, match="Stop reporting workers"):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await retained(pool) == before
        assert await _primary_key(pool) == primary
