"""Historical selections, receipts, ownership and mutable Core boundaries."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.feed import ReportingFeedError
from adcp.reporting.ledger import (
    ReportingControlTotalRecord,
    ReportingMaterializationCheck,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.ledger.status_projection import mismatch_key

from ._durable_materializer_support import durable_case
from ._feed_support import (
    ARRAYS,
    feed_harness,
    feed_request,
    feeds,
    mixed_case,
    restart,
    walk,
    without_feed,
)
from ._receipt_support import adjustment_for, extra_materialization, receipt_case, request_for
from .test_reporting_notification_outbox import statement

__all__ = ["feeds"]


@pytest.mark.parametrize("feedback", [False, True])
async def test_legacy_status_without_obligation_id_closes_over_its_exact_revision(feeds, feedback):
    from adcp.reporting.ledger.status import _consumer_status_to_wire

    h = feeds
    s, _, _ = await mixed_case(h)
    _, _, checkpoint = await walk(h.store, feed_request(s), s.binding.principal)
    legacy = replace(
        statement(s.obligation, s.binding.consumer_id),
        consumer_status="received",
        reporting_obligation_id=None,
        reporting_revision_id=s.revision.reporting_revision_id,
        observed_revision_content_sha256=s.revision.revision_content_sha256,
    )
    # The approved public Core API accepts a revision without repeating its
    # owner's ID. Its exact revision record supplies the dependency, never scope.
    await h.store.record_consumer_status_with_lifecycle(legacy)
    before = without_feed(await h.image())
    pages, rows, _ = await walk(
        h.store,
        feed_request(s, changes_after=checkpoint),
        s.binding.principal,
        consumer_status_enabled=feedback,
    )
    assert len(pages) == 3
    assert rows["consumer_statuses"] == [_consumer_status_to_wire(legacy)]
    assert rows["periods"][0]["reporting_obligation_id"] == s.obligation.reporting_obligation_id
    assert rows["revisions"][0]["reporting_revision_id"] == s.revision.reporting_revision_id
    snapshot = await h.store.read_reporting_feed_snapshot(
        pages[0]["ledger_snapshot_id"], caller=s.binding.principal
    )
    member = next(
        entry
        for entry in snapshot.inputs["dependency_membership"]
        if entry["record"] == ["consumer_status", legacy.reporting_status_id]
    )
    assert member["dependencies"] == [
        ["obligation", s.obligation.reporting_obligation_id],
        ["revision", s.revision.reporting_revision_id],
    ]
    assert without_feed(await h.image()) == before


async def test_snapshot_restatement_and_official_keep_exact_receipt_adjustment_targets(
    feeds,
):
    h = feeds
    case = await durable_case(h.store, count=3, reconciliation_mode="consumer_receipt")
    feed_operation_1 = await case.service().run_once()
    assert (feed_operation_1).state == "verified"
    request = {
        "account": {"account_id": case.config.account_id},
        "view": "periods",
        "pagination": {"max_results": 1},
    }
    _, initial, checkpoint = await walk(h.store, request, case.scope.principal)
    assert len(initial["materializations"]) == 1
    restatement = await case.publish(
        "restated-snapshot", finality="snapshot", supersedes=case.revision.reporting_revision_id
    )
    feed_operation_2 = await case.service().run_once()
    assert (feed_operation_2).state == "verified"
    outcomes = await case.outcomes()
    retained_outcome = next(
        r for r in outcomes if r.reporting_revision_id == restatement.reporting_revision_id
    )
    evidence = retained_outcome.verification
    receipt = ReportingRevisionReceiptRecord(
        case.scope,
        "accepted-snapshot-artifact",
        restatement.reporting_revision_id,
        retained_outcome.reporting_materialization_id,
        "accepted",
        evidence.verification_profile,
        evidence.row_count,
        evidence.control_totals,
        retained_outcome.completed_at,
        observed_canonical_content_digest=evidence.canonical_content_digest,
    )
    admitted = await h.store.ingest_receipt_batch(
        {
            "account": request["account"],
            "idempotency_key": "snapshot-receipt",
            "receipts": [receipt_to_wire(receipt)],
        },
        caller=case.scope.principal,
    )
    assert admitted["results"][0]["result"] == "recorded"
    official = await case.publish()
    assert official.supersedes_reporting_revision_id is None
    official_case = replace(case, revision=official)
    adjustment = await adjustment_for(
        h,
        official_case,
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1.50", "decimal", case.obligation.currency),
        ),
    )
    admitted = await h.store.ingest_receipt_batch(
        {
            "account": request["account"],
            "idempotency_key": "official-and-adjustment",
            "adjustment_receipts": [adjustment],
        },
        caller=case.scope.principal,
    )
    assert all(r["result"] == "recorded" for r in admitted["results"])
    before = without_feed(await h.image())
    pages, rows, _ = await walk(
        h.store, {**request, "changes_after": checkpoint}, case.scope.principal
    )
    snapshot = await h.store.read_reporting_feed_snapshot(
        pages[0]["ledger_snapshot_id"], caller=case.scope.principal
    )
    assert len(rows["periods"]) == 1
    assert {r["reporting_revision_id"] for r in rows["revisions"]} == {
        case.revision.reporting_revision_id,
        official.reporting_revision_id,
        restatement.reporting_revision_id,
    }
    assert (
        snapshot.inputs["selections"][case.obligation.reporting_obligation_id][
            "reporting_revision_id"
        ]
        == official.reporting_revision_id
    )
    assert rows["receipts"][0]["reporting_revision_id"] == restatement.reporting_revision_id
    assert rows["adjustments"][0]["adjusts_reporting_revision_id"] == official.reporting_revision_id
    assert (
        rows["adjustment_receipts"][0]["adjusts_reporting_revision_id"]
        == official.reporting_revision_id
    )
    assert {r["reporting_revision_id"] for r in rows["materializations"]} == {
        restatement.reporting_revision_id
    }
    assert (
        len(snapshot.inputs["materializer_boundaries"])
        == len(snapshot.inputs["receipt_boundaries"])
        == 2
    )
    assert len(snapshot.inputs["revision_ownership"]) == 3
    assert {b["reporting_obligation_id"] for b in snapshot.inputs["revision_ownership"]} == {
        case.obligation.reporting_obligation_id
    }
    assert all("ext" not in page for page in pages)
    assert sum(len(rows[array]) for array in ARRAYS) == snapshot.total_count
    assert without_feed(await h.image()) == before
    # The newest official has no artifact: older accepted evidence stays intact
    # and cannot be mistaken for evidence of the current selection in B2.4.
    assert not any(
        r.reporting_revision_id == official.reporting_revision_id for r in await case.outcomes()
    )


async def test_rejected_adjustment_digest_and_replacement_chain_are_preserved_without_retry(feeds):
    h = feeds
    s = await receipt_case(h)
    item = await adjustment_for(h, s)
    rejected = {
        **item,
        "status": "rejected",
        "rejection_codes": ["LOAD_FAILED"],
        "observed_adjustment_sha256": "f" * 64,
    }
    request = request_for(s, adjustment_receipts=[rejected])
    admitted = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert all(r["result"] == "recorded" for r in admitted["results"])
    before = without_feed(await h.image())
    first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    original = await walk(h.store, feed_request(s), s.binding.principal, first=first)
    assert original[1]["adjustment_receipts"][0]["observed_adjustment_sha256"] == "f" * 64
    assert without_feed(await h.image()) == before
    accepted = {
        **item,
        "reporting_receipt_id": "accepted-adjustment-leaf",
        "supersedes_reporting_receipt_id": item["reporting_receipt_id"],
    }
    response = await h.store.ingest_receipt_batch(
        {
            "account": request["account"],
            "idempotency_key": "replacement-only",
            "adjustment_receipts": [accepted],
        },
        caller=s.binding.principal,
    )
    assert response["results"][0]["result"] == "recorded"
    before = without_feed(await h.image())
    feed_operation_3 = await walk(
        await restart(h), feed_request(s), s.binding.principal, first=first
    )
    assert feed_operation_3 == original
    _, delta, _ = await walk(
        h.store, feed_request(s, changes_after=original[2]), s.binding.principal
    )
    assert [r["status"] for r in delta["adjustment_receipts"]] == ["rejected", "accepted"]
    assert len(delta["adjustments"]) == len(delta["revisions"]) == len(delta["periods"]) == 1
    assert without_feed(await h.image()) == before


@pytest.mark.parametrize("feedback", [False, True])
async def test_page_one_freezes_issue_waiver_status_replacement_configuration_and_clock(
    feeds, feedback
):
    h = feeds
    s, _, _ = await mixed_case(h)
    bad = replace(
        statement(s.obligation),
        consumer_status="unreadable",
        reporting_revision_id=s.revision.reporting_revision_id,
        failure_code="access_denied",
    )
    await h.store.record_consumer_status_with_lifecycle(bad)
    issue = await h.store.get_issue(account_id=s.obligation.account_id, issue_key=mismatch_key(bad))
    assert issue is not None
    req = feed_request(s)
    first = await h.store.read_reporting_feed(
        req, caller=s.binding.principal, consumer_status_enabled=feedback
    )
    original = await h.store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    expected = await walk(
        h.store, req, s.binding.principal, first=first, consumer_status_enabled=feedback
    )
    await h.store.set_issue_state(
        account_id=s.obligation.account_id, issue_key=issue.issue_key, state="waived", at=h.clock()
    )
    good = replace(
        bad,
        reporting_status_id="consumer-status-replacement",
        consumer_status="received",
        supersedes_reporting_status_id=bad.reporting_status_id,
        failure_code=None,
        observed_revision_content_sha256=s.revision.revision_content_sha256,
    )
    await h.store.record_consumer_status_with_lifecycle(good)
    configs = await h.store.list_configurations(account_id=s.obligation.account_id)
    await h.store.put_configuration(
        replace(
            configs[0],
            deactivated_at=None,
            status_retention_days=configs[0].status_retention_days + 10,
        )
    )
    await h.store.set_revision_readable(
        account_id=s.obligation.account_id,
        reporting_revision_id=s.revision.reporting_revision_id,
        readable=False,
    )
    await h.store.record_materialization_check(
        ReportingMaterializationCheck(
            s.delivery.scope,
            s.outcome.reporting_materialization_id,
            "later-unavailable-check",
            "unavailable",
            h.clock(),
        )
    )
    h.clock.now += timedelta(days=500)
    store = await restart(h)
    before = without_feed(await h.image())
    feed_operation_4 = await walk(
        store, req, s.binding.principal, first=first, consumer_status_enabled=not feedback
    )
    assert feed_operation_4 == expected
    assert (
        await store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        == original
    )
    assert [r["reporting_status_id"] for r in expected[1]["consumer_statuses"]] == [
        bad.reporting_status_id
    ]
    assert original.inputs["core"]["lifecycles"]
    assert original.inputs["readable_materializations"][s.outcome.reporting_materialization_id]
    assert without_feed(await h.image()) == before


@pytest.mark.parametrize("later", ["success", "failure", "expired", "corrupt"])
async def test_accepted_artifact_and_canonical_evidence_remain_frozen_after_later_outcomes(
    feeds, later, monkeypatch
):
    h = feeds
    s, _, response = await mixed_case(h)
    first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    original = await h.store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    if later == "success":
        await extra_materialization(h, s, "later-success", 2)
    elif later == "failure":
        await h.store.commit_materialization_attempt(
            replace(s.attempt, reporting_materialization_id="later-failure", attempt=2)
        )
        await h.store.commit_materialization(
            replace(
                s.outcome,
                reporting_materialization_id="later-failure",
                status="failed",
                resource=None,
                verification=None,
                failure_code="WRITE_FAILED",
            )
        )
    elif later == "expired":
        # A deterministic future clock boundary; immutable resource expiry is
        # preserved. Actual unmodified DB-time selection has a separate vector.
        h.clock.now += timedelta(days=500)
        if h.pool is not None:
            from adcp.reporting.feed import pg as feed_pg

            async def future(connection):
                return (
                    await (
                        await connection.execute("SELECT clock_timestamp() + interval '500 days'")
                    ).fetchone()
                )[0]

            monkeypatch.setattr(feed_pg, "_now", future)
    else:
        await h.store.record_materialization_check(
            ReportingMaterializationCheck(
                s.delivery.scope,
                s.outcome.reporting_materialization_id,
                "later-health",
                later,
                h.clock(),
            )
        )
    before = without_feed(await h.image())
    _, rows, _ = await walk(await restart(h), feed_request(s), s.binding.principal, first=first)
    assert rows["receipts"][0] == response["results"][0]["receipt"]
    assert len(rows["materializations"]) == 1
    assert (
        rows["materializations"][0]["reporting_materialization_id"]
        == s.outcome.reporting_materialization_id
    )
    assert original.inputs["readable_materializations"][s.outcome.reporting_materialization_id]
    fresh = await h.store.read_reporting_feed(
        feed_request(s, limit=100), caller=s.binding.principal
    )
    assert fresh["receipts"] == rows["receipts"]
    if later in {"expired", "corrupt"}:
        after = await h.store.read_reporting_feed_snapshot(
            fresh["ledger_snapshot_id"], caller=s.binding.principal
        )
        assert not after.inputs["readable_materializations"][s.outcome.reporting_materialization_id]
    assert without_feed(await h.image()) == before


async def test_late_committed_backdated_check_cannot_rewrite_receipt_admission(feeds):
    h = feeds
    s, request, response = await mixed_case(h)
    first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    original = await h.store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    # This is a valid later commit with an earlier observation time. Immutable
    # acceptance must use the evidence committed when the receipt was admitted.
    await h.store.record_materialization_check(
        ReportingMaterializationCheck(
            s.delivery.scope,
            s.outcome.reporting_materialization_id,
            "late-backdated-check",
            "unavailable",
            s.receipt.observed_at,
        )
    )
    before = without_feed(await h.image())
    feed_operation_5 = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    assert feed_operation_5 == response
    fresh = await h.store.read_reporting_feed(
        feed_request(s, limit=100), caller=s.binding.principal
    )
    assert fresh["receipts"] == [response["results"][0]["receipt"]]
    frozen = await h.store.read_reporting_feed_snapshot(
        fresh["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert not frozen.inputs["readable_materializations"][s.outcome.reporting_materialization_id]
    assert original.inputs["readable_materializations"][s.outcome.reporting_materialization_id]
    assert original.inputs["receipt_boundaries"] == frozen.inputs["receipt_boundaries"]
    assert (
        await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        == original
    )
    assert without_feed(await h.image()) == before


async def test_conflicting_owner_in_retained_receipt_boundary_fails_new_capture_only(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    first = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    if h.pool is None:
        boundary = h.store._receipt_boundaries[0]
        bad_revision = replace(
            boundary.core.revisions[0], reporting_obligation_id="different-owner"
        )
        h.store._receipt_boundaries[0] = replace(
            boundary, core=replace(boundary.core, revisions=(bad_revision,))
        )
    else:
        async with h.pool.connection() as c, c.transaction():
            await c.execute("SET LOCAL session_replication_role=replica")
            await c.execute(
                "UPDATE reporting_receipt_ingestion_boundaries SET input=jsonb_set(input,"
                "'{core,revisions,0,reporting_obligation_id}',%s::jsonb),"
                " content_sha256=reporting_receipt_ingestion_sha256(jsonb_set(input,"
                "'{core,revisions,0,reporting_obligation_id}',%s::jsonb)) WHERE sequence=1",
                (json.dumps("different-owner"), json.dumps("different-owner")),
            )
    store = await restart(h)
    before = await h.image()
    feed_operation_6 = await walk(store, feed_request(s), s.binding.principal, first=first)
    assert (feed_operation_6)[1]["receipts"]
    with pytest.raises(ReportingFeedError) as error:
        await store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    assert error.value.code == "REPORTING_FEED_HISTORY_CORRUPT"
    assert await h.image() == before


async def test_memory_feed_owner_tag_cannot_relabel_another_callers_history():
    async with feed_harness("memory") as h:
        s, _, _ = await mixed_case(h)
        seq, owner, record = h.store._delivery_records[0]
        h.store._delivery_records[0] = (seq, replace(owner, consumer_id="wrong-owner"), record)
        before = await h.image()
        with pytest.raises(ReportingFeedError) as error:
            await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
        assert error.value.code == "REPORTING_FEED_HISTORY_CORRUPT"
        assert await h.image() == before
