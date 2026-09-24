"""Two independently pooled sweepers, database-time equality and named crashes."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    PgReportingReconciliationStore,
    ReportingDeliveryEscalation,
    ReportingScheduleSpec,
)
from adcp.reporting.ledger.status_snapshot import read_snapshot_on
from adcp.reporting.outbox import PgStatusNotificationStore, ReportingStatusSweeper
from adcp.reporting.outbox.status import escalation_identity
from adcp.reporting.outbox.status_pg import _replay_storage

from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from ._reliable_support import service_process
from .test_reporting_notification_outbox import statement
from .test_reporting_notification_process_matrix import case_deadline


async def database_seed(pool, case="expected", *, baseline=True):
    """Seed a historical baseline at an instant derived from a captured DB clock.

    The checkpoint precedes its deadline by one microsecond. Production sweep
    walks that persisted deadline exactly; all child clocks are clock_timestamp.
    No time advancement, wall-clock sleep or ManualClock participates.
    """
    ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
    escalation = (
        30
        if case == "stale_escalation"
        else 60 if case in {"stale_coincident", "mismatch_escalation"} else None
    )
    policy = (
        None
        if escalation is None
        else ReportingDeliveryEscalation(
            consumer_mismatch_escalation=timedelta(seconds=escalation),
            operations_contact_email="operations@example.test",
        )
    )
    status = PgStatusNotificationStore(ledger, escalation=policy)
    await status.create_schema()
    async with pool.connection() as c:
        (at,) = await (await c.execute("SELECT clock_timestamp()")).fetchone()
    anchor = at - timedelta(
        hours=(
            1
            if case == "period_end"
            else (
                3
                if case
                in {
                    "recovery",
                    "stale_grace",
                    "stale_escalation",
                    "stale_coincident",
                    "mismatch_escalation",
                }
                else 2
            )
        )
    )
    config = replace(
        configuration(),
        activated_at=anchor,
        deactivated_at=anchor + timedelta(hours=1),
        schedule=ReportingScheduleSpec(
            "PT1H", "PT1M" if case.startswith("stale") else "PT1H", period_anchor=anchor
        ),
        automated_recovery_window=timedelta(hours=0 if case == "zero_recovery" else 1),
    )
    await ledger.put_configuration(config)
    obligation = await ledger.commit_obligation(obligation_for(config))
    if case in {
        "period_end",
        "stale_grace",
        "stale_escalation",
        "stale_coincident",
        "mismatch_escalation",
    }:
        first, rows = revision_for(obligation)
        first = replace(
            first,
            created_at=anchor + timedelta(minutes=30),
            observed_at=anchor + timedelta(minutes=30),
            data_through=anchor + timedelta(minutes=30),
        )
        await ledger.commit_revision(first, rows)
        if case != "period_end":
            opened = at - timedelta(seconds=30 if case == "stale_escalation" else 60)
            record = replace(
                statement(obligation),
                period_start=obligation.period.start,
                period_end=obligation.period.end,
                consumer_status="unreadable" if case == "mismatch_escalation" else "received",
                reporting_revision_id=first.reporting_revision_id,
                observed_revision_content_sha256=first.revision_content_sha256,
                failure_code="access_denied" if case == "mismatch_escalation" else None,
                recorded_at=(
                    opened if case == "mismatch_escalation" else opened - timedelta(seconds=1)
                ),
                status_as_of=(
                    opened if case == "mismatch_escalation" else opened - timedelta(seconds=1)
                ),
            )
            await ledger.record_consumer_status(record)
            if case.startswith("stale"):
                second, rows = revision_for(obligation, suffix="second")
                await ledger.commit_revision(
                    replace(
                        second,
                        created_at=opened,
                        observed_at=opened,
                        supersedes_reporting_revision_id=first.reporting_revision_id,
                    ),
                    rows,
                )
    if baseline:
        async with status._transaction("acct_a") as c:
            snapshot = await read_snapshot_on(
                c, account_id="acct_a", as_of=at - timedelta(microseconds=1)
            )
            (through,) = await (
                await c.execute(
                    "SELECT max_sequence FROM reporting_status_dirty_heads"
                    " WHERE account_id='acct_a'"
                )
            ).fetchone()
            await c.execute(
                "INSERT INTO reporting_status_accounts (account_id, policy)"
                " VALUES ('acct_a', %s::jsonb)",
                (json.dumps(escalation_identity(policy)),),
            )
            await status._apply_on(c, snapshot, through=through, baseline=True)
            await c.execute(
                "UPDATE reporting_status_accounts SET baseline_complete=TRUE,"
                " baseline_highwater=%s, dirty_sequence=%s, baseline_at=%s,"
                " replay_lifecycles=%s::jsonb, selector_target_version=2, selector_transition='complete' WHERE account_id='acct_a'",
                (through, through, snapshot.as_of, _replay_storage(snapshot)),
            )
    return ledger, status, at, escalation


async def settle_status(pool):
    async with pool.connection() as c, c.transaction():
        await PgReportingReconciliationStore._lock_account(c, "acct_a")


@pytest.mark.parametrize(
    "case,previous,health",
    [
        ("expected", "waiting", "delayed"),
        ("recovery", "delayed", "action_required"),
        ("zero_recovery", "waiting", "action_required"),
        ("period_end", "healthy", "complete"),
        ("stale_grace", "delayed", "action_required"),
        ("stale_escalation", "delayed", "action_required"),
        ("stale_coincident", "delayed", "action_required"),
        ("mismatch_escalation", "action_required", "action_required"),
    ],
)
@case_deadline
async def test_two_real_database_clock_sweepers_at_every_exact_deadline(case, previous, health):
    async with isolated_reporting_pool(autocommit=True) as pool:
        _, status, at, escalation = await database_seed(pool, case)
        checkpoints = await status.checkpoints(account_id="acct_a")
        assert len([c for c in checkpoints if c.next_due_at == at]) == 2
        async with (
            service_process(
                pool,
                "status_clock_sweeper",
                start_paused=True,
                pause="after_due_claim",
                escalation_seconds=escalation,
            ) as first,
            service_process(
                pool,
                "status_clock_sweeper",
                start_paused=True,
                pause="after_due_claim",
                escalation_seconds=escalation,
            ) as second,
        ):
            ready = await asyncio.gather(
                first.event("status_worker_ready"), second.event("status_worker_ready")
            )
            assert ready[0]["backend_pid"] != ready[1]["backend_pid"]
            for child in (first, second):
                await child.send(**{"continue": "status_worker_ready"})
            await asyncio.gather(first.event("after_due_claim"), second.event("after_due_claim"))
            leases = [
                c.lease_token
                for c in await status.checkpoints(account_id="acct_a")
                if c.lease_token
            ]
            assert len(set(leases)) == len(leases) == 2
            for child in (first, second):
                await child.send(**{"continue": "after_due_claim"})
            await asyncio.gather(first.event("done"), second.event("done"))
            await asyncio.gather(first.finish(), second.finish())
        events = await status.outbox.list_events(account_id="acct_a")
        assert len(events) == 2
        assert {(e.cause.previous_health, e.cause.health) for e in events} == {(previous, health)}
        assert all(e.fired_at >= at for e in events)
        assert len({e.notification_id for e in events}) == 2
        status_operation_1 = await ReportingStatusSweeper(status).run_once(account_id="acct_a")
        assert not (status_operation_1).did_work


@pytest.mark.parametrize(
    "crash",
    [
        "after_due_claim",
        "after_lifecycle_intent_pre_event",
        "after_event_insert_pre_commit",
        "after_checkpoint_event_commit_pre_ack",
    ],
)
@case_deadline
async def test_real_clock_checkpoint_event_crash_restart_converges_once(crash):
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger, status, _, _ = await database_seed(pool, "zero_recovery")
        async with service_process(pool, "status_clock_sweeper", pause=crash) as child:
            await child.event(crash)
            before = await status.outbox.list_events(account_id="acct_a")
            assert len(before) == (2 if crash == "after_checkpoint_event_commit_pre_ack" else 0)
            await child.kill()
        await settle_status(pool)
        # Capture expiry into durable state; reclaim uses production DB time.
        async with status._transaction("acct_a") as c:
            await c.execute(
                "UPDATE reporting_status_scope_checkpoints SET lease_expires_at=clock_timestamp()"
                " WHERE lease_token IS NOT NULL"
            )
        async with service_process(pool, "status_clock_sweeper") as restarted:
            await restarted.event("done")
            await restarted.finish()
        status = PgStatusNotificationStore(ledger)
        after = await status.outbox.list_events(account_id="acct_a")
        assert len(after) == 2
        if before:
            assert after == before
        status_operation_2 = await ReportingStatusSweeper(status).run_once(account_id="acct_a")
        assert not (status_operation_2).did_work


@case_deadline
async def test_consumer_process_crash_before_lifecycle_and_baseline_pre_highwater_restart():
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger, status, _, _ = await database_seed(pool, "period_end", baseline=False)
        async with service_process(
            pool, "status_consumer", pause="consumer_status_pre_lifecycle"
        ) as child:
            await child.event("consumer_status_pre_lifecycle")
            assert not await ledger.list_consumer_statuses(account_id="acct_a", consumer_id="buyer")
            await child.kill()
        await settle_status(pool)
        async with service_process(pool, "status_consumer") as child:
            await child.event("done")
            await child.finish()
        assert (
            len(await ledger.list_consumer_statuses(account_id="acct_a", consumer_id="buyer")) == 1
        )
        async with service_process(
            pool, "status_baseline", pause="baseline_scopes_pre_high_water"
        ) as child:
            await child.event("baseline_scopes_pre_high_water")
            async with pool.connection() as c:
                assert (
                    await (
                        await c.execute("SELECT count(*) FROM reporting_status_accounts")
                    ).fetchone()
                )[0] == 0
            await child.kill()
        await settle_status(pool)
        assert not await status.baseline_ready(account_id="acct_a")
        async with service_process(pool, "status_baseline") as child:
            await child.event("done")
            await child.finish()
        assert await status.baseline_ready(account_id="acct_a")
        assert not await status.outbox.list_events(account_id="acct_a")
        status_operation_3 = await status.project_one(account_id="acct_a")
        assert not (status_operation_3).did_work
