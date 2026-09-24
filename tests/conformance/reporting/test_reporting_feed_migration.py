"""Isolated feed migration, immutable snapshots, unchanged upstream catalogs."""

import asyncio
import json
from importlib.resources import files

import pytest

from adcp.reporting.feed import PgReportingFeedStore, ReportingFeedError
from adcp.reporting.materializer import ReportingMaterializerLease
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects
from adcp.reporting.outbox.status_schema import REQUIRED_STATUS_OBJECTS
from adcp.reporting.receipts import PgReportingReceiptStore

from ._durable_materializer_support import DurableHarness, durable_case
from ._feed_support import feed_harness, feed_request, mixed_case, walk, without_feed
from ._generation_support import isolated_reporting_pool
from ._receipt_support import receipt_case, request_for
from ._reconciliation_support import Clock

SQL = files("adcp.reporting.ledger").joinpath("reporting_feed.sql").read_text()
MANIFEST = json.loads(files("adcp.reporting.feed").joinpath("required_schema.json").read_text())


async def fairness(pool):
    async with pool.connection() as c:
        return await (
            await c.execute(
                "SELECT to_jsonb(t) FROM adcp_reporting_configuration_lease_turns t"
                " ORDER BY to_jsonb(t)::text"
            )
        ).fetchall()


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_feed_migration_preserves_parent_catalog_receipts_pending_and_fairness(
    notifications, autocommit
):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        parent = PgReportingReceiptStore(pool=pool, notifications=notifications)
        await parent.create_schema()
        h = DurableHarness(parent, Clock(), pool)
        s = await receipt_case(h)
        req = request_for(s)
        response = await parent.ingest_receipt_batch(req, caller=s.binding.principal)
        pending = await durable_case(parent, account="pending-account")
        for _ in range(8):
            lease = await pending.claim()
            if isinstance(lease, ReportingMaterializerLease):
                break
        assert isinstance(lease, ReportingMaterializerLease) and lease.scope == pending.scope
        assert (
            lease.attempt.reporting_materialization_id == lease.request.reporting_materialization_id
        )
        turn = await parent.lease_period_close(
            worker_id="feed-upgrade", now=h.clock(), lease_seconds=30
        )
        assert turn is not None
        await parent.release_period_close(turn, worker_id="feed-upgrade")
        turns = await fairness(pool)
        before = await h.image()
        async with pool.connection() as c:
            original = await schema_objects(c)
        waiver_objects = {
            key: value
            for key, value in REQUIRED_STATUS_OBJECTS.items()
            if "reporting_issue_waiver_bindings" in key
        }
        assert len(waiver_objects) == 10
        parent_manifest = {**REQUIRED_OBJECTS, **waiver_objects}
        for package in ("materializer", "receipts"):
            parent_manifest.update(
                json.loads(
                    files("adcp.reporting." + package).joinpath("required_schema.json").read_text()
                )
            )
        assert len(parent_manifest) == 763
        assert original == parent_manifest
        new = PgReportingFeedStore(pool=pool, notifications=notifications)
        with pytest.raises(ReportingFeedError) as error:
            await new.reporting_feed_ready()
        assert error.value.code == "REPORTING_FEED_SCHEMA_UNREADY"
        await asyncio.gather(*(new.create_schema() for _ in range(3)))
        feed_operation_1 = await new.reporting_feed_ready()
        assert feed_operation_1
        async with pool.connection() as c:
            actual = await schema_objects(c)
        assert {k: actual[k] for k in original} == original
        assert actual == {**original, **MANIFEST}
        assert {k: v for k, v in actual.items() if "reporting_feed_" in k} == MANIFEST
        assert len(MANIFEST) == 33
        for package, count in (("materializer", 187), ("receipts", 102)):
            required = json.loads(
                files("adcp.reporting." + package).joinpath("required_schema.json").read_text()
            )
            assert len(required) == count and all(actual[k] == v for k, v in required.items())
        assert without_feed(await h.image()) == before
        assert await fairness(pool) == turns
        first = await new.read_reporting_feed(feed_request(s), caller=s.binding.principal)
        saved = await new.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        await new.create_schema()
        assert (
            await new.read_reporting_feed_snapshot(saved.snapshot_id, caller=s.binding.principal)
            == saved
        )
        feed_operation_2 = await parent.ingest_receipt_batch(req, caller=s.binding.principal)
        assert feed_operation_2 == response
        assert await fairness(pool) == turns
        feed_condition_3 = (
            await parent.materializer_ready() and await parent.receipt_ingestion_ready()
        )
        assert feed_condition_3


