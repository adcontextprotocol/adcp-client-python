"""Optional durable seller contracts for future destination writers and receipt handlers.

These stores persist evidence only. They do not resolve credentials, perform
external writes, mount tasks, or advertise Managed Delivery/Reconciled Billing.
The existing Core store protocol and construction contract remain unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from adcp.reporting.ledger._delivery_state import (
    DeliveryContext,
    RecordT,
    adjustment_payload,
    adjustment_sha256,
    change_id,
    decode_record,
    fail,
    iso,
    payload,
    principal,
    replay,
    revision_control_totals,
    totals_to_wire,
    unavailable,
    validate_transition,
)
from adcp.reporting.ledger.delivery_changes import (
    ReportingReconciliationChange,
    ReportingReconciliationCheckpoint,
    ReportingReconciliationCursor,
    ReportingReconciliationFilter,
    ReportingReconciliationPage,
    ReportingReconciliationSnapshotToken,
    change_boundary,
    change_page,
    read_position,
    validate_boundary,
)
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationKey,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingReceiptKey,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.models import (
    ReportingAdjustmentRecord,
    ReportingConfigurationGenerationKey,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import InMemoryReportingLedgerStore, LedgerConflictError


@runtime_checkable
class ReportingDestinationStore(Protocol):
    """Trusted configuration ingestion. Never accept a destination from a receipt body."""

    async def put_destination_binding(
        self, record: ReportingDestinationBinding
    ) -> tuple[ReportingDestinationBinding, bool]: ...

    async def bind_obligation_delivery(
        self, record: ReportingObligationDeliveryRecord
    ) -> tuple[ReportingObligationDeliveryRecord, bool]: ...

    async def get_destination_binding(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        generation_key: ReportingConfigurationGenerationKey,
    ) -> ReportingDestinationBinding | None: ...

    async def get_obligation_delivery(
        self, scope: ReportingDeliveryScope
    ) -> ReportingObligationDeliveryRecord | None: ...


@runtime_checkable
class ReportingMaterializationStore(Protocol):
    async def commit_materialization_attempt(
        self, record: ReportingMaterializationAttempt
    ) -> tuple[ReportingMaterializationAttempt, bool]: ...

    async def commit_materialization(
        self, record: ReportingMaterializationRecord
    ) -> tuple[ReportingMaterializationRecord, bool]: ...

    async def record_materialization_check(
        self, record: ReportingMaterializationCheck
    ) -> tuple[ReportingMaterializationCheck, bool]: ...

    async def get_materialization(
        self, key: ReportingMaterializationKey
    ) -> ReportingMaterializationView | None: ...


@runtime_checkable
class ReportingReceiptStore(Protocol):
    """Transport-derived scope is mandatory. Exact retries precede temporal checks.

    ``received_at`` is assigned by the store; callers retry the immutable input
    or the returned record. Both receipt kinds share the same scoped ID namespace.
    """

    async def record_revision_receipt(
        self, record: ReportingRevisionReceiptRecord
    ) -> tuple[ReportingRevisionReceiptRecord, bool]: ...

    async def record_adjustment_receipt(
        self, record: ReportingAdjustmentReceiptRecord
    ) -> tuple[ReportingAdjustmentReceiptRecord, bool]: ...

    async def get_receipt(self, key: ReportingReceiptKey) -> ReportingReceiptRecord | None: ...


@runtime_checkable
class ReportingReconciliationStore(
    ReportingDestinationStore, ReportingMaterializationStore, ReportingReceiptStore, Protocol
):
    async def read_reconciliation_snapshot(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        boundary: ReportingReconciliationSnapshotToken | None = None,
    ) -> ReportingReconciliationSnapshot: ...


@dataclass(frozen=True, slots=True)
class ReportingMaterializationView:
    attempt: ReportingMaterializationAttempt
    binding: ReportingDestinationBinding
    outcome: ReportingMaterializationRecord | None
    checks: tuple[ReportingMaterializationCheck, ...]

    @property
    def check(self) -> ReportingMaterializationCheck | None:
        return max(self.checks, key=lambda item: item.checked_at, default=None)

    def readable_at(self, at: datetime) -> bool:
        outcome = self.outcome
        check = max(
            (item for item in self.checks if item.checked_at <= at),
            key=lambda item: item.checked_at,
            default=None,
        )
        return bool(
            outcome is not None
            and outcome.status in {"available", "delivered"}
            and outcome.resource is not None
            and outcome.completed_at <= at < outcome.resource.expires_at
            and (check is None or check.state == "readable")
        )

    def to_wire(self) -> dict[str, Any]:
        """An inert projection for future handlers. Never exposes the trusted reference."""
        attempt, binding, outcome = self.attempt, self.binding, self.outcome
        generation = attempt.scope.generation_key
        result: dict[str, Any] = {
            "reporting_materialization_id": attempt.reporting_materialization_id,
            "reporting_revision_id": attempt.reporting_revision_id,
            "reporting_obligation_id": attempt.scope.reporting_obligation_id,
            "delivery_config_id": generation.delivery_config_id,
            "delivery_config_version": generation.delivery_config_version,
            "destination_ref": binding.destination_ref,
            "feed_purpose": binding.feed_purpose,
            "method": binding.method,
            "transport": binding.transport,
            "attempt": attempt.attempt,
            "status": outcome.status if outcome is not None else "pending",
            "created_at": iso(attempt.created_at),
        }
        if outcome is None:
            return result
        if outcome.status == "failed":
            result.update(failed_at=iso(outcome.completed_at), failure_code=outcome.failure_code)
            return result
        resource, verification = outcome.resource, outcome.verification
        assert resource is not None and verification is not None
        descriptor = {
            key: value
            for key, value in asdict(resource).items()
            if value is not None and key != "object_refs"
        }
        descriptor["expires_at"] = iso(resource.expires_at)
        descriptor["reader_compatibility"] = list(resource.reader_compatibility)
        if resource.kind == "manifest":
            descriptor["manifest_version"] = "1.0"
        evidence: dict[str, Any] = {
            "verified_at": iso(verification.verified_at),
            "verification_path": verification.verification_path,
            "verification_profile": verification.verification_profile,
            "row_count": verification.row_count,
            "control_totals": totals_to_wire(verification.control_totals),
        }
        if verification.canonical_content_digest is not None:
            evidence["canonical_content_digest"] = verification.canonical_content_digest.to_wire()
        if verification.physical_checksums:
            evidence["physical_checksums"] = [
                asdict(item) for item in verification.physical_checksums
            ]
        if verification.native_version_ref is not None:
            evidence["native_commit_evidence"] = {
                "native_version_ref": verification.native_version_ref,
                "observed_through": verification.native_observed_through,
            }
        result.update(
            ready_at=iso(outcome.completed_at), resource=descriptor, verification=evidence
        )
        return result


@dataclass(frozen=True, slots=True)
class ReportingReconciliationSnapshot:
    caller: ReportingDeliveryPrincipal
    boundary: ReportingReconciliationSnapshotToken
    records: tuple[ReportingDeliveryRecord, ...]

    def materialization(
        self, key: ReportingMaterializationKey
    ) -> ReportingMaterializationView | None:
        if key.principal != self.caller:
            return None
        attempt = next(
            (
                item
                for item in self.records
                if isinstance(item, ReportingMaterializationAttempt) and item.key == key
            ),
            None,
        )
        if attempt is None:
            return None
        binding = next(
            (
                item
                for item in self.records
                if isinstance(item, ReportingDestinationBinding)
                and item.generation_key == attempt.scope.generation_key
            ),
            None,
        )
        if binding is None:
            unavailable()
        outcome = next(
            (
                item
                for item in self.records
                if isinstance(item, ReportingMaterializationRecord) and item.key == key
            ),
            None,
        )
        return ReportingMaterializationView(
            attempt,
            binding,
            outcome,
            tuple(
                item
                for item in self.records
                if isinstance(item, ReportingMaterializationCheck)
                and item.reporting_materialization_id == key.reporting_materialization_id
            ),
        )

    @property
    def current_receipts(self) -> tuple[ReportingReceiptRecord, ...]:
        receipts = tuple(
            item
            for item in self.records
            if isinstance(item, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord))
        )
        replaced = {item.supersedes_reporting_receipt_id for item in receipts}
        return tuple(item for item in receipts if item.reporting_receipt_id not in replaced)

    @property
    def terminal_acceptances(self) -> tuple[ReportingReceiptKey, ...]:
        return tuple(item.key for item in self.current_receipts if item.status == "accepted")


def _boundary_unavailable(requested: bool) -> NoReturn:
    """Separate a caller's stale/forged boundary from genuine retained damage."""
    if requested:
        raise LedgerConflictError("INVALID_CHECKPOINT", "reconciliation boundary is unavailable")
    fail("REPORTING_HISTORY_CORRUPT")


