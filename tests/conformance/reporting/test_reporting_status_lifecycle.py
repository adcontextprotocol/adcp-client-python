"""Lifecycle, complete deadline selection and first-turn candidate repair."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingDeliveryEscalation
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import mismatch_key
from adcp.reporting.outbox import (
    ReportingNotificationError,
    ReportingStatusScope,
    ReportingStatusSweeper,
)

from . import test_reporting_status_projection_contract as _status_contract
from ._generation_support import END, START, configuration, obligation_for, revision_for
from ._reliable_support import reliable_factory
from .test_reporting_notification_outbox import statement

status_harness = _status_contract.status_harness


async def test_mismatch_open_resolve_recur_before_drain_and_restart(status_harness):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    leaf = replace(
        statement(obligation),
        consumer_status="received",
        reporting_revision_id=revision.reporting_revision_id,
        observed_revision_content_sha256=revision.revision_content_sha256,
    )
    await h.ledger.record_consumer_status(leaf)
    await h.status.baseline(account_id="acct_a")
    occurrence_ids = []
    for n, value in enumerate(("unreadable", "received", "unreadable")):
        h.clock.advance()
        leaf = replace(
            leaf,
            reporting_status_id=f"cycle-{n}",
            consumer_status=value,
            supersedes_reporting_status_id=leaf.reporting_status_id,
            status_as_of=h.clock(),
            recorded_at=h.clock(),
            failure_code="transport_failed" if value == "unreadable" else None,
        )
        await h.ledger.record_consumer_status(leaf)
        issue = await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(leaf))
        if value == "received":
            assert issue is None
        else:
            assert issue is not None and issue.opened_at == h.clock()
            occurrence_ids.append(issue.issue_id)
    # Replacing clients must not drop the undrained committed cycles.
    cls = type(h.status)
    await h.reliable.restart()
    h.status = cls(h.ledger)
    await h.drain()
    events = await h.events(consumer="buyer")
    assert [e.cause.health for e in events] == ["action_required", "complete", "action_required"]
    assert occurrence_ids[0] != occurrence_ids[1]
    assert events[0].cause.issue_ids == (occurrence_ids[0],)
    assert events[2].cause.issue_ids == (occurrence_ids[1],)
    assert not await h.events(consumer="")
    status_operation_1 = await h.status.project_one(account_id="acct_a")
    assert not (status_operation_1).did_work


async def test_obligation_missing_attaches_once_and_never_broadens(status_harness):
    h = status_harness
    config = configuration()
    await h.ledger.put_configuration(config)
    obligation = obligation_for(config)
    missing = replace(
        statement(obligation), consumer_status="obligation_missing", reporting_obligation_id=None
    )
    await h.ledger.record_consumer_status(missing)
    issue = await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(missing))
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    broad = dict(snapshot.issue_scopes)[issue.issue_id]
    assert broad.generation_key == config.generation_key and broad.reporting_obligation_id is None
    await h.status.baseline(account_id="acct_a")
    await h.ledger.commit_obligation(obligation)
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    attached = dict(snapshot.issue_scopes)[issue.issue_id]
    assert attached == ReportingStatusScope.for_obligation(obligation, "buyer")
    assert (
        await h.ledger.get_issue(account_id="acct_a", issue_key=issue.issue_key)
    ).issue_id == issue.issue_id
    with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
        await h.ledger.ensure_issue_opened(
            issue_key=issue.issue_key,
            account_id="acct_a",
            consumer_id="buyer",
            observed_at=h.clock(),
            status_scope=broad,
        )
    await h.drain()


@pytest.mark.parametrize("zero_sla,zero_recovery", [(False, False), (True, False), (True, True)])
async def test_readable_healthy_completes_at_period_end_exactly(
    status_harness, zero_sla, zero_recovery
):
    h = status_harness
    config = configuration()
    if zero_sla:
        config = replace(config, schedule=replace(config.schedule, delivery_sla="PT0S"))
    if zero_recovery:
        config = replace(config, automated_recovery_window=timedelta(0))
    obligation, revision, rows = await h.seed(config=config)
    h.clock.now = START + timedelta(minutes=30)
    revision = replace(
        revision, created_at=h.clock(), observed_at=h.clock(), data_through=h.clock()
    )
    await h.ledger.commit_revision(revision, rows)
    await h.status.baseline(account_id="acct_a")
    sweep = ReportingStatusSweeper(h.status)
    assert all(
        c.snapshot["health"] == "healthy" for c in await h.status.checkpoints(account_id="acct_a")
    )
    h.clock.now = END - timedelta(microseconds=1)
    status_operation_2 = await sweep.run_once(account_id="acct_a")
    assert not (status_operation_2).did_work
    h.clock.now = END
    status_operation_3 = await sweep.run_once(account_id="acct_a")
    assert (status_operation_3).events == 2
    (event,) = await h.events()
    assert (event.cause.previous_health, event.cause.health) == ("healthy", "complete")
    status_operation_4 = await sweep.run_once(account_id="acct_a")
    assert not (status_operation_4).did_work
    h.clock.now += timedelta(microseconds=1)
    status_operation_5 = await sweep.run_once(account_id="acct_a")
    assert not (status_operation_5).did_work


@pytest.mark.parametrize("escalation_seconds", [None, 0, 60, 3600, 7200])
async def test_stale_received_grace_and_escalation_share_exact_next_deadline(
    status_harness, escalation_seconds
):
    h = status_harness
    if escalation_seconds is not None:
        h.status.escalation = ReportingDeliveryEscalation(
            consumer_mismatch_escalation=timedelta(seconds=escalation_seconds),
            operations_contact_email="reporting@example.test",
        )
    obligation, revision, _ = await h.seed(readable=True)
    received = replace(
        statement(obligation),
        consumer_status="received",
        reporting_revision_id=revision.reporting_revision_id,
        observed_revision_content_sha256=revision.revision_content_sha256,
    )
    await h.ledger.record_consumer_status(received)
    await h.status.baseline(account_id="acct_a")
    replacement, rows = revision_for(obligation, suffix="restated")
    replacement = replace(
        replacement,
        created_at=h.clock(),
        observed_at=h.clock(),
        supersedes_reporting_revision_id=revision.reporting_revision_id,
    )
    await h.ledger.commit_revision(replacement, rows)
    await h.drain()
    initial = (await h.events(consumer="buyer"))[-1]
    assert initial.cause.health == ("action_required" if escalation_seconds == 0 else "delayed")
    checkpoints = await h.status.checkpoints(account_id="acct_a")
    checkpoint = next(
        c
        for c in checkpoints
        if c.scope == ReportingStatusScope.for_obligation(obligation, "buyer")
    )
    sweep = ReportingStatusSweeper(h.status)
    if escalation_seconds == 0:
        assert checkpoint.next_due_at is None
    else:
        expected = h.clock() + timedelta(seconds=min(3600, escalation_seconds or 3600))
        assert checkpoint.next_due_at == expected
        h.clock.now = expected - timedelta(microseconds=1)
        status_operation_18 = await sweep.run_once(account_id="acct_a")
        assert not (status_operation_18).did_work
        h.clock.now = expected
        status_operation_19 = await sweep.run_once(account_id="acct_a")
        assert (status_operation_19).events == 2
        assert (await h.events(consumer="buyer"))[-1].cause.health == "action_required"
    status_operation_6 = await sweep.run_once(account_id="acct_a")
    assert not (status_operation_6).did_work
    if escalation_seconds == 7200:
        before = (await h.events(consumer="buyer"))[-1]
        later = replacement.created_at + timedelta(seconds=escalation_seconds)
        h.clock.now = later - timedelta(microseconds=1)
        status_operation_20 = await sweep.run_once(account_id="acct_a")
        assert not (status_operation_20).did_work
        h.clock.now = later
        status_operation_21 = await sweep.run_once(account_id="acct_a")
        assert (status_operation_21).events == 2
        after = (await h.events(consumer="buyer"))[-1]
        assert after.cause.previous_health == after.cause.health == "action_required"
        assert after.cause.issue_ids == before.cause.issue_ids
        assert after.cause.fingerprint != before.cause.fingerprint
        assert after.notification_id != before.notification_id
        summary = await ReportingStatusHandler(
            h.ledger, consumer_status_enabled=True, escalation=h.status.escalation
        ).handle({}, caller=ReportingStatusCaller("acct_a", "buyer"))
        assert summary["issues"][0]["recommended_action"] == "contact_buyer"
        h.clock.now += timedelta(microseconds=1)
        status_operation_22 = await sweep.run_once(account_id="acct_a")
        assert not (status_operation_22).did_work


@pytest.mark.parametrize("revision_after_claim", [False, True])
async def test_revision_invalidates_claimed_candidate_and_clears_on_first_turn(
    status_harness, revision_after_claim
):
    h = status_harness
    obligation, revision, rows = await h.seed()
    h.clock.now = obligation.period.expected_at - timedelta(seconds=1)
    await h.status.baseline(account_id="acct_a")
    h.clock.now = obligation.period.expected_at
    if revision_after_claim:
        lease = await h.status.claim_due(account_id="acct_a")
        assert lease is not None
    await h.ledger.commit_revision(revision, rows)
    if not revision_after_claim:
        lease = await h.status.claim_due(account_id="acct_a")
        assert lease is not None
    status_operation_7 = await h.status.complete_due(lease)
    assert (status_operation_7).did_work
    assert [e.cause.health for e in await h.events()] == ["complete"]
    assert all(c.next_due_at is None for c in await h.status.checkpoints(account_id="acct_a"))
    status_operation_8 = await ReportingStatusSweeper(h.status).run_once(account_id="acct_a")
    assert not (status_operation_8).did_work


@pytest.mark.parametrize("mutation", ["readability", "waiver", "agreement", "receipt_and_waiver"])
async def test_activity_after_due_claim_repairs_candidate_without_stale_escalation(
    status_harness, mutation
):
    from ._reconciliation_support import scenario

    h = status_harness
    if mutation == "receipt_and_waiver":
        managed = await scenario(h.ledger)
        obligation, revision = managed.obligation, managed.revision
        await h.ledger.commit_materialization(managed.outcome)
    else:
        obligation, revision, _ = await h.seed(readable=True)
    h.status.escalation = ReportingDeliveryEscalation(
        consumer_mismatch_escalation=timedelta(seconds=60),
        operations_contact_email="reporting@example.test",
    )
    unreadable = replace(
        statement(obligation),
        consumer_status="unreadable",
        failure_code="access_denied",
        reporting_revision_id=revision.reporting_revision_id,
        status_as_of=h.clock(),
        recorded_at=h.clock(),
    )
    await h.ledger.record_consumer_status(unreadable)
    issue = await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(unreadable))
    await h.status.baseline(account_id="acct_a")
    h.clock.now += timedelta(seconds=60)
    lease = await h.status.claim_due(account_id="acct_a")
    assert lease is not None
    if mutation == "readability":
        await h.ledger.set_revision_readable(
            account_id="acct_a",
            reporting_revision_id=revision.reporting_revision_id,
            readable=False,
        )
    elif mutation == "agreement":
        await h.ledger.record_consumer_status(
            replace(
                unreadable,
                reporting_status_id="agree-after-claim",
                supersedes_reporting_status_id=unreadable.reporting_status_id,
                consumer_status="received",
                failure_code=None,
                observed_revision_content_sha256=revision.revision_content_sha256,
                status_as_of=h.clock(),
                recorded_at=h.clock(),
            )
        )
    else:
        async with h.ledger.transaction():
            if mutation == "receipt_and_waiver":
                status_operation_23 = await h.ledger.record_revision_receipt(managed.receipt)
                assert (status_operation_23)[1]
            await h.ledger.set_issue_state(
                issue_key=issue.issue_key, account_id="acct_a", state="waived", at=h.clock()
            )
    status_operation_9 = await h.status.complete_due(lease)
    assert (status_operation_9).did_work
    events = await h.events(consumer="buyer")
    assert len(events) == 1
    assert issue.issue_id not in events[0].cause.issue_ids
    summary = await ReportingStatusHandler(
        h.ledger, consumer_status_enabled=True, escalation=h.status.escalation
    ).handle({}, caller=ReportingStatusCaller("acct_a", "buyer"))
    assert events[0].cause.health == summary["health"]
    assert events[0].cause.issue_ids == tuple(sorted(i["issue_id"] for i in summary["issues"]))
    assert all(c.next_due_at is None for c in await h.status.checkpoints(account_id="acct_a"))
    status_operation_10 = await ReportingStatusSweeper(h.status).run_once(account_id="acct_a")
    assert not (status_operation_10).did_work
    status_operation_11 = await h.status.project_one(account_id="acct_a")
    assert not (status_operation_11).did_work


async def test_expiry_equality_reclaim_stale_ack_and_release(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed()
    h.clock.now = obligation.period.expected_at - timedelta(seconds=1)
    await h.status.baseline(account_id="acct_a")
    h.clock.now = obligation.period.expected_at
    lease = await h.status.claim_due(account_id="acct_a", lease_seconds=1)
    assert lease is not None
    h.clock.now = lease.expires_at
    status_operation_12 = await h.status.complete_due(lease)
    assert not (status_operation_12).did_work
    status_operation_13 = await h.status.release_due(lease)
    assert not status_operation_13
    replacement = await h.status.claim_due(account_id="acct_a", lease_seconds=2)
    assert replacement is not None and replacement.token != lease.token
    status_operation_14 = await h.status.complete_due(replacement)
    assert (status_operation_14).events == 2
    status_operation_15 = await h.status.complete_due(lease)
    assert not (status_operation_15).did_work
    status_operation_16 = await h.status.release_due(replacement)
    assert not status_operation_16


async def test_waiver_recovers_and_agreement_rearms_a_new_occurrence(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    status = replace(
        statement(obligation), consumer_status="unreadable", failure_code="access_denied"
    )
    await h.ledger.record_consumer_status(status)
    await h.status.baseline(account_id="acct_a")
    await h.ledger.set_issue_state(
        issue_key=mismatch_key(status), account_id="acct_a", state="waived", at=h.clock()
    )
    await h.drain()
    summary = await ReportingStatusHandler(h.ledger, consumer_status_enabled=True).handle(
        {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    assert summary["health"] == "complete" and summary["issues"] == []
    assert await h.status.baseline_ready(account_id="acct_a")
    (recovery,) = await h.events(consumer="buyer")
    assert (
        recovery.cause.previous_health == "action_required" and recovery.cause.health == "complete"
    )
    old = await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(status))
    # Still disagreeing while waived never creates repeated occurrences.
    await h.ledger.read_status_snapshot(account_id="acct_a")
    status_operation_17 = await h.status.project_one(account_id="acct_a")
    assert not (status_operation_17).did_work
    revision = (
        await h.ledger.list_revisions(
            account_id="acct_a", reporting_obligation_id=obligation.reporting_obligation_id
        )
    )[0]
    h.clock.advance()
    agreeing = replace(
        status,
        reporting_status_id="agree",
        consumer_status="received",
        reporting_revision_id=revision.reporting_revision_id,
        failure_code=None,
        observed_revision_content_sha256=revision.revision_content_sha256,
        supersedes_reporting_status_id=status.reporting_status_id,
        recorded_at=h.clock(),
        status_as_of=h.clock(),
    )
    await h.ledger.record_consumer_status(agreeing)
    assert await h.ledger.get_issue(account_id="acct_a", issue_key=mismatch_key(status)) == old
    h.clock.advance()
    recur = replace(
        status,
        reporting_status_id="recur",
        supersedes_reporting_status_id="agree",
        recorded_at=h.clock(),
        status_as_of=h.clock(),
    )
    await h.ledger.record_consumer_status(recur)
    await h.drain()
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    new_id = (await h.events(consumer="buyer"))[-1].cause.issue_ids[0]
    new = next(i for i in snapshot.lifecycles if i.issue_id == new_id)
    assert new.issue_id != old.issue_id
    assert (new.issue_key, new.generation) != (old.issue_key, old.generation)
    assert new.opened_at == h.clock() and new.issue_state == "open"
    assert next(i for i in snapshot.lifecycles if i.issue_id == old.issue_id) == old
    assert (await h.events(consumer="buyer"))[-1].cause.issue_ids == (new.issue_id,)
    assert await h.status.baseline_ready(account_id="acct_a")


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_rejected_issue_scope_never_commits_a_lifecycle_move(backend, notifications):
    """A refused scope refinement leaves the issue exactly as it was.

    Default-off stores deliberately keep no rollback copy, so the reference
    store must validate before it moves a record. The status harness always
    enables notifications, which is why the memory arm escaped: with
    ``notifications=False`` an invalid scope used to leave a waived issue
    behind while PostgreSQL rolled the statement back.
    """
    async with reliable_factory(backend, notifications=notifications) as reliable:
        ledger = reliable.store
        opened = await ledger.ensure_issue_opened(
            issue_key="k1", account_id="acct_a", consumer_id="buyer", observed_at=reliable.clock()
        )
        foreign = ReportingStatusScope("acct_b", consumer_id="buyer")
        attempts = (
            lambda: ledger.set_issue_state(
                issue_key="k1",
                account_id="acct_a",
                state="waived",
                at=reliable.clock(),
                status_scope=foreign,
            ),
            lambda: ledger.retire_issue(
                issue_key="k1", account_id="acct_a", at=reliable.clock(), status_scope=foreign
            ),
            lambda: ledger.ensure_issue_opened(
                issue_key="k2",
                account_id="acct_a",
                consumer_id="buyer",
                observed_at=reliable.clock(),
                status_scope=foreign,
            ),
        )
        for attempt in attempts:
            with pytest.raises(ReportingNotificationError, match="invalid_status_scope"):
                await attempt()
            assert await ledger.get_issue(account_id="acct_a", issue_key="k1") == opened
        # The refused occurrence never consumed a generation either, so a
        # later legitimate open is still this key's first occurrence.
        assert await ledger.get_issue(account_id="acct_a", issue_key="k2") is None
        reopened = await ledger.ensure_issue_opened(
            issue_key="k2", account_id="acct_a", consumer_id="buyer", observed_at=reliable.clock()
        )
        assert reopened.generation == 1 and reopened.issue_state == "open"
