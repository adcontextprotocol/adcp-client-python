"""Shared buyer intent vectors; PostgreSQL uses isolated real schemas."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from adcp.reporting.submissions import (
    InMemoryReportingSubmissionIntentStore,
    PgReportingSubmissionIntentStore,
    ReportingReceiptSubmissionClient,
    ReportingSubmissionIntentStore,
    ReportingSubmissionReceipt,
    ReportingSubmissionScope,
)
from adcp.types import (
    ReportingAdjustmentReceipt,
    ReportingReceipt,
    SyncReportingReceiptsRequest,
    SyncReportingReceiptsResponse,
)
from adcp.types.core import TaskResult, TaskStatus

from ._generation_support import isolated_reporting_pool

SCOPE = ReportingSubmissionScope("https://seller.example.test", "account-a", "buyer-a")
OBSERVED = "2026-09-01T01:00:00Z"
RECEIVED = "2026-09-01T02:00:00Z"
SECRET = "private provider diagnostic: PRIVATE_SENTINEL"


def receipt(index: int = 0, *, adjustment: bool = False) -> ReportingSubmissionReceipt:
    if adjustment:
        return ReportingAdjustmentReceipt.model_validate(
            {
                "reporting_receipt_id": f"adjustment-receipt-{index:06d}",
                "reporting_adjustment_id": f"adjustment-{index}",
                "adjusts_reporting_revision_id": f"revision-{index}",
                "status": "accepted",
                "observed_adjustment_sha256": "b" * 64,
                "observed_at": OBSERVED,
            }
        )
    return ReportingReceipt.model_validate(
        {
            "reporting_receipt_id": f"revision-receipt-{index:06d}",
            "reporting_obligation_id": f"obligation-{index}",
            "reporting_revision_id": f"revision-{index}",
            "reporting_materialization_id": f"materialization-{index}",
            "status": "accepted",
            "verification_profile": "manifest_checksums",
            "observed_row_count": 0,
            "observed_control_totals": [],
            "observed_manifest_sha256": "a" * 64,
            "observed_at": OBSERVED,
        }
    )


def mixed(count: int = 3) -> list[ReportingSubmissionReceipt]:
    return [receipt(i, adjustment=i % 2 == 0) for i in range(count)]


def response_for(
    request: SyncReportingReceiptsRequest,
    *,
    fail: set[str] | None = None,
    reverse: bool = False,
    result: str = "recorded",
) -> SyncReportingReceiptsResponse:
    wire = request.model_dump(mode="json", exclude_none=True)
    results = []
    for kind, name in (("receipt", "receipts"), ("adjustment_receipt", "adjustment_receipts")):
        for item in wire.get(name, []):
            if item["reporting_receipt_id"] in (fail or set()):
                results.append(
                    {
                        "result": "failed",
                        "reporting_receipt_id": item["reporting_receipt_id"],
                        "errors": [
                            {"code": "REPORTING_RECORD_UNAVAILABLE", "message": SECRET},
                            {"code": "UNRECOGNIZED_PRIVATE_SENTINEL", "message": SECRET},
                        ],
                    }
                )
            else:
                results.append({"result": result, kind: {**item, "received_at": RECEIVED}})
    if reverse:
        results.reverse()
    return SyncReportingReceiptsResponse.model_validate({"results": results})


class ReceiptClient:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail = fail

    async def sync_reporting_receipts(
        self, request: SyncReportingReceiptsRequest
    ) -> TaskResult[SyncReportingReceiptsResponse]:
        self.requests.append(request.model_dump(mode="json", exclude_none=True))
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=response_for(request, fail=self.fail, reverse=True),
        )


class TrustedAuthorizer:
    """A fixture registry entry bound to a client, not to request parameters."""

    def __init__(
        self, client: ReportingReceiptSubmissionClient, scope: ReportingSubmissionScope = SCOPE
    ) -> None:
        self.client = client
        self.scope = scope
        self.allowed = True
        self.calls = 0

    async def __call__(self, client: ReportingReceiptSubmissionClient) -> ReportingSubmissionScope:
        self.calls += 1
        if client is not self.client or not self.allowed:
            raise RuntimeError(SECRET)
        return self.scope


@dataclass
class IntentHarness:
    store: ReportingSubmissionIntentStore
    pool: Any = None


@pytest.fixture(params=["memory", "postgres"])
async def intent_store(request: pytest.FixtureRequest) -> AsyncIterator[IntentHarness]:
    if request.param == "memory":
        yield IntentHarness(InMemoryReportingSubmissionIntentStore())
    else:
        async with isolated_reporting_pool() as pool:
            store = PgReportingSubmissionIntentStore(pool=pool)
            await store.create_schema()
            yield IntentHarness(store, pool)
