"""Upgrade literal beta.15 tables and retained evidence on real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adcp.reporting.ledger.pg import PgReportingLedgerStore
from tests.conformance.reporting._generation_support import (
    isolated_reporting_pool,
)

from ._generation_support import require_rolling_database

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_BETA15_SCHEMA = _FIXTURES / "reporting_ledger_beta15.sql"
_BETA15_DATA = _FIXTURES / "reporting_ledger_beta15_data.sql"
_MIGRATION = files("adcp.reporting.ledger").joinpath("reporting_ledger_account_generations.sql")
_TABLES = (
    "reporting_configurations",
    "reporting_obligations",
    "reporting_revisions",
    "reporting_revision_rows",
    "reporting_adjustments",
    "reporting_consumer_statuses",
    "reporting_issue_lifecycle",
    "reporting_ledger_changes",
)


def test_upgrade_fixture_is_the_literal_beta15_schema() -> None:
    # v8.0.0-beta.15:src/adcp/reporting/ledger/reporting_ledger.sql, byte for
    # byte. Do not synthesize an "old" schema by editing the current DDL: that
    # would allow future upgrades to pass without ever seeing released tables.
    assert hashlib.sha256(_BETA15_SCHEMA.read_bytes()).hexdigest() == (
        "00b3dd643cf338a2a8ffb1ac98b3acd49ca6666b82a76b8f5c551f557c968371"
    )


async def _load_beta15(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        await connection.execute(_BETA15_SCHEMA.read_text())
        await connection.execute(_BETA15_DATA.read_text())


async def _raw_upgrade(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as connection:
        await connection.execute(_MIGRATION.read_text())


async def _retained_rows(pool: AsyncConnectionPool) -> dict[str, list[Any]]:
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
            if table == "reporting_configurations":
                # The durable period-close fairness turn is additive and
                # defaulted. Dropping it only while it still holds the
                # never-leased default keeps every byte of the pre-existing
                # evidence under comparison: an upgrade that gave a retained
                # generation a non-zero turn would still fail here.
                for record in result[table]:
                    if record.get("lease_turn") == 0:
                        record.pop("lease_turn", None)
            if table == "reporting_obligations":
                # #1171 adds an explicit unknown currency. This test still
                # compares every byte of the pre-existing #1169 evidence.
                for record in result[table]:
                    if record.get("currency") is None:
                        record.pop("currency", None)
            if table == "reporting_revisions":
                for record in result[table]:
                    if record.get("canonical_content_digest") is None:
                        record.pop("canonical_content_digest", None)
                    if record.get("managed_control_totals") is None:
                        record.pop("managed_control_totals", None)
            if table == "reporting_adjustments":
                for record in result[table]:
                    if record.get("managed_control_total_deltas") is None:
                        record.pop("managed_control_total_deltas", None)
    return result


async def _primary_key(pool: AsyncConnectionPool) -> tuple[Any, ...]:
    async with pool.connection() as connection:
        row = await (
            await connection.execute(
                "SELECT conname, oid, conindid, pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'reporting_configurations'::regclass AND contype = 'p'"
            )
        ).fetchone()
    assert row is not None
    return row


async def _other_constraints(pool: AsyncConnectionPool) -> list[Any]:
    """Keep every unrelated constraint/index, including its physical identity."""
    async with pool.connection() as connection:
        constraints = await (
            await connection.execute(
                "SELECT pg_constraint.oid, conname, pg_get_constraintdef(pg_constraint.oid)"
                " FROM pg_constraint"
                " JOIN pg_class ON pg_class.oid = conrelid"
                " WHERE connamespace = current_schema()::regnamespace"
                " AND relname = ANY(%s)"
                " AND NOT (conrelid = 'reporting_configurations'::regclass AND contype = 'p')"
                " AND conname <> 'reporting_obligations_currency_code'"
                " ORDER BY pg_constraint.oid",
                (list(_TABLES),),
            )
        ).fetchall()
        indexes = await (
            await connection.execute(
                "SELECT indexrelid, pg_get_indexdef(indexrelid) FROM pg_index"
                " JOIN pg_class ON pg_class.oid = indrelid"
                " WHERE relnamespace = current_schema()::regnamespace"
                " AND relname = ANY(%s)"
                " AND NOT (indrelid = 'reporting_configurations'::regclass AND indisprimary)"
                " ORDER BY indexrelid",
                (list(_TABLES),),
            )
        ).fetchall()
    return [constraints, indexes]


async def test_beta15_boot_fails_closed_and_preserves_all_evidence() -> None:
    from adcp.reporting.migration import ReportingOwnershipMigrationError

    async with isolated_reporting_pool() as pool:
        await _load_beta15(pool)
        before = await _retained_rows(pool)
        results = await asyncio.gather(
            *(PgReportingLedgerStore(pool=pool).create_schema() for _ in range(4)),
            return_exceptions=True,
        )
        assert all(isinstance(r, ReportingOwnershipMigrationError) for r in results)
        assert await _retained_rows(pool) == before


@pytest.mark.parametrize("autocommit", [False, True])
async def test_concurrent_bootstrap_creates_the_owned_key(autocommit: bool) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await asyncio.gather(*(PgReportingLedgerStore(pool=pool).create_schema() for _ in range(8)))
        primary = await _primary_key(pool)
        assert (
            primary[3]
            == "PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version)"
        )
        await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _primary_key(pool) == primary


async def test_standalone_key_upgrade_requires_explicit_maintenance() -> None:
    require_rolling_database()
    from psycopg.errors import RaiseException

    async with isolated_reporting_pool(autocommit=True) as pool:
        await _load_beta15(pool)
        before = await _retained_rows(pool)
        with pytest.raises(RaiseException, match="stopped-worker maintenance"):
            await _raw_upgrade(pool)
        assert await _retained_rows(pool) == before


@pytest.mark.parametrize(
    "key_change",
    [
        'RENAME CONSTRAINT reporting_configurations_pkey TO "adopter key"',
        "DROP CONSTRAINT reporting_configurations_pkey, ADD PRIMARY "
        "KEY(account_id,delivery_config_id)",
    ],
)
async def test_legacy_key_variants_do_not_authorize_implicit_ownership(key_change: str) -> None:
    from adcp.reporting.migration import ReportingOwnershipMigrationError

    async with isolated_reporting_pool() as pool:
        await _load_beta15(pool)
        async with pool.connection() as c:
            await c.execute("ALTER TABLE reporting_configurations " + key_change)
        before = await _retained_rows(pool)
        with pytest.raises(ReportingOwnershipMigrationError):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _retained_rows(pool) == before


async def test_adopter_foreign_keys_survive_refused_implicit_upgrade() -> None:
    from adcp.reporting.migration import ReportingOwnershipMigrationError

    async with isolated_reporting_pool() as pool:
        await _load_beta15(pool)
        async with pool.connection() as c:
            await c.execute(
                "CREATE TABLE adopter_reference(config_id TEXT,version INTEGER,FOREIGN "
                "KEY(config_id,version) REFERENCES "
                "reporting_configurations(delivery_config_id,delivery_config_version))"
            )
            await c.execute("INSERT INTO adopter_reference VALUES('daily',1)")
        before = await _retained_rows(pool)
        with pytest.raises(ReportingOwnershipMigrationError):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _retained_rows(pool) == before
        async with pool.connection() as c:
            assert await (await c.execute("SELECT * FROM adopter_reference")).fetchall() == [
                ("daily", 1)
            ]
