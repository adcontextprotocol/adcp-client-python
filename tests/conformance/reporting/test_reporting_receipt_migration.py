"""Additive receipt manifests and migration preserve the approved materializer."""

import asyncio
import json
from importlib.resources import files

import pytest

from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.receipts import PgReportingReceiptStore, ReportingReceiptError

from ._durable_materializer_support import DurableHarness, durable_case
from ._generation_support import isolated_reporting_pool
from ._receipt_support import receipt_case, receipt_harness, request_for
from ._reconciliation_support import Clock

SQL = files("adcp.reporting.ledger").joinpath("reporting_receipt_ingestion.sql").read_text()
MANIFEST = json.loads(files("adcp.reporting.receipts").joinpath("required_schema.json").read_text())
PARENT_MANIFEST = json.loads(
    files("adcp.reporting.materializer").joinpath("required_schema.json").read_text()
)


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_populated_repeated_concurrent_migration_preserves_parent_objects_captures_and_queues(
    notifications, autocommit
):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        old = PgReportingMaterializerStore(pool=pool, notifications=notifications)
        await old.create_schema()
        case = await durable_case(old)
        assert (await case.service().run_once()).state == "verified"
        h = DurableHarness(old, Clock(), pool)
        before = await h.image()
        captures = await old.read_materializer_boundaries(caller=case.scope.principal)
        queue = await h.queue()
        async with pool.connection() as c:
            original = await schema_objects(c)
        new = PgReportingReceiptStore(pool=pool, notifications=notifications)
        with pytest.raises(ReportingReceiptError) as error:
            await new.receipt_ingestion_ready()
        assert error.value.code == "RECEIPT_SCHEMA_UNREADY"
        await asyncio.gather(*(new.create_schema() for _ in range(3)))
        await new.create_schema()
        assert await new.receipt_ingestion_ready()
        async with pool.connection() as c:
            actual = await schema_objects(c)
        assert {key: actual[key] for key in original} == original
        assert len(PARENT_MANIFEST) == 187
        assert {key: actual[key] for key in PARENT_MANIFEST} == PARENT_MANIFEST
        assert {
            key: value for key, value in actual.items() if "reporting_receipt_ingestion_" in key
        } == MANIFEST
        after = await h.image()
        assert {key: after[key] for key in before} == before
        assert await new.read_materializer_boundaries(caller=case.scope.principal) == captures
        assert await h.queue() == queue
        assert queue[1] == (("quarantined",) if notifications else ())


async def test_interrupted_receipt_migration_is_invisible_and_restart_converges():
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingMaterializerStore(pool=pool).create_schema()
        entered, release = asyncio.Event(), asyncio.Event()

        async def migrate():
            async with pool.connection() as c, c.transaction():
                await c.execute(SQL)
                entered.set()
                await release.wait()

        task = asyncio.create_task(migrate())
        await asyncio.wait_for(entered.wait(), 10)
        async with pool.connection() as c:
            assert (
                await (
                    await c.execute("SELECT to_regclass('reporting_receipt_ingestion_batches')")
                ).fetchone()
            )[0] is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        new = PgReportingReceiptStore(pool=pool)
        with pytest.raises(ReportingReceiptError):
            await new.receipt_ingestion_ready()
        await new.create_schema()
        assert await new.receipt_ingestion_ready()


@pytest.mark.parametrize(
    "damage",
    [
        "DROP INDEX reporting_receipt_ingestion_chain",
        "ALTER TABLE reporting_receipt_ingestion_results "
        "DISABLE TRIGGER reporting_receipt_ingestion_result",
        "ALTER TABLE reporting_receipt_ingestion_batches ALTER COLUMN expected_count DROP NOT NULL",
        "DROP TABLE reporting_receipt_ingestion_boundaries CASCADE",
        "ALTER TABLE reporting_materializer_work DISABLE TRIGGER reporting_materializer_guard",
        "ALTER TABLE reporting_reconciliation_records "
        "DISABLE TRIGGER reporting_reconciliation_guard",
        # Every object still present and enabled, but a financial predicate body
        # silently replaced. Dropped objects and disabled triggers cannot stand
        # in for this: a weakened tuple predicate is exactly how a deployment
        # would lose graph enforcement without any visible schema difference.
        "CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_graph()"
        " RETURNS TRIGGER LANGUAGE plpgsql AS $damage$ BEGIN RETURN NEW; END $damage$",
        "CREATE OR REPLACE FUNCTION reporting_receipt_ingestion_sha256(document JSONB)"
        " RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $damage$ SELECT repeat('0', 64) $damage$",
    ],
)
@pytest.mark.parametrize("notifications", [False, True])
async def test_old_partial_mismatched_schemas_fail_closed_even_for_completed_replay(
    damage, notifications
):
    async with receipt_harness("postgres", notifications=notifications) as h:
        s = await receipt_case(h)
        request = request_for(s)
        await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
        async with h.pool.connection() as c:
            await c.execute(damage)
        before = await h.image()
        with pytest.raises(ReportingReceiptError) as error:
            await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
        assert error.value.code == "RECEIPT_SCHEMA_UNREADY"
        assert await h.image() == before


async def test_receipt_store_does_not_claim_readiness_on_old_parent_schema():
    async with isolated_reporting_pool(autocommit=True) as pool:
        old = PgReportingMaterializerStore(pool=pool, clock=Clock())
        await old.create_schema()
        h = DurableHarness(old, Clock(), pool)
        s = await receipt_case(h)
        before = await h.image()
        new = PgReportingReceiptStore(pool=pool)
        with pytest.raises(ReportingReceiptError) as error:
            await new.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
        assert error.value.code == "RECEIPT_SCHEMA_UNREADY"
        assert await h.image() == before
