"""Buyer events for reporting subscriptions: ``poll``, ``events``, ``handle_webhook`` (AdCP 3.3).

Events are derived from durable state, never taken from a webhook body:

1. :meth:`ReportingClient.poll` runs ``reconcile`` and reads the subscription
   readback and each account's ``subscription_coverage[]``.
2. It compares that snapshot with the one this consumer last acknowledged,
   and emits one :class:`ReportingEvent` per difference.
3. ``await batch.ack()`` saves the new snapshot. Until then nothing moves:
   a crash before the ack replays the same events, with the same ids, on the
   next poll. Delivery is at least once; deduplicate on ``event.id``.

Event ids are ``sha256(agent, type, natural key, version)``, so a replay
after a crash, or the same change seen by two workers, produces the same id.

A seller's ``principal.changed`` webhook is a doorbell: pass the verified
delivery to :meth:`ReportingClient.handle_webhook`, which wakes a running
:meth:`ReportingClient.events` loop, or polls inline when none runs.

Event types (every event has ``id``, ``type``, ``agent_id``, ``occurred_at``,
``observed_at`` and a ``data`` mapping):

* ``revision.available`` and ``revision.adjusted`` (a new revision replaced
  one this consumer saw);
* ``period.late``, ``period.missing_obligation``, ``period.withheld``;
* ``coverage.exception_opened`` and ``coverage.exception_closed``, with
  ``reason``, ``from`` and ``until``;
* ``account.covered``, ``account.unauthorized``, ``account.regranted``;
* ``subscription.state_changed`` (``active``, ``inactive``,
  ``action_required``);
* ``timezone.cutover``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.subscriptions._clock import iso_z, utc_instant
from adcp.reporting.subscriptions.reconcile import (
    DEFAULT_GRACE,
    BuyerReportingState,
    ReconcileReport,
    _reconcile,
)

#: Version of the saved snapshot document; bump on a breaking change.
SNAPSHOT_VERSION = 1

EVENT_TYPES = frozenset(
    {
        "revision.available",
        "revision.adjusted",
        "period.late",
        "period.missing_obligation",
        "period.withheld",
        "coverage.exception_opened",
        "coverage.exception_closed",
        "account.covered",
        "account.unauthorized",
        "account.regranted",
        "subscription.state_changed",
        "timezone.cutover",
    }
)


@dataclass(frozen=True)
class ReportingEvent:
    """One change this consumer has not acknowledged yet."""

    id: str
    type: str
    agent_id: str
    occurred_at: str
    observed_at: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        # ``event.account_id``, ``event.reporting_revision_id`` and friends.
        data = object.__getattribute__(self, "data")
        if name in data:
            return data[name]
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "agent_id": self.agent_id,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "data": dict(self.data),
        }


class ReportingEventStore(Protocol):
    """Where a consumer's acknowledged snapshot lives, per agent.

    ``save`` must refuse (raise) when the stored revision is no longer
    ``expected_revision``, so two workers never both acknowledge.
    """

    async def load(self, agent_id: str) -> tuple[Mapping[str, Any], int] | None: ...

    async def save(
        self, agent_id: str, snapshot: Mapping[str, Any], *, expected_revision: int | None
    ) -> int: ...


class ReportingEventConflictError(RuntimeError):
    """Another worker acknowledged first; poll again."""

    code = "REPORTING_EVENTS_CONFLICT"
    recovery = "transient"


class InMemoryReportingEventStore:
    """Process-local snapshots: development and tests only."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[dict[str, Any], int]] = {}

    async def load(self, agent_id: str) -> tuple[Mapping[str, Any], int] | None:
        item = self._items.get(agent_id)
        return None if item is None else (json.loads(json.dumps(item[0])), item[1])

    async def save(
        self, agent_id: str, snapshot: Mapping[str, Any], *, expected_revision: int | None
    ) -> int:
        current = self._items.get(agent_id)
        if (current[1] if current else None) != expected_revision:
            raise ReportingEventConflictError(
                "Another worker acknowledged these events first. Poll again."
            )
        revision = (current[1] if current else 0) + 1
        self._items[agent_id] = (json.loads(json.dumps(snapshot)), revision)
        return revision


