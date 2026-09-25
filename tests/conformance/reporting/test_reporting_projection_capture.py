"""Ordered capture, checkpoint, activation, rollback and frozen feed integration."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingMaterializationCheck
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ownership import page_revision_ownership

from ._feed_support import feed_request, walk
from ._generation_support import END
from ._projection_support import drain, inputs, projections
from ._receipt_support import adjustment_for, receipt_case, request_for

__all__ = ["projections"]


async def test_captured_receipt_adjustment_and_readability_cycles_are_not_collapsed(projections):
    h = projections
    s = await receipt_case(h)
    account = s.obligation.account_id
    production_operation_1 = await h.projection.activate(account_id=account)
    assert production_operation_1
    assert await h.projection.baseline_ready(account_id=account)
    starting = next(
        c.generation
        for c in await h.projection.checkpoints(account_id=account)
        if c.scope.reporting_obligation_id == s.obligation.reporting_obligation_id
        and c.scope.consumer_id == s.binding.consumer_id
    )
    baseline = await inputs(h, account)
    assert len(baseline) == 1
    await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
    rejected = ReportingMaterializationCheck(
        s.attempt.scope,
        s.attempt.reporting_materialization_id,
        "projection-corruption",
        "corrupt",
        END + timedelta(seconds=30),
    )
    await h.store.record_materialization_check(rejected)
    await h.store.record_materialization_check(
        replace(
            rejected,
            check_id="projection-recovery",
            state="readable",
            checked_at=END + timedelta(seconds=31),
        )
    )
    item = await adjustment_for(h, s)
    await h.store.ingest_receipt_batch(
        {
            "account": {"account_id": account},
            "idempotency_key": "projection-adjustment-0001",
            "adjustment_receipts": [item],
        },
        caller=s.binding.principal,
    )
    frozen = await inputs(h, account)
    assert len(frozen) == 6
    # All five transitions are pending while today's rows already look recovered.
    assert len(frozen[0].reconciliation) + 4 == len(frozen[-1].reconciliation)
    transitions = await drain(h.projection, account)
    assert len(transitions) == 5
    checkpoints = await h.projection.checkpoints(account_id=account)
    obligation = next(
        c
        for c in checkpoints
        if c.scope.reporting_obligation_id == s.obligation.reporting_obligation_id
        and c.scope.consumer_id == s.binding.consumer_id
    )
    assert obligation.generation == starting + 5
    assert obligation.snapshot["health"] == "complete"
    assert (
        all(t.events > 0 for t in transitions)
        if h.projection.policy["notifications_enabled"]
        else all(t.events == 0 for t in transitions)
    )
    assert await inputs(h, account) == frozen
    before = await h.image()
    await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
    assert await h.image() == before
    production_operation_2 = await h.projection.activate(account_id=account)
    assert not production_operation_2


async def test_activation_preserves_legacy_snapshot_and_new_pages_have_exact_local_ownership(
    projections,
):
    h = projections
    s = await receipt_case(h)
    req = feed_request(s)
    first = await h.store.read_reporting_feed(req, caller=s.binding.principal)
    legacy = await walk(h.store, req, s.binding.principal, first=first)
    production_operation_3 = await h.projection.activate(account_id=s.obligation.account_id)
    assert production_operation_3
    resumed = await walk(h.store, req, s.binding.principal, first=first)
    assert resumed == legacy
    new = await walk(h.store, req, s.binding.principal)
    for page in new[0]:
        bindings = page_revision_ownership(page)
        assert bindings is not None
        assert set(bindings) == {r["reporting_revision_id"] for r in page.get("revisions", [])}
        assert all(owner == s.obligation.reporting_obligation_id for owner in bindings.values())
    assert new[0][0]["changes_checkpoint"] != first["changes_checkpoint"]


async def test_capture_failure_rolls_back_every_source_collection_or_row(projections, monkeypatch):
    h = projections
    s = await receipt_case(h)
    await h.projection.activate(account_id=s.obligation.account_id)
    before = await h.image()
    if h.pool is None:

        def fail(self, account_id):
            raise ReportingNotificationError("status_projection_history_corrupt")

        monkeypatch.setattr(type(h.store), "_capture_projection", fail)
        with pytest.raises(ReportingNotificationError):
            await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
        monkeypatch.undo()
    else:
        from psycopg import sql

        async with h.pool.connection() as c:
            await c.execute(
                "CREATE FUNCTION test_projection_failure() RETURNS trigger LANGUAGE plpgsql"
                " AS $$BEGIN RAISE EXCEPTION 'injected'; END$$"
            )
            await c.execute(
                "CREATE TRIGGER test_projection_failure"
                " BEFORE INSERT ON reporting_projection_inputs"
                " FOR EACH ROW EXECUTE FUNCTION test_projection_failure()"
            )
        try:
            with pytest.raises(Exception):
                await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
        finally:
            async with h.pool.connection() as c:
                await c.execute(
                    sql.SQL("DROP TRIGGER test_projection_failure ON reporting_projection_inputs")
                )
                await c.execute("DROP FUNCTION test_projection_failure()")
    assert await h.image() == before
