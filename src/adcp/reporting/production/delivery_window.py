"""Immutable first-attempt deadlines for the three production delivery queues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from adcp.reporting.evidence import aware_utc
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.activity import ActivityRequest, ReportingActivityStore, WebhookAttempt
from adcp.reporting.outbox.models import DeliveryLease
from adcp.reporting.production.store import ReportingProductionStore, SavedDeliveryWindow

RETRY_HORIZON_SECONDS = 86400


def inspect_window(
    saved: SavedDeliveryWindow | None,
    expected: tuple[str, str],
    moment: datetime,
) -> tuple[datetime | None, bool]:
    """The recorded deadline and whether ``moment`` has passed it.

    A window recorded for another queue or body, or one that starts after
    ``moment``, is a retry contract violation rather than an expiry.
    """
    if saved is None:
        return None, False
    if saved[:2] != expected or moment < saved[2]:
        raise ReportingNotificationError("notification_retry_unready")
    return saved[3], moment >= saved[3]


@dataclass(frozen=True)
class ProductionDeliveryWindow:
    """One queue's window over the production store that records it.

    The store runs the reservation transaction (its lock, its clock, its
    activity participant); this object owns the queue identity and the policy.
    """

    store: ReportingProductionStore
    queue: str

    _inspect = staticmethod(inspect_window)

    async def inspect(self, lease: DeliveryLease, *, now: datetime) -> tuple[datetime | None, bool]:
        binding = lease.delivery.binding
        key = (binding.account_id, binding.idempotency_key)
        expected = (self.queue, binding.body_sha256)
        saved, moment = await self.store.read_delivery_window(key, now=aware_utc(now))
        return inspect_window(saved, expected, moment)

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
        if self.queue not in {"core", "status", "ready"}:
            raise ReportingNotificationError("notification_retry_unready")
        return await self.store.reserve_delivery_attempt(
            activity,
            lease,
            request=request,
            now=aware_utc(now),
            queue=self.queue,
            expected=(self.queue, binding.body_sha256),
            horizon_seconds=RETRY_HORIZON_SECONDS,
        )
