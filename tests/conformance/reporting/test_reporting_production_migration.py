"""Populated, concurrent and interrupted B2.4 bootstrap with old objects intact."""

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from importlib.resources import files

import pytest

from adcp.reporting.feed import PgReportingFeedStore
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.materializer import ReportingMaterializerLease
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.outbox.status_pg import PgStatusNotificationStore
from adcp.reporting.production.pg import PgReportingProductionStore
from adcp.reporting.production.schema import validate_production_schema
from adcp.reporting.projection.schema import validate_projection_schema

from ._durable_materializer_support import DurableHarness, durable_case
from ._feed_support import feed_request, mixed_case, walk
from ._generation_support import isolated_reporting_pool
from ._reconciliation_support import Clock
from .test_reporting_feed_migration import fairness


def manifests():
    return {
        package: json.loads(
            files("adcp.reporting." + package).joinpath("required_schema.json").read_text()
        )
        for package in ("materializer", "receipts", "feed", "projection", "production")
    }


def original_rows(image, before):
    # This one documented additive column fences newly activated checkpoints.
    # Its default leaves old C checkpoints compatible until explicit activation.
    result = deepcopy({key: image[key] for key in before})
    for (row,) in result.get("reporting_status_scope_checkpoints", []):
        writer_floor = row.pop("projection_writer_floor", 1)
        assert writer_floor == 1
    return result


async def test_catalog_round_trips_stay_bounded_and_still_detect_fresh_ddl():
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingProductionStore(pool=pool, notifications=True)
        await store.create_schema()
        async with pool.connection() as connection:

            class CountQueries:
                def __init__(self):
                    self.calls = 0

                async def execute(self, *args, **kwargs):
                    self.calls += 1
                    return await connection.execute(*args, **kwargs)

            counted = CountQueries()
            original = await schema_objects(counted)
            initial_queries = counted.calls
            # Adopter tables are allowed, but must not multiply the number of
            # catalog round trips needed by every production readiness check.
            from psycopg import sql

            for number in range(24):
                await connection.execute(
                    sql.SQL("CREATE TABLE {} (id integer PRIMARY KEY)").format(
                        sql.Identifier(f"reporting_catalog_probe_{number}")
                    )
                )
            # A similarly named table outside current_schema() stays excluded.
            await connection.execute("CREATE TEMP TABLE reporting_catalog_outside (id integer)")
            counted.calls = 0
            async with connection.transaction():
                await connection.execute("SET TRANSACTION READ ONLY")
                expanded = await schema_objects(counted)
            assert counted.calls == initial_queries <= 10
            assert all(expanded[key] == value for key, value in original.items())
            assert "table:reporting_catalog_probe_23" in expanded
            assert "table:reporting_catalog_outside" not in expanded
            await validate_production_schema(connection, notifications=True)
            await connection.execute(
                "ALTER TABLE reporting_production_delivery_windows"
                " DISABLE TRIGGER reporting_production_delivery_window_immutable"
            )
            changed = await schema_objects(connection)
            key = (
                "trigger:reporting_production_delivery_windows."
                "reporting_production_delivery_window_immutable"
            )
            assert original[key]["enabled"] and not changed[key]["enabled"]
            with pytest.raises(ReportingNotificationError):
                await validate_production_schema(connection, notifications=True)


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_populated_repeat_concurrent_migration_keeps_history_fairness_and_frozen_pages(
    notifications, autocommit
):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        parent = PgReportingFeedStore(pool=pool, notifications=notifications)
        await parent.create_schema()
        # Install the optional historical C objects through their supported
        # notification-enabled owner. The actual receipt/feed writer retains
        # this cell's requested off/on mode.
        old_projection = PgStatusNotificationStore(
            PgReportingFeedStore(pool=pool, notifications=True)
        )
        await old_projection.create_schema()
        h = DurableHarness(parent, Clock(), pool)
        case, request, response = await mixed_case(h)
        pending = await durable_case(parent, account="pending-account")
        lease = None
        for _ in range(8):
            candidate = await pending.claim()
            if isinstance(candidate, ReportingMaterializerLease):
                lease = candidate
                break
        assert lease is not None and lease.admission_epoch == 0
        if notifications:
            await old_projection.baseline(account_id=case.obligation.account_id)
        first = await parent.read_reporting_feed(feed_request(case), caller=case.binding.principal)
        original = await parent.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=case.binding.principal
        )
        expected = await walk(parent, feed_request(case), case.binding.principal, first=first)
        before = await h.image()
        old_turns = await fairness(pool)
        async with pool.connection() as c:
            old_objects = await schema_objects(c)
            with pytest.raises(ReportingNotificationError):
                await validate_production_schema(c, notifications=notifications)
        child = PgReportingProductionStore(pool=pool, notifications=notifications)
        await asyncio.wait_for(asyncio.gather(*(child.create_schema() for _ in range(3))), 30)
        await child.create_schema()
        async with pool.connection() as c:
            current = await schema_objects(c)
            await validate_projection_schema(c, notifications=notifications)
            await validate_production_schema(c, notifications=notifications)
            assert (
                await (
                    await c.execute("SELECT count(*) FROM reporting_production_accounts")
                ).fetchone()
            )[0] == 0
        assert {key: current[key] for key in old_objects} == old_objects
        required = manifests()
        assert set(required["projection"]).isdisjoint(required["production"])
        assert set(current) - set(old_objects) == set(required["projection"]) | set(
            required["production"]
        )
        for objects in required.values():
            assert all(current.get(key) == value for key, value in objects.items())
        assert original_rows(await h.image(), before) == before
        assert await fairness(pool) == old_turns
        production_operation_1 = await parent.ingest_receipt_batch(
            request, caller=case.binding.principal
        )
        assert production_operation_1 == response
        assert (
            await walk(child, feed_request(case), case.binding.principal, first=first) == expected
        )
        assert (
            await child.read_reporting_feed_snapshot(
                original.snapshot_id, caller=case.binding.principal
            )
            == original
        )
        assert original_rows(await h.image(), before) == before
        print(
            json.dumps(
                {
                    "b24_migration": "concurrent-repeat",
                    "notifications": notifications,
                    "autocommit": autocommit,
                    "preserved_objects": len(old_objects),
                    "isolated_additions": {
                        key: len(required[key]) for key in ("projection", "production")
                    },
                    "pending_epoch": lease.admission_epoch,
                    "old_pages": len(expected[0]),
                }
            ),
            flush=True,
        )


