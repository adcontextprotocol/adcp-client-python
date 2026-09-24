"""Deterministic in-memory model of the durable materializer state machine.

This implementation is for conformance and never establishes production tier
readiness. Mutations, including disabled-notification turns, roll back together.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from adcp.reporting.ledger._delivery_state import RecordT
from adcp.reporting.ledger.delivery import InMemoryReportingReconciliationStore
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.ledger.models import ReportingConfiguration
from adcp.reporting.ledger.notification_events import delivery_dirty, materialization_event
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer._errors import (
    ReportingMaterializerUsageError,
    materializer_errors,
)
from adcp.reporting.materializer.capture import (
    MaterializerNotificationState,
    ReportingMaterializerBoundary,
    private_snapshot,
)
from adcp.reporting.materializer.contracts import (
    ReportingDestinationRequest,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterError,
    ReportingWriterFailure,
    binding_fingerprint,
    failure,
)
from adcp.reporting.materializer.verification import (
    ReportingVerifiedDestination,
    validate_materialization_target,
    validate_verified_destination,
)
from adcp.reporting.materializer.work import (
    MaterializerContext,
    MaterializerReason,
    ReportingMaterializerLease,
    ReportingMaterializerTurn,
    key_for,
    public_failure,
    validate_lease_seconds,
    verification_key_id,
)


@dataclass
class _Candidate:
    scope: ReportingDeliveryScope
    generation: int = 1
    due_at: datetime | None = None
    reason: MaterializerReason = "ready"
    turn: int = 0


@dataclass
class _Work:
    scope: ReportingDeliveryScope
    generation: int
    attempt: ReportingMaterializationAttempt
    request: ReportingDestinationRequest
    due_at: datetime | None
    notifications_enabled: bool
    # This reservation can never produce a production-admitted readiness event,
    # including when a later installation resumes an uncertain external effect.
    admission_epoch: int = 0
    token: str | None = None
    lease_until: datetime | None = None
    completion_token: str | None = None
    acked: bool = False
    retry_allowed: bool = False
    reason: MaterializerReason = "ready"


class InMemoryReportingMaterializerStore(InMemoryReportingReconciliationStore):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._materializer_candidates: dict[ReportingDeliveryScope, _Candidate] = {}
        self._materializer_work: dict[tuple[str, str, str], _Work] = {}
        self._materializer_account_turns: dict[str, int] = {}
        self._materializer_turn = 0
        self._materializer_outbox = (
            MaterializerNotificationState() if self._notification_state is not None else None
        )
        self._materializer_boundaries: list[ReportingMaterializerBoundary] = []
        self._materializer_status_heads: dict[ReportingDeliveryPrincipal, int] = {}
        self._materializer_account_heads: dict[str, int] = {}

    def _wake(self, scope: ReportingDeliveryScope) -> None:
        candidate = self._materializer_candidates.get(scope)
        if candidate is None:
            candidate = _Candidate(scope)
            self._materializer_candidates[scope] = candidate
        else:
            candidate.generation += 1
        candidate.due_at, candidate.reason = self._clock(), "ready"

    def _wake_obligation(self, account_id: str, obligation_id: str) -> None:
        obligation = self._obligations.get(obligation_id)
        if obligation is None or obligation.account_id != account_id:
            return
        for _, _, record in self._retained_delivery_records():
            if isinstance(record, ReportingDestinationBinding) and (
                record.generation_key == obligation.generation_key
            ):
                self._wake(
                    ReportingDeliveryScope(
                        obligation.generation_key, record.consumer_id, obligation_id
                    )
                )

    def _append(self, account_id: str, kind: Any, record_id: str) -> None:
        super()._append(account_id, kind, record_id)
        if kind == "obligation":
            self._wake_obligation(account_id, record_id)
        elif kind == "revision":
            self._wake_obligation(account_id, self._revisions[record_id].reporting_obligation_id)

    async def put_configuration(self, configuration: ReportingConfiguration) -> None:
        async with self._mutation():
            before = self._configurations.get(configuration.generation_key)
            await super().put_configuration(configuration)
            if before != configuration:
                for scope in self._materializer_candidates:
                    if scope.generation_key == configuration.generation_key:
                        self._wake(scope)

    async def set_revision_readable(
        self,
        *,
        account_id: str,
        reporting_revision_id: str,
        readable: bool,
    ) -> None:
        async with self._mutation():
            before = self._revisions.get(reporting_revision_id)
            await super().set_revision_readable(
                account_id=account_id,
                reporting_revision_id=reporting_revision_id,
                readable=readable,
            )
            revision = self._revisions.get(reporting_revision_id)
            if revision is not None and revision != before:
                self._wake_obligation(account_id, revision.reporting_obligation_id)

    def _commit_record_unlocked(
        self, record: RecordT, *, notify: bool = True, dirty: bool = True
    ) -> tuple[RecordT, bool]:
        # Only the fenced verified finish may enqueue a readiness intent. An
        # ordinary public outcome keeps the projection dirty work it has always
        # produced; suppressing that would silently stall the status projector.
        stored, added = super()._commit_record_unlocked(
            record,
            notify=notify and not isinstance(record, ReportingMaterializationRecord),
            dirty=dirty,
        )
        if added:
            if isinstance(record, ReportingDestinationBinding):
                for obligation in self._obligations.values():
                    if obligation.generation_key == record.generation_key:
                        self._wake(
                            ReportingDeliveryScope(
                                obligation.generation_key,
                                record.consumer_id,
                                obligation.reporting_obligation_id,
                            )
                        )
            elif isinstance(record, ReportingMaterializationCheck):
                self._wake(record.scope)
        return stored, added

    def _context(self, scope: ReportingDeliveryScope) -> MaterializerContext:
        records = tuple(c.record for c in self._caller_changes(scope.principal))
        binding = next(
            (
                r
                for r in records
                if isinstance(r, ReportingDestinationBinding)
                and r.generation_key == scope.generation_key
            ),
            None,
        )
        obligation = self._obligations.get(scope.reporting_obligation_id)
        configuration = self._configurations.get(scope.generation_key)
        if (
            binding is None
            or obligation is None
            or configuration is None
            or obligation.generation_key != scope.generation_key
        ):
            raise failure("BINDING_MISMATCH")
        delivery = next(
            (
                r
                for r in records
                if isinstance(r, ReportingObligationDeliveryRecord) and r.scope == scope
            ),
            None,
        )
        return MaterializerContext(
            configuration,
            obligation,
            binding,
            delivery,
            tuple(
                r
                for r in self._revisions.values()
                if r.account_id == scope.principal.account_id
                and r.reporting_obligation_id == scope.reporting_obligation_id
            ),
            records,
        )

    def _work_key(self, attempt: ReportingMaterializationAttempt) -> tuple[str, str, str]:
        return (
            attempt.scope.principal.account_id,
            attempt.scope.consumer_id,
            attempt.reporting_materialization_id,
        )

    def _park(
        self,
        candidate: _Candidate,
        reason: MaterializerReason,
        due: datetime | None = None,
    ) -> ReportingMaterializerTurn:
        candidate.reason, candidate.due_at = reason, due
        return ReportingMaterializerTurn("parked", reason)

    @materializer_errors
    async def claim_materialization(
        self,
        *,
        keys: tuple[ReportingVerificationKey, ...],
        lease_seconds: int = 30,
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        validate_lease_seconds(lease_seconds)
        async with self._mutation():
            now = self._clock()
            pending = {w.scope for w in self._materializer_work.values() if not w.acked}
            works = [
                w
                for w in self._materializer_work.values()
                if not w.acked
                and w.due_at is not None
                and w.due_at <= now
                and (w.lease_until is None or w.lease_until <= now)
            ]
            candidates = [
                c
                for c in self._materializer_candidates.values()
                if c.due_at is not None and c.due_at <= now and c.scope not in pending
            ]
            accounts = {w.scope.principal.account_id for w in works} | {
                c.scope.principal.account_id for c in candidates
            }
            if not accounts:
                return ReportingMaterializerTurn("idle")
            account_id = min(
                accounts, key=lambda a: (self._materializer_account_turns.get(a, 0), a)
            )
            self._materializer_turn += 1
            self._materializer_account_turns[account_id] = self._materializer_turn
            account_work = [w for w in works if w.scope.principal.account_id == account_id]
            if account_work:
                work = min(account_work, key=lambda w: (w.due_at or now, w.request.external_id))
                return self._lease(work, keys, lease_seconds)
            candidate = min(
                (c for c in candidates if c.scope.principal.account_id == account_id),
                key=lambda c: (c.turn, c.scope.consumer_id, c.scope.reporting_obligation_id),
            )
            candidate.turn = self._materializer_turn
            try:
                context = self._context(candidate.scope)
                key = key_for(context.binding, context.obligation, keys)
            except LedgerConflictError:
                return self._park(candidate, "history_corrupt")
            except ReportingWriterError:
                return self._park(candidate, "component_unavailable")
            revision, reason = context.selection(now)
            if revision is None or reason != "ready":
                at = context.configuration.activated_at
                return self._park(candidate, reason, at if at is not None and at > now else None)
            attempts = context.attempts(revision.reporting_revision_id)
            if tuple(a.attempt for a in attempts) != tuple(range(1, len(attempts) + 1)):
                return self._park(candidate, "history_corrupt")
            if context.pending_attempts():
                return self._park(candidate, "legacy_pending")
            if attempts:
                outcome = context.outcome(attempts[-1])
                assert outcome is not None
                if outcome.status != "failed":
                    reason, expires = context.retained_success(outcome, now)
                    return self._park(candidate, reason, expires)
                owned = self._materializer_work.get(self._work_key(attempts[-1]))
                if owned is None or not owned.retry_allowed:
                    return self._park(
                        candidate, "legacy_terminal" if owned is None else "operator_required"
                    )
            if context.delivery is None:
                if context.obligation.currency is None:
                    return self._park(candidate, "operator_required")
                delivery = ReportingObligationDeliveryRecord(
                    candidate.scope,
                    context.obligation.currency,
                    max(now, context.obligation.period.end)
                    + timedelta(days=context.binding.resource_retention_days),
                    now,
                )
                self._commit_record_unlocked(delivery, notify=False, dirty=False)
            attempt = ReportingMaterializationAttempt(
                candidate.scope,
                revision.reporting_revision_id,
                "rpm_" + uuid4().hex,
                len(attempts) + 1,
                now,
            )
            self._commit_record_unlocked(attempt, notify=False, dirty=False)
            request = ReportingDestinationRequest.from_binding(context.binding, attempt, key)
            work = _Work(
                candidate.scope,
                candidate.generation,
                attempt,
                request,
                now,
                self._notification_state is not None,
            )
            self._materializer_work[self._work_key(attempt)] = work
            self._park(candidate, "ready")
            return self._lease(work, keys, lease_seconds)

    def _lease(
        self,
        work: _Work,
        keys: tuple[ReportingVerificationKey, ...],
        seconds: int,
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        if work.notifications_enabled != (self._notification_state is not None):
            return self._park_work(work, "component_unavailable")
        try:
            context = self._context(work.scope)
            if not context.resumable(work.attempt):
                return self._park_work(work, "history_corrupt")
            key = key_for(
                context.binding,
                context.obligation,
                keys,
                required=verification_key_id(work.request.verification_key),
            )
            if (
                ReportingDestinationRequest.from_binding(context.binding, work.attempt, key)
                != work.request
            ):
                raise failure("BINDING_MISMATCH")
        except (ReportingWriterError, LedgerConflictError):
            return self._park_work(work, "component_unavailable")
        work.token = str(uuid4())
        work.lease_until = self._clock() + timedelta(seconds=seconds)
        work.due_at = work.lease_until
        return ReportingMaterializerLease(
            work.scope,
            work.generation,
            work.token,
            work.lease_until,
            work.attempt,
            work.request,
            context,
            work.notifications_enabled,
        )

    def _park_work(self, work: _Work, reason: MaterializerReason) -> ReportingMaterializerTurn:
        work.due_at, work.reason = None, reason
        work.token, work.lease_until = None, None
        return self._park(self._materializer_candidates[work.scope], reason)

    def _held(self, lease: ReportingMaterializerLease) -> _Work | None:
        work = self._materializer_work.get(self._work_key(lease.attempt))
        if (
            work is None
            or work.acked
            or work.token != lease.token
            or work.lease_until is None
            or work.lease_until <= self._clock()
        ):
            return None
        if (
            work.request != lease.request
            or work.generation != lease.generation
            or work.notifications_enabled != lease.notifications_enabled
            or work.notifications_enabled != (self._notification_state is not None)
        ):
            raise failure("BINDING_MISMATCH")
        return work

    def _target(
        self, lease: ReportingMaterializerLease
    ) -> tuple[MaterializerContext, MaterializerReason]:
        context = self._context(lease.scope)
        selected, reason = context.selection(self._clock())
        if reason != "ready":
            return context, reason
        candidate = self._materializer_candidates[lease.scope]
        if (
            candidate.generation != lease.generation
            or selected is None
            or selected.reporting_revision_id != lease.attempt.reporting_revision_id
        ):
            return context, "target_changed"
        if binding_fingerprint(context.binding) != lease.request.binding_fingerprint:
            return context, "binding_changed"
        return context, "ready"

    @materializer_errors
    async def renew_materialization(
        self, lease: ReportingMaterializerLease, *, lease_seconds: int
    ) -> bool:
        validate_lease_seconds(lease_seconds)
        async with self._mutation():
            work = self._held(lease)
            if work is None:
                return False
            work.lease_until = self._clock() + timedelta(seconds=lease_seconds)
            work.due_at = work.lease_until
            return True

    @materializer_errors
    async def authorize_materialization(self, lease: ReportingMaterializerLease) -> None:
        async with self._mutation():
            if self._held(lease) is None:
                raise failure("LEASE_LOST")
            _, reason = self._target(lease)
            if reason != "ready":
                raise failure(
                    "HISTORY_CORRUPT" if reason == "history_corrupt" else "CURRENT_REVISION_CHANGED"
                )

    @materializer_errors
    async def finish_materialization(
        self,
        lease: ReportingMaterializerLease,
        *,
        prepared: ReportingPreparedRevision | None = None,
        verified: ReportingVerifiedDestination | None = None,
        error: ReportingWriterFailure | None = None,
    ) -> ReportingMaterializerTurn:
        if verified is not None:
            validate_verified_destination(verified, lease.request, token=lease.token)
            if prepared is None or prepared.request != lease.request or error is not None:
                raise failure("BINDING_MISMATCH")
        elif error is None:
            raise failure("BINDING_MISMATCH")
        async with self._mutation():
            work = self._held(lease)
            if work is None:
                previous = self._materializer_work.get(self._work_key(lease.attempt))
                if (
                    previous is not None
                    and previous.acked
                    and previous.completion_token == lease.token
                    and previous.request == lease.request
                ):
                    return ReportingMaterializerTurn(
                        "verified" if previous.reason == "verified" else "failed",
                        previous.reason,
                        lease.attempt.reporting_materialization_id,
                    )
                return ReportingMaterializerTurn(
                    "pending", "effect_unknown", lease.attempt.reporting_materialization_id
                )
            context, reason = self._target(lease)
            if reason != "ready":
                error = ReportingWriterFailure("CURRENT_REVISION_CHANGED", "new_attempt", "applied")
                verified = None
            elif verified is not None and prepared is not None:
                validate_materialization_target(
                    prepared, binding=context.binding, revisions=context.revisions
                )
            if error is not None and (
                error.effect == "unknown"
                or error.retry == "same_identity"
                or error.code in {"DEADLINE_EXCEEDED", "LEASE_LOST"}
            ):
                work.token, work.lease_until, work.reason = None, None, "effect_unknown"
                work.due_at = self._clock() + timedelta(
                    seconds=max(1, min(error.retry_after_seconds or 5, 300))
                )
                return ReportingMaterializerTurn(
                    "pending", work.reason, work.attempt.reporting_materialization_id
                )
            now = self._clock()
            if verified is not None and (
                context.delivery is None
                or verified.resource.expires_at
                < max(
                    context.delivery.resource_retained_until,
                    now + timedelta(days=context.binding.resource_retention_days),
                )
            ):
                error = ReportingWriterFailure("RESOURCE_UNAVAILABLE", "new_attempt", "applied")
                verified = None
            if verified is not None:
                # Validate again under the finish lock, immediately before
                # copying observations into the immutable terminal record.
                validate_verified_destination(verified, lease.request, token=lease.token)
            outcome = ReportingMaterializationRecord(
                lease.scope,
                lease.attempt.reporting_revision_id,
                lease.attempt.reporting_materialization_id,
                context.binding.success_status if verified is not None else "failed",
                now,
                verified.resource if verified is not None else None,
                replace(verified.verification, verified_at=now) if verified is not None else None,
                public_failure(error) if error is not None else None,
            )
            stored, inserted = self._commit_record_unlocked(outcome, notify=False, dirty=False)
            self._materializer_dirty(stored, context)
            if inserted and verified is not None and self._notification_state is not None:
                revision = next(
                    r
                    for r in context.revisions
                    if r.reporting_revision_id == stored.reporting_revision_id
                )
                event = materialization_event(
                    stored,
                    context.records,
                    context.obligation,
                    revision,
                    context.configuration,
                    now,
                )
                if event is None:
                    raise failure("BINDING_MISMATCH")
                assert self._materializer_outbox is not None
                self._materializer_outbox.enqueue(event)
                if (
                    self._materializer_outbox.events.get(
                        (event.account_id, event.consumer_namespace, event.notification_id)
                    )
                    != event
                ):
                    raise failure("BINDING_MISMATCH")
            if self._held(lease) is None:
                raise failure("LEASE_LOST")
            work.completion_token = work.token
            work.acked, work.token, work.lease_until = True, None, None
            work.retry_allowed = error is not None and error.retry == "new_attempt"
            if reason == "ready":
                reason = (
                    "verified"
                    if verified is not None
                    else ("retry" if work.retry_allowed else "operator_required")
                )
            work.reason = reason
            due = (
                now + timedelta(seconds=max(1, error.retry_after_seconds or 5))
                if work.retry_allowed and error
                else None
            )
            if reason == "target_changed":
                due = now
            elif reason not in {"retry", "verified"}:
                due = None
            if (
                reason == "inactive"
                and context.configuration.activated_at is not None
                and context.configuration.activated_at > now
            ):
                due = context.configuration.activated_at
            if verified is not None:
                due = verified.resource.expires_at
            self._park(self._materializer_candidates[lease.scope], reason, due)
            return ReportingMaterializerTurn(
                "verified" if verified is not None else "failed",
                reason,
                stored.reporting_materialization_id,
            )

    def _materializer_dirty(
        self, outcome: ReportingMaterializationRecord, context: MaterializerContext
    ) -> None:
        scope, reason, evidence = delivery_dirty(outcome, context.obligation)
        self._dirty_status(scope, reason, after=evidence)

        from adcp.reporting.ledger.status_snapshot import settle_memory_snapshot

        core = settle_memory_snapshot(self, scope.account_id)
        core = replace(core, as_of=outcome.completed_at)
        sequence = self._materializer_status_heads.get(outcome.scope.principal, 0) + 1
        self._materializer_status_heads[outcome.scope.principal] = sequence
        account_sequence = self._materializer_account_heads.get(scope.account_id, 0) + 1
        self._materializer_account_heads[scope.account_id] = account_sequence
        self._materializer_boundaries.append(
            ReportingMaterializerBoundary(
                outcome.scope.principal,
                sequence,
                account_sequence,
                outcome.reporting_materialization_id,
                outcome.completed_at,
                private_snapshot(core, outcome.scope.principal),
                tuple(c.record for c in self._caller_changes(outcome.scope.principal)),
            )
        )

    @materializer_errors
    async def read_materializer_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingMaterializerBoundary, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ReportingMaterializerUsageError(
                "materializer boundary reads require bounded positions"
            )
        async with self._lock:
            return tuple(
                b
                for b in self._materializer_boundaries
                if b.caller == caller and b.sequence > after
            )[:limit]

    @materializer_errors
    async def import_pending_materialization(
        self,
        *,
        scope: ReportingDeliveryScope,
        reporting_materialization_id: str,
        original_external_id: str,
        keys: tuple[ReportingVerificationKey, ...],
    ) -> None:
        async with self._mutation():
            context = self._context(scope)
            attempt = next(
                (
                    r
                    for r in context.records
                    if isinstance(r, ReportingMaterializationAttempt)
                    and r.reporting_materialization_id == reporting_materialization_id
                    and r.scope == scope
                ),
                None,
            )
            if attempt is None or context.outcome(attempt) is not None:
                raise failure("BINDING_MISMATCH")
            if not context.resumable(attempt):
                raise failure("HISTORY_CORRUPT")
            key = key_for(context.binding, context.obligation, keys)
            request = ReportingDestinationRequest.from_binding(context.binding, attempt, key)
            if original_external_id != request.external_id:
                raise failure("BINDING_MISMATCH")
            if scope not in self._materializer_candidates:
                self._wake(scope)
            existing = self._materializer_work.get(self._work_key(attempt))
            if existing is not None:
                if existing.token is None and not existing.acked:
                    existing.due_at = self._clock()
                return
            if any(w.scope == scope and not w.acked for w in self._materializer_work.values()):
                raise failure("BINDING_MISMATCH")
            self._materializer_work[self._work_key(attempt)] = _Work(
                scope,
                self._materializer_candidates[scope].generation,
                attempt,
                request,
                self._clock(),
                self._notification_state is not None,
            )
