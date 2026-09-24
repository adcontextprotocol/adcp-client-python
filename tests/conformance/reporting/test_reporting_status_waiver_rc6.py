"""An rc.6 bilateral waiver covers one disagreement, never a later statement."""

from dataclasses import replace

import pytest

from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import mismatch_key
from adcp.reporting.outbox import PgStatusNotificationStore, ReportingNotificationError
from adcp.reporting.outbox.status_schema import validate_status_schema

from . import test_reporting_status_projection_contract as _status_contract
from ._generation_support import isolated_reporting_pool, revision_for
from .test_reporting_notification_outbox import statement

status_harness = _status_contract.status_harness


@pytest.mark.parametrize("advance_clock", [False, True])
async def test_later_conflicting_statement_is_not_covered_by_a_waiver(
    status_harness, advance_clock
):
    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    original = replace(
        statement(obligation), consumer_status="unreadable", failure_code="access_denied"
    )
    await h.ledger.record_consumer_status(original)
    await h.status.baseline(account_id="acct_a")
    # The adopter has obtained bilateral agreement for this exact occurrence.
    # external_ref is correlation only, not evidence or proof of that agreement.
    waived = await h.ledger.set_issue_state(
        issue_key=mismatch_key(original),
        account_id="acct_a",
        state="waived",
        at=h.clock(),
        external_ref="private-waiver-audit",
    )
    await h.drain()
    handler = ReportingStatusHandler(h.ledger, consumer_status_enabled=True)
    caller = ReportingStatusCaller("acct_a", "buyer")
    recovered = await handler.handle({"view": "summary"}, caller=caller)
    assert recovered["health"] == "complete" and recovered["issues"] == []
    (recovery,) = await h.events(consumer="buyer")
    assert recovery.cause.previous_health == "action_required"
    assert recovery.cause.health == "complete" and not recovery.cause.issue_ids

    if advance_clock:
        h.clock.advance()
    later = replace(
        original,
        reporting_status_id="later-conflicting-statement",
        supersedes_reporting_status_id=original.reporting_status_id,
        recorded_at=h.clock(),
        status_as_of=h.clock(),
    )
    await h.ledger.record_consumer_status(later)
    current = await handler.handle({"view": "summary"}, caller=caller)
    assert current["health"] == "action_required"
    (issue,) = current["issues"]
    assert issue["issue_id"] != waived.issue_id
    assert issue["reporting_status_id"] == later.reporting_status_id
    await h.drain()
    assert (await h.events(consumer="buyer"))[-1].cause.health == "action_required"
    retained = await h.ledger.get_issue(account_id="acct_a", issue_key=waived.issue_key)
    assert retained == waived


async def test_waiver_does_not_cover_a_changed_seller_diagnosis(status_harness):
    h = status_harness
    obligation, revision, _ = await h.seed(readable=True)
    received = replace(
        statement(obligation),
        consumer_status="received",
        reporting_revision_id=revision.reporting_revision_id,
        observed_revision_content_sha256=revision.revision_content_sha256,
    )
    await h.ledger.record_consumer_status(received)
    h.clock.advance()
    second, rows = revision_for(obligation, suffix="waived-restatement")
    second = replace(
        second,
        supersedes_reporting_revision_id=revision.reporting_revision_id,
        created_at=h.clock(),
        observed_at=h.clock(),
    )
    await h.ledger.commit_revision(second, rows)
    await h.status.baseline(account_id="acct_a")
    waived = await h.ledger.set_issue_state(
        issue_key=mismatch_key(received), account_id="acct_a", state="waived", at=h.clock()
    )
    await h.drain()
    handler = ReportingStatusHandler(h.ledger, consumer_status_enabled=True)
    caller = ReportingStatusCaller("acct_a", "buyer")
    recovered = await handler.handle({"view": "summary"}, caller=caller)
    assert recovered["health"] == "complete" and recovered["issues"] == []
    h.clock.advance()
    third, rows = revision_for(obligation, suffix="new-restatement")
    third = replace(
        third,
        supersedes_reporting_revision_id=second.reporting_revision_id,
        created_at=h.clock(),
        observed_at=h.clock(),
    )
    await h.ledger.commit_revision(third, rows)
    changed = await handler.handle({"view": "summary"}, caller=caller)
    assert changed["health"] in {"delayed", "action_required"}
    (issue,) = changed["issues"]
    assert issue["issue_id"] != waived.issue_id
    assert issue["reporting_status_id"] == received.reporting_status_id
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    current = next(i for i in snapshot.lifecycles if i.issue_id == issue["issue_id"])
    assert current.opened_at == h.clock()
    assert next(i for i in snapshot.lifecycles if i.issue_id == waived.issue_id) == waived
    await h.drain()
    assert (await h.events(consumer="buyer"))[-1].cause.issue_ids == (current.issue_id,)


