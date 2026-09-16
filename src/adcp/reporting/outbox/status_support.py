"""Explicit optional C scheduling and per-account advertisement proof."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status import ReportingStatusHandler
from adcp.reporting.outbox.routing import ReportingEnvelopeCipher, ReportingNotificationSubscription
from adcp.reporting.outbox.status import (
    ReportingStatusProjector,
    ReportingStatusSweeper,
    StatusNotificationStore,
)
from adcp.reporting.outbox.worker import ReportingNotificationWorker
from adcp.server.base import ADCPHandler


@dataclass(frozen=True)
class ReportingStatusSupport:
    """Mount only while the adopter schedules projector, sweep and HTTP turns.

    ``account_ids`` is the server's explicit supported account set, not an
    account-listing service or request-body identity. Global static claims
    require every one to be baselined. The protocol capability request has no
    account selector: callers must declare this enumeration complete for the
    authenticated or deployment surface, never select it from request context.
    """

    store: StatusNotificationStore
    worker: ReportingNotificationWorker
    projector: ReportingStatusProjector | None = None
    sweeper: ReportingStatusSweeper | None = None
    scheduled: bool = False
    account_ids: tuple[str, ...] = ()
    account_surface_complete: bool = False
    handler: ADCPHandler[Any] | None = None
    readiness_subscriptions: tuple[ReportingNotificationSubscription, ...] = ()

    async def queue_durable(self) -> bool:
        from adcp.reporting.outbox.status_memory import InMemoryStatusNotificationStore

        if (
            not self.scheduled
            or self.projector is None
            or self.sweeper is None
            or type(self.worker) is not ReportingNotificationWorker
            or type(self.worker.cipher) is not ReportingEnvelopeCipher
            or type(self.projector) is not ReportingStatusProjector
            or type(self.sweeper) is not ReportingStatusSweeper
            or self.projector.store is not self.store
            or self.sweeper.store is not self.store
        ):
            return False
        if type(self.store) is InMemoryStatusNotificationStore:
            return False
        from adcp.reporting.outbox.status_pg import (
            PgReportingStatusOutbox,
            PgStatusNotificationStore,
        )
        from adcp.reporting.outbox.status_schema import validate_status_schema

        if (
            type(self.store) is not PgStatusNotificationStore
            or not isinstance(self.store, PgStatusNotificationStore)
            or type(self.worker.outbox) is not PgReportingStatusOutbox
            or self.worker.outbox is not self.store.outbox
            or self.store.ledger._pool is not self.store.outbox._pool
            or not self.store.ledger._notifications_enabled
            or (self.worker.activity is not None and self.worker.activity is not self.worker.outbox)
        ):
            return False
        try:
            async with self.store.ledger._pool.connection() as connection:
                await validate_status_schema(connection, activity=self.worker.activity is not None)
        except ReportingNotificationError:
            return False
        return True

    async def account_ready(self, *, account_id: str) -> bool:
        if not await self.queue_durable() or not await self.store.baseline_ready(
            account_id=account_id
        ):
            return False
        from adcp.reporting.outbox.status_pg import PgStatusNotificationStore

        assert isinstance(self.store, PgStatusNotificationStore)
        async with self.store.ledger._pool.connection() as connection:
            managed = await (
                await connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM reporting_reconciliation_records"
                    " WHERE account_id=%s AND record_kind='destination_binding')",
                    (account_id,),
                )
            ).fetchone()
            if managed is not None and managed[0]:
                return False  # Managed expiry/reconciliation health ships with D.
        subscriptions = await self.worker.subscriptions.list_active(
            account_id=account_id, notification_type="reporting.status_changed"
        )
        probes = tuple(s for s in self.readiness_subscriptions if s.account_id == account_id)
        if not probes:
            return False
        identities: set[str] = set()
        for subscription in subscriptions:
            if (
                type(subscription) is not ReportingNotificationSubscription
                or subscription.account_id != account_id
                or subscription.subscriber_id in identities
                or "reporting.status_changed" not in subscription.event_types
                or not (
                    subscription.active and subscription.authorized and subscription.proof_valid
                )
            ):
                raise ReportingNotificationError("status_subscription_unready")
            ReportingNotificationSubscription.__post_init__(subscription)
            identities.add(subscription.subscriber_id)
            sender = await self.worker._sender(subscription)
            await sender.aclose()
        for subscription in probes:
            if (
                type(subscription) is not ReportingNotificationSubscription
                or "reporting.status_changed" not in subscription.event_types
                or not (
                    subscription.active and subscription.authorized and subscription.proof_valid
                )
            ):
                raise ReportingNotificationError("status_subscription_unready")
            ReportingNotificationSubscription.__post_init__(subscription)
            sender = await self.worker._sender(subscription)
            await sender.aclose()
        return True

    async def durable(self) -> bool:
        from adcp.server.mcp_tools import get_tools_for_handler

        if not self.account_surface_complete or not self.account_ids or self.handler is None:
            return False
        projection = getattr(self.handler, "reporting_status_handler", None)
        if (
            type(projection) is not ReportingStatusHandler
            or projection._store is not getattr(self.store, "ledger", None)
            or projection._escalation != getattr(self.store, "escalation", None)
            or not projection._consumer_status_enabled
            or "get_reporting_status"
            not in {t["name"] for t in get_tools_for_handler(self.handler)}
        ):
            return False
        return all([await self.account_ready(account_id=a) for a in sorted(set(self.account_ids))])

    async def advertised_notifications(self) -> dict[str, str]:
        """An explicit global claim, after checking the entire trusted surface."""
        return (
            {
                "status_task": "get_reporting_status",
                "status_notification": "reporting.status_changed",
            }
            if await self.durable()
            else {}
        )


async def validate_status_claims(
    response: dict[str, Any],
    *,
    support: ReportingStatusSupport | None,
    handler: ADCPHandler[Any] | None = None,
) -> None:
    claim = response.get("media_buy", {}).get("reporting_delivery", {}).get("status_notification")
    if claim is None:
        return
    block = response.get("media_buy", {}).get("reporting_delivery", {})
    if (
        claim != "reporting.status_changed"
        or support is None
        or block.get("status_task") != "get_reporting_status"
        or (handler is not None and support.handler is not handler)
    ):
        raise ReportingNotificationError("status_capability_requires_durable_reporting")
    if not await support.durable():
        raise ReportingNotificationError("status_capability_requires_durable_reporting")
    from adcp.server.mcp_tools import get_tools_for_handler

    assert support.handler is not None
    mounted = {t["name"] for t in get_tools_for_handler(support.handler)}
    for field, task in block.items():
        if field.endswith("_task") and isinstance(task, str) and task not in mounted:
            raise ReportingNotificationError("reporting_capability_requires_mounted_task")
