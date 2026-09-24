"""Closed B+C history union; each queue retains its own HTTP attempt participant."""

from __future__ import annotations

from datetime import datetime

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.pg import PG_AVAILABLE
from adcp.reporting.outbox._activity_pg import _ACTIVITY_COLUMNS, _attempt
from adcp.reporting.outbox.activity import WebhookAttempt, activity_limit, retention_cutoff
from adcp.reporting.outbox.identity import canonical_consumer
from adcp.reporting.outbox.pg import PgReportingOutbox, database_now
from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox


class PgReportingActivityUnionStore:
    """Globally order both durable histories before applying the requested limit."""

    def __init__(self, b_outbox: PgReportingOutbox, status_outbox: PgReportingStatusOutbox) -> None:
        if not PG_AVAILABLE:
            raise ImportError("PgReportingActivityUnionStore requires PostgreSQL; install adcp[pg]")
        if (
            type(b_outbox) is not PgReportingOutbox
            or type(status_outbox) is not PgReportingStatusOutbox
            or b_outbox._pool is not status_outbox._pool
            or b_outbox._clock is not status_outbox._clock
        ):
            raise ReportingNotificationError("activity_union_requires_shared_pool")
        self.b_outbox, self.status_outbox = b_outbox, status_outbox

    async def list_activity(
        self, *, account_id: str, consumer_id: str, limit: int = 50
    ) -> tuple[WebhookAttempt, ...]:
        consumer_id, limit = canonical_consumer(consumer_id), activity_limit(limit)
        async with self.b_outbox._activity_transaction() as connection:
            rows = await (
                await connection.execute(
                    f"SELECT {_ACTIVITY_COLUMNS} FROM ("  # nosec B608
                    " SELECT *, 0 AS queue_rank FROM reporting_webhook_attempts"
                    " WHERE account_id=%s AND principal_id=%s UNION ALL"
                    " SELECT *, 1 AS queue_rank FROM reporting_status_webhook_attempts"
                    " WHERE account_id=%s AND principal_id=%s) AS history"
                    " ORDER BY fired_at DESC, notification_id DESC, idempotency_key DESC,"
                    " subscriber_id DESC, attempt DESC, delivery_id DESC, queue_rank DESC LIMIT %s",
                    (account_id, consumer_id, account_id, consumer_id, limit),
                )
            ).fetchall()
        return tuple(_attempt(row) for row in rows)

    async def purge_activity(
        self, *, account_id: str, consumer_id: str, now: datetime, retention_days: int = 30
    ) -> int:
        consumer_id = canonical_consumer(consumer_id)
        retention_cutoff(now, retention_days)
        async with self.b_outbox._activity_transaction() as connection:
            cutoff = retention_cutoff(
                await database_now(connection, self.b_outbox._clock), retention_days
            )
            count = 0
            for table in ("reporting_webhook_attempts", "reporting_status_webhook_attempts"):
                cursor = await connection.execute(
                    f"DELETE FROM {table} WHERE account_id=%s AND principal_id=%s"  # nosec B608
                    " AND completed_at IS NOT NULL AND completed_at < %s",
                    (account_id, consumer_id, cutoff),
                )
                count += cursor.rowcount
        return count
