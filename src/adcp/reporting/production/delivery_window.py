"""Immutable first-attempt deadlines for the three production delivery queues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.activity import ActivityRequest, ReportingActivityStore, WebhookAttempt
from adcp.reporting.outbox.memory import InMemoryReportingOutbox
from adcp.reporting.outbox.models import DeliveryLease
from adcp.reporting.production.memory import InMemoryReportingProductionStore

if TYPE_CHECKING:
    from adcp.reporting.production.pg import PgReportingProductionStore

RETRY_HORIZON_SECONDS = 86400


@dataclass(frozen=True)
class ProductionDeliveryWindow:
    store: InMemoryReportingProductionStore | PgReportingProductionStore
    queue: str

    @staticmethod
    def _inspect(
        saved: tuple[str, str, datetime, datetime] | None,
        expected: tuple[str, str],
        moment: datetime,
    ) -> tuple[datetime | None, bool]:
        if saved is None:
            return None, False
        if saved[:2] != expected or moment < saved[2]:
            raise ReportingNotificationError("notification_retry_unready")
        return saved[3], moment >= saved[3]

    async def inspect(self, lease: DeliveryLease, *, now: datetime) -> tuple[datetime | None, bool]:
        binding = lease.delivery.binding
        key = (binding.account_id, binding.idempotency_key)
        expected = (self.queue, binding.body_sha256)
        if isinstance(self.store, InMemoryReportingProductionStore):
            async with self.store._lock:
                return self._inspect(
                    self.store._production_delivery_windows.get(key), expected, aware_utc(now)
                )
        from adcp.reporting.outbox.pg import database_now

        try:
            async with self.store._connection() as connection, connection.transaction():
                saved = await (
                    await connection.execute(
                        "SELECT queue,body_sha256,started_at,expires_at"
                        " FROM reporting_production_delivery_windows"
                        " WHERE account_id=%s AND idempotency_key=%s",
                        key,
                    )
                ).fetchone()
                return self._inspect(
                    saved, expected, await database_now(connection, self.store._clock)
                )
        except ReportingNotificationError:
            raise
        except Exception:
            raise ReportingNotificationError("notification_retry_unready") from None

    async def reserve_attempt(
        self,
        activity: ReportingActivityStore,
        lease: DeliveryLease,
        *,
        request: ActivityRequest,
        now: datetime,
    ) -> tuple[WebhookAttempt | None, datetime | None, bool]:
        """Commit the immutable deadline with the original SDK HTTP reservation.

        False admission never produces an activity ordinal. A transaction
        failing after either insertion rolls back the window, ordinal head and
        attempt together. Unknown commit outcomes leave the original identity.
        """
        binding = lease.delivery.binding
        key = (binding.account_id, binding.idempotency_key)
        expected = (self.queue, binding.body_sha256)
        if self.queue not in {"core", "status", "ready"}:
            raise ReportingNotificationError("notification_retry_unready")
        if isinstance(self.store, InMemoryReportingProductionStore):
            if (
                not isinstance(activity, InMemoryReportingOutbox)
                or activity._store is not self.store
            ):
                raise ReportingNotificationError("notification_retry_unready")
            async with self.store._mutation():
                at = aware_utc(now)
                saved = self.store._production_delivery_windows.get(key)
                deadline, expired = self._inspect(saved, expected, at)
                if expired:
                    return None, deadline, True
                attempt = activity._reserve_attempt_locked(lease, request=request, now=at)
                if attempt is None:
                    return None, deadline, False
                retained = self.store._production_delivery_windows.setdefault(
                    key,
                    (
                        *expected,
                        attempt.fired_at,
                        attempt.fired_at + timedelta(seconds=RETRY_HORIZON_SECONDS),
                    ),
                )
                return attempt, retained[3], False

        from adcp.reporting.outbox.pg import PgReportingOutbox, database_now

        if not isinstance(activity, PgReportingOutbox) or activity._pool is not self.store._pool:
            raise ReportingNotificationError("notification_retry_unready")
        try:
            # The activity participant supplies its exact queue adapter and
            # connection. Take the inherited account lock before its row lock.
            async with activity._activity_transaction() as connection:
                await self.store._lock_account(connection, binding.account_id)
                moment = await database_now(connection, self.store._clock)
                saved = await (
                    await connection.execute(
                        "SELECT queue,body_sha256,started_at,expires_at"
                        " FROM reporting_production_delivery_windows"
                        " WHERE account_id=%s AND idempotency_key=%s",
                        key,
                    )
                ).fetchone()
                deadline, expired = self._inspect(saved, expected, moment)
                if expired:
                    return None, deadline, True
                attempt = await activity._reserve_attempt_on(connection, lease, request=request)
                if attempt is None:
                    return None, deadline, False
                deadline, expired = self._inspect(saved, expected, attempt.fired_at)
                if expired:
                    # Time can cross the deadline while reserving an ordinal.
                    # Roll back that provisional head and attempt, too.
                    raise ReportingNotificationError("notification_retry_expired")
                await connection.execute(
                    "INSERT INTO reporting_production_delivery_windows"
                    " (account_id,idempotency_key,queue,body_sha256,started_at,expires_at)"
                    " VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        *key,
                        *expected,
                        attempt.fired_at,
                        attempt.fired_at + timedelta(seconds=RETRY_HORIZON_SECONDS),
                    ),
                )
                return (
                    attempt,
                    deadline or attempt.fired_at + timedelta(seconds=RETRY_HORIZON_SECONDS),
                    False,
                )
        except ReportingNotificationError as error:
            if error.code == "notification_retry_expired":
                return None, deadline, True
            raise
