from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime
from urllib.parse import urljoin

import httpx
import pytest
import rfc8785

from adcp.decisioning.capabilities import MediaBuy
from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingInspectionContext,
    ReportingObservation,
    ReportingReconciliationError,
    ReportingTier,
    build_reporting_receipt,
    evaluate_reporting_ledger,
    load_reporting_ledger,
    reconcile_reporting,
    reconcile_reporting_core,
    reporting_tiers,
)
from adcp.reporting_inspection import (
    HttpsReportingResourceReader,
    ManifestReportingInspector,
    ReportingInspectionCode,
    ReportingInspectionError,
    _canonical_rows_bytes,
    _decode_rows,
)
from adcp.types import (
    ReportingDeliveryCapabilities,
    ReportingMaterialization,
    ReportingObligation,
    ReportingRevision,
)
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
    "revision_content_sha256": "e" * 64,
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


class _CoreClient:
    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]:
        obligation = _obligation("obligation-core")
        for field in (
            "destination_ref",
            "materialization_count",
            "successful_materialization_count",
            "receipt_count",
            "accepted_receipt_count",
            "resource_retained_until",
        ):
            obligation.pop(field, None)
        obligation.update(
            reconciliation_mode="delivery_only",
            reconciliation_status="not_required",
            health="complete",
        )
        revision = deepcopy(REVISION)
        revision.pop("canonical_content_digest")
        response = _response()
        response.update(
            periods=[obligation],
            revisions=[revision],
            materializations=[],
            receipts=[],
            pagination={"has_more": False, "total_count": 2},
        )
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=GetReportingStatusResponse.model_validate(response),
        )


@pytest.mark.asyncio
async def test_core_reconciliation_needs_no_inspector_or_receipt_client() -> None:
    result = await reconcile_reporting_core(
        _CoreClient(),
        GetReportingStatusRequest.model_validate(
            {
                "account": {"account_id": "account-1"},
                "view": "periods",
                "period": {"start": PERIOD["start"], "end": PERIOD["end"]},
            }
        ),
        expected_periods=[
            ExpectedReportingPeriod(
                "billing-feed",
                1,
                "billing-v1",
                "billing",
                "billing-v1",
                ("buy-1", "buy-2"),
                PERIOD["start"],
                PERIOD["end"],
            )
        ],
        now=datetime.fromisoformat("2026-09-03T00:00:00+00:00"),
    )

    assert result.definitive
    assert result.obligations[0].definitive


def test_reporting_tiers_enforce_cumulative_flags() -> None:
    core = ReportingDeliveryCapabilities.model_construct(
        managed_delivery=False, reconciled_billing=False
    )
    assert reporting_tiers(core) == frozenset({ReportingTier.CORE})

    invalid = ReportingDeliveryCapabilities.model_construct(
        managed_delivery=False, reconciled_billing=True
    )
    with pytest.raises(ReportingReconciliationError) as error:
        reporting_tiers(invalid)
    assert error.value.code == "INVALID_REPORTING_CAPABILITIES"


class _ResourceReader:
    def __init__(self, resources: dict[str, bytes]) -> None:
        self.resources = resources
        self.calls: list[str] = []

    async def read(self, locator: str, *, base: str | None = None, max_bytes: int) -> bytes:
        resolved = urljoin(base, locator) if base else locator
        self.calls.append(resolved)
        body = self.resources[resolved]
        if len(body) > max_bytes:
            raise ValueError("too large")
        return body


