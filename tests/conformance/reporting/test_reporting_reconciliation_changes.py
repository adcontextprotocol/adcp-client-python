"""Independent scoped checkpoints cannot lose records to Core or to interleaving writes."""

from dataclasses import replace
from datetime import timedelta
from typing import get_args

import pytest

from adcp.reporting.ledger import (
    LedgerConflictError,
    PgReportingReconciliationStore,
    ReportingAdjustmentReceiptRecord,
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryPrincipal,
    ReportingMaterializationCheck,
    ReportingReconciliationCheckpoint,
    ReportingReconciliationCursor,
    ReportingReconciliationFeedStore,
    ReportingReconciliationFilter,
    ReportingReconciliationStore,
    ReportingStatusCaller,
    ReportingStatusHandler,
    adjustment_to_wire,
)
from adcp.reporting.ledger.models import LedgerRecordKind
from adcp.reporting.ledger.store import decode_cursor, encode_cursor

from ._generation_support import END
from ._reconciliation_support import Clock, Store, scenario


async def test_frozen_pages_final_checkpoints_and_interleavings(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, clock = reconciliation_store
    s = await scenario(store)
    caller = s.binding.principal
    assert isinstance(store, ReportingReconciliationFeedStore)
    assert not hasattr(ReportingReconciliationStore, "read_reconciliation_changes")
    core = await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(caller.account_id, caller.consumer_id)
    )
    with pytest.raises(LedgerConflictError) as invalid:
        await store.read_reconciliation_changes(
            caller=caller, changes_after=core["changes_checkpoint"]
        )
    assert invalid.value.code == "INVALID_CHECKPOINT"
    before = await store.read_reconciliation_snapshot(caller=caller)
    first = await store.read_reconciliation_changes(caller=caller, limit=1)
    assert first.has_more and first.cursor
    assert first.changes_checkpoint is None
    assert first.changes[0].record == s.binding

    # Same-account and other-account writers interleave with this frozen walk.
    foreign = await scenario(store, consumer_id="another-buyer")
    await store.commit_materialization(foreign.outcome)
    await store.record_revision_receipt(foreign.receipt)
    other = await scenario(store, account_id="acct_b")
    await store.commit_materialization(other.outcome)
    own_before = await store.read_reconciliation_changes(caller=caller)
    foreign_only = await scenario(store, account_id="acct_c")
    await store.commit_materialization(foreign_only.outcome)
    unaffected = await store.read_reconciliation_changes(
        caller=caller, changes_after=own_before.changes_checkpoint
    )
    assert (
        unaffected.changes == () and unaffected.changes_checkpoint == own_before.changes_checkpoint
    )
    await store.commit_materialization(s.outcome)
    accepted, _ = await store.record_revision_receipt(s.receipt)

    # A fresh reader instance needs only the token, not an in-process snapshot map.
    reader = (
        PgReportingReconciliationStore(pool=store._pool, clock=clock)
        if isinstance(store, PgReportingReconciliationStore)
        else store
    )
    page = first
    collected = list(page.changes)
    while page.has_more:
        page = await reader.read_reconciliation_changes(caller=caller, cursor=page.cursor, limit=1)
        assert page.boundary == first.boundary
        assert (page.changes_checkpoint is None) == page.has_more
        collected.extend(page.changes)
    assert tuple(item.record for item in collected) == before.records
    assert len({item.sequence for item in collected}) == len(collected)
    later = await reader.read_reconciliation_changes(
        caller=caller, changes_after=page.changes_checkpoint
    )
    assert tuple(item.record for item in later.changes) == (s.outcome, accepted)
    empty = await reader.read_reconciliation_changes(
        caller=caller, changes_after=later.changes_checkpoint
    )
    assert empty.changes == () and not empty.has_more
    assert empty.changes_checkpoint == later.changes_checkpoint

    # A persisted partial page resumes its frozen walk by cursor, not checkpoint.
    resumed = await reader.read_reconciliation_changes(caller=caller, cursor=first.cursor)
    assert tuple(item.record for item in resumed.changes) == before.records[1:]
    for token_name, token in [
        ("cursor", first.cursor),
        ("changes_after", later.changes_checkpoint),
    ]:
        for stranger in [
            ReportingDeliveryPrincipal(caller.account_id, "another-buyer"),
            ReportingDeliveryPrincipal("acct_b", caller.consumer_id),
        ]:
            with pytest.raises(LedgerConflictError) as invalid:
                await reader.read_reconciliation_changes(caller=stranger, **{token_name: token})
            assert invalid.value.code == "INVALID_CHECKPOINT"


