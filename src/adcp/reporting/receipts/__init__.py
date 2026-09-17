"""Durable seller receipt ingress. Optional PG driver is imported lazily."""

from typing import TYPE_CHECKING, Any

from adcp.reporting.receipts.capture import ReportingReceiptBoundary
from adcp.reporting.receipts.errors import ReceiptErrorCode, ReportingReceiptError
from adcp.reporting.receipts.handler import ReceiptAccountResolver, ReportingReceiptHandler
from adcp.reporting.receipts.memory import InMemoryReportingReceiptStore
from adcp.reporting.receipts.store import ReportingReceiptBatchStore, ReportingReceiptCaptureStore

if TYPE_CHECKING:
    from adcp.reporting.receipts.pg import PgReportingReceiptStore

__all__ = [
    "InMemoryReportingReceiptStore",
    "PgReportingReceiptStore",
    "ReceiptAccountResolver",
    "ReceiptErrorCode",
    "ReportingReceiptBatchStore",
    "ReportingReceiptBoundary",
    "ReportingReceiptCaptureStore",
    "ReportingReceiptError",
    "ReportingReceiptHandler",
]


def __getattr__(name: str) -> Any:
    if name == "PgReportingReceiptStore":
        from adcp.reporting.receipts.pg import PgReportingReceiptStore

        return PgReportingReceiptStore
    raise AttributeError(name)
