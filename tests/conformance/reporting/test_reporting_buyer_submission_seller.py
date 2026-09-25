"""The buyer retains actual reviewed seller mixed-batch outcomes unchanged."""

from __future__ import annotations

import pytest

from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.submissions import (
    InMemoryReportingSubmissionIntentStore,
    PgReportingSubmissionIntentStore,
    ReportingReceiptFailureCode,
    ReportingSubmissionScope,
    submit_reporting_receipts,
)
from adcp.types import ReportingAdjustmentReceipt, ReportingReceipt, SyncReportingReceiptsResponse
from adcp.types.core import TaskResult, TaskStatus

from ._buyer_submission_support import TrustedAuthorizer
from ._receipt_support import adjustment_for, receipt_case, receipt_harness


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_seller_stale_rejected_leaf_and_accepted_terminality_remain_item_outcomes(backend):
    async with receipt_harness(backend) as h:
        s = await receipt_case(h)
        scope = ReportingSubmissionScope("seller", s.obligation.account_id, s.binding.consumer_id)
        if h.pool is None:
            buyer = InMemoryReportingSubmissionIntentStore()
        else:
            buyer = PgReportingSubmissionIntentStore(pool=h.pool)
            await buyer.create_schema()

        class Client:
            async def sync_reporting_receipts(self, request):
                response = await h.store.ingest_receipt_batch(
                    request.model_dump(mode="json", exclude_none=True), caller=s.binding.principal
                )
                return TaskResult(
                    status=TaskStatus.COMPLETED,
                    data=SyncReportingReceiptsResponse.model_validate(response),
                )

        client = Client()
        authorizer = TrustedAuthorizer(client, scope)
        original = receipt_to_wire(s.receipt)
        rejected = ReportingReceipt.model_validate(
            {**original, "status": "rejected", "rejection_codes": ["LOAD_FAILED"]}
        )
        first = await submit_reporting_receipts(
            client, authorizer=authorizer, store=buyer, receipts=[rejected]
        )
        assert not first.pending and first.outcomes[0].receipt.status == "rejected"
        second_rejected = rejected.model_copy(
            update={
                "reporting_receipt_id": "buyer-rejected-replacement-2",
                "supersedes_reporting_receipt_id": rejected.reporting_receipt_id,
            }
        )
        second = await submit_reporting_receipts(
            client, authorizer=authorizer, store=buyer, receipts=[second_rejected]
        )
        assert (
            second.outcomes[0].receipt.reporting_receipt_id == second_rejected.reporting_receipt_id
        )
        stale = ReportingReceipt.model_validate(
            {
                **original,
                "reporting_receipt_id": "buyer-stale-replacement-3",
                "supersedes_reporting_receipt_id": rejected.reporting_receipt_id,
            }
        )
        adjustment = ReportingAdjustmentReceipt.model_validate(await adjustment_for(h, s))
        mixed = await submit_reporting_receipts(
            client, authorizer=authorizer, store=buyer, receipts=[adjustment, stale]
        )
        assert not mixed.pending
        assert [outcome.result for outcome in mixed.outcomes] == ["recorded", "failed"]
        assert mixed.outcomes[1].error_codes == (
            ReportingReceiptFailureCode.REPORTING_RECORD_UNAVAILABLE,
        )
        accepted = ReportingReceipt.model_validate(
            {
                **original,
                "reporting_receipt_id": "buyer-current-replacement-4",
                "supersedes_reporting_receipt_id": second_rejected.reporting_receipt_id,
            }
        )
        final = await submit_reporting_receipts(
            client, authorizer=authorizer, store=buyer, receipts=[accepted]
        )
        assert final.outcomes[0].receipt.status == "accepted"
        terminal = accepted.model_copy(
            update={
                "reporting_receipt_id": "buyer-after-terminal-5",
                "supersedes_reporting_receipt_id": accepted.reporting_receipt_id,
            }
        )
        failure = await submit_reporting_receipts(
            client, authorizer=authorizer, store=buyer, receipts=[terminal]
        )
        assert not failure.pending and not failure.submitted_receipts
        assert failure.outcomes[0].error_codes == (
            ReportingReceiptFailureCode.ACCEPTED_RECEIPT_TERMINAL,
        )
        # Old completed intents retain rejected, failed and accepted evidence;
        # the submission engine never edits a leaf or constructs a replacement.
        assert await buyer.get(scope, first.submission.submission_id) == first.submission
        assert await buyer.get(scope, mixed.submission.submission_id) == mixed.submission
        assert await buyer.get(scope, final.submission.submission_id) == final.submission
