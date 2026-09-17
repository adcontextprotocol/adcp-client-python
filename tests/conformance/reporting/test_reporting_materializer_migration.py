"""Isolated manifests, atomic migration, bounded discovery and database fences."""

import asyncio
import json
from dataclasses import replace
from importlib.resources import files

import pytest

from adcp.reporting.ledger import LedgerConflictError, PgReportingReconciliationStore, derive_period
from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.outbox._schema import REQUIRED_OBJECTS, schema_objects, validate_schema

from ._durable_materializer_support import durable_case, durable_harness
from ._generation_support import isolated_reporting_pool, obligation_for

SQL = files("adcp.reporting.ledger").joinpath("reporting_materializer.sql").read_text()
MANIFEST = json.loads(
    files("adcp.reporting.materializer").joinpath("required_schema.json").read_text()
)


@pytest.mark.parametrize("autocommit", [False, True])
async def test_populated_repeated_and_concurrent_install_preserves_all_old_objects_and_rows(
    autocommit,
):
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        old = PgReportingReconciliationStore(pool=pool)
        await old.create_schema()
        case = await durable_case(old)
        async with pool.connection() as c:
            original = await schema_objects(c)
            physical = await (
                await c.execute(
                    "SELECT tableoid::regclass::text,ctid::text,xmin::text,to_jsonb(r)"
                    " FROM reporting_reconciliation_records r ORDER BY record_id"
                )
            ).fetchall()
        new = PgReportingMaterializerStore(pool=pool)
        with pytest.raises(LedgerConflictError, match="materializer schema"):
            await new.materializer_ready()
        await asyncio.gather(*(new.create_schema() for _ in range(3)))
        for _ in range(2):
            await new.create_schema()
            assert await new.materializer_ready()
            async with pool.connection() as c:
                actual = await schema_objects(c)
                assert {key: actual[key] for key in original} == original == REQUIRED_OBJECTS
                assert {
                    key: value for key, value in actual.items() if "reporting_materializer_" in key
                } == MANIFEST
                assert (
                    await (
                        await c.execute(
                            "SELECT tableoid::regclass::text,ctid::text,xmin::text,to_jsonb(r)"
                            " FROM reporting_reconciliation_records r ORDER BY record_id"
                        )
                    ).fetchall()
                    == physical
                )
                await validate_schema(c, activity=True)
        case.store = new
        assert (await case.service().run_once()).state == "verified"
        # Discovery is persisted, and reinstall cannot invalidate leased generations.
        before = await new.read_reconciliation_snapshot(caller=case.scope.principal)
        await new.create_schema()
        assert (
            await new.read_reconciliation_snapshot(caller=case.scope.principal)
        ).records == before.records


async def test_interrupted_migration_is_invisible_and_restart_converges():
    async with isolated_reporting_pool(autocommit=True) as pool:
        await PgReportingReconciliationStore(pool=pool).create_schema()
        entered, release = asyncio.Event(), asyncio.Event()

        async def install():
            async with pool.connection() as c, c.transaction():
                await c.execute(SQL)
                entered.set()
                await release.wait()

        task = asyncio.create_task(install())
        await asyncio.wait_for(entered.wait(), 10)
        async with pool.connection() as c:
            assert (
                await (
                    await c.execute("SELECT to_regclass('reporting_materializer_work')")
                ).fetchone()
            )[0] is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store = PgReportingMaterializerStore(pool=pool)
        with pytest.raises(LedgerConflictError):
            await store.materializer_ready()
        await store.create_schema()
        assert await store.materializer_ready()


@pytest.mark.parametrize(
    "damage",
    [
        "DROP INDEX reporting_materializer_work_due",
        "ALTER TABLE reporting_materializer_work DISABLE TRIGGER reporting_materializer_guard",
        "ALTER TABLE reporting_materializer_work ALTER COLUMN generation DROP NOT NULL",
        "ALTER TABLE reporting_materializer_notification_expansions"
        " ALTER COLUMN state SET DEFAULT 'pending'",
        "DROP TABLE reporting_materializer_status_boundaries CASCADE",
        "ALTER TABLE reporting_reconciliation_records"
        " DISABLE TRIGGER reporting_reconciliation_guard",
    ],
)
@pytest.mark.parametrize("notifications", [False, True])
async def test_partial_mismatched_and_disabled_guard_schemas_fail_closed(damage, notifications):
    async with durable_harness("postgres", notifications=notifications) as h:
        case = await durable_case(h.store)
        async with h.pool.connection() as c:
            await c.execute(damage)
        with pytest.raises(LedgerConflictError):
            await case.claim()
        assert not await h.works()