def _manifest_inspection_fixture():
    rows = [
        {"media_buy_id": "buy-2", "date": "2026-08-01", "impressions": 4, "spend": 5},
        {"media_buy_id": "buy-1", "date": "2026-08-01", "impressions": 3, "spend": 7},
    ]
    data = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "media_buy_id": {"type": "string"},
            "date": {"type": "string"},
            "impressions": {"type": "integer"},
            "spend": {"type": "number"},
        },
        "required": ["media_buy_id", "date", "impressions", "spend"],
        "additionalProperties": False,
    }
    schema_body = json.dumps(schema, separators=(",", ":")).encode()
    schema_digest = hashlib.sha256(schema_body).hexdigest()
    definition = {
        "contract_version": "1.0",
        "media_type": "application/vnd.adcp.reporting-definition+json",
        "report_definition_id": "billing-v1",
        "reporting_profile": "billing-v1",
        "grain": "media_buy_day",
        "source": {
            "provider": {"domain": "seller.example"},
            "system": "test-ledger",
            "api_version": "1",
            "query_semantics": {},
        },
        "calendar": {"timezone_basis": "utc"},
        "metrics": [
            {"name": "impressions", "source_expression": "impressions", "aggregation": "sum"},
            {"name": "spend", "source_expression": "spend", "aggregation": "sum"},
        ],
        "dimensions": ["media_buy_id", "date"],
        "restatement_policy": {
            "source_requery_duration": "P30D",
            "emit_only_on_content_change": True,
        },
        "finality_policies": [
            {
                "finality_policy_id": "billing-v1-source-final",
                "basis": "source_final",
                "source_signal": "closed",
            }
        ],
    }
    definition_body = json.dumps(definition, separators=(",", ":")).encode()
    canonical_contract = {
        "contract_version": "1.0",
        "media_type": "application/vnd.adcp.reporting-canonicalization+json",
        "algorithm": "adcp_jcs_rows_v1",
        "schema_sha256": schema_digest,
        "primary_keys": ["media_buy_id", "date"],
        "golden_vectors": {
            "empty_report": {
                "name": "empty",
                "purpose": "empty_report",
                "input_rows": [],
                "canonical_utf8_base64": "W10=",
                "sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            },
            "ordering_encoding": {
                "name": "ordering",
                "purpose": "ordering_encoding",
                "input_rows": [
                    {"date": "2026-08-02", "media_buy_id": "buy-2"},
                    {"date": "2026-08-01", "media_buy_id": "buy-1"},
                ],
                "canonical_utf8_base64": (
                    "W3siZGF0ZSI6IjIwMjYtMDgtMDEiLCJtZWRpYV9idXlfaWQiOiJidXktMSJ9LHsiZGF0ZSI6"
                    "IjIwMjYtMDgtMDIiLCJtZWRpYV9idXlfaWQiOiJidXktMiJ9XQ=="
                ),
                "sha256": "ce716e16aef215a0ee62500e669d9a89c6de33afa190f528b195648f1a2f71c8",
            },
        },
    }
    canonical_body = json.dumps(canonical_contract, separators=(",", ":")).encode()
    ordered = sorted(rows, key=lambda row: (row["media_buy_id"], row["date"]))
    canonical_digest = hashlib.sha256(rfc8785.dumps(ordered)).hexdigest()
    totals = [
        {"name": "impressions", "value": "7", "value_type": "integer"},
        {"name": "spend", "value": "12", "value_type": "decimal", "unit": "USD"},
    ]
    manifest = {
        "manifest_version": "1.0",
        "complete": True,
        "reporting_revision_id": "revision-august-official",
        "reporting_obligation_id": "obligation-billing",
        "reporting_materialization_id": "materialization-billing",
        "period": PERIOD,
        "format": "jsonl",
        "compression": "none",
        "files": [
            {
                "object_ref": "data.jsonl",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "row_count": 2,
            }
        ],
        "total_size_bytes": len(data),
        "row_count": 2,
        "control_totals": totals,
        "created_at": "2026-09-02T00:00:00Z",
    }
    manifest_body = json.dumps(manifest, separators=(",", ":")).encode()
    revision = deepcopy(REVISION)
    revision.update(
        row_count=2,
        control_totals=totals,
        schema_uri="https://contracts.example/rows.json",
        schema_sha256=schema_digest,
        report_definition_uri="https://contracts.example/definition.json",
        report_definition_sha256=hashlib.sha256(definition_body).hexdigest(),
        canonical_content_digest={
            "algorithm": "sha256",
            "value": canonical_digest,
            "canonicalization_id": "rows-v1",
            "canonicalization_uri": "https://contracts.example/canonical.json",
            "canonicalization_sha256": hashlib.sha256(canonical_body).hexdigest(),
        },
    )
    obligation = _obligation()
    materialization = _materialization()
    materialization.update(
        method="file_transfer",
        transport="https",
        resource={
            "resource_ref": "resource-manifest",
            "kind": "manifest",
            "location": "https://files.example/manifest.json",
            "manifest_version": "1.0",
            "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
            "immutability": "immutable_location",
            "expires_at": "2026-12-01T00:00:00Z",
        },
        verification={
            "verified_at": "2026-09-02T00:00:05Z",
            "verification_path": "destination",
            "verification_profile": "canonical_digest",
            "row_count": 2,
            "control_totals": totals,
            "canonical_content_digest": revision["canonical_content_digest"],
            "physical_checksums": [
                {
                    "object_ref": "data.jsonl",
                    "algorithm": "sha256",
                    "value": hashlib.sha256(data).hexdigest(),
                }
            ],
        },
    )
    context = ReportingInspectionContext(
        ReportingObligation.model_validate(obligation),
        ReportingRevision.model_validate(revision),
        ReportingMaterialization.model_validate(materialization),
    )
    resources = {
        "https://files.example/manifest.json": manifest_body,
        "https://files.example/data.jsonl": data,
        "https://contracts.example/rows.json": schema_body,
        "https://contracts.example/definition.json": definition_body,
        "https://contracts.example/canonical.json": canonical_body,
    }
    return context, resources


@pytest.mark.asyncio
async def test_builtin_manifest_inspector_verifies_complete_resource() -> None:
    context, resources = _manifest_inspection_fixture()

    observation = await ManifestReportingInspector(_ResourceReader(resources))(context)

    assert observation.row_count == 2
    assert observation.manifest_sha256 == context.materialization.resource.manifest_sha256
    assert observation.canonical_content_digest == context.revision.canonical_content_digest


@pytest.mark.asyncio
async def test_builtin_manifest_inspector_rejects_corrupt_object() -> None:
    context, resources = _manifest_inspection_fixture()
    data = resources["https://files.example/data.jsonl"]
    resources["https://files.example/data.jsonl"] = b"X" + data[1:]

    with pytest.raises(ReportingInspectionError) as error:
        await ManifestReportingInspector(_ResourceReader(resources))(context)

    assert error.value.code == ReportingInspectionCode.OBJECT_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_builtin_manifest_inspector_enforces_aggregate_decoded_byte_limit() -> None:
    context, resources = _manifest_inspection_fixture()

    with pytest.raises(ReportingInspectionError) as error:
        await ManifestReportingInspector(_ResourceReader(resources), max_total_decoded_bytes=1)(
            context
        )

    assert error.value.code == ReportingInspectionCode.RESOURCE_TOO_LARGE
    assert "decoded-byte" in str(error.value)


@pytest.mark.asyncio
async def test_https_reporting_reader_rejects_unsafe_locators_before_fetch() -> None:
    reader = HttpsReportingResourceReader()

    with pytest.raises(ReportingInspectionError) as insecure:
        await reader.read("http://files.example/report.json", max_bytes=100)
    assert insecure.value.code == ReportingInspectionCode.UNSAFE_RESOURCE

    with pytest.raises(ReportingInspectionError) as cross_origin:
        await reader.read(
            "https://attacker.example/data.jsonl",
            base="https://files.example/manifest.json",
            max_bytes=100,
        )
    assert cross_origin.value.code == ReportingInspectionCode.UNSAFE_RESOURCE


@pytest.mark.asyncio
async def test_https_reporting_reader_requires_trusted_credential_free_dns_origin() -> None:
    reader = HttpsReportingResourceReader(trusted_origins=["https://files.example"])

    for locator in (
        "https://user:password@files.example/report.json",
        "https://8.8.8.8/report.json",
        "https://other.example/report.json",
    ):
        with pytest.raises(ReportingInspectionError) as error:
            await reader.read(locator, max_bytes=100)
        assert error.value.code == ReportingInspectionCode.UNSAFE_RESOURCE


@pytest.mark.asyncio
async def test_https_reporting_reader_checks_content_type_and_resolves_off_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_threads: list[int] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"report")

    def build_transport(*_: object, **__: object) -> httpx.AsyncBaseTransport:
        factory_threads.append(threading.get_ident())
        return httpx.MockTransport(handler)

    monkeypatch.setattr(
        "adcp.reporting_inspection.build_async_ip_pinned_transport", build_transport
    )
    reader = HttpsReportingResourceReader(trusted_origins=["https://files.example"])

    with pytest.raises(ReportingInspectionError) as error:
        await reader.read(
            "https://files.example/report.json",
            max_bytes=100,
            expected_content_types=frozenset({"application/json"}),
        )

    assert error.value.code == ReportingInspectionCode.UNEXPECTED_CONTENT_TYPE
    assert factory_threads[0] != threading.get_ident()