@dataclass
class EventBatch:
    """One poll: the events since the last acknowledgement, and its ``ack``."""

    events: list[ReportingEvent]
    report: ReconcileReport
    _store: ReportingEventStore
    _agent_id: str
    _snapshot: Mapping[str, Any]
    _revision: int | None
    acked: bool = False

    async def ack(self) -> None:
        """Save this poll's snapshot, so its events are not delivered again."""
        if self.acked:
            return
        await self._store.save(self._agent_id, self._snapshot, expected_revision=self._revision)
        self.acked = True


class VerifiedWebhook:
    """A webhook delivery whose signature the SDK verified.

    Build it with :meth:`from_outcome` from a :class:`~adcp.webhook_receiver.WebhookReceiver`
    outcome; :meth:`ReportingClient.handle_webhook` refuses anything else at
    runtime, so an unverified body never reaches the poller.
    """

    __slots__ = ("payload", "sender_identity")

    def __init__(self, payload: Any, sender_identity: str, *, _token: object = None) -> None:
        if _token is not _VERIFIED:
            raise TypeError("use VerifiedWebhook.from_outcome(outcome)")
        self.payload = payload
        self.sender_identity = sender_identity

    @classmethod
    def from_outcome(cls, outcome: Any) -> VerifiedWebhook:
        """From a ``WebhookReceiver.receive`` outcome that verified and was not rejected."""
        if getattr(outcome, "rejected", True) or not getattr(outcome, "sender_identity", None):
            raise ValueError("the webhook was not verified; it cannot wake the poller")
        return cls(outcome.payload, outcome.sender_identity, _token=_VERIFIED)


_VERIFIED = object()


# -- snapshots -------------------------------------------------------------------------


def _key(*parts: Any) -> str:
    return json.dumps([str(part) if part is not None else None for part in parts])


def _snapshot(report: ReconcileReport, context: Any) -> dict[str, Any]:
    """The state events are derived from, as a JSON document."""
    periods: dict[str, dict[str, Any]] = {}
    for line in report.periods:
        if line.status == "not_owed":
            continue
        periods[
            _key(
                line.account_id,
                line.subscription_id,
                line.subscription_version,
                line.offering_id,
                line.period["start"],
                line.period["end"],
            )
        ] = {
            "status": line.status,
            "reporting_revision_id": line.reporting_revision_id,
            "reporting_obligation_id": line.reporting_obligation_id,
            "finality": line.finality,
            "expected_at": line.expected_at,
            "period": dict(line.period),
            "account_id": line.account_id,
            "subscription_id": line.subscription_id,
            "subscription_version": line.subscription_version,
            "offering_id": line.offering_id,
            "media_buy_ids": list(line.media_buy_ids),
        }
    subscriptions: dict[str, dict[str, Any]] = {}
    unauthorized: dict[str, str] = {}
    for state in context.subscriptions:
        value = state.model_dump(mode="json", exclude_none=True)
        issues = value.get("issues") or []
        subscriptions[str(state.subscription_id)] = {
            "state": value.get("state"),
            "subscription_version": value.get("subscription_version"),
            "pending_action": issues[0] if issues else None,
        }
        for entry in value.get("accounts_unauthorized") or []:
            unauthorized[entry["account_id"]] = entry["unauthorized_at"]
    covered: list[str] = sorted(
        {_key(line.subscription_id, line.account_id) for line in report.periods}
    )
    exceptions: dict[str, dict[str, Any]] = {}
    cutovers: dict[str, dict[str, Any]] = {}
    authorizations: dict[str, dict[str, Any]] = {}
    for account_id in report.accounts:
        record = context.state.accounts.get(account_id)
        if record is None:
            continue
        if record.listed:
            authorizations[account_id] = {
                "authorized_at": None
                if record.authorized_at is None
                else iso_z(record.authorized_at),
                "regranted_at": None if record.regranted_at is None else iso_z(record.regranted_at),
            }
        for subscription_id, entry in record.coverage.items():
            covered.append(_key(subscription_id, account_id))
            for item in entry.get("excluded") or ():
                exceptions[
                    _key(
                        subscription_id,
                        account_id,
                        item.get("reason"),
                        item.get("media_buy_id"),
                        item.get("from"),
                        ",".join(item.get("package_ids") or ()),
                    )
                ] = {
                    "subscription_id": subscription_id,
                    "account_id": account_id,
                    "reason": item.get("reason"),
                    "media_buy_id": item.get("media_buy_id"),
                    "from": item.get("from"),
                    "until": item.get("until"),
                    "package_ids": list(item.get("package_ids") or ()),
                }
            for item in entry.get("timezone_cutovers") or ():
                cutovers[
                    _key(
                        subscription_id,
                        account_id,
                        item.get("offering_id"),
                        item.get("effective_at"),
                    )
                ] = {
                    "subscription_id": subscription_id,
                    "account_id": account_id,
                    "offering_id": item.get("offering_id"),
                    "effective_at": item.get("effective_at"),
                    "timezone": item.get("timezone"),
                    "previous_timezone": item.get("previous_timezone"),
                }
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "periods": periods,
        "subscriptions": subscriptions,
        "covered": sorted(set(covered)),
        "unauthorized": unauthorized,
        "authorizations": authorizations,
        "exceptions": exceptions,
        "cutovers": cutovers,
    }


