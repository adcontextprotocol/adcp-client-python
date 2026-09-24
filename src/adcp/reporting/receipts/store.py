"""Optional batch participant; no change to any required legacy store protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.receipts.capture import ReportingReceiptBoundary


@runtime_checkable
class ReportingReceiptBatchStore(Protocol):
    """Trusted callers only. Each ordinal co-commits with all of its domain effects."""

    async def ingest_receipt_batch(
        self, request: dict[str, Any], *, caller: ReportingDeliveryPrincipal
    ) -> dict[str, Any]: ...


@runtime_checkable
class ReportingReceiptCaptureStore(Protocol):
    async def read_receipt_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingReceiptBoundary, ...]: ...
