"""Shared receipt-ingress vectors; production PG receipt time is database time."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import (
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingDeliveryScope,
)
from adcp.reporting.ledger.delivery import adjustment_to_wire, receipt_to_wire
from adcp.reporting.ledger.models import derive_period
from adcp.reporting.ledger.producer import revision_content_sha256
from adcp.reporting.receipts import InMemoryReportingReceiptStore

from ._durable_materializer_support import DurableHarness
from ._generation_support import END, configuration, isolated_reporting_pool
from ._reconciliation_support import Clock, scenario


@asynccontextmanager
async def receipt_harness(backend, *, notifications=False, pool=None):
    clock = Clock()
    if backend == "memory":
        yield DurableHarness(
            InMemoryReportingReceiptStore(clock=clock, notifications=notifications), clock
        )
        return
    from adcp.reporting.receipts import PgReportingReceiptStore

    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingReceiptStore(pool=pool, clock=clock, notifications=notifications)
        await store.create_schema()
        yield DurableHarness(store, clock, pool)


@pytest.fixture(
    params=[("memory", False), ("memory", True), ("postgres", False), ("postgres", True)]
)
async def receipts(request):
    backend, notifications = request.param
    async with receipt_harness(backend, notifications=notifications) as h:
        yield h


async def receipt_case(h, **kwargs):
    s = await scenario(h.store, **kwargs)
    await h.store.commit_materialization(s.outcome)
    return s


def request_for(s, *, key="receipt-batch-0001", **changes):
    return {
        "adcp_version": "3.2-rc.3",
        "account": {"account_id": s.obligation.account_id},
        "idempotency_key": key,
        "receipts": [receipt_to_wire(s.receipt)],
        **changes,
    }


async def adjustment_for(h, s, **changes):
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
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1.50", "decimal", "EUR"),
        ),
    )
    adjustment = replace(adjustment, **changes)
    await h.store.commit_adjustment(adjustment)
    return {
        "reporting_receipt_id": "adjustment-receipt-0001",
        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
        "adjusts_reporting_revision_id": adjustment.adjusts_reporting_revision_id,
        "status": "accepted",
        "observed_adjustment_sha256": adjustment_to_wire(adjustment)["canonical_adjustment_sha256"],
        "observed_at": (END + timedelta(seconds=7)).isoformat().replace("+00:00", "Z"),
    }


async def batch_state(h):
    if h.pool is None:
        return tuple(
            (len(s.results), s.response is not None)
            for s in getattr(h.store, "_receipt_batches", {}).values()
        )
    async with h.pool.connection() as c:
        return tuple(
            await (
                await c.execute(
                    "SELECT (SELECT count(*) FROM reporting_receipt_ingestion_results r"
                    " WHERE (r.account_id,r.consumer_id,r.idempotency_key)="
                    "(b.account_id,b.consumer_id,b.idempotency_key)),"
                    " final_response IS NOT NULL FROM reporting_receipt_ingestion_batches b"
                    " ORDER BY account_id,consumer_id,idempotency_key"
                )
            ).fetchall()
        )


@dataclass(frozen=True)
class ForeignTargets:
    """Existing, wholly VALID records that belong to a different exact target.

    Nothing here is corrupt or unauthorized at the application boundary: every
    row is a legitimate artifact for its own obligation, revision, consumer or
    adjustment. A receipt that points at one of them from another target must
    still be refused, and refused indistinguishably from an absent record.
    """

    obligation: object
    revision: object
    materialization_id: str
    adjustment: object
    consumer_id: str
    consumer_materialization_id: str


async def foreign_targets(h, s):
    """Commit one more complete valid period and one more valid consumer."""
    config = replace(
        configuration(s.obligation.account_id),
        feed_purpose=s.binding.feed_purpose,
        required_finality=s.obligation.required_finality,
    )
    period = derive_period(config.schedule, account_timezone=config.account_timezone, ordinal=1)
    obligation = replace(
        s.obligation,
        reporting_obligation_id="rpo_foreign",
        period=period,
        scope_resolved_at=period.end,
        automated_recovery_deadline_at=period.expected_at + config.automated_recovery_window,
    )
    await h.store.commit_obligation(obligation)
    scope = ReportingDeliveryScope(
        obligation.generation_key, s.binding.consumer_id, obligation.reporting_obligation_id
    )
    await h.store.bind_obligation_delivery(
        replace(s.delivery, scope=scope, resource_retained_until=period.end + timedelta(days=400))
    )
    rows = [
        {
            "media_buy_id": obligation.media_buy_ids[0],
            "impressions": 5,
            "spend": "12.50",
            "currency": "EUR",
        }
    ]
    revision = replace(
        s.revision,
        reporting_revision_id="revision-foreign",
        reporting_obligation_id=obligation.reporting_obligation_id,
        observed_at=period.end,
        data_through=period.end,
        created_at=period.end + timedelta(seconds=1),
        finalized_at=period.end if s.revision.finality == "official" else None,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id="revision-foreign",
            row_count=s.revision.row_count,
            control_totals=s.revision.control_totals,
            reporting_rows=rows,
            control_total_evidence=s.revision.managed_control_totals,
        ),
    )
    await h.store.commit_revision(revision, rows)
    completed = period.end + timedelta(seconds=3)
    await h.store.commit_materialization_attempt(
        replace(
            s.attempt,
            scope=scope,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id="materialization-foreign",
            created_at=period.end + timedelta(seconds=2),
        )
    )
    await h.store.commit_materialization(
        replace(
            s.outcome,
            scope=scope,
            reporting_revision_id=revision.reporting_revision_id,
            reporting_materialization_id="materialization-foreign",
            completed_at=completed,
            resource=replace(s.outcome.resource, expires_at=completed + timedelta(days=400)),
            verification=replace(s.outcome.verification, verified_at=completed),
        )
    )
    adjustment = ReportingAdjustmentRecord(
        "adjustment-foreign",
        obligation.account_id,
        revision.reporting_revision_id,
        "source_correction",
        period.end,
        period.end + timedelta(days=30),
        (("spend", "-1.50"),),
        period.end + timedelta(seconds=5),
        period.end + timedelta(seconds=6),
        managed_control_total_deltas=(
            ReportingControlTotalRecord("spend", "-1.50", "decimal", "EUR"),
        ),
    )
    await h.store.commit_adjustment(adjustment)
    # One more valid consumer holding its own artifact for the caller's revision.
    consumer_id = f"{s.binding.consumer_id}-foreign"
    other = ReportingDeliveryScope(
        s.obligation.generation_key, consumer_id, s.obligation.reporting_obligation_id
    )
    await h.store.put_destination_binding(replace(s.binding, consumer_id=consumer_id))
    await h.store.bind_obligation_delivery(replace(s.delivery, scope=other))
    await h.store.commit_materialization_attempt(
        replace(s.attempt, scope=other, reporting_materialization_id="materialization-consumer")
    )
    await h.store.commit_materialization(
        replace(s.outcome, scope=other, reporting_materialization_id="materialization-consumer")
    )
    return ForeignTargets(
        obligation,
        revision,
        "materialization-foreign",
        adjustment,
        consumer_id,
        "materialization-consumer",
    )


async def extra_materialization(h, s, materialization_id, attempt_number):
    """Commit one more valid attempt/materialization for s's exact scope."""
    created = s.attempt.created_at + timedelta(seconds=attempt_number)
    completed = s.outcome.completed_at + timedelta(seconds=attempt_number)
    await h.store.commit_materialization_attempt(
        replace(
            s.attempt,
            reporting_materialization_id=materialization_id,
            attempt=attempt_number,
            created_at=created,
        )
    )
    await h.store.commit_materialization(
        replace(
            s.outcome,
            reporting_materialization_id=materialization_id,
            completed_at=completed,
            resource=replace(s.outcome.resource, expires_at=completed + timedelta(days=400)),
            verification=replace(s.outcome.verification, verified_at=completed),
        )
    )
    return materialization_id


async def foreign_account(h, *, account_id="acct_b"):
    """Commit one more COMPLETE, VALID account and return its exact identifiers.

    Obligations, revisions and adjustments are globally keyed, so the same
    identifier cannot exist under two accounts. The account relationship can
    therefore only be probed by naming another account's real identifiers from
    this account's receipt, which is what the returned values are for.
    """
    other = await receipt_case(h, account_id=account_id)
    adjustment = await adjustment_for(h, other, reporting_adjustment_id=f"adjustment-{account_id}")
    materialization = await extra_materialization(h, other, f"materialization-{account_id}", 2)
    return other, adjustment, materialization
