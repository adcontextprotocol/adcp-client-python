"""What the notification layer asks of a database-backed ledger.

``adcp`` ships the in-memory stores and checks their composition inline. Every
other ledger (the PostgreSQL store in ``adcp-reporting-storage``, or an
adopter's own) answers the same questions through these protocols, so the
capability checks in :mod:`adcp.reporting.outbox` never import a storage
implementation. Each ``validate_*`` method raises
:class:`~adcp.reporting.ledger.notification_models.ReportingNotificationError`
with the code the inline checks use, and a store that does not implement a
protocol is treated as not durable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.reporting.outbox.worker import ReportingNotificationWorker

__all__ = ["ReportingDurableNotificationLedger", "ReportingDurableStatusStore"]


@runtime_checkable
class ReportingDurableNotificationLedger(Protocol):
    """A ledger whose notification outbox shares its durable storage."""

    @property
    def storage_identity(self) -> object:
        """The storage resource every participant must share; compared by identity."""
        ...

    @property
    def notifications_enabled(self) -> bool: ...

    @property
    def durable_reconciliation(self) -> bool:
        """Whether this ledger froze destination bindings transactionally (Managed tier)."""
        ...

    async def validate_notification_chain(self, outbox: object) -> None:
        """Raise ``notification_chain_unready`` unless ``outbox`` is this ledger's queue.

        Includes the installed-schema check for the notification tables.
        """
        ...

    def validate_activity_chain(self, outbox: object) -> None:
        """Raise ``activity_chain_unready`` unless ``outbox`` is this ledger's activity queue."""
        ...

    def validate_composite_activity_chain(
        self, *, outbox: object, status_store: object, reader: object
    ) -> None:
        """Raise ``activity_chain_unready`` unless the B+C activity union is closed."""
        ...

    def activity_schema_contract(self, *, composite: bool) -> str:
        """A digest of the manifests ``validate_activity_schema`` checks."""
        ...

    async def validate_activity_schema(self, *, composite: bool) -> None:
        """Raise ``notification_schema_unready`` unless the activity catalog is installed."""
        ...


@runtime_checkable
class ReportingDurableStatusStore(Protocol):
    """A status-notification store whose queue shares the ledger's durable storage."""

    #: The ledger the status queue reads; the activity chain requires the same object.
    ledger: Any
    #: The store's own status queue, which its worker must drive.
    outbox: Any

    async def status_queue_durable(self, worker: ReportingNotificationWorker) -> bool:
        """Whether ``worker`` drives this store's own durable queue with its schema installed."""
        ...

    async def managed_delivery_recorded(self, *, account_id: str) -> bool:
        """Whether the account holds a frozen destination binding (Managed tier)."""
        ...
