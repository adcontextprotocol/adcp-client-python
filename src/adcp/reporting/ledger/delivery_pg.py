"""PostgreSQL reconciliation evidence with a separate, principal-qualified feed."""

from __future__ import annotations

from datetime import datetime
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
    record_identity,
    replay,
    storage_identity,
    unavailable,
    validate_transition,
)
from adcp.reporting.ledger.delivery import (
    ReportingReconciliationSnapshot,
    _boundary_unavailable,
    _ReconciliationOperations,
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
    ReportingDestinationBinding,
)
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
from adcp.reporting.ledger.store import LedgerConflictError

_IDENTITY_COLUMNS = (
    "account_id",
    "consumer_id",
    "namespace",
    "record_id",
    "record_kind",
    "delivery_config_id",
    "delivery_config_version",
    "reporting_obligation_id",
    "reporting_revision_id",
    "reporting_materialization_id",
    "reporting_adjustment_id",
    "attempt_number",
    "receipt_chain_key",
    "receipt_status",
    "supersedes_receipt_id",
)
_SELECT_IDENTITY = ", ".join("r." + column for column in _IDENTITY_COLUMNS)
_FEED_JOIN = (
    "r.account_id = c.account_id AND r.consumer_id = c.consumer_id"
    " AND r.namespace = c.namespace AND r.record_id = c.record_id"
    " AND r.record_kind = c.record_kind AND r.change_id = c.change_id"
    " AND r.content_sha256 = c.content_sha256"
)
_FEED_FILTER = (
    "(%s::text[] IS NULL OR c.record_kind = ANY(%s))"
    " AND (%s::text IS NULL OR r.reporting_obligation_id = %s)"
)


def _filter_params(filters: ReportingReconciliationFilter) -> tuple[Any, ...]:
    kinds = list(filters.record_kinds) or None
    return kinds, kinds, filters.reporting_obligation_id, filters.reporting_obligation_id


