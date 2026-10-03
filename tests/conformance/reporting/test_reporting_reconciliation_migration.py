"""Literal beta.15/#1171 upgrades, database enforcement, and restart persistence."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from adcp.reporting.ledger import (
    LedgerConflictError,
    PgReportingReconciliationStore,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingMaterializationCheck,
)
from adcp.reporting.ledger import ReportingStatusCaller as OwnershipCaller

from ._generation_support import NOW, isolated_reporting_pool, require_rolling_database
from ._reconciliation_support import scenario
from .test_reporting_currency_migration import retained

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RESOURCES = files("adcp.reporting.ledger")
MIGRATION = RESOURCES.joinpath("reporting_ledger_reconciliation.sql")


def test_literal_stacked_schema_fixture() -> None:
    # Literal #1175 bootstrap, unchanged at corrected #1171 head ff584b6f.
    assert hashlib.sha256((FIXTURES / "reporting_ledger_1171.sql").read_bytes()).hexdigest() == (
        "65d9b44e220f1828b48beb32a44334b956fbf27081bed72390de1c526e67df4e"
    )


@pytest.mark.parametrize("source", ["reporting_ledger_beta15.sql", "reporting_ledger_1171.sql"])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_upgrade_preserves_history_and_is_concurrent_idempotent(
    source: str, autocommit: bool
) -> None:
    require_rolling_database()
    from psycopg import sql

    from adcp.reporting.migration import ReportingOwnershipMigrationError, migrate_legacy_reporting

    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / source).read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
        before = await retained(pool)
        stores = [PgReportingReconciliationStore(pool=pool, clock=lambda: NOW) for _ in range(6)]
        results = await asyncio.gather(*(s.create_schema() for s in stores), return_exceptions=True)
        assert all(isinstance(result, ReportingOwnershipMigrationError) for result in results)
        assert await retained(pool) == before
        archive = "adcp_reporting_quarantine_reconciliation"
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
                assert (
                    await stores[0].list_configurations(caller=OwnershipCaller("acct_a", "buyer"))
                    == ()
                )
                fresh = await scenario(stores[0], account_id="new-account")
                await stores[0].commit_materialization(fresh.outcome)
                receipt, _ = await stores[0].record_revision_receipt(fresh.receipt)
                assert await stores[1].get_receipt(receipt.key) == receipt
            finally:
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
                )


@pytest.mark.parametrize("autocommit", [False, True])
async def test_failed_extension_upgrade_rolls_back_prerequisites(autocommit: bool) -> None:
    pytest.importorskip("psycopg")
    from adcp.reporting.migration import ReportingOwnershipMigrationError

    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_beta15.sql").read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            await connection.execute(
                "CREATE TABLE reporting_reconciliation_records (adopter_marker TEXT)"
            )
        before = await retained(pool)
        with pytest.raises(ReportingOwnershipMigrationError):
            await PgReportingReconciliationStore(pool=pool).create_schema()
        assert await retained(pool) == before
        async with pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT count(*) FROM pg_attribute"
                    " WHERE attrelid = 'reporting_revisions'::regclass"
                    " AND attname = 'canonical_content_digest' AND NOT attisdropped"
                )
            ).fetchone()
            assert row[0] == 0


async def test_new_evidence_and_acceptance_are_database_immutable() -> None:
    pytest.importorskip("psycopg")
    import psycopg

    async with isolated_reporting_pool() as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store)
        await store.commit_materialization(s.outcome)
        receipt, _ = await store.record_revision_receipt(s.receipt)
        for statement in [
            "UPDATE reporting_reconciliation_records SET payload = '{}'::jsonb",
            "DELETE FROM reporting_reconciliation_records",
            "UPDATE reporting_receipt_heads SET receipt_status = 'rejected'",
            "DELETE FROM reporting_receipt_heads",
            "UPDATE reporting_revisions SET canonical_content_digest = NULL",
            "UPDATE reporting_revisions SET managed_control_totals = NULL",
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(statement)
        assert await store.get_receipt(receipt.key) == receipt


async def test_accepted_records_survive_all_application_connections_closing() -> None:
    pytest.importorskip("psycopg_pool")
    require_rolling_database()
    from psycopg_pool import AsyncConnectionPool

    async with isolated_reporting_pool() as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store)
        await store.commit_materialization(s.outcome)
        receipt, _ = await store.record_revision_receipt(s.receipt)
        first_page = await store.read_reconciliation_changes(caller=s.binding.principal, limit=2)
        assert first_page.has_more
        async with pool.connection() as connection:
            schema = (await (await connection.execute("SELECT current_schema()")).fetchone())[0]
        await pool.close()
        async with AsyncConnectionPool(
            os.environ["ADCP_PG_TEST_URL"],
            kwargs={"options": f"-csearch_path={schema}"},
            open=False,
        ) as restarted_pool:
            await restarted_pool.wait()
            restarted = PgReportingReconciliationStore(pool=restarted_pool, clock=lambda: NOW)
            assert await restarted.record_revision_receipt(s.receipt) == (receipt, False)
            assert await restarted.get_receipt(receipt.key) == receipt
            remainder = await restarted.read_reconciliation_changes(
                caller=s.binding.principal, cursor=first_page.cursor
            )
            assert remainder.boundary == first_page.boundary
            assert tuple(item.record for item in first_page.changes + remainder.changes) == (
                s.binding,
                s.delivery,
                s.attempt,
                s.outcome,
                receipt,
            )
            check = ReportingMaterializationCheck(
                s.attempt.scope,
                s.attempt.reporting_materialization_id,
                "after-restart-check",
                "readable",
                NOW,
            )
            await restarted.record_materialization_check(check)
            later = await restarted.read_reconciliation_changes(
                caller=s.binding.principal, changes_after=remainder.changes_checkpoint
            )
            assert tuple(item.record for item in later.changes) == (check,)
            with pytest.raises(LedgerConflictError) as error:
                await restarted.record_revision_receipt(
                    replace(s.receipt, reporting_receipt_id="receipt-after-restart-0002")
                )
            assert error.value.code == "ACCEPTED_RECEIPT_TERMINAL"


@pytest.mark.parametrize("autocommit", [False, True])
async def test_record_receipt_head_and_feed_rollback_together(autocommit: bool) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store)
        await store.commit_materialization(s.outcome)

        class FailingStore(PgReportingReconciliationStore):
            async def _append_reconciliation_change(
                self, connection: Any, record: ReportingDeliveryRecord
            ) -> None:
                await super()._append_reconciliation_change(connection, record)
                raise RuntimeError("injected transaction failure")

        failing = FailingStore(pool=pool, clock=lambda: NOW)
        before = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            await failing.record_revision_receipt(s.receipt)
        assert await store.get_receipt(s.receipt.key) is None
        after = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
        assert after.records == before.records
        assert after.boundary.max_sequence == before.boundary.max_sequence
        async with pool.connection() as connection:
            assert (
                await (
                    await connection.execute("SELECT count(*) FROM reporting_receipt_heads")
                ).fetchone()
            )[0] == 0
        assert (await store.record_revision_receipt(s.receipt))[1]


@pytest.mark.parametrize(
    "corruption",
    ["unknown_field", "nested_unknown_field", "generation_extra", "fingerprint", "missing_feed"],
)
async def test_corrupt_retained_evidence_fails_closed_without_echoing_payload(
    corruption: str,
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        s = await scenario(store)
        await store.commit_materialization(s.outcome)
        async with pool.connection() as connection:
            # Deliberately simulate damaged storage using the database-owner role.
            # The supported SDK path cannot update/delete these immutable rows.
            await connection.execute(
                "ALTER TABLE reporting_reconciliation_records DISABLE TRIGGER ALL"
            )
            if corruption == "missing_feed":
                await connection.execute(
                    "ALTER TABLE reporting_reconciliation_changes DISABLE TRIGGER ALL"
                )
                await connection.execute(
                    "DELETE FROM reporting_reconciliation_changes"
                    " WHERE record_kind = 'materialization'"
                )
                await connection.execute(
                    "ALTER TABLE reporting_reconciliation_changes ENABLE TRIGGER ALL"
                )
            elif corruption == "fingerprint":
                await connection.execute(
                    "UPDATE reporting_reconciliation_records SET content_sha256 = %s"
                    " WHERE record_kind = 'materialization'",
                    ("0" * 64,),
                )
            else:
                path = (
                    "{credential}"
                    if corruption == "unknown_field"
                    else "{verification,canonical_content_digest,credential}"
                )
                if corruption == "generation_extra":
                    path = "{scope,generation_key,credential}"
                await connection.execute(
                    "UPDATE reporting_reconciliation_records"
                    " SET payload = jsonb_set(payload, %s::text[], %s::jsonb)"
                    " WHERE record_kind = 'materialization'",
                    (path, '"MUST_NOT_RETAIN_PROVIDER_RESPONSE"'),
                )
            await connection.execute(
                "ALTER TABLE reporting_reconciliation_records ENABLE TRIGGER ALL"
            )
        for read in [
            store.get_materialization(s.attempt.key),
            store.record_revision_receipt(s.receipt),
        ]:
            with pytest.raises(LedgerConflictError) as error:
                await read
            assert error.value.code in {"INVALID_REPORTING_RECORD", "REPORTING_HISTORY_CORRUPT"}
            assert "MUST_NOT_RETAIN" not in str(error.value)
            assert error.value.__cause__ is None and error.value.__context__ is None


async def initial_retained_state(pool, *, kind="revision_receipt", write_change=True):
    """Literal pre-ownership Core and reconciliation, including opaque evidence."""
    async with pool.connection() as connection:
        await connection.execute((FIXTURES / "reporting_ledger_1171.sql").read_text())
        await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
        await connection.execute(
            (FIXTURES / "reporting_ledger_reconciliation_initial.sql").read_text()
        )
        await connection.execute(
            "INSERT INTO reporting_reconciliation_records "
            "(account_id,consumer_id,namespace,record_id,record_kind,delivery_config_id,"
            "delivery_config_version,reporting_obligation_id,reporting_revision_id,"
            "receipt_chain_key,receipt_status,payload,content_sha256,change_id) "
            "VALUES ('acct_a','buyer',%s,'retained-record',%s,'daily',1,'rpo_acct_a',"
            "'rpr_acct_a_official',%s,%s,jsonb_build_object('kind',%s::text,'opaque','original'),%s,"
            "'retained-change')",
            (
                "receipt" if kind == "revision_receipt" else kind,
                kind,
                "retained-chain" if kind == "revision_receipt" else None,
                "accepted" if kind == "revision_receipt" else None,
                kind,
                "a" * 64,
            ),
        )
        if kind == "revision_receipt":
            await connection.execute(
                "INSERT INTO reporting_receipt_heads(account_id,consumer_id,chain_key,receipt_id,"
                "receipt_status) VALUES ('acct_a','buyer','retained-chain','retained-record','accepted')"
            )
        if write_change:
            await connection.execute(
                "INSERT INTO reporting_ledger_changes(account_id,record_kind,record_id) "
                "VALUES ('acct_a',%s,'retained-change')",
                (kind,),
            )
        before = {}
        for table in (
            "reporting_reconciliation_records",
            "reporting_receipt_heads",
            "reporting_ledger_changes",
        ):
            before[table] = await (
                await connection.execute(
                    f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text"
                )
            ).fetchall()
        return before


async def assert_retained_quarantine(pool, before):
    require_rolling_database()
    from psycopg import sql

    from adcp.reporting.migration import ReportingOwnershipMigrationError, migrate_legacy_reporting

    store = PgReportingReconciliationStore(pool=pool)
    with pytest.raises(ReportingOwnershipMigrationError):
        await store.create_schema()
    archive = "adcp_reporting_quarantine_initial"
    async with pool.connection() as connection:
        await migrate_legacy_reporting(connection, archive_schema=archive, workers_stopped=True)
        try:
            for table, rows in before.items():
                retained = await (
                    await connection.execute(
                        sql.SQL(
                            "SELECT to_jsonb(t) FROM {}.{} t ORDER BY to_jsonb(t)::text"
                        ).format(sql.Identifier(archive), sql.Identifier(table))
                    )
                ).fetchall()
                assert retained == rows
            # The old receipt's consumer field is not proof of generation ownership.
            assert await store.list_configurations(caller=OwnershipCaller("acct_a", "buyer")) == ()
            assert (
                await store.read_reconciliation_changes(
                    caller=ReportingDeliveryPrincipal("acct_a", "buyer")
                )
                is not None
            )
            assert (
                await (
                    await connection.execute(
                        "SELECT count(*) FROM reporting_reconciliation_records"
                    )
                ).fetchone()
            )[0] == 0
        finally:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
            )


@pytest.mark.parametrize("autocommit", [False, True])
async def test_initial_reconciliation_records_heads_and_changes_stay_quarantined(autocommit):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        await assert_retained_quarantine(pool, await initial_retained_state(pool))


@pytest.mark.parametrize("damage", ["missing_attempt", "missing_feed"])
async def test_initial_orphaned_outcome_is_archived_without_repair_or_replay(damage):
    async with isolated_reporting_pool() as pool:
        before = await initial_retained_state(
            pool, kind="materialization", write_change=damage != "missing_feed"
        )
        await assert_retained_quarantine(pool, before)
