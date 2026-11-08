"""The issue-lifecycle event outbox a ledger store keeps for the issue observer.

Every write that moves an issue occurrence's lifecycle -- opening it,
acknowledging or waiving it, retiring it as resolved -- records one event in
the same transaction, and an escalation sweep records ``escalated`` when an
open or acknowledged occurrence passes ``opened_at`` plus the seller's
``consumer_mismatch_escalation``. A worker claims pending events under a lease
and settles each one after handing it to the observer (see
:class:`adcp.reporting.issue_events.ReportingIssueEventDispatcher`).

A store opts in by implementing :class:`ReportingIssueEventStore` and setting
``issue_events_supported = True``. :class:`InMemoryReportingLedgerStore
<adcp.reporting.ledger.store.InMemoryReportingLedgerStore>` does;
``PgReportingLedgerStore`` does from adcp-reporting-storage 0.2.0b4, once
migration ``0008`` is applied (``python -m adcp migrate``).

Each transition is recorded exactly once. The lifecycle is forward-only, so an
occurrence makes each transition at most once, and ``(issue_id, kind)`` is
unique; :func:`issue_event_id` derives the event's id from that pair, so every
redelivery of one transition carries the same id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from adcp.reporting.canonical_json import canonical_json_utf8_v1

__all__ = [
    "ISSUE_EVENT_KINDS",
    "ReportingIssueEventKind",
    "ReportingIssueEventLease",
    "ReportingIssueEventStore",
    "issue_event_id",
    "supports_issue_events",
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
KIND_FOR_STATE: dict[str, ReportingIssueEventKind] = {
    "open": "opened",
    "acknowledged": "acknowledged",
    "waived": "waived",
    "resolved": "resolved",
}


def issue_event_id(issue_id: str, kind: str) -> str:
    """The stable id of one transition of one issue occurrence.

    Every store derives it the same way (adcp-reporting-storage included), so
    an observer can deduplicate on it whichever store recorded the event.
    """
    payload = canonical_json_utf8_v1(["reporting-issue-event-v1", issue_id, kind])
    return "rpie_" + hashlib.sha256(payload).hexdigest()[:40]


class ReportingIssueEventLease(Protocol):
    """One claimed issue event, as a store hands it to a worker."""

    @property
    def event_id(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def consumer_id(self) -> str | None: ...

    @property
    def issue_key(self) -> str: ...

    @property
    def issue_id(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def kind(self) -> ReportingIssueEventKind: ...

    @property
    def occurred_at(self) -> datetime:
        """When the transition took effect."""
        ...

    @property
    def attempts(self) -> int:
        """Delivery attempts, this one included."""
        ...


@runtime_checkable
class ReportingIssueEventStore(Protocol):
    """A ledger store that records issue events and serves them under a lease.

    ``issue_events_supported`` is the opt-in: a store that merely has methods
    of these names is not taken to record events beside its lifecycle writes.
    """

    issue_events_supported: bool

    async def issue_events_ready(self) -> bool:
        """Whether the store can record events now (its schema is installed)."""
        ...

    async def sweep_issue_escalations(self, *, after: timedelta, limit: int = 100) -> int:
        """Record ``escalated`` for occurrences open or acknowledged past ``opened_at + after``.

        Settles each swept account first, as a status read would, so a
        condition that cleared resolves instead of escalating. Returns the
        events recorded.
        """
        ...

    async def claim_issue_events(
        self, *, lease_seconds: float = 60.0, limit: int = 20
    ) -> tuple[ReportingIssueEventLease, ...]:
        """Lease due events, at most one per occurrence, each in recording order."""
        ...

    async def complete_issue_event(self, lease: ReportingIssueEventLease) -> bool:
        """Mark a leased event delivered; ``False`` when the lease was lost."""
        ...

    async def release_issue_event(
        self, lease: ReportingIssueEventLease, *, retry_after: timedelta
    ) -> bool:
        """Give back a failed delivery, due again ``retry_after`` from now."""
        ...

    async def prune_issue_events(self, *, older_than: timedelta, limit: int = 1000) -> int:
        """Delete up to ``limit`` events delivered more than ``older_than`` ago."""
        ...


def supports_issue_events(store: object) -> bool:
    """Whether ``store`` opted in to recording issue events."""
    return getattr(store, "issue_events_supported", False) is True and isinstance(
        store, ReportingIssueEventStore
    )


@dataclass(frozen=True)
class MemoryIssueEventLease:
    """The reference store's claimed event."""

    event_id: str
    account_id: str
    consumer_id: str | None
    issue_key: str
    issue_id: str
    generation: int
    kind: ReportingIssueEventKind
    occurred_at: datetime
    recorded_at: datetime
    attempts: int
    lease_token: str = field(repr=False)
    lease_expires_at: datetime


@dataclass
class MemoryIssueEvent:
    """One recorded event in the reference store."""

    seq: int
    event_id: str
    account_id: str
    consumer_id: str | None
    issue_key: str
    issue_id: str
    generation: int
    kind: ReportingIssueEventKind
    occurred_at: datetime
    recorded_at: datetime
    due_at: datetime
    attempts: int = 0
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    delivered_at: datetime | None = None
