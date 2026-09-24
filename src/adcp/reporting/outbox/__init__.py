"""Optional transactional reporting notifications, activity and status lifecycle."""

from typing import TYPE_CHECKING

from adcp.reporting.ledger.notification_models import (
    AdjustmentPublished,
    MaterializationReady,
    ReportingDomainEvent,
    ReportingNotificationError,
    ReportingStatusDirty,
    ReportingStatusEvidence,
    ReportingStatusScope,
    RevisionPublished,
    StatusChanged,
    validate_notification_payload,
)
from adcp.reporting.outbox.activity import (
    ActivityOutcome,
    ActivityRequest,
    ReportingActivityProjector,
    ReportingActivityReader,
    ReportingActivityStore,
    WebhookAttempt,
    sanitize_activity_url,
)
from adcp.reporting.outbox.identity import resolve_reporting_consumer
from adcp.reporting.outbox.memory import InMemoryReportingOutbox
from adcp.reporting.outbox.models import (
    DeliveryBinding,
    DeliveryLease,
    DeliveryStatus,
    ExpansionLease,
    ReportingNotificationOutbox,
    StoredDelivery,
)
from adcp.reporting.outbox.routing import (
    ReportingEnvelopeCipher,
    ReportingLegacyAuthentication,
    ReportingNotificationSubscription,
    ReportingSigningMaterial,
    ReportingSigningResolver,
    ReportingSubscriptionResolver,
)
from adcp.reporting.outbox.status import (
    ReportingStatusProjector,
    ReportingStatusSweeper,
    StatusBoundary,
    StatusCheckpoint,
    StatusDueLease,
    StatusNotificationStore,
    StatusSelectorRebuildStore,
    StatusTurn,
)
from adcp.reporting.outbox.status_memory import (
    InMemoryReportingStatusOutbox,
    InMemoryStatusNotificationStore,
)
from adcp.reporting.outbox.status_service import (
    ReportingStatusNotificationLifecycle,
    ReportingStatusService,
)
from adcp.reporting.outbox.status_support import ReportingStatusSupport
from adcp.reporting.outbox.support import ReportingActivitySupport
from adcp.reporting.outbox.worker import ReportingNotificationWorker

if TYPE_CHECKING:
    from adcp.reporting.outbox.pg import PgReportingOutbox
    from adcp.reporting.outbox.status_activity_pg import PgReportingActivityUnionStore
    from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox, PgStatusNotificationStore

__all__ = [
    "ReportingStatusNotificationLifecycle",
    "ReportingStatusService",
    "ReportingActivityReader",
    "ReportingStatusSupport",
    "PgReportingActivityUnionStore",
    "StatusChanged",
    "StatusBoundary",
    "StatusCheckpoint",
    "StatusDueLease",
    "StatusNotificationStore",
    "StatusSelectorRebuildStore",
    "StatusTurn",
    "ReportingStatusProjector",
    "ReportingStatusSweeper",
    "InMemoryReportingStatusOutbox",
    "InMemoryStatusNotificationStore",
    "PgReportingStatusOutbox",
    "PgStatusNotificationStore",
    "ActivityOutcome",
    "ActivityRequest",
    "ReportingActivityProjector",
    "ReportingActivityStore",
    "ReportingActivitySupport",
    "WebhookAttempt",
    "resolve_reporting_consumer",
    "sanitize_activity_url",
    "AdjustmentPublished",
    "DeliveryBinding",
    "DeliveryLease",
    "DeliveryStatus",
    "ExpansionLease",
    "InMemoryReportingOutbox",
    "MaterializationReady",
    "PgReportingOutbox",
    "ReportingDomainEvent",
    "ReportingEnvelopeCipher",
    "ReportingLegacyAuthentication",
    "ReportingNotificationError",
    "ReportingNotificationOutbox",
    "ReportingNotificationSubscription",
    "ReportingNotificationWorker",
    "ReportingSigningMaterial",
    "ReportingSigningResolver",
    "ReportingStatusDirty",
    "ReportingStatusEvidence",
    "ReportingStatusScope",
    "ReportingSubscriptionResolver",
    "RevisionPublished",
    "StoredDelivery",
    "validate_notification_payload",
]


def __getattr__(name: str) -> object:
    if name == "PgReportingActivityUnionStore":
        from adcp.reporting.outbox.status_activity_pg import PgReportingActivityUnionStore

        return PgReportingActivityUnionStore
    if name in {"PgReportingStatusOutbox", "PgStatusNotificationStore"}:
        from adcp.reporting.outbox.status_pg import (
            PgReportingStatusOutbox,
            PgStatusNotificationStore,
        )

        return {
            "PgReportingStatusOutbox": PgReportingStatusOutbox,
            "PgStatusNotificationStore": PgStatusNotificationStore,
        }[name]
    if name == "PgReportingOutbox":
        from adcp.reporting.outbox.pg import PgReportingOutbox

        return PgReportingOutbox
    raise AttributeError(name)