async def test_backfill_is_bounded_indexed_restartable_and_fair_between_accounts():
    async with isolated_reporting_pool(autocommit=True) as pool:
        old = PgReportingReconciliationStore(pool=pool)
        await old.create_schema()
        cases = [await durable_case(old, account=name) for name in ("acct_a", "acct_b")]
        first = cases[0]
        for ordinal in range(1, 70):
            period = derive_period(first.config.schedule, account_timezone="UTC", ordinal=ordinal)
            await old.commit_obligation(
                replace(
                    obligation_for(first.config),
                    reporting_obligation_id=f"pending-{ordinal:03d}",
                    period=period,
                    scope_resolved_at=period.end,
                    automated_recovery_deadline_at=period.expected_at
                    + first.config.automated_recovery_window,
                )
            )
        store = PgReportingMaterializerStore(pool=pool)
        await store.create_schema()
        claimed = []
        for _ in range(4):
            result = await store.claim_materialization(keys=first.keys)
            if hasattr(result, "attempt"):
                claimed.append(result.scope.principal.account_id)
        assert "acct_b" in claimed  # A's large backlog cannot starve B.
        for _ in range(80):
            store = PgReportingMaterializerStore(pool=pool)  # Restart each turn.
            result = await store.claim_materialization(keys=first.keys)
            if getattr(result, "state", None) == "idle":
                break
        async with pool.connection() as c:
            assert (
                await (
                    await c.execute("SELECT count(*) FROM reporting_materializer_candidates")
                ).fetchone()
            )[0] == 71
            assert (
                await (
                    await c.execute(
                        "SELECT bool_and(complete) FROM reporting_materializer_discovery"
                    )
                ).fetchone()
            )[0]
            await c.execute("SET enable_seqscan=off")
            (now,) = await (await c.execute("SELECT clock_timestamp()")).fetchone()
            plans = []
            for query, params in (
                (
                    "SELECT account_id,consumer_id,delivery_config_id,delivery_config_version"
                    " FROM reporting_reconciliation_records"
                    " WHERE record_kind='destination_binding'",
                    (),
                ),
                (
                    "SELECT account_id FROM reporting_materializer_accounts WHERE due_at<=%s"
                    " ORDER BY served_at,account_id LIMIT 16",
                    (now,),
                ),
                (
                    "SELECT reporting_obligation_id FROM reporting_obligations WHERE account_id=%s"
                    " AND delivery_config_id=%s AND delivery_config_version=%s"
                    " AND reporting_obligation_id>%s ORDER BY reporting_obligation_id LIMIT 32",
                    ("acct_a", "daily", 1, ""),
                ),
                (
                    "SELECT reporting_materialization_id FROM reporting_materializer_work"
                    " WHERE account_id=%s AND state='pending' AND due_at<=%s"
                    " ORDER BY due_at LIMIT 1",
                    ("acct_a", now),
                ),
            ):
                plan = await (await c.execute("EXPLAIN (FORMAT JSON) " + query, params)).fetchone()
                plans.append(json.dumps(plan))
            assert all("Seq Scan" not in plan and "Index" in plan for plan in plans)


async def test_enabled_work_cannot_be_resumed_by_notifications_disabled_store():
    async with durable_harness("postgres", notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        await h.expire()
        disabled = PgReportingMaterializerStore(pool=h.pool, notifications=False)
        result = await disabled.claim_materialization(keys=case.keys)
        assert result.reason == "component_unavailable"
        assert (await h.works())[0][0] == lease.request.external_id
        assert (await h.works())[0][1] == "pending"


async def test_bounded_sampling_walks_past_a_full_page_of_busy_account_locks():
    async with durable_harness("postgres") as h:
        busy = [f"busy-{i:02d}" for i in range(16)]
        for account in busy:
            await durable_case(h.store, account=account)
        available = await durable_case(h.store, account="zz-available")
        async with h.pool.connection() as connection, connection.transaction():
            for account in busy:
                await h.store._lock_account(connection, account)
            assert (await available.claim()).state == "idle"
            selected = await available.claim()
            assert selected.scope.principal.account_id == "zz-available"
            assert len(await h.works()) == 1


async def test_pre_activation_event_never_promotes_and_old_outbox_cannot_claim_it():
    from adcp.reporting.ledger.notification_models import decode_event
    from adcp.reporting.outbox import PgReportingOutbox

    async with durable_harness("postgres", notifications=True) as h:
        case = await durable_case(h.store)
        assert (await case.service().run_once()).state == "verified"
        before = await h.queue()
        # Remove only the ordinary Core event's pending expansion via its own
        # worker protocol. Materializer records remain in their isolated queue.
        outbox = PgReportingOutbox(pool=h.pool)
        from datetime import datetime, timezone

        while lease := await outbox.claim_expansion(
            account_id="acct_a", now=datetime.now(timezone.utc), lease_seconds=30
        ):
            assert decode_event(lease.event).notification_type != "reporting.delivery_ready"
            await outbox.finish_expansion(lease, now=datetime.now(timezone.utc), state="suppressed")
        from psycopg import errors

        for mutation in (
            "UPDATE reporting_materializer_notification_events SET admission_epoch=1",
            "UPDATE reporting_materializer_notification_expansions SET state='pending'",
            "UPDATE reporting_materializer_work SET admission_epoch=1",
        ):
            with pytest.raises(errors.CheckViolation):
                async with h.pool.connection() as c, c.transaction():
                    await c.execute(mutation)
        await case.publish()
        await h.store.put_configuration(
            replace(case.config, deactivated_at=case.revision.created_at)
        )
        assert await h.queue() == before


async def test_schema_loss_after_reservation_fails_authorization_before_external_write():
    async with durable_harness("postgres", notifications=True) as h:
        case = await durable_case(h.store)
        lease = await case.claim()
        async with h.pool.connection() as c:
            await c.execute("DROP INDEX reporting_materializer_work_due")
        with pytest.raises(LedgerConflictError):
            await h.store.authorize_materialization(lease)
        assert case.writer.write_effects == 0
        assert not await case.outcomes()
        assert len(await h.works()) == 1 and (await h.works())[0][1] == "pending"
