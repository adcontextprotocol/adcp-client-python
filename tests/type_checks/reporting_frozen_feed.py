"""One optional feed participant composes with the approved receipt/materializer."""

from typing import Any

from typing_extensions import assert_type

from adcp.reporting.feed import (
    InMemoryReportingFeedStore,
    PgReportingFeedStore,
    ReportingFeedError,
    ReportingFeedSnapshot,
    ReportingFeedStore,
)
from adcp.reporting.ledger import ReportingDeliveryPrincipal, ReportingLedgerStore
from adcp.reporting.materializer import ReportingMaterializerStore
from adcp.reporting.receipts import (
    ReceiptAccountResolver,
    ReportingReceiptBatchStore,
    ReportingReceiptHandler,
)
from adcp.server import ToolContext
from adcp.server.base import NotImplementedResponse


async def adopter(
    postgres: PgReportingFeedStore,
    memory: InMemoryReportingFeedStore,
    caller: ReportingDeliveryPrincipal,
    request: dict[str, Any],
    resolve_account: ReceiptAccountResolver,
    context: ToolContext,
) -> ReportingMaterializerStore:
    await postgres.create_schema()
    feed: ReportingFeedStore = postgres
    feed = memory
    receipts: ReportingReceiptBatchStore = postgres
    receipts = memory
    ledger: ReportingLedgerStore = postgres
    ledger = memory
    materializer: ReportingMaterializerStore = postgres
    materializer = memory
    assert_type(await feed.reporting_feed_ready(), bool)
    assert_type(await feed.read_reporting_feed(request, caller=caller), dict[str, Any])

    async def reauthorize() -> None:
        account = await resolve_account(request["account"], context, caller.consumer_id)
        if account != caller.account_id:
            raise ReportingFeedError("UNAUTHORIZED")

    assert_type(
        await feed.read_reporting_feed(request, caller=caller, reauthorize=reauthorize),
        dict[str, Any],
    )
    assert_type(
        await feed.read_reporting_feed_snapshot("snapshot", caller=caller),
        ReportingFeedSnapshot | None,
    )
    assert_type(await receipts.ingest_receipt_batch(request, caller=caller), dict[str, Any])
    await ledger.list_configurations(account_id=caller.account_id)
    handler = ReportingReceiptHandler(postgres, resolve_account=resolve_account)
    assert_type(handler.reporting_feed_store, ReportingFeedStore | None)
    assert_type(
        await handler.get_reporting_status(request, context),
        dict[str, Any] | NotImplementedResponse,
    )
    return materializer
