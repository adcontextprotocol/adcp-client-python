"""Shared evidence, privacy and retention boundaries, independent of persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, replace
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
    revision_content_sha256,
    revision_to_wire,
)
from adcp.types import ReportingMaterialization, ReportingReceipt, ReportingRevision

from ._generation_support import END, NOW
from ._reconciliation_support import Clock, Store, scenario


async def test_each_immutable_record_rejects_changed_replay(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    check = ReportingMaterializationCheck(
        s.attempt.scope, s.attempt.reporting_materialization_id, "check-immutable", "readable", NOW
    )
    await store.record_materialization_check(check)
    receipt, _ = await store.record_revision_receipt(s.receipt)
    candidates = [
        (
            store.put_destination_binding,
            replace(s.binding, trusted_binding_ref="trusted-generation-2"),
        ),
        (
            store.bind_obligation_delivery,
            replace(s.delivery, resource_retained_until=NOW + timedelta(days=900)),
        ),
        (store.commit_materialization_attempt, replace(s.attempt, attempt=2)),
        (store.record_materialization_check, replace(check, state="corrupt")),
        (store.record_revision_receipt, replace(receipt, received_at=NOW + timedelta(seconds=1))),
    ]
    for write, changed in candidates:
        with pytest.raises(LedgerConflictError) as error:
            await write(changed)
        assert error.value.code == "REPORTING_IDENTITY_CONFLICT"
    assert (
        await store.get_destination_binding(
            caller=s.binding.principal, generation_key=s.binding.generation_key
        )
        == s.binding
    )
    assert await store.get_obligation_delivery(s.delivery.scope) == s.delivery


async def test_native_versions_preserve_decoded_value_and_require_exact_path_evidence(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(
        store, method="warehouse_materialization", profile="native_commit", billing=False
    )
    native = "/decoded+native=version/" + "a" * 800
    resource = replace(s.outcome.resource, native_version_ref=native)
    verification = replace(s.outcome.verification, native_version_ref=native)
    outcome = replace(s.outcome, resource=resource, verification=verification)
    for wrong in [
        replace(verification, native_version_ref="different-version"),
        replace(verification, native_observed_through="representative_consumer"),
    ]:
        with pytest.raises(LedgerConflictError) as error:
            await store.commit_materialization(replace(outcome, verification=wrong))
        assert error.value.code == "NATIVE_COMMIT_MISMATCH"
    await store.commit_materialization(outcome)
    view = await store.get_materialization(s.attempt.key)
    projected = ReportingMaterialization.model_validate(view.to_wire())
    assert projected.resource.native_version_ref.root == native
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(s.receipt)
    assert error.value.code == "RECEIPT_EVIDENCE_MISMATCH"
    receipt, _ = await store.record_revision_receipt(
        replace(s.receipt, observed_native_version_ref=native)
    )
    from adcp.reporting.ledger import receipt_to_wire

    assert (
        ReportingReceipt.model_validate(receipt_to_wire(receipt)).observed_native_version_ref.root
        == native
    )


async def test_competing_attempt_ids_cannot_claim_the_same_ordinal(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    results = await asyncio.gather(
        *(
            store.commit_materialization_attempt(
                replace(s.attempt, reporting_materialization_id=f"competing-attempt-{i}", attempt=2)
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert all(
        isinstance(result, tuple)
        or (
            isinstance(result, LedgerConflictError)
            and result.code == "MATERIALIZATION_ATTEMPT_CONFLICT"
        )
        for result in results
    )


async def test_cross_account_and_principal_keys_are_indistinguishable_from_absence(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    owner = await scenario(store)
    other = await scenario(store, account_id="acct_b", consumer_id="another-buyer")
    await store.commit_materialization(owner.outcome)
    await store.record_revision_receipt(owner.receipt)
    for caller in [
        ReportingDeliveryPrincipal("unknown-account", "buyer"),
        ReportingDeliveryPrincipal("acct_a", "unknown-consumer"),
    ]:
        assert (
            await store.get_destination_binding(
                caller=caller, generation_key=owner.binding.generation_key
            )
            is None
        )
        assert (
            await store.get_obligation_delivery(
                replace(
                    owner.delivery.scope,
                    consumer_id=caller.consumer_id,
                    generation_key=replace(
                        owner.binding.generation_key, account_id=caller.account_id
                    ),
                )
            )
            is None
        )
        for target in [owner.attempt.reporting_materialization_id, "missing-materialization"]:
            assert (
                await store.get_materialization(ReportingMaterializationKey(caller, target)) is None
            )
        for target in [owner.receipt.reporting_receipt_id, "missing-receipt-0001"]:
            assert await store.get_receipt(ReportingReceiptKey(caller, target)) is None
    # This principal has a valid account binding, but the foreign revision and
    # obligation must still have exactly the same result as nonexistent IDs.
    for field, existing, missing in [
        ("reporting_revision_id", owner.revision.reporting_revision_id, "missing-revision"),
        ("reporting_obligation_id", owner.obligation.reporting_obligation_id, "missing-obligation"),
    ]:
        errors = []
        for target in [existing, missing]:
            attempt = replace(
                other.attempt, reporting_materialization_id="foreign-attempt", attempt=2
            )
            if field == "reporting_revision_id":
                attempt = replace(attempt, reporting_revision_id=target)
            else:
                attempt = replace(
                    attempt, scope=replace(attempt.scope, reporting_obligation_id=target)
                )
            with pytest.raises(LedgerConflictError) as error:
                await store.commit_materialization_attempt(attempt)
            errors.append((error.value.code, str(error.value)))
        assert errors == [("REPORTING_RECORD_UNAVAILABLE", "reporting record is unavailable")] * 2


async def test_receipt_kinds_share_identity_but_never_replacement_chains(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    rejected, _ = await store.record_revision_receipt(
        replace(s.receipt, status="rejected", rejection_codes=("LOAD_FAILED",))
    )
    adjustment = ReportingAdjustmentRecord(
        "adjustment-namespace",
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
    await store.commit_adjustment(adjustment)
    receipt = ReportingAdjustmentReceiptRecord(
        s.attempt.scope,
        rejected.reporting_receipt_id,
        adjustment.reporting_adjustment_id,
        s.revision.reporting_revision_id,
        "accepted",
        adjustment_to_wire(adjustment)["canonical_adjustment_sha256"],
        END + timedelta(seconds=7),
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.record_adjustment_receipt(receipt)
    assert error.value.code == "REPORTING_IDENTITY_CONFLICT"
    with pytest.raises(LedgerConflictError) as error:
        await store.record_adjustment_receipt(
            replace(
                receipt,
                reporting_receipt_id="adjustment-own-receipt-0001",
                supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
            )
        )
    assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    for caller in ["other-consumer", "missing-consumer"]:
        with pytest.raises(LedgerConflictError) as error:
            await store.record_adjustment_receipt(
                replace(receipt, scope=replace(receipt.scope, consumer_id=caller))
            )
        assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"


async def test_readability_history_and_receipts_use_observation_time(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, clock = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    clock.now = END + timedelta(seconds=20)
    corrupt = ReportingMaterializationCheck(
        s.attempt.scope,
        s.attempt.reporting_materialization_id,
        "corrupt-check",
        "corrupt",
        END + timedelta(seconds=8),
    )
    await store.record_materialization_check(corrupt)
    observed_bad = replace(s.receipt, observed_at=END + timedelta(seconds=9))
    with pytest.raises(LedgerConflictError) as error:
        await store.record_revision_receipt(observed_bad)
    assert error.value.code == "MATERIALIZATION_UNREADABLE"
    repaired = replace(
        corrupt, check_id="repaired-check", state="readable", checked_at=END + timedelta(seconds=10)
    )
    await store.record_materialization_check(repaired)
    with pytest.raises(LedgerConflictError) as error:
        await store.record_materialization_check(replace(corrupt, check_id="out-of-order-check"))
    assert error.value.code == "REPORTING_TIME_INVALID"
    view = await store.get_materialization(s.attempt.key)
    assert view.readable_at(END + timedelta(seconds=7))
    assert not view.readable_at(END + timedelta(seconds=9))
    assert view.readable_at(END + timedelta(seconds=11))
    assert not view.readable_at(view.outcome.resource.expires_at)
    accepted, _ = await store.record_revision_receipt(
        replace(s.receipt, observed_at=END + timedelta(seconds=11))
    )
    clock.now = view.outcome.resource.expires_at
    with pytest.raises(LedgerConflictError) as error:
        await store.record_materialization_check(
            replace(repaired, check_id="expired-check", checked_at=clock.now)
        )
    assert error.value.code == "RESOURCE_RETENTION_INVALID"
    assert await store.get_receipt(accepted.key) == accepted


async def test_caller_owned_collections_are_frozen_before_retention(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    totals = list(s.outcome.verification.control_totals)
    checksums = list(s.outcome.verification.physical_checksums)
    objects = list(s.outcome.resource.object_refs)
    evidence = replace(s.outcome.verification, control_totals=totals, physical_checksums=checksums)
    outcome = replace(
        s.outcome, verification=evidence, resource=replace(s.outcome.resource, object_refs=objects)
    )
    totals.clear()
    checksums.clear()
    objects.clear()
    assert await store.commit_materialization(outcome) == (s.outcome, True)
    with pytest.raises(ValueError) as error:
        replace(s.receipt, observed_canonical_content_digest={"credential": "MUST_NOT_RETAIN"})
    assert "MUST_NOT_RETAIN" not in str(error.value)
    snapshot = await store.read_reconciliation_snapshot(caller=s.attempt.scope.principal)
    assert "MUST_NOT_RETAIN" not in json.dumps(asdict(snapshot), default=str)


async def test_managed_revision_replay_binds_rows_metadata_and_canonical_contract(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    page = await store.read_revision_rows(
        account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
    )
    assert await store.commit_revision(s.revision, page.rows) == s.revision
    writes = await asyncio.gather(*(store.commit_revision(s.revision, page.rows) for _ in range(6)))
    assert all(item == s.revision for item in writes)
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(s.revision, [{**page.rows[0], "spend": "15.00"}])
    assert error.value.code == "REVISION_CONTENT_MISMATCH"
    for changed in [
        replace(s.revision, finalized_at=END + timedelta(seconds=1)),
        replace(
            s.revision,
            canonical_content_digest=replace(
                s.revision.canonical_content_digest, canonicalization_id="different-contract"
            ),
        ),
    ]:
        with pytest.raises(LedgerConflictError) as error:
            await store.commit_revision(changed, page.rows)
        assert error.value.code == "REVISION_IMMUTABLE"
    # Memory must not hand out mutable references to retained row content.
    page.rows[0]["spend"] = "999.00"
    retained = await store.read_revision_rows(
        account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
    )
    assert retained.rows[0]["spend"] == "12.50"


async def test_expected_total_types_and_nonmonetary_units_survive_storage(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    totals = (
        ReportingControlTotalRecord("impressions", "5", "decimal", "impressions"),
        ReportingControlTotalRecord("spend", "12.50", "decimal", "EUR"),
    )
    s = await scenario(store, control_total_evidence=totals)
    await store.commit_materialization(s.outcome)
    receipt, _ = await store.record_revision_receipt(s.receipt)
    retained = await store.get_revision(
        account_id="acct_a", reporting_revision_id=s.revision.reporting_revision_id
    )
    assert retained.managed_control_totals == totals
    projected = ReportingRevision.model_validate(
        revision_to_wire(retained, obligation=s.obligation)
    ).model_dump(mode="json", exclude_none=True)
    assert projected["control_totals"] == [total.to_wire() for total in totals]
    assert receipt.observed_control_totals == totals
    rows = (
        await store.read_revision_rows(
            account_id="acct_a", reporting_revision_id=retained.reporting_revision_id
        )
    ).rows
    digest_input = {
        "reporting_revision_id": retained.reporting_revision_id,
        "row_count": retained.row_count,
        "control_totals": projected["control_totals"],
        "reporting_rows": list(rows),
    }
    assert (
        hashlib.sha256(canonical_json_utf8_v1(digest_input)).hexdigest()
        == retained.revision_content_sha256
    )
    core = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller("acct_a", "buyer")
    )
    assert core["revisions"][0]["control_totals"] == projected["control_totals"]
    assert core["materializations"] == core["receipts"] == []
    changed_totals = (replace(totals[0], value_type="integer"), totals[1])
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(
            replace(
                retained,
                managed_control_totals=changed_totals,
                revision_content_sha256=revision_content_sha256(
                    reporting_revision_id=retained.reporting_revision_id,
                    row_count=retained.row_count,
                    control_totals=retained.control_totals,
                    reporting_rows=rows,
                    control_total_evidence=changed_totals,
                ),
            ),
            rows,
        )
    assert error.value.code == "REVISION_IMMUTABLE"


async def test_adjustment_hash_retains_declared_type_unit_and_rejects_changed_replay(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    total = ReportingControlTotalRecord("spend", "-1", "decimal", "EUR")
    adjustment = ReportingAdjustmentRecord(
        "typed-adjustment",
        "acct_a",
        s.revision.reporting_revision_id,
        "source_correction",
        END,
        END + timedelta(days=30),
        (("spend", "-1"),),
        END + timedelta(seconds=5),
        END + timedelta(seconds=6),
        managed_control_total_deltas=(total,),
    )
    retained = await store.commit_adjustment(adjustment)
    assert retained.managed_control_total_deltas == (total,)
    wire = adjustment_to_wire(retained)
    assert wire["control_total_deltas"] == [total.to_wire()]
    changed = replace(
        adjustment, managed_control_total_deltas=(replace(total, value_type="integer"),)
    )
    assert (
        adjustment_to_wire(changed)["canonical_adjustment_sha256"]
        != wire["canonical_adjustment_sha256"]
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_adjustment(changed)
    assert error.value.code == "ADJUSTMENT_IMMUTABLE"
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_adjustment(
            replace(
                adjustment,
                reporting_adjustment_id="wrong-currency-adjustment",
                managed_control_total_deltas=(replace(total, unit="USD"),),
            )
        )
    assert error.value.code == "CURRENCY_MISMATCH"


async def test_zero_row_revision_is_verifiable_and_missing_canonical_evidence_is_not(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store, billing=False, finality="snapshot")
    totals = (("impressions", "0"), ("spend", "0.00"))
    evidence = (
        ReportingControlTotalRecord("impressions", "0", "integer"),
        ReportingControlTotalRecord("spend", "0.00", "decimal", "EUR"),
    )
    digest = replace(s.revision.canonical_content_digest, value=hashlib.sha256(b"[]").hexdigest())
    zero = replace(
        s.revision,
        reporting_revision_id="empty-revision",
        row_count=0,
        control_totals=totals,
        canonical_content_digest=digest,
        managed_control_totals=evidence,
        supersedes_reporting_revision_id=s.revision.reporting_revision_id,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="empty-revision",
            row_count=0,
            control_totals=totals,
            reporting_rows=[],
            control_total_evidence=evidence,
        ),
    )
    await store.commit_revision(zero, [])
    attempt = replace(
        s.attempt,
        reporting_revision_id=zero.reporting_revision_id,
        reporting_materialization_id="empty-materialization",
    )
    await store.commit_materialization_attempt(attempt)
    outcome = replace(
        s.outcome,
        reporting_revision_id=zero.reporting_revision_id,
        reporting_materialization_id=attempt.reporting_materialization_id,
        resource=replace(
            s.outcome.resource,
            resource_ref="empty-resource",
            location="reports/empty/manifest.json",
        ),
        verification=replace(
            s.outcome.verification,
            row_count=0,
            control_totals=evidence,
            canonical_content_digest=digest,
        ),
    )
    await store.commit_materialization(outcome)
    accepted, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            reporting_receipt_id="empty-revision-receipt-0001",
            reporting_revision_id=zero.reporting_revision_id,
            reporting_materialization_id=attempt.reporting_materialization_id,
            observed_row_count=0,
            observed_control_totals=evidence,
            observed_canonical_content_digest=digest,
        )
    )
    assert accepted.observed_row_count == 0
    unverified = replace(
        zero,
        reporting_revision_id="legacy-revision-without-digest",
        canonical_content_digest=None,
        supersedes_reporting_revision_id=zero.reporting_revision_id,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="legacy-revision-without-digest",
            row_count=0,
            control_totals=totals,
            reporting_rows=[],
            control_total_evidence=evidence,
        ),
    )
    await store.commit_revision(unverified, [])
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_materialization_attempt(
            replace(
                attempt,
                reporting_revision_id=unverified.reporting_revision_id,
                reporting_materialization_id="unverified-materialization",
            )
        )
    assert error.value.code == "REVISION_CANONICAL_EVIDENCE_REQUIRED"


def test_core_import_and_startup_with_optional_dependencies_unavailable() -> None:
    script = """
import importlib.abc
import sys

class NoOptionalProviders(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'psycopg', 'psycopg_pool', 'boto3', 'google'}:
            raise ImportError('optional provider dependency is unavailable')

sys.meta_path.insert(0, NoOptionalProviders())
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore, InMemoryReportingReconciliationStore, ReportingStatusHandler,
)
ReportingStatusHandler(InMemoryReportingLedgerStore())
ReportingStatusHandler(InMemoryReportingReconciliationStore())
assert 'adcp.reporting.ledger.delivery_pg' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
