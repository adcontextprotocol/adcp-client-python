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
from adcp.reporting.ledger._delivery_state import change_id, decode_record, receipt_chain

from ._generation_support import NOW, isolated_reporting_pool
from ._reconciliation_support import scenario
from .test_reporting_currency_migration import retained

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RESOURCES = files("adcp.reporting.ledger")
MIGRATION = RESOURCES.joinpath("reporting_ledger_reconciliation.sql")


class InitialReconciliationStore(PgReportingReconciliationStore):
    """Seed the literal initial schema through its original global-feed contract."""

    async def _records(
        self, connection: Any, who: ReportingDeliveryPrincipal, maximum: int | None = None
    ) -> tuple[ReportingDeliveryRecord, ...]:
        rows = await (
            await connection.execute(
                "SELECT r.payload FROM reporting_reconciliation_records r"
                " JOIN reporting_ledger_changes c ON c.account_id = r.account_id"
                " AND c.record_kind = r.record_kind AND c.record_id = r.change_id"
                " WHERE r.account_id = %s AND r.consumer_id = %s"
                " AND (%s::bigint IS NULL OR c.seq <= %s::bigint) ORDER BY c.seq",
                (who.account_id, who.consumer_id, maximum, maximum),
            )
        ).fetchall()
        return tuple(decode_record(row[0]) for row in rows)

    async def _append_reconciliation_change(
        self, connection: Any, record: ReportingDeliveryRecord
    ) -> None:
        from adcp.reporting.ledger._delivery_state import principal

        await connection.execute(
            "INSERT INTO reporting_ledger_changes (account_id, record_kind, record_id)"
            " VALUES (%s, %s, %s)",
            (principal(record).account_id, record.kind, change_id(record)),
        )


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
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / source).read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            if source == "reporting_ledger_1171.sql":
                # Simulate a currency explicitly inserted by the predecessor,
                # before installing its update-protection trigger.
                await connection.execute("UPDATE reporting_obligations SET currency = 'EUR'")
                await connection.execute(
                    RESOURCES.joinpath("reporting_ledger_account_generations.sql").read_text()
                )
                await connection.execute(
                    RESOURCES.joinpath("reporting_ledger_obligation_currency.sql").read_text()
                )
        before = await retained(pool)
        stores = [PgReportingReconciliationStore(pool=pool, clock=lambda: NOW) for _ in range(6)]
        await asyncio.gather(*(store.create_schema() for store in stores))
        expected = before
        for record in expected["reporting_obligations"]:
            record.setdefault("currency", None)
        for record in expected["reporting_revisions"]:
            record["canonical_content_digest"] = None
            record["managed_control_totals"] = None
        for record in expected["reporting_adjustments"]:
            record["managed_control_total_deltas"] = None
        assert await retained(pool) == expected
        async with pool.connection() as connection:
            await connection.execute(MIGRATION.read_text())
            keys_before = await (
                await connection.execute(
                    "SELECT oid, conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE connamespace = current_schema()::regnamespace ORDER BY oid"
                )
            ).fetchall()
            assert (
                await (
                    await connection.execute(
                        "SELECT count(*) FROM reporting_reconciliation_records"
                    )
                ).fetchone()
            )[0] == 0
        await stores[0].create_schema()
        async with pool.connection() as connection:
            assert (
                await (
                    await connection.execute(
                        "SELECT oid, conname, pg_get_constraintdef(oid) FROM pg_constraint"
                        " WHERE connamespace = current_schema()::regnamespace ORDER BY oid"
                    )
                ).fetchall()
                == keys_before
            )
        assert await retained(pool) == expected
        import psycopg

        for statement in [
            "UPDATE reporting_revisions SET managed_control_totals = '[]'::jsonb",
            "UPDATE reporting_adjustments SET managed_control_total_deltas = '[]'::jsonb",
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with pool.connection() as connection:
                    await connection.execute(statement)
        s = await scenario(stores[0], account_id="new-account")
        await stores[0].commit_materialization(s.outcome)
        receipt, _ = await stores[0].record_revision_receipt(s.receipt)
        assert await stores[1].get_receipt(receipt.key) == receipt


@pytest.mark.parametrize("autocommit", [False, True])
async def test_failed_extension_upgrade_rolls_back_prerequisites(autocommit: bool) -> None:
    pytest.importorskip("psycopg")
    import psycopg

    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_beta15.sql").read_text())
            await connection.execute((FIXTURES / "reporting_ledger_beta15_data.sql").read_text())
            await connection.execute(
                "CREATE TABLE reporting_reconciliation_records (adopter_marker TEXT)"
            )
        before = await retained(pool)
        with pytest.raises(psycopg.Error):
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
                "ALTER TABLE reporting_reconciliation_records" " DISABLE TRIGGER ALL"
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
                "ALTER TABLE reporting_reconciliation_records" " ENABLE TRIGGER ALL"
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


@pytest.mark.parametrize("autocommit", [False, True])
async def test_upgrade_from_initial_reconciliation_preserves_records_heads_and_changes(
    autocommit: bool,
) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_1171.sql").read_text())
            await connection.execute(
                RESOURCES.joinpath("reporting_ledger_account_generations.sql").read_text()
            )
            await connection.execute(
                RESOURCES.joinpath("reporting_ledger_obligation_currency.sql").read_text()
            )
            await connection.execute(
                (FIXTURES / "reporting_ledger_reconciliation_initial.sql").read_text()
            )
        legacy_store = InitialReconciliationStore(pool=pool, clock=lambda: NOW)
        store = legacy_store
        s = await scenario(store)
        await store.commit_materialization(s.outcome)
        # The initial schema used an application head write. Seed that old
        # transaction explicitly; the upgraded schema advances heads itself.
        async with pool.connection() as connection, connection.transaction():
            receipt = replace(s.receipt, received_at=NOW)
            await store._insert(connection, receipt)
            await store._append_reconciliation_change(connection, receipt)
            await connection.execute(
                "INSERT INTO reporting_receipt_heads"
                " (account_id, consumer_id, chain_key, receipt_id, receipt_status)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    "acct_a",
                    "buyer",
                    receipt_chain(receipt),
                    receipt.reporting_receipt_id,
                    receipt.status,
                ),
            )
        foreign = await scenario(legacy_store, consumer_id="other-buyer")
        await legacy_store.commit_materialization(foreign.outcome)
        async with pool.connection() as connection:
            before_records = await (
                await connection.execute(
                    "SELECT * FROM reporting_reconciliation_records ORDER BY change_id"
                )
            ).fetchall()
            before_heads = await (
                await connection.execute("SELECT * FROM reporting_receipt_heads")
            ).fetchall()
            before_changes = await (
                await connection.execute("SELECT * FROM reporting_ledger_changes ORDER BY seq")
            ).fetchall()
        store = PgReportingReconciliationStore(pool=pool, clock=lambda: NOW)
        await asyncio.gather(*(store.create_schema() for _ in range(4)))
        async with pool.connection() as connection:
            assert (
                await (
                    await connection.execute(
                        "SELECT * FROM reporting_reconciliation_records ORDER BY change_id"
                    )
                ).fetchall()
                == before_records
            )
            assert (
                await (await connection.execute("SELECT * FROM reporting_receipt_heads")).fetchall()
                == before_heads
            )
            assert (
                await (
                    await connection.execute("SELECT * FROM reporting_ledger_changes ORDER BY seq")
                ).fetchall()
                == before_changes
            )
        assert await store.record_revision_receipt(s.receipt) == (receipt, False)
        assert (
            len((await store.read_reconciliation_changes(caller=s.binding.principal)).changes) == 5
        )
        assert tuple(
            item.sequence
            for item in (
                await store.read_reconciliation_changes(caller=foreign.binding.principal)
            ).changes
        ) == (1, 2, 3, 4)
        core_before = await store.open_snapshot(account_id="acct_a", filters_fingerprint="buyer")
        before = await store.read_reconciliation_changes(caller=s.binding.principal)
        await store.record_revision_receipt(foreign.receipt)
        assert (
            await store.open_snapshot(account_id="acct_a", filters_fingerprint="buyer")
            == core_before
        )
        assert await store.read_reconciliation_changes(caller=s.binding.principal) == before


