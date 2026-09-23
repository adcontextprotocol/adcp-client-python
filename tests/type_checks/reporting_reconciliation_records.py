"""Adopters can replace each durable seam without adding dependencies to Core."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    InMemoryReportingReconciliationStore,
    PgReportingReconciliationStore,
    ReportingCanonicalDigest,
    ReportingConfigurationGenerationKey,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingDestinationStore,
    ReportingLedgerStore,
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingMaterializationStore,
    ReportingMaterializationView,
    ReportingObligationDeliveryRecord,
    ReportingReceiptKey,
    ReportingReceiptRecord,
    ReportingReceiptStore,
    ReportingReconciliationCheckpoint,
    ReportingReconciliationCursor,
    ReportingReconciliationFeedStore,
    ReportingReconciliationPage,
    ReportingReconciliationSnapshotToken,
    ReportingReconciliationStore,
    ReportingRevisionReceiptRecord,
    ReportingRevisionRecord,
    revision_content_sha256,
)


def core_only() -> ReportingLedgerStore:
    return InMemoryReportingLedgerStore()


def reference() -> ReportingReconciliationStore:
    return InMemoryReportingReconciliationStore()


def persistent(pool: AsyncConnectionPool) -> ReportingReconciliationStore:
    return PgReportingReconciliationStore(pool=pool)


def incremental(pool: AsyncConnectionPool) -> ReportingReconciliationFeedStore:
    return PgReportingReconciliationStore(pool=pool)


async def incremental_read(
    store: ReportingReconciliationFeedStore,
    caller: ReportingDeliveryPrincipal,
    checkpoint: ReportingReconciliationCheckpoint | None,
) -> ReportingReconciliationPage:
    return await store.read_reconciliation_changes(
        caller=caller, changes_after=checkpoint, limit=50
    )


async def continue_incremental_read(
    store: ReportingReconciliationFeedStore, page: ReportingReconciliationPage
) -> ReportingReconciliationPage:
    boundary: ReportingReconciliationSnapshotToken = page.boundary
    if page.has_more:
        assert page.cursor is not None
        cursor: ReportingReconciliationCursor = page.cursor
        return await store.read_reconciliation_changes(
            caller=page.caller, cursor=cursor, filters=boundary.filters
        )
    assert page.changes_checkpoint is not None
    return await store.read_reconciliation_changes(
        caller=page.caller, changes_after=page.changes_checkpoint, filters=boundary.filters
    )


def trusted_publisher_evidence(
    revision: ReportingRevisionRecord,
    digest: ReportingCanonicalDigest,
    expected_totals: tuple[ReportingControlTotalRecord, ...],
    rows: Sequence[dict[str, Any]],
) -> ReportingRevisionRecord:
    return replace(
        revision,
        canonical_content_digest=digest,
        managed_control_totals=expected_totals,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=revision.row_count,
            control_totals=revision.control_totals,
            reporting_rows=rows,
            control_total_evidence=expected_totals,
        ),
    )


async def trusted_configuration_ingest(
    store: ReportingDestinationStore,
    *,
    generation: ReportingConfigurationGenerationKey,
    consumer_id: str,
    obligation_id: str,
    retained_until: datetime,
) -> None:
    now = datetime.now(timezone.utc)
    binding = ReportingDestinationBinding(
        generation_key=generation,
        consumer_id=consumer_id,
        destination_ref="destination-generation-1",
        trusted_binding_ref="trusted-binding-1",
        method="warehouse_materialization",
        transport="warehouse",
        verification_profile="canonical_digest",
        reconciliation_mode="consumer_receipt",
        feed_purpose="billing",
        resource_retention_days=400,
        created_at=now,
        success_status="delivered",
    )
    stored_binding, recorded = await store.put_destination_binding(binding)
    scope = ReportingDeliveryScope(stored_binding.generation_key, consumer_id, obligation_id)
    delivery = ReportingObligationDeliveryRecord(scope, "EUR", retained_until, now)
    await store.bind_obligation_delivery(delivery)
    assert isinstance(recorded, bool)


async def destination_writer_completion(
    store: ReportingMaterializationStore,
    attempt: ReportingMaterializationAttempt,
    verified_result: ReportingMaterializationRecord,
) -> ReportingMaterializationView | None:
    # A writer resolves credentials through its trusted binding outside these
    # records. Only its immutable attempt and verified public evidence cross here.
    await store.commit_materialization_attempt(attempt)
    await store.commit_materialization(verified_result)
    return await store.get_materialization(attempt.key)


async def receipt_handler_storage(
    store: ReportingReceiptStore, authenticated_receipt: ReportingRevisionReceiptRecord
) -> tuple[ReportingReceiptKey, bool]:
    stored, recorded = await store.record_revision_receipt(authenticated_receipt)
    total: ReportingControlTotalRecord = stored.observed_control_totals[0]
    unit: str | None = total.unit
    assert unit is None or isinstance(unit, str)
    return stored.key, recorded


async def retained_receipt(
    store: ReportingReceiptStore, key: ReportingReceiptKey
) -> ReportingReceiptRecord | None:
    return await store.get_receipt(key)
