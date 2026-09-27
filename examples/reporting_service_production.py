"""Compose PostgreSQL reporting from registered adapters and trusted providers.

See docs/reliable-reporting-service.md for ReportingProductionOptions inputs.
The service owns the graph and source storage; the caller owns the pool and
provider clients. Existing hand-built graphs can still use from_production().
"""

from collections.abc import Mapping
from typing import Any

from adcp.reporting import ReliableReportingService, ReportingAdapter, ReportingProductionOptions
from adcp.reporting.service import ReportingContextResolver
from adcp.server import ADCPHandler


def compose_service(
    *,
    pool: Any,
    adapters: Mapping[str, ReportingAdapter],
    production: ReportingProductionOptions,
    account_context: ReportingContextResolver,
    application: ADCPHandler[Any],
) -> tuple[ReliableReportingService, ADCPHandler[Any]]:
    """Register before mounting; start/close own workers, while pools stay borrowed.

    Each stable name identifies a fixed ReportingServiceOffering.profile. Account
    resolution must return its currency, scope, metric set and source offerings.
    Recovery uses persisted generation facts and the provider's live account
    binding; it does not call account_context or enumerate accounts.

    Mount the returned exact handler on MCP and A2A, then use service.start and
    service.close as lifespan hooks. Retain the same event loop until shutdown
    settles; configure owned pools with ReportingServiceResource when needed.
    Reporting admission comes from the typed sync_accounts task, never configure.
    """
    service = ReliableReportingService.postgres(
        pool=pool, account_context=account_context, production=production
    )
    for name, adapter in adapters.items():
        service.sources.register(name, adapter)
    handler = service.install(application)
    return service, handler
