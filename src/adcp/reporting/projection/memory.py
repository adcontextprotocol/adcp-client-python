"""Memory conformance participant with the same unconditional rollback boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from adcp.reporting.feed.memory import InMemoryReportingFeedStore
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.models import ReportingDeliveryEscalation
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
)
from adcp.reporting.ledger.status_projection import with_replay_lifecycles
from adcp.reporting.ledger.status_snapshot import memory_snapshot
from adcp.reporting.ledger.store import _MEMORY_TRANSACTION
from adcp.reporting.outbox.memory import NotificationState
from adcp.reporting.outbox.status import (
    StatusCheckpoint,
    StatusDueLease,
    StatusTurn,
    escalation_identity,
    settled_replay,
)
from adcp.reporting.outbox.status_memory import (
    InMemoryReportingStatusOutbox,
    InMemoryStatusNotificationStore,
    _StatusMemoryState,
)
from adcp.reporting.projection.capture import (
    ReportingProjectionInput,
    capture_memory_input,
    source_identity,
    with_projection_core,
)
from adcp.reporting.projection.history import (
    HistoricalBoundary,
    checkpoint_key,
    project_boundary,
)

_REPLAY: ContextVar[object | None] = ContextVar("reporting_projection_replay", default=None)


@dataclass
class _ProjectionAccount:
    policy: dict[str, Any]
    checkpoint_floor: int
    current: ReportingProjectionInput
    source: bytes
    cursor: int = 0
    inputs: list[ReportingProjectionInput] = field(default_factory=list)
    ready: bool = False
    legacy: tuple[HistoricalBoundary, ...] = ()
    legacy_cursor: int = 0
    baselines: dict[str, StatusCheckpoint] = field(default_factory=dict)
    historical_checkpoints: dict[str, StatusCheckpoint] = field(default_factory=dict)
    historical_steps: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


class InMemoryReportingProjectionOutbox(InMemoryReportingStatusOutbox):
    _store: InMemoryReportingProjectionStore

    @property
    def _state(self) -> NotificationState:
        state = self._store._projection_outbox
        if state is None:
            raise ReportingNotificationError("notifications_disabled")
        return state


class InMemoryReportingProjectionStore(InMemoryReportingFeedStore):
    """Reference semantics, never a production durability claim."""

    _projection_read_policy: dict[str, Any]
    _projection_read_escalation: ReportingDeliveryEscalation | None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._projection_accounts: dict[str, _ProjectionAccount] = {}
        self._projection_outbox = (
            NotificationState() if self._notification_state is not None else None
        )

    def _capture_projection(self, account_id: str) -> ReportingProjectionInput:
        return capture_memory_input(
            memory_snapshot(self, account_id), tuple(getattr(self, "_delivery_records", ()))
        )

    @asynccontextmanager
    async def _mutation(self) -> AsyncIterator[None]:
        nested = _MEMORY_TRANSACTION.get() == (id(self), asyncio.current_task())
        async with super()._mutation():
            yield
            if not nested and _REPLAY.get() != id(self):
                for account_id, state in self._projection_accounts.items():
                    captured = self._capture_projection(account_id)
                    identity = source_identity(captured)
                    if identity != state.source:
                        state.inputs.append(captured)
                        state.source = identity

    def _feed_projection_options(self, caller: ReportingDeliveryPrincipal) -> dict[str, Any]:
        state = self._projection_accounts.get(caller.account_id)
        if state is None or not state.ready:
            return {}
        return {
            "representation_version": 2,
            "revision_ownership": state.policy["ownership_enabled"],
            "activated_consumer_status_enabled": state.policy["consumer_status_enabled"],
        }

    async def read_projection_input(
        self, *, caller: ReportingDeliveryPrincipal
    ) -> ReportingProjectionInput:
        async with self._lock:
            state = self._projection_accounts.get(caller.account_id)
            if state is None or not state.ready:
                raise ReportingNotificationError("status_projection_activation_required")
            return state.inputs[-1].at(self._clock())

    async def read_tier_status(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
    ) -> dict[str, Any]:
        from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
        from adcp.reporting.projection.wire import render_tier_status

        async with self._lock:
            state = self._projection_accounts.get(caller.account_id)
            value = state.inputs[-1].at(self._clock()) if state and state.ready else None
            policy = state.policy if state and state.ready else None
        if value is None or policy is None:
            return await ReportingStatusHandler(
                self, consumer_status_enabled=consumer_status_enabled
            ).handle(request, caller=ReportingStatusCaller(caller.account_id, caller.consumer_id))
        return render_tier_status(self, request, value, caller, policy, consumer_status_enabled)


class InMemoryReportingStatusProjection(InMemoryStatusNotificationStore):
    _projection_version = 2
    ledger: InMemoryReportingProjectionStore

    def __init__(
        self,
        ledger: InMemoryReportingProjectionStore,
        *,
        consumer_status_enabled: bool = False,
        revision_ownership: bool = False,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        if not isinstance(ledger, InMemoryReportingProjectionStore):
            raise ReportingNotificationError("status_projection_component_unready")
        if type(consumer_status_enabled) is not bool or type(revision_ownership) is not bool:
            raise ValueError("projection feature selections must be booleans")
        self.ledger, self.escalation = ledger, escalation
        self.consumer_status_enabled, self.revision_ownership = (
            consumer_status_enabled,
            revision_ownership,
        )
        if ledger._status_notification_state is None:
            ledger._status_notification_state = _StatusMemoryState()
        self.outbox = InMemoryReportingProjectionOutbox(ledger)
        ledger._projection_read_policy = self.policy
        ledger._projection_read_escalation = escalation

    def _enqueue_status(self, event: ReportingDomainEvent) -> None:
        self.outbox._state.enqueue(event)

    async def checkpoints(self, *, account_id: str) -> tuple[StatusCheckpoint, ...]:
        # A checkpoint contains a mutable JSON projection. Never lend that
        # dictionary to a reader or retain it in a caller's published result.
        return deepcopy(await super().checkpoints(account_id=account_id))

    @property
    def policy(self) -> dict[str, Any]:
        return {
            "version": 2,
            "escalation": escalation_identity(self.escalation),
            "consumer_status_enabled": self.consumer_status_enabled,
            "ownership_enabled": self.revision_ownership,
            "notifications_enabled": self.ledger._notification_state is not None,
        }

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        token = _REPLAY.set(id(self.ledger))
        try:
            async with self.ledger._mutation():
                yield
        finally:
            _REPLAY.reset(token)

    def _account(self, account_id: str) -> _ProjectionAccount:
        state = self.ledger._projection_accounts.get(account_id)
        if state is None:
            raise ReportingNotificationError("status_projection_activation_required")
        if state.policy != self.policy:
            raise ReportingNotificationError("status_projection_policy_conflict")
        return state

    def _cursor(self, account_id: str) -> int:
        self._account(account_id)
        return super()._cursor(account_id)

    async def baseline_ready(self, *, account_id: str) -> bool:
        async with self.ledger._lock:
            if account_id not in self.ledger._projection_accounts:
                return False
            return self._account(account_id).ready

    def _apply_value(
        self, value: ReportingProjectionInput, *, through: int, silent: bool = False
    ) -> int:
        return self._apply(
            settled_replay(value.core),
            through=through,
            reconciliation=value.reconciliation,
            consumer_status_enabled=self.consumer_status_enabled,
            enqueue=self.ledger._notification_state is not None and not silent,
        )

    async def activate(self, *, account_id: str) -> bool:
        started = await self._begin_activation(account_id=account_id)
        while True:
            async with self._transaction():
                state = self._account(account_id)
                if state.ready:
                    return started
                self._activation_step(account_id)

    async def _begin_activation(self, *, account_id: str) -> bool:
        async with self._transaction():
            if account_id in self.ledger._projection_accounts:
                self._account(account_id)
                return False
            old = self._state.accounts.get(account_id)
            notifications = self.ledger._notification_state
            through = (
                max(
                    (d.sequence for d in notifications.dirty if d.scope.account_id == account_id),
                    default=0,
                )
                if notifications
                else 0
            )
            if old is not None:
                if old[1] != escalation_identity(self.escalation):
                    raise ReportingNotificationError("status_policy_conflict")
                if old[0] != through or self._needs_rebuild(account_id):
                    raise ReportingNotificationError("status_projection_legacy_drain_required")
            checkpoints = [
                c for c in self._state.checkpoints.values() if c.scope.account_id == account_id
            ]
            now = self.ledger._clock()
            if any(
                (c.lease_expires_at and c.lease_expires_at > now)
                or (c.next_due_at and c.next_due_at <= now)
                for c in checkpoints
            ):
                raise ReportingNotificationError("status_projection_legacy_drain_required")
            value = self.ledger._capture_projection(account_id)
            floor = max((c.source_sequence for c in checkpoints), default=0)
            legacy = tuple(
                sorted(
                    (
                        b
                        for b in (
                            *self.ledger._materializer_boundaries,
                            *getattr(self.ledger, "_receipt_boundaries", ()),
                        )
                        if b.caller.account_id == account_id
                    ),
                    key=lambda b: b.account_sequence,
                )
            )
            expected = self.ledger._materializer_account_heads.get(account_id, 0)
            if len(legacy) != expected or any(
                b.account_sequence != n for n, b in enumerate(legacy, 1)
            ):
                raise ReportingNotificationError("status_projection_history_corrupt")
            self.ledger._projection_accounts[account_id] = _ProjectionAccount(
                self.policy,
                floor + expected,
                value,
                source_identity(value),
                inputs=[value],
                legacy=legacy,
                baselines={checkpoint_key(c): c for c in checkpoints},
            )
            self._state.accounts[account_id] = (through, escalation_identity(self.escalation))
            self._state.selector_accounts[account_id] = "complete"
            return True

    def _activation_step(self, account_id: str) -> StatusTurn:
        state = self._account(account_id)
        if state.legacy_cursor < len(state.legacy):
            boundary = state.legacy[state.legacy_cursor]
            for step in project_boundary(
                boundary,
                state.historical_checkpoints,
                baselines=state.baselines,
                source_sequence=state.checkpoint_floor
                - len(state.legacy)
                + boundary.account_sequence,
                escalation=self.escalation,
                consumer_status_enabled=self.consumer_status_enabled,
            ):
                state.historical_checkpoints[checkpoint_key(step.checkpoint)] = step.checkpoint
                state.historical_steps.append((boundary.account_sequence, step.document()))
                self._state.checkpoints[step.checkpoint.scope.checkpoint_key] = step.checkpoint
            state.legacy_cursor += 1
            return StatusTurn(True)
        self._apply_value(state.inputs[0], through=state.checkpoint_floor + 1, silent=True)
        state.cursor, state.ready = 1, True
        return StatusTurn(True)

    async def baseline(self, *, account_id: str) -> bool:
        return await self.activate(account_id=account_id)

    def _project_version(self, account_id: str) -> StatusTurn:
        state = self._account(account_id)
        if not state.ready:
            return self._activation_step(account_id)
        following = state.inputs[state.cursor] if state.cursor < len(state.inputs) else None
        due = min(
            (
                c.next_due_at
                for c in self._state.checkpoints.values()
                if c.scope.account_id == account_id and c.next_due_at is not None
            ),
            default=None,
        )
        cursor = state.cursor
        if (
            due is not None
            and due <= self.ledger._clock()
            and (following is None or due < following.core.as_of)
        ):
            value = state.current.at(due)
        elif following is not None:
            value, cursor = following, cursor + 1
        else:
            return StatusTurn(False)
        if value.core.as_of < state.current.core.as_of:
            raise ReportingNotificationError("status_projection_clock_regressed")
        value = with_projection_core(
            value, settled_replay(with_replay_lifecycles(value.core, state.current.core))
        )
        count = self._apply_value(value, through=state.checkpoint_floor + cursor)
        state.current, state.cursor = value, cursor
        return StatusTurn(True, count)

    async def project_one(self, *, account_id: str) -> StatusTurn:
        async with self._transaction():
            return self._project_version(account_id)

    async def rebuild_one(self) -> StatusTurn:
        async with self._transaction():
            for account_id, state in sorted(self.ledger._projection_accounts.items()):
                if state.cursor < len(state.inputs) and state.policy == self.policy:
                    return self._project_version(account_id)
            return StatusTurn(False)

    async def complete_due(self, lease: StatusDueLease) -> StatusTurn:
        async with self._transaction():
            self._account(lease.scope.account_id)
            checkpoint = self._state.checkpoints.get(lease.scope.checkpoint_key)
            if (
                checkpoint is None
                or checkpoint.lease_token != lease.token
                or checkpoint.lease_expires_at != lease.expires_at
                or lease.expires_at <= self.ledger._clock()
            ):
                return StatusTurn(False)
            result = self._project_version(lease.scope.account_id)
            current = self._state.checkpoints[lease.scope.checkpoint_key]
            if current.lease_token != lease.token or lease.expires_at <= self.ledger._clock():
                raise ReportingNotificationError("status_lease_lost")
            self._state.checkpoints[lease.scope.checkpoint_key] = replace(
                current,
                lease_token=None,
                lease_expires_at=None,
            )
            return result

    async def sweep_one(self) -> StatusTurn:
        async with self.ledger._lock:
            at = self.ledger._clock()
            due = sorted(
                (c.next_due_at, c.scope.account_id)
                for c in self._state.checkpoints.values()
                if c.next_due_at is not None
                and c.next_due_at <= at
                and c.scope.account_id in self.ledger._projection_accounts
                and self.ledger._projection_accounts[c.scope.account_id].policy == self.policy
                and (c.lease_expires_at is None or c.lease_expires_at <= at)
            )
        if not due:
            return StatusTurn(False)
        lease = await self.claim_due(account_id=due[0][1])
        return await self.complete_due(lease) if lease is not None else StatusTurn(False)
