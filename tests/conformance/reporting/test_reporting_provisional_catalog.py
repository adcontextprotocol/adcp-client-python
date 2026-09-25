"""The observation extension preserves the exact historical catalog and rows."""

import asyncio
import json
from importlib.resources import files

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects
from adcp.reporting.outbox.status_schema import REQUIRED_STATUS_OBJECTS
from adcp.reporting.receipts import PgReportingReceiptStore, ReportingReceiptError

from ._durable_materializer_support import DurableHarness, durable_case
from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from ._provisional_catalog import PROVISIONAL_OBJECTS
from ._reconciliation_support import Clock
from .test_reporting_notification_migration import retained_physical_rows


@pytest.mark.parametrize("autocommit", [False, True])
async def test_atomic_observation_bootstrap_preserves_exact_763_parent_objects(
    autocommit, monkeypatch
):
    # This is a source-level DDL control, not installed historical-binary proof.
    # All eight pre-observation migration files and old manifests stay unchanged.
    root = files("adcp.reporting.ledger")
    chain = (
        "reporting_ledger.sql",
        "reporting_ledger_account_generations.sql",
        "reporting_ledger_obligation_currency.sql",
        "reporting_ledger_reconciliation.sql",
        "reporting_notification_outbox.sql",
        "reporting_webhook_activity.sql",
        "reporting_materializer.sql",
        "reporting_receipt_ingestion.sql",
    )
    expected_parent = {
        **REQUIRED_OBJECTS,
        **{
            key: value
            for key, value in REQUIRED_STATUS_OBJECTS.items()
            if "reporting_issue_waiver_bindings" in key
        },
    }
    for package in ("materializer", "receipts"):
        expected_parent.update(
            json.loads(
                files("adcp.reporting." + package).joinpath("required_schema.json").read_text()
            )
        )
    assert len(expected_parent) == 763
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        async with pool.connection() as connection, connection.transaction():
            for name in chain:
                await connection.execute(root.joinpath(name).read_text())
        store = PgReportingReceiptStore(pool=pool, clock=Clock())
        await durable_case(store)
        h = DurableHarness(store, store._clock, pool)
        rows = await h.image()
        physical = await retained_physical_rows(pool)
        async with pool.connection() as connection:
            assert await schema_objects(connection) == expected_parent
        original = store._create_schema_on

        async def fail_after_observation_ddl(connection):
            await original(connection)
            raise RuntimeError("injected observation bootstrap failure")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_create_schema_on", fail_after_observation_ddl)
            with pytest.raises(ReportingReceiptError) as failure:
                await store.create_schema()
            assert failure.value.code == "RECEIPT_STORAGE_UNAVAILABLE"
        async with pool.connection() as connection:
            assert await schema_objects(connection) == expected_parent
        assert await h.image() == rows
        assert await retained_physical_rows(pool) == physical
        await asyncio.gather(*(store.create_schema() for _ in range(3)))
        async with pool.connection() as connection:
            actual = await schema_objects(connection)
        assert {key: actual[key] for key in expected_parent} == expected_parent
        assert actual == {**expected_parent, **PROVISIONAL_OBJECTS}
        assert await retained_physical_rows(pool) == physical
        image = await h.image()
        assert image == {
            **rows,
            "reporting_provisional_acquisitions": [],
            "reporting_provisional_observations": [],
        }


async def test_both_observation_tables_reject_update_and_delete():
    from tests.test_reporting_provisional_observations import latest
    from tests.test_reporting_settling import _capabilities, _harness

    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool() as pool:
        producer, store, _, _ = await _harness(
            _capabilities(restatement_window=None),
            store_factory=lambda clock: PgReportingLedgerStore(
                pool=pool, clock=clock, notifications=True
            ),
        )
        await producer.run_worker()
        before = await latest(store)
        for table in ("reporting_provisional_acquisitions", "reporting_provisional_observations"):
            for operation in (f"UPDATE {table} SET payload=payload", f"DELETE FROM {table}"):
                async with pool.connection() as connection:
                    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                        async with connection.transaction():
                            await connection.execute(operation)
                assert await latest(store) == before


async def test_observation_foreign_keys_bind_both_obligation_and_revision_accounts():
    psycopg = pytest.importorskip("psycopg")
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        records = []
        for account in ("owner-a", "owner-b"):
            config = configuration(account)
            obligation = obligation_for(config)
            revision, rows = revision_for(obligation)
            await store.put_configuration(config)
            await store.commit_obligation(obligation)
            await store.commit_revision(revision, rows)
            records.append((obligation, revision))
        first, second = records
        insert_acquisition = (
            "INSERT INTO reporting_provisional_acquisitions "
            "(account_id, reporting_obligation_id, ordinal, source_execution_key, payload) "
            "VALUES (%s, %s, 0, 'probe-execution', '{}')"
        )
        async with pool.connection() as connection:
            with pytest.raises(psycopg.errors.ForeignKeyViolation) as failure:
                async with connection.transaction():
                    await connection.execute(
                        insert_acquisition, (second[0].account_id, first[0].reporting_obligation_id)
                    )
            assert failure.value.diag.constraint_name == "provisional_acquisition_owner_fk"
            await connection.execute(
                insert_acquisition, (first[0].account_id, first[0].reporting_obligation_id)
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation) as failure:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO reporting_provisional_observations "
                        "(account_id, reporting_obligation_id, ordinal, "
                        "reporting_revision_id, payload) "
                        "VALUES (%s, %s, 0, %s, '{}')",
                        (
                            first[0].account_id,
                            first[0].reporting_obligation_id,
                            second[1].reporting_revision_id,
                        ),
                    )
            assert failure.value.diag.constraint_name == "provisional_observation_revision_fk"
            assert (
                await (
                    await connection.execute(
                        "SELECT count(*) FROM reporting_provisional_observations"
                    )
                ).fetchone()
            )[0] == 0