def test_builtin_inspector_rejects_duplicate_json_keys_and_uses_jcs_key_ordering() -> None:
    with pytest.raises(ReportingInspectionError) as duplicate:
        _decode_rows(
            b'{"media_buy_id":"buy-1","media_buy_id":"buy-2"}\n',
            "jsonl",
            "none",
            max_decoded_bytes=1024,
            max_rows=10,
        )
    assert duplicate.value.code == ReportingInspectionCode.INVALID_ROWS

    assert _canonical_rows_bytes([{"id": 2}, {"id": 10}], ["id"]) == (b'[{"id":10},{"id":2}]')


@pytest.mark.asyncio
async def test_builtin_inspector_rejects_bad_golden_vector_and_unsafe_schema() -> None:
    context, resources = _manifest_inspection_fixture()
    canonical_url = "https://contracts.example/canonical.json"
    canonical = json.loads(resources[canonical_url])
    canonical["golden_vectors"]["ordering_encoding"]["sha256"] = "0" * 64
    canonical_body = json.dumps(canonical, separators=(",", ":")).encode()
    resources[canonical_url] = canonical_body
    digest = context.revision.canonical_content_digest
    assert digest is not None
    updated_digest = digest.model_copy(
        update={"canonicalization_sha256": hashlib.sha256(canonical_body).hexdigest()}
    )
    updated_context = ReportingInspectionContext(
        context.obligation,
        context.revision.model_copy(update={"canonical_content_digest": updated_digest}),
        context.materialization.model_copy(
            update={
                "verification": context.materialization.verification.model_copy(
                    update={"canonical_content_digest": updated_digest}
                )
            }
        ),
    )
    with pytest.raises(ReportingInspectionError) as bad_vector:
        await ManifestReportingInspector(_ResourceReader(resources))(updated_context)
    assert bad_vector.value.code == ReportingInspectionCode.INVALID_CONTRACT

    context, resources = _manifest_inspection_fixture()
    schema_url = "https://contracts.example/rows.json"
    schema = json.loads(resources[schema_url])
    schema["$dynamicRef"] = "#"
    schema_body = json.dumps(schema, separators=(",", ":")).encode()
    resources[schema_url] = schema_body
    unsafe_schema_context = ReportingInspectionContext(
        context.obligation,
        context.revision.model_copy(
            update={"schema_sha256": hashlib.sha256(schema_body).hexdigest()}
        ),
        context.materialization,
    )
    with pytest.raises(ReportingInspectionError) as unsafe_schema:
        await ManifestReportingInspector(_ResourceReader(resources))(unsafe_schema_context)
    assert unsafe_schema.value.code == ReportingInspectionCode.INVALID_CONTRACT

    context, resources = _manifest_inspection_fixture()
    schema_url = "https://contracts.example/rows.json"
    schema = json.loads(resources[schema_url])
    schema["properties"]["media_buy_id"]["pattern"] = "^(a+)+$"
    schema_body = json.dumps(schema, separators=(",", ":")).encode()
    resources[schema_url] = schema_body
    regex_schema_context = ReportingInspectionContext(
        context.obligation,
        context.revision.model_copy(
            update={"schema_sha256": hashlib.sha256(schema_body).hexdigest()}
        ),
        context.materialization,
    )
    with pytest.raises(ReportingInspectionError) as regex_schema:
        await ManifestReportingInspector(_ResourceReader(resources))(regex_schema_context)
    assert regex_schema.value.code == ReportingInspectionCode.INVALID_CONTRACT

    context, resources = _manifest_inspection_fixture()
    schema_url = "https://contracts.example/rows.json"
    schema = json.loads(resources[schema_url])
    schema["$schema"] = "https://json-schema.org/draft/2019-09/schema"
    schema_body = json.dumps(schema, separators=(",", ":")).encode()
    resources[schema_url] = schema_body
    dialect_mismatch_context = ReportingInspectionContext(
        context.obligation,
        context.revision.model_copy(
            update={"schema_sha256": hashlib.sha256(schema_body).hexdigest()}
        ),
        context.materialization,
    )
    with pytest.raises(ReportingInspectionError) as dialect_mismatch:
        await ManifestReportingInspector(_ResourceReader(resources))(dialect_mismatch_context)
    assert dialect_mismatch.value.code == ReportingInspectionCode.INVALID_CONTRACT

    context, resources = _manifest_inspection_fixture()
    schema_url = "https://contracts.example/rows.json"
    schema = json.loads(resources[schema_url])
    schema["$defs"] = {"loop": {"$ref": "#/$defs/loop"}}
    schema["allOf"] = [{"$ref": "#/$defs/loop"}]
    schema_body = json.dumps(schema, separators=(",", ":")).encode()
    resources[schema_url] = schema_body
    cyclic_schema_context = ReportingInspectionContext(
        context.obligation,
        context.revision.model_copy(
            update={"schema_sha256": hashlib.sha256(schema_body).hexdigest()}
        ),
        context.materialization,
    )
    with pytest.raises(ReportingInspectionError) as cyclic_schema:
        await ManifestReportingInspector(_ResourceReader(resources))(cyclic_schema_context)
    assert cyclic_schema.value.code == ReportingInspectionCode.INVALID_CONTRACT


