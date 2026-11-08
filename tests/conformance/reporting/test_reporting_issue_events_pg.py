"""The issue-lifecycle event outbox against real PostgreSQL (adcp-client-python#1258).

Migration ``0008`` installs ``reporting_issue_events``; every write that moves
a ``reporting_issue_lifecycle`` row records its transition there in the same
transaction, the escalation sweep records ``escalated`` exactly once, and a
worker claims, completes or releases events under a lease. The issue observer
in ``adcp`` reads this outbox; these tests pin the storage half.

Requires ``ADCP_PG_TEST_URL``; skips otherwise.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from adcp.reporting.ledger.status_projection import mismatch_key

from ._generation_support import (
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)
from ._reliable_support import ManualClock
from .test_reporting_notification_outbox import statement

ACCOUNT = "acct_a"
HOUR = timedelta(hours=1)


async def _store(pool: Any, clock: ManualClock, *, migrated: bool = True) -> Any:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.migrations import migrate

    store = PgReportingLedgerStore(pool=pool, clock=clock)
    await store.create_schema()
    if migrated:
        await migrate(pool)
    return store


async def _events(pool: Any) -> list[dict[str, Any]]:
    async with pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_id, issue_id, kind, occurred_at, attempts, delivered_at, generation"
            " FROM reporting_issue_events ORDER BY seq"
        )
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in await cursor.fetchall()]


async def _open(store: Any, clock: ManualClock, key: str = "rpik_condition") -> Any:
    return await store.ensure_issue_opened(
        issue_key=key, account_id=ACCOUNT, consumer_id="buyer", observed_at=clock()
    )


async def _catalog(pool: Any) -> set[tuple[str, str]]:
    async with pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT c.relkind::text, c.relname FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = current_schema()"
                " UNION ALL"
                " SELECT 'column', table_name || '.' || column_name || ':' || data_type"
                " FROM information_schema.columns WHERE table_schema = current_schema()"
                " UNION ALL"
                " SELECT 'constraint', conrelid::regclass::text || '.' || conname"
                " FROM pg_constraint WHERE connamespace = current_schema()::regnamespace"
            )
        ).fetchall()
    return {(kind, name) for kind, name in rows}


async def test_migration_0008_adds_only_the_event_table() -> None:
    """Purely additive: dropping its objects leaves exactly the 0001-0007 catalog."""
    from adcp_reporting_storage.migrations import migrate

    async with isolated_reporting_pool(autocommit=True) as pool:
        await migrate(pool)
        with_events = await _catalog(pool)
        async with pool.connection() as connection:
            await connection.execute("DROP TABLE reporting_issue_events")
            await connection.execute("DELETE FROM adcp_schema_migrations WHERE version = '0008'")
        without = await _catalog(pool)
        added = with_events - without
        assert without <= with_events  # nothing existing was changed or removed
        # Only the table, its columns, constraints, indexes and sequence.
        assert all(name.startswith("reporting_issue_events") for _, name in added), added
        assert ("r", "reporting_issue_events") in added
        # Re-applying records it again and restores the same catalog.
        assert [item.version for item in await migrate(pool)] == ["0008"]
        assert await _catalog(pool) == with_events


async def test_an_unmigrated_ledger_records_nothing_and_says_so() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock, migrated=False)
        assert store.issue_events_supported is True
        assert not await store.issue_events_ready()
        issue = await _open(store, clock)  # the lifecycle write itself is unchanged
        await store.retire_issue(issue_key=issue.issue_key, account_id=ACCOUNT, at=clock())
        assert await store.claim_issue_events() == ()
        assert await store.sweep_issue_escalations(after=timedelta(0)) == 0
        assert await store.prune_issue_events(older_than=timedelta(0)) == 0
        from adcp_reporting_storage.migrations import migrate

        await migrate(pool)
        assert await store.issue_events_ready()
        assert await _events(pool) == []  # history is not backfilled


async def test_each_lifecycle_write_records_its_transition_once() -> None:
    from adcp_reporting_storage.ledger.issue_events import issue_event_id

    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        opened_at = clock()
        first = await _open(store, clock)
        clock.advance(timedelta(minutes=1))
        assert (await _open(store, clock)).issue_id == first.issue_id  # idempotent: no event
        acknowledged_at = clock.advance(timedelta(minutes=1))
        for _ in range(2):  # a re-acknowledge changes nothing and records nothing
            await store.set_issue_state(
                issue_key=first.issue_key,
                account_id=ACCOUNT,
                state="acknowledged",
                at=acknowledged_at,
            )
        await store.set_issue_state(  # external_ref alone is not a transition
            issue_key=first.issue_key,
            account_id=ACCOUNT,
            state="acknowledged",
            at=clock.advance(),
            external_ref="TICKET-1",
        )
        resolved_at = clock.advance(timedelta(minutes=1))
        await store.retire_issue(issue_key=first.issue_key, account_id=ACCOUNT, at=resolved_at)
        assert (
            await store.retire_issue(  # convergent: nothing live, nothing recorded
                issue_key=first.issue_key, account_id=ACCOUNT, at=clock.advance()
            )
            is None
        )
        reopened_at = clock.advance(timedelta(minutes=1))
        second = await _open(store, clock)
        assert second.issue_id != first.issue_id and second.generation == 2
        waived_at = clock.advance(timedelta(minutes=1))
        await store.set_issue_state(
            issue_key=second.issue_key, account_id=ACCOUNT, state="waived", at=waived_at
        )
        events = await _events(pool)
    assert [(e["issue_id"], e["kind"], e["occurred_at"]) for e in events] == [
        (first.issue_id, "opened", opened_at),
        (first.issue_id, "acknowledged", acknowledged_at),
        (first.issue_id, "resolved", resolved_at),
        (second.issue_id, "opened", reopened_at),
        (second.issue_id, "waived", waived_at),
    ]
    assert [e["event_id"] for e in events] == [
        issue_event_id(e["issue_id"], e["kind"]) for e in events
    ]
    assert all(e["attempts"] == 0 and e["delivered_at"] is None for e in events)


async def test_a_rolled_back_lifecycle_write_records_no_event() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        with pytest.raises(RuntimeError, match="abort"):
            async with store.transaction():
                await _open(store, clock)
                raise RuntimeError("abort")
        assert await store.get_issue(issue_key="rpik_condition", account_id=ACCOUNT) is None
        assert await _events(pool) == []


async def _seed_mismatch(store: Any, clock: ManualClock) -> tuple[Any, Any, Any]:
    """A readable revision the buyer reports unreadable: a seller-side mismatch."""
    config = configuration()
    await store.put_configuration(config)
    obligation = await store.commit_obligation(obligation_for(config))
    revision, rows = revision_for(obligation)
    await store.commit_revision(revision, rows)
    unreadable = replace(
        statement(obligation),
        consumer_status="unreadable",
        failure_code="transport_failed",
        reporting_revision_id=revision.reporting_revision_id,
        status_as_of=clock(),
        recorded_at=clock(),
    )
    await store.record_consumer_status_with_lifecycle(unreadable)
    return obligation, revision, unreadable


async def test_ingest_and_agreement_record_opened_and_resolved_in_the_status_transaction() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        _, revision, unreadable = await _seed_mismatch(store, clock)
        issue = await store.get_issue(account_id=ACCOUNT, issue_key=mismatch_key(unreadable))
        assert issue is not None and issue.opened_at == unreadable.recorded_at
        clock.advance(timedelta(minutes=5))
        agreeing = replace(
            unreadable,
            reporting_status_id="consumer-status-buyer-agrees",
            consumer_status="received",
            failure_code=None,
            observed_revision_content_sha256=revision.revision_content_sha256,
            supersedes_reporting_status_id=unreadable.reporting_status_id,
            status_as_of=clock(),
            recorded_at=clock(),
        )
        await store.record_consumer_status_with_lifecycle(agreeing)
        events = await _events(pool)
    assert [(e["issue_id"], e["kind"]) for e in events] == [
        (issue.issue_id, "opened"),
        (issue.issue_id, "resolved"),
    ]
    assert events[0]["occurred_at"] == unreadable.recorded_at
    assert events[1]["occurred_at"] == clock()


async def test_escalation_is_recorded_once_at_the_boundary_across_sweepers() -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        opened = await _open(store, clock, "rpik_open")
        acknowledged = await _open(store, clock, "rpik_acknowledged")
        await store.set_issue_state(
            issue_key=acknowledged.issue_key, account_id=ACCOUNT, state="acknowledged", at=clock()
        )
        resolved = await _open(store, clock, "rpik_resolved")
        await store.retire_issue(issue_key=resolved.issue_key, account_id=ACCOUNT, at=clock())
        waived = await _open(store, clock, "rpik_waived")
        await store.set_issue_state(
            issue_key=waived.issue_key, account_id=ACCOUNT, state="waived", at=clock()
        )
        boundary = opened.opened_at + HOUR
        clock.now = boundary - timedelta(seconds=1)
        assert await store.sweep_issue_escalations(after=HOUR) == 0
        clock.now = boundary
        replicas = [PgReportingLedgerStore(pool=pool, clock=clock) for _ in range(4)]
        recorded = await asyncio.gather(
            *(replica.sweep_issue_escalations(after=HOUR) for replica in replicas)
        )
        assert sum(recorded) == 2  # open and acknowledged; never resolved or waived
        assert await store.sweep_issue_escalations(after=HOUR) == 0
        escalated = [e for e in await _events(pool) if e["kind"] == "escalated"]
    assert {e["issue_id"] for e in escalated} == {opened.issue_id, acknowledged.issue_id}
    assert {e["occurred_at"] for e in escalated} == {boundary}


async def test_a_condition_that_cleared_unobserved_resolves_instead_of_escalating() -> None:
    """The sweep settles the account first, as a status read would."""
    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        _, revision, unreadable = await _seed_mismatch(store, clock)
        issue = await store.get_issue(account_id=ACCOUNT, issue_key=mismatch_key(unreadable))
        # The seller now agrees the revision is unreadable; nothing has re-read since.
        await store.set_revision_readable(
            account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id, readable=False
        )
        assert await store.get_issue(account_id=ACCOUNT, issue_key=issue.issue_key) is not None
        clock.now = issue.opened_at + HOUR
        assert await store.sweep_issue_escalations(after=HOUR) == 0
        assert await store.get_issue(account_id=ACCOUNT, issue_key=issue.issue_key) is None
        assert [e["kind"] for e in await _events(pool)] == ["opened", "resolved"]


async def test_claims_deliver_each_occurrence_in_order_under_exclusive_leases() -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        a = await _open(store, clock, "rpik_a")
        await store.set_issue_state(
            issue_key=a.issue_key, account_id=ACCOUNT, state="acknowledged", at=clock.advance()
        )
        b = await _open(store, clock, "rpik_b")
        # One event per occurrence, oldest first; the second worker gets nothing.
        first = await store.claim_issue_events(lease_seconds=30, limit=10)
        assert [(lease.issue_id, lease.kind, lease.attempts) for lease in first] == [
            (a.issue_id, "opened", 1),
            (b.issue_id, "opened", 1),
        ]
        other = PgReportingLedgerStore(pool=pool, clock=clock)
        assert await other.claim_issue_events(lease_seconds=30, limit=10) == ()
        # A's next event becomes claimable once its first is delivered.
        assert await store.complete_issue_event(first[0])
        assert not await store.complete_issue_event(first[0])  # already settled
        (acknowledged,) = await other.claim_issue_events(lease_seconds=30, limit=10)
        assert (acknowledged.issue_id, acknowledged.kind) == (a.issue_id, "acknowledged")
        # A failed delivery is due again retry_after from now, and not before.
        retry_at = clock() + timedelta(seconds=10)  # before B's lease expires
        assert await other.release_issue_event(acknowledged, retry_after=timedelta(seconds=10))
        assert await store.claim_issue_events(lease_seconds=30) == ()
        clock.now = retry_at
        (again,) = await store.claim_issue_events(lease_seconds=30)
        assert (again.event_id, again.attempts) == (acknowledged.event_id, 2)
        # An expired lease is claimed again; the stale holder can no longer settle it.
        clock.now = retry_at + timedelta(seconds=31)
        reclaimed = await other.claim_issue_events(lease_seconds=30)
        assert {lease.event_id for lease in reclaimed} == {first[1].event_id, again.event_id}
        assert not await store.complete_issue_event(first[1])
        assert not await store.release_issue_event(again, retry_after=timedelta(0))
        for lease in reclaimed:
            assert await other.complete_issue_event(lease)
        assert await store.claim_issue_events() == ()
        events = await _events(pool)
    assert all(e["delivered_at"] is not None for e in events)


async def test_concurrent_claims_never_lease_one_event_twice() -> None:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        for index in range(12):
            await _open(store, clock, f"rpik_{index:02d}")
        workers = [PgReportingLedgerStore(pool=pool, clock=clock) for _ in range(4)]
        claimed = await asyncio.gather(
            *(worker.claim_issue_events(lease_seconds=30, limit=5) for worker in workers)
        )
    ids = [lease.event_id for batch in claimed for lease in batch]
    assert len(ids) == len(set(ids)) == 12


async def test_prune_deletes_only_events_delivered_before_the_horizon() -> None:
    async with isolated_reporting_pool(autocommit=True) as pool:
        clock = ManualClock()
        store = await _store(pool, clock)
        for key in ("rpik_old", "rpik_new", "rpik_pending"):
            await _open(store, clock, key)
        old, new, pending = await store.claim_issue_events(limit=3)
        assert await store.complete_issue_event(old)
        clock.advance(timedelta(days=1))
        assert await store.complete_issue_event(new)
        clock.advance(timedelta(hours=1))
        assert await store.prune_issue_events(older_than=timedelta(days=1)) == 1
        remaining = {e["issue_id"] for e in await _events(pool)}
    assert remaining == {new.issue_id, pending.issue_id}
