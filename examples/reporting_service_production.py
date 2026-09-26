"""Own an existing B2 graph through the service lifecycle.

This explicit bridge still requires production providers, fixed source profiles
and the typed account task. It does not implement the separate adapter-first
factory, durable acquisition-envelope, or database-time fencing contracts.
"""

from collections.abc import Mapping
from typing import Any

from adcp.reporting.ledger import ReportingProducer
from adcp.reporting.materializer import ReportingMaterializerService
from adcp.reporting.production import (
    ReportingProductionConfigurationTask,
    ReportingProductionHandler,
    ReportingProductionOffering,
    ReportingProductionSourceRegistry,
    ReportingProductionSupport,
)
from adcp.reporting.projection import InMemoryReportingStatusProjection, PgReportingStatusProjection
from adcp.reporting.receipts import ReceiptAccountResolver
from adcp.reporting.service import ReliableReportingService, ReportingContextResolver
from adcp.server import ADCPHandler


def compose_service(
    *,
    materializer: ReportingMaterializerService,
    projection: InMemoryReportingStatusProjection | PgReportingStatusProjection,
    offerings: tuple[ReportingProductionOffering, ...],
    producers: Mapping[str, ReportingProducer],
    account_context: ReportingContextResolver,
    configuration_task: ReportingProductionConfigurationTask,
    resolve_account: ReceiptAccountResolver,
    application: ADCPHandler[Any],
) -> tuple[ReliableReportingService, ReportingProductionHandler]:
    """Register before mounting; start/close own workers, while pools stay borrowed.

    Each stable name identifies one fixed execution profile. Account resolution
    must return that profile's currency, scope, metric set and source offerings.
    Recovery uses persisted generation facts and the provider's live account
    binding; it does not call account_context or enumerate accounts.

    Mount the returned exact handler on MCP and A2A, then use service.start and
    service.close as lifespan hooks. Retain the same event loop until shutdown
    settles; configure owned pools with ReportingServiceResource when needed.
    Reporting admission comes from the typed sync_accounts task, never configure.
    """
    registry = ReportingProductionSourceRegistry(account_context=account_context)
    for name, producer in producers.items():
        registry.register(name, producer)
    support = ReportingProductionSupport(
        materializer,
        projection,
        offerings=offerings,
        configuration_task=configuration_task,
        resolve_account=resolve_account,
        source_registry=registry,
    )
    service = ReliableReportingService.from_production(support)
    service.install(application)
    return service, support.handler
