"""The buyer's receipts policy: never on read, only after the SDK verified and you committed."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter

from adcp.reporting import ReportingObservation
from adcp.reporting._reconcile import load_reporting_ledger
from adcp.reporting.subscriptions import (
    CommitResult,
    ReceiptPolicy,
    ReceiptReport,
    Reject,
    VerifiedRevision,
)
from adcp.reporting.subscriptions.facade import ReportingClient
from adcp.reporting.subscriptions.receipts import apply_receipt_policy
from adcp.types import GetReportingStatusRequest
from adcp.types.domains.core.reporting_canonical_content_digest import (
    ReportingCanonicalContentDigest,
)
from adcp.types.domains.core.reporting_control_total import ReportingControlTotal
from tests.test_reporting_reconciliation import DIGEST, TOTALS, _Client

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _observation(rows: int = 7) -> ReportingObservation:
    return ReportingObservation(
        row_count=rows,
        control_totals=[TypeAdapter(ReportingControlTotal).validate_python(i) for i in TOTALS],
        canonical_content_digest=ReportingCanonicalContentDigest.model_validate(DIGEST),
    )


async def _ledger(client: _Client):
    request = GetReportingStatusRequest.model_validate(
        {"account": {"account_id": "account-1"}, "view": "periods"}
    )
    return await load_reporting_ledger(client, request)


def test_the_policy_is_off_by_default_and_verified_needs_commit_and_a_verifier() -> None:
    assert ReceiptPolicy().mode == "off"
    with pytest.raises(ValueError, match="needs commit"):
        ReceiptPolicy(mode="verified", inspect=lambda _c: None)
    with pytest.raises(ValueError, match="exactly one"):
        ReceiptPolicy(mode="verified", commit=lambda _r: "defer")
    with pytest.raises(ValueError):
        Reject([])


async def test_a_commit_ref_submits_an_accepted_receipt_after_verification() -> None:
    client = _Client()
    seen: list[VerifiedRevision] = []

    async def inspect(_context):
        return _observation()

    async def commit(revision: VerifiedRevision):
        seen.append(revision)
        return CommitResult("warehouse-job-42")

    report = ReceiptReport()
    policy = ReceiptPolicy(mode="verified", commit=commit, inspect=inspect)
    wrote = await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, report, now=NOW
    )
    assert wrote
    assert [r.consumer_commit_ref for r in report.submitted] == ["warehouse-job-42"]
    assert client.recorded_receipt["status"] == "accepted"
    assert seen[0].observation.row_count == 7 and seen[0].resource is not None
    # Receipted: a second run submits nothing.
    again = ReceiptReport()
    assert not await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, again, now=NOW
    )


async def test_defer_submits_nothing_and_reject_sends_your_codes() -> None:
    async def inspect(_context):
        return _observation()

    deferred = ReceiptReport()
    client = _Client()
    policy = ReceiptPolicy(mode="verified", commit=lambda _r: "defer", inspect=inspect)
    assert not await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, deferred, now=NOW
    )
    assert deferred.deferred == ["obligation-billing"] and client.recorded_receipt is None
    rejected = ReceiptReport()
    policy = ReceiptPolicy(
        mode="verified", commit=lambda _r: Reject(["WAREHOUSE_LOAD_FAILED"]), inspect=inspect
    )
    assert await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, rejected, now=NOW
    )
    assert client.recorded_receipt["status"] == "rejected"
    assert client.recorded_receipt["rejection_codes"] == ["WAREHOUSE_LOAD_FAILED"]


async def test_a_mismatch_the_sdk_found_is_rejected_without_calling_commit() -> None:
    calls: list[object] = []

    async def inspect(_context):
        return _observation(rows=6)

    client = _Client()
    report = ReceiptReport()
    policy = ReceiptPolicy(mode="verified", commit=calls.append, inspect=inspect)
    assert await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, report, now=NOW
    )
    assert calls == []
    assert [r.rejection_codes for r in report.rejected] == [["ROW_COUNT_MISMATCH"]]


async def test_off_submits_nothing() -> None:
    client = _Client()
    report = ReceiptReport()
    assert not await apply_receipt_policy(
        client, "account-1", await _ledger(client), ReceiptPolicy(), report, now=NOW
    )
    assert client.recorded_receipt is None


async def test_submit_receipts_is_the_manual_path() -> None:
    client = _Client()
    reporting = ReportingClient(client)
    from adcp.reporting import build_reporting_receipt
    from adcp.reporting._reconcile import ReportingInspectionContext, _validate_read_ledger

    ledger = await _ledger(client)
    _leaves, _complete, index = _validate_read_ledger(ledger)
    obligation = ledger.obligations[0]
    revision, materialization, _ = index.selections[obligation.reporting_obligation_id]
    receipt = build_reporting_receipt(
        ReportingInspectionContext(obligation, revision, materialization), _observation()
    )
    results = await reporting.submit_receipts("account-1", receipts=[receipt])
    assert [str(getattr(r.result, "value", r.result)) for r in results] == ["recorded"]
    from adcp.reporting.subscriptions import ReportingSubscriptionError

    with pytest.raises(ReportingSubscriptionError, match="Nothing to submit"):
        await reporting.submit_receipts("account-1")


async def test_a_rejection_is_sent_once_and_an_unverifiable_revision_is_not_receipted() -> None:
    async def inspect(_context):
        return _observation()

    client = _Client()
    policy = ReceiptPolicy(
        mode="verified", commit=lambda _r: Reject(["LOAD_FAILED"]), inspect=inspect
    )
    first = ReceiptReport()
    assert await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, first, now=NOW
    )
    again = ReceiptReport()
    assert not await apply_receipt_policy(
        client, "account-1", await _ledger(client), policy, again, now=NOW
    )
    assert again.rejected == []

    from adcp.reporting_inspection import ReportingInspectionError

    async def refusing(_context):
        raise ReportingInspectionError("MANIFEST_DIGEST_MISMATCH", "the manifest does not match")

    fresh = _Client()
    failed = ReceiptReport()
    policy = ReceiptPolicy(mode="verified", commit=lambda _r: "defer", inspect=refusing)
    assert not await apply_receipt_policy(
        fresh, "account-1", await _ledger(fresh), policy, failed, now=NOW
    )
    assert failed.failed == [("obligation-billing", "MANIFEST_DIGEST_MISMATCH")]
    assert fresh.recorded_receipt is None