@pytest.mark.parametrize("damage", ["missing_attempt", "missing_feed"])
async def test_initial_upgrade_rejects_orphaned_outcome_without_repair(damage: str) -> None:
    pytest.importorskip("psycopg")
    import psycopg

    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection:
            await connection.execute((FIXTURES / "reporting_ledger_1171.sql").read_text())
            await connection.execute(
                RESOURCES.joinpath("reporting_ledger_account_generations.sql").read_text()
            )
            await connection.execute(
                RESOURCES.joinpath("reporting_ledger_obligation_currency.sql").read_text()
            )
            await connection.execute(
                (FIXTURES / "reporting_ledger_reconciliation_initial.sql").read_text()
            )
        store = InitialReconciliationStore(pool=pool, clock=lambda: NOW)
        s = await scenario(store)
        from .test_reporting_reconciliation_sql import raw_insert

        await raw_insert(
            pool,
            (
                replace(s.outcome, reporting_materialization_id="orphaned-outcome")
                if damage == "missing_attempt"
                else s.outcome
            ),
            legacy=True,
            write_change=damage != "missing_feed",
        )
        async with pool.connection() as connection:
            before = await (
                await connection.execute(
                    "SELECT * FROM reporting_reconciliation_records ORDER BY change_id"
                )
            ).fetchall()
        with pytest.raises(psycopg.errors.CheckViolation):
            await store.create_schema()
        async with pool.connection() as connection:
            assert (
                await (
                    await connection.execute(
                        "SELECT * FROM reporting_reconciliation_records ORDER BY change_id"
                    )
                ).fetchall()
                == before
            )
            assert not await (
                await connection.execute(
                    "SELECT 1 FROM pg_trigger WHERE tgname = 'reporting_reconciliation_guard'"
                    " AND tgrelid = 'reporting_reconciliation_records'::regclass"
                )
            ).fetchone()
