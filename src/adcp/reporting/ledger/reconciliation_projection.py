"""Pure tier evidence over one captured private record history.

Selection precedes artifact/receipt inspection. Acceptance is validated against
its immutable admission prefix; later artifacts and checks cannot revoke it.
Current readability is a separate condition for delivery health.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger._delivery_state import (
    _verify_materialization,
    _verify_receipt,
    adjustment_sha256,
    current_receipt,
    fingerprint,
    iso,
    principal,
    record_identity,
)
from adcp.reporting.ledger.delivery import ReportingMaterializationView
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.health import issue_id_for
from adcp.reporting.ledger.models import (
    ReportingAdjustmentRecord,
    ReportingIssue,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.revision_selection import select_reporting_revision


@dataclass(frozen=True)
class ReconciliationProjection:
    wire_json: bytes
    evidence_json: bytes
    issues: tuple[ReportingIssue, ...] = ()
    satisfied: bool = True
    deadlines: tuple[datetime, ...] = ()

    @property
    def wire(self) -> dict[str, Any]:
        return dict(json.loads(self.wire_json))


def project_reconciliation(
    obligation: ReportingObligationRecord,
    revisions: tuple[ReportingRevisionRecord, ...],
    adjustments: tuple[ReportingAdjustmentRecord, ...],
    records: tuple[ReportingDeliveryRecord, ...],
    *,
    consumer_id: str | None,
    as_of: datetime,
) -> ReconciliationProjection:
    """All inputs belong to the captured boundary; no live store is consulted."""
    selection = select_reporting_revision(
        revisions,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        required_finality=obligation.required_finality,
    )
    revision = selection.revision if selection.kind == "selected" else None
    owned_ids = {r.reporting_revision_id for r in revisions}
    owned_adjustments = tuple(
        a for a in adjustments if a.adjusts_reporting_revision_id in owned_ids
    )
    caller = ReportingDeliveryPrincipal(obligation.account_id, consumer_id) if consumer_id else None
    history = tuple(r for r in records if caller is not None and principal(r) == caller)
    scoped = tuple(
        r
        for r in history
        if (
            r.generation_key == obligation.generation_key
            if isinstance(r, ReportingDestinationBinding)
            else r.scope.reporting_obligation_id == obligation.reporting_obligation_id
        )
    )
    if len({record_identity(r) for r in scoped}) != len(scoped):
        raise LedgerConflictError("HISTORY_UNAVAILABLE", "reconciliation history is inconsistent")
    evidence: dict[str, Any] = {
        "reporting_obligation_id": obligation.reporting_obligation_id,
        "consumer_id": consumer_id,
        "selected": revision.reporting_revision_id if revision else None,
        "records": [fingerprint(r) for r in scoped],
        "adjustments": [
            [
                a.reporting_adjustment_id,
                hashlib.sha256(
                    json.dumps(asdict(a), default=iso, sort_keys=True).encode()
                ).hexdigest(),
            ]
            for a in owned_adjustments
        ],
    }
    wire: dict[str, Any] = {"adjustment_count": len(owned_adjustments)}
    bindings = [r for r in scoped if isinstance(r, ReportingDestinationBinding)]
    if len(bindings) > 1:
        raise LedgerConflictError("HISTORY_UNAVAILABLE", "reconciliation binding is inconsistent")
    if not bindings:
        if scoped:
            raise LedgerConflictError(
                "HISTORY_UNAVAILABLE", "reconciliation binding is unavailable"
            )
        return ReconciliationProjection(
            canonical_json_utf8_v1(wire), canonical_json_utf8_v1(evidence)
        )
    binding = bindings[0]
    deliveries = [r for r in scoped if isinstance(r, ReportingObligationDeliveryRecord)]
    if len(deliveries) > 1:
        raise LedgerConflictError("HISTORY_UNAVAILABLE", "reconciliation delivery is inconsistent")
    delivery = deliveries[0] if deliveries else None
    attempts = {
        r.reporting_materialization_id: r
        for r in scoped
        if isinstance(r, ReportingMaterializationAttempt)
    }
    outcomes = tuple(r for r in scoped if isinstance(r, ReportingMaterializationRecord))
    successes = tuple(r for r in outcomes if r.status in {"available", "delivered"})
    receipts = tuple(r for r in scoped if isinstance(r, ReportingRevisionReceiptRecord))
    adjustment_receipts = tuple(
        r for r in scoped if isinstance(r, ReportingAdjustmentReceiptRecord)
    )
    checks = tuple(r for r in scoped if isinstance(r, ReportingMaterializationCheck))
    by_revision = {r.reporting_revision_id: r for r in revisions}
    by_adjustment = {a.reporting_adjustment_id: a for a in owned_adjustments}
    readable: dict[str, bool] = {}
    deadlines: set[datetime] = set()
    for outcome in outcomes:
        attempt, target = attempts.get(outcome.reporting_materialization_id), by_revision.get(
            outcome.reporting_revision_id
        )
        if (
            attempt is None
            or target is None
            or delivery is None
            or attempt.scope != outcome.scope
            or attempt.reporting_revision_id != outcome.reporting_revision_id
        ):
            raise LedgerConflictError(
                "HISTORY_UNAVAILABLE", "materialization dependencies are unavailable"
            )
        if outcome.status != "failed":
            _verify_materialization(outcome, binding, delivery, target, obligation)
        view = ReportingMaterializationView(
            attempt,
            binding,
            outcome,
            tuple(
                c
                for c in checks
                if c.reporting_materialization_id == outcome.reporting_materialization_id
            ),
        )
        readable[outcome.reporting_materialization_id] = view.readable_at(as_of) and target.readable
        if outcome.resource is not None:
            deadlines.add(outcome.resource.expires_at)
    leaves = {}
    for index, record in enumerate(history):
        if record not in (*receipts, *adjustment_receipts):
            continue
        if isinstance(record, ReportingRevisionReceiptRecord):
            target = by_revision.get(record.reporting_revision_id)
            if target is None:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "receipt revision is unavailable")
            _verify_receipt(record, history[: index + 1], target)
            leaves[("revision", record.reporting_revision_id)] = current_receipt(history, record)
        elif isinstance(record, ReportingAdjustmentReceiptRecord):
            adjustment = by_adjustment.get(record.reporting_adjustment_id)
            if (
                adjustment is None
                or adjustment.adjusts_reporting_revision_id != record.adjusts_reporting_revision_id
                or (
                    record.status == "accepted"
                    and record.observed_adjustment_sha256 != adjustment_sha256(adjustment)
                )
            ):
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "adjustment receipt is inconsistent"
                )
            leaves[("adjustment", record.reporting_adjustment_id)] = current_receipt(
                history, record
            )
    selected_successes = tuple(
        r
        for r in successes
        if revision is not None and r.reporting_revision_id == revision.reporting_revision_id
    )
    current = max(
        selected_successes,
        key=lambda r: attempts[r.reporting_materialization_id].attempt,
        default=None,
    )
    artifact_readable = bool(current and readable[current.reporting_materialization_id])
    evidence["readable"] = readable
    # Counts describe immutable successful outcomes, not today's health checks.
    wire.update(
        destination_ref=binding.destination_ref,
        reconciliation_mode=binding.reconciliation_mode,
        materialization_count=len(outcomes),
        successful_materialization_count=len(successes),
    )
    retained = [o.resource.expires_at for o in successes if o.resource is not None]
    if retained:
        wire["resource_retained_until"] = iso(min(retained))
    requires_receipt = binding.reconciliation_mode == "consumer_receipt"
    selected_receipt = (
        leaves.get(("revision", revision.reporting_revision_id)) if revision else None
    )
    accepted = bool(selected_receipt and selected_receipt.status == "accepted")
    applicable = tuple(
        a
        for a in owned_adjustments
        if revision is not None
        and a.adjusts_reporting_revision_id == revision.reporting_revision_id
    )
    pending_adjustments = tuple(
        a
        for a in applicable
        if (
            (leaf := leaves.get(("adjustment", a.reporting_adjustment_id))) is None
            or leaf.status != "accepted"
        )
    )
    if requires_receipt:
        wire.update(
            receipt_count=len(receipts),
            accepted_receipt_count=sum(r.status == "accepted" for r in receipts),
            adjustment_receipt_count=len(adjustment_receipts),
            accepted_adjustment_receipt_count=sum(
                r.status == "accepted" for r in adjustment_receipts
            ),
            pending_adjustment_count=len(pending_adjustments),
        )
        rejected = bool(selected_receipt and selected_receipt.status == "rejected") or any(
            (leaf := leaves.get(("adjustment", a.reporting_adjustment_id))) is not None
            and leaf.status == "rejected"
            for a in applicable
        )
        wire["reconciliation_status"] = (
            "rejected"
            if rejected
            else "accepted" if accepted and not pending_adjustments else "pending"
        )
    else:
        wire["reconciliation_status"] = "not_required"
    issues: list[ReportingIssue] = []

    def issue(code: str, *, buyer: bool = False, immediate: bool = False) -> None:
        if not immediate and as_of < obligation.period.expected_at:
            return
        severity: Literal["delayed", "action_required"] = (
            "action_required"
            if immediate or as_of >= obligation.automated_recovery_deadline_at
            else "delayed"
        )
        # An immutable evidence change differentiates a recurring artifact/receipt
        # failure while the ordered checkpoint retains its monotonic generation.
        occurrence = hashlib.sha256(canonical_json_utf8_v1(evidence)).hexdigest()
        issues.append(
            ReportingIssue(
                issue_id_for(
                    "tier-status-v2",
                    obligation.account_id,
                    consumer_id,
                    obligation.reporting_obligation_id,
                    code,
                    occurrence,
                ),
                code,
                severity,
                "buyer" if buyer else "seller",
                "contact_buyer" if buyer else "contact_seller",
                reporting_obligation_id=obligation.reporting_obligation_id,
                delivery_config_id=obligation.delivery_config_id,
                delivery_config_version=obligation.delivery_config_version,
                feed_purpose=obligation.feed_purpose,
                media_buy_ids=obligation.media_buy_ids,
                expected_at=obligation.period.expected_at,
            )
        )

    if revision is not None:
        if not artifact_readable:
            expired = (
                current is not None
                and current.resource is not None
                and current.resource.expires_at <= as_of
            )
            issue(
                "RESOURCE_EXPIRED" if expired else "DELIVERY_FAILED", immediate=current is not None
            )
        if requires_receipt:
            if revision.finality != "official" or revision.canonical_content_digest is None:
                issue("HISTORY_UNAVAILABLE", immediate=True)
            if not accepted:
                issue(
                    "RECEIPT_REJECTED" if selected_receipt else "RECEIPT_REQUIRED",
                    buyer=True,
                    immediate=True,
                )
            for adjustment in pending_adjustments:
                leaf = leaves.get(("adjustment", adjustment.reporting_adjustment_id))
                issue(
                    "ADJUSTMENT_RECEIPT_REJECTED" if leaf else "ADJUSTMENT_RECEIPT_REQUIRED",
                    buyer=True,
                    immediate=True,
                )
    satisfied = bool(
        revision is not None
        and artifact_readable
        and (
            not requires_receipt
            or (
                revision.finality == "official"
                and revision.canonical_content_digest is not None
                and accepted
                and not pending_adjustments
            )
        )
    )
    # This bound is independent of transient corruption/readability; never
    # claim the provider's retention beyond the frozen generation commitment.
    if "resource_retained_until" in wire:
        assert delivery is not None
        wire["resource_retained_until"] = iso(min(delivery.resource_retained_until, *retained))
    return ReconciliationProjection(
        canonical_json_utf8_v1(wire),
        canonical_json_utf8_v1(evidence),
        tuple(issues),
        satisfied,
        tuple(sorted(deadlines)),
    )
