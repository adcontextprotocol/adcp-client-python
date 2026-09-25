"""Strict additive public buyer submission and unchanged checkpoint contracts."""

from collections.abc import Sequence

from adcp import ADCPClient
from adcp.reporting import ReportingCheckpointStore
from adcp.reporting.submissions import (
    InMemoryReportingSubmissionIntentStore,
    ReportingReceiptSubmissionClient,
    ReportingSubmissionAuthorizer,
    ReportingSubmissionIntentStore,
    ReportingSubmissionReceipt,
    ReportingSubmissionResult,
    ReportingSubmissionScope,
    submit_reporting_receipts,
)
from adcp.types import ReportingAdjustment, ReportingAdjustmentReceipt, ReportingReceipt


class ExistingCheckpoint:
    """An existing structural implementation still requires only get and put."""

    def __init__(self) -> None:
        self.receipts: dict[str, ReportingReceipt] = {}

    async def get(self, reporting_materialization_id: str) -> ReportingReceipt | None:
        return self.receipts.get(reporting_materialization_id)

    async def put(self, receipt: ReportingReceipt) -> None:
        self.receipts[receipt.reporting_materialization_id] = receipt


checkpoint: ReportingCheckpointStore = ExistingCheckpoint()
volatile_test_store: ReportingSubmissionIntentStore = InMemoryReportingSubmissionIntentStore()


class AuthorizedRegistryBinding:
    """Values come from the adopter's verified registry/account authorization."""

    def __init__(self, client: ADCPClient, resolved_scope: ReportingSubmissionScope) -> None:
        self.client = client
        self.resolved_scope = resolved_scope
        self.active = True

    async def __call__(self, client: ReportingReceiptSubmissionClient) -> ReportingSubmissionScope:
        if client is not self.client or not self.active:
            raise PermissionError("reporting access unavailable")
        return self.resolved_scope


async def submit_validated_plan(
    client: ADCPClient,
    registry: ReportingSubmissionAuthorizer,
    durable_store: ReportingSubmissionIntentStore,
    selected_receipts: Sequence[ReportingSubmissionReceipt],
) -> ReportingSubmissionResult:
    # Plan/history validation precedes this function. Never derive identity from
    # the selected receipts, or reconstruct new receipts on an uncertain retry.
    return await submit_reporting_receipts(
        client, authorizer=registry, store=durable_store, receipts=selected_receipts
    )


async def recover_uncertain_submission(
    client: ADCPClient,
    registry: ReportingSubmissionAuthorizer,
    durable_store: ReportingSubmissionIntentStore,
) -> ReportingSubmissionResult:
    return await submit_reporting_receipts(client, authorizer=registry, store=durable_store)


def adjustment_target(adjustment: ReportingAdjustment, receipt: ReportingAdjustmentReceipt) -> bool:
    # The supported curated imports were already present on the accepted base.
    return adjustment.reporting_adjustment_id == receipt.reporting_adjustment_id


def confirmed_count(result: ReportingSubmissionResult) -> int:
    receipts: tuple[ReportingSubmissionReceipt, ...] = result.submitted_receipts
    for outcome in result.outcomes:
        if outcome.result == "failed":
            for error in outcome.error_codes:
                assert isinstance(error.value, str)
        else:
            confirmed: ReportingSubmissionReceipt | None = outcome.receipt
            assert confirmed is not None
    return len(receipts)
