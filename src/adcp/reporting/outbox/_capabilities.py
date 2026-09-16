"""Conservative startup check for the optional notification capability fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from adcp.reporting.ledger.delivery import InMemoryReportingReconciliationStore
from adcp.reporting.ledger.delivery_models import ReportingDeliveryScope
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.store import InMemoryReportingLedgerStore, ReportingLedgerStore
from adcp.reporting.outbox.memory import InMemoryReportingOutbox
from adcp.reporting.outbox.routing import ReportingEnvelopeCipher, ReportingNotificationSubscription

if TYPE_CHECKING:
    from adcp.reporting.outbox.activity import ReportingActivityProjector
    from adcp.reporting.outbox.worker import ReportingNotificationWorker


async def advertised_notifications(
    worker: ReportingNotificationWorker,
    ledger: ReportingLedgerStore,
    *,
    account_id: str,
    ready_scope: ReportingDeliveryScope | None,
    activity_projector: ReportingActivityProjector | None = None,
) -> dict[str, str | bool]:
    from adcp.reporting.outbox.worker import ReportingNotificationWorker

    if (
        type(worker) is not ReportingNotificationWorker
        or type(worker.cipher) is not ReportingEnvelopeCipher
    ):
        raise ReportingNotificationError("notification_chain_unready")
    if type(worker.outbox) is InMemoryReportingOutbox:
        if (
            type(ledger) not in {InMemoryReportingLedgerStore, InMemoryReportingReconciliationStore}
            or worker.outbox._store is not ledger
            or worker.outbox._store._notification_state is None
        ):
            raise ReportingNotificationError("notification_chain_unready")
    else:
        # Deliberately lazy: the base SDK and memory implementation need no PG extra.
        from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore
        from adcp.reporting.ledger.pg import PgReportingLedgerStore
        from adcp.reporting.outbox._schema import validate_schema
        from adcp.reporting.outbox.pg import PgReportingOutbox

        if (
            type(worker.outbox) is not PgReportingOutbox
            or not isinstance(worker.outbox, PgReportingOutbox)
            or type(ledger) not in {PgReportingLedgerStore, PgReportingReconciliationStore}
            or not isinstance(ledger, PgReportingLedgerStore)
            or ledger._pool is not worker.outbox._pool
            or not ledger._notifications_enabled
        ):
            raise ReportingNotificationError("notification_chain_unready")
        async with ledger._pool.connection() as connection, connection.transaction():
            await validate_schema(connection)

    # A successful empty resolution is a configured service, not an error.
    # Every configured mode must also have usable trusted signing material.
    # This is a startup check; dispatch still repeats exact resolution per attempt.
    for event_type in ("reporting.ledger_changed", "reporting.delivery_ready"):
        subscriptions = await worker.subscriptions.list_active(
            account_id=account_id, notification_type=event_type
        )
        identifiers: set[str] = set()
        for subscription in subscriptions:
            if (
                type(subscription) is not ReportingNotificationSubscription
                or subscription.account_id != account_id
                or subscription.subscriber_id in identifiers
                or event_type not in subscription.event_types
                or not (
                    subscription.active and subscription.authorized and subscription.proof_valid
                )
            ):
                raise ReportingNotificationError("notification_chain_unready")
            ReportingNotificationSubscription.__post_init__(subscription)
            identifiers.add(subscription.subscriber_id)
            sender = await worker._sender(subscription)
            await sender.aclose()

    result: dict[str, str | bool] = {
        "ledger_notification": "reporting.ledger_changed",
        "supports_webhook_activity": False,
    }
    if activity_projector is not None:
        from adcp.reporting.outbox.support import ReportingActivitySupport

        result["supports_webhook_activity"] = await ReportingActivitySupport(
            worker, ledger, activity_projector
        ).durable()
    if ready_scope is not None:
        if ready_scope.principal.account_id != account_id:
            raise ReportingNotificationError("notification_chain_unready")
        # A capability string cannot turn Core into Managed. These concrete
        # stores froze and validated the configuration/destination/obligation
        # relationship transactionally. All references remain immutable.
        if not isinstance(ledger, InMemoryReportingReconciliationStore):
            from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore

            if not isinstance(ledger, PgReportingReconciliationStore):
                raise ReportingNotificationError("core_delivery_ready_forbidden")
        binding = await ledger.get_destination_binding(
            caller=ready_scope.principal, generation_key=ready_scope.generation_key
        )
        frozen = await ledger.get_obligation_delivery(ready_scope)
        obligation = await ledger.get_obligation(
            account_id=account_id, reporting_obligation_id=ready_scope.reporting_obligation_id
        )
        configurations = await ledger.list_configurations(account_id=account_id)
        if (
            binding is None
            or frozen is None
            or obligation is None
            or obligation.generation_key != ready_scope.generation_key
            or not any(
                config.generation_key == ready_scope.generation_key for config in configurations
            )
            or frozen.currency != obligation.currency
        ):
            raise ReportingNotificationError("notification_chain_unready")
        # B1 contracts and a frozen binding are not a durable materializer.
        # B2 must supply a concrete verified write/finish readiness proof before
        # a positive delivery_ready capability can be added here.
    return result