class _ReconciliationOperations:
    """Typed forwarding shared by the two storage mechanisms; not an adopter hook."""

    async def _commit(self, record: RecordT) -> tuple[RecordT, bool]:
        raise NotImplementedError

    async def put_destination_binding(
        self, record: ReportingDestinationBinding
    ) -> tuple[ReportingDestinationBinding, bool]:
        return await self._commit(record)

    async def get_destination_binding(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        generation_key: ReportingConfigurationGenerationKey,
    ) -> ReportingDestinationBinding | None:
        if caller.account_id != generation_key.account_id:
            return None
        snapshot = await self.read_reconciliation_snapshot(caller=caller)
        return next(
            (
                item
                for item in snapshot.records
                if isinstance(item, ReportingDestinationBinding)
                and item.generation_key == generation_key
            ),
            None,
        )

    async def get_obligation_delivery(
        self, scope: ReportingDeliveryScope
    ) -> ReportingObligationDeliveryRecord | None:
        snapshot = await self.read_reconciliation_snapshot(caller=scope.principal)
        return next(
            (
                item
                for item in snapshot.records
                if isinstance(item, ReportingObligationDeliveryRecord) and item.scope == scope
            ),
            None,
        )

    async def bind_obligation_delivery(
        self, record: ReportingObligationDeliveryRecord
    ) -> tuple[ReportingObligationDeliveryRecord, bool]:
        return await self._commit(record)

    async def commit_materialization_attempt(
        self, record: ReportingMaterializationAttempt
    ) -> tuple[ReportingMaterializationAttempt, bool]:
        return await self._commit(record)

    async def commit_materialization(
        self, record: ReportingMaterializationRecord
    ) -> tuple[ReportingMaterializationRecord, bool]:
        return await self._commit(record)

    async def record_materialization_check(
        self, record: ReportingMaterializationCheck
    ) -> tuple[ReportingMaterializationCheck, bool]:
        return await self._commit(record)

    async def record_revision_receipt(
        self, record: ReportingRevisionReceiptRecord
    ) -> tuple[ReportingRevisionReceiptRecord, bool]:
        return await self._commit(record)

    async def record_adjustment_receipt(
        self, record: ReportingAdjustmentReceiptRecord
    ) -> tuple[ReportingAdjustmentReceiptRecord, bool]:
        return await self._commit(record)

    async def read_reconciliation_snapshot(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        boundary: ReportingReconciliationSnapshotToken | None = None,
    ) -> ReportingReconciliationSnapshot:
        raise NotImplementedError

    async def get_materialization(
        self, key: ReportingMaterializationKey
    ) -> ReportingMaterializationView | None:
        snapshot = await self.read_reconciliation_snapshot(caller=key.principal)
        return snapshot.materialization(key)

    async def get_receipt(self, key: ReportingReceiptKey) -> ReportingReceiptRecord | None:
        snapshot = await self.read_reconciliation_snapshot(caller=key.principal)
        return next(
            (
                item
                for item in snapshot.records
                if isinstance(
                    item, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)
                )
                and item.key == key
            ),
            None,
        )


