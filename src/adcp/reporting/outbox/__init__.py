"""Optional transactional reporting notifications. Status projection is not enabled."""

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
    validate_notification_payload,
)
from adcp.reporting.outbox.activity import (
    ActivityOutcome,
    ActivityRequest,
    ReportingActivityProjector,
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
from adcp.reporting.outbox.support import ReportingActivitySupport
from adcp.reporting.outbox.worker import ReportingNotificationWorker

if TYPE_CHECKING:
    from adcp.reporting.outbox.pg import PgReportingOutbox

__all__ = [
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
    if name == "PgReportingOutbox":
        from adcp.reporting.outbox.pg import PgReportingOutbox

        return PgReportingOutbox
    raise AttributeError(name)
