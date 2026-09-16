"""One deterministic seller scenario for both reconciliation storage mechanisms."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, TypeAlias

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger import (
    InMemoryReportingReconciliationStore,
    PgReportingReconciliationStore,
    ReportingCanonicalDigest,
    ReportingControlTotalRecord,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingFinality,
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingObligationRecord,
    ReportingPhysicalChecksum,
    ReportingResourceRecord,
    ReportingRevisionReceiptRecord,
    ReportingRevisionRecord,
    ReportingVerificationRecord,
    revision_content_sha256,
)
from adcp.reporting.ledger.delivery_models import DeliveryMethod, VerificationProfile

from ._generation_support import (
    END,
    NOW,
    START,
    configuration,
    isolated_reporting_pool,
    obligation_for,
)

Store: TypeAlias = InMemoryReportingReconciliationStore | PgReportingReconciliationStore


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=["memory", "postgres"])
async def reconciliation_store(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[Store, Clock]]:
    clock = Clock()
    if request.param == "memory":
        yield InMemoryReportingReconciliationStore(clock=clock), clock
    else:
        async with isolated_reporting_pool() as pool:
            store = PgReportingReconciliationStore(pool=pool, clock=clock)
            await store.create_schema()
            yield store, clock


@dataclass(frozen=True)
class Scenario:
    binding: ReportingDestinationBinding
    delivery: ReportingObligationDeliveryRecord
    obligation: ReportingObligationRecord
    revision: ReportingRevisionRecord
    attempt: ReportingMaterializationAttempt
    outcome: ReportingMaterializationRecord
    receipt: ReportingRevisionReceiptRecord


async def scenario(
    store: Store,
    *,
    account_id: str = "acct_a",
    consumer_id: str = "buyer",
    method: DeliveryMethod = "file_transfer",
    profile: VerificationProfile = "canonical_digest",
    billing: bool = True,
    finality: ReportingFinality = "official",
    reconciliation_mode: Literal["delivery_only", "consumer_receipt"] = "consumer_receipt",
    control_total_evidence: tuple[ReportingControlTotalRecord, ...] | None = None,
    reader_compatibility: tuple[str, ...] | None = None,
    revision_id: str | None = None,
    obligation_id: str | None = None,
    destination_ref: str = "destination-generation-1",
) -> Scenario:
    feed = "billing" if billing else "analytics"
    config = replace(configuration(account_id), feed_purpose=feed, required_finality=finality)
    await store.put_configuration(config)
    obligation = replace(obligation_for(config), currency="EUR")
    if obligation_id is not None:
        obligation = replace(obligation, reporting_obligation_id=obligation_id)
    await store.commit_obligation(obligation)
    scope = ReportingDeliveryScope(
        config.generation_key, consumer_id, obligation.reporting_obligation_id
    )
    binding = ReportingDestinationBinding(
        generation_key=config.generation_key,
        consumer_id=consumer_id,
        destination_ref=destination_ref,
        trusted_binding_ref="trusted-binding-1",
        method=method,
        transport="test-storage",
        verification_profile=profile,
        reconciliation_mode=reconciliation_mode,
        feed_purpose=feed,
        resource_retention_days=400,
        created_at=START,
        format="jsonl" if method == "file_transfer" else None,
        reader_compatibility=(
            reader_compatibility
            if reader_compatibility is not None
            else (("jsonl-v1",) if method == "file_transfer" else ("table-v1",))
        ),
        success_status="delivered" if method == "warehouse_materialization" else "available",
    )
    await store.put_destination_binding(binding)
    delivery = ReportingObligationDeliveryRecord(scope, "EUR", END + timedelta(days=400), END)
    await store.bind_obligation_delivery(delivery)
    rows = [
        {
            "media_buy_id": obligation.media_buy_ids[0],
            "impressions": 5,
            "spend": "12.50",
            "currency": "EUR",
        }
    ]
    totals = (("impressions", "5"), ("spend", "12.50"))
    total_records = (
        ReportingControlTotalRecord("impressions", "5", "integer"),
        ReportingControlTotalRecord("spend", "12.50", "decimal", "EUR"),
    )
    if control_total_evidence is not None:
        total_records = control_total_evidence
    revision_id = revision_id or f"revision-{account_id}"
    digest = ReportingCanonicalDigest(
        value=hashlib.sha256(canonical_json_utf8_v1(rows)).hexdigest(),
        canonicalization_id="rows-v1",
        canonicalization_uri="https://contracts.example.test/rows-v1.json",
        canonicalization_sha256="b" * 64,
    )
    revision = ReportingRevisionRecord(
        reporting_revision_id=revision_id,
        account_id=account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality=finality,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=1,
            control_totals=totals,
            reporting_rows=rows,
            control_total_evidence=total_records,
        ),
        row_count=1,
        control_totals=totals,
        observed_at=END,
        data_through=END,
        created_at=END + timedelta(seconds=1),
        finality_basis="source_final" if finality == "official" else None,
        finality_policy_id="policy-v1" if finality == "official" else None,
        finalized_at=END if finality == "official" else None,
        canonical_content_digest=digest,
        managed_control_totals=total_records,
    )
    await store.commit_revision(revision, rows)
    attempt = ReportingMaterializationAttempt(
        scope, revision_id, "materialization-1", 1, END + timedelta(seconds=2)
    )
    await store.commit_materialization_attempt(attempt)
    completed = END + timedelta(seconds=3)
    resource = ReportingResourceRecord(
        resource_ref="resource-1",
        kind={
            "file_transfer": "manifest",
            "dataset_share": "dataset",
            "warehouse_materialization": "warehouse_relation",
        }[method],
        location="reports/official/manifest.json",
        immutability=(
            "immutable_location"
            if method == "file_transfer" and profile != "native_commit"
            else "native_version"
        ),
        expires_at=completed + timedelta(days=400),
        manifest_sha256="c" * 64 if method == "file_transfer" else None,
        native_version_ref=(
            "version-1" if method != "file_transfer" or profile == "native_commit" else None
        ),
        object_refs=("reports/official/part-000.jsonl",) if method == "file_transfer" else (),
        reader_compatibility=binding.reader_compatibility,
    )
    verification = ReportingVerificationRecord(
        verified_at=completed,
        verification_path={
            "file_transfer": "destination" if profile == "native_commit" else "producer",
            "dataset_share": "representative_consumer",
            "warehouse_materialization": "destination",
        }[method],
        verification_profile=profile,
        row_count=1,
        control_totals=total_records,
        canonical_content_digest=digest if profile == "canonical_digest" else None,
        physical_checksums=(
            (ReportingPhysicalChecksum(resource.object_refs[0], "sha256", "d" * 64),)
            if method == "file_transfer"
            else ()
        ),
        native_version_ref="version-1" if profile == "native_commit" else None,
        native_observed_through=(
            ("representative_consumer" if method == "dataset_share" else "destination")
            if profile == "native_commit"
            else None
        ),
        verified_format=binding.format,
    )
    outcome = ReportingMaterializationRecord(
        scope,
        revision_id,
        attempt.reporting_materialization_id,
        binding.success_status,
        completed,
        resource=resource,
        verification=verification,
    )
    receipt = ReportingRevisionReceiptRecord(
        scope=scope,
        reporting_receipt_id="receipt-first-0001",
        reporting_revision_id=revision_id,
        reporting_materialization_id=attempt.reporting_materialization_id,
        status="accepted",
        verification_profile=profile,
        observed_row_count=1,
        observed_control_totals=total_records,
        observed_canonical_content_digest=digest if profile == "canonical_digest" else None,
        observed_manifest_sha256=(
            resource.manifest_sha256 if profile == "manifest_checksums" else None
        ),
        observed_native_version_ref=(
            resource.native_version_ref if profile == "native_commit" else None
        ),
        observed_at=END + timedelta(seconds=4),
        consumer_commit_ref="load-0001",
    )
    return Scenario(binding, delivery, obligation, revision, attempt, outcome, receipt)
