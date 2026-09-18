"""Combined wire membership, exact dependency history, immutable private capture."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingMaterializationCheck
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.types import GetReportingStatusResponse

from ._feed_support import (
    ARRAYS,
    feed_request,
    feeds,
    mixed_case,
    restart,
    second_consumer,
    walk,
    without_feed,
)
from ._generation_support import configuration, revision_for
from ._receipt_support import extra_materialization, request_for
from .test_reporting_notification_outbox import statement

__all__ = ["feeds"]


@pytest.mark.parametrize("limit", [1, 5, 6, 7])
async def test_allowlist_exact_totals_dependency_order_and_final_only_checkpoint(feeds, limit):
    h = feeds
    s, receipt_request, receipt_response = await mixed_case(h)
    before = without_feed(await h.image())
    pages, rows, checkpoint = await walk(h.store, feed_request(s, limit=limit), s.binding.principal)
    assert len(pages) == (6 if limit == 1 else 2 if limit == 5 else 1)
    assert [len(rows[a]) for a in ARRAYS] == [1, 1, 1, 0, 1, 1, 1]
    assert all(p["changes_checkpoint"] == checkpoint for p in pages)
    assert (
        rows["receipts"][0]["reporting_revision_id"]
        == rows["revisions"][0]["reporting_revision_id"]
    )
    assert (
        rows["adjustment_receipts"][0]["reporting_adjustment_id"]
        == rows["adjustments"][0]["reporting_adjustment_id"]
    )
    assert (
        rows["receipts"][0]["reporting_materialization_id"]
        == rows["materializations"][0]["reporting_materialization_id"]
    )
    assert (
        rows["materializations"][0]["reporting_obligation_id"]
        == rows["periods"][0]["reporting_obligation_id"]
    )
    assert all(len(p["changes_checkpoint"]) <= 2048 for p in pages)
    for page in pages:
        GetReportingStatusResponse.model_validate(page)
        assert "ext" not in page
        text = json.dumps(page)
        for secret in (
            "trusted_binding_ref",
            "trusted-binding-1",
            "obligation_delivery",
            "materialization_attempt",
            "materialization_check",
            "signing_key",
        ):
            assert secret not in text
    assert without_feed(await h.image()) == before
    assert (
        await h.store.ingest_receipt_batch(receipt_request, caller=s.binding.principal)
        == receipt_response
    )


async def test_incremental_receipt_replays_old_exact_revision_materialization_adjustment_and_owner(
    feeds,
):
    h = feeds
    s, _, _ = await mixed_case(h)
    _, _, checkpoint = await walk(h.store, feed_request(s), s.binding.principal)
    await extra_materialization(h, s, "new-materialization", 2)
    new_receipt = replace(
        s.receipt,
        reporting_materialization_id="new-materialization",
        reporting_receipt_id="receipt-second-0002",
        observed_at=s.receipt.observed_at + timedelta(seconds=10),
    )
    # Accepted terminality is per revision: consumer rejection is not a retry.
    # The new outcome alone must still bring the old owner and revision.
    pages, rows, _ = await walk(
        h.store, feed_request(s, changes_after=checkpoint), s.binding.principal
    )
    assert [len(rows[a]) for a in ARRAYS] == [1, 1, 0, 0, 1, 0, 0]
    assert (
        rows["materializations"][0]["reporting_materialization_id"]
        == new_receipt.reporting_materialization_id
    )
    snapshot = await h.store.read_reporting_feed_snapshot(
        pages[0]["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert snapshot.after != (0, 0)
    assert snapshot.inputs["selections"][s.obligation.reporting_obligation_id]["history"] == [
        s.revision.reporting_revision_id
    ]
    assert snapshot.inputs["receipt_boundaries"]
    assert len(snapshot.inputs["reconciliation"]) >= 8


async def test_receipt_only_delta_closes_exact_old_targets(feeds):
    h = feeds
    from ._receipt_support import adjustment_for, receipt_case

    s = await receipt_case(h)
    adjustment = await adjustment_for(h, s)
    _, _, checkpoint = await walk(h.store, feed_request(s), s.binding.principal)
    request = request_for(s, adjustment_receipts=[adjustment])
    await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
    _, rows, _ = await walk(h.store, feed_request(s, changes_after=checkpoint), s.binding.principal)
    assert [len(rows[a]) for a in ARRAYS] == [1, 1, 1, 0, 1, 1, 1]


@pytest.mark.parametrize("feedback", [False, True])
async def test_foreign_consumer_writes_do_not_change_open_snapshot_or_visible_vector(
    feeds, feedback
):
    h = feeds
    s, _, _ = await mixed_case(h)
    request = feed_request(s)
    first = await h.store.read_reporting_feed(
        request, caller=s.binding.principal, consumer_status_enabled=feedback
    )
    original = await h.store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    other = await second_consumer(h, s)
    store = await restart(h)
    pages, rows, checkpoint = await walk(
        store, request, s.binding.principal, first=first, consumer_status_enabled=not feedback
    )
    assert pages[-1]["pagination"]["total_count"] == 6
    assert (
        rows["receipts"][0]["received_at"]
        != (
            await store.read_reporting_feed(
                feed_request(other, limit=100), caller=other.binding.principal
            )
        )["receipts"][0]["received_at"]
        or h.pool is None
    )
    saved = await store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert saved == original and saved.ownership_mode == "absent"
    assert saved.inputs["consumer_status_enabled"] is feedback
    assert (
        await store.read_reporting_feed_snapshot(saved.snapshot_id, caller=other.binding.principal)
        is None
    )
    fresh = await store.read_reporting_feed(
        feed_request(s, changes_after=checkpoint), caller=s.binding.principal
    )
    fresh_snapshot = await store.read_reporting_feed_snapshot(
        fresh["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert fresh_snapshot.through == original.through
    assert fresh["pagination"] == {"total_count": 0, "has_more": False}
    assert other.binding.consumer_id not in json.dumps(saved.to_storage())


@pytest.mark.parametrize("feedback", [False, True])
async def test_foreign_core_status_does_not_advance_visible_maximum_or_change_counts(
    feeds, feedback
):
    h = feeds
    s, _, _ = await mixed_case(h)
    own = replace(
        statement(s.obligation),
        consumer_status="received",
        reporting_revision_id=s.revision.reporting_revision_id,
        observed_revision_content_sha256=s.revision.revision_content_sha256,
    )
    await h.store.record_consumer_status_with_lifecycle(own)
    pages, rows, checkpoint = await walk(
        h.store, feed_request(s), s.binding.principal, consumer_status_enabled=feedback
    )
    original = await h.store.read_reporting_feed_snapshot(
        pages[0]["ledger_snapshot_id"], caller=s.binding.principal
    )
    foreign = replace(
        own, reporting_status_id="foreign-core-status", consumer_id="status-only-consumer"
    )
    await h.store.record_consumer_status_with_lifecycle(foreign)
    store = await restart(h)
    current = await store.read_reporting_feed(
        feed_request(s, changes_after=checkpoint),
        caller=s.binding.principal,
        consumer_status_enabled=feedback,
    )
    snapshot = await store.read_reporting_feed_snapshot(
        current["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert snapshot.through == original.through
    assert current["pagination"] == {"total_count": 0, "has_more": False}
    assert rows["consumer_statuses"][0]["reporting_status_id"] == own.reporting_status_id
    assert "foreign-core-status" not in json.dumps(snapshot.to_storage())
    other = await store.read_reporting_feed(
        feed_request(s, limit=100),
        caller=replace(s.binding.principal, consumer_id=foreign.consumer_id),
        consumer_status_enabled=feedback,
    )
    assert len(other["consumer_statuses"]) == 1
    assert other["consumer_statuses"][0]["reporting_status_id"] == foreign.reporting_status_id
    assert other["materializations"] == other["receipts"] == other["adjustment_receipts"] == []


async def test_readability_clock_configuration_and_private_inputs_survive_restart(
    feeds, monkeypatch
):
    h = feeds
    s, _, _ = await mixed_case(h)
    req = feed_request(s)
    first = await h.store.read_reporting_feed(req, caller=s.binding.principal)
    original = await h.store.read_reporting_feed_snapshot(
        first["ledger_snapshot_id"], caller=s.binding.principal
    )
    expected = await walk(h.store, req, s.binding.principal, first=first)
    configs = await h.store.list_configurations(account_id=s.obligation.account_id)
    await h.store.put_configuration(
        replace(configs[0], deactivated_at=configs[0].deactivated_at + timedelta(hours=1))
    )
    await h.store.set_revision_readable(
        reporting_revision_id=s.revision.reporting_revision_id,
        account_id=s.obligation.account_id,
        readable=False,
    )
    await h.store.record_materialization_check(
        ReportingMaterializationCheck(
            s.delivery.scope,
            s.outcome.reporting_materialization_id,
            "later-check",
            "corrupt",
            h.clock(),
        )
    )
    h.clock.now += timedelta(days=500)
    store = await restart(h)
    if h.pool is None:

        def forbidden(*args, **kwargs):
            raise AssertionError("continuation consulted current projection")

        monkeypatch.setattr(store, "_capture_feed", forbidden)
    else:

        async def forbidden(*args, **kwargs):
            raise AssertionError("continuation consulted current projection")

        monkeypatch.setattr(store, "_capture_feed_on", forbidden)
    assert await walk(store, req, s.binding.principal, first=first) == expected
    assert (
        await store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        == original
    )
    assert original.inputs["readable_materializations"][s.outcome.reporting_materialization_id]


async def test_identical_scope_obligations_keep_exact_ownership_across_pages(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    config = replace(
        configuration(),
        delivery_config_id="other",
        feed_purpose="billing",
        required_finality="official",
    )
    await h.store.put_configuration(config)
    obligation = replace(
        s.obligation, reporting_obligation_id="identical-scope-owner", delivery_config_id="other"
    )
    await h.store.commit_obligation(obligation)
    revision, rows = revision_for(obligation, suffix="other-owner")
    await h.store.commit_revision(revision, rows)
    pages, records, _ = await walk(h.store, feed_request(s), s.binding.principal)
    snapshot = await h.store.read_reporting_feed_snapshot(
        pages[0]["ledger_snapshot_id"], caller=s.binding.principal
    )
    assert len(records["periods"]) == len(records["revisions"]) == 2
    assert snapshot.inputs["revision_ownership"] == sorted(
        [
            {
                "reporting_revision_id": s.revision.reporting_revision_id,
                "reporting_obligation_id": s.obligation.reporting_obligation_id,
            },
            {
                "reporting_revision_id": revision.reporting_revision_id,
                "reporting_obligation_id": obligation.reporting_obligation_id,
            },
        ],
        key=lambda b: b["reporting_revision_id"],
    )
    assert records["receipts"][0]["reporting_obligation_id"] == s.obligation.reporting_obligation_id


async def test_core_record_bytes_and_legacy_handler_are_unchanged(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    old = await ReportingStatusHandler(h.store).handle(
        {"view": "periods"},
        caller=ReportingStatusCaller(s.obligation.account_id, s.binding.consumer_id),
    )
    new = await h.store.read_reporting_feed(feed_request(s, limit=100), caller=s.binding.principal)
    for array in ("periods", "revisions", "adjustments"):
        assert old[array] == new[array]
    assert old["materializations"] == old["receipts"] == []
    assert all(p["reconciliation_mode"] == "delivery_only" for p in new["periods"])


@pytest.mark.parametrize("empty_filter", [False, True])
async def test_empty_final_page_still_supplies_checkpoint_for_complete_consumption(
    feeds, empty_filter
):
    h = feeds
    s, _, _ = await mixed_case(h)
    extra = {"delivery_config_ids": ["unmatched"]} if empty_filter else {}
    _, _, before = await walk(h.store, feed_request(s, **extra), s.binding.principal)
    pages, rows, after = await walk(
        h.store, feed_request(s, changes_after=before, **extra), s.binding.principal
    )
    assert len(pages) == 1 and sum(map(len, rows.values())) == 0
    assert pages[0]["pagination"] == {"total_count": 0, "has_more": False}
    assert after and len(after) <= 2048
