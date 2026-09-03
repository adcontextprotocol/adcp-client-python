from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest

from adcp.decisioning.capabilities import MediaBuy
from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingInspectionContext,
    ReportingObservation,
    build_reporting_receipt,
    evaluate_reporting_ledger,
    load_reporting_ledger,
    reconcile_reporting,
)
from adcp.types import ReportingDeliveryCapabilities
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.generated_poc.core.reporting_canonical_content_digest import (
    ReportingCanonicalContentDigest,
)
from adcp.types.generated_poc.core.reporting_control_total import ReportingControlTotal
from adcp.types.generated_poc.media_buy.get_reporting_status_request import (
    GetReportingStatusRequest,
)
from adcp.types.generated_poc.media_buy.get_reporting_status_response import (
    GetReportingStatusResponse,
)
from adcp.types.generated_poc.media_buy.sync_reporting_receipts_request import (
    SyncReportingReceiptsRequest,
)
from adcp.types.generated_poc.media_buy.sync_reporting_receipts_response import (
    SyncReportingReceiptsResponse,
)

PERIOD = {
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-09-01T00:00:00Z",
    "source_timezone": "UTC",
}
COVERAGE = {
    "status": "full",
    "evaluated_at": PERIOD["end"],
    "media_buy_ids": ["buy-1", "buy-2"],
    "fully_covered_media_buy_ids": ["buy-1", "buy-2"],
    "partially_covered_media_buy_ids": [],
    "unsupported_media_buy_ids": [],
    "unknown_media_buy_ids": [],
    "package_ids": [],
    "covered_package_ids": [],
    "unsupported_package_ids": [],
    "unknown_package_ids": [],
    "limitations": [],
}
TOTALS = [
    {
        "name": "impressions",
        "value": "4200",
        "value_type": "integer",
        "unit": "impressions",
    },
    {"name": "spend", "value": "7000.00", "value_type": "decimal", "unit": "USD"},
]
DIGEST = {
    "algorithm": "sha256",
    "value": "a" * 64,
    "canonicalization_id": "rows-v1",
    "canonicalization_uri": "https://schemas.example/canonicalization/rows-v1.json",
    "canonicalization_sha256": "b" * 64,
}


def test_capability_uses_public_reporting_delivery_model() -> None:
    reporting = ReportingDeliveryCapabilities.model_validate(
        {
            "supported": True,
            "managed_delivery": True,
            "reconciled_billing": True,
            "offerings": [
                {
                    "offering_id": "billing-daily",
                    "feed_purpose": "billing",
                    "report_definition_id": "billing-v1",
                    "report_definition_uri": "https://schemas.example/reporting/billing-v1.json",
                    "report_definition_sha256": "d" * 64,
                    "reporting_profile": {
                        "id": "billing-v1",
                        "version": "1",
                        "schema_uri": "https://schemas.example/reporting/billing-v1.json",
                        "schema_sha256": "c" * 64,
                        "grain": "media_buy_day",
                        "primary_keys": ["media_buy_id", "date"],
                        "canonicalization_id": "rows-v1",
                        "canonicalization_uri": "https://schemas.example/canonicalization/rows-v1.json",
                        "canonicalization_sha256": "b" * 64,
                    },
                    "schedule": {
                        "period_duration": "P1D",
                        "alignment": "utc",
                        "delivery_sla": "PT6H",
                    },
                    "supported_finality": ["official"],
                    "reconciliation_mode": "consumer_receipt",
                    "method": {
                        "pattern": "file_transfer",
                        "transport": "s3",
                        "orchestration": "producer_managed",
                        "destination_modes": ["existing"],
                        "provider": {"domain": "aws.amazon.com"},
                        "format": "jsonl",
                    },
                }
            ],
            "automated_recovery_window_seconds": 86400,
            "status_retention_days": 400,
            "resource_retention_days": 90,
            "supports_webhook_activity": True,
            "authorization_revocation_seconds": 3600,
        }
    )

    capabilities = MediaBuy(reporting_delivery=reporting)

    assert capabilities.reporting_delivery is reporting


