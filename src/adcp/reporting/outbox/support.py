"""Independent reporting and list-accounts activity capability gates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.outbox.activity import ReportingActivityProjector
from adcp.reporting.outbox.durable import (
    ReportingDurableNotificationLedger,
    ReportingDurableStatusStore,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.store import ReportingLedgerStore
    from adcp.reporting.outbox.status_support import ReportingStatusSupport
    from adcp.reporting.outbox.worker import ReportingNotificationWorker


_ProofKey = tuple[str, tuple[int, ...], str]


@dataclass
class _SchemaFlight:
    key: _ProofKey
    epoch: int
    task: asyncio.Task[bool] | None = None


@dataclass
class _SchemaValidation:
    epoch: int = 0
    positive: _ProofKey | None = None
    flight: _SchemaFlight | None = None


def _consume_schema_failure(task: asyncio.Task[bool]) -> None:
    # A canceled waiter does not cancel other callers' proof. If every waiter
    # leaves, retrieve the result without logging catalog/driver exceptions.
    if not task.cancelled():
        task.exception()


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
    status: ReportingStatusSupport | None = None
    _schema_validation: _SchemaValidation = field(
        default_factory=_SchemaValidation, init=False, repr=False, compare=False
    )

    def invalidate_schema_validation(self) -> None:
        """Discard schema evidence before controlled DDL, after draining callers.

        An older in-flight scan cannot publish into the new epoch. A fresh
        support/server instance after migration is the normal deployment path;
        serving-time DDL is not automatically detected by discovery.
        """
        state = self._schema_validation
        state.epoch += 1
        state.positive = None
        state.flight = None

    async def _schema_proven(
        self, ledger: ReportingDurableNotificationLedger, *, composite: bool
    ) -> bool:
        assert self.projector is not None
        wiring: tuple[object, ...] = (
            self.worker,
            self.ledger,
            self.projector,
            self.projector.store,
            self.worker.outbox,
            self.worker.activity,
            self.worker.cipher,
            self.worker.subscriptions,
            self.worker.signing,
            ledger.storage_identity,
        )
        if composite:
            assert self.status is not None
            wiring += (
                self.status,
                self.status.store,
                self.status.worker,
                self.status.worker.outbox,
            )
        key: _ProofKey = (
            "B+C" if composite else "B",
            tuple(id(component) for component in wiring),
            ledger.activity_schema_contract(composite=composite),
        )
        state = self._schema_validation
        if state.positive == key:
            return True
        flight = state.flight
        if flight is None:
            flight = _SchemaFlight(key, state.epoch)
            state.flight = flight
            flight.task = asyncio.create_task(
                self._scan_schema(ledger, flight, composite=composite)
            )
            flight.task.add_done_callback(_consume_schema_failure)
        task = flight.task
        if task is None or flight.key != key or task.get_loop() is not asyncio.get_running_loop():
            # Overlapping, differently wired/looped startup is not evidence.
            # Completed startup leaves no task or loop in this instance.
            return False
        if not await asyncio.shield(task):
            return False
        # Wiring may have changed while the catalog checkout was suspended.
        # Rerun the same cheap checks before accepting the completed proof.
        return await self.durable()

    async def _scan_schema(
        self,
        ledger: ReportingDurableNotificationLedger,
        flight: _SchemaFlight,
        *,
        composite: bool,
    ) -> bool:
        state = self._schema_validation
        try:
            await ledger.validate_activity_schema(composite=composite)
            if state.epoch != flight.epoch or state.flight is not flight:
                return False
            state.positive = flight.key
            return True
        finally:
            if state.flight is flight:
                state.flight = None

    async def durable(self) -> bool:
        from adcp.reporting.ledger.delivery import InMemoryReportingReconciliationStore
        from adcp.reporting.ledger.store import InMemoryReportingLedgerStore
        from adcp.reporting.outbox.memory import InMemoryReportingOutbox
        from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
        from adcp.reporting.outbox.worker import ReportingNotificationWorker

        if self.projector is None or self.worker.activity is None:
            return False
        if self.status is not None:
            return await self._composite_durable()
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
        # A durable ledger proves its own queue and storage identity.
        ledger = self.ledger
        if not isinstance(ledger, ReportingDurableNotificationLedger):
            raise ReportingNotificationError("activity_chain_unready")
        ledger.validate_activity_chain(self.worker.outbox)
        return await self._schema_proven(ledger, composite=False)

    async def _composite_durable(self) -> bool:
        """Only the closed B+C read/purge union may replace the original B reader."""
        assert self.status is not None
        if not self.status.scheduled:
            return False
        from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
        from adcp.reporting.outbox.worker import ReportingNotificationWorker

        store = self.status.store
        if not isinstance(store, ReportingDurableStatusStore):
            return False
        reader = self.projector.store if self.projector is not None else None
        ledger = self.ledger
        if (
            type(self.worker) is not ReportingNotificationWorker
            or not isinstance(ledger, ReportingDurableNotificationLedger)
            or type(self.worker.cipher) is not ReportingEnvelopeCipher
            or type(self.status.worker) is not ReportingNotificationWorker
            or type(self.projector) is not ReportingActivityProjector
            or id(self.worker.activity) != id(self.worker.outbox)
            or id(self.status.worker.activity) != id(store.outbox)
            or id(self.status.worker.outbox) != id(store.outbox)
            or ledger is not store.ledger
            or self.worker.cipher is not self.status.worker.cipher
            or self.worker.subscriptions is not self.status.worker.subscriptions
            or self.worker.signing is not self.status.worker.signing
        ):
            raise ReportingNotificationError("activity_chain_unready")
        # Exact queue, union-reader and storage identity checks are the ledger's.
        ledger.validate_composite_activity_chain(
            outbox=self.worker.outbox, status_store=store, reader=reader
        )
        return await self._schema_proven(ledger, composite=True)

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
