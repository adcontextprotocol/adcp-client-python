"""Frozen authorized reporting feeds; PostgreSQL remains an optional lazy import."""

from typing import TYPE_CHECKING, Any

from adcp.reporting.feed.errors import FeedErrorCode, ReportingFeedError
from adcp.reporting.feed.memory import InMemoryReportingFeedStore
from adcp.reporting.feed.snapshot import ReportingFeedRecord, ReportingFeedSnapshot
from adcp.reporting.feed.store import ReportingFeedStore

if TYPE_CHECKING:
    from adcp.reporting.feed.pg import PgReportingFeedStore

__all__ = [
    "FeedErrorCode",
    "InMemoryReportingFeedStore",
    "PgReportingFeedStore",
    "ReportingFeedError",
    "ReportingFeedRecord",
    "ReportingFeedSnapshot",
    "ReportingFeedStore",
]


def __getattr__(name: str) -> Any:
    if name == "PgReportingFeedStore":
        from adcp.reporting.feed.pg import PgReportingFeedStore

        return PgReportingFeedStore
    raise AttributeError(name)
