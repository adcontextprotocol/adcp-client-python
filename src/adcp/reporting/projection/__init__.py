"""Versioned private reporting projection, independent of optional delivery workers."""

from typing import TYPE_CHECKING, Any

from adcp.reporting.projection.capture import ReportingProjectionInput
from adcp.reporting.projection.memory import (
    InMemoryReportingProjectionOutbox,
    InMemoryReportingProjectionStore,
    InMemoryReportingStatusProjection,
)

if TYPE_CHECKING:
    from adcp.reporting.projection.notifications import PgReportingProjectionOutbox
    from adcp.reporting.projection.pg import PgReportingProjectionStore, PgReportingStatusProjection

__all__ = [
    "InMemoryReportingProjectionOutbox",
    "InMemoryReportingProjectionStore",
    "InMemoryReportingStatusProjection",
    "PgReportingProjectionOutbox",
    "PgReportingProjectionStore",
    "PgReportingStatusProjection",
    "ReportingProjectionInput",
]


def __getattr__(name: str) -> Any:
    if name in {"PgReportingProjectionStore", "PgReportingStatusProjection"}:
        from adcp.reporting.projection import pg

        return getattr(pg, name)
    if name == "PgReportingProjectionOutbox":
        from adcp.reporting.projection.notifications import PgReportingProjectionOutbox

        return PgReportingProjectionOutbox
    raise AttributeError(name)
