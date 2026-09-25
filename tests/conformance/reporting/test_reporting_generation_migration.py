"""Upgrade literal beta.15 tables and retained evidence on real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adcp.reporting.ledger import LeasedConfiguration, LedgerConflictError
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from tests.conformance.reporting._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
)

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


async def test_beta15_upgrade_preserves_all_evidence_and_survives_concurrent_boots() -> None:
    async with isolated_reporting_pool() as pool:
        await _load_beta15(pool)
        before = await _retained_rows(pool)
        assert all(before.values()), "The fixture must contain evidence in every beta.15 table"
        constraints = await _other_constraints(pool)
        assert (await _primary_key(pool))[
            3
        ] == "PRIMARY KEY (delivery_config_id, delivery_config_version)"
        # An application boot and a deployment migration may race. Both entry
        # points must share a lock and converge without rebuilding the key twice.
        await asyncio.gather(
            *(PgReportingLedgerStore(pool=pool).create_schema() for _ in range(4)),
            *(_raw_upgrade(pool) for _ in range(4)),
        )
        upgraded_key = await _primary_key(pool)
        assert (
            upgraded_key[3]
            == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
        )
        assert await _retained_rows(pool) == before
        # Later additive migrations may add constraints, but cannot replace or
        # alter any of these pre-existing physical constraint/index identities.
        for original, upgraded in zip(constraints, await _other_constraints(pool)):
            assert set(original) <= set(upgraded)
        await asyncio.gather(_raw_upgrade(pool), PgReportingLedgerStore(pool=pool).create_schema())
        assert await _primary_key(pool) == upgraded_key

        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        old = replace(configuration(), required_finality="official")
        assert await store.list_configurations(account_id=old.account_id) == (old,)
        await store.put_configuration(old)  # Retained beta.15 configuration digest still replays.
        with pytest.raises(LedgerConflictError) as caught:
            await store.put_configuration(replace(old, media_buy_ids=("changed",)))
        assert caught.value.code == "CONFIGURATION_GENERATION_IMMUTABLE"
        revision = await store.get_revision(
            account_id=old.account_id, reporting_revision_id="rpr_acct_a_official"
        )
        assert revision is not None and revision.finality == "official"
        rows = await store.read_revision_rows(
            account_id=old.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert rows.rows == ({"media_buy_id": "mb_acct_a", "impressions": 5},)
        receipt_operation_1 = await store.commit_revision(revision, rows.rows)
        assert receipt_operation_1 == revision
        statuses = await store.list_consumer_statuses(
            account_id=old.account_id, consumer_id="shared-buyer"
        )
        assert len(statuses) == 1 and statuses[0].mismatch_code == "metric_missing"
        receipt_operation_2 = await store.record_consumer_status(statuses[0])
        assert receipt_operation_2 == (statuses[0], False)
        assert await _retained_rows(pool) == before  # Replays append no new feed entries.

        # Existing leases survive, and releasing an old handle must not release
        # a new tenant's same-name generation even if the worker id is reused.
        receipt_operation_3 = await store.lease_period_close(
            worker_id="extra", now=NOW, lease_seconds=60
        )
        assert receipt_operation_3 is None
        other = configuration("acct_b")
        await store.put_configuration(other)
        other_lease = await store.lease_period_close(
            worker_id="beta15-worker", now=NOW, lease_seconds=3600
        )
        assert other_lease is not None and other_lease.generation_key == other.generation_key
        old_lease = LeasedConfiguration("acct_a", "daily", 1, NOW + timedelta(hours=1))
        await store.release_period_close(old_lease, worker_id="beta15-worker")
        reclaimed = await store.lease_period_close(
            worker_id="new-worker", now=NOW, lease_seconds=60
        )
        assert reclaimed is not None and reclaimed.generation_key == old.generation_key
        receipt_operation_4 = await store.lease_period_close(
            worker_id="extra", now=NOW, lease_seconds=60
        )
        assert receipt_operation_4 is None

        # The retained feed and its sequence continue; no history is renumbered.
        checkpoint = await store.open_snapshot(account_id=old.account_id, filters_fingerprint="")
        assert checkpoint.max_sequence == 4
        await store.commit_obligation(obligation_for(other))
        new_checkpoint = await store.open_snapshot(
            account_id=other.account_id, filters_fingerprint=""
        )
        assert new_checkpoint.max_sequence == checkpoint.max_sequence + 1
        assert await store.list_configurations(account_id=old.account_id) == (old,)


@pytest.mark.parametrize("autocommit", [False, True])
async def test_concurrent_bootstrap_creates_the_account_key(autocommit: bool) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await asyncio.gather(*(PgReportingLedgerStore(pool=pool).create_schema() for _ in range(8)))
        primary = await _primary_key(pool)
        assert primary[3] == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
        await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _primary_key(pool) == primary


async def test_standalone_migration_is_atomic_in_autocommit_mode() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        await _load_beta15(pool)
        before = await _retained_rows(pool)
        await asyncio.gather(*(_raw_upgrade(pool) for _ in range(8)))
        assert (await _primary_key(pool))[
            3
        ] == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
        assert await _retained_rows(pool) == before


async def test_upgrade_recognizes_a_renamed_beta15_primary_key() -> None:
    async with isolated_reporting_pool() as pool:
        await _load_beta15(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_configurations"
                ' RENAME CONSTRAINT reporting_configurations_pkey TO "adopter key"'
            )
        before = await _retained_rows(pool)
        await PgReportingLedgerStore(pool=pool).create_schema()
        primary = await _primary_key(pool)
        assert primary[0] == "adopter key"
        assert primary[3] == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
        assert await _retained_rows(pool) == before
        await _raw_upgrade(pool)
        assert await _primary_key(pool) == primary


async def test_upgrade_refuses_an_unexpected_primary_key_without_changing_rows() -> None:
    async with isolated_reporting_pool() as pool:
        from psycopg.errors import RaiseException

        await _load_beta15(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_configurations"
                " DROP CONSTRAINT reporting_configurations_pkey,"
                " ADD PRIMARY KEY (account_id, delivery_config_id)"
            )
        primary = await _primary_key(pool)
        before = await _retained_rows(pool)
        with pytest.raises(RaiseException, match="Unexpected reporting_configurations primary key"):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _primary_key(pool) == primary
        assert await _retained_rows(pool) == before


async def test_upgrade_preserves_adopter_foreign_keys_and_rolls_back_on_failure() -> None:
    async with isolated_reporting_pool() as pool:
        from psycopg.errors import DependentObjectsStillExist

        await _load_beta15(pool)
        async with pool.connection() as connection:
            await connection.execute(
                "CREATE TABLE adopter_reference (config_id TEXT, version INTEGER,"
                " FOREIGN KEY (config_id, version) REFERENCES reporting_configurations"
                " (delivery_config_id, delivery_config_version))"
            )
            await connection.execute("INSERT INTO adopter_reference VALUES ('daily', 1)")
        primary = await _primary_key(pool)
        constraints = await _other_constraints(pool)
        before = await _retained_rows(pool)
        with pytest.raises(DependentObjectsStillExist):
            await PgReportingLedgerStore(pool=pool).create_schema()
        assert await _primary_key(pool) == primary
        assert await _other_constraints(pool) == constraints
        assert await _retained_rows(pool) == before
        async with pool.connection() as connection:
            assert await (
                await connection.execute("SELECT * FROM adopter_reference")
            ).fetchall() == [("daily", 1)]
            # Adopter-owned remediation; the SDK must never do this by CASCADE.
            await connection.execute("DROP TABLE adopter_reference")
        await PgReportingLedgerStore(pool=pool).create_schema()
        assert (await _primary_key(pool))[
            3
        ] == "PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)"