REVISION = {
    "reporting_revision_id": "revision-august-official",
    "report_definition_id": "billing-v1",
    "report_definition_uri": "https://schemas.example/reporting/billing-v1.json",
    "report_definition_sha256": "d" * 64,
    "reporting_profile": "billing-v1",
    "schema_version": "1",
    "schema_uri": "https://schemas.example/billing-v1.json",
    "schema_sha256": "c" * 64,
    "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
    "schema_ref_policy": "local_fragment_only",
    "account_id": "account-1",
    "media_buy_ids": ["buy-1", "buy-2"],
    "coverage": COVERAGE,
    "period": PERIOD,
    "finality": "official",
    "finality_basis": "source_final",
    "finality_policy_id": "billing-v1-source-final",
    "finalized_at": "2026-09-02T00:00:00Z",
    "observed_at": "2026-09-02T00:00:00Z",
    "data_through": "2026-09-01T00:00:00Z",
    "data_through_precision": "exact",
    "row_count": 7,
    "control_totals": TOTALS,
    "canonical_content_digest": DIGEST,
    "created_at": "2026-09-02T00:00:00Z",
}


def _obligation(identifier: str = "obligation-billing") -> dict[str, object]:
    return {
        "reporting_obligation_id": identifier,
        "delivery_config_id": "billing-feed",
        "delivery_config_version": 1,
        "report_definition_id": "billing-v1",
        "feed_purpose": "billing",
        "reporting_profile": "billing-v1",
        "account_id": "account-1",
        "media_buy_ids": ["buy-1", "buy-2"],
        "coverage": COVERAGE,
        "scope_resolved_at": PERIOD["end"],
        "period": PERIOD,
        "expected_at": "2026-09-02T00:00:00Z",
        "schedule": {
            "period_duration": "P1M",
            "alignment": "billing_cycle",
            "delivery_sla": "P1D",
        },
        "destination_ref": f"destination-{identifier}",
        "required_finality": "official",
        "reconciliation_mode": "consumer_receipt",
        "reconciliation_status": "pending",
        "health": "waiting",
        "production_status": "published",
        "revision_count": 1,
        "materialization_count": 1,
        "successful_materialization_count": 1,
        "receipt_count": 0,
        "accepted_receipt_count": 0,
        "issues": [],
        "resource_retained_until": "2026-12-01T00:00:00Z",
    }


def _materialization(
    identifier: str = "materialization-billing",
    obligation_id: str = "obligation-billing",
) -> dict[str, object]:
    return {
        "reporting_materialization_id": identifier,
        "reporting_revision_id": REVISION["reporting_revision_id"],
        "reporting_obligation_id": obligation_id,
        "delivery_config_id": "billing-feed",
        "delivery_config_version": 1,
        "destination_ref": f"destination-{obligation_id}",
        "feed_purpose": "billing",
        "method": "dataset_share",
        "transport": "delta_sharing",
        "attempt": 1,
        "status": "available",
        "ready_at": "2026-09-02T00:00:05Z",
        "resource": {
            "resource_ref": f"resource-{identifier}",
            "kind": "dataset",
            "location": "share.billing",
            "native_version_ref": "version-42",
            "immutability": "native_version",
            "expires_at": "2026-12-01T00:00:00Z",
        },
        "verification": {
            "verified_at": "2026-09-02T00:00:05Z",
            "verification_path": "representative_consumer",
            "verification_profile": "canonical_digest",
            "row_count": 7,
            "control_totals": TOTALS,
            "canonical_content_digest": DIGEST,
        },
        "created_at": "2026-09-02T00:00:01Z",
    }


