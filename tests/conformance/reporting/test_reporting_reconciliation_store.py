"""The same state machine, including races and hostile references, on memory and real PG."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import timedelta

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger import (
    LedgerConflictError,
    ReportingAdjustmentReceiptRecord,
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingMaterializationCheck,
    ReportingMaterializationKey,
    ReportingReceiptKey,
    ReportingStatusCaller,
    ReportingStatusHandler,
    adjustment_to_wire,
    materialization_to_wire,
    receipt_to_wire,
    revision_to_wire,
)
from adcp.types import (
    GetReportingStatusResponse,
    ReportingMaterialization,
    ReportingReceipt,
    ReportingRevision,
)

from ._generation_support import END, NOW
from ._reconciliation_support import Clock, Store, scenario


async def test_attempt_outcome_replay_and_generated_projections(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    before = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    pending = before.materialization(s.attempt.key)
    assert pending is not None and pending.outcome is None and not pending.readable_at(NOW)
    assert ReportingMaterialization.model_validate(pending.to_wire()).status.value == "pending"
    stored, created = await store.commit_materialization(s.outcome)
    assert created and stored == s.outcome
    assert await store.commit_materialization(s.outcome) == (stored, False)
    assert await store.commit_materialization_attempt(s.attempt) == (s.attempt, False)
    assert await store.put_destination_binding(s.binding) == (s.binding, False)
    assert await store.bind_obligation_delivery(s.delivery) == (s.delivery, False)
    with pytest.raises(LedgerConflictError, match="retained evidence") as conflict:
        await store.commit_materialization(
            replace(s.outcome, completed_at=END + timedelta(seconds=5))
        )
    assert conflict.value.code == "REPORTING_IDENTITY_CONFLICT"
    view = await store.get_materialization(s.attempt.key)
    assert view is not None and view.readable_at(NOW)
    projection = materialization_to_wire(view, obligation=s.obligation)
    assert ReportingMaterialization.model_validate(projection).verification.row_count == 1
    assert s.binding.trusted_binding_ref not in json.dumps(projection)
    ReportingRevision.model_validate(revision_to_wire(s.revision, obligation=s.obligation))
    historical = await store.read_reconciliation_snapshot(
        caller=s.attempt.scope.principal, boundary=before.boundary
    )
    assert historical == before


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("rows", "VERIFICATION_TOTALS_MISMATCH"),
        ("totals", "VERIFICATION_TOTALS_MISMATCH"),
        ("digest", "VERIFICATION_DIGEST_MISMATCH"),
        ("format", "VERIFICATION_PROFILE_MISMATCH"),
        ("profile", "VERIFICATION_PROFILE_MISMATCH"),
        ("time", "VERIFICATION_TIME_MISMATCH"),
        ("path", "MATERIALIZATION_STATUS_MISMATCH"),
        ("checksums", "PHYSICAL_CHECKSUMS_REQUIRED"),
        ("objects", "PHYSICAL_CHECKSUM_BINDING_MISMATCH"),
        ("retention", "RESOURCE_RETENTION_INVALID"),
        ("reader", "READER_COMPATIBILITY_MISMATCH"),
    ],
)
async def test_verification_gate(
    reconciliation_store: tuple[Store, Clock], mutation: str, code: str
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    v, r = s.outcome.verification, s.outcome.resource
    assert v is not None and r is not None and v.canonical_content_digest is not None
    outcome = s.outcome
    if mutation == "rows":
        v = replace(v, row_count=2)
    if mutation == "totals":
        v = replace(
            v, control_totals=(replace(v.control_totals[0], value="6"), v.control_totals[1])
        )
    if mutation == "digest":
        v = replace(v, canonical_content_digest=replace(v.canonical_content_digest, value="0" * 64))
    if mutation == "format":
        v = replace(v, verified_format="csv")
    if mutation == "profile":
        v = replace(v, verification_profile="native_commit")
    if mutation == "time":
        v = replace(v, verified_at=END)
    if mutation == "path":
        outcome = replace(outcome, status="delivered")
    if mutation == "checksums":
        v = replace(v, physical_checksums=())
    if mutation == "objects":
        r = replace(r, object_refs=("another/part.jsonl",))
    if mutation == "retention":
        r = replace(r, expires_at=NOW)
    if mutation == "reader":
        r = replace(r, reader_compatibility=())
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_materialization(replace(outcome, verification=v, resource=r))
    assert error.value.code == code
    assert (await store.get_materialization(s.attempt.key)).outcome is None
    await store.commit_materialization(s.outcome)


async def test_rejected_replacement_and_terminal_acceptance(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    rejected = replace(
        s.receipt, status="rejected", observed_row_count=99, rejection_codes=("ROW_COUNT_MISMATCH",)
    )
    first, written = await store.record_revision_receipt(rejected)
    assert written and first.received_at == NOW
    assert await store.record_revision_receipt(rejected) == (first, False)
    replacement = replace(
        s.receipt,
        reporting_receipt_id="receipt-replacement-0002",
        supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
    )
    accepted, written = await store.record_revision_receipt(replacement)
    assert written
    assert await store.record_revision_receipt(first) == (first, False)
    assert await store.record_revision_receipt(replacement) == (accepted, False)
    assert await store.record_revision_receipt(accepted) == (accepted, False)
    ReportingReceipt.model_validate(receipt_to_wire(first))
    ReportingReceipt.model_validate(receipt_to_wire(accepted))
    with pytest.raises(LedgerConflictError) as conflict:
        await store.record_revision_receipt(
            replace(
                replacement,
                reporting_receipt_id="receipt-third-0003",
                supersedes_reporting_receipt_id=accepted.reporting_receipt_id,
            )
        )
    assert conflict.value.code == "ACCEPTED_RECEIPT_TERMINAL"
    with pytest.raises(LedgerConflictError) as conflict:
        await store.record_revision_receipt(
            replace(replacement, consumer_commit_ref="another-load")
        )
    assert conflict.value.code == "REPORTING_IDENTITY_CONFLICT"
    snapshot = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert snapshot.current_receipts == (accepted,)
    assert snapshot.terminal_acceptances == (accepted.key,)


async def test_retry_materialization_does_not_reset_receipt_chain(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    rejected, _ = await store.record_revision_receipt(
        replace(s.receipt, status="rejected", rejection_codes=("CONTENT_MISMATCH",))
    )
    retry = replace(s.attempt, reporting_materialization_id="materialization-2", attempt=2)
    await store.commit_materialization_attempt(retry)
    await store.commit_materialization(
        replace(s.outcome, reporting_materialization_id=retry.reporting_materialization_id)
    )
    receipt = replace(
        s.receipt,
        reporting_receipt_id="receipt-for-retry-0002",
        reporting_materialization_id=retry.reporting_materialization_id,
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(receipt)
    assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    accepted, _ = await store.record_revision_receipt(
        replace(receipt, supersedes_reporting_receipt_id=rejected.reporting_receipt_id)
    )
    assert accepted.status == "accepted"


async def test_corruption_and_retention_preserve_receipts_and_snapshots(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, clock = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    accepted, _ = await store.record_revision_receipt(s.receipt)
    before = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    corrupt = ReportingMaterializationCheck(
        s.attempt.scope, s.attempt.reporting_materialization_id, "check-1", "corrupt", NOW
    )
    assert (await store.record_materialization_check(corrupt))[1]
    assert not (await store.record_materialization_check(corrupt))[1]
    assert not (await store.get_materialization(s.attempt.key)).readable_at(NOW)
    assert (
        await store.read_reconciliation_snapshot(
            caller=s.attempt.scope.principal, boundary=before.boundary
        )
    ) == before
    assert before.materialization(s.attempt.key).readable_at(NOW)
    clock.now = NOW + timedelta(days=500)
    await store.set_revision_readable(
        account_id=s.revision.account_id,
        reporting_revision_id=s.revision.reporting_revision_id,
        readable=False,
    )
    assert await store.record_revision_receipt(s.receipt) == (accepted, False)
    assert await store.commit_materialization(s.outcome) == (s.outcome, False)
    assert await store.get_receipt(accepted.key) == accepted
    assert not (await store.get_materialization(s.attempt.key)).readable_at(clock.now)


async def test_concurrent_workers_converge_without_terminal_races(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    writes = await asyncio.gather(*(store.commit_materialization(s.outcome) for _ in range(12)))
    assert sum(created for _, created in writes) == 1
    rejected, _ = await store.record_revision_receipt(
        replace(s.receipt, status="rejected", rejection_codes=("CONTENT_MISMATCH",))
    )
    receipts = [
        replace(
            s.receipt,
            reporting_receipt_id=f"receipt-concurrent-{i:04}",
            supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
        )
        for i in range(12)
    ]
    outcomes = await asyncio.gather(
        *(store.record_revision_receipt(item) for item in receipts), return_exceptions=True
    )
    assert sum(isinstance(item, tuple) for item in outcomes) == 1
    assert all(
        isinstance(item, tuple) or isinstance(item, LedgerConflictError) for item in outcomes
    )
    snapshot = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert len(snapshot.terminal_acceptances) == 1
    assert len([r for r in snapshot.records if r.kind == "revision_receipt"]) == 2


async def test_account_principal_isolation_and_shared_identifiers(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    first = await scenario(store)
    other_account = await scenario(store, account_id="acct_b")
    other_principal = await scenario(store, consumer_id="other-buyer")
    for s in [first, other_account, other_principal]:
        await store.commit_materialization(s.outcome)
        await store.record_revision_receipt(s.receipt)
    for s in [first, other_account, other_principal]:
        records = (
            await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
        ).records
        assert all(
            (r.principal if hasattr(r, "principal") else r.scope.principal)
            == s.attempt.scope.principal
            for r in records
        )
        assert (await store.get_receipt(s.receipt.key)).scope == s.receipt.scope
    stranger = ReportingDeliveryPrincipal("acct_a", "stranger")
    assert (
        await store.get_receipt(ReportingReceiptKey(stranger, first.receipt.reporting_receipt_id))
        is None
    )
    assert (
        await store.get_materialization(
            ReportingMaterializationKey(stranger, first.attempt.reporting_materialization_id)
        )
        is None
    )
    errors = []
    for revision_id in [first.revision.reporting_revision_id, "unknown-revision"]:
        with pytest.raises(LedgerConflictError) as error:
            await store.record_revision_receipt(
                replace(
                    first.receipt,
                    scope=replace(first.attempt.scope, consumer_id="stranger"),
                    reporting_revision_id=revision_id,
                )
            )
        errors.append((error.value.code, str(error.value)))
    assert errors[0] == errors[1]


async def test_adjustment_digest_finality_and_independent_receipt_chain(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    adjustment = ReportingAdjustmentRecord(
        "adjustment-1",
        s.obligation.account_id,
        s.revision.reporting_revision_id,
        "source_correction",
        END,
        END + timedelta(days=30),
        (("spend", "-1.50"),),
        END + timedelta(seconds=5),
        END + timedelta(seconds=6),
        reason_detail="Late correction",
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1.50", "decimal", "EUR"),
        ),
    )
    await store.commit_adjustment(adjustment)
    wire = adjustment_to_wire(adjustment)
    expected = wire.pop("canonical_adjustment_sha256")
    assert expected == hashlib.sha256(canonical_json_utf8_v1(wire)).hexdigest()
    wire["canonical_adjustment_sha256"] = expected
    GetReportingStatusResponse.model_validate({"status": "completed", "adjustments": [wire]})
    record = ReportingAdjustmentReceiptRecord(
        s.attempt.scope,
        "adjustment-receipt-0001",
        adjustment.reporting_adjustment_id,
        s.revision.reporting_revision_id,
        "accepted",
        expected,
        END + timedelta(seconds=7),
    )
    # The schema defines independent evidence; it does not require acceptance
    # of the official receipt first. Completion still needs both receipts.
    accepted, _ = await store.record_adjustment_receipt(record)
    GetReportingStatusResponse.model_validate(
        {"status": "completed", "adjustment_receipts": [receipt_to_wire(accepted)]}
    )
    assert await store.record_adjustment_receipt(record) == (accepted, False)
    with pytest.raises(LedgerConflictError) as error:
        await store.record_adjustment_receipt(
            replace(
                record,
                reporting_receipt_id="adjustment-receipt-0002",
                supersedes_reporting_receipt_id=record.reporting_receipt_id,
            )
        )
    assert error.value.code == "ACCEPTED_RECEIPT_TERMINAL"
    await store.commit_materialization(s.outcome)
    await store.record_revision_receipt(s.receipt)
    assert (
        len(
            (
                await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
            ).terminal_acceptances
        )
        == 2
    )


async def test_core_handler_keeps_empty_higher_tier_projection(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    await store.record_revision_receipt(s.receipt)
    result = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    assert result["materializations"] == result["receipts"] == []
    assert result["periods"][0]["reconciliation_mode"] == "delivery_only"


async def test_immutable_records_and_public_metadata_rejection(
    reconciliation_store: tuple[Store, Clock], caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    assert s.outcome.resource is not None
    with pytest.raises((FrozenInstanceError, AttributeError)):
        s.outcome.status = "failed"
    sentinel = "Bearer DO_NOT_RETAIN_123"
    for metadata in [
        sentinel,
        "https://example.test/data?X-Amz-Signature=DO_NOT_RETAIN_123",
        "password=test",
    ]:
        with pytest.raises(ValueError) as error:
            replace(s.outcome.resource, location=metadata)
        assert metadata not in str(error.value)
        assert metadata not in caplog.text
        assert "DO_NOT_RETAIN_123" not in str(error.value)
    assert "DO_NOT_RETAIN_123" not in caplog.text
    assert "DO_NOT_RETAIN_123" not in json.dumps(asdict(s.outcome), default=str)