def _event_id(agent_id: str, type_: str, natural_key: str, version: Any) -> str:
    digest = hashlib.sha256(
        canonical_json_utf8_v1([agent_id, type_, natural_key, version])
    ).hexdigest()
    return f"rse_{digest[:40]}"


def derive_events(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    agent_id: str,
    observed_at: datetime,
    grace: timedelta = DEFAULT_GRACE,
) -> list[ReportingEvent]:
    """The events between two snapshots, in a deterministic order.

    With no ``previous`` snapshot every present fact is new: each delivered
    revision, open exception, covered account and so on is emitted once.
    """
    before: Mapping[str, Any] = previous or {}
    observed = iso_z(observed_at)
    events: list[ReportingEvent] = []

    def emit(
        type_: str, natural_key: str, version: Any, occurred: str | None, data: Mapping[str, Any]
    ) -> None:
        events.append(
            ReportingEvent(
                id=_event_id(agent_id, type_, natural_key, version),
                type=type_,
                agent_id=agent_id,
                occurred_at=occurred or observed,
                observed_at=observed,
                data=dict(data),
            )
        )

    old_subscriptions = before.get("subscriptions") or {}
    for subscription_id, value in sorted((current.get("subscriptions") or {}).items()):
        old = old_subscriptions.get(subscription_id)
        if old is None or old.get("state") != value.get("state"):
            emit(
                "subscription.state_changed",
                _key(subscription_id),
                [value.get("subscription_version"), value.get("state")],
                None,
                {
                    "subscription_id": subscription_id,
                    "state": value.get("state"),
                    "previous_state": None if old is None else old.get("state"),
                    "subscription_version": value.get("subscription_version"),
                    "pending_action": value.get("pending_action"),
                },
            )

    old_covered = set(before.get("covered") or ())
    for item in current.get("covered") or ():
        if item not in old_covered:
            subscription_id, account_id = json.loads(item)
            emit(
                "account.covered",
                item,
                1,
                None,
                {"subscription_id": subscription_id, "account_id": account_id},
            )

    old_unauthorized = before.get("unauthorized") or {}
    new_unauthorized = current.get("unauthorized") or {}
    for account_id, lost in sorted(new_unauthorized.items()):
        if old_unauthorized.get(account_id) != lost:
            emit(
                "account.unauthorized",
                _key(account_id),
                lost,
                lost,
                {"account_id": account_id, "unauthorized_at": lost},
            )
    old_authorizations = before.get("authorizations") or {}
    for account_id, lost in sorted(old_unauthorized.items()):
        if account_id in new_unauthorized:
            continue
        granted = (current.get("authorizations") or {}).get(account_id)
        if granted is None:
            continue
        regranted = granted.get("regranted_at") or granted.get("authorized_at")
        emit(
            "account.regranted",
            _key(account_id),
            [lost, regranted],
            regranted,
            {
                "account_id": account_id,
                "unauthorized_at": lost,
                "regranted_at": regranted,
                "authorized_at": granted.get("authorized_at"),
                "chain": "continued"
                if granted.get("authorized_at")
                == (old_authorizations.get(account_id) or {}).get("authorized_at")
                else "new",
            },
        )

    old_exceptions = before.get("exceptions") or {}
    new_exceptions = current.get("exceptions") or {}
    for key, value in sorted(new_exceptions.items()):
        old = old_exceptions.get(key)
        if value.get("until") is None and (old is None or old.get("until") is not None):
            emit("coverage.exception_opened", key, value.get("from"), value.get("from"), value)
        if value.get("until") is not None and (old is not None and old.get("until") is None):
            emit("coverage.exception_closed", key, value.get("until"), value.get("until"), value)
    for key, old in sorted(old_exceptions.items()):
        if key not in new_exceptions and old.get("until") is None:
            emit("coverage.exception_closed", key, "gone", None, {**old, "until": None})

    old_cutovers = before.get("cutovers") or {}
    for key, value in sorted((current.get("cutovers") or {}).items()):
        if key not in old_cutovers:
            emit("timezone.cutover", key, 1, value.get("effective_at"), value)

    old_periods = before.get("periods") or {}
    for key, value in sorted((current.get("periods") or {}).items()):
        old = old_periods.get(key) or {}
        line = {k: value[k] for k in value if k not in {"status"}}
        revision = value.get("reporting_revision_id")
        old_revision = old.get("reporting_revision_id")
        if revision is not None and revision != old_revision:
            type_ = "revision.adjusted" if old_revision is not None else "revision.available"
            emit(
                type_,
                key,
                revision,
                None,
                {**line, "superseded_reporting_revision_id": old_revision},
            )
        status = value.get("status")
        if status != old.get("status") and status in {"late", "missing_obligation", "withheld"}:
            occurred = None
            if status == "late" and value.get("expected_at"):
                occurred = iso_z(_parse(value["expected_at"]) + grace)
            elif status == "missing_obligation":
                occurred = iso_z(_parse(value["period"]["end"]) + grace)
            emit(f"period.{status}", key, status, occurred, line)
    return events


