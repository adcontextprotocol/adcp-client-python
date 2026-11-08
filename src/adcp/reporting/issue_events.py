"""Tell the seller when a reporting issue opens, escalates or resolves (#1258).

A ``CONSUMER_STATUS_MISMATCH`` is caller-scoped: only the buyer whose
statement caused it sees it in ``get_reporting_status``. A mismatch the seller
must act on is invisible to the seller's operators unless the buyer calls
them. The issue observer closes that gap::

    from adcp.reporting import ReliableReportingService, ReportingIssueEvent

    async def on_issue(event: ReportingIssueEvent) -> None:
        if event.issue is not None and event.issue.responsible_party != "buyer":
            await page_operations(event)  # dedupe on event.event_id

    service = ReliableReportingService(..., issue_observer=on_issue)

When it is called
-----------------

Once per lifecycle transition of an issue occurrence, after the transaction
that made the transition commits:

========================= ===========================================================
``opened``                An occurrence opened: a ``sync_reporting_status``
                          statement landed (or a status read first observed it).
``escalated``             An open or acknowledged occurrence passed ``opened_at``
                          plus the seller's ``consumer_mismatch_escalation``.
``acknowledged``          An operator acknowledged it (``store.set_issue_state``).
``waived``                Buyer and seller agreed off-protocol to disregard it.
``resolved``              The condition cleared: the buyer filed an agreeing
                          statement, or the seller's record no longer conflicts.
========================= ===========================================================

The transition and its event commit in one transaction, and the worker turn
(``run_worker``, or the ``worker_interval`` loop) delivers committed events to
the observer. ``escalated`` is recorded by that turn too, when the boundary
passes, so an unattended mismatch escalates whether or not anyone reads its
status. Each occurrence's events arrive in the order they were recorded.

Delivery is at least once
-------------------------

Each transition is recorded exactly once, even when several replicas make it
together. Delivery is **at least once**: a worker that stops after the
observer returned, but before the event was marked delivered, delivers it
again. Every delivery of one transition carries the same ``event_id``, derived
from the occurrence and the kind, so **deduplicate on** ``event_id``.

The observer cannot fail the protocol operation: it runs on the worker turn,
not in the buyer's call. When it raises or times out, the error goes to the
service's ``worker_error_handler`` (or the log), and the event is retried on a
later turn with exponential backoff, without limit. A replayed or retried
``sync_reporting_status`` that changes nothing records nothing.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from adcp.reporting.ledger.issue_events import (
    ReportingIssueEventKind,
    ReportingIssueEventLease,
    ReportingIssueEventStore,
    issue_event_id,
    supports_issue_events,
)
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingDeliveryEscalation,
    ReportingIssue,
    ReportingIssueLifecycle,
    ReportingIssueStateValue,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot

__all__ = [
    "ReportingIssueDelivery",
    "ReportingIssueEvent",
    "ReportingIssueEventDispatcher",
    "ReportingIssueEventKind",
    "ReportingIssueObserver",
    "issue_event_id",
    "render_issue_event",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportingIssueEvent:
    """One lifecycle transition of one reporting-issue occurrence.

    Delivered at least once: deduplicate on ``event_id``, which is the same on
    every delivery of this transition.
    """

    #: Stable across redeliveries of this transition; deduplicate on it.
    event_id: str
    kind: ReportingIssueEventKind
    #: When the transition took effect: the issue's ``opened_at``, the
    #: escalation boundary, the operator's instant, or when it was retired.
    occurred_at: datetime
    account_id: str
    #: The buyer whose statement caused a caller-scoped issue.
    consumer_id: str | None
    issue_id: str
    #: The issue as ``get_reporting_status`` rendered it for ``consumer_id`` at
    #: ``occurred_at``: ``responsible_party``, ``recommended_action``,
    #: ``severity``, ``opened_at``, the causing ``reporting_status_id``. Its
    #: ``issue_state`` is this event's. ``None`` for ``resolved`` (the condition
    #: no longer projects) or when the evidence is no longer retained.
    issue: ReportingIssue | None
    #: This delivery's attempt number, from 1.
    attempt: int = 1

    @property
    def reporting_status_id(self) -> str | None:
        """The consumer statement that caused the issue, when it is rendered."""
        return self.issue.reporting_status_id if self.issue is not None else None


#: The adopter's callback: sync or async (an awaitable result is awaited), its
#: return value otherwise ignored.
ReportingIssueObserver = Callable[[ReportingIssueEvent], object]

#: The issue state each kind renders the occurrence in.
_RENDER_STATE: dict[str, ReportingIssueStateValue] = {
    "opened": "open",
    "acknowledged": "acknowledged",
    "waived": "waived",
}


def render_issue_event(
    lease: ReportingIssueEventLease,
    snapshot: ReportingStatusSnapshot | None,
    *,
    escalation: ReportingDeliveryEscalation | None = None,
) -> ReportingIssueEvent:
    """Build the observer's event, rendering the issue as of ``occurred_at``.

    The issue is projected exactly as ``get_reporting_status`` projects it for
    ``consumer_id``, over the evidence that existed at ``occurred_at``:
    statements recorded by then (with the chain leaf as of then), obligations
    and revisions created by then, and the occurrence in this event's state.
    Rendering is best effort: anything it cannot reconstruct yields
    ``issue=None`` rather than a wrong issue.
    """
    issue: ReportingIssue | None = None
    if lease.kind != "resolved" and snapshot is not None and lease.consumer_id is not None:
        try:
            issue = _render(lease, snapshot, escalation)
        except Exception:  # noqa: BLE001 - rendering must never block delivery
            logger.warning("Reliable Reporting could not render issue event %s", lease.event_id)
    return ReportingIssueEvent(
        event_id=lease.event_id,
        kind=lease.kind,
        occurred_at=lease.occurred_at,
        account_id=lease.account_id,
        consumer_id=lease.consumer_id,
        issue_id=lease.issue_id,
        issue=issue,
        attempt=lease.attempts,
    )


def _render(
    lease: ReportingIssueEventLease,
    snapshot: ReportingStatusSnapshot,
    escalation: ReportingDeliveryEscalation | None,
) -> ReportingIssue | None:
    from adcp.reporting.ledger.notification_models import ReportingStatusScope
    from adcp.reporting.ledger.status_projection import StatusProjectionInput, _project

    at = lease.occurred_at
    occurrence = next(
        (
            item
            for item in snapshot.lifecycles
            if item.issue_key == lease.issue_key and item.generation == lease.generation
        ),
        None,
    )
    if occurrence is None:
        return None
    state = _RENDER_STATE.get(lease.kind)
    if state is None:  # escalated: as it stands, unless it has since been retired
        state = occurrence.issue_state if occurrence.issue_state == "acknowledged" else "open"
    # A waived occurrence leaves the projection by design, so render the
    # disagreement it waived and label it waived.
    rendered: ReportingIssueLifecycle = replace(
        occurrence,
        issue_state="acknowledged" if state == "waived" else state,
        retired_at=None,
        external_ref=None if lease.kind == "opened" else occurrence.external_ref,
        waived_reporting_status_id=None,
        waived_conflict_sha256=None,
    )
    as_of = replace(
        snapshot,
        as_of=at,
        statuses=_statuses_as_of(snapshot.statuses, at),
        obligations=tuple(o for o in snapshot.obligations if o.created_at <= at),
        revisions=tuple(r for r in snapshot.revisions if r.created_at <= at),
        lifecycles=tuple(rendered if item is occurrence else item for item in snapshot.lifecycles),
    )
    result, _ = _project(
        StatusProjectionInput(
            as_of,
            ReportingStatusScope(lease.account_id, consumer_id=lease.consumer_id),
            escalation,
        )
    )
    issue = next((item for item in result.issues if item.issue_id == lease.issue_id), None)
    if issue is None:
        return None
    return replace(issue, issue_state=state)


def _statuses_as_of(
    statuses: Sequence[ConsumerStatusRecord], at: datetime
) -> tuple[ConsumerStatusRecord, ...]:
    """The statements recorded by ``at``, superseded only by statements recorded by then."""
    recorded = [status for status in statuses if status.recorded_at <= at]
    superseded = {
        (status.consumer_id, status.supersedes_reporting_status_id)
        for status in recorded
        if status.supersedes_reporting_status_id is not None
    }
    return tuple(
        replace(
            status,
            superseded=(status.consumer_id, status.reporting_status_id) in superseded,
        )
        for status in recorded
    )


@dataclass(frozen=True)
class ReportingIssueDelivery:
    """What one dispatcher turn did."""

    #: Escalations recorded by this turn's sweep.
    escalations_recorded: int = 0
    #: Events the observer accepted.
    delivered: int = 0
    #: Events the observer raised on or timed out on; each is retried later.
    failed: int = 0

    @property
    def did_work(self) -> bool:
        return bool(self.escalations_recorded or self.delivered or self.failed)


ReportingIssueErrorReporter = Callable[[str, BaseException], Awaitable[None]]


class ReportingIssueEventDispatcher:
    """Deliver a store's recorded issue events to an observer, at least once.

    One :meth:`run_once` sweeps escalations, then claims due events in small
    leased batches (at most one per occurrence at a time, so an occurrence's
    events arrive in order), renders each, calls the observer, and settles the
    lease: delivered, or released for a retry after an exponential backoff
    (``retry_base`` doubling to ``retry_cap``). Delivered events are pruned
    after ``retention`` (``None`` keeps them). An event whose status snapshot
    cannot be read is released for a retry rather than delivered unrendered.

    :class:`~adcp.reporting.service.ReliableReportingService` runs one on each
    worker turn when given ``issue_observer``. Schedule it yourself only when
    you compose the ledger without the service.
    """

    def __init__(
        self,
        store: ReportingIssueEventStore,
        observer: ReportingIssueObserver,
        *,
        escalation: ReportingDeliveryEscalation | None = None,
        on_error: ReportingIssueErrorReporter | None = None,
        observer_timeout: timedelta = timedelta(seconds=10),
        batch_size: int = 10,
        max_batches: int = 10,
        retry_base: timedelta = timedelta(seconds=30),
        retry_cap: timedelta = timedelta(hours=1),
        retention: timedelta | None = timedelta(days=7),
    ) -> None:
        if not supports_issue_events(store):
            raise TypeError(
                "the ledger store does not record issue events; use "
                "InMemoryReportingLedgerStore, or PgReportingLedgerStore from "
                "adcp-reporting-storage 0.2.0b4 or later"
            )
        if not callable(observer):
            raise TypeError("observer must be callable")
        if observer_timeout <= timedelta(0) or batch_size < 1 or max_batches < 1:
            raise ValueError("observer_timeout, batch_size and max_batches must be positive")
        if (
            retry_base <= timedelta(0)
            or retry_cap < retry_base
            or (retention is not None and retention < timedelta(0))
        ):
            raise ValueError("invalid retry or retention policy")
        self.store = store
        self.observer = observer
        self.escalation = escalation
        self._on_error = on_error
        self._timeout = observer_timeout
        self._batch_size = batch_size
        self._max_batches = max_batches
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self._retention = retention

    @property
    def lease_seconds(self) -> float:
        """Long enough for a whole batch to time out, so a lease never lapses mid-batch."""
        return self._batch_size * self._timeout.total_seconds() + 30

    async def run_once(self) -> ReportingIssueDelivery:
        escalations = 0
        window = self.escalation.consumer_mismatch_escalation if self.escalation else None
        if window is not None:
            escalations = await self.store.sweep_issue_escalations(after=window)
        delivered = failed = 0
        for _ in range(self._max_batches):
            leases = await self.store.claim_issue_events(
                lease_seconds=self.lease_seconds, limit=self._batch_size
            )
            if not leases:
                break
            snapshots, unreadable = await self._snapshots(leases)
            for lease in leases:
                if (lease.account_id, lease.consumer_id) in unreadable:
                    failed += 1
                    await self.store.release_issue_event(
                        lease, retry_after=self._backoff(lease.attempts)
                    )
                    continue
                event = render_issue_event(
                    lease,
                    snapshots.get((lease.account_id, lease.consumer_id)),
                    escalation=self.escalation,
                )
                try:
                    await self._call(event)
                except Exception as error:
                    failed += 1
                    await self.store.release_issue_event(
                        lease, retry_after=self._backoff(lease.attempts)
                    )
                    await self._report(f"issue_observer:{lease.event_id}", error)
                    continue
                if await self.store.complete_issue_event(lease):
                    delivered += 1
        if self._retention is not None:
            await self.store.prune_issue_events(older_than=self._retention)
        return ReportingIssueDelivery(escalations, delivered, failed)

    async def _snapshots(self, leases: Sequence[ReportingIssueEventLease]) -> tuple[
        dict[tuple[str, str | None], ReportingStatusSnapshot],
        set[tuple[str, str | None]],
    ]:
        """One status snapshot per caller for rendering, and the callers that failed."""
        from adcp.reporting.ledger.status import ReportingStatusCaller
        from adcp.reporting.ledger.status_snapshot import ReportingStatusParticipant

        snapshots: dict[tuple[str, str | None], ReportingStatusSnapshot] = {}
        unreadable: set[tuple[str, str | None]] = set()
        if not isinstance(self.store, ReportingStatusParticipant):
            return snapshots, unreadable
        for account_id, consumer_id in sorted(
            {(lease.account_id, lease.consumer_id) for lease in leases if lease.kind != "resolved"},
            key=lambda item: (item[0], item[1] or ""),
        ):
            if consumer_id is None:
                continue
            try:
                snapshots[(account_id, consumer_id)] = await self.store.read_status_snapshot(
                    caller=ReportingStatusCaller(account_id=account_id, consumer_id=consumer_id)
                )
            except Exception as error:  # noqa: BLE001 - retried with the event
                unreadable.add((account_id, consumer_id))
                await self._report("issue_events:snapshot", error)
        return snapshots, unreadable

    async def _call(self, event: ReportingIssueEvent) -> None:
        result = self.observer(event)
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, self._timeout.total_seconds())

    def _backoff(self, attempts: int) -> timedelta:
        exponent = min(max(attempts - 1, 0), 20)
        delay: timedelta = self._retry_base * (1 << exponent)
        return min(delay, self._retry_cap)

    async def _report(self, component: str, error: BaseException) -> None:
        if self._on_error is not None:
            await self._on_error(component, error)
            return
        logger.error("Reliable Reporting %s failed", component.split(":", 1)[0])