class InMemoryReportingReconciliationStore(InMemoryReportingLedgerStore, _ReconciliationOperations):
    """Optional reference extension. No destination/receipt services at construction."""

    _delivery_records: list[tuple[int, ReportingDeliveryPrincipal, ReportingDeliveryRecord]]

    async def _commit(self, record: RecordT) -> tuple[RecordT, bool]:
        candidate = cast(RecordT, decode_record(payload(record)))
        async with self._mutation():
            return self._commit_record_unlocked(candidate)

    def _commit_record_unlocked(
        self, record: RecordT, *, notify: bool = True, dirty: bool = True
    ) -> tuple[RecordT, bool]:
        """Caller owns the memory mutation; no lock or callback is acquired here.

        ``notify`` and ``dirty`` are independent: suppressing a readiness event
        must never also drop the projection work that an ordinary public write
        has always produced.
        """
        candidate = decode_record(payload(record))
        who = principal(candidate)
        records = tuple(item.record for item in self._caller_changes(who))
        existing = replay(candidate, records)
        if existing is not None:
            return cast(RecordT, existing), False
        context = self._delivery_context(candidate)
        stored = validate_transition(candidate, records, context, self._clock())
        self._append_reconciliation_change(stored)
        if (notify or dirty) and self._notification_state is not None:
            from adcp.reporting.ledger.notification_events import (
                delivery_dirty,
                materialization_event,
            )

            if notify:
                event = materialization_event(
                    stored,
                    records,
                    context.obligation,
                    context.revision,
                    context.configuration,
                    self._clock(),
                )
                if event is not None:
                    self._record_notification(event)
            if dirty:
                scope, reason, evidence = delivery_dirty(stored, context.obligation)
                self._dirty_status(scope, reason, after=evidence)
        return cast(RecordT, stored), True

    def _append_reconciliation_change(self, record: ReportingDeliveryRecord) -> None:
        who = principal(record)
        retained = self._retained_delivery_records()
        sequence = sum(owner == who for _, owner, _ in retained) + 1
        # One assignment publishes the record, its feed row, and its local head.
        # Core's sequence and change list never participate in this transaction.
        self._delivery_records = [*retained, (sequence, who, record)]

    def _retained_delivery_records(
        self,
    ) -> list[tuple[int, ReportingDeliveryPrincipal, ReportingDeliveryRecord]]:
        # Lazily allocated so construction keeps Core's exact component surface.
        if not hasattr(self, "_delivery_records"):
            self._delivery_records = []
        return self._delivery_records

    def _caller_changes(
        self, caller: ReportingDeliveryPrincipal
    ) -> tuple[ReportingReconciliationChange, ...]:
        retained = [
            item
            for item in self._retained_delivery_records()
            if item[1] == caller or principal(item[2]) == caller
        ]
        if any(
            type(sequence) is not int
            or sequence != ordinal
            or owner != caller
            or principal(record) != caller
            for ordinal, (sequence, owner, record) in enumerate(retained, start=1)
        ):
            fail("REPORTING_HISTORY_CORRUPT")
        return tuple(
            ReportingReconciliationChange(sequence, record) for sequence, _, record in retained
        )

    def _delivery_context(self, record: ReportingDeliveryRecord) -> DeliveryContext:
        if isinstance(record, ReportingDestinationBinding):
            return DeliveryContext(configuration=self._configurations.get(record.generation_key))
        obligation = self._obligations.get(record.scope.reporting_obligation_id)
        revision_id = getattr(record, "reporting_revision_id", None)
        if isinstance(record, ReportingAdjustmentReceiptRecord):
            revision_id = record.adjusts_reporting_revision_id
        revision = self._revisions.get(revision_id) if revision_id is not None else None
        return DeliveryContext(
            configuration=self._configurations.get(record.scope.generation_key),
            obligation=obligation,
            revision=revision,
            adjustment=(
                self._adjustments.get(record.reporting_adjustment_id)
                if isinstance(record, ReportingAdjustmentReceiptRecord)
                else None
            ),
        )

    async def read_reconciliation_snapshot(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        boundary: ReportingReconciliationSnapshotToken | None = None,
    ) -> ReportingReconciliationSnapshot:
        requested = boundary is not None
        async with self._lock:
            changes = self._caller_changes(caller)
            if boundary is None:
                boundary = change_boundary(caller, len(changes), self._clock())
            if (
                type(boundary) is not ReportingReconciliationSnapshotToken
                or boundary.caller != caller
                or boundary.min_sequence != 0
                or boundary.filters != ReportingReconciliationFilter()
            ):
                unavailable()
            validate_boundary(caller, boundary, len(changes))
            if boundary.total_count != boundary.max_sequence:
                # A caller-presented boundary that disagrees with the retained feed
                # is a stale or hand-built token, never evidence that storage is
                # corrupt. Only a boundary this store opened can accuse itself.
                _boundary_unavailable(requested)
            return ReportingReconciliationSnapshot(
                caller,
                boundary,
                tuple(item.record for item in changes if item.sequence <= boundary.max_sequence),
            )

    async def read_reconciliation_changes(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        changes_after: ReportingReconciliationCheckpoint | None = None,
        cursor: ReportingReconciliationCursor | None = None,
        limit: int = 100,
        filters: ReportingReconciliationFilter = ReportingReconciliationFilter(),
    ) -> ReportingReconciliationPage:
        after, boundary, last_key = read_position(caller, changes_after, cursor, limit, filters)
        continued = boundary is not None
        async with self._lock:
            records = self._caller_changes(caller)
            if boundary is None:
                boundary = change_boundary(
                    caller,
                    len(records),
                    self._clock(),
                    after=after,
                    total_count=sum(
                        item.sequence > after and filters.matches(item.record) for item in records
                    ),
                    filters=filters,
                )
            validate_boundary(caller, boundary, len(records))
            if last_key is not None and not any(
                item.sequence == after
                and change_id(item.record) == last_key
                and filters.matches(item.record)
                for item in records
            ):
                raise LedgerConflictError("INVALID_CHECKPOINT", "reconciliation key is unavailable")
            changes = tuple(
                item
                for item in records
                if boundary.min_sequence < item.sequence <= boundary.max_sequence
                and filters.matches(item.record)
            )
            if len(changes) != boundary.total_count:
                _boundary_unavailable(continued)
            return change_page(
                caller,
                boundary,
                after,
                tuple(item for item in changes if item.sequence > after),
                limit,
            )


