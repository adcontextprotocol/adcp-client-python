"""Failure injection at every domain/dirty boundary, including autocommit PG."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    PgReportingReconciliationStore,
    ReportingAdjustmentRecord,
    ReportingMaterializationCheck,
    derive_period,
)
from adcp.reporting.outbox import PgReportingOutbox

from ._generation_support import END, NOW, START, configuration, revision_for
from ._reconciliation_support import scenario
from ._reliable_support import (
    Barrier,
    NotificationHarness,
    notification_subscription,
    reliable_factory,
)
from .test_reporting_notification_outbox import seed, statement


async def test_postgres_lease_and_retry_use_database_time_despite_caller_clock():
    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        h = NotificationHarness(reliable)
        await seed(h)
        pool = reliable.blobs.pool
        assert pool is not None
        outbox = PgReportingOutbox(pool=pool)
        caller_time = NOW + timedelta(days=100000)

        async def database_time():
            async with pool.connection() as conn:
                return (await (await conn.execute("SELECT clock_timestamp()")).fetchone())[0]

        before = await database_time()
        lease = await outbox.claim_expansion(
            account_id="acct_a", now=caller_time, lease_seconds=600
        )
        after = await database_time()
        assert lease is not None
        assert before <= lease.expires_at - timedelta(seconds=600) <= after
        assert (
            await outbox.claim_expansion(account_id="acct_a", now=caller_time, lease_seconds=600)
            is None
        )
        before = await database_time()
        assert await outbox.finish_expansion(
            lease, now=caller_time, state="pending", retry_at=caller_time + timedelta(seconds=600)
        )
        after = await database_time()
        async with pool.connection() as conn:
            due_at = (
                await (
                    await conn.execute(
                        "SELECT due_at FROM reporting_notification_expansions"
                        " WHERE account_id = %s AND consumer_namespace = %s"
                        " AND notification_id = %s",
                        ("acct_a", lease.consumer_namespace, lease.notification_id),
                    )
                ).fetchone()
            )[0]
            assert before <= due_at - timedelta(seconds=600) <= after
            # Explicit database state, never a timing sleep, makes this retry due.
            await conn.execute(
                "UPDATE reporting_notification_expansions SET due_at = clock_timestamp()"
                " WHERE account_id = %s AND consumer_namespace = %s AND notification_id = %s",
                ("acct_a", lease.consumer_namespace, lease.notification_id),
            )
        reclaimed = await outbox.claim_expansion(account_id="acct_a", now=NOW, lease_seconds=600)
        assert reclaimed is not None and reclaimed.token != lease.token
        assert not await outbox.finish_expansion(lease, now=NOW, state="complete")
        assert await outbox.finish_expansion(reclaimed, now=caller_time, state="complete")


async def test_postgres_expansion_expiry_before_final_fence_rolls_back_all_members():
    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        h = NotificationHarness(reliable)
        await seed(h)
        h.subscriptions.put(notification_subscription(subscriber="second"))
        outbox = h.outbox
        insert = outbox._insert_delivery

        async def expire_during_insert(conn, delivery, at):
            await insert(conn, delivery, at)
            reliable.clock.advance(timedelta(seconds=61))

        outbox._insert_delivery = expire_during_insert
        worker = h.worker()
        worker.outbox = outbox
        assert await worker.expand_one(account_id="acct_a")
        assert await outbox.list_deliveries(account_id="acct_a") == ()
        assert len(await outbox.list_events(account_id="acct_a")) == 1
        # A new worker snapshots the current complete membership after expiry.
        assert await h.worker().expand_one(account_id="acct_a")
        assert {
            row.delivery.binding.subscriber_id
            for row in await outbox.list_deliveries(account_id="acct_a")
        } == {"buyer", "second"}


async def prepare(h: NotificationHarness, operation: str):
    store = h.reliable.store
    if operation.startswith("managed_"):
        s = await scenario(store)
        if operation in {"managed_check", "managed_receipt"}:
            await store.commit_materialization(s.outcome)
        if operation == "managed_destination":
            return lambda: store.put_destination_binding(replace(s.binding, consumer_id="auditor"))
        if operation == "managed_obligation":
            await store.put_destination_binding(replace(s.binding, consumer_id="auditor"))
            return lambda: store.bind_obligation_delivery(
                replace(s.delivery, scope=replace(s.delivery.scope, consumer_id="auditor"))
            )
        if operation == "managed_attempt":
            return lambda: store.commit_materialization_attempt(
                replace(
                    s.attempt,
                    reporting_materialization_id="attempt-two",
                    attempt=2,
                    created_at=s.attempt.created_at + timedelta(seconds=1),
                )
            )
        if operation == "managed_materialization":
            return lambda: store.commit_materialization(s.outcome)
        if operation == "managed_check":
            return lambda: store.record_materialization_check(
                ReportingMaterializationCheck(
                    s.attempt.scope,
                    s.attempt.reporting_materialization_id,
                    "check-one",
                    "unavailable",
                    NOW,
                )
            )
        return lambda: store.record_revision_receipt(s.receipt)
    obligation, revision, rows = await seed(h, official=operation == "adjustment")
    if operation == "configuration":
        return lambda: store.put_configuration(replace(configuration(), delivery_config_version=2))
    if operation == "obligation":
        other = replace(
            obligation,
            reporting_obligation_id="other-period",
            period=derive_period(configuration().schedule, account_timezone="UTC", ordinal=1),
            scope_resolved_at=END + timedelta(hours=1),
        )
        return lambda: store.commit_obligation(other)
    if operation == "revision":
        changed, rows = revision_for(obligation, suffix="second")
        return lambda: store.commit_revision(changed, rows)
    if operation == "readability":
        return lambda: store.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=False,
        )
    if operation == "adjustment":
        value = ReportingAdjustmentRecord(
            "adj-new",
            "acct_a",
            revision.reporting_revision_id,
            "source_correction",
            START,
            END,
            (("impressions", "-1"),),
            NOW,
            NOW,
        )
        return lambda: store.commit_adjustment(value)
    if operation == "consumer_status":
        return lambda: store.record_consumer_status(statement(obligation))
    if operation in {"issue_state", "issue_retire"}:
        await store.ensure_issue_opened(
            issue_key="opaque", account_id="acct_a", consumer_id="buyer", observed_at=NOW
        )
    if operation == "issue_state":
        return lambda: store.set_issue_state(
            issue_key="opaque", account_id="acct_a", state="acknowledged", at=NOW
        )
    if operation == "issue_retire":
        return lambda: store.retire_issue(issue_key="opaque", account_id="acct_a", at=NOW)
    return lambda: store.ensure_issue_opened(
        issue_key="opaque", account_id="acct_a", consumer_id="buyer", observed_at=NOW
    )


async def image(h: NotificationHarness):
    """Read actual retained domain rows; never infer rollback from event count."""
    if isinstance(h.reliable.store, InMemoryReportingLedgerStore):
        return deepcopy(
            {
                key: value
                for key, value in vars(h.reliable.store).items()
                if key not in {"_clock", "_lock"}
            }
        )
    pool = h.reliable.blobs.pool
    tables = (
        "reporting_configurations",
        "reporting_obligations",
        "reporting_revisions",
        "reporting_adjustments",
        "reporting_consumer_statuses",
        "reporting_issue_lifecycle",
        "reporting_ledger_changes",
        "reporting_reconciliation_records",
        "reporting_reconciliation_heads",
        "reporting_reconciliation_changes",
        "reporting_notification_events",
        "reporting_notification_expansions",
        "reporting_status_dirty",
        "reporting_status_dirty_heads",
        "reporting_issue_status_scopes",
    )
    from psycopg import sql

    result = {}
    async with pool.connection() as conn:
        for table in tables:
            rows = await (
                await conn.execute(
                    sql.SQL(
                        "SELECT to_jsonb(t) FROM {} t WHERE account_id = %s"
                        " ORDER BY to_jsonb(t)::text"
                    ).format(sql.Identifier(table)),
                    ("acct_a",),
                )
            ).fetchall()
            result[table] = rows
        result["rows"] = await (
            await conn.execute(
                "SELECT r.reporting_revision_id, r.ordinal, r.row_payload"
                " FROM reporting_revision_rows r"
                " JOIN reporting_revisions v ON v.reporting_revision_id = r.reporting_revision_id"
                " WHERE v.account_id = %s ORDER BY r.reporting_revision_id, r.ordinal",
                ("acct_a",),
            )
        ).fetchall()
    return result


@pytest.mark.parametrize(
    "operation",
    [
        "configuration",
        "obligation",
        "revision",
        "readability",
        "adjustment",
        "consumer_status",
        "issue_open",
        "issue_state",
        "issue_retire",
        "managed_destination",
        "managed_obligation",
        "managed_attempt",
        "managed_materialization",
        "managed_check",
        "managed_receipt",
    ],
)
@pytest.mark.parametrize("failure_position", ["before_dirty", "after_dirty"])
async def test_every_domain_mutation_rolls_back_if_dirty_enqueue_fails(
    notification_harness, monkeypatch, operation, failure_position
):
    h = notification_harness
    mutation = await prepare(h, operation)
    before = await image(h)
    cls = type(h.reliable.store)
    original = cls._dirty_status
    if isinstance(h.reliable.store, InMemoryReportingLedgerStore):

        def fail(self, *args, **kwargs):
            if failure_position == "after_dirty":
                original(self, *args, **kwargs)
            raise OSError("injected enqueue failure")

    else:

        async def fail(self, *args, **kwargs):
            if failure_position == "after_dirty":
                await original(self, *args, **kwargs)
            raise OSError("injected enqueue failure")

    with monkeypatch.context() as context:
        context.setattr(cls, "_dirty_status", fail)
        with pytest.raises(OSError, match="injected"):
            await mutation()
    assert await image(h) == before
    # Rollback does not leave a ghost immutable ID or a consumed feed generation.
    await mutation()
    assert await image(h) != before


@pytest.mark.parametrize("position", ["before_event", "after_event"])
async def test_event_enqueue_failure_rolls_back_revision_and_rows(
    notification_harness, monkeypatch, position
):
    h = notification_harness
    mutation = await prepare(h, "revision")
    before = await image(h)
    cls = type(h.reliable.store)
    original = cls._record_notification
    if isinstance(h.reliable.store, InMemoryReportingLedgerStore):

        def fail(self, *args):
            if position == "after_event":
                original(self, *args)
            raise OSError("event failure")

    else:

        async def fail(self, *args):
            if position == "after_event":
                await original(self, *args)
            raise OSError("event failure")

    with monkeypatch.context() as context:
        context.setattr(cls, "_record_notification", fail)
        with pytest.raises(OSError, match="event failure"):
            await mutation()
    assert await image(h) == before


async def test_postgres_precommit_invisibility_from_distinct_autocommit_pool(monkeypatch):
    from psycopg_pool import AsyncConnectionPool

    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        h = NotificationHarness(reliable)
        mutation = await prepare(h, "revision")
        before = await h.outbox.list_events(account_id="acct_a")
        barrier = Barrier()
        original = PgReportingReconciliationStore._record_notification

        async def pause_after_event(self, conn, event):
            await original(self, conn, event)
            await barrier.pause()
            raise OSError("rollback after insert")

        monkeypatch.setattr(
            PgReportingReconciliationStore, "_record_notification", pause_after_event
        )
        parent = reliable.blobs.pool
        async with AsyncConnectionPool(parent.conninfo, kwargs=parent.kwargs, open=False) as other:
            await other.wait()
            observer = PgReportingReconciliationStore(pool=other)
            observer_outbox = PgReportingOutbox(pool=other)
            task = asyncio.create_task(mutation())
            try:
                await barrier.wait()
                assert (
                    await observer.get_revision(
                        account_id="acct_a", reporting_revision_id="rpr_acct_a_second"
                    )
                    is None
                )
                assert await observer_outbox.list_events(account_id="acct_a") == before
            finally:
                barrier.release()
                with pytest.raises(OSError):
                    await task
            assert (
                await observer.get_revision(
                    account_id="acct_a", reporting_revision_id="rpr_acct_a_second"
                )
                is None
            )
            assert await observer_outbox.list_events(account_id="acct_a") == before


async def test_postgres_partial_fanout_crash_and_changed_membership_never_mix(monkeypatch):
    async with reliable_factory("postgres", notifications=True, autocommit=True) as reliable:
        h = NotificationHarness(reliable)
        h.receiver.install(monkeypatch)
        await seed(h)
        h.subscriptions.put(notification_subscription(subscriber="old-second"))
        original = PgReportingOutbox._insert_delivery
        barrier = Barrier()

        async def insert_then_crash(self, conn, delivery, at):
            await original(self, conn, delivery, at)
            await barrier.pause()
            raise OSError("partial fanout crash")

        with monkeypatch.context() as patch:
            patch.setattr(PgReportingOutbox, "_insert_delivery", insert_then_crash)
            task = asyncio.create_task(h.worker().expand_one(account_id="acct_a"))
            try:
                await barrier.wait()
                assert await h.outbox.list_deliveries(account_id="acct_a") == ()
                h.subscriptions.values.clear()
                h.subscriptions.put(notification_subscription(subscriber="new-only"))
            finally:
                barrier.release()
                with pytest.raises(OSError):
                    await task
        assert await h.outbox.list_deliveries(account_id="acct_a") == ()
        reliable.clock.advance(timedelta(seconds=61))
        await reliable.restart()
        await h.drain()
        rows = await h.outbox.list_deliveries(account_id="acct_a")
        assert [(row.delivery.binding.subscriber_id, row.state) for row in rows] == [
            ("new-only", "complete")
        ]
