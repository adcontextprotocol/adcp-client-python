"""One semantic contract for the memory reference and the durable C participant."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import StatusProjectionInput, project_status_scope
from adcp.reporting.outbox import (
    InMemoryStatusNotificationStore,
    PgStatusNotificationStore,
    ReportingStatusScope,
    ReportingStatusSweeper,
    StatusChanged,
    validate_notification_payload,
)

from ._generation_support import configuration, obligation_for, revision_for
from ._reliable_support import reliable_factory
from .test_reporting_notification_outbox import statement


@dataclass
class StatusHarness:
    reliable: object
    status: object

    @property
    def ledger(self):
        return self.reliable.store

    @property
    def clock(self):
        return self.reliable.clock

    async def seed(self, *, readable=False, config=None):
        config = config or configuration()
        await self.ledger.put_configuration(config)
        obligation = await self.ledger.commit_obligation(obligation_for(config))
        revision, rows = revision_for(obligation)
        if readable:
            await self.ledger.commit_revision(revision, rows)
        return obligation, revision, rows

    async def drain(self):
        for _ in range(80):
            turn = await self.status.project_one(account_id="acct_a")
            if not turn.did_work:
                return
        pytest.fail("dirty cursor failed to become idle")

    async def events(self, *, obligation=True, consumer=""):
        return tuple(
            sorted(
                (
                    e
                    for e in await self.status.outbox.list_events(account_id="acct_a")
                    if bool(e.cause.scope.reporting_obligation_id) == obligation
                    and e.consumer_namespace == consumer
                ),
                key=lambda e: e.cause.checkpoint_generation,
            )
        )


@pytest.fixture(params=["memory", "postgres"])
async def status_harness(request):
    async with reliable_factory(request.param, notifications=True) as reliable:
        cls = (
            InMemoryStatusNotificationStore
            if isinstance(reliable.store, InMemoryReportingLedgerStore)
            else PgStatusNotificationStore
        )
        status = cls(reliable.store)
        await status.create_schema()
        yield StatusHarness(reliable, status)


async def test_baseline_previous_health_and_ordered_readability_cycles(status_harness):
    h = status_harness
    _, revision, _ = await h.seed(readable=True)
    status_operation_1 = await h.status.baseline(account_id="acct_a")
    assert status_operation_1
    status_operation_2 = await h.status.baseline(account_id="acct_a")
    assert not status_operation_2
    assert await h.status.baseline_ready(account_id="acct_a")
    assert not await h.status.outbox.list_events(account_id="acct_a")
    for readable in (False, True, False, True):
        h.clock.advance()
        await h.ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=readable,
        )
    await h.drain()
    for aggregate in (True, False):
        events = await h.events(obligation=aggregate)
        assert [e.cause.health for e in events] == [
            "action_required",
            "complete",
            "action_required",
            "complete",
        ]
        assert [e.cause.previous_health for e in events] == [
            "complete",
            "action_required",
            "complete",
            "action_required",
        ]
        assert [e.cause.checkpoint_generation for e in events] == [1, 2, 3, 4]
        assert len({e.notification_id for e in events}) == 4
        for event in events:
            assert isinstance(event.cause, StatusChanged)
            payload = json.loads(event.body(subscriber_id="buyer", idempotency_key="1" * 32))
            validate_notification_payload(payload)
            assert ("reporting_obligation_id" in payload) == aggregate
            assert payload.get("issue_ids", "absent") is not None


async def test_one_source_transaction_never_exposes_intermediate_rows(status_harness):
    h = status_harness
    _, revision, _ = await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    async with h.ledger.transaction():
        for readable in (False, True):
            await h.ledger.set_revision_readable(
                account_id="acct_a",
                reporting_revision_id=revision.reporting_revision_id,
                readable=readable,
            )
    turn = await h.status.project_one(account_id="acct_a")
    assert turn.did_work and turn.events == 0
    status_operation_3 = await h.status.project_one(account_id="acct_a")
    assert not (status_operation_3).did_work
    assert not await h.events()


@pytest.mark.parametrize("zero_recovery", [False, True])
@pytest.mark.parametrize("zero_sla", [False, True])
async def test_exact_expected_recovery_and_second_turn_idle(
    status_harness, zero_recovery, zero_sla
):
    h = status_harness
    config = configuration()
    if zero_recovery:
        config = replace(config, automated_recovery_window=timedelta(0))
    if zero_sla:
        config = replace(config, schedule=replace(config.schedule, delivery_sla="PT0S"))
    obligation, _, _ = await h.seed(config=config)
    h.clock.now = obligation.period.expected_at - timedelta(microseconds=1)
    await h.status.baseline(account_id="acct_a")
    sweepers = (ReportingStatusSweeper(h.status), ReportingStatusSweeper(h.status))
    status_operation_4 = await sweepers[0].run_once(account_id="acct_a")
    assert not (status_operation_4).did_work
    h.clock.now = obligation.period.expected_at
    status_operation_5 = await sweepers[0].run_once(account_id="acct_a")
    assert (status_operation_5).did_work
    status_operation_6 = await sweepers[1].run_once(account_id="acct_a")
    assert not (status_operation_6).did_work
    first = (await h.events())[0]
    assert first.cause.previous_health == "waiting"
    assert first.cause.health == ("action_required" if zero_recovery else "delayed")
    h.clock.now = obligation.automated_recovery_deadline_at
    second = await sweepers[1].run_once(account_id="acct_a")
    assert second.did_work is (not zero_recovery)
    assert [e.cause.health for e in await h.events()] == (
        ["action_required"] if zero_recovery else ["delayed", "action_required"]
    )
    status_operation_7 = await sweepers[0].run_once(account_id="acct_a")
    assert not (status_operation_7).did_work
    assert all(c.next_due_at is None for c in await h.status.checkpoints(account_id="acct_a"))
    h.clock.now += timedelta(microseconds=1)
    status_operation_8 = await sweepers[1].run_once(account_id="acct_a")
    assert not (status_operation_8).did_work


async def test_new_scope_initial_fire_is_distinct_from_migration_baseline(status_harness):
    h = status_harness
    await h.status.baseline(account_id="acct_a")
    config = replace(configuration(), automated_recovery_window=timedelta(0))
    await h.seed(config=config)
    await h.drain()
    (event,) = await h.events()
    assert event.cause.health == "action_required"
    assert event.cause.previous_health is None
    payload = json.loads(event.body(subscriber_id="buyer", idempotency_key="2" * 32))
    assert "previous_health" not in payload
    assert payload["issue_ids"]


async def test_pure_handler_event_parity_and_feed_purpose_filter(status_harness):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    await h.status.baseline(account_id="acct_a")
    await h.ledger.set_revision_readable(
        account_id="acct_a", reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    await h.drain()
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    caller = ReportingStatusCaller("acct_a", "buyer")
    handler = ReportingStatusHandler(h.ledger)
    summary = handler.render_snapshot({"view": "summary"}, caller=caller, snapshot=snapshot)
    result = project_status_scope(
        StatusProjectionInput(snapshot, ReportingStatusScope.for_obligation(obligation))
    )
    event = (await h.events())[-1]
    assert summary["health"] == result.health == event.cause.health
    assert summary["issues"] == [i.to_wire() for i in result.issues]
    assert event.cause.issue_ids == result.issue_ids
    filtered = handler.render_snapshot(
        {"view": "periods", "feed_purposes": ["billing"]}, caller=caller, snapshot=snapshot
    )
    assert filtered["periods"] == filtered["revisions"] == []


async def test_two_colliding_private_consumers_receive_every_seller_change(status_harness):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    for consumer in ("buyer", "auditor"):
        value = replace(
            statement(obligation, consumer),
            reporting_status_id=f"statement-{consumer}",
            consumer_status="received",
            reporting_revision_id=revision.reporting_revision_id,
            observed_revision_content_sha256=revision.revision_content_sha256,
        )
        await h.ledger.record_consumer_status(value)
    await h.status.baseline(account_id="acct_a")
    for readable in (False, True):
        h.clock.advance()
        await h.ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=readable,
        )
    await h.drain()
    for consumer in ("", "buyer", "auditor"):
        for obligation_scope in (False, True):
            events = await h.events(consumer=consumer, obligation=obligation_scope)
            assert [e.cause.health for e in events] == ["action_required", "complete"]
            assert all(e.consumer_namespace == consumer for e in events)


async def test_full_issue_semantics_and_seventeenth_issue_change(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    scope = ReportingStatusScope.for_obligation(obligation, "buyer")
    issues = []
    for n in range(18):
        issues.append(
            await h.ledger.ensure_issue_opened(
                issue_key=f"opaque-{n}",
                account_id="acct_a",
                consumer_id="buyer",
                observed_at=h.clock(),
                status_scope=scope,
            )
        )
    await h.status.baseline(account_id="acct_a")
    last = sorted(issues, key=lambda i: i.issue_id)[-1]
    await h.ledger.set_issue_state(
        issue_key=last.issue_key, account_id="acct_a", state="acknowledged", at=h.clock()
    )
    await h.drain()
    (event,) = await h.events(consumer="buyer")
    assert event.cause.previous_health == event.cause.health == "action_required"
    assert len(event.cause.issue_ids) == 16 and last.issue_id not in event.cause.issue_ids
    assert not await h.events(consumer="auditor")
    assert not await h.events(consumer="")


async def test_partial_coverage_has_a_public_schema_valid_issue(status_harness):
    h = status_harness
    config = configuration()
    await h.status.baseline(account_id="acct_a")
    await h.ledger.put_configuration(config)
    obligation = replace(obligation_for(config), coverage_status="partial")
    await h.ledger.commit_obligation(obligation)
    await h.drain()
    summary = await ReportingStatusHandler(h.ledger).handle(
        {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    coverage = next(i for i in summary["issues"] if i["code"] == "REPORTING_COVERAGE_INCOMPLETE")
    assert coverage["issue_id"] in (await h.events())[-1].cause.issue_ids
    assert await h.status.baseline_ready(account_id="acct_a")