def _response(receipts: list[dict[str, object]] | None = None) -> dict[str, object]:
    receipts = receipts or []
    item = _obligation()
    if receipts:
        accepted_receipts = [receipt for receipt in receipts if receipt.get("status") == "accepted"]
        item.update(receipt_count=len(receipts), accepted_receipt_count=len(accepted_receipts))
        if accepted_receipts:
            item.update(reconciliation_status="accepted", health="complete")
        else:
            item.update(reconciliation_status="rejected", health="waiting")
    return {
        "status": "completed",
        "view": "periods",
        "ledger_snapshot_id": ("snapshot-after-receipt" if receipts else "snapshot-before-receipt"),
        "ledger_as_of": ("2026-09-02T00:01:01Z" if receipts else "2026-09-02T00:00:06Z"),
        "account_id": "account-1",
        "scope": {
            "period_start": PERIOD["start"],
            "period_end": PERIOD["end"],
            "scope_closed": True,
            "media_buy_ids": ["buy-1", "buy-2"],
            "all_accessible_media_buys": False,
            "delivery_config_generations": [
                {
                    "delivery_config_id": "billing-feed",
                    "delivery_config_version": 1,
                    "feed_purpose": "billing",
                }
            ],
            "feed_purposes": ["billing"],
            "finality": ["official"],
            "ledger_retained_from": "2026-07-01T00:00:00Z",
            "coverage_complete": True,
        },
        "periods": [item],
        "revisions": [deepcopy(REVISION)],
        "materializations": [_materialization()],
        "receipts": receipts,
        "pagination": {"has_more": False, "total_count": 3 + len(receipts)},
    }


class _Client:
    def __init__(self) -> None:
        self.recorded_receipt: dict[str, object] | None = None

    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]:
        response = _response([self.recorded_receipt] if self.recorded_receipt else [])
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=GetReportingStatusResponse.model_validate(response),
        )

    async def sync_reporting_receipts(
        self, request: SyncReportingReceiptsRequest
    ) -> TaskResult[SyncReportingReceiptsResponse]:
        self.recorded_receipt = request.receipts[0].model_dump(mode="json", exclude_none=True)
        self.recorded_receipt["received_at"] = "2026-09-02T00:01:00Z"
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=SyncReportingReceiptsResponse.model_validate(
                {"results": [{"result": "recorded", "receipt": self.recorded_receipt}]}
            ),
        )


@pytest.mark.asyncio
async def test_reconciles_billing_and_records_matching_receipt() -> None:
    client = _Client()
    inspections = 0

    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        nonlocal inspections
        inspections += 1
        if inspections == 1:
            raise OSError("transient warehouse read")
        return ReportingObservation(
            row_count=7,
            control_totals=[ReportingControlTotal.model_validate(item) for item in TOTALS],
            canonical_content_digest=ReportingCanonicalContentDigest.model_validate(DIGEST),
            consumer_commit_ref="buyer-ledger-42",
        )

    result = await reconcile_reporting(
        client,
        GetReportingStatusRequest.model_validate(
            {
                "account": {"account_id": "account-1"},
                "view": "periods",
                "period": {"start": PERIOD["start"], "end": PERIOD["end"]},
            }
        ),
        inspect,
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-2"),
                str(PERIOD["start"]),
                str(PERIOD["end"]),
            )
        ],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert result.definitive, result.obligations
    assert inspections == 2
    assert len(result.submitted_receipts) == 1
    assert result.submitted_receipts[0].status.value == "accepted"
    assert len(result.totals_by_revision) == 1
    assert result.totals_by_revision[0][0] == REVISION["reporting_revision_id"]


def test_consumer_billing_mismatch_creates_rejected_receipt() -> None:
    response = GetReportingStatusResponse.model_validate(_response())
    receipt = build_reporting_receipt(
        ReportingInspectionContext(
            response.periods[0],
            response.revisions[0],
            response.materializations[0],
        ),
        ReportingObservation(
            row_count=8,
            control_totals=[
                ReportingControlTotal.model_validate(
                    {
                        "name": "impressions",
                        "value": "4199",
                        "value_type": "integer",
                        "unit": "impressions",
                    }
                ),
                ReportingControlTotal.model_validate(TOTALS[1]),
            ],
            canonical_content_digest=ReportingCanonicalContentDigest.model_validate(
                {**DIGEST, "value": "d" * 64}
            ),
            consumer_commit_ref="buyer-ledger-disputed-42",
        ),
        reporting_receipt_id="reporting-receipt:billing-dispute",
        observed_at=datetime.fromisoformat("2026-09-02T00:01:00+00:00"),
    )

    assert receipt.status.value == "rejected"
    assert [code.root for code in receipt.rejection_codes or []] == [
        "ROW_COUNT_MISMATCH",
        "CONTROL_TOTAL_MISMATCH",
        "CANONICAL_DIGEST_MISMATCH",
    ]


