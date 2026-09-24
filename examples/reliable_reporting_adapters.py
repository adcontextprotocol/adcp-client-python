"""Minimal sync and async adapters for ReliableReportingService.

The provider clients and normalizers are application protocols here so this
example imports no vendor SDK. Replace them with the corresponding GAM or
FreeWheel client in an adopter application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from adcp.reporting.inline_source import InlineFetchResult
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    ReportingSourceSliceRequestV1,
)


@dataclass(frozen=True)
class ProviderReport:
    rows: Sequence[Mapping[str, Any]]
    data_through: datetime
    provisional_until: datetime | None = None


class GAMClient(Protocol):
    def run_report(
        self,
        *,
        network_code: str,
        start: datetime,
        end: datetime,
        metrics: Sequence[str],
    ) -> ProviderReport: ...


class FreeWheelClient(Protocol):
    async def fetch_delivery(
        self, *, network_id: str, start: datetime, end: datetime
    ) -> ProviderReport: ...


class GAMReportingAdapter:
    """A synchronous provider client; the SDK moves the call off-loop."""

    def __init__(self, client: GAMClient, capabilities: ReportingSourceCapabilitiesV1) -> None:
        self.client = client
        self.capabilities = capabilities

    def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        report = self.client.run_report(
            network_code=str(request.identity.source_scope["network_code"]),
            start=request.period.start,
            end=request.period.end,
            metrics=request.requested_metrics,
        )
        return InlineFetchResult(
            rows=report.rows,
            data_through=report.data_through,
            provisional_until=report.provisional_until,
        )


class FreeWheelReportingAdapter:
    """An asynchronous provider client uses the identical adapter surface."""

    def __init__(
        self, client: FreeWheelClient, capabilities: ReportingSourceCapabilitiesV1
    ) -> None:
        self.client = client
        self.capabilities = capabilities

    async def fetch_slice(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
        report = await self.client.fetch_delivery(
            network_id=str(request.identity.source_scope["network_id"]),
            start=request.period.start,
            end=request.period.end,
        )
        return InlineFetchResult(
            rows=report.rows,
            data_through=report.data_through,
            provisional_until=report.provisional_until,
        )