async def test_successive_waivers_preserve_each_terminal_record_and_other_callers(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    original = replace(
        statement(obligation), consumer_status="unreadable", failure_code="access_denied"
    )
    await h.ledger.record_consumer_status(original)
    await h.ledger.record_consumer_status(
        replace(original, consumer_id="auditor", reporting_status_id="auditor-statement")
    )
    await h.status.baseline(account_id="acct_a")
    handler = ReportingStatusHandler(h.ledger, consumer_status_enabled=True)
    caller = ReportingStatusCaller("acct_a", "buyer")
    retained = []
    current = original
    for ordinal in range(3):
        response = await handler.handle({"view": "summary"}, caller=caller)
        (projected,) = response["issues"]
        snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
        occurrence = next(i for i in snapshot.lifecycles if i.issue_id == projected["issue_id"])
        waived = await h.ledger.set_issue_state(
            issue_key=occurrence.issue_key, account_id="acct_a", state="waived", at=h.clock()
        )
        retained.append(waived)
        h.clock.advance()
        repeated = await h.ledger.set_issue_state(
            issue_key=occurrence.issue_key, account_id="acct_a", state="waived", at=h.clock()
        )
        assert repeated == waived
        await h.drain()
        recovered = await handler.handle({"view": "summary"}, caller=caller)
        assert recovered["health"] == "complete" and recovered["issues"] == []
        auditor = await handler.handle(
            {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "auditor")
        )
        assert auditor["health"] == "action_required"
        if ordinal < 2:
            later = replace(
                current,
                reporting_status_id=f"recurrence-{ordinal}",
                supersedes_reporting_status_id=current.reporting_status_id,
                recorded_at=h.clock(),
                status_as_of=h.clock(),
            )
            await h.ledger.record_consumer_status(later)
            current = later
    snapshot = await h.ledger.read_status_snapshot(account_id="acct_a")
    assert len({i.issue_id for i in retained}) == 3
    assert all(i in snapshot.lifecycles for i in retained)


@pytest.mark.parametrize("column", ["reporting_status_id", "conflict_sha256"])
async def test_missing_waiver_binding_column_fails_readiness(column):
    from adcp.reporting.ledger import PgReportingLedgerStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        status = PgStatusNotificationStore(PgReportingLedgerStore(pool=pool, notifications=True))
        await status.create_schema()
        async with pool.connection() as conn:
            await validate_status_schema(conn, activity=True)
            # Both names come from this test's fixed parameter list, not input.
            await conn.execute(f"ALTER TABLE reporting_issue_waiver_bindings DROP COLUMN {column}")
            with pytest.raises(ReportingNotificationError, match="status_schema_unready"):
                await validate_status_schema(conn, activity=True)


async def test_failed_source_transaction_cannot_leave_a_waiver_binding(status_harness):
    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    original = replace(
        statement(obligation), consumer_status="unreadable", failure_code="access_denied"
    )
    await h.ledger.record_consumer_status(original)
    await h.status.baseline(account_id="acct_a")
    key = mismatch_key(original)
    before = await h.ledger.get_issue(account_id="acct_a", issue_key=key)

    class AbortWaiverError(Exception):
        pass

    with pytest.raises(AbortWaiverError):
        async with h.ledger.transaction():
            waived = await h.ledger.set_issue_state(
                issue_key=key, account_id="acct_a", state="waived", at=h.clock()
            )
            assert waived.waived_reporting_status_id == original.reporting_status_id
            raise AbortWaiverError()
    assert await h.ledger.get_issue(account_id="acct_a", issue_key=key) == before
    await h.drain()
    assert not await h.events(consumer="buyer")
    current = await ReportingStatusHandler(h.ledger, consumer_status_enabled=True).handle(
        {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    assert current["health"] == "action_required"
    committed = await h.ledger.set_issue_state(
        issue_key=key, account_id="acct_a", state="waived", at=h.clock()
    )
    assert committed.waived_reporting_status_id == original.reporting_status_id
    await h.drain()
    assert (await h.events(consumer="buyer"))[-1].cause.health == "complete"


async def test_legacy_unbound_waiver_preserves_audit_but_cannot_suppress_health(status_harness):
    from adcp.reporting.ledger import InMemoryReportingLedgerStore

    h = status_harness
    obligation, _, _ = await h.seed(readable=True)
    original = replace(
        statement(obligation), consumer_status="unreadable", failure_code="access_denied"
    )
    await h.ledger.record_consumer_status(original)
    await h.status.baseline(account_id="acct_a")
    key = mismatch_key(original)
    occurrence = await h.ledger.get_issue(account_id="acct_a", issue_key=key)
    legacy = replace(occurrence, issue_state="waived", retired_at=h.clock())
    # Historical fixture: old writers retained the lifecycle without an exact
    # statement/conflict binding. Do not fabricate bilateral consent on upgrade.
    if isinstance(h.ledger, InMemoryReportingLedgerStore):
        h.ledger._issues[("acct_a", key)] = legacy
    else:
        async with h.ledger._pool.connection() as conn:
            await conn.execute(
                "UPDATE reporting_issue_lifecycle SET issue_state='waived', retired_at=%s"
                " WHERE account_id=%s AND issue_key=%s AND generation=%s",
                (h.clock(), "acct_a", key, occurrence.generation),
            )
    current = await ReportingStatusHandler(h.ledger, consumer_status_enabled=True).handle(
        {"view": "summary"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    assert current["health"] == "action_required"
    assert current["issues"][0]["issue_id"] != legacy.issue_id
    assert await h.ledger.get_issue(account_id="acct_a", issue_key=key) == legacy
