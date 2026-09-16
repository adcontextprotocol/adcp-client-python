"""Pure event mappings, invoked only by an opted-in store's transaction."""

from __future__ import annotations

from datetime import datetime

from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.ledger.models import (
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.notification_models import (
    AdjustmentPublished,
    DirtyReason,
    MaterializationReady,
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusEvidence,
    ReportingStatusScope,
    RevisionPublished,
    new_event,
)


def revision_event(revision: ReportingRevisionRecord, at: datetime) -> ReportingDomainEvent:
    return new_event(
        revision.account_id,
        RevisionPublished(
            revision.reporting_revision_id,
            revision.finality,
            revision.supersedes_reporting_revision_id,
        ),
        at,
    )


def adjustment_event(adjustment: ReportingAdjustmentRecord, at: datetime) -> ReportingDomainEvent:
    return new_event(
        adjustment.account_id,
        AdjustmentPublished(
            adjustment.reporting_adjustment_id,
            adjustment.adjusts_reporting_revision_id,
        ),
        at,
    )


def materialization_event(
    record: ReportingDeliveryRecord,
    records: tuple[ReportingDeliveryRecord, ...],
    obligation: ReportingObligationRecord | None,
    revision: ReportingRevisionRecord | None,
    configuration: ReportingConfiguration | None,
    at: datetime,
) -> ReportingDomainEvent | None:
    """Call after the shared reconciliation validator verified the exact graph.

    Core has no obligation-delivery binding or Managed destination. Neither an
    advertised capability, a profile label, nor a subscriber request can create
    this structural evidence. No rows/resources/provider metadata are copied.
    """
    if not isinstance(record, ReportingMaterializationRecord) or record.status == "failed":
        return None
    binding = next(
        (
            item
            for item in records
            if isinstance(item, ReportingDestinationBinding)
            and item.generation_key == record.scope.generation_key
            and item.consumer_id == record.scope.consumer_id
        ),
        None,
    )
    frozen = next(
        (
            item
            for item in records
            if isinstance(item, ReportingObligationDeliveryRecord) and item.scope == record.scope
        ),
        None,
    )
    if (
        binding is None
        or frozen is None
        or revision is None
        or obligation is None
        or configuration is None
        or configuration.generation_key != record.scope.generation_key
        or record.verification is None
        or record.resource is None
        or record.scope.reporting_obligation_id != obligation.reporting_obligation_id
        or record.scope.generation_key != obligation.generation_key
        or revision.account_id != obligation.account_id
        or revision.reporting_obligation_id != obligation.reporting_obligation_id
        or revision.reporting_revision_id != record.reporting_revision_id
    ):
        raise ReportingNotificationError("core_delivery_ready_forbidden")
    return new_event(
        obligation.account_id,
        MaterializationReady(
            generation_key=record.scope.generation_key,
            consumer_id=record.scope.consumer_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            destination_ref=binding.destination_ref,
            method=binding.method,
            reporting_revision_id=record.reporting_revision_id,
            reporting_materialization_id=record.reporting_materialization_id,
            readiness=record.status,
            finality=revision.finality,
            data_through=revision.data_through,
            feed_purpose=binding.feed_purpose,
        ),
        at,
    )


def delivery_dirty(
    record: ReportingDeliveryRecord, obligation: ReportingObligationRecord | None
) -> tuple[ReportingStatusScope, DirtyReason, ReportingStatusEvidence]:
    from adcp.reporting.ledger._delivery_state import change_id

    if isinstance(record, ReportingDestinationBinding):
        scope = ReportingStatusScope(
            record.generation_key.account_id,
            record.generation_key,
            consumer_id=record.consumer_id,
            feed_purpose=record.feed_purpose,
        )
        reason: DirtyReason = "destination"
    else:
        if obligation is None:
            raise ReportingNotificationError("invalid_status_scope")
        scope = ReportingStatusScope.for_obligation(obligation, record.scope.consumer_id)
        reason = "receipt" if record.kind.endswith("receipt") else "materialization"
    return scope, reason, ReportingStatusEvidence(record.kind, change_id(record))