def _parse(value: str) -> datetime:
    return utc_instant(datetime.fromisoformat(value.replace("Z", "+00:00")))


# -- the consumer ----------------------------------------------------------------------


class ReportingEvents:
    """``poll``, ``events`` and ``handle_webhook`` for one client (see the module doc)."""

    def __init__(
        self,
        client: Any,
        state: BuyerReportingState,
        *,
        store: ReportingEventStore | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self.store: ReportingEventStore = store or InMemoryReportingEventStore()
        self._agent_id = agent_id
        self._doorbell: asyncio.Event | None = None
        self._listening = 0

    @property
    def agent_id(self) -> str:
        if self._agent_id is not None:
            return self._agent_id
        config = getattr(self._client, "agent_config", None)
        identifier = getattr(config, "id", None) or getattr(config, "agent_uri", None)
        return str(identifier or "default")

    async def poll(
        self,
        *,
        grace: timedelta = DEFAULT_GRACE,
        baseline: bool = False,
        now: datetime | None = None,
    ) -> EventBatch:
        """One batch of events since the last acknowledgement; call ``batch.ack()`` after handling.

        ``baseline=True`` on a consumer with no snapshot yet records the
        current state without emitting it.
        """
        instant = utc_instant(now, field="now") if now is not None else datetime.now(timezone.utc)
        report, context = await _reconcile(
            self._client,
            state=self._state,
            since=None,
            accounts="covered",
            subscription_id=None,
            grace=grace,
            include_not_owed=False,
            now=instant,
        )
        current = _snapshot(report, context)
        loaded = await self.store.load(self.agent_id)
        previous, revision = (None, None) if loaded is None else loaded
        if previous is not None and previous.get("snapshot_version") != SNAPSHOT_VERSION:
            previous = None
        if not report.definitive and previous is not None:
            # Never treat an unread account as a change: keep the previous view of
            # what this run could not read.
            current = _merge_partial(previous, current, report)
        if loaded is None and baseline:
            events: list[ReportingEvent] = []
        else:
            events = derive_events(
                previous, current, agent_id=self.agent_id, observed_at=instant, grace=grace
            )
        return EventBatch(events, report, self.store, self.agent_id, current, revision)

    async def events(
        self,
        *,
        poll_every: timedelta = timedelta(minutes=5),
        stop: asyncio.Event | None = None,
        grace: timedelta = DEFAULT_GRACE,
    ) -> AsyncIterator[ReportingEvent]:
        """Every event, forever (or until ``stop`` is set).

        Each batch is acknowledged once the loop body has run for all of its
        events, so an exception in the body redelivers the batch.
        """
        if self._doorbell is None:
            self._doorbell = asyncio.Event()
        self._listening += 1
        try:
            while stop is None or not stop.is_set():
                batch = await self.poll(grace=grace)
                for event in batch.events:
                    yield event
                await batch.ack()
                self._doorbell.clear()
                waiters = [asyncio.ensure_future(self._doorbell.wait())]
                if stop is not None:
                    waiters.append(asyncio.ensure_future(stop.wait()))
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=poll_every.total_seconds(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for item in pending:
                    item.cancel()
        finally:
            self._listening -= 1

    async def handle_webhook(self, verified: Any) -> EventBatch | None:
        """A verified seller webhook: wake the ``events()`` loop, or poll inline.

        ``verified`` is a :class:`VerifiedWebhook` or a
        :class:`~adcp.webhook_receiver.WebhookOutcome` that verified and was not
        rejected; anything else is refused. The body is never trusted as event
        content: it only says "something changed". With a running ``events()``
        loop this returns ``None``; otherwise it returns the polled batch, for
        the caller to handle and ``ack``.
        """
        if not isinstance(verified, VerifiedWebhook):
            from adcp.webhook_receiver import WebhookOutcome

            if not isinstance(verified, WebhookOutcome):
                raise TypeError(
                    "handle_webhook takes a verified delivery: VerifiedWebhook.from_outcome("
                    "await receiver.receive(...)). Verify the signature first."
                )
            verified = VerifiedWebhook.from_outcome(verified)
        if self._listening and self._doorbell is not None:
            self._doorbell.set()
            return None
        return await self.poll()


def _merge_partial(
    previous: Mapping[str, Any], current: dict[str, Any], report: ReconcileReport
) -> dict[str, Any]:
    unread = {issue.account_id for issue in report.issues if issue.account_id}
    everything = not report.accounts or any(issue.account_id is None for issue in report.issues)
    merged = dict(current)
    for section in ("periods", "exceptions", "cutovers"):
        values = dict(current.get(section) or {})
        for key, value in (previous.get(section) or {}).items():
            if everything or value.get("account_id") in unread:
                values.setdefault(key, value)
        merged[section] = values
    if everything:
        # A run that could not read the principal or the roster keeps the
        # previous subscriptions and losses, so nothing reads as changed.
        merged["subscriptions"] = {
            **dict(previous.get("subscriptions") or {}),
            **dict(current.get("subscriptions") or {}),
        }
        merged["unauthorized"] = {
            **dict(previous.get("unauthorized") or {}),
            **dict(current.get("unauthorized") or {}),
        }
        merged["covered"] = sorted(
            set(current.get("covered") or ()) | set(previous.get("covered") or ())
        )
        merged["authorizations"] = {
            **dict(previous.get("authorizations") or {}),
            **dict(current.get("authorizations") or {}),
        }
    return merged


__all__ = [
    "EVENT_TYPES",
    "SNAPSHOT_VERSION",
    "EventBatch",
    "InMemoryReportingEventStore",
    "ReportingEvent",
    "ReportingEventConflictError",
    "ReportingEventStore",
    "ReportingEvents",
    "VerifiedWebhook",
    "derive_events",
]
