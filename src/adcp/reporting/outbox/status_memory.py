"""Memory semantic reference for the optional status store; never advertises durability."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from secrets import token_hex
from typing import Any, Literal

from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionInput,
    project_status_scope,
    projection_scopes,
    with_replay_lifecycles,
)
from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot
from adcp.reporting.ledger.store import InMemoryReportingLedgerStore
from adcp.reporting.outbox.memory import InMemoryReportingOutbox, NotificationState
from adcp.reporting.outbox.status import (
    StatusCheckpoint,
    StatusDueLease,
    StatusTurn,
    advance_checkpoint,
    escalation_identity,
    settled_replay,
)
from adcp.reporting.revision_selection import REPORTING_SELECTOR_VERSION


@dataclass
class _StatusMemoryState:
    accounts: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    checkpoints: dict[tuple[str, str, str, int, str, str], StatusCheckpoint] = field(
        default_factory=dict
    )
    outbox: NotificationState = field(default_factory=NotificationState)
    replay: dict[str, ReportingStatusSnapshot] = field(default_factory=dict)
    selector_accounts: dict[str, Literal["transitioning", "complete"]] = field(default_factory=dict)


class InMemoryReportingStatusOutbox(InMemoryReportingOutbox):
    @property
    def _state(self) -> NotificationState:
        state: _StatusMemoryState = self._store._status_notification_state
        return state.outbox


class InMemoryStatusNotificationStore:
    def __init__(
        self,
        ledger: InMemoryReportingLedgerStore,
        *,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        if ledger._notification_state is None:
            raise ValueError("construct the ledger with notifications=True")
        self.ledger, self.escalation = ledger, escalation
        if ledger._status_notification_state is None:
            ledger._status_notification_state = _StatusMemoryState()
        state = ledger._status_notification_state
        if not hasattr(state, "selector_accounts"):
            state.selector_accounts = {}
        for key, checkpoint in tuple(state.checkpoints.items()):
            # An old shared-state image has no epoch fields. Decode by keyword
            # rather than letting new dataclass class defaults label it v2.
            if "selector_semantics_version" not in vars(checkpoint):
                state.checkpoints[key] = StatusCheckpoint(
                    scope=checkpoint.scope,
                    fingerprint=checkpoint.fingerprint,
                    generation=checkpoint.generation,
                    snapshot=checkpoint.snapshot,
                    next_due_at=checkpoint.next_due_at,
                    source_sequence=checkpoint.source_sequence,
                    baseline=checkpoint.baseline,
                    publishable=checkpoint.publishable,
                    lease_token=checkpoint.lease_token,
                    lease_expires_at=checkpoint.lease_expires_at,
                    selector_semantics_version=1,
                    selector_writer_floor=1,
                )
        self.outbox = InMemoryReportingStatusOutbox(ledger)

    @property
    def _state(self) -> _StatusMemoryState:
        state: _StatusMemoryState = self.ledger._status_notification_state
        return state

    async def create_schema(self) -> None:
        pass

    def _cursor(self, account_id: str) -> int:
        account = self._state.accounts.get(account_id)
        if account is None:
            raise ReportingNotificationError("status_baseline_required")
        if account[1] != escalation_identity(self.escalation):
            raise ReportingNotificationError("status_policy_conflict")
        return account[0]

    async def baseline(self, *, account_id: str) -> bool:
        async with self.ledger._mutation():
            if account_id in self._state.accounts:
                if not self._needs_rebuild(account_id):
                    self._cursor(account_id)
                return False
            snapshot = settle_memory_snapshot(self.ledger, account_id)
            assert self.ledger._notification_state is not None
            through = max(
                (
                    d.sequence
                    for d in self.ledger._notification_state.dirty
                    if d.scope.account_id == account_id
                ),
                default=0,
            )
            self._apply(snapshot, through=through, baseline=True)
            self._state.replay[account_id] = snapshot
            self._state.accounts[account_id] = (through, escalation_identity(self.escalation))
            self._state.selector_accounts[account_id] = "complete"
            return True

    async def baseline_ready(self, *, account_id: str) -> bool:
        async with self.ledger._mutation():
            if account_id not in self._state.accounts:
                return False
            if self._needs_rebuild(account_id):
                return False
            self._cursor(account_id)
            return True

    def _needs_rebuild(self, account_id: str) -> bool:
        return account_id in self._state.accounts and (
            self._state.selector_accounts.get(account_id) != "complete"
            or any(
                c.scope.account_id == account_id
                and (
                    c.selector_semantics_version != REPORTING_SELECTOR_VERSION
                    or c.selector_writer_floor != REPORTING_SELECTOR_VERSION
                )
                for c in self._state.checkpoints.values()
            )
        )

    def _rebuild(self, account_id: str) -> StatusTurn:
        if not self._needs_rebuild(account_id):
            return StatusTurn(False)
        if self._state.selector_accounts.get(account_id) != "transitioning":
            through, policy = self._state.accounts[account_id]
            expected = escalation_identity(self.escalation)
            if policy not in (
                expected,
                {k: v for k, v in expected.items() if k != "selector_semantics_version"},
            ):
                raise ReportingNotificationError("status_policy_conflict")
            self._state.accounts[account_id] = (through, expected)
            self._state.selector_accounts[account_id] = "transitioning"
            for key, checkpoint in tuple(self._state.checkpoints.items()):
                if checkpoint.scope.account_id == account_id:
                    self._state.checkpoints[key] = replace(
                        checkpoint, selector_writer_floor=REPORTING_SELECTOR_VERSION
                    )
            return StatusTurn(True)
        turn = self._project(account_id)
        if turn.did_work:
            return turn
        snapshot = settle_memory_snapshot(self.ledger, account_id)
        deadlines = [
            c.next_due_at
            for c in self._state.checkpoints.values()
            if c.scope.account_id == account_id
            and c.next_due_at is not None
            and c.next_due_at <= snapshot.as_of
        ]
        if deadlines:
            return StatusTurn(
                True,
                self._apply(
                    replace(snapshot, as_of=min(deadlines)), through=self._cursor(account_id)
                ),
            )
        count = self._apply(snapshot, through=self._cursor(account_id))
        self._state.replay[account_id] = snapshot
        self._state.selector_accounts[account_id] = "complete"
        return StatusTurn(True, count)

    async def rebuild_one(self) -> StatusTurn:
        async with self.ledger._mutation():
            expected = escalation_identity(self.escalation)
            legacy = {k: v for k, v in expected.items() if k != "selector_semantics_version"}
            account_id = next(
                (
                    a
                    for a in sorted(self._state.accounts)
                    if self._needs_rebuild(a) and self._state.accounts[a][1] in (expected, legacy)
                ),
                None,
            )
            return self._rebuild(account_id) if account_id is not None else StatusTurn(False)

    def _apply(
        self, snapshot: ReportingStatusSnapshot, *, through: int, baseline: bool = False
    ) -> int:
        snapshot = settled_replay(snapshot)
        events = 0
        scopes = {s.checkpoint_key: s for s in projection_scopes(snapshot)}
        scopes.update(
            (key, c.scope)
            for key, c in self._state.checkpoints.items()
            if c.scope.account_id == snapshot.account_id
        )
        for _, scope in sorted(scopes.items()):
            result = project_status_scope(StatusProjectionInput(snapshot, scope, self.escalation))
            checkpoint, event = advance_checkpoint(
                self._state.checkpoints.get(scope.checkpoint_key),
                result,
                fired_at=self.ledger._clock(),
                source_sequence=through,
                baseline=baseline,
            )
            self._state.checkpoints[scope.checkpoint_key] = checkpoint
            if event is not None:
                self._state.outbox.enqueue(event)
                events += 1
        return events

    def _project(self, account_id: str) -> StatusTurn:
        cursor = self._cursor(account_id)
        assert self.ledger._notification_state is not None
        boundary = next(
            (
                b
                for b in self.ledger._notification_state.boundaries
                if b.snapshot.account_id == account_id and b.through > cursor
            ),
            None,
        )
        if boundary is None:
            return StatusTurn(False)
        snapshot = settled_replay(
            with_replay_lifecycles(boundary.snapshot, self._state.replay.get(account_id))
        )
        count = self._apply(snapshot, through=boundary.through)
        self._state.replay[account_id] = snapshot
        self._state.accounts[account_id] = (boundary.through, escalation_identity(self.escalation))
        return StatusTurn(True, count)

    async def project_one(self, *, account_id: str) -> StatusTurn:
        async with self.ledger._mutation():
            rebuilt = self._rebuild(account_id)
            if rebuilt.did_work:
                return rebuilt
            return self._project(account_id)

    async def claim_due(
        self, *, account_id: str, lease_seconds: float = 30
    ) -> StatusDueLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self.ledger._lock:
            if self._needs_rebuild(account_id):
                return None
            self._cursor(account_id)
            at = self.ledger._clock()
            for key, checkpoint in sorted(self._state.checkpoints.items()):
                if checkpoint.scope.account_id != account_id or checkpoint.next_due_at is None:
                    continue
                if checkpoint.next_due_at > at or (
                    checkpoint.lease_expires_at is not None and checkpoint.lease_expires_at > at
                ):
                    continue
                lease = StatusDueLease(
                    checkpoint.scope,
                    token_hex(32),
                    at + timedelta(seconds=lease_seconds),
                    checkpoint.next_due_at,
                )
                self._state.checkpoints[key] = replace(
                    checkpoint, lease_token=lease.token, lease_expires_at=lease.expires_at
                )
                return lease
        return None

    async def complete_due(self, lease: StatusDueLease) -> StatusTurn:
        try:
            return await self._complete_due(lease)
        except _ExpiredStatusLeaseError:
            return StatusTurn(False)

    async def _complete_due(self, lease: StatusDueLease) -> StatusTurn:
        async with self.ledger._mutation():
            if self._needs_rebuild(lease.scope.account_id):
                return StatusTurn(False)
            checkpoint = self._state.checkpoints.get(lease.scope.checkpoint_key)
            at = self.ledger._clock()
            if (
                checkpoint is None
                or checkpoint.lease_token != lease.token
                or checkpoint.lease_expires_at != lease.expires_at
                or lease.expires_at <= at
            ):
                return StatusTurn(False)
            # Pending committed source transactions win over a previously claimed deadline.
            count = 0
            repaired = False
            while (turn := self._project(lease.scope.account_id)).did_work:
                count += turn.events
                repaired = True
            snapshot = settle_memory_snapshot(self.ledger, lease.scope.account_id)
            if repaired:
                count += self._apply(snapshot, through=self._cursor(lease.scope.account_id))
            else:
                # Walk distinct semantic deadlines in order, including overdue sweeps.
                for _ in range(1000):
                    due = [
                        c.next_due_at
                        for c in self._state.checkpoints.values()
                        if c.scope.account_id == lease.scope.account_id
                        and c.next_due_at is not None
                        and c.next_due_at <= at
                    ]
                    if not due:
                        break
                    count += self._apply(
                        replace(snapshot, as_of=min(due)),
                        through=self._cursor(lease.scope.account_id),
                    )
                else:
                    raise ReportingNotificationError("status_deadline_limit")
            current = self._state.checkpoints[lease.scope.checkpoint_key]
            if current.lease_token != lease.token or lease.expires_at <= self.ledger._clock():
                raise _ExpiredStatusLeaseError
            self._state.checkpoints[lease.scope.checkpoint_key] = replace(
                current, lease_token=None, lease_expires_at=None
            )
            return StatusTurn(True, count)

    async def release_due(self, lease: StatusDueLease) -> bool:
        async with self.ledger._lock:
            checkpoint = self._state.checkpoints.get(lease.scope.checkpoint_key)
            if (
                checkpoint is None
                or checkpoint.lease_token != lease.token
                or checkpoint.lease_expires_at != lease.expires_at
                or (lease.expires_at <= self.ledger._clock())
            ):
                return False
            self._state.checkpoints[lease.scope.checkpoint_key] = replace(
                checkpoint, lease_token=None, lease_expires_at=None
            )
            return True

    async def checkpoints(self, *, account_id: str) -> tuple[StatusCheckpoint, ...]:
        async with self.ledger._lock:
            return tuple(
                c
                for _, c in sorted(self._state.checkpoints.items())
                if c.scope.account_id == account_id
            )


class _ExpiredStatusLeaseError(Exception):
    pass
