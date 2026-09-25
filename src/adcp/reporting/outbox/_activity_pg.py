"""SQL activity participant for PgReportingOutbox, with no independent queue."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from secrets import token_hex
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.activity import (
    ActivityOutcome,
    ActivityRequest,
    WebhookAttempt,
    activity_limit,
    retention_cutoff,
)
from adcp.reporting.outbox.identity import canonical_consumer
from adcp.reporting.outbox.models import DeliveryBinding, DeliveryLease

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_ACTIVITY_COLUMNS = (
    "binding, attempt, lease_token, reservation_token, fired_at, url, payload_size_bytes,"
    " status, completed_at, http_status_code, response_time_ms"
)


def _attempt(row: Any) -> WebhookAttempt:
    try:
        return WebhookAttempt(
            DeliveryBinding(**row[0]),
            row[1],
            row[2],
            row[3],
            row[4],
            ActivityRequest(row[5], row[6]),
            ActivityOutcome(row[7], row[9], row[10]) if row[7] != "pending" else None,
            row[8],
        )
    except (TypeError, ValueError, KeyError):
        raise ReportingNotificationError("invalid_activity_record") from None


class _PgReportingActivity:
    _pool: AsyncConnectionPool
    _clock: Callable[[], datetime] | None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as connection:
            yield connection

    @asynccontextmanager
    async def _activity_transaction(self) -> AsyncIterator[Any]:
        try:
            async with self._connection() as conn, conn.transaction():
                yield conn
        except ReportingNotificationError:
            raise
        except Exception:
            # psycopg diagnostics may include statements, parameters, provider
            # strings, and connection URLs. None belong in activity failures.
            raise ReportingNotificationError("activity_store_unavailable") from None

    async def _activity_fence(self, conn: Any, lease: DeliveryLease) -> datetime | None:
        from adcp.reporting.outbox.pg import _DELIVERY_COLUMNS, _delivery, database_now

        b = lease.delivery.binding
        # The projection is an SDK constant; every selector is a bound value.
        row = await (
            await conn.execute(
                f"SELECT {_DELIVERY_COLUMNS}, lease_expires_at"  # nosec B608
                " FROM reporting_notification_deliveries"
                " WHERE account_id = %s AND principal_id = %s AND consumer_namespace = %s"
                " AND delivery_id = %s AND subscriber_id = %s AND notification_id = %s"
                " AND idempotency_key = %s AND state = 'leased' AND lease_token = %s FOR UPDATE",
                (
                    b.account_id,
                    b.principal_id,
                    b.consumer_namespace,
                    b.delivery_id,
                    b.subscriber_id,
                    b.notification_id,
                    b.idempotency_key,
                    lease.token,
                ),
            )
        ).fetchone()
        # Take database time AFTER acquiring the row lock, never before waiting.
        at = await database_now(conn, self._clock)
        if (
            row is None
            or _delivery(row) != lease.delivery
            or row[-1] != lease.expires_at
            or row[-1] <= at
        ):
            return None
        return at

    async def reserve_attempt(
        self, lease: DeliveryLease, *, request: ActivityRequest, now: datetime
    ) -> WebhookAttempt | None:
        async with self._activity_transaction() as conn:
            return await self._reserve_attempt_on(conn, lease, request=request)

    async def _reserve_attempt_on(
        self, conn: Any, lease: DeliveryLease, *, request: ActivityRequest
    ) -> WebhookAttempt | None:
        from adcp.reporting.outbox.pg import database_now

        b = lease.delivery.binding
        consumer = canonical_consumer(b.principal_id)
        request = ActivityRequest(request.url, request.payload_size_bytes)
        key = (b.account_id, consumer, b.subscriber_id, b.idempotency_key)
        if await self._activity_fence(conn, lease) is None:
            return None
        duplicate = await (
            await conn.execute(
                "SELECT 1 FROM reporting_webhook_attempts WHERE account_id = %s"
                " AND principal_id = %s AND consumer_namespace = %s"
                " AND delivery_id = %s AND lease_token = %s",
                (b.account_id, consumer, b.consumer_namespace, b.delivery_id, lease.token),
            )
        ).fetchone()
        if duplicate is not None:
            return None
        number = await self._next_attempt_on(conn, b)
        at = await database_now(conn, self._clock)
        # The row stays locked from the fence through this commit. A later
        # reclaim can never mutate this reservation, including after purge.
        if at >= lease.expires_at:
            # Roll back the increment as well; no reservation means no HTTP.
            raise ReportingNotificationError("activity_lease_expired")
        reservation = token_hex(32)
        await conn.execute(
            "INSERT INTO reporting_webhook_attempts (account_id, principal_id, subscriber_id,"
            " idempotency_key, notification_id, attempt, delivery_id, consumer_namespace,"
            " lease_token, reservation_token, binding, fired_at, url, payload_size_bytes)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (
                *key,
                b.notification_id,
                number,
                b.delivery_id,
                b.consumer_namespace,
                lease.token,
                reservation,
                json.dumps(asdict(b)),
                at,
                request.url,
                request.payload_size_bytes,
            ),
        )
        return WebhookAttempt(b, number, lease.token, reservation, at, request)

    async def _next_attempt_on(self, conn: Any, binding: DeliveryBinding) -> int:
        b = binding
        row = await (
            await conn.execute(
                "INSERT INTO reporting_webhook_attempt_heads (account_id, principal_id,"
                " subscriber_id, idempotency_key, last_attempt)"
                " VALUES (%s,%s,%s,%s,1) ON CONFLICT"
                " (account_id, principal_id, subscriber_id, idempotency_key)"
                " DO UPDATE SET last_attempt = reporting_webhook_attempt_heads.last_attempt + 1"
                " WHERE reporting_webhook_attempt_heads.account_id = %s"
                " AND reporting_webhook_attempt_heads.principal_id = %s RETURNING last_attempt",
                (
                    b.account_id,
                    b.principal_id,
                    b.subscriber_id,
                    b.idempotency_key,
                    b.account_id,
                    b.principal_id,
                ),
            )
        ).fetchone()
        assert row is not None
        return int(row[0])

    async def complete_attempt(
        self, attempt: WebhookAttempt, *, outcome: ActivityOutcome, now: datetime
    ) -> bool:
        from adcp.reporting.outbox.pg import database_now

        ActivityOutcome.__post_init__(outcome)
        b = attempt.binding
        consumer = canonical_consumer(b.principal_id)
        if attempt.outcome is not None or attempt.completed_at is not None:
            return False
        async with self._activity_transaction() as conn:
            at = await database_now(conn, self._clock)
            if at < attempt.fired_at:
                return False
            cursor = await conn.execute(
                "UPDATE reporting_webhook_attempts SET status = %s, completed_at = %s,"
                " http_status_code = %s, response_time_ms = %s"
                " WHERE account_id = %s AND principal_id = %s AND subscriber_id = %s"
                " AND notification_id = %s AND idempotency_key = %s AND attempt = %s"
                " AND delivery_id = %s AND consumer_namespace = %s AND reservation_token = %s"
                " AND status = 'pending' AND fired_at = %s AND url = %s"
                " AND payload_size_bytes = %s AND lease_token = %s AND binding = %s::jsonb",
                (
                    outcome.status,
                    at,
                    outcome.http_status_code,
                    outcome.response_time_ms,
                    b.account_id,
                    consumer,
                    b.subscriber_id,
                    b.notification_id,
                    b.idempotency_key,
                    attempt.attempt,
                    b.delivery_id,
                    b.consumer_namespace,
                    attempt.reservation_token,
                    attempt.fired_at,
                    attempt.request.url,
                    attempt.request.payload_size_bytes,
                    attempt.lease_token,
                    json.dumps(asdict(b)),
                ),
            )
            return bool(cursor.rowcount)

    async def list_activity(
        self, *, account_id: str, consumer_id: str, limit: int = 50
    ) -> tuple[WebhookAttempt, ...]:
        consumer_id, limit = canonical_consumer(consumer_id), activity_limit(limit)
        async with self._activity_transaction() as conn:
            # Only the SDK-owned column list is interpolated, never tenant input.
            rows = await (
                await conn.execute(
                    f"SELECT {_ACTIVITY_COLUMNS} FROM reporting_webhook_attempts"  # nosec B608
                    " WHERE account_id = %s AND principal_id = %s ORDER BY fired_at DESC,"
                    " notification_id DESC, idempotency_key DESC, subscriber_id DESC, attempt DESC,"
                    " delivery_id DESC LIMIT %s",
                    (account_id, consumer_id, limit),
                )
            ).fetchall()
        return tuple(_attempt(row) for row in rows)

    async def purge_activity(
        self, *, account_id: str, consumer_id: str, now: datetime, retention_days: int = 30
    ) -> int:
        from adcp.reporting.outbox.pg import database_now

        consumer_id = canonical_consumer(consumer_id)
        retention_cutoff(now, retention_days)
        async with self._activity_transaction() as conn:
            cutoff = retention_cutoff(await database_now(conn, self._clock), retention_days)
            cursor = await conn.execute(
                "DELETE FROM reporting_webhook_attempts WHERE account_id = %s AND principal_id = %s"
                " AND completed_at IS NOT NULL AND completed_at < %s",
                (account_id, consumer_id, cutoff),
            )
            return int(cursor.rowcount)
