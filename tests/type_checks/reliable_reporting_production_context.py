"""Installed public fixed-profile production bridge typing."""

from typing import Any

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
