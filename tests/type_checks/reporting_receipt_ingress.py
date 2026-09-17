"""Additive receipt composition keeps existing stores and structural protocols."""

from typing import Any

from typing_extensions import assert_type

from adcp.reporting.ledger import ReportingDeliveryPrincipal
from adcp.reporting.materializer import ReportingMaterializerStore
from adcp.reporting.receipts import (
    InMemoryReportingReceiptStore,
    PgReportingReceiptStore,
    ReceiptAccountResolver,
    ReportingReceiptBatchStore,
    ReportingReceiptBoundary,
    ReportingReceiptCaptureStore,
    ReportingReceiptHandler,
)


async def adopter(
    postgres: PgReportingReceiptStore,
    memory: InMemoryReportingReceiptStore,
    caller: ReportingDeliveryPrincipal,
    request: dict[str, Any],
    resolve_account: ReceiptAccountResolver,
) -> ReportingMaterializerStore:
    store: ReportingReceiptBatchStore = postgres
    store = memory
    capture: ReportingReceiptCaptureStore = postgres
    capture = memory
    materializer: ReportingMaterializerStore = postgres
    materializer = memory
    assert_type(await store.ingest_receipt_batch(request, caller=caller), dict[str, Any])
    assert_type(
        await capture.read_receipt_boundaries(caller=caller), tuple[ReportingReceiptBoundary, ...]
    )
    handler = ReportingReceiptHandler(store, resolve_account=resolve_account)
    assert_type(handler.receipt_store, ReportingReceiptBatchStore)
    assert_type(await postgres.receipt_ingestion_ready(), bool)
    return materializer
