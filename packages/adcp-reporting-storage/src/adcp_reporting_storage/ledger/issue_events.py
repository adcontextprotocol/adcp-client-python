"""The issue-lifecycle event outbox over PostgreSQL (adcp-client-python#1258).

Every write that moves a ``reporting_issue_lifecycle`` row records the
transition in ``reporting_issue_events`` inside the same transaction, so an
event exists exactly when its transition committed. The escalation sweep adds
``escalated`` when an open or acknowledged occurrence passes ``opened_at`` plus
the seller's ``consumer_mismatch_escalation_seconds``. A worker claims pending
events under a lease, hands each to the adopter's issue observer, and stamps it
delivered.

Delivery is at least once. ``UNIQUE (issue_id, kind)`` makes the *record*
exactly once across replicas, and ``event_id`` is derived from that pair, so a
redelivery (a worker that crashed after the observer returned, or a lease that
expired mid-call) carries the same ``event_id`` and an observer deduplicates on
it.

The table is installed by migration ``0008`` (``python -m adcp migrate``).
Until it exists, lifecycle writes record nothing and
:meth:`~adcp_reporting_storage.ledger.pg.PgReportingLedgerStore.issue_events_ready`
is ``False``; nothing else changes, so a store of this release runs on a
ledger that has not been migrated yet.

Only existing ``adcp`` APIs are used here, so this module works with every
``adcp`` this distribution supports.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger._record_rows import _ISSUE_COLUMNS, _issue_from_row
from adcp.reporting.ledger.models import ReportingIssueLifecycle

if TYPE_CHECKING:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

__all__ = [
    "ISSUE_EVENT_KINDS",
    "ReportingIssueEventKind",
    "ReportingIssueEventLease",
    "issue_event_id",
]

ReportingIssueEventKind = Literal["opened", "escalated", "acknowledged", "waived", "resolved"]

#: Every kind, in lifecycle order.
ISSUE_EVENT_KINDS: tuple[ReportingIssueEventKind, ...] = (
    "opened",
    "escalated",
    "acknowledged",
    "waived",
    "resolved",
)

#: The event that entering each ``issue_state`` records.
_KIND_FOR_STATE: dict[str, ReportingIssueEventKind] = {
    "open": "opened",
    "acknowledged": "acknowledged",
    "waived": "waived",
    "resolved": "resolved",
}

#: Claimed columns in :class:`ReportingIssueEventLease` order, then the recording order.
_CLAIMED_COLUMNS = (
    "e.event_id, e.account_id, e.consumer_id, e.issue_key, e.issue_id, e.generation, e.kind,"
    " e.occurred_at, e.recorded_at, e.attempts, e.lease_token, e.lease_expires_at, e.seq"
)


def issue_event_id(issue_id: str, kind: str) -> str:
    """The stable id of one transition of one occurrence.

    The lifecycle is forward-only, so ``(issue_id, kind)`` happens at most
    once; every redelivery of that transition carries this id.
    """
    payload = canonical_json_utf8_v1(["reporting-issue-event-v1", issue_id, kind])
    return "rpie_" + hashlib.sha256(payload).hexdigest()[:40]


@dataclass(frozen=True)
class ReportingIssueEventLease:
    """One claimed issue event: what happened, and the lease to settle it with."""

    event_id: str
    account_id: str
    consumer_id: str | None
    issue_key: str
    issue_id: str
    generation: int
    kind: ReportingIssueEventKind
    #: When the transition took effect.
    occurred_at: datetime
    #: When the transition was recorded.
    recorded_at: datetime
    #: Delivery attempts, this one included.
    attempts: int
    lease_token: str = field(repr=False)
    lease_expires_at: datetime


def _lease_from_row(row: Any) -> ReportingIssueEventLease:
    return ReportingIssueEventLease(
        event_id=row[0],
        account_id=row[1],
        consumer_id=row[2],
        issue_key=row[3],
        issue_id=row[4],
        generation=int(row[5]),
        kind=row[6],
        occurred_at=row[7],
        recorded_at=row[8],
        attempts=int(row[9]),
        lease_token=row[10],
        lease_expires_at=row[11],
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


async def issue_event_storage_on(connection: Any) -> bool:
    """Whether migration ``0008`` installed the outbox in this schema."""
    row = await (
        await connection.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.reporting_issue_events')"
            " IS NOT NULL"
        )
    ).fetchone()
    return bool(row and row[0])


async def record_issue_transition_on(
    connection: Any,
    *,
    before_state: str | None,
    after: ReportingIssueLifecycle,
    clock: Callable[[], datetime] | None,
    occurred_at: datetime | None = None,
) -> None:
    """Record the events one lifecycle write produced, in the writer's transaction.

    Runs inside the caller's account transaction, so the events commit or roll
    back with the write. ``before_state`` is the occurrence's state before the
    write, or ``None`` when the write created it. A write that leaves the
    state unchanged (an idempotent re-acknowledge, an ``external_ref``
    attachment) records nothing. ``occurred_at`` defaults to the occurrence's
    ``retired_at`` for retiring states and to the recording instant otherwise.
    """
    from adcp_reporting_storage.outbox.pg import database_now

    if not await issue_event_storage_on(connection):
        return
    now = await database_now(connection, clock)
    kinds: list[tuple[ReportingIssueEventKind, datetime]] = []
    if before_state is None:
        kinds.append(("opened", after.opened_at))
        before_state = "open"
    if after.issue_state != before_state:
        at = occurred_at or after.retired_at or now
        kinds.append((_KIND_FOR_STATE[after.issue_state], at))
    for kind, at in kinds:
        await _insert_on(connection, after, kind, occurred_at=at, now=now)


async def _insert_on(
    connection: Any,
    issue: ReportingIssueLifecycle,
    kind: ReportingIssueEventKind,
    *,
    occurred_at: datetime,
    now: datetime,
) -> bool:
    row = await (
        await connection.execute(
            "INSERT INTO reporting_issue_events"
            " (event_id, account_id, issue_key, generation, issue_id, consumer_id, kind,"
            "  occurred_at, recorded_at, due_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (issue_id, kind) DO NOTHING RETURNING 1",
            (
                issue_event_id(issue.issue_id, kind),
                issue.account_id,
                issue.issue_key,
                issue.generation,
                issue.issue_id,
                issue.consumer_id,
                kind,
                _utc(occurred_at),
                _utc(now),
                _utc(now),
            ),
        )
    ).fetchone()
    return row is not None


async def sweep_escalations(store: PgReportingLedgerStore, *, after: timedelta, limit: int) -> int:
    """Record ``escalated`` for occurrences unresolved past ``opened_at + after``.

    Each candidate account is swept in its own transaction under its advisory
    lock: the account's lifecycle is settled first, as a status read settles
    it, so a condition that cleared is retired (recording ``resolved``)
    instead of escalating. ``ON CONFLICT DO NOTHING`` keeps the record exactly
    once when replicas sweep together. Returns the events recorded.
    """
    from adcp_reporting_storage.ledger.status_snapshot import settle_snapshot_on
    from adcp_reporting_storage.outbox.pg import database_now

    if after < timedelta(0):
        raise ValueError("after must be nonnegative")
    if limit < 1:
        raise ValueError("limit must be positive")
    async with store._connection() as connection, connection.transaction():
        if not await issue_event_storage_on(connection):
            return 0
        now = await database_now(connection, store._clock)
        accounts = await (
            await connection.execute(
                "SELECT DISTINCT l.account_id FROM reporting_issue_lifecycle l"
                " WHERE l.issue_state IN ('open', 'acknowledged') AND l.opened_at <= %s"
                "   AND NOT EXISTS (SELECT 1 FROM reporting_issue_events e"
                "                   WHERE e.issue_id = l.issue_id AND e.kind = 'escalated')"
                " ORDER BY l.account_id LIMIT %s",
                (now - after, limit),
            )
        ).fetchall()
    recorded = 0
    for (account_id,) in accounts:
        async with store._connection() as connection, connection.transaction():
            await store._lock_account(connection, account_id)
            now = await database_now(connection, store._clock)
            await settle_snapshot_on(store, connection, account_id=account_id, as_of=now)
            rows = await (
                await connection.execute(
                    f"SELECT {_ISSUE_COLUMNS} FROM reporting_issue_lifecycle l"  # noqa: S608  # nosec B608
                    " WHERE l.account_id = %s AND l.issue_state IN ('open', 'acknowledged')"
                    "   AND l.opened_at <= %s"
                    " ORDER BY l.opened_at, l.issue_id",
                    (account_id, now - after),
                )
            ).fetchall()
            for row in rows:
                issue = _issue_from_row(row)
                if await _insert_on(
                    connection,
                    issue,
                    "escalated",
                    occurred_at=_utc(issue.opened_at) + after,
                    now=now,
                ):
                    recorded += 1
    return recorded


async def claim(
    store: PgReportingLedgerStore, *, lease_seconds: float, limit: int
) -> tuple[ReportingIssueEventLease, ...]:
    """Lease due events, at most one per occurrence, oldest first.

    An occurrence's events are delivered in the order they were recorded: an
    event is claimable only once every earlier event of its occurrence has
    been delivered. ``SKIP LOCKED`` lets workers on several replicas claim
    disjoint events; an expired lease is claimable again.
    """
    from adcp_reporting_storage.outbox.pg import database_now

    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if limit < 1:
        raise ValueError("limit must be positive")
    async with store._connection() as connection, connection.transaction():
        if not await issue_event_storage_on(connection):
            return ()
        now = await database_now(connection, store._clock)
        token = uuid4().hex
        rows = await (
            await connection.execute(
                "WITH due AS ("
                "  SELECT e.event_id FROM reporting_issue_events e"
                "  WHERE e.delivered_at IS NULL AND e.due_at <= %s"
                "    AND (e.lease_expires_at IS NULL OR e.lease_expires_at <= %s)"
                "    AND NOT EXISTS (SELECT 1 FROM reporting_issue_events p"
                "                    WHERE p.issue_id = e.issue_id AND p.delivered_at IS NULL"
                "                      AND p.seq < e.seq)"
                "  ORDER BY e.due_at, e.seq LIMIT %s"
                "  FOR UPDATE SKIP LOCKED)"
                " UPDATE reporting_issue_events e"
                " SET lease_token = %s, lease_expires_at = %s, attempts = e.attempts + 1"
                " FROM due WHERE e.event_id = due.event_id"
                f" RETURNING {_CLAIMED_COLUMNS}",  # noqa: S608  # nosec B608
                (now, now, limit, token, now + timedelta(seconds=lease_seconds)),
            )
        ).fetchall()
    return tuple(_lease_from_row(row) for row in sorted(rows, key=lambda row: row[-1]))


async def complete(store: PgReportingLedgerStore, lease: ReportingIssueEventLease) -> bool:
    """Stamp a delivered event. ``False`` when the lease was lost to another worker."""
    from adcp_reporting_storage.outbox.pg import database_now

    async with store._connection() as connection, connection.transaction():
        now = await database_now(connection, store._clock)
        row = await (
            await connection.execute(
                "UPDATE reporting_issue_events"
                " SET delivered_at = %s, lease_token = NULL, lease_expires_at = NULL"
                " WHERE event_id = %s AND lease_token = %s AND delivered_at IS NULL"
                " RETURNING 1",
                (now, lease.event_id, lease.lease_token),
            )
        ).fetchone()
    return row is not None


async def release(
    store: PgReportingLedgerStore, lease: ReportingIssueEventLease, *, retry_after: timedelta
) -> bool:
    """Give a failed delivery back, due again ``retry_after`` from now (the store's clock)."""
    from adcp_reporting_storage.outbox.pg import database_now

    async with store._connection() as connection, connection.transaction():
        now = await database_now(connection, store._clock)
        row = await (
            await connection.execute(
                "UPDATE reporting_issue_events"
                " SET due_at = %s, lease_token = NULL, lease_expires_at = NULL"
                " WHERE event_id = %s AND lease_token = %s AND delivered_at IS NULL"
                " RETURNING 1",
                (now + max(retry_after, timedelta(0)), lease.event_id, lease.lease_token),
            )
        ).fetchone()
    return row is not None


async def prune(store: PgReportingLedgerStore, *, older_than: timedelta, limit: int) -> int:
    """Delete up to ``limit`` events delivered more than ``older_than`` ago."""
    from adcp_reporting_storage.outbox.pg import database_now

    if limit < 1:
        raise ValueError("limit must be positive")
    async with store._connection() as connection, connection.transaction():
        if not await issue_event_storage_on(connection):
            return 0
        delivered_before = await database_now(connection, store._clock) - older_than
        rows = await (
            await connection.execute(
                "DELETE FROM reporting_issue_events WHERE event_id IN ("
                "  SELECT event_id FROM reporting_issue_events"
                "  WHERE delivered_at IS NOT NULL AND delivered_at < %s"
                "  ORDER BY delivered_at LIMIT %s)"
                " RETURNING 1",
                (delivered_before, limit),
            )
        ).fetchall()
    return len(rows)
