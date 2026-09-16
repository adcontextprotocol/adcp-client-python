"""Additional shared financial-state and wire/buyer constraints for both stores."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting import ReportingLedger, evaluate_reporting_ledger
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger import (
    LedgerConflictError,
    ReportingAdjustmentReceiptRecord,
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryScope,
    ReportingMaterializationRecord,
    ReportingStatusCaller,
    ReportingStatusHandler,
    adjustment_to_wire,
    receipt_to_wire,
    revision_content_sha256,
    revision_to_wire,
)
from adcp.reporting.ledger.delivery_models import DeliveryMethod, VerificationProfile
from adcp.types import GetReportingStatusResponse, ReportingReceipt

from ._generation_support import END, NOW, configuration
from ._reconciliation_support import Clock, Store, scenario


@pytest.mark.parametrize(
    "method,profile",
    [
        ("file_transfer", "canonical_digest"),
        ("file_transfer", "manifest_checksums"),
        ("dataset_share", "canonical_digest"),
        ("dataset_share", "native_commit"),
        ("warehouse_materialization", "canonical_digest"),
        ("warehouse_materialization", "native_commit"),
    ],
)
async def test_generated_projections_are_accepted_by_buyer_reconciler(
    reconciliation_store: tuple[Store, Clock], method: DeliveryMethod, profile: VerificationProfile
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store, method=method, profile=profile, billing=profile == "canonical_digest")
    await store.commit_materialization(s.outcome)
    receipt, _ = await store.record_revision_receipt(s.receipt)
    view = await store.get_materialization(s.attempt.key)
    assert view is not None and view.readable_at(NOW)
    # Construct a future periods projection locally. The real mounted handler
    # deliberately continues serving Core with empty higher-tier arrays.
    result = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    result["periods"][0].update(
        destination_ref=s.binding.destination_ref,
        reconciliation_mode="consumer_receipt",
        reconciliation_status="accepted",
        materialization_count=1,
        successful_materialization_count=1,
        receipt_count=1,
        accepted_receipt_count=1,
        resource_retained_until=s.delivery.resource_retained_until.isoformat(),
    )
    result["revisions"] = [revision_to_wire(s.revision, obligation=s.obligation)]
    result["materializations"] = [view.to_wire()]
    result["receipts"] = [receipt_to_wire(receipt)]
    result["pagination"]["total_count"] = 4
    response = GetReportingStatusResponse.model_validate(result)
    ledger = ReportingLedger(
        response.ledger_snapshot_id,
        response.ledger_as_of,
        response.account_id,
        response.scope,
        response.periods,
        response.revisions,
        response.materializations,
        response.receipts,
    )
    verdict = evaluate_reporting_ledger(ledger, expected_periods=[], now=NOW)
    assert verdict.definitive, verdict.obligations


@pytest.mark.parametrize(
    "field", ["currency", "value_type", "decimal_spelling", "digest", "row_count"]
)
async def test_acceptance_requires_exact_evidence_and_rejection_preserves_disagreement(
    reconciliation_store: tuple[Store, Clock], field: str
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    receipt = s.receipt
    totals = list(receipt.observed_control_totals)
    if field == "currency":
        totals[1] = replace(totals[1], unit="USD")
    if field == "value_type":
        totals[0] = replace(totals[0], value_type="decimal")
    if field == "decimal_spelling":
        totals[1] = replace(totals[1], value="12.5")
    receipt = replace(receipt, observed_control_totals=tuple(totals))
    if field == "digest":
        receipt = replace(
            receipt,
            observed_canonical_content_digest=replace(
                receipt.observed_canonical_content_digest, value="0" * 64
            ),
        )
    if field == "row_count":
        receipt = replace(receipt, observed_row_count=0)
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(receipt)
    assert error.value.code in {"RECEIPT_EVIDENCE_MISMATCH", "RECEIPT_TOTALS_MISMATCH"}
    rejected, _ = await store.record_revision_receipt(
        replace(receipt, status="rejected", rejection_codes=("EVIDENCE_MISMATCH",))
    )
    ReportingReceipt.model_validate(receipt_to_wire(rejected))
    replacement = replace(
        s.receipt,
        reporting_receipt_id="receipt-corrected-0002",
        supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
    )
    accepted, _ = await store.record_revision_receipt(replacement)
    assert accepted.status == "accepted"


async def test_rejected_leaf_must_be_current_and_accepted_leaf_survives_another_attempt(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    r1, _ = await store.record_revision_receipt(
        replace(s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",))
    )
    r2_input = replace(
        r1,
        received_at=None,
        reporting_receipt_id="receipt-rejected-0002",
        supersedes_reporting_receipt_id=r1.reporting_receipt_id,
    )
    r2, _ = await store.record_revision_receipt(r2_input)
    with pytest.raises(LedgerConflictError) as stale:
        await store.record_revision_receipt(
            replace(
                s.receipt,
                reporting_receipt_id="receipt-stale-0003",
                supersedes_reporting_receipt_id=r1.reporting_receipt_id,
            )
        )
    assert stale.value.code == "REPORTING_RECORD_UNAVAILABLE"
    accepted, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            reporting_receipt_id="receipt-accepted-0003",
            supersedes_reporting_receipt_id=r2.reporting_receipt_id,
        )
    )
    a2 = replace(s.attempt, reporting_materialization_id="materialization-2", attempt=2)
    await store.commit_materialization_attempt(a2)
    await store.commit_materialization(
        replace(s.outcome, reporting_materialization_id=a2.reporting_materialization_id)
    )
    with pytest.raises(LedgerConflictError) as terminal:
        await store.record_revision_receipt(
            replace(
                s.receipt,
                reporting_receipt_id="receipt-after-retry-0004",
                reporting_materialization_id=a2.reporting_materialization_id,
            )
        )
    assert terminal.value.code == "ACCEPTED_RECEIPT_TERMINAL"
    assert await store.get_receipt(accepted.key) == accepted


async def test_failed_attempt_is_terminal_and_retry_has_a_new_identity(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    failed = ReportingMaterializationRecord(
        s.attempt.scope,
        s.attempt.reporting_revision_id,
        s.attempt.reporting_materialization_id,
        "failed",
        s.outcome.completed_at,
        failure_code="CONTENT_CORRUPT",
    )
    await store.commit_materialization(failed)
    assert await store.commit_materialization(failed) == (failed, False)
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_materialization(s.outcome)
    assert error.value.code == "REPORTING_IDENTITY_CONFLICT"
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(s.receipt)
    assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    # Many workers proposing the same next attempt converge on a single append.
    retry = replace(s.attempt, reporting_materialization_id="materialization-retry", attempt=2)
    results = await asyncio.gather(*(store.commit_materialization_attempt(retry) for _ in range(8)))
    assert sum(written for _, written in results) == 1
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_materialization_attempt(
            replace(retry, reporting_materialization_id="duplicate-attempt")
        )
    assert error.value.code == "MATERIALIZATION_ATTEMPT_CONFLICT"


async def test_new_snapshot_revision_has_its_own_terminal_acceptance(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store, billing=False, finality="snapshot")
    await store.commit_materialization(s.outcome)
    first, _ = await store.record_revision_receipt(s.receipt)
    rows = (
        await store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
        )
    ).rows
    revision = replace(
        s.revision,
        reporting_revision_id="snapshot-restatement",
        supersedes_reporting_revision_id=s.revision.reporting_revision_id,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="snapshot-restatement",
            row_count=1,
            control_totals=s.revision.control_totals,
            reporting_rows=rows,
            control_total_evidence=s.revision.managed_control_totals,
        ),
    )
    await store.commit_revision(revision, rows)
    attempt = replace(
        s.attempt,
        reporting_revision_id=revision.reporting_revision_id,
        reporting_materialization_id="restated-materialization",
    )
    await store.commit_materialization_attempt(attempt)
    await store.commit_materialization(
        replace(
            s.outcome,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id=attempt.reporting_materialization_id,
        )
    )
    second, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            reporting_receipt_id="restated-receipt-0002",
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id=attempt.reporting_materialization_id,
        )
    )
    snapshot = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert snapshot.terminal_acceptances == (first.key, second.key)


async def test_snapshot_and_later_official_keep_independent_delivery_and_receipt_histories(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store, billing=False, finality="snapshot")
    await store.commit_materialization(s.outcome)
    rejected, _ = await store.record_revision_receipt(
        replace(s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",))
    )
    earlier = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    snapshot_rows = (
        await store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
        )
    ).rows
    official_rows = [{**snapshot_rows[0], "spend": "13.50"}]
    official_totals = (
        s.revision.managed_control_totals[0],
        replace(s.revision.managed_control_totals[1], value="13.50"),
    )
    totals = tuple((item.name, item.value) for item in official_totals)
    digest = replace(
        s.revision.canonical_content_digest,
        value=hashlib.sha256(canonical_json_utf8_v1(official_rows)).hexdigest(),
    )
    official = replace(
        s.revision,
        reporting_revision_id="later-official-revision",
        finality="official",
        finality_basis="source_final",
        finality_policy_id="policy-v1",
        finalized_at=END + timedelta(seconds=5),
        observed_at=END + timedelta(seconds=5),
        created_at=END + timedelta(seconds=6),
        control_totals=totals,
        managed_control_totals=official_totals,
        canonical_content_digest=digest,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="later-official-revision",
            row_count=1,
            control_totals=totals,
            reporting_rows=official_rows,
            control_total_evidence=official_totals,
        ),
    )
    assert official.supersedes_reporting_revision_id is None
    with pytest.raises(ValueError, match="supersede"):
        replace(official, supersedes_reporting_revision_id=s.revision.reporting_revision_id)
    await store.commit_revision(official, official_rows)
    attempt = replace(
        s.attempt,
        reporting_revision_id=official.reporting_revision_id,
        reporting_materialization_id="official-materialization",
        created_at=END + timedelta(seconds=7),
    )
    assert attempt.attempt == s.attempt.attempt == 1
    await store.commit_materialization_attempt(attempt)
    completed_at = END + timedelta(seconds=8)
    outcome = replace(
        s.outcome,
        reporting_revision_id=official.reporting_revision_id,
        reporting_materialization_id=attempt.reporting_materialization_id,
        completed_at=completed_at,
        resource=replace(
            s.outcome.resource,
            resource_ref="official-resource",
            location="reports/later-official/manifest.json",
            manifest_sha256="e" * 64,
            object_refs=("reports/later-official/part-000.jsonl",),
            expires_at=completed_at + timedelta(days=400),
        ),
        verification=replace(
            s.outcome.verification,
            verified_at=completed_at,
            control_totals=official_totals,
            canonical_content_digest=digest,
            physical_checksums=(
                replace(
                    s.outcome.verification.physical_checksums[0],
                    object_ref="reports/later-official/part-000.jsonl",
                    value="f" * 64,
                ),
            ),
        ),
    )
    await store.commit_materialization(outcome)
    receipt = replace(
        s.receipt,
        reporting_receipt_id="official-receipt-0001",
        reporting_revision_id=official.reporting_revision_id,
        reporting_materialization_id=attempt.reporting_materialization_id,
        observed_control_totals=official_totals,
        observed_canonical_content_digest=digest,
        observed_at=END + timedelta(seconds=9),
        consumer_commit_ref="official-load-0001",
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(
            replace(receipt, supersedes_reporting_receipt_id=rejected.reporting_receipt_id)
        )
    assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    official_accepted, _ = await store.record_revision_receipt(receipt)
    # Accepting the later official leaves the snapshot's rejected chain repairable.
    snapshot_accepted, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            reporting_receipt_id="snapshot-repaired-receipt-0002",
            supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
            observed_at=END + timedelta(seconds=10),
        )
    )
    history = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert history.current_receipts == (official_accepted, snapshot_accepted)
    assert history.terminal_acceptances == (official_accepted.key, snapshot_accepted.key)
    assert history.materialization(s.attempt.key).outcome == s.outcome
    assert history.materialization(attempt.key).outcome == outcome
    assert (
        await store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
        )
    ).rows == snapshot_rows
    assert await store.list_revisions(
        account_id="acct_a", reporting_obligation_id=s.obligation.reporting_obligation_id
    ) == (s.revision, official)
    assert (
        await store.read_reconciliation_snapshot(
            caller=s.attempt.scope.principal, boundary=earlier.boundary
        )
        == earlier
    )
    for accepted in (official_accepted, snapshot_accepted):
        assert await store.record_revision_receipt(accepted) == (accepted, False)
        with pytest.raises(LedgerConflictError) as error:
            await store.record_revision_receipt(
                replace(
                    accepted,
                    received_at=None,
                    reporting_receipt_id=accepted.reporting_receipt_id + "-successor",
                    supersedes_reporting_receipt_id=accepted.reporting_receipt_id,
                )
            )
        assert error.value.code == "ACCEPTED_RECEIPT_TERMINAL"
    assert await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal) == history
    core = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    GetReportingStatusResponse.model_validate(core)
    assert {item["finality"] for item in core["revisions"]} == {"snapshot", "official"}
    assert core["materializations"] == core["receipts"] == []


@pytest.mark.parametrize(
    "violation",
    ["before_finalization", "after_creation", "period", "receipt_before_creation", "digest"],
)
async def test_adjustment_ordering_and_digest_disagreement(
    reconciliation_store: tuple[Store, Clock], violation: str
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    adjustment = ReportingAdjustmentRecord(
        "adjustment-order",
        "acct_a",
        s.revision.reporting_revision_id,
        "source_correction",
        END,
        END + timedelta(days=30),
        (("spend", "-1.50"),),
        END + timedelta(seconds=5),
        END + timedelta(seconds=6),
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1.50", "decimal", "EUR"),
        ),
    )
    if violation == "before_finalization":
        adjustment = replace(adjustment, correction_observed_at=END - timedelta(seconds=1))
    if violation == "after_creation":
        adjustment = replace(adjustment, correction_observed_at=END + timedelta(seconds=8))
    if violation == "period":
        adjustment = replace(adjustment, accounting_period_end=END)
    await store.commit_adjustment(adjustment)
    digest = adjustment_to_wire(adjustment)["canonical_adjustment_sha256"]
    record = ReportingAdjustmentReceiptRecord(
        s.attempt.scope,
        "adjustment-order-receipt-1",
        adjustment.reporting_adjustment_id,
        s.revision.reporting_revision_id,
        "accepted",
        digest,
        END + timedelta(seconds=7),
    )
    if violation == "receipt_before_creation":
        record = replace(record, observed_at=END)
    if violation == "digest":
        record = replace(record, observed_adjustment_sha256="0" * 64)
    with pytest.raises(LedgerConflictError) as error:
        await store.record_adjustment_receipt(record)
    assert error.value.code == (
        "ADJUSTMENT_DIGEST_MISMATCH" if violation == "digest" else "ADJUSTMENT_ORDER_INVALID"
    )
    if violation == "digest":
        rejected, _ = await store.record_adjustment_receipt(
            replace(record, status="rejected", rejection_codes=("DIGEST_MISMATCH",))
        )
        accepted, _ = await store.record_adjustment_receipt(
            replace(
                record,
                reporting_receipt_id="adjustment-order-receipt-2",
                observed_adjustment_sha256=digest,
                supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
            )
        )
        assert accepted.status == "accepted"
        with pytest.raises(LedgerConflictError) as replay:
            await store.record_adjustment_receipt(
                replace(accepted, observed_adjustment_sha256="f" * 64)
            )
        assert replay.value.code == "REPORTING_IDENTITY_CONFLICT"


@pytest.mark.parametrize("different_scope", [False, True])
async def test_revision_fanout_requires_exact_frozen_logical_scope(
    reconciliation_store: tuple[Store, Clock], different_scope: bool
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    generation = replace(s.binding.generation_key, delivery_config_id="second-destination")
    config = replace(
        configuration(),
        delivery_config_id=generation.delivery_config_id,
        feed_purpose="billing",
        required_finality="official",
    )
    await store.put_configuration(config)
    obligation = replace(
        s.obligation,
        reporting_obligation_id="obligation-second-destination",
        delivery_config_id=generation.delivery_config_id,
        media_buy_ids=("another-buy",) if different_scope else s.obligation.media_buy_ids,
    )
    await store.commit_obligation(obligation)
    await store.put_destination_binding(
        replace(s.binding, generation_key=generation, destination_ref="destination-generation-2")
    )
    scope = ReportingDeliveryScope(generation, "buyer", obligation.reporting_obligation_id)
    await store.bind_obligation_delivery(replace(s.delivery, scope=scope))
    attempt = replace(s.attempt, scope=scope, reporting_materialization_id="fanout-materialization")
    if different_scope:
        with pytest.raises(LedgerConflictError) as error:
            await store.commit_materialization_attempt(attempt)
        assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    else:
        await store.commit_materialization_attempt(attempt)
        await store.commit_materialization(
            replace(
                s.outcome,
                scope=scope,
                reporting_materialization_id=attempt.reporting_materialization_id,
            )
        )
        receipt, _ = await store.record_revision_receipt(
            replace(
                s.receipt,
                scope=scope,
                reporting_receipt_id="fanout-receipt-0001",
                reporting_materialization_id=attempt.reporting_materialization_id,
            )
        )
        assert receipt.scope == scope


async def test_core_and_managed_delivery_only_do_not_require_receipt_components(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    # Construction and create_schema have already succeeded without any writer,
    # resolver, receipt client or outbox being supplied by the fixture.
    s = await scenario(store, billing=False, reconciliation_mode="delivery_only")
    await store.commit_materialization(s.outcome)
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(s.receipt)
    assert error.value.code == "RECEIPTS_NOT_ENABLED"