@pytest.mark.parametrize("notifications", [False, True])
async def test_interrupted_complete_migration_rolls_back_every_new_object(
    notifications, monkeypatch
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        parent = PgReportingFeedStore(pool=pool, notifications=notifications)
        await parent.create_schema()
        await PgStatusNotificationStore(
            PgReportingFeedStore(pool=pool, notifications=True)
        ).create_schema()
        h = DurableHarness(parent, Clock(), pool)
        case, request, response = await mixed_case(h)
        before = await h.image()
        async with pool.connection() as c:
            original = await schema_objects(c)
        child = PgReportingProductionStore(pool=pool, notifications=notifications)
        entered = asyncio.Event()
        from psycopg import AsyncConnection

        execute = AsyncConnection.execute

        async def interrupt(connection, query, *args, **kwargs):
            result = await execute(connection, query, *args, **kwargs)
            if isinstance(query, str) and query.startswith("-- B2.4 production admission."):
                entered.set()
                await asyncio.Event().wait()
            return result

        with monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", interrupt)
            task = asyncio.create_task(child.create_schema())
            try:
                await asyncio.wait_for(entered.wait(), 20)
                async with pool.connection() as c:
                    # Catalog deparsing can acquire a relation lock behind
                    # the intentionally paused ALTER TABLE. Observe raw MVCC
                    # catalog visibility now; compare every definition after
                    # cancellation releases those DDL locks.
                    assert (
                        await (
                            await c.execute(
                                "SELECT count(*) FROM pg_class c"
                                " JOIN pg_namespace n ON n.oid=c.relnamespace"
                                " WHERE n.nspname=current_schema()"
                                " AND c.relname='reporting_production_delivery_windows'"
                            )
                        ).fetchone()
                    )[0] == 0
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert await h.image() == before
        async with pool.connection() as c:
            assert await schema_objects(c) == original
        await child.create_schema()
        assert original_rows(await h.image(), before) == before
        production_operation_2 = await parent.ingest_receipt_batch(
            request, caller=case.binding.principal
        )
        assert production_operation_2 == response


@pytest.mark.parametrize(
    "damage",
    [
        (
            "ALTER TABLE reporting_production_delivery_windows"
            " DISABLE TRIGGER reporting_production_delivery_window_immutable"
        ),
        "ALTER TABLE reporting_production_delivery_windows ALTER COLUMN expires_at DROP NOT NULL",
        "DROP TABLE reporting_production_delivery_windows",
        "DROP INDEX reporting_production_source_pending",
        (
            "ALTER TABLE reporting_projection_inputs"
            " DISABLE TRIGGER reporting_projection_input_immutable"
        ),
        (
            "ALTER TABLE reporting_status_scope_checkpoints"
            " DISABLE TRIGGER reporting_projection_checkpoint_guard"
        ),
    ],
)
@pytest.mark.parametrize("notifications", [False, True])
async def test_partial_or_mismatched_production_objects_refuse_fresh_readiness(
    damage, notifications
):
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingProductionStore(pool=pool, notifications=notifications)
        await store.create_schema()
        async with pool.connection() as c:
            await c.execute(damage)
            with pytest.raises(ReportingNotificationError) as caught:
                await validate_production_schema(c, notifications=notifications)
            assert caught.value.code == "reporting_production_schema_unready"


async def test_retry_window_is_immutable_and_repeated_bootstrap_never_restarts_deadline():
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingProductionStore(pool=pool)
        await store.create_schema()
        started = Clock()()
        row = (
            "account-window",
            "window-key",
            "core",
            "a" * 64,
            started,
            started + timedelta(seconds=86400),
        )
        async with pool.connection() as c:
            await c.execute(
                "INSERT INTO reporting_production_delivery_windows VALUES(%s,%s,%s,%s,%s,%s)", row
            )
        for statement in (
            "UPDATE reporting_production_delivery_windows"
            " SET expires_at=expires_at+interval '1 second'",
            "DELETE FROM reporting_production_delivery_windows",
        ):
            async with pool.connection() as c:
                with pytest.raises(Exception):
                    async with c.transaction():
                        await c.execute(statement)
        await store.create_schema()
        async with pool.connection() as c:
            assert await (
                await c.execute("SELECT * FROM reporting_production_delivery_windows")
            ).fetchall() == [row]
