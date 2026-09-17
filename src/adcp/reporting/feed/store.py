"""Optional feed participant. All legacy required protocols remain unchanged."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from adcp.reporting.feed.snapshot import ReportingFeedSnapshot
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal


@runtime_checkable
class ReportingFeedStore(Protocol):
    """Trusted caller seam. The mounted handler reauthorizes every request."""

    async def read_reporting_feed(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingDeliveryPrincipal,
        consumer_status_enabled: bool = False,
    ) -> dict[str, Any]: ...

    async def read_reporting_feed_snapshot(
        self,
        snapshot_id: str,
        *,
        caller: ReportingDeliveryPrincipal,
    ) -> ReportingFeedSnapshot | None: ...

    async def reporting_feed_ready(self) -> bool: ...
