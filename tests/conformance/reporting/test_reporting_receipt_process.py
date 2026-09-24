"""Actual process death and independent size-one pools resume durable ordinals."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ._receipt_support import (
    adjustment_for,
    batch_state,
    receipt_case,
    receipt_harness,
    request_for,
)
from .test_reporting_materializer_process import Child


@asynccontextmanager
async def worker(h, s, request, *, pause=None, installed=None, python=None, script=None):
    process = await asyncio.create_subprocess_exec(
        str(python or sys.executable),
        *(["-I"] if installed else []),
        str(script or Path(__file__).with_name("_receipt_process.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child = Child(process)
    try:
        await child.send(
            {
                "conninfo": h.pool.conninfo,
                "kwargs": h.pool.kwargs,
                "caller": {
                    "account_id": s.obligation.account_id,
                    "consumer_id": s.binding.consumer_id,
                },
                "request": request,
                "pause": pause,
                "notifications": h.store._notifications_enabled,
                "installed": installed,
            }
        )
        yield child
    finally:
        await child.kill()
        diagnostic = await process.stderr.read()
        if process.returncode not in {0, -9}:
            pytest.fail(diagnostic.decode())


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize(
    "point",
    ["header", "receipt", "feed", "capture", "ordinal", "between_ordinals", "final", "committed"],
)
async def test_process_crash_resumes_original_ordinals_and_final_response(point, notifications):
    async with receipt_harness("postgres", notifications=notifications) as h:
        s = await receipt_case(h)
        adjustment = await adjustment_for(h, s)
        request = request_for(s, adjustment_receipts=[adjustment])
        async with worker(h, s, request, pause=point) as child:
            await child.event(point)
            expected = (
                2 if point in {"final", "committed"} else (1 if point == "between_ordinals" else 0)
            )
            if expected:
                assert await batch_state(h) == ((expected, point == "committed"),)
            else:
                assert await batch_state(h) == ()
            # These are MVCC reads from another connection while the process
            # owns the account lock. No partial receipt/feed/capture is visible.
            async with h.pool.connection() as c:
                row = await (
                    await c.execute(
                        "SELECT (SELECT count(*) FROM reporting_reconciliation_records "
                        "WHERE namespace='receipt'),"
                        " (SELECT count(*) FROM reporting_reconciliation_changes "
                        "WHERE namespace='receipt'),"
                        " (SELECT count(*) FROM reporting_receipt_ingestion_boundaries)"
                    )
                ).fetchone()
            assert row == (expected, expected, expected)
            await child.kill()
        original = await h.store.get_receipt(s.receipt.key)
        async with worker(h, s, request) as resumed:
            response = (await resumed.event("done"))["result"]
            receipt_operation_2 = await asyncio.wait_for(resumed.process.wait(), 5)
            assert receipt_operation_2 == 0
        assert [r["result"] for r in response["results"]] == ["recorded", "recorded"]
        if original is not None:
            from adcp.reporting.ledger.delivery import receipt_to_wire

            assert response["results"][0]["receipt"] == receipt_to_wire(original)
        receipt_operation_1 = await h.store.ingest_receipt_batch(
            request, caller=s.binding.principal
        )
        assert receipt_operation_1 == response
        assert await batch_state(h) == ((2, True),)
        assert len(await h.store.read_receipt_boundaries(caller=s.binding.principal)) == 2
        assert await h.queue() == ((), ())


async def test_two_processes_serialize_with_size_one_pools_and_keep_original_recorded_outcomes():
    async with receipt_harness("postgres") as h:
        s = await receipt_case(h)
        request = request_for(s)
        async with worker(h, s, request, pause="receipt") as first:
            await first.event("receipt")
            async with worker(h, s, request) as second:
                pending = asyncio.create_task(second.event("done"))
                await asyncio.sleep(0.1)
                assert not pending.done()
                await first.send({"continue": True})
                a = (await first.event("done"))["result"]
                b = (await asyncio.wait_for(pending, 30))["result"]
                assert a == b and a["results"][0]["result"] == "recorded"
                receipt_operation_4 = await asyncio.wait_for(second.process.wait(), 5)
                assert receipt_operation_4 == 0
            receipt_operation_3 = await asyncio.wait_for(first.process.wait(), 5)
            assert receipt_operation_3 == 0