class PgReportingReconciliationStore(PgReportingLedgerStore, _ReconciliationOperations):
    """Evidence and receipt heads commit with the feed, including autocommit pools.

    All state decisions run under the same per-account transaction lock as Core
    writes. Receipt replacement additionally uses a conditional head update;
    accepted heads and immutable evidence are protected by database triggers.
    """

    async def _commit(self, record: RecordT) -> tuple[RecordT, bool]:
        try:
            return await self._commit_record(record)
        except Exception as error:
            # A concurrent/raw SQL writer must not turn a constraint detail
            # (which may include an entire row) into a provider-payload echo.
            if not str(getattr(error, "sqlstate", "")).startswith("23"):
                raise
        unavailable()

    async def _commit_record(self, record: RecordT) -> tuple[RecordT, bool]:
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
                await self._append_reconciliation_change(connection, stored)
                if self._notifications_enabled:
                    from adcp.reporting.ledger.notification_events import (
                        delivery_dirty,
                        materialization_event,
                    )

                    event = materialization_event(
                        stored,
                        records,
                        context.obligation,
                        context.revision,
                        context.configuration,
                        now,
                    )
                    if event is not None:
                        await self._record_notification(connection, event)
                    scope, reason, evidence = delivery_dirty(stored, context.obligation)
                    await self._dirty_status(connection, scope, reason, after=evidence)
                return cast(RecordT, stored), True

    async def _append_reconciliation_change(
        self, connection: Any, record: ReportingDeliveryRecord
    ) -> None:
        who = principal(record)
        row = await (
            await connection.execute(
                "INSERT INTO reporting_reconciliation_heads (account_id, consumer_id, max_sequence)"
                " VALUES (%s, %s, 1) ON CONFLICT (account_id, consumer_id) DO UPDATE"
                " SET max_sequence = reporting_reconciliation_heads.max_sequence + 1"
                " RETURNING max_sequence",
                (who.account_id, who.consumer_id),
            )
        ).fetchone()
        assert row is not None
        await connection.execute(
            "INSERT INTO reporting_reconciliation_changes"
            " (account_id, consumer_id, seq, namespace, record_id, record_kind,"
            " change_id, content_sha256, committed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, clock_timestamp()))",
            (
                who.account_id,
                who.consumer_id,
                row[0],
                *record_identity(record),
                record.kind,
                change_id(record),
                fingerprint(record),
                self._clock() if self._clock is not None else None,
            ),
        )

    async def _validate_feed(
        self, connection: Any, who: ReportingDeliveryPrincipal
    ) -> tuple[int, datetime]:
        # Scope both sides before the join, count, or boundary. A missing/moved
        # record or feed row must fail, never disappear through an inner join.
        # The payload digest scan covers the caller's whole retained graph, not
        # just the rows a requested filter happens to select: a single retained
        # payload that disagrees with its fingerprint must fail every page.
        # The joined SQL fragment is constant; every caller value is parameterized.
        row = await (
            await connection.execute(
                "SELECT count(c.seq), COALESCE(max(c.seq), 0),"  # nosec B608
                " COALESCE((SELECT max_sequence FROM reporting_reconciliation_heads"
                " WHERE account_id = %s AND consumer_id = %s), 0),"
                " COALESCE(bool_or(c.seq IS NULL OR r.record_id IS NULL), false),"
                " COALESCE((SELECT bool_or(x.content_sha256"
                " <> reporting_payload_sha256(x.payload))"
                " FROM reporting_reconciliation_records x"
                " WHERE x.account_id = %s AND x.consumer_id = %s), false), clock_timestamp()"
                " FROM (SELECT * FROM reporting_reconciliation_changes"
                " WHERE account_id = %s AND consumer_id = %s) c"
                " FULL JOIN (SELECT * FROM reporting_reconciliation_records"
                " WHERE account_id = %s AND consumer_id = %s) r ON " + _FEED_JOIN,
                (who.account_id, who.consumer_id) * 4,
            )
        ).fetchone()
        assert row is not None
        if row[0] != row[1] or row[1] != row[2] or row[3] or row[4]:
            fail("REPORTING_HISTORY_CORRUPT")
        return row[2], row[5]

    async def _records(
        self, connection: Any, who: ReportingDeliveryPrincipal, maximum: int | None = None
    ) -> tuple[ReportingDeliveryRecord, ...]:
        await self._validate_feed(connection, who)
        return tuple(item.record for item in await self._changes(connection, who, maximum))

    async def _changes(
        self,
        connection: Any,
        who: ReportingDeliveryPrincipal,
        maximum: int | None = None,
        *,
        after: int = 0,
        limit: int | None = None,
        filters: ReportingReconciliationFilter = ReportingReconciliationFilter(),
    ) -> tuple[ReportingReconciliationChange, ...]:
        rows = await (
            await connection.execute(
                "SELECT r.payload, r.content_sha256, c.seq, r.change_id, "
                f"{_SELECT_IDENTITY} FROM reporting_reconciliation_changes c"  # noqa: S608  # nosec B608
                f" JOIN reporting_reconciliation_records r ON {_FEED_JOIN}"
                " WHERE c.account_id = %s AND c.consumer_id = %s"
                " AND (%s::bigint IS NULL OR c.seq <= %s::bigint) AND c.seq > %s"
                f" AND {_FEED_FILTER} ORDER BY c.seq LIMIT %s",
                (
                    who.account_id,
                    who.consumer_id,
                    maximum,
                    maximum,
                    after,
                    *_filter_params(filters),
                    limit,
                ),
            )
        ).fetchall()
        records = tuple(decode_record(row[0]) for row in rows)
        if any(
            principal(record) != who
            or fingerprint(record) != row[1]
            or row[2] is None
            or change_id(record) != row[3]
            or storage_identity(record) != tuple(row[4:])
            for record, row in zip(records, rows)
        ):
            fail("REPORTING_HISTORY_CORRUPT")
        return tuple(
            ReportingReconciliationChange(row[2], record) for row, record in zip(rows, records)
        )

    async def _change_count(
        self,
        connection: Any,
        caller: ReportingDeliveryPrincipal,
        after: int,
        maximum: int,
        filters: ReportingReconciliationFilter,
    ) -> int:
        row = await (
            await connection.execute(
                "SELECT count(*) FROM reporting_reconciliation_changes c"
                f" JOIN reporting_reconciliation_records r ON {_FEED_JOIN}"  # noqa: S608  # nosec B608
                " WHERE c.account_id = %s AND c.consumer_id = %s AND c.seq > %s AND c.seq <= %s"
                f" AND {_FEED_FILTER}",
                (caller.account_id, caller.consumer_id, after, maximum, *_filter_params(filters)),
            )
        ).fetchone()
        assert row is not None
        return int(row[0])

    async def _delivery_context(
        self, connection: Any, record: ReportingDeliveryRecord
    ) -> DeliveryContext:
        who = principal(record)
        configuration = None
        if isinstance(record, ReportingDestinationBinding) or self._notifications_enabled:
            generation = (
                record.generation_key
                if isinstance(record, ReportingDestinationBinding)
                else record.scope.generation_key
            )
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
            configuration = _configuration_from_row(row) if row else None
        if isinstance(record, ReportingDestinationBinding):
            return DeliveryContext(configuration=configuration)
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
        if isinstance(record, ReportingAdjustmentReceiptRecord):
            adjustment_row = await (
                await connection.execute(
                    f"SELECT {_ADJUSTMENT_COLUMNS} FROM reporting_adjustments"  # noqa: S608  # nosec B608
                    " WHERE account_id = %s AND reporting_adjustment_id = %s",
                    (who.account_id, record.reporting_adjustment_id),
                )
            ).fetchone()
        return DeliveryContext(
            configuration=configuration,
            obligation=_obligation_from_row(obligation_row) if obligation_row else None,
            revision=_revision_from_row(revision_row) if revision_row else None,
            adjustment=_adjustment_from_row(adjustment_row) if adjustment_row else None,
        )

    async def _insert(self, connection: Any, record: ReportingDeliveryRecord) -> None:
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
                *storage_identity(record),
                _json(payload(record)),
                fingerprint(record),
                change_id(record),
            ),
        )

    async def read_reconciliation_snapshot(
        self,
        *,
        caller: ReportingDeliveryPrincipal,
        boundary: ReportingReconciliationSnapshotToken | None = None,
    ) -> ReportingReconciliationSnapshot:
        requested = boundary is not None
        async with self._pool.connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            maximum, now = await self._validate_feed(connection, caller)
            if boundary is None:
                boundary = change_boundary(
                    caller, maximum, self._clock() if self._clock is not None else now
                )
            if (
                type(boundary) is not ReportingReconciliationSnapshotToken
                or boundary.caller != caller
                or boundary.min_sequence != 0
                or boundary.filters != ReportingReconciliationFilter()
            ):
                unavailable()
            validate_boundary(caller, boundary, maximum)
            records = tuple(
                item.record
                for item in await self._changes(connection, caller, boundary.max_sequence)
            )
            if len(records) != boundary.total_count:
                # See _boundary_unavailable: the caller's token, not the store.
                _boundary_unavailable(requested)
        return ReportingReconciliationSnapshot(caller, boundary, records)

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
        async with self._pool.connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            maximum, now = await self._validate_feed(connection, caller)
            if boundary is None:
                count = await self._change_count(connection, caller, after, maximum, filters)
                boundary = change_boundary(
                    caller,
                    maximum,
                    self._clock() if self._clock is not None else now,
                    after=after,
                    total_count=count,
                    filters=filters,
                )
            validate_boundary(caller, boundary, maximum)
            if last_key is not None:
                last = await self._changes(
                    connection, caller, after, after=after - 1, limit=1, filters=filters
                )
                if not last or change_id(last[0].record) != last_key:
                    raise LedgerConflictError(
                        "INVALID_CHECKPOINT", "reconciliation key is unavailable"
                    )
                count = await self._change_count(
                    connection, caller, boundary.min_sequence, boundary.max_sequence, filters
                )
                if count != boundary.total_count:
                    _boundary_unavailable(True)
            changes = await self._changes(
                connection,
                caller,
                boundary.max_sequence,
                after=after,
                limit=limit + 1,
                filters=filters,
            )
        return change_page(caller, boundary, after, changes, limit)