async def test_interrupted_feed_migration_is_invisible_and_retry_retains_history():
    async with isolated_reporting_pool(autocommit=True) as pool:
        parent = PgReportingReceiptStore(pool=pool, clock=Clock())
        await parent.create_schema()
        h = DurableHarness(parent, parent._clock, pool)
        s = await receipt_case(h)
        response = await parent.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
        before = await h.image()
        entered, release = asyncio.Event(), asyncio.Event()

        async def migrate():
            async with pool.connection() as c, c.transaction():
                await c.execute(SQL)
                entered.set()
                await release.wait()

        task = asyncio.create_task(migrate())
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with pool.connection() as c:
                assert (
                    await (
                        await c.execute("SELECT to_regclass('reporting_feed_snapshots')")
                    ).fetchone()
                )[0] is None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await h.image() == before
        new = PgReportingFeedStore(pool=pool)
        with pytest.raises(ReportingFeedError):
            await new.reporting_feed_ready()
        await new.create_schema()
        feed_operation_4 = await parent.ingest_receipt_batch(
            request_for(s), caller=s.binding.principal
        )
        assert feed_operation_4 == response
        assert without_feed(await h.image()) == before


@pytest.mark.parametrize(
    "damage",
    [
        "ALTER TABLE reporting_feed_snapshots DISABLE TRIGGER reporting_feed_immutable",
        "ALTER TABLE reporting_feed_snapshots ALTER COLUMN document DROP NOT NULL",
        "DROP TABLE reporting_feed_snapshots",
        (
            "CREATE OR REPLACE FUNCTION reporting_feed_immutable() RETURNS TRIGGER"
            " LANGUAGE plpgsql AS $body$ BEGIN RETURN NEW; END $body$"
        ),
        (
            "ALTER TABLE reporting_receipt_ingestion_results"
            " DISABLE TRIGGER reporting_receipt_ingestion_result"
        ),
        "ALTER TABLE reporting_materializer_work DISABLE TRIGGER reporting_materializer_guard",
    ],
)
@pytest.mark.parametrize("notifications", [False, True])
async def test_old_partial_or_mismatched_schema_refuses_new_reads_and_continuations(
    damage, notifications
):
    async with feed_harness("postgres", notifications=notifications) as h:
        s, _, _ = await mixed_case(h)
        first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
        async with h.pool.connection() as c:
            await c.execute(damage)
        before = await h.image()
        for req in (
            feed_request(s),
            feed_request(s, pagination={"cursor": first["pagination"]["cursor"]}),
        ):
            with pytest.raises(ReportingFeedError) as error:
                await h.store.read_reporting_feed(req, caller=s.binding.principal)
            assert error.value.code == "REPORTING_FEED_SCHEMA_UNREADY"
        assert await h.image() == before


@pytest.mark.parametrize(
    "assignment",
    [
        "ownership_mode='present'",
        "representation_version=2",
        "signing_key=decode(repeat('00',32),'hex')",
        "document=document",
    ],
)
async def test_snapshot_membership_representation_ownership_and_signing_identity_are_immutable(
    assignment,
):
    async with feed_harness("postgres") as h:
        s, _, _ = await mixed_case(h)
        first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
        before = await h.image()
        import psycopg

        async with h.pool.connection() as c:
            with pytest.raises(psycopg.errors.CheckViolation):
                await c.execute("UPDATE reporting_feed_snapshots SET " + assignment)
        assert await h.image() == before
        pages, _, _ = await walk(h.store, feed_request(s), s.binding.principal, first=first)
        assert pages[-1]["pagination"]["total_count"] == 6
