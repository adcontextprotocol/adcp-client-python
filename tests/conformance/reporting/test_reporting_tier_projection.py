"""One captured tier projection over the real memory/PostgreSQL financial graph."""

from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingMaterializationCheck
from adcp.reporting.ledger.notification_models import ReportingStatusScope
from adcp.reporting.ledger.reconciliation_projection import project_reconciliation
from adcp.reporting.ledger.status_projection import StatusProjectionInput, project_status_scope
from adcp.reporting.outbox.status import advance_checkpoint

from ._generation_support import END
from ._receipt_support import adjustment_for, receipt_case, receipts, request_for

__all__ = ["receipts"]


async def captured(h, s, *, feedback=False):
    core = await h.store.read_status_snapshot(account_id=s.obligation.account_id)
    private = await h.store.read_reconciliation_snapshot(caller=s.binding.principal)
    core = replace(core, as_of=h.clock.now)
    scope = ReportingStatusScope(
        core.account_id,
        s.binding.generation_key,
        consumer_id=s.binding.consumer_id,
        reporting_obligation_id=s.obligation.reporting_obligation_id,
        feed_purpose=s.obligation.feed_purpose,
    )
    return StatusProjectionInput(
        core, scope, reconciliation=private.records, consumer_status_enabled=feedback
    )


def projected(value):
    result = project_status_scope(value)
    assert len(result.obligations) == 1
    tier = result.obligations[0].reconciliation
    assert tier is not None
    return result, tier


async def test_receipts_adjustments_and_reversible_health_keep_each_ordered_generation(receipts):
    h = receipts
    s = await receipt_case(h)
    first = await captured(h, s)
    result, tier = projected(first)
    assert tier.wire["reconciliation_status"] == "pending"
    assert tier.wire["successful_materialization_count"] == 1
    previous, _ = advance_checkpoint(None, result, fired_at=h.clock.now, source_sequence=1)
    await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
    accepted = await captured(h, s)
    result, tier = projected(accepted)
    assert tier.satisfied and tier.wire["reconciliation_status"] == "accepted"
    previous, event = advance_checkpoint(previous, result, fired_at=h.clock.now, source_sequence=2)
    assert event is not None and previous.generation == 2
    item = await adjustment_for(h, s)
    pending_adjustment = await captured(h, s)
    result, tier = projected(pending_adjustment)
    assert not tier.satisfied and tier.wire["pending_adjustment_count"] == 1
    previous, event = advance_checkpoint(previous, result, fired_at=h.clock.now, source_sequence=3)
    assert event is not None and previous.generation == 3
    await h.store.ingest_receipt_batch(
        {
            "account": {"account_id": s.obligation.account_id},
            "idempotency_key": "adjustment-batch-0001",
            "adjustment_receipts": [item],
        },
        caller=s.binding.principal,
    )
    completed = await captured(h, s)
    result, tier = projected(completed)
    assert tier.satisfied and tier.wire["pending_adjustment_count"] == 0
    previous, event = advance_checkpoint(previous, result, fired_at=h.clock.now, source_sequence=4)
    assert event is not None and previous.generation == 4
    # Replay every old captured input after later mutable state has changed.
    assert projected(first)[1].wire["reconciliation_status"] == "pending"
    assert projected(accepted)[1].wire["pending_adjustment_count"] == 0
    assert projected(pending_adjustment)[1].wire["pending_adjustment_count"] == 1


@pytest.mark.parametrize("later", ["corrupt", "expired", "unreadable", "success", "failure"])
async def test_accepted_artifact_evidence_survives_later_health_and_attempts(receipts, later):
    h = receipts
    s = await receipt_case(h)
    await h.store.ingest_receipt_batch(request_for(s), caller=s.binding.principal)
    if later == "corrupt":
        await h.store.record_materialization_check(
            ReportingMaterializationCheck(
                s.attempt.scope,
                s.attempt.reporting_materialization_id,
                "later-corruption",
                "corrupt",
                END + timedelta(seconds=20),
            )
        )
    elif later == "expired":
        h.clock.now = s.outcome.resource.expires_at
    elif later == "unreadable":
        await h.store.set_revision_readable(
            account_id=s.obligation.account_id,
            reporting_revision_id=s.revision.reporting_revision_id,
            readable=False,
        )
    else:
        attempt = replace(s.attempt, reporting_materialization_id="materialization-2", attempt=2)
        await h.store.commit_materialization_attempt(attempt)
        outcome = replace(
            s.outcome,
            reporting_materialization_id=attempt.reporting_materialization_id,
            status=s.outcome.status if later == "success" else "failed",
            resource=s.outcome.resource if later == "success" else None,
            verification=s.outcome.verification if later == "success" else None,
            failure_code=None if later == "success" else "WRITE_FAILED",
        )
        await h.store.commit_materialization(outcome)
    _, tier = projected(await captured(h, s))
    assert tier.wire["reconciliation_status"] == "accepted"
    assert tier.wire["accepted_receipt_count"] == 1
    assert tier.wire["successful_materialization_count"] == (2 if later == "success" else 1)
    assert tier.satisfied is (later in {"success", "failure"})
    assert tier.wire[
        "resource_retained_until"
    ] == s.delivery.resource_retained_until.isoformat().replace("+00:00", "Z")


async def test_official_selection_precedes_available_snapshot_artifacts(receipts):
    h = receipts
    s = await receipt_case(
        h, finality="snapshot", billing=False, reconciliation_mode="delivery_only"
    )
    private = await h.store.read_reconciliation_snapshot(caller=s.binding.principal)
    official = replace(
        s.revision,
        reporting_revision_id="unmaterialized-official",
        finality="official",
        finality_basis="source_final",
        finality_policy_id="policy-1",
        finalized_at=END,
    )
    result = project_reconciliation(
        replace(s.obligation, required_finality="official"),
        (s.revision, official),
        (),
        private.records,
        consumer_id=s.binding.consumer_id,
        as_of=h.clock.now,
    )
    assert not result.satisfied
    assert result.wire["successful_materialization_count"] == 1
    assert result.wire["reconciliation_status"] == "not_required"
