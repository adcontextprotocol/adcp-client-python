"""Shared receipt-ingress vectors; production PG receipt time is database time."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import ReportingAdjustmentRecord, ReportingControlTotalRecord
from adcp.reporting.ledger.delivery import adjustment_to_wire, receipt_to_wire
from adcp.reporting.receipts import InMemoryReportingReceiptStore

from ._durable_materializer_support import DurableHarness
from ._generation_support import END, isolated_reporting_pool
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