@pytest.mark.asyncio
async def test_builtin_inspector_enforces_aggregate_row_limit() -> None:
    context, resources = _manifest_inspection_fixture()

    with pytest.raises(ReportingInspectionError) as limit:
        await ManifestReportingInspector(_ResourceReader(resources), max_total_rows=1)(context)
    assert limit.value.code == ReportingInspectionCode.RESOURCE_TOO_LARGE


@pytest.mark.asyncio
async def test_reconciler_requires_reconciled_tier_for_billing_and_canonical_data() -> None:
    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        raise AssertionError("tier validation must run before inspection")

    capabilities = ReportingDeliveryCapabilities.model_construct(
        managed_delivery=True, reconciled_billing=False
    )
    with pytest.raises(ReportingReconciliationError) as error:
        await reconcile_reporting(
            _Client(),
            GetReportingStatusRequest.model_validate(
                {"account": {"account_id": "account-1"}, "view": "periods"}
            ),
            inspect,
            expected_periods=[],
            reporting_capabilities=capabilities,
        )
    assert error.value.code == "RECONCILED_BILLING_NOT_ENABLED"


@pytest.mark.asyncio
async def test_reconciler_uses_builtin_reader_and_does_not_retry_corruption() -> None:
    context, resources = _manifest_inspection_fixture()
    data = resources["https://files.example/data.jsonl"]
    resources["https://files.example/data.jsonl"] = b"X" + data[1:]
    reader = _ResourceReader(resources)
    response = _response()
    response.update(
        periods=[context.obligation.model_dump(mode="json", exclude_none=True)],
        revisions=[context.revision.model_dump(mode="json", exclude_none=True)],
        materializations=[context.materialization.model_dump(mode="json", exclude_none=True)],
        pagination={"has_more": False, "total_count": 3},
    )

    class Client:
        async def get_reporting_status(self, request):
            return TaskResult(
                status=TaskStatus.COMPLETED,
                data=GetReportingStatusResponse.model_validate(response),
            )

        async def sync_reporting_receipts(self, request):
            raise AssertionError("corrupt data must not produce a receipt")

    with pytest.raises(ReportingReconciliationError) as error:
        await reconcile_reporting(
            Client(),
            GetReportingStatusRequest.model_validate(
                {
                    "account": {"account_id": "account-1"},
                    "view": "periods",
                    "period": {"start": PERIOD["start"], "end": PERIOD["end"]},
                }
            ),
            expected_periods=[],
            resource_reader=reader,
            max_inspection_attempts=3,
        )

    assert error.value.code == "OBJECT_DIGEST_MISMATCH"
    assert reader.calls.count("https://files.example/data.jsonl") == 1


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


