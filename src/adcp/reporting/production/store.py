"""The store contract the production composition depends on.

:class:`~adcp.reporting.production.service.ReportingProductionSupport` composes
a materializer, a status projection and notification workers over one store.
It used to recognize the two shipped stores by class. The in-memory store now
lives in ``adcp`` and the PostgreSQL store in ``adcp-reporting-storage``, so
the composition asks the store instead: which projection and queues belong to
it, whether its schema is installed, what maintenance it needs. Both shipped
stores keep the exact-class checks inside their own answers, so an adopter
subclass is still refused where it was refused before.
"""

from __future__ import annotations

import weakref
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adcp.reporting.feed.store import ReportingFeedStore
from adcp.reporting.ledger.delivery import ReportingReconciliationStore
from adcp.reporting.ledger.store import ReportingLedgerStore
from adcp.reporting.materializer.work import ReportingMaterializerStore
from adcp.reporting.receipts.store import ReportingReceiptBatchStore

if TYPE_CHECKING:
    from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
    from adcp.reporting.ledger.models import ReportingConfiguration, ReportingDeliveryEscalation
    from adcp.reporting.outbox.activity import (
        ActivityRequest,
        ReportingActivityStore,
        WebhookAttempt,
    )
    from adcp.reporting.outbox.models import DeliveryLease
    from adcp.reporting.outbox.status import StatusTurn
    from adcp.reporting.outbox.worker import ReportingNotificationWorker
    from adcp.reporting.production.service import ReportingProductionSupport
    from adcp.reporting.production.source_registry import ReportingProductionSourceContext

__all__ = [
    "MaintenanceOperation",
    "ReportingProductionProjection",
    "ReportingProductionStore",
    "SavedDeliveryWindow",
]

#: ``(queue, body_sha256, started_at, expires_at)`` of a recorded first attempt.
SavedDeliveryWindow = tuple[str, str, datetime, datetime]
#: A named, bounded, resumable retention pass the production worker schedules.
MaintenanceOperation = tuple[str, Callable[[], Awaitable[Any]]]


@runtime_checkable
class ReportingProductionProjection(Protocol):
    """The v2 status projection a production store builds over itself."""

    ledger: Any
    outbox: Any
    escalation: ReportingDeliveryEscalation | None
    consumer_status_enabled: bool

    @property
    def policy(self) -> dict[str, Any]: ...

    async def rebuild_one(self) -> StatusTurn: ...

    async def sweep_one(self) -> StatusTurn: ...

    async def activate(self, *, account_id: str) -> bool: ...


@runtime_checkable
class ReportingProductionStore(
    ReportingLedgerStore,
    ReportingReconciliationStore,
    ReportingMaterializerStore,
    ReportingReceiptBatchStore,
    ReportingFeedStore,
    Protocol,
):
    """A ledger that can carry the production composition.

    It is a Core ledger, a reconciliation store, a materializer store, a
    receipt-batch store and a frozen feed store at once, plus the composition
    hooks below.
    """

    #: Set by :class:`ReportingProductionSupport` when it takes ownership of the store.
    production_support: weakref.ReferenceType[ReportingProductionSupport] | None
    #: Set by the status projection; the composition checks it matches its policy.
    projection_read_policy: dict[str, Any]

    @property
    def durable(self) -> bool:
        """Whether storage survives the process. Only durable stores advertise production."""
        ...

    @property
    def clock(self) -> Callable[[], datetime] | None:
        """The explicit clock override, or ``None`` for the store's own time source."""
        ...

    @property
    def storage_identity(self) -> object:
        """The resource shared by every participant (a pool, or the store itself)."""
        ...

    @property
    def storage_open(self) -> bool:
        """Whether that resource still accepts work."""
        ...

    def is_sdk_production_store(self) -> bool:
        """Exactly a shipped production store, not a subclass."""
        ...

    def status_projection(
        self,
        *,
        consumer_status_enabled: bool,
        escalation: ReportingDeliveryEscalation | None,
        revision_ownership: bool,
    ) -> ReportingProductionProjection: ...

    def is_sdk_status_projection(self, projection: object) -> bool:
        """Exactly the shipped projection class for this store."""
        ...

    def projection_linked(self, projection: ReportingProductionProjection) -> bool:
        """Whether the projection's queue shares this store's storage."""
        ...

    def production_outboxes(
        self, projection: ReportingProductionProjection
    ) -> tuple[Any, Any, Any]:
        """The core, status and readiness queues, in that order."""
        ...

    def notification_workers_linked(self, workers: Sequence[ReportingNotificationWorker]) -> bool:
        """Whether every worker drives this store's exact queue classes over its storage."""
        ...

    async def validate_production_schema(
        self, *, notifications: bool, service_context: bool
    ) -> None:
        """Raise ``reporting_production_schema_unready`` unless the catalog is installed."""
        ...

    async def read_delivery_window(
        self, key: tuple[str, str], *, now: datetime
    ) -> tuple[SavedDeliveryWindow | None, datetime]:
        """The recorded window for ``(account_id, idempotency_key)`` and the store's instant."""
        ...

    async def reserve_delivery_attempt(
        self,
        activity: ReportingActivityStore,
        lease: DeliveryLease,
        *,
        request: ActivityRequest,
        now: datetime,
        queue: str,
        expected: tuple[str, str],
        horizon_seconds: int,
    ) -> tuple[WebhookAttempt | None, datetime | None, bool]:
        """Reserve one HTTP attempt and its immutable first-attempt deadline atomically."""
        ...

    def maintenance_operations(self) -> tuple[MaintenanceOperation, ...]:
        """Retention passes for the features this store opted into, in run order."""
        ...

    async def activate_production(self, *, account_id: str) -> bool: ...

    async def admit_production_configuration(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        offering_id: str,
        service_context: ReportingProductionSourceContext | None = None,
    ) -> None: ...
