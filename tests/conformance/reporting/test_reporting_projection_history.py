"""Retained preactivation inputs, incremental cutover and permanent quarantine."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from adcp.reporting.ledger import (
    ReportingAdjustmentReceiptRecord,
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.delivery import adjustment_to_wire
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.projection.history import checkpoint_document, checkpoint_key

from ._durable_materializer_support import durable_case
from ._projection_support import projection_harness, projections

__all__ = ["projections"]


async def history_image(h, account):
    if h.pool is None:
        state = h.store._projection_accounts[account]
        return [(sequence, document) for sequence, document in state.historical_steps]
    async with h.pool.connection() as c:
        return await (
            await c.execute(
                "SELECT account_sequence,input FROM reporting_projection_legacy_steps"
                " WHERE account_id=%s ORDER BY account_sequence,scope_key",
                (account,),
            )
        ).fetchall()


async def prepare_history(h):
    h.clock.now = datetime.now(timezone.utc)
    first = await durable_case(
        h.store, required="official", finality="official", reconciliation_mode="consumer_receipt"
    )
    second = await durable_case(
        h.store,
        required="official",
        finality="official",
        reconciliation_mode="consumer_receipt",
        consumer="https://buyer.example.test/other",
    )
    for _ in range(2):
        lease = await first.claim()
        case = first if lease.scope == first.scope else second
        prepared, evidence = await case.verified(lease)
        await h.store.finish_materialization(lease, prepared=prepared, verified=evidence)
    outcome = (await first.outcomes())[0]
    h.clock.now = datetime.now(timezone.utc)
    receipt = ReportingRevisionReceiptRecord(
        first.scope,
        "historical-receipt",
        first.revision.reporting_revision_id,
        outcome.reporting_materialization_id,
        "accepted",
        first.binding.verification_profile,
        first.revision.row_count,
        first.revision.managed_control_totals,
        h.clock(),
        observed_canonical_content_digest=first.revision.canonical_content_digest,
    )
    await h.store.record_revision_receipt(receipt)
    adjustment = ReportingAdjustmentRecord(
        "historical-adjustment",
        first.config.account_id,
        first.revision.reporting_revision_id,
        "source_correction",
        first.obligation.period.end,
        first.obligation.period.end + timedelta(days=30),
        (("spend", "-0.50"),),
        h.clock(),
        h.clock(),
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-0.50", "decimal", "USD"),
        ),
    )
    await h.store.commit_adjustment(adjustment)
    rejected = ReportingAdjustmentReceiptRecord(
        first.scope,
        "historical-adjustment-rejected",
        adjustment.reporting_adjustment_id,
        first.revision.reporting_revision_id,
        "rejected",
        adjustment_to_wire(adjustment)["canonical_adjustment_sha256"],
        h.clock(),
        rejection_codes=("CONTROL_TOTAL_MISMATCH",),
    )
    await h.store.record_adjustment_receipt(rejected)
    await h.store.record_adjustment_receipt(
        replace(
            rejected,
            reporting_receipt_id="historical-adjustment-accepted",
            status="accepted",
            supersedes_reporting_receipt_id=rejected.reporting_receipt_id,
            rejection_codes=(),
        )
    )
    h.clock.now = datetime.now(timezone.utc)
    return first, second


async def original_inputs(h, cases):
    result = []
    for case in cases:
        records = (
            *await h.store.read_materializer_boundaries(caller=case.scope.principal),
            *await h.store.read_receipt_boundaries(caller=case.scope.principal),
        )
        result.extend(b.to_storage() for b in records)
    return tuple(result)


async def test_captured_history_replays_after_interruption_without_promoting_readiness(
    projections, monkeypatch
):
    await history_replay(projections, monkeypatch, legacy_baseline=False)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_captured_history_preserves_the_existing_c_baseline(backend, monkeypatch):
    async with projection_harness(backend, notifications=True) as h:
        await history_replay(h, monkeypatch, legacy_baseline=True)


async def history_replay(h, monkeypatch, *, legacy_baseline):
    first, second = await prepare_history(h)
    account = first.config.account_id
    baselines = ()
    if legacy_baseline:
        if h.pool is None:
            from adcp.reporting.outbox.status_memory import InMemoryStatusNotificationStore

            old = InMemoryStatusNotificationStore(h.store)
        else:
            from adcp.reporting.outbox.status_pg import PgStatusNotificationStore

            old = PgStatusNotificationStore(h.store)
        production_operation_5 = await old.baseline(account_id=account)
        assert production_operation_5
        baselines = await old.checkpoints(account_id=account)
        assert baselines
    original_queue = await h.queue()
    originals = await original_inputs(h, (first, second))
    assert len(originals) == 5
    production_operation_1 = await h.projection._begin_activation(account_id=account)
    assert production_operation_1
    assert not await h.projection.baseline_ready(account_id=account)
    if h.pool is None:
        archived = h.store._projection_accounts[account].baselines
        assert archived == {checkpoint_key(c): c for c in baselines}
    else:
        async with h.pool.connection() as c:
            archived = dict(
                await (
                    await c.execute(
                        "SELECT scope_key,checkpoint FROM reporting_projection_legacy_baselines"
                        " WHERE account_id=%s",
                        (account,),
                    )
                ).fetchall()
            )
        assert archived == {checkpoint_key(c): checkpoint_document(c) for c in baselines}
    production_operation_2 = await h.projection.project_one(account_id=account)
    assert (production_operation_2).did_work
    before = await history_image(h, account)
    assert before
    # An interrupted invocation has a committed first input; the next complete
    # transaction fails after preparing its replay, and must advance nothing.
    if h.pool is None:
        import adcp.reporting.projection.memory as module

        original = module.project_boundary

        def failure(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected replay transaction failure")

        with monkeypatch.context() as patch:
            patch.setattr(module, "project_boundary", failure)
            with pytest.raises(RuntimeError):
                await h.projection.project_one(account_id=account)
    else:
        async with h.pool.connection() as c:
            await c.execute(
                "CREATE FUNCTION fail_history() RETURNS trigger LANGUAGE plpgsql AS"
                " $$BEGIN RAISE EXCEPTION 'injected replay failure'; END$$"
            )
            await c.execute(
                "CREATE TRIGGER fail_history BEFORE INSERT ON reporting_projection_legacy_steps"
                " FOR EACH ROW EXECUTE FUNCTION fail_history()"
            )
        try:
            with pytest.raises(Exception):
                await h.projection.project_one(account_id=account)
        finally:
            async with h.pool.connection() as c:
                await c.execute("DROP TRIGGER fail_history ON reporting_projection_legacy_steps")
                await c.execute("DROP FUNCTION fail_history()")
    assert await history_image(h, account) == before
    projection = type(h.projection)(
        h.store,
        consumer_status_enabled=False,
        revision_ownership=True,
    )
    production_operation_3 = await projection.activate(account_id=account)
    assert not production_operation_3
    assert await projection.baseline_ready(account_id=account)
    history = await history_image(h, account)
    personal = [
        doc["checkpoint"]
        for _, doc in history
        if doc["checkpoint"]["scope"]["consumer_id"] == first.binding.consumer_id
        and doc["checkpoint"]["scope"]["reporting_obligation_id"]
        == first.obligation.reporting_obligation_id
    ]
    assert [c["snapshot"]["health"] for c in personal] == [
        "action_required",
        "complete",
        "action_required",
        "complete",
    ]
    assert [c["generation"] for c in personal] == [1, 2, 3, 4]
    assert all(doc["admission_epoch"] == 0 for _, doc in history)
    assert {doc["checkpoint"]["scope"]["consumer_id"] for _, doc in history} == {
        first.binding.consumer_id,
        second.binding.consumer_id,
    }
    assert await h.queue() == original_queue
    current = await projection.checkpoints(account_id=account)
    assert (
        next(
            c
            for c in current
            if c.scope.consumer_id == first.binding.consumer_id
            and c.scope.reporting_obligation_id == first.obligation.reporting_obligation_id
        ).generation
        >= 4
    )
    assert await original_inputs(h, (first, second)) == originals
    production_operation_4 = await projection.activate(account_id=account)
    assert not production_operation_4


async def test_missing_retained_capture_refuses_activation(projections):
    h = projections
    first, _ = await prepare_history(h)
    account = first.config.account_id
    if h.pool is None:
        h.store._materializer_account_heads[account] += 1
    else:
        async with h.pool.connection() as c:
            await c.execute(
                "UPDATE reporting_materializer_accounts SET captured_sequence=captured_sequence+1"
                " WHERE account_id=%s",
                (account,),
            )
    with pytest.raises(ReportingNotificationError, match="status_projection_history_corrupt"):
        await h.projection.activate(account_id=account)
    assert not await h.projection.baseline_ready(account_id=account)