def materialization_to_wire(
    view: ReportingMaterializationView, *, obligation: ReportingObligationRecord
) -> dict[str, Any]:
    if view.attempt.scope.generation_key != obligation.generation_key or (
        view.attempt.scope.reporting_obligation_id != obligation.reporting_obligation_id
    ):
        unavailable()
    result = view.to_wire()
    result["feed_purpose"] = obligation.feed_purpose
    return result


def receipt_to_wire(record: ReportingReceiptRecord) -> dict[str, Any]:
    result = payload(record)
    result.pop("scope")
    result.pop("kind")
    if isinstance(record, ReportingRevisionReceiptRecord):
        result["reporting_obligation_id"] = record.scope.reporting_obligation_id
        result["observed_control_totals"] = totals_to_wire(record.observed_control_totals)
        if record.observed_canonical_content_digest is not None:
            result["observed_canonical_content_digest"] = (
                record.observed_canonical_content_digest.to_wire()
            )
    return {
        key: value
        for key, value in result.items()
        if value is not None and not (key == "rejection_codes" and not value)
    }


def adjustment_to_wire(adjustment: ReportingAdjustmentRecord) -> dict[str, Any]:
    """Complete immutable adjustment evidence, including reason_detail in its digest."""
    return {
        **adjustment_payload(adjustment),
        "canonical_adjustment_sha256": adjustment_sha256(adjustment),
    }


def revision_to_wire(
    revision: ReportingRevisionRecord, *, obligation: ReportingObligationRecord
) -> dict[str, Any]:
    """Opt-in evidence projection; Core's mounted handler remains unchanged."""
    from adcp.reporting.ledger.status import _revision_to_wire

    if (
        revision.account_id != obligation.account_id
        or revision.reporting_obligation_id != obligation.reporting_obligation_id
    ):
        unavailable()
    result = _revision_to_wire(revision, obligation)
    result["control_totals"] = totals_to_wire(revision_control_totals(revision, obligation))
    if revision.canonical_content_digest is not None:
        result["canonical_content_digest"] = revision.canonical_content_digest.to_wire()
    return result
