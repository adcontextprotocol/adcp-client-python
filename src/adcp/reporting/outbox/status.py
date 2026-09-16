"""Optional durable status lifecycle API and deterministic worker turns.

Importable without PostgreSQL. Stores own the transaction; neither the
projector nor the sweeper performs HTTP or supplies a second persistence seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusDirty,
    ReportingStatusScope,
    StatusChanged,
)
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionResult,
    apply_intents_to_snapshot,
    lifecycle_intents,
)
from adcp.reporting.revision_selection import REPORTING_SELECTOR_VERSION


@dataclass(frozen=True)
class StatusBoundary:
    transaction_id: str
    through: int
    dirty: tuple[ReportingStatusDirty, ...]
    snapshot: ReportingStatusSnapshot


@dataclass(frozen=True)
class StatusCheckpoint:
    scope: ReportingStatusScope
    fingerprint: str
    generation: int
    snapshot: dict[str, Any]
    next_due_at: datetime | None
    source_sequence: int
    baseline: bool
    publishable: bool
    lease_token: str | None = field(default=None, repr=False)
    lease_expires_at: datetime | None = None
    selector_semantics_version: int = REPORTING_SELECTOR_VERSION
    selector_writer_floor: int = REPORTING_SELECTOR_VERSION


@dataclass(frozen=True)
class StatusDueLease:
    scope: ReportingStatusScope
    token: str = field(repr=False)
    expires_at: datetime
    due_at: datetime


@dataclass(frozen=True)
class StatusTurn:
    did_work: bool
    events: int = 0


class StatusNotificationStore(Protocol):
    """Opt-in C participant; no methods are added to the ledger/outbox protocols."""

    async def create_schema(self) -> None: ...

    async def baseline(self, *, account_id: str) -> bool: ...

    async def baseline_ready(self, *, account_id: str) -> bool: ...

    async def project_one(self, *, account_id: str) -> StatusTurn: ...

    async def claim_due(
        self, *, account_id: str, lease_seconds: float
    ) -> StatusDueLease | None: ...

    async def complete_due(self, lease: StatusDueLease) -> StatusTurn: ...

    async def release_due(self, lease: StatusDueLease) -> bool: ...

    async def checkpoints(self, *, account_id: str) -> tuple[StatusCheckpoint, ...]: ...


@runtime_checkable
class StatusSelectorRebuildStore(Protocol):
    """Indexed, account-discovering C cutover seam; no materializer work queue."""

    async def rebuild_one(self) -> StatusTurn: ...


@dataclass(frozen=True)
class ReportingStatusProjector:
    store: StatusNotificationStore

    async def run_once(self, *, account_id: str) -> StatusTurn:
        return await self.store.project_one(account_id=account_id)

    async def rebuild_once(self) -> StatusTurn:
        """Reproject one populated old scope's account, without an account list."""
        if not isinstance(self.store, StatusSelectorRebuildStore):
            raise ReportingNotificationError("status_selector_rebuild_unsupported")
        return await self.store.rebuild_one()


@dataclass(frozen=True)
class ReportingStatusSweeper:
    store: StatusNotificationStore
    lease_seconds: float = 30

    async def run_once(self, *, account_id: str) -> StatusTurn:
        lease = await self.store.claim_due(account_id=account_id, lease_seconds=self.lease_seconds)
        if lease is None:
            return StatusTurn(False)
        return await self.store.complete_due(lease)


def advance_checkpoint(
    previous: StatusCheckpoint | None,
    result: StatusProjectionResult,
    *,
    fired_at: datetime,
    source_sequence: int,
    baseline: bool = False,
) -> tuple[StatusCheckpoint, ReportingDomainEvent | None]:
    """Shared mutation/sweep primitive. Caller already owns the typed scope lock."""
    if result.intents:
        raise ReportingNotificationError("status_lifecycle_pending")
    generation = previous.generation if previous is not None else 0
    event = None
    if (
        not baseline
        and result.publishable
        and (previous is None or previous.fingerprint != result.fingerprint)
    ):
        generation += 1
        event = ReportingDomainEvent(
            result.scope.account_id,
            str(uuid4()),
            fired_at,
            StatusChanged(
                scope=result.scope,
                health=result.health,
                fingerprint=result.fingerprint,
                checkpoint_generation=generation,
                previous_health=previous.snapshot["health"] if previous else None,
                issue_ids=result.issue_ids,
            ),
            cause_generation=generation,
        )
        event.body(subscriber_id="validation", idempotency_key="0" * 32)
    return (
        StatusCheckpoint(
            scope=result.scope,
            fingerprint=(
                result.fingerprint
                if result.publishable or previous is None
                else previous.fingerprint
            ),
            generation=generation,
            snapshot=result.canonical(),
            next_due_at=result.next_due_at,
            source_sequence=source_sequence,
            baseline=baseline or bool(previous and previous.baseline),
            publishable=result.publishable,
            lease_token=previous.lease_token if previous else None,
            lease_expires_at=previous.lease_expires_at if previous else None,
        ),
        event,
    )


def settled_replay(snapshot: ReportingStatusSnapshot) -> ReportingStatusSnapshot:
    """Replay a captured committed boundary without consulting final current rows."""
    for _ in range(3):
        intents = lifecycle_intents(snapshot)
        if not intents:
            return snapshot
        snapshot = apply_intents_to_snapshot(snapshot, intents)
    raise ReportingNotificationError("status_lifecycle_did_not_converge")


def escalation_identity(escalation: ReportingDeliveryEscalation | None) -> dict[str, Any]:
    return {
        **(escalation.to_wire() if escalation else {}),
        "selector_semantics_version": REPORTING_SELECTOR_VERSION,
    }
