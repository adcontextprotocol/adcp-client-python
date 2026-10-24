"""The adapter-first Reliable Reporting service over a borrowed PostgreSQL pool."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from adcp.reporting.service import (
    ReliableReportingService,
    ReportingCallerResolver,
    ReportingContextResolver,
)
from adcp.reporting.service_production import ReportingProductionOptions

__all__ = ["postgres_reliable_reporting_service"]


def postgres_reliable_reporting_service(
    *,
    pool: Any,
    account_context: ReportingContextResolver,
    caller_resolver: ReportingCallerResolver | None = None,
    clock: Callable[[], datetime] | None = None,
    production: ReportingProductionOptions | None = None,
    **kwargs: Any,
) -> ReliableReportingService:
    """Create durable ledger, staging and seal stores over a borrowed pool.

    ``production`` additionally composes the managed pipeline, receipts and
    optional signed notifications when ``install`` freezes registered sources.
    Startup installs all SDK-owned schemas. Explicit source-store overrides
    remain the adopter's responsibility. No pool is opened or closed here.

    This is what ``ReliableReportingService.postgres(...)`` was before the
    storage implementations moved to this distribution.
    """
    from adcp_reporting_storage.inline_storage import PgReportingSealStore, PgReportingStagingStore
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore
    from adcp_reporting_storage.production.pg import PgReportingProductionStore

    store = (
        PgReportingProductionStore(pool=pool, clock=clock, notifications=production.notifications)
        if production is not None
        else PgReportingLedgerStore(pool=pool, clock=clock)
    )
    staging = PgReportingStagingStore(pool=pool)
    return ReliableReportingService(
        store=store,
        account_context=account_context,
        caller_resolver=caller_resolver,
        clock=clock,
        production=production,
        staging=staging,
        seals=PgReportingSealStore(pool=pool),
        initialize_source_storage=staging.create_schema,
        **kwargs,
    )
