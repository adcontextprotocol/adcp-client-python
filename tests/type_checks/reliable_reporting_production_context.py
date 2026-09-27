"""Installed public fixed-profile production bridge typing."""

from typing import Any

from adcp.reporting import ReportingAdapter, ReportingProductionOptions, ReportingServiceOffering
from adcp.reporting.ledger import ReportingProducer
from adcp.reporting.production import ReportingProductionSourceRegistry, ReportingProductionSupport
from adcp.reporting.service import ReliableReportingService, ReportingContextResolver
from adcp.server import ADCPHandler


def register(
    context: ReportingContextResolver, producer: ReportingProducer
) -> ReportingProductionSourceRegistry:
    registry = ReportingProductionSourceRegistry(account_context=context)
    registry.register("fixed-provider-profile", producer)
    return registry


def compose(
    production: ReportingProductionSupport, application: ADCPHandler[Any]
) -> ReliableReportingService:
    service = ReliableReportingService.from_production(production)
    service.install(application)
    return service


def compose_adapters(
    pool: Any,
    context: ReportingContextResolver,
    adapter: ReportingAdapter,
    options: ReportingProductionOptions,
    application: ADCPHandler[Any],
) -> ReliableReportingService:
    offering: ReportingServiceOffering = options.offerings[0]
    service = ReliableReportingService.postgres(
        pool=pool, account_context=context, production=options
    )
    service.sources.register(offering.adapter, adapter)
    service.install(application)
    return service
