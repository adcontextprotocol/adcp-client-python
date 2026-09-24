"""Process-local outbox sharing the ledger's lock and rollback boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from secrets import token_hex
from typing import TYPE_CHECKING

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.notification_models import (
    DirtyReason,
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusDirty,
    ReportingStatusEvidence,
    ReportingStatusScope,
    event_storage,
)
from adcp.reporting.outbox.activity import (
    ActivityOutcome,
    ActivityRequest,
    WebhookAttempt,
    activity_limit,
    retention_cutoff,
)
from adcp.reporting.outbox.identity import canonical_consumer
from adcp.reporting.outbox.models import (
    ERROR_CODES,
    DeliveryLease,
    DeliveryStatus,
    ErrorCode,
    ExpansionLease,
    StoredDelivery,
    WorkState,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.store import InMemoryReportingLedgerStore
    from adcp.reporting.outbox.status import StatusBoundary


@dataclass
class _Work:
    due_at: datetime
    state: WorkState = "pending"
    token: str | None = None
    expires_at: datetime | None = None
    attempts: int = 0
    error_code: ErrorCode | None = None

    def available(self, now: datetime) -> bool:
        return self.due_at <= now and (
            self.state == "pending"
            or (self.state == "leased" and self.expires_at is not None and self.expires_at <= now)
        )

    def held(self, token: str, now: datetime) -> bool:
        return (
            self.state == "leased"
            and self.token == token
            and self.expires_at is not None
            and self.expires_at > now
        )

    def claim(self, now: datetime, seconds: float) -> tuple[str, datetime]:
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.token = token_hex(32)
        self.expires_at = now + timedelta(seconds=seconds)
        self.attempts += 1
        self.state = "leased"
        return self.token, self.expires_at


@dataclass
class NotificationState:
    events: dict[tuple[str, str, str], ReportingDomainEvent] = field(default_factory=dict)
    causes: dict[tuple[str, str, str, str, str, int], str] = field(default_factory=dict)
    expansions: dict[tuple[str, str, str, int], _Work] = field(default_factory=dict)
    deliveries: dict[tuple[str, str, str], tuple[StoredDelivery, _Work]] = field(
        default_factory=dict
    )
    emissions: set[tuple[str, str, str, int, str]] = field(default_factory=set)
    dirty: list[ReportingStatusDirty] = field(default_factory=list)
    boundaries: list[StatusBoundary] = field(default_factory=list)
    checkpoints: dict[tuple[str, str], int] = field(default_factory=dict)
    issue_scopes: dict[tuple[str, str], ReportingStatusScope] = field(default_factory=dict)
    activity_heads: dict[tuple[str, str, str, str, str], int] = field(default_factory=dict)
    activity: dict[tuple[str, str, str, str, str, int], WebhookAttempt] = field(
        default_factory=dict
    )

    def enqueue(self, event: ReportingDomainEvent) -> None:
        """Internal synchronous participant; caller owns the ledger transaction."""
        existing_id = self.causes.get(event.causal_key)
        if existing_id is not None:
            if (
                self.events[(event.account_id, event.consumer_namespace, existing_id)].cause
                != event.cause
            ):
                raise ReportingNotificationError("event_identity_conflict")
            return
        self.events[(event.account_id, event.consumer_namespace, event.notification_id)] = event
        self.causes[event.causal_key] = event.notification_id
        self.expansions[(event.account_id, event.consumer_namespace, event.notification_id, 1)] = (
            _Work(event.fired_at)
        )

    def mark_dirty(
        self,
        scope: ReportingStatusScope,
        reason: DirtyReason,
        at: datetime,
        before: ReportingStatusEvidence | None = None,
        after: ReportingStatusEvidence | None = None,
    ) -> None:
        import hashlib

        sequence = sum(item.scope.account_id == scope.account_id for item in self.dirty) + 1
        evidence = after or before
        cause_id = (
            evidence.record_id
            if evidence is not None
            else hashlib.sha256(canonical_json_utf8_v1(asdict(scope))).hexdigest()
        )
        generation = (
            sum(
                item.scope == scope and item.reason == reason and item.cause_id == cause_id
                for item in self.dirty
            )
            + 1
        )
        self.dirty.append(
            ReportingStatusDirty(
                sequence, scope, reason, aware_utc(at), cause_id, generation, before, after
            )
        )


def validate_finish(state: WorkState, error_code: ErrorCode | None) -> None:
    if state not in {"pending", "complete", "suppressed", "quarantined"}:
        raise ValueError("invalid work transition")
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError("invalid local error classification")


def _finish(
    work: _Work,
    *,
    now: datetime,
    state: WorkState,
    error_code: ErrorCode | None,
    retry_at: datetime | None,
) -> None:
    validate_finish(state, error_code)
    work.state, work.error_code = state, error_code
    work.due_at = aware_utc(retry_at or now)
    work.token = work.expires_at = None


class InMemoryReportingOutbox:
    """View over an opted-in ledger. Recreating this view preserves its state."""

    def __init__(self, store: InMemoryReportingLedgerStore) -> None:
        if store._notification_state is None:
            raise ValueError("construct the ledger with notifications=True")
        self._store = store

    @property
    def _state(self) -> NotificationState:
        state = self._store._notification_state
        assert state is not None
        return state

    async def list_events(self, *, account_id: str) -> tuple[ReportingDomainEvent, ...]:
        async with self._store._lock:
            return tuple(
                event for (owner, _, _), event in self._state.events.items() if owner == account_id
            )

    async def claim_expansion(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> ExpansionLease | None:
        now = aware_utc(now)
        async with self._store._lock:
            for (owner, consumer, notification, generation), work in self._state.expansions.items():
                if owner == account_id and work.available(now):
                    token, expires = work.claim(now, lease_seconds)
                    event = self._state.events[(owner, consumer, notification)]
                    return ExpansionLease(
                        owner,
                        notification,
                        generation,
                        token,
                        expires,
                        work.attempts,
                        event_storage(event),
                        event.consumer_namespace,
                    )
        return None

    async def complete_expansion(
        self, lease: ExpansionLease, deliveries: tuple[StoredDelivery, ...], *, now: datetime
    ) -> bool:
        async with self._store._mutation():
            work = self._state.expansions.get(
                (
                    lease.account_id,
                    lease.consumer_namespace,
                    lease.notification_id,
                    lease.emission_generation,
                )
            )
            if work is None or not work.held(lease.token, now):
                return False
            for delivery in deliveries:
                binding = delivery.binding
                if (
                    binding.account_id != lease.account_id
                    or binding.notification_id != lease.notification_id
                    or binding.emission_generation != lease.emission_generation
                    or binding.consumer_namespace != lease.consumer_namespace
                ):
                    raise ReportingNotificationError("invalid_configuration")
                key = (
                    binding.account_id,
                    binding.consumer_namespace,
                    binding.notification_id,
                    binding.emission_generation,
                    binding.subscriber_id,
                )
                if key not in self._state.emissions:
                    self._state.deliveries[
                        (binding.account_id, binding.consumer_namespace, binding.delivery_id)
                    ] = (
                        delivery,
                        _Work(now),
                    )
                    self._state.emissions.add(key)
            _finish(work, now=now, state="complete", error_code=None, retry_at=None)
            return True

    async def finish_expansion(
        self,
        lease: ExpansionLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        validate_finish(state, error_code)
        async with self._store._lock:
            work = self._state.expansions.get(
                (
                    lease.account_id,
                    lease.consumer_namespace,
                    lease.notification_id,
                    lease.emission_generation,
                )
            )
            if work is None or not work.held(lease.token, now):
                return False
            _finish(work, now=now, state=state, error_code=error_code, retry_at=retry_at)
            return True

    async def reemit(
        self,
        *,
        account_id: str,
        notification_id: str,
        now: datetime,
        consumer_namespace: str | None = None,
    ) -> int:
        async with self._store._lock:
            consumers = [
                c
                for a, c, n in self._state.events
                if a == account_id
                and n == notification_id
                and (consumer_namespace is None or c == consumer_namespace)
            ]
            if len(consumers) != 1:
                raise ReportingNotificationError("event_unavailable")
            consumer = consumers[0]
            generation = 1 + max(
                g
                for a, c, n, g in self._state.expansions
                if (a, c, n) == (account_id, consumer, notification_id)
            )
            self._state.expansions[(account_id, consumer, notification_id, generation)] = _Work(now)
            return generation

    async def claim_delivery(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> DeliveryLease | None:
        now = aware_utc(now)
        async with self._store._lock:
            for (owner, _, _), (delivery, work) in self._state.deliveries.items():
                if owner == account_id and work.available(now):
                    token, expires = work.claim(now, lease_seconds)
                    return DeliveryLease(delivery, token, expires, work.attempts)
        return None

    async def delivery_lease_current(self, lease: DeliveryLease, *, now: datetime) -> bool:
        async with self._store._lock:
            binding = lease.delivery.binding
            item = self._state.deliveries.get(
                (binding.account_id, binding.consumer_namespace, binding.delivery_id)
            )
            return item is not None and item[0] == lease.delivery and item[1].held(lease.token, now)

    async def finish_delivery(
        self,
        lease: DeliveryLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool:
        validate_finish(state, error_code)
        async with self._store._lock:
            binding = lease.delivery.binding
            item = self._state.deliveries.get(
                (binding.account_id, binding.consumer_namespace, binding.delivery_id)
            )
            if item is None or item[0] != lease.delivery or not item[1].held(lease.token, now):
                return False
            _finish(item[1], now=now, state=state, error_code=error_code, retry_at=retry_at)
            return True

    async def reserve_attempt(
        self, lease: DeliveryLease, *, request: ActivityRequest, now: datetime
    ) -> WebhookAttempt | None:
        binding = lease.delivery.binding
        consumer = canonical_consumer(binding.principal_id)
        request = ActivityRequest(request.url, request.payload_size_bytes)
        key = (
            binding.account_id,
            consumer,
            binding.consumer_namespace,
            binding.subscriber_id,
            binding.idempotency_key,
        )
        async with self._store._lock:
            item = self._state.deliveries.get(
                (binding.account_id, binding.consumer_namespace, binding.delivery_id)
            )
            if (
                item is None
                or item[0] != lease.delivery
                or item[1].expires_at != lease.expires_at
                or not item[1].held(lease.token, now)
            ):
                return None
            if any(
                row.lease_token == lease.token and row.binding == binding
                for row in self._state.activity.values()
            ):
                return None
            number = self._state.activity_heads.get(key, 0) + 1
            attempt = WebhookAttempt(
                binding, number, lease.token, token_hex(32), aware_utc(now), request
            )
            self._state.activity_heads[key] = number
            self._state.activity[(*key, number)] = attempt
            return attempt

    async def complete_attempt(
        self, attempt: WebhookAttempt, *, outcome: ActivityOutcome, now: datetime
    ) -> bool:
        ActivityOutcome.__post_init__(outcome)
        binding = attempt.binding
        consumer = canonical_consumer(binding.principal_id)
        key = (
            binding.account_id,
            consumer,
            binding.consumer_namespace,
            binding.subscriber_id,
            binding.idempotency_key,
            attempt.attempt,
        )
        async with self._store._lock:
            original = self._state.activity.get(key)
            if (
                original != attempt
                or attempt.outcome is not None
                or aware_utc(now) < attempt.fired_at
            ):
                return False
            self._state.activity[key] = replace(
                attempt, outcome=outcome, completed_at=aware_utc(now)
            )
            return True

    async def list_activity(
        self, *, account_id: str, consumer_id: str, limit: int = 50
    ) -> tuple[WebhookAttempt, ...]:
        consumer_id, limit = canonical_consumer(consumer_id), activity_limit(limit)
        async with self._store._lock:
            return tuple(
                sorted(
                    (
                        row
                        for key, row in self._state.activity.items()
                        if key[0] == account_id and key[1] == consumer_id
                    ),
                    key=lambda row: (
                        row.fired_at,
                        row.binding.notification_id,
                        row.binding.idempotency_key,
                        row.binding.subscriber_id,
                        row.attempt,
                        row.binding.delivery_id,
                    ),
                    reverse=True,
                )[:limit]
            )

    async def purge_activity(
        self, *, account_id: str, consumer_id: str, now: datetime, retention_days: int = 30
    ) -> int:
        consumer_id = canonical_consumer(consumer_id)
        cutoff = retention_cutoff(now, retention_days)
        async with self._store._lock:
            keys = [
                key
                for key, row in self._state.activity.items()
                if key[0] == account_id
                and key[1] == consumer_id
                and row.completed_at is not None
                and row.completed_at < cutoff
            ]
            for key in keys:
                del self._state.activity[key]
            return len(keys)

    async def list_deliveries(self, *, account_id: str) -> tuple[DeliveryStatus, ...]:
        async with self._store._lock:
            return tuple(
                DeliveryStatus(delivery, work.state, work.attempts, work.due_at, work.error_code)
                for (owner, _, _), (delivery, work) in self._state.deliveries.items()
                if owner == account_id
            )

    async def mark_status_dirty(
        self, scope: ReportingStatusScope, *, reason: DirtyReason, now: datetime
    ) -> None:
        """Additive sweep seam; this slice schedules no clock-driven work."""
        async with self._store._lock:
            self._state.mark_dirty(scope, reason, now)

    async def read_status_dirty(
        self, *, account_id: str, after: int = 0, limit: int = 100
    ) -> tuple[ReportingStatusDirty, ...]:
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("invalid dirty checkpoint window")
        async with self._store._lock:
            return tuple(
                deepcopy(
                    [
                        item
                        for item in self._state.dirty
                        if item.scope.account_id == account_id and item.sequence > after
                    ][:limit]
                )
            )

    async def status_checkpoint(self, *, account_id: str, projector_id: str) -> int:
        async with self._store._lock:
            return self._state.checkpoints.get((account_id, projector_id), 0)

    async def advance_status_checkpoint(
        self, *, account_id: str, projector_id: str, expected: int, through: int
    ) -> bool:
        async with self._store._lock:
            maximum = sum(item.scope.account_id == account_id for item in self._state.dirty)
            key = (account_id, projector_id)
            if not 0 <= expected <= through <= maximum:
                raise ValueError("invalid status checkpoint")
            if self._state.checkpoints.get(key, 0) != expected:
                return False
            self._state.checkpoints[key] = through
            return True