async def test_repairs_and_receipts_remain_incrementally_visible_after_core_advances(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    caller = s.binding.principal
    original = await store.read_reconciliation_changes(caller=caller)
    corrupt = ReportingMaterializationCheck(
        s.attempt.scope,
        s.attempt.reporting_materialization_id,
        "incremental-corruption",
        "corrupt",
        END + timedelta(seconds=8),
    )
    await store.record_materialization_check(corrupt)
    rejected, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            status="rejected",
            rejection_codes=("CONTENT_CORRUPT",),
            observed_at=END + timedelta(seconds=9),
        )
    )
    repair = replace(
        corrupt,
        check_id="incremental-repair",
        state="readable",
        checked_at=END + timedelta(seconds=10),
    )
    await store.record_materialization_check(repair)
    accepted, _ = await store.record_revision_receipt(
        replace(
            s.receipt,
            reporting_receipt_id="incremental-accepted-receipt",
            supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
            observed_at=END + timedelta(seconds=11),
        )
    )
    await ReportingStatusHandler(store).handle(
        {"view": "periods"}, caller=ReportingStatusCaller(caller.account_id, caller.consumer_id)
    )
    changes = []
    checkpoint = original.changes_checkpoint
    cursor = None
    # Partial pages retain only a cursor; the final checkpoint covers all repairs.
    for expected in (corrupt, rejected, repair, accepted):
        page = await store.read_reconciliation_changes(
            caller=caller,
            changes_after=checkpoint if cursor is None else None,
            cursor=cursor,
            limit=1,
        )
        assert len(page.changes) == 1 and page.changes[0].record == expected
        changes.extend(page.changes)
        assert (page.changes_checkpoint is None) == page.has_more
        cursor = page.cursor
        if not page.has_more:
            checkpoint = page.changes_checkpoint
    assert len({item.sequence for item in changes}) == 4
    assert not (
        await store.read_reconciliation_changes(caller=caller, changes_after=checkpoint)
    ).changes
    for record, write in [
        (corrupt, store.record_materialization_check),
        (repair, store.record_materialization_check),
        (accepted, store.record_revision_receipt),
    ]:
        assert not (await write(record))[1]
    assert not (
        await store.read_reconciliation_changes(caller=caller, changes_after=checkpoint)
    ).changes


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_feed",
        "negative",
        "bool",
        "extra",
        "time",
        "backward",
        "missing",
        "version",
        "filter",
        "bound",
        "key",
        "snapshot",
        "count",
        "count_none",
        "count_bool",
        "type",
        "false_version",
    ],
)
async def test_invalid_reconciliation_positions_fail_closed(
    reconciliation_store: tuple[Store, Clock],
    mutation: str,
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    caller = s.binding.principal
    page = await store.read_reconciliation_changes(caller=caller, limit=1)
    position = decode_cursor(page.cursor)
    if mutation == "foreign_feed":
        position["feed"] = "core"
    if mutation == "negative":
        position["seq"] = -1
    if mutation == "bool":
        position["seq"] = False
    if mutation == "extra":
        position["unknown"] = "untrusted"
    if mutation == "time":
        position["as_of"] = "not-a-time"
    if mutation == "backward":
        position["seq"] = position["through"] + 1
    if mutation == "missing":
        position.pop("consumer")
    if mutation == "version":
        position["version"] = 2
    if mutation == "false_version":
        position["version"] = True
    if mutation == "filter":
        position["filter"] = "f" * 64
    if mutation == "bound":
        position["through"] += 1
    if mutation == "key":
        position["key"] = "f" * 64
    if mutation == "snapshot":
        position["snapshot"] = "rprc_wrong"
    if mutation == "count":
        position["count"] += 1
    if mutation == "count_none":
        position["count"] = None
    if mutation == "count_bool":
        position["count"] = True
    if mutation == "type":
        position["type"] = "checkpoint"
    with pytest.raises(LedgerConflictError) as error:
        await store.read_reconciliation_changes(caller=caller, cursor=encode_cursor(position))
    assert error.value.code == "INVALID_CHECKPOINT"
    assert "untrusted" not in str(error.value)


async def test_adjustment_receipts_have_their_own_incremental_change(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    before = await store.read_reconciliation_changes(caller=s.binding.principal)
    adjustment = ReportingAdjustmentRecord(
        "incremental-adjustment",
        "acct_a",
        s.revision.reporting_revision_id,
        "source_correction",
        END,
        END + timedelta(days=30),
        (("spend", "-1"),),
        END + timedelta(seconds=5),
        END + timedelta(seconds=6),
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1", "decimal", "EUR"),
        ),
    )
    await store.commit_adjustment(adjustment)
    unchanged = await store.read_reconciliation_changes(
        caller=s.binding.principal, changes_after=before.changes_checkpoint
    )
    assert unchanged.changes == () and unchanged.changes_checkpoint == before.changes_checkpoint
    receipt = ReportingAdjustmentReceiptRecord(
        s.attempt.scope,
        "incremental-adjustment-receipt",
        adjustment.reporting_adjustment_id,
        s.revision.reporting_revision_id,
        "accepted",
        adjustment_to_wire(adjustment)["canonical_adjustment_sha256"],
        END + timedelta(seconds=7),
    )
    accepted, _ = await store.record_adjustment_receipt(receipt)
    after = await store.read_reconciliation_changes(
        caller=s.binding.principal, changes_after=before.changes_checkpoint
    )
    assert tuple(item.record for item in after.changes) == (accepted,)
    assert not (await store.record_adjustment_receipt(receipt))[1]
    assert not (
        await store.read_reconciliation_changes(
            caller=s.binding.principal, changes_after=after.changes_checkpoint
        )
    ).changes


async def test_foreign_reconciliation_writes_cannot_move_core_or_another_principal(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    own = await scenario(store)
    foreign = await scenario(store, consumer_id="foreign-consumer")
    other_account = await scenario(store, account_id="acct_other")
    caller = own.binding.principal
    empty_caller = ReportingDeliveryPrincipal(caller.account_id, "no-evidence")
    handler = ReportingStatusHandler(store)
    core_caller = ReportingStatusCaller(caller.account_id, caller.consumer_id)
    core_before = await handler.handle({"view": "periods"}, caller=core_caller)
    before = await store.read_reconciliation_snapshot(caller=caller)
    empty = await store.read_reconciliation_snapshot(caller=empty_caller)
    first = await store.read_reconciliation_changes(caller=caller, limit=1)
    complete = await store.read_reconciliation_changes(caller=caller)
    checkpoint = complete.changes_checkpoint
    tail = await store.read_reconciliation_changes(caller=caller, changes_after=checkpoint)
    assert get_args(LedgerRecordKind) == ("obligation", "revision", "adjustment", "consumer_status")
    assert before.boundary.max_sequence == before.boundary.total_count == 3
    for candidate in (foreign, other_account):
        await store.commit_materialization(candidate.outcome)
        await store.record_revision_receipt(candidate.receipt)
        assert await handler.handle({"view": "periods"}, caller=core_caller) == core_before
        assert await store.read_reconciliation_snapshot(caller=caller) == before
        assert await store.read_reconciliation_snapshot(caller=empty_caller) == empty
        assert await store.read_reconciliation_changes(caller=caller, limit=1) == first
        assert await store.read_reconciliation_changes(caller=caller) == complete
        assert (
            await store.read_reconciliation_changes(caller=caller, changes_after=checkpoint) == tail
        )
    await store.commit_materialization(own.outcome)
    accepted, _ = await store.record_revision_receipt(own.receipt)
    later = await store.read_reconciliation_changes(caller=caller, changes_after=checkpoint)
    assert tuple(item.sequence for item in later.changes) == (4, 5)
    assert tuple(item.record for item in later.changes) == (own.outcome, accepted)
    assert await handler.handle({"view": "periods"}, caller=core_caller) == core_before


@pytest.mark.parametrize("by_obligation", [False, True])
async def test_filtered_keyset_walk_freezes_count_bounds_and_final_checkpoint(
    reconciliation_store: tuple[Store, Clock], by_obligation: bool
) -> None:
    store, clock = reconciliation_store
    s = await scenario(store)
    await store.commit_materialization(s.outcome)
    await store.record_revision_receipt(s.receipt)
    caller = s.binding.principal
    filters = ReportingReconciliationFilter(
        record_kinds=("materialization_attempt", "materialization", "revision_receipt"),
        reporting_obligation_id=s.obligation.reporting_obligation_id if by_obligation else None,
    )
    page = await store.read_reconciliation_changes(caller=caller, filters=filters, limit=1)
    assert page.total_count == 3 and page.changes_checkpoint is None
    first = page
    new_attempt = replace(s.attempt, attempt=2, reporting_materialization_id="later-attempt")
    await store.commit_materialization_attempt(new_attempt)
    collected = list(page.changes)
    reader = (
        PgReportingReconciliationStore(pool=store._pool, clock=clock)
        if isinstance(store, PgReportingReconciliationStore)
        else store
    )
    while page.has_more:
        assert page.cursor is not None and page.changes_checkpoint is None
        page = await reader.read_reconciliation_changes(
            caller=caller,
            filters=filters,
            cursor=ReportingReconciliationCursor(str(page.cursor)),
            limit=1,
        )
        assert page.boundary == first.boundary and page.total_count == 3
        collected.extend(page.changes)
    assert tuple(item.sequence for item in collected) == (3, 4, 5)
    assert page.changes_checkpoint is not None
    checkpoint = ReportingReconciliationCheckpoint(str(page.changes_checkpoint))
    later = await reader.read_reconciliation_changes(
        caller=caller, filters=filters, changes_after=checkpoint
    )
    assert later.total_count == 1 and later.changes[0].record == new_attempt
    for token in ({"cursor": first.cursor}, {"changes_after": checkpoint}):
        with pytest.raises(LedgerConflictError) as error:
            await reader.read_reconciliation_changes(caller=caller, **token)
        assert error.value.code == "INVALID_CHECKPOINT"
    empty = await reader.read_reconciliation_changes(
        caller=caller,
        filters=ReportingReconciliationFilter(reporting_obligation_id="not-this-obligation"),
        limit=1,
    )
    assert empty.total_count == 0 and empty.changes == () and empty.cursor is None
    assert empty.changes_checkpoint is not None


@pytest.mark.parametrize("limit", [0, -1, True, 1001])
async def test_invalid_page_limits_do_not_open_a_feed(
    reconciliation_store: tuple[Store, Clock], limit: int
) -> None:
    store, _ = reconciliation_store
    caller = ReportingDeliveryPrincipal("acct_a", "buyer")
    with pytest.raises(LedgerConflictError) as error:
        await store.read_reconciliation_changes(caller=caller, limit=limit)
    assert error.value.code == "INVALID_CHECKPOINT"
    assert not (await store.read_reconciliation_changes(caller=caller)).changes


async def test_position_types_and_unissued_boundaries_cannot_be_interchanged(
    reconciliation_store: tuple[Store, Clock],
) -> None:
    store, _ = reconciliation_store
    s = await scenario(store)
    caller = s.binding.principal
    first = await store.read_reconciliation_changes(caller=caller, limit=1)
    last = await store.read_reconciliation_changes(caller=caller)
    for tokens in (
        {"cursor": last.changes_checkpoint},
        {"changes_after": first.cursor},
        {"cursor": first.cursor, "changes_after": last.changes_checkpoint},
        {"cursor": "!untrusted"},
        {"cursor": "x" * 5000},
    ):
        with pytest.raises(LedgerConflictError) as error:
            await store.read_reconciliation_changes(caller=caller, **tokens)
        assert error.value.code == "INVALID_CHECKPOINT"
        assert error.value.__cause__ is None and error.value.__context__ is None
    core = await store.open_snapshot(account_id=caller.account_id, filters_fingerprint="buyer")
    with pytest.raises(LedgerConflictError) as error:
        await store.read_reconciliation_snapshot(caller=caller, boundary=core)
    assert error.value.code == "REPORTING_RECORD_UNAVAILABLE"
    from adcp.reporting.ledger.delivery_changes import change_boundary

    future = change_boundary(caller, last.boundary.max_sequence + 1, last.boundary.ledger_as_of)
    with pytest.raises(LedgerConflictError) as error:
        await store.read_reconciliation_snapshot(caller=caller, boundary=future)
    assert error.value.code == "INVALID_CHECKPOINT"
