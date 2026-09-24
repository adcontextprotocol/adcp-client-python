"""Real database-clock roles in the shared process harness. No injected clock."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import PgReportingReconciliationStore, ReportingDeliveryEscalation
from adcp.reporting.outbox import PgStatusNotificationStore

from .test_reporting_notification_outbox import statement


async def run_status_role(role, settings, *, barrier, emit, bounded, receiver, install_test_socket):
    pause = settings.get("pause")
    paused = False

    async def stop(point, **kwargs):
        nonlocal paused
        if pause == point and not paused:
            paused = True
            await barrier(point, **kwargs)

    class Connection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            result = await bounded(
                super().execute(query, params, **kwargs),
                stage="status_database_statement",
                seconds=18,
            )
            if isinstance(query, str) and query.startswith(
                "INSERT INTO reporting_consumer_statuses"
            ):
                await stop("consumer_status_pre_lifecycle", backend_pid=self.info.backend_pid)
            return result

    async with AsyncConnectionPool(
        settings["conninfo"],
        kwargs={**settings["pool_kwargs"], "autocommit": True},
        min_size=1,
        max_size=1,
        connection_class=Connection,
        open=False,
    ) as pool:
        await bounded(pool.wait(timeout=10), stage="status_pool_open", seconds=12)
        if role == "status_receiver":
            await receiver(pool, settings, lambda: datetime.now(timezone.utc))
            return
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
        escalation = settings.get("escalation_seconds")
        policy = (
            None
            if escalation is None
            else ReportingDeliveryEscalation(
                consumer_mismatch_escalation=timedelta(seconds=escalation),
                operations_contact_email="operations@example.test",
            )
        )
        status = PgStatusNotificationStore(ledger, escalation=policy)
        assert ledger._clock is None and status.outbox._clock is None
        async with pool.connection() as connection:
            pid = connection.info.backend_pid
        if settings.get("start_paused"):
            await barrier("status_worker_ready", backend_pid=pid)

        import adcp.reporting.outbox.status_pg as module

        enqueue, apply = module._enqueue_on, status._apply_on

        async def enqueue_at_boundary(connection, event):
            await enqueue(connection, event)
            await stop("after_event_insert_pre_commit", backend_pid=connection.info.backend_pid)

        async def apply_at_boundary(connection, snapshot, **kwargs):
            if not kwargs.get("baseline"):
                await stop(
                    "after_lifecycle_intent_pre_event", backend_pid=connection.info.backend_pid
                )
            count = await apply(connection, snapshot, **kwargs)
            if kwargs.get("baseline"):
                await stop(
                    "baseline_scopes_pre_high_water", backend_pid=connection.info.backend_pid
                )
            return count

        module._enqueue_on = enqueue_at_boundary
        status._apply_on = apply_at_boundary

        if role == "status_http_worker":
            from adcp.reporting.outbox import ReportingEnvelopeCipher, ReportingNotificationWorker

            from ._reliable_support import (
                FailurePlan,
                ScriptedSigning,
                ScriptedSubscriptions,
                notification_subscription,
            )

            failures = FailurePlan()
            subscriptions = ScriptedSubscriptions(failures)
            subscriptions.put(notification_subscription(events=("reporting.status_changed",)))
            worker = ReportingNotificationWorker(
                outbox=status.outbox,
                subscriptions=subscriptions,
                signing=ScriptedSigning(failures),
                cipher=ReportingEnvelopeCipher(b"e" * 32),
                activity=status.outbox,
            )
            install_test_socket(settings, lambda: datetime.now(timezone.utc))
            worked = False
            for _ in range(40):
                expanded = await worker.expand_one(account_id="acct_a")
                delivered = await worker.deliver_one(account_id="acct_a")
                worked = worked or expanded or delivered
                if not (expanded or delivered):
                    break
            else:
                raise AssertionError("status_http_queue_did_not_converge")
        elif role == "status_baseline":
            worked = await status.baseline(account_id="acct_a")
        elif role == "status_projector":
            worked = False
            for _ in range(settings.get("turns", 1)):
                worked = (await status.project_one(account_id="acct_a")).did_work or worked
        elif role == "status_consumer":
            obligation = await ledger.get_obligation(
                account_id="acct_a", reporting_obligation_id="rpo_acct_a"
            )
            assert obligation is not None
            snapshot = await ledger.read_status_snapshot(account_id="acct_a")
            record = replace(
                statement(obligation),
                consumer_status="unreadable",
                failure_code="access_denied",
                period_start=obligation.period.start,
                period_end=obligation.period.end,
                status_as_of=snapshot.as_of,
                recorded_at=snapshot.as_of,
            )
            _, worked = await ledger.record_consumer_status_with_lifecycle(record)
        else:
            assert role == "status_clock_sweeper"
            lease = await status.claim_due(account_id="acct_a", lease_seconds=60)
            worked = False
            if lease is not None:
                await stop("after_due_claim", backend_pid=pid)
                worked = (await status.complete_due(lease)).did_work
        await stop("after_checkpoint_event_commit_pre_ack", backend_pid=pid)
        emit("done", did_work=worked)
