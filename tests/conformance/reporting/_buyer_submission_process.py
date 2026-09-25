"""Independent buyer + actual seller PG worker, paused for external SIGKILL."""

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path


async def main():
    settings = json.loads(await asyncio.to_thread(sys.stdin.readline))
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

    from adcp.reporting.ledger import ReportingDeliveryPrincipal
    from adcp.reporting.receipts import PgReportingReceiptStore
    from adcp.reporting.submissions import (
        PgReportingSubmissionIntentStore,
        ReportingSubmissionScope,
        submit_reporting_receipts,
    )
    from adcp.types import (
        ReportingAdjustmentReceipt,
        ReportingReceipt,
        SyncReportingReceiptsResponse,
    )
    from adcp.types.core import TaskResult, TaskStatus

    origins = {}
    if settings.get("installed"):
        installed = settings["installed"]
        assert list(sys.version_info[:2]) == [3, 10]
        workspace = Path(installed["workspace"]).resolve()
        for name, digest in installed["modules"].items():
            module = importlib.import_module(name)
            origin = Path(module.__file__).resolve()
            assert hashlib.sha256(origin.read_bytes()).hexdigest() == digest
            assert "site-packages" in str(origin) and not origin.is_relative_to(workspace)
            origins[name] = str(origin)
        assert not any(Path(entry).resolve().is_relative_to(workspace) for entry in sys.path)

    async def pause(point):
        if settings.get("pause") == point:
            print(json.dumps({"point": point}), flush=True)
            command = json.loads(await asyncio.to_thread(sys.stdin.readline))
            assert command["continue"]

    class Connection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            result = await super().execute(query, params, **kwargs)
            if isinstance(query, str):
                for prefix, point in (
                    ("INSERT INTO reporting_buyer_submission_scopes", "scope_row"),
                    ("INSERT INTO reporting_buyer_submission_intents", "intent_row"),
                    ("UPDATE reporting_buyer_submission_intents", "confirmation_row"),
                ):
                    if query.startswith(prefix):
                        await pause(point)
            return result

    class BuyerStore(PgReportingSubmissionIntentStore):
        async def reserve(self, proposed):
            await pause("before_intent")
            state = await super().reserve(proposed)
            await pause("intent_committed")
            return state

        async def confirm(self, scope, submission_id, chunk, response):
            await pause("response_delivered")
            state = await super().confirm(scope, submission_id, chunk, response)
            await pause("confirmation_committed")
            return state

    # This is trusted worker configuration from the parent test's authenticated
    # fixture registry. Neither account nor principal is taken from a request.
    scope = ReportingSubmissionScope(**settings["trusted_scope"])
    async with AsyncConnectionPool(
        settings["conninfo"],
        kwargs=settings["kwargs"],
        min_size=1,
        max_size=1,
        connection_class=Connection,
        open=False,
    ) as pool:
        buyer = BuyerStore(pool=pool)
        seller = PgReportingReceiptStore(pool=pool)

        class Client:
            def __init__(self):
                self.calls = 0

            async def sync_reporting_receipts(self, request):
                self.calls += 1
                await pause("before_seller")
                response = await seller.ingest_receipt_batch(
                    request.model_dump(mode="json", exclude_none=True),
                    caller=ReportingDeliveryPrincipal(scope.account_id, scope.consumer_id),
                )
                await pause("seller_committed")
                return TaskResult(
                    status=TaskStatus.COMPLETED,
                    data=SyncReportingReceiptsResponse.model_validate(response),
                )

        client = Client()

        async def authorize(candidate):
            assert candidate is client and settings.get("authorized", True)
            return scope

        inputs = (
            None
            if settings.get("resume")
            else [
                (
                    ReportingReceipt
                    if "reporting_materialization_id" in item
                    else ReportingAdjustmentReceipt
                ).model_validate(item)
                for item in settings["receipts"]
            ]
        )
        result = await submit_reporting_receipts(
            client, authorizer=authorize, store=buyer, receipts=inputs
        )
        await pause("returned")
        print(
            json.dumps(
                {
                    "point": "done",
                    "pending": result.pending,
                    "proposal_deferred": result.proposal_deferred,
                    "outcomes": [outcome.result for outcome in result.outcomes],
                    "submitted": len(result.submitted_receipts),
                    "calls": client.calls,
                    "origins": origins,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
