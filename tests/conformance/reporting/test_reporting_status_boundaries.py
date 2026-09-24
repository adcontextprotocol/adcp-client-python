"""Scope collision fanout, baseline/source locking and C-only SQL integrity."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    PgReportingLedgerStore,
    derive_period,
)
from adcp.reporting.outbox import (
    PgStatusNotificationStore,
    ReportingNotificationError,
)
from adcp.reporting.outbox.status_schema import validate_status_schema

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from ._reliable_support import Barrier
from .test_reporting_notification_outbox import statement

status_harness = _contract.status_harness


async def test_config_obligation_collisions_siblings_and_broad_issue_fanout(status_harness):
    h = status_harness
    config = replace(
        configuration(),
        delivery_config_id="shared",
        deactivated_at=configuration().deactivated_at + timedelta(hours=1),
    )
    await h.ledger.put_configuration(config)
    first = replace(obligation_for(config), reporting_obligation_id="shared")
    second_period = derive_period(
        config.schedule, account_timezone=config.account_timezone, ordinal=1
    )
    sibling = replace(
        first,
        reporting_obligation_id="sibling",
        period=second_period,
        scope_resolved_at=second_period.end,
        automated_recovery_deadline_at=second_period.expected_at + config.automated_recovery_window,
    )
    other_config = replace(configuration(), delivery_config_id="sibling")
    await h.ledger.put_configuration(other_config)
    other = replace(obligation_for(other_config), reporting_obligation_id="other")
    revisions = []
    for i, obligation in enumerate((first, sibling, other)):
        await h.ledger.commit_obligation(obligation)
        revision, rows = revision_for(obligation, suffix=str(i))
        revisions.append(revision)
        await h.ledger.commit_revision(revision, rows)
        for consumer in ("buyer", "auditor"):
            await h.ledger.record_consumer_status(
                replace(
                    statement(obligation, consumer),
                    reporting_status_id=f"status-{consumer}-{i}",
                    consumer_status="received",
                    period_start=obligation.period.start,
                    period_end=obligation.period.end,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                )
            )
    await h.status.baseline(account_id="acct_a")
    checkpoints = await h.status.checkpoints(account_id="acct_a")
    assert len({c.scope.checkpoint_key for c in checkpoints}) == len(checkpoints) == 15
    await h.ledger.set_revision_readable(
        account_id="acct_a",
        reporting_revision_id=revisions[0].reporting_revision_id,
        readable=False,
    )
    await h.drain()
    events = await h.status.outbox.list_events(account_id="acct_a")
    assert len(events) == 6
    assert {
        (e.cause.scope.generation_key.delivery_config_id, e.cause.scope.reporting_obligation_id)
        for e in events
    } == {("shared", None), ("shared", "shared")}
    assert {e.consumer_namespace for e in events} == {"", "buyer", "auditor"}
    for checkpoint in await h.status.checkpoints(account_id="acct_a"):
        expected = int(
            checkpoint.scope.generation_key == first.generation_key
            and checkpoint.scope.reporting_obligation_id in {None, "shared"}
        )
        assert checkpoint.generation == expected
    await h.ledger.ensure_issue_opened(
        account_id="acct_a",
        consumer_id="buyer",
        issue_key="opaque-legacy-condition-with-no-parseable-scope",
        observed_at=h.clock(),
    )
    await h.drain()
    new = [
        e
        for e in await h.status.outbox.list_events(account_id="acct_a")
        if e.notification_id not in {old.notification_id for old in events}
    ]
    assert len(new) == 5 and {e.consumer_namespace for e in new} == {"buyer"}
    assert {e.cause.scope.generation_key for e in new} == {
        first.generation_key,
        other.generation_key,
    }
    status_operation_1 = await h.status.project_one(account_id="acct_a")
    assert not (status_operation_1).did_work


async def test_baseline_serializes_a_mutation_immediately_before_and_after_its_highwater(
    monkeypatch,
):
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    attempted = asyncio.get_running_loop().create_future()

    class WriterConnection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            if isinstance(query, str) and "pg_advisory_xact_lock" in query and not attempted.done():
                attempted.set_result(self.info.backend_pid)
            return await super().execute(query, params, **kwargs)

    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger = PgReportingLedgerStore(pool=pool, notifications=True)
        status = PgStatusNotificationStore(ledger)
        await status.create_schema()
        config = configuration()
        await ledger.put_configuration(config)
        obligation = await ledger.commit_obligation(obligation_for(config))
        revision, rows = revision_for(obligation)
        await ledger.commit_revision(revision, rows)
        await ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=False,
        )
        gate, original = Barrier(), status._apply_on

        async def pause(connection, snapshot, **kwargs):
            result = await original(connection, snapshot, **kwargs)
            if kwargs.get("baseline"):
                await gate.pause()
            return result

        monkeypatch.setattr(status, "_apply_on", pause)
        async with AsyncConnectionPool(
            pool.conninfo,
            kwargs=pool.kwargs,
            connection_class=WriterConnection,
            min_size=1,
            max_size=1,
            open=False,
        ) as other:
            writer = PgReportingLedgerStore(pool=other, notifications=True)
            baseline = asyncio.create_task(status.baseline(account_id="acct_a"))
            await gate.wait()
            mutation = asyncio.create_task(
                writer.set_revision_readable(
                    account_id="acct_a",
                    reporting_revision_id=revision.reporting_revision_id,
                    readable=True,
                )
            )
            pid = await asyncio.wait_for(attempted, 5)

            async def blocked():
                async with pool.connection() as conn:
                    while True:
                        blockers = (
                            await (
                                await conn.execute("SELECT pg_blocking_pids(%s)", (pid,))
                            ).fetchone()
                        )[0]
                        if blockers:
                            return
                        await asyncio.sleep(0)

            await asyncio.wait_for(blocked(), 5)
            async with pool.connection() as conn:
                assert (
                    await (
                        await conn.execute(
                            "SELECT count(*) FROM reporting_status_accounts WHERE baseline_complete"
                        )
                    ).fetchone()
                )[0] == 0
            gate.release()
            await asyncio.wait_for(asyncio.gather(baseline, mutation), 10)
        assert not await status.outbox.list_events(account_id="acct_a")
        status_operation_2 = await status.project_one(account_id="acct_a")
        assert (status_operation_2).events == 2
        assert {
            (e.cause.previous_health, e.cause.health)
            for e in await status.outbox.list_events(account_id="acct_a")
        } == {("action_required", "complete")}
        status_operation_3 = await status.project_one(account_id="acct_a")
        assert not (status_operation_3).did_work
        async with pool.connection() as conn:
            captured = await (
                await conn.execute(
                    "SELECT b.as_of >= max((d.snapshot->>'changed_at')::timestamptz)"
                    " FROM reporting_status_boundaries b JOIN reporting_status_dirty d"
                    " ON d.account_id=b.account_id"
                    " AND d.sequence BETWEEN b.first_sequence AND b.through"
                    " GROUP BY b.account_id, b.transaction_id, b.as_of"
                )
            ).fetchall()
            assert captured and all(row[0] for row in captured)


@pytest.mark.parametrize(
    "damage",
    [
        "DROP INDEX reporting_status_scope_due",
        (
            "ALTER TABLE reporting_status_notification_events DISABLE TRIGGER"
            " reporting_status_event_guard"
        ),
        (
            "ALTER TABLE reporting_status_scope_checkpoints DISABLE TRIGGER"
            " reporting_status_scope_guard"
        ),
        "ALTER TABLE reporting_status_dirty DISABLE TRIGGER reporting_status_boundary_mark",
        (
            "ALTER TABLE reporting_status_boundary_writes DISABLE TRIGGER"
            " reporting_status_boundary_capture"
        ),
        (
            "ALTER TABLE reporting_status_webhook_attempts DISABLE TRIGGER"
            " reporting_status_webhook_attempt_guard"
        ),
    ],
)
async def test_status_manifest_damage_fails_only_its_owned_feature(damage):
    from adcp.reporting.outbox._schema import validate_schema

    async with isolated_reporting_pool(autocommit=True) as pool:
        status = PgStatusNotificationStore(PgReportingLedgerStore(pool=pool, notifications=True))
        await status.create_schema()
        async with pool.connection() as conn:
            await conn.execute(damage)
            await validate_schema(conn, activity=True)  # Every B object is unchanged.
            with pytest.raises(ReportingNotificationError, match="status_schema_unready"):
                await validate_status_schema(conn, activity=True)
            if "webhook" in damage:
                await validate_status_schema(conn)  # Status does not depend on activity.


async def test_boundary_rejects_source_writes_after_forced_early_capture():
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger = PgReportingLedgerStore(pool=pool, notifications=True)
        status = PgStatusNotificationStore(ledger)
        await status.create_schema()
        import psycopg

        with pytest.raises(psycopg.Error, match="status boundary was already captured"):
            async with ledger.transaction():
                await ledger.put_configuration(configuration())
                async with ledger._connection() as conn:
                    await conn.execute(
                        "SET CONSTRAINTS reporting_status_boundary_capture IMMEDIATE"
                    )
                await ledger.commit_obligation(obligation_for(configuration()))
        assert not await ledger.list_configurations(account_id="acct_a")
        async with pool.connection() as conn:
            assert (
                await (
                    await conn.execute("SELECT count(*) FROM reporting_status_boundaries")
                ).fetchone()
            )[0] == 0
