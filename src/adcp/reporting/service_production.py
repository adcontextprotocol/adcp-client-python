"""Domain inputs for the adapter-first managed reporting factory.

The service builds the SDK components. Adopters still supply their actual
destination, verifier, authenticated account task and live source bindings;
these declarations cannot turn an in-memory destination into a durable one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from adcp.reporting.ledger import ProducerOfferings, ReportingDeliveryEscalation, ReportingProducer
from adcp.reporting.materializer import (
    ReportingDestinationIO,
    ReportingMaterializerService,
    ReportingRevisionVerifierRegistry,
    ReportingVerificationKey,
)
from adcp.reporting.outbox.routing import ReportingEnvelopeCipher, ReportingSubscriptionResolver
from adcp.reporting.outbox.worker import ReportingNotificationWorker
from adcp.reporting.production import (
    ReportingProductionConfigurationTask,
    ReportingProductionDestination,
    ReportingProductionOffering,
    ReportingProductionSigning,
    ReportingProductionSourceRegistry,
    ReportingProductionSupport,
    production_notification_workers,
)
from adcp.reporting.production.memory import InMemoryReportingProductionStore
from adcp.reporting.projection import InMemoryReportingStatusProjection, PgReportingStatusProjection
from adcp.reporting.receipts import ReceiptAccountResolver
from adcp.types import ReportingDeliveryOffering

if TYPE_CHECKING:
    from adcp.decisioning.registry import BuyerAgentRegistry
    from adcp.reporting.production.pg import PgReportingProductionStore
    from adcp.reporting.service import ReportingAdapterRegistry, ReportingContextResolver

__all__ = ["ReportingServiceOffering", "ReportingProductionOptions"]


@dataclass(frozen=True)
class ReportingServiceOffering:
    """Bind a public offering to one registered adapter's fixed source profile.

    Adapters may reuse local source offering IDs. Public offering IDs must be
    unique, and all offerings of one adapter must use the same profile and
    verifier. Account admission checks the resolved context against that
    profile and freezes the selected adapter for the configuration generation.
    """

    adapter: str
    offering: ReportingDeliveryOffering
    profile: ProducerOfferings
    source_offering_id: str
    verification_key: ReportingVerificationKey


@dataclass(frozen=True)
class ReportingProductionOptions:
    """Trusted inputs for ``ReliableReportingService.memory/postgres``.

    Register sources, then ``install(application)`` to compose and validate the
    graph. Mount the returned handler before starting the service. The factory
    owns its workers; the database pool and adopter providers remain borrowed.
    Receipt handlers and destination authorization use the existing production
    path. Push requires all three notification inputs and enabled notifications.
    """

    offerings: tuple[ReportingServiceOffering, ...]
    destination: ReportingProductionDestination
    registry: ReportingRevisionVerifierRegistry
    configuration_task: ReportingProductionConfigurationTask
    resolve_account: ReceiptAccountResolver
    buyer_agents: BuyerAgentRegistry | None = None
    notifications: bool = False
    subscriptions: ReportingSubscriptionResolver | None = None
    cipher: ReportingEnvelopeCipher | None = None
    signing: ReportingProductionSigning | None = None
    automated_recovery_window: timedelta = timedelta(hours=6)
    status_retention_days: int = 400
    poll_seconds: float = 0.25

    def __post_init__(self) -> None:
        object.__setattr__(self, "offerings", tuple(self.offerings))
        if not self.offerings:
            raise ValueError("production requires at least one source offering")
        notification_inputs = (self.subscriptions, self.cipher, self.signing)
        if any(value is not None for value in notification_inputs) and (
            not self.notifications or any(value is None for value in notification_inputs)
        ):
            raise ValueError("push requires notifications, subscriptions, cipher and signing")

    def _compose(
        self,
        *,
        store: InMemoryReportingProductionStore | PgReportingProductionStore,
        sources: ReportingAdapterRegistry,
        account_context: ReportingContextResolver,
        clock: Callable[[], datetime],
        escalation: ReportingDeliveryEscalation,
        consumer_status_enabled: bool,
    ) -> ReportingProductionSupport:
        from adcp.reporting.production.pg import PgReportingProductionStore

        if set(sources.names) != {item.adapter for item in self.offerings}:
            raise ValueError("every registered production adapter must have an offering")
        source_registry = ReportingProductionSourceRegistry(account_context=account_context)
        profiles: dict[str, ReportingServiceOffering] = {}
        producers: dict[str, ReportingProducer] = {}
        offerings: list[ReportingProductionOffering] = []
        for item in self.offerings:
            registered = sources.get(item.adapter)
            previous = profiles.get(item.adapter)
            if previous is not None and (
                previous.profile != item.profile
                or previous.verification_key != item.verification_key
            ):
                raise ValueError("one adapter must use one fixed source profile and verifier")
            if previous is None:
                producer = ReportingProducer(
                    source=registered.executor,
                    object_reader=registered.object_reader,
                    offerings=item.profile,
                    store=store,
                    revision_verifier=self.registry.require(item.verification_key),
                    escalation=escalation,
                    clock=clock,
                    worker_id=f"reporting-service:{item.adapter}",
                )
                profiles[item.adapter] = item
                producers[item.adapter] = producer
                source_registry.register(item.adapter, producer)
            offerings.append(
                ReportingProductionOffering(
                    item.offering,
                    producers[item.adapter],
                    item.verification_key,
                    item.source_offering_id,
                )
            )
        projection: InMemoryReportingStatusProjection | PgReportingStatusProjection
        if type(store) is PgReportingProductionStore:
            projection = PgReportingStatusProjection(
                store,
                consumer_status_enabled=consumer_status_enabled,
                escalation=escalation,
                revision_ownership=True,
            )
        elif type(store) is InMemoryReportingProductionStore:
            projection = InMemoryReportingStatusProjection(
                store,
                consumer_status_enabled=consumer_status_enabled,
                escalation=escalation,
                revision_ownership=True,
            )
        else:
            raise ValueError("production requires the factory's matching production store")
        workers: tuple[ReportingNotificationWorker, ...] = ()
        if self.subscriptions is not None and self.cipher is not None and self.signing is not None:
            workers = production_notification_workers(
                store,
                projection,
                subscriptions=self.subscriptions,
                cipher=self.cipher,
                signing=self.signing,
            )
        return ReportingProductionSupport(
            ReportingMaterializerService(
                store, ReportingDestinationIO(self.registry, self.destination), self.destination
            ),
            projection,
            offerings=tuple(offerings),
            source_registry=source_registry,
            configuration_task=self.configuration_task,
            resolve_account=self.resolve_account,
            buyer_agents=self.buyer_agents,
            automated_recovery_window=self.automated_recovery_window,
            status_retention_days=self.status_retention_days,
            notification_workers=workers,
            poll_seconds=self.poll_seconds,
        )