@pytest.mark.asyncio
async def test_inspection_timeout_exhausts_retry_budget() -> None:
    attempts = 0

    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(ReportingReconciliationError) as exc_info:
        await reconcile_reporting(
            _Client(),
            GetReportingStatusRequest.model_validate(
                {"account": {"account_id": "account-1"}, "view": "periods"}
            ),
            inspect,
            expected_periods=[],
            max_inspection_attempts=2,
            inspection_timeout_seconds=0.01,
            inspection_retry_backoff_seconds=0,
        )

    assert attempts == 2
    assert exc_info.value.code == "INSPECTION_FAILED"
    assert "timed out after 0.01 seconds" in str(exc_info.value)


@pytest.mark.asyncio
async def test_inspection_retries_use_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("adcp.reporting.asyncio.sleep", fake_sleep)

    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        raise OSError("destination unavailable")

    with pytest.raises(ReportingReconciliationError):
        await reconcile_reporting(
            _Client(),
            GetReportingStatusRequest.model_validate(
                {"account": {"account_id": "account-1"}, "view": "periods"}
            ),
            inspect,
            expected_periods=[],
            max_inspection_attempts=3,
            inspection_retry_backoff_seconds=0.25,
        )

    assert delays == [0.25, 0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_inspection_attempts": 0}, "max_inspection_attempts"),
        ({"max_inspection_attempts": True}, "max_inspection_attempts"),
        ({"inspection_timeout_seconds": 0}, "inspection_timeout_seconds"),
        ({"inspection_timeout_seconds": float("inf")}, "inspection_timeout_seconds"),
        ({"inspection_retry_backoff_seconds": -1}, "inspection_retry_backoff_seconds"),
        (
            {"inspection_retry_backoff_seconds": float("nan")},
            "inspection_retry_backoff_seconds",
        ),
    ],
)
async def test_invalid_inspection_retry_configuration_fails_fast(
    kwargs: dict[str, float | int], message: str
) -> None:
    async def inspect(_: ReportingInspectionContext) -> ReportingObservation:
        raise AssertionError("inspection must not run")

    with pytest.raises(ValueError, match=message):
        await reconcile_reporting(
            _Client(),
            GetReportingStatusRequest.model_validate(
                {"account": {"account_id": "account-1"}, "view": "periods"}
            ),
            inspect,
            expected_periods=[],
            **kwargs,
        )


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
