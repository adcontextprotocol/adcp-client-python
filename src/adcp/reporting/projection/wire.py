"""Tier-correct exact views over one captured, caller-private financial history."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from adcp.reporting.ledger.delivery import (
    ReportingMaterializationView,
    adjustment_to_wire,
    materialization_to_wire,
    receipt_to_wire,
    revision_to_wire,
)
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.models import ReportingObligationRecord, ReportingRevisionRecord
from adcp.reporting.ledger.reconciliation_projection import project_reconciliation
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.reporting.ledger.status_projection import ReportingStatusSnapshot
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.capture import private_snapshot
from adcp.reporting.outbox.status import settled_replay
from adcp.reporting.projection.capture import ReportingProjectionInput


@runtime_checkable
class ReportingTierStatusStore(Protocol):
    async def read_tier_status(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
    ) -> dict[str, Any]: ...


def render_tier_status(
    store: Any,
    request: dict[str, Any],
    value: ReportingProjectionInput,
    caller: ReportingDeliveryPrincipal,
    policy: dict[str, Any],
    consumer_status_enabled: bool,
) -> dict[str, Any]:
    if (
        getattr(store, "_projection_read_policy", None) != policy
        or policy["consumer_status_enabled"] != consumer_status_enabled
    ):
        raise LedgerConflictError(
            "STATUS_PROJECTION_UNAVAILABLE", "reporting projection is unavailable"
        )
    return ReportingStatusHandler(
        store,
        consumer_status_enabled=consumer_status_enabled,
        escalation=getattr(store, "_projection_read_escalation", None),
    ).render_snapshot(
        request,
        caller=ReportingStatusCaller(caller.account_id, caller.consumer_id),
        snapshot=settled_replay(private_snapshot(value.core, caller)),
        reconciliation=value.reconciliation,
        revision_ownership=policy["ownership_enabled"],
    )


def exact_revision_evidence(
    core: ReportingStatusSnapshot,
    revision: ReportingRevisionRecord,
    owner: ReportingObligationRecord | None,
    records: tuple[ReportingDeliveryRecord, ...],
    caller: ReportingStatusCaller,
) -> dict[str, Any]:
    if owner is None or owner.account_id != caller.account_id:
        raise LedgerConflictError(
            "LOOKUP_UNAVAILABLE", "no such revision is available to this caller"
        )
    history = tuple(
        r for r in core.revisions if r.reporting_obligation_id == owner.reporting_obligation_id
    )
    project_reconciliation(
        owner, history, core.adjustments, records, consumer_id=caller.consumer_id, as_of=core.as_of
    )
    binding = next(
        (
            r
            for r in records
            if isinstance(r, ReportingDestinationBinding)
            and r.generation_key == owner.generation_key
        ),
        None,
    )
    if binding is None:
        return {}
    attempts = {
        r.reporting_materialization_id: r
        for r in records
        if isinstance(r, ReportingMaterializationAttempt)
        and r.reporting_revision_id == revision.reporting_revision_id
    }
    artifacts = [
        r
        for r in records
        if isinstance(r, ReportingMaterializationRecord)
        and r.reporting_revision_id == revision.reporting_revision_id
    ]
    adjustments = tuple(
        a
        for a in core.adjustments
        if a.adjusts_reporting_revision_id == revision.reporting_revision_id
    )
    adjustment_ids = {a.reporting_adjustment_id for a in adjustments}
    receipts = [
        r
        for r in records
        if isinstance(r, ReportingRevisionReceiptRecord)
        and r.reporting_revision_id == revision.reporting_revision_id
    ]
    adjustment_receipts = [
        r
        for r in records
        if isinstance(r, ReportingAdjustmentReceiptRecord)
        and r.reporting_adjustment_id in adjustment_ids
    ]
    return {
        "revision": revision_to_wire(revision, obligation=owner),
        "adjustments": [adjustment_to_wire(a) for a in adjustments],
        "materializations": [
            materialization_to_wire(
                ReportingMaterializationView(
                    attempts[r.reporting_materialization_id],
                    binding,
                    r,
                    tuple(
                        c
                        for c in records
                        if isinstance(c, ReportingMaterializationCheck)
                        and c.reporting_materialization_id == r.reporting_materialization_id
                    ),
                ),
                obligation=owner,
            )
            for r in artifacts
        ],
        "receipts": [receipt_to_wire(r) for r in receipts],
        "adjustment_receipts": [receipt_to_wire(r) for r in adjustment_receipts],
        "pagination": {
            "total_count": 1
            + len(adjustments)
            + len(artifacts)
            + len(receipts)
            + len(adjustment_receipts),
            "has_more": False,
        },
    }
