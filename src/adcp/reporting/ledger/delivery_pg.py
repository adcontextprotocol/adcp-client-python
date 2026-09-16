"""PostgreSQL reconciliation extension, sharing the Core ledger transaction/feed."""

from __future__ import annotations

from typing import Any, cast

from adcp.reporting.ledger._delivery_state import (
    DeliveryContext,
    RecordT,
    change_id,
    decode_record,
    fail,
    fingerprint,
    payload,
    principal,
    receipt_chain,
    record_identity,
    replay,
    unavailable,
    validate_transition,
)
from adcp.reporting.ledger.delivery import (
    ReportingReconciliationSnapshot,
    _ReconciliationOperations,
)
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDeliveryRecord,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingReceiptRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.models import LedgerSnapshot
from adcp.reporting.ledger.pg import (
    _ADJUSTMENT_COLUMNS,
    _OBLIGATION_COLUMNS,
    _REVISION_COLUMNS,
    PgReportingLedgerStore,
    _adjustment_from_row,
    _configuration_from_row,
    _json,
    _obligation_from_row,
    _revision_from_row,
)


class PgReportingReconciliationStore(PgReportingLedgerStore, _ReconciliationOperations):
    """Evidence and receipt heads commit with the feed, including autocommit pools.

    All state decisions run under the same per-account transaction lock as Core
    writes. Receipt replacement additionally uses a conditional head update;
    accepted heads and immutable evidence are protected by database triggers.
    """

    async def _commit(self, record: RecordT) -> tuple[RecordT, bool]:
        candidate = decode_record(payload(record))
        who = principal(candidate)
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._lock_account(connection, who.account_id)
                records = await self._records(connection, who)
                existing = replay(candidate, records)
                if existing is not None:
                    return cast(RecordT, existing), False
                context = await self._delivery_context(connection, candidate)
                if self._clock is not None:
                    now = self._clock()
                else:
                    time_row = await (
                        await connection.execute("SELECT clock_timestamp()")
                    ).fetchone()
                    assert time_row is not None
                    now = time_row[0]
                stored = validate_transition(candidate, records, context, now)
                await self._insert(connection, stored)
                if isinstance(
                    stored, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)
                ):
                    await self._advance_receipt(connection, stored)
                await self._append_change(
                    connection, who.account_id, stored.kind, change_id(stored)
                )
                return cast(RecordT, stored), True

    async def _records(
        self, connection: Any, who: ReportingDeliveryPrincipal, maximum: int | None = None
    ) -> tuple[ReportingDeliveryRecord, ...]:
        rows = await (
            await connection.execute(
                "SELECT r.payload, r.content_sha256, c.seq, r.namespace, r.record_id,"
                " r.record_kind, r.change_id FROM reporting_reconciliation_records r"
                " LEFT JOIN reporting_ledger_changes c ON c.account_id = r.account_id"
                " AND c.record_kind = r.record_kind AND c.record_id = r.change_id"
                " WHERE r.account_id = %s AND r.consumer_id = %s"
                " AND (c.seq IS NULL OR %s::bigint IS NULL OR c.seq <= %s::bigint) ORDER BY c.seq",
                (who.account_id, who.consumer_id, maximum, maximum),
            )
        ).fetchall()
        records = tuple(decode_record(row[0]) for row in rows)
        if any(
            principal(record) != who
            or fingerprint(record) != row[1]
            or row[2] is None
            or record_identity(record) != (row[3], row[4])
            or record.kind != row[5]
            or change_id(record) != row[6]
            for record, row in zip(records, rows)
        ):
            fail("REPORTING_HISTORY_CORRUPT")
        return records

    async def _delivery_context(
        self, connection: Any, record: ReportingDeliveryRecord
    ) -> DeliveryContext:
        who = principal(record)
        if isinstance(record, ReportingDestinationBinding):
            generation = record.generation_key
            row = await (
                await connection.execute(
                    "SELECT delivery_config_id, delivery_config_version, account_id,"
                    " report_definition_id, reporting_profile, feed_purpose, required_finality,"
                    " account_timezone, schedule, media_buy_ids, activated_at, deactivated_at,"
                    " automated_recovery_seconds, status_retention_days, definition,"
                    " authoritative_party"
                    " FROM reporting_configurations WHERE account_id = %s"
                    " AND delivery_config_id = %s AND delivery_config_version = %s",
                    (
                        who.account_id,
                        generation.delivery_config_id,
                        generation.delivery_config_version,
                    ),
                )
            ).fetchone()
            return DeliveryContext(configuration=_configuration_from_row(row) if row else None)
        obligation_row = await (
            await connection.execute(
                f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # noqa: S608  # nosec B608
                " WHERE account_id = %s AND reporting_obligation_id = %s",
                (who.account_id, record.scope.reporting_obligation_id),
            )
        ).fetchone()
        revision_id = getattr(record, "reporting_revision_id", None)
        if isinstance(record, ReportingAdjustmentReceiptRecord):
            revision_id = record.adjusts_reporting_revision_id
        revision_row = await (
            await connection.execute(
                f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # noqa: S608  # nosec B608
                " WHERE account_id = %s AND reporting_revision_id = %s",
                (who.account_id, revision_id),
            )
        ).fetchone()
        adjustment_row = None
        revision_obligation_row = None
        if revision_row is not None:
            revision_obligation_row = await (
                await connection.execute(
                    f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_obligation_id = %s",
                    (who.account_id, revision_row[2]),
                )
            ).fetchone()
        if isinstance(record, ReportingAdjustmentReceiptRecord):
            adjustment_row = await (
                await connection.execute(
                    f"SELECT {_ADJUSTMENT_COLUMNS} FROM reporting_adjustments"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_adjustment_id = %s",
                    (who.account_id, record.reporting_adjustment_id),
                )
            ).fetchone()
        return DeliveryContext(
            obligation=_obligation_from_row(obligation_row) if obligation_row else None,
            revision=_revision_from_row(revision_row) if revision_row else None,
            revision_obligation=(
                _obligation_from_row(revision_obligation_row) if revision_obligation_row else None
            ),
            adjustment=_adjustment_from_row(adjustment_row) if adjustment_row else None,
        )

    async def _insert(self, connection: Any, record: ReportingDeliveryRecord) -> None:
        who = principal(record)
        namespace, record_id = record_identity(record)
        generation = (
            record.generation_key
            if isinstance(record, ReportingDestinationBinding)
            else record.scope.generation_key
        )
        receipt = (
            record
            if isinstance(
                record, (ReportingRevisionReceiptRecord, ReportingAdjustmentReceiptRecord)
            )
            else None
        )
        await connection.execute(
            "INSERT INTO reporting_reconciliation_records"
            " (account_id, consumer_id, namespace, record_id, record_kind, delivery_config_id,"
            " delivery_config_version, reporting_obligation_id, reporting_revision_id,"
            " reporting_materialization_id, reporting_adjustment_id, attempt_number,"
            " receipt_chain_key, receipt_status, supersedes_receipt_id, payload,"
            " content_sha256, change_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s::jsonb, %s, %s)",
            (
                who.account_id,
                who.consumer_id,
                namespace,
                record_id,
                record.kind,
                generation.delivery_config_id,
                generation.delivery_config_version,
                (
                    None
                    if isinstance(record, ReportingDestinationBinding)
                    else record.scope.reporting_obligation_id
                ),
                (
                    record.adjusts_reporting_revision_id
                    if isinstance(record, ReportingAdjustmentReceiptRecord)
                    else getattr(record, "reporting_revision_id", None)
                ),
                getattr(record, "reporting_materialization_id", None),
                (
                    record.reporting_adjustment_id
                    if isinstance(record, ReportingAdjustmentReceiptRecord)
                    else None
                ),
                record.attempt if isinstance(record, ReportingMaterializationAttempt) else None,
                receipt_chain(receipt) if receipt is not None else None,
                receipt.status if receipt is not None else None,
                receipt.supersedes_reporting_receipt_id if receipt is not None else None,
                _json(payload(record)),
                fingerprint(record),
                change_id(record),
            ),
        )

    async def _advance_receipt(self, connection: Any, record: ReportingReceiptRecord) -> None:
        who = principal(record)
        chain = receipt_chain(record)
        if record.supersedes_reporting_receipt_id is None:
            cursor = await connection.execute(
                "INSERT INTO reporting_receipt_heads"
                " (account_id, consumer_id, chain_key, receipt_id, receipt_status)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (account_id, consumer_id, chain_key) DO NOTHING",
                (
                    who.account_id,
                    who.consumer_id,
                    chain,
                    record.reporting_receipt_id,
                    record.status,
                ),
            )
        else:
            cursor = await connection.execute(
                "UPDATE reporting_receipt_heads SET receipt_id = %s, receipt_status = %s,"
                " supersedes_receipt_id = %s"
                " WHERE account_id = %s AND consumer_id = %s AND chain_key = %s"
                " AND receipt_id = %s AND receipt_status = 'rejected'",
                (
                    record.reporting_receipt_id,
                    record.status,
                    record.supersedes_reporting_receipt_id,
                    who.account_id,
                    who.consumer_id,
                    chain,
                    record.supersedes_reporting_receipt_id,
                ),
            )
        if cursor.rowcount != 1:
            unavailable()

    async def read_reconciliation_snapshot(
        self, *, caller: ReportingDeliveryPrincipal, boundary: LedgerSnapshot | None = None
    ) -> ReportingReconciliationSnapshot:
        if boundary is None:
            boundary = await self.open_snapshot(
                account_id=caller.account_id, filters_fingerprint=caller.consumer_id
            )
        if boundary.account_id != caller.account_id:
            unavailable()
        async with self._pool.connection() as connection:
            records = await self._records(connection, caller, boundary.max_sequence)
        return ReportingReconciliationSnapshot(caller, boundary, records)
