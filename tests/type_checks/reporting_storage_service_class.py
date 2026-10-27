"""Adopter-facing type contract: the Postgres factory returns the requested subclass."""

from typing import Any

from adcp_reporting_storage import postgres_reliable_reporting_service

from adcp.reporting import ReliableReportingService


class HostReportingService(ReliableReportingService):
    pass


def build(pool: Any, account_context: Any) -> HostReportingService:
    return postgres_reliable_reporting_service(
        pool=pool, account_context=account_context, service_class=HostReportingService
    )


def build_default(pool: Any, account_context: Any) -> ReliableReportingService:
    return postgres_reliable_reporting_service(pool=pool, account_context=account_context)
