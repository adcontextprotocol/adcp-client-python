"""Independent reporting and list-accounts activity capability gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.activity import ReportingActivityProjector

if TYPE_CHECKING:
    from adcp.reporting.ledger.store import ReportingLedgerStore
    from adcp.reporting.outbox.worker import ReportingNotificationWorker


@dataclass(frozen=True)
class ReportingActivitySupport:
    """The concrete writer/store/projector chain scheduled by the adopter.

    Mount this as ``reporting_activity=`` on the platform server factory. Mount
    ``projector`` separately as ``account_activity=`` to enable list-accounts
    enrichment. Neither relationship notification claims nor memory components
    supply evidence of durable reporting activity.
    """

    worker: ReportingNotificationWorker
    ledger: ReportingLedgerStore
    projector: ReportingActivityProjector | None = None

    async def durable(self) -> bool:
        from adcp.reporting.ledger.delivery import InMemoryReportingReconciliationStore
        from adcp.reporting.ledger.store import InMemoryReportingLedgerStore
        from adcp.reporting.outbox.memory import InMemoryReportingOutbox
        from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
        from adcp.reporting.outbox.worker import ReportingNotificationWorker

        if self.projector is None or self.worker.activity is None:
            return False
        if (
            type(self.worker) is not ReportingNotificationWorker
            or type(self.worker.cipher) is not ReportingEnvelopeCipher
            or type(self.projector) is not ReportingActivityProjector
            or id(self.worker.activity) != id(self.worker.outbox)
            or id(self.projector.store) != id(self.worker.outbox)
        ):
            raise ReportingNotificationError("activity_chain_unready")
        if type(self.worker.outbox) is InMemoryReportingOutbox:
            if (
                type(self.ledger)
                not in {InMemoryReportingLedgerStore, InMemoryReportingReconciliationStore}
                or self.worker.outbox._store is not self.ledger
            ):
                raise ReportingNotificationError("activity_chain_unready")
            return False
        # Lazy imports retain base-install operation without the [pg] extra.
        from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore
        from adcp.reporting.ledger.pg import PgReportingLedgerStore
        from adcp.reporting.outbox._schema import validate_schema
        from adcp.reporting.outbox.pg import PgReportingOutbox

        if (
            type(self.worker.outbox) is not PgReportingOutbox
            or not isinstance(self.worker.outbox, PgReportingOutbox)
            or type(self.ledger) not in {PgReportingLedgerStore, PgReportingReconciliationStore}
            or not isinstance(self.ledger, PgReportingLedgerStore)
            or self.ledger._pool is not self.worker.outbox._pool
            or not self.ledger._notifications_enabled
        ):
            raise ReportingNotificationError("activity_chain_unready")
        async with self.ledger._pool.connection() as connection:
            await validate_schema(connection, activity=True)
        return True

    async def capability_flags(
        self, *, account_activity: ReportingActivityProjector | None = None
    ) -> dict[str, bool]:
        durable = await self.durable()
        return {
            "reporting": durable,
            "account_notifications": durable and account_activity is self.projector,
        }


async def validate_activity_claims(
    response: dict[str, Any],
    *,
    support: ReportingActivitySupport | None,
    account_activity: ReportingActivityProjector | None,
    account_listing: bool,
) -> None:
    reporting = response.get("media_buy", {}).get("reporting_delivery", {})
    account = response.get("account", {}).get("notifications", {})
    reporting_claim = reporting.get("supports_webhook_activity") is True
    account_claim = account.get("supports_webhook_activity") is True
    if not reporting_claim and not account_claim:
        return
    flags = (
        await support.capability_flags(account_activity=account_activity)
        if support is not None
        else {"reporting": False, "account_notifications": False}
    )
    if reporting_claim and not flags["reporting"]:
        raise ReportingNotificationError("activity_capability_requires_durable_reporting")
    if account_claim and not (flags["account_notifications"] and account_listing):
        raise ReportingNotificationError("activity_capability_requires_account_projection")