@pytest.mark.asyncio
async def test_missing_expected_period_prevents_definitive_result() -> None:
    ledger = await load_reporting_ledger(
        _Client(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-2"),
                "2026-07-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
            )
        ],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert len(result.missing_expected_periods) == 1


@pytest.mark.asyncio
async def test_same_feed_period_cannot_hide_a_missing_campaign() -> None:
    ledger = await load_reporting_ledger(
        _Client(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-3"),
                str(PERIOD["start"]),
                str(PERIOD["end"]),
            )
        ],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert len(result.missing_expected_periods) == 1


@pytest.mark.asyncio
async def test_revision_campaign_scope_must_match_obligation() -> None:
    raw = _response()
    raw["revisions"][0]["media_buy_ids"] = ["buy-1"]
    raw["periods"][0].update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
    )

    class ScopeMismatchClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        ScopeMismatchClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert "REVISION_SCOPE_MISMATCH" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_missing_denominator_prevents_definitive_result() -> None:
    ledger = await load_reporting_ledger(
        _Client(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert not result.missing_expected_periods


@pytest.mark.asyncio
async def test_partial_reporting_coverage_prevents_definitive_result() -> None:
    raw = _response()
    partial = deepcopy(COVERAGE)
    partial.update(
        status="partial",
        fully_covered_media_buy_ids=["buy-1"],
        partially_covered_media_buy_ids=["buy-2"],
    )
    raw["periods"][0].update(
        coverage=partial,
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    raw["revisions"][0]["coverage"] = deepcopy(partial)

    class PartialCoverageClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        PartialCoverageClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert not result.definitive
    assert "REPORTING_COVERAGE_INCOMPLETE" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_incomplete_associated_history_prevents_definitive_result() -> None:
    raw = _response()
    periods = raw["periods"]
    assert isinstance(periods, list)
    assert isinstance(periods[0], dict)
    periods[0]["revision_count"] = 2

    class IncompleteClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        IncompleteClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert "ASSOCIATED_HISTORY_INCOMPLETE" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_one_revision_fans_out_without_double_counting_totals() -> None:
    raw = _response()
    first = _obligation("obligation-a")
    second = _obligation("obligation-b")
    for item in (first, second):
        item.update(
            reconciliation_mode="delivery_only",
            reconciliation_status="not_required",
            health="complete",
        )
    raw["periods"] = [first, second]
    raw["materializations"] = [
        _materialization("materialization-a", "obligation-a"),
        _materialization("materialization-b", "obligation-b"),
    ]
    raw["pagination"] = {"has_more": False, "total_count": 5}

    class FanoutClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        FanoutClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert result.definitive, result.obligations
    assert len(result.totals_by_revision) == 1


@pytest.mark.asyncio
async def test_mismatched_native_producer_evidence_is_not_definitive() -> None:
    raw = _response()
    obligation = raw["periods"][0]
    assert isinstance(obligation, dict)
    obligation.update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    attempt = raw["materializations"][0]
    assert isinstance(attempt, dict)
    verification = attempt["verification"]
    assert isinstance(verification, dict)
    verification.update(
        verification_profile="native_commit",
        verification_path="representative_consumer",
        native_commit_evidence={
            "native_version_ref": "version-incorrect",
            "observed_through": "representative_consumer",
        },
    )
    verification.pop("canonical_content_digest")

    class NativeMismatchClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        NativeMismatchClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )
    assert not result.definitive
    assert "PRODUCER_NATIVE_EVIDENCE_MISMATCH" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_official_revision_requires_finality_evidence() -> None:
    raw = _response()
    obligation = raw["periods"][0]
    assert isinstance(obligation, dict)
    obligation.update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    revision = raw["revisions"][0]
    assert isinstance(revision, dict)
    revision.pop("finality_basis")
    revision.pop("finality_policy_id")
    revision.pop("finalized_at")

    class MissingFinalityEvidenceClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        MissingFinalityEvidenceClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert not result.definitive
    assert "FINALITY_EVIDENCE_MISSING" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_materialization_method_evidence_must_match_method() -> None:
    raw = _response()
    obligation = raw["periods"][0]
    assert isinstance(obligation, dict)
    obligation.update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    materialization = raw["materializations"][0]
    assert isinstance(materialization, dict)
    resource = materialization["resource"]
    verification = materialization["verification"]
    assert isinstance(resource, dict)
    assert isinstance(verification, dict)
    resource["kind"] = "warehouse_relation"
    verification["verification_path"] = "destination"

    class MethodMismatchClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        MethodMismatchClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert not result.definitive
    assert "MATERIALIZATION_METHOD_EVIDENCE_MISMATCH" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_superseded_revision_must_be_present() -> None:
    raw = _response()
    obligation = raw["periods"][0]
    assert isinstance(obligation, dict)
    obligation.update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    revision = raw["revisions"][0]
    assert isinstance(revision, dict)
    revision["supersedes_reporting_revision_id"] = "revision-missing"

    class IncompleteRevisionChainClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        IncompleteRevisionChainClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert not result.definitive
    assert "INCOMPLETE_REVISION_CHAIN" in result.obligations[0].reasons


@pytest.mark.asyncio
async def test_recorded_rejected_checkpoint_is_reinspected() -> None:
    response = GetReportingStatusResponse.model_validate(_response())
    rejected = build_reporting_receipt(
        ReportingInspectionContext(
            response.periods[0],
            response.revisions[0],
            response.materializations[0],
        ),
        ReportingObservation(
            row_count=8,
            control_totals=[ReportingControlTotal.model_validate(item) for item in TOTALS],
            canonical_content_digest=ReportingCanonicalContentDigest.model_validate(DIGEST),
        ),
        reporting_receipt_id="reporting-receipt:rejected-checkpoint",
    )

    class CheckpointStore:
        receipt = rejected

        async def get(self, reporting_materialization_id: str):
            assert reporting_materialization_id == "materialization-billing"
            return self.receipt

        async def put(self, receipt):
            self.receipt = receipt

    client = _Client()
    client.recorded_receipt = rejected.model_dump(mode="json", exclude_none=True)
    inspections = 0

    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        nonlocal inspections
        inspections += 1
        return ReportingObservation(
            row_count=7,
            control_totals=[ReportingControlTotal.model_validate(item) for item in TOTALS],
            canonical_content_digest=ReportingCanonicalContentDigest.model_validate(DIGEST),
        )

    result = await reconcile_reporting(
        client,
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
        inspect,
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-2"),
                str(PERIOD["start"]),
                str(PERIOD["end"]),
            )
        ],
        checkpoint_store=CheckpointStore(),
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert inspections == 1
    assert result.definitive
    assert result.submitted_receipts[0].status.value == "accepted"


@pytest.mark.asyncio
async def test_core_obligation_without_materialization_can_be_definitive() -> None:
    raw = _response()
    obligation = raw["periods"][0]
    assert isinstance(obligation, dict)
    obligation.pop("destination_ref")
    obligation.pop("materialization_count")
    obligation.pop("successful_materialization_count")
    obligation.pop("receipt_count")
    obligation.pop("accepted_receipt_count")
    obligation.pop("resource_retained_until")
    obligation.update(
        reconciliation_mode="delivery_only",
        reconciliation_status="not_required",
        health="complete",
    )
    raw["materializations"] = []
    raw["pagination"] = {"has_more": False, "total_count": 2}

    class CoreClient(_Client):
        async def get_reporting_status(
            self, request: GetReportingStatusRequest
        ) -> TaskResult[GetReportingStatusResponse]:
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(deepcopy(raw)),
            )

    ledger = await load_reporting_ledger(
        CoreClient(),
        GetReportingStatusRequest.model_validate(
            {"account": {"account_id": "account-1"}, "view": "periods"}
        ),
    )
    result = evaluate_reporting_ledger(
        ledger,
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-2"),
                str(PERIOD["start"]),
                str(PERIOD["end"]),
            )
        ],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert result.definitive, result.obligations
    assert result.obligations[0].reporting_materialization_id is None
