"""Persistence contract for independently leased fanout and HTTP work.

These are additive protocols. Existing ReportingLedgerStore implementations do
not acquire any new required method or producer callback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, TypeAlias

from adcp.reporting.ledger.notification_models import ReportingDomainEvent, ReportingStatusDirty

WorkState: TypeAlias = Literal["pending", "leased", "complete", "suppressed", "quarantined"]
ErrorCode: TypeAlias = Literal[
    "network",
    "retryable_http",
    "permanent_http",
    "signing_unavailable",
    "permanent_scope",
    "subscription_unavailable",
    "subscription_changed",
    "invalid_configuration",
    "invalid_payload",
    "integrity_failure",
    "lease_expired",
]
ERROR_CODES = frozenset(
    {
        "network",
        "retryable_http",
        "permanent_http",
        "signing_unavailable",
        "permanent_scope",
        "subscription_unavailable",
        "subscription_changed",
        "invalid_configuration",
        "invalid_payload",
        "integrity_failure",
        "lease_expired",
    }
)


@dataclass(frozen=True)
class DeliveryBinding:
    """Only non-secret routing identifiers/hashes may be stored in plaintext.

    Every field participates in authenticated encryption. destination_sha256
    binds the full URL (including query); the URL itself is encrypted.
    """

    account_id: str
    delivery_id: str
    subscriber_id: str
    principal_id: str
    notification_id: str
    notification_type: str
    emission_generation: int
    idempotency_key: str
    destination_sha256: str
    subscription_fingerprint: str
    signing_scope_id: str | None
    cause_kind: str
    cause_id: str
    cause_generation: int
    consumer_namespace: str
    auth_mode: str
    body_sha256: str
    envelope_version: int
    key_version: str


@dataclass(frozen=True)
class StoredDelivery:
    binding: DeliveryBinding
    envelope: bytes = field(repr=False)


@dataclass(frozen=True)
class ExpansionLease:
    account_id: str
    notification_id: str
    emission_generation: int
    token: str = field(repr=False)
    expires_at: datetime
    attempt_count: int
    event: dict[str, Any] = field(repr=False)
    consumer_namespace: str = ""


@dataclass(frozen=True)
class DeliveryLease:
    delivery: StoredDelivery
    token: str = field(repr=False)
    expires_at: datetime
    attempt_count: int


@dataclass(frozen=True)
class DeliveryStatus:
    delivery: StoredDelivery
    state: WorkState
    attempt_count: int
    due_at: datetime
    error_code: ErrorCode | None


class ReportingNotificationOutbox(Protocol):
    async def list_events(self, *, account_id: str) -> tuple[ReportingDomainEvent, ...]: ...

    async def claim_expansion(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> ExpansionLease | None: ...

    async def complete_expansion(
        self, lease: ExpansionLease, deliveries: tuple[StoredDelivery, ...], *, now: datetime
    ) -> bool: ...

    async def finish_expansion(
        self,
        lease: ExpansionLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool: ...

    async def reemit(self, *, account_id: str, notification_id: str, now: datetime) -> int: ...

    async def claim_delivery(
        self, *, account_id: str, now: datetime, lease_seconds: float
    ) -> DeliveryLease | None: ...

    async def delivery_lease_current(self, lease: DeliveryLease, *, now: datetime) -> bool: ...

    async def finish_delivery(
        self,
        lease: DeliveryLease,
        *,
        now: datetime,
        state: WorkState,
        error_code: ErrorCode | None = None,
        retry_at: datetime | None = None,
    ) -> bool: ...

    async def list_deliveries(self, *, account_id: str) -> tuple[DeliveryStatus, ...]: ...

    async def read_status_dirty(
        self, *, account_id: str, after: int = 0, limit: int = 100
    ) -> tuple[ReportingStatusDirty, ...]: ...

    async def status_checkpoint(self, *, account_id: str, projector_id: str) -> int: ...

    async def advance_status_checkpoint(
        self, *, account_id: str, projector_id: str, expected: int, through: int
    ) -> bool: ...
