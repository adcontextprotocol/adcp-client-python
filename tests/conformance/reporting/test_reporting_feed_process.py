"""Real process death before/after snapshot commit and mounted cold continuation."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ._feed_support import feed_harness, feed_request, mixed_case, walk, without_feed
from .test_reporting_materializer_process import Child


@asynccontextmanager
async def feed_process(h, s, request, *, python=None, script=None, helper=None, **options):
    process = await asyncio.create_subprocess_exec(
        str(python or sys.executable),
        *(["-I"] if options.get("installed") else []),
        str(script or Path(__file__).with_name("_feed_process.py")),
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
                "notifications": h.store._notifications_enabled,
                "caller": {
                    "account_id": s.obligation.account_id,
                    "consumer_id": s.binding.consumer_id,
                },
                "request": request,
                "action": "page",
                "helper": str(helper or Path(__file__).with_name("_receipt_transport.py")),
                **options,
            }
        )
        yield child
    finally:
        await child.kill()
        diagnostic = await process.stderr.read()
        assert process.returncode in {0, -9}, (process.returncode, len(diagnostic))


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize("point", ["captured", "inserted", "committed"])
async def test_process_crash_preserves_receipts_and_cold_mcp_a2a_pages(point, notifications):
    async with feed_harness("postgres", notifications=notifications) as h:
        s, request, response = await mixed_case(h)
        before = without_feed(await h.image())
        async with feed_process(h, s, feed_request(s), pause=point) as child:
            boundary = await child.event(point)
            # Observe MVCC at each phase: captured inputs and uncommitted INSERT
            # are invisible; no partial snapshot or receipt/history change leaks.
            async with h.pool.connection() as c:
                count = (
                    await (
                        await c.execute("SELECT count(*) FROM reporting_feed_snapshots")
                    ).fetchone()
                )[0]
            assert count == int(point == "committed")
            await child.kill()
        assert without_feed(await h.image()) == before
        feed_operation_1 = await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
        assert feed_operation_1 == response
        if point == "committed":
            first = boundary["result"]
        else:
            async with feed_process(h, s, feed_request(s)) as child:
                first = (await child.event("done"))["result"]["pages"][0]
                feed_operation_2 = await asyncio.wait_for(child.process.wait(), 5)
                assert feed_operation_2 == 0
        expected = await walk(h.store, feed_request(s), s.binding.principal, first=first)
        continuation = feed_request(
            s, pagination={"max_results": 1, "cursor": first["pagination"]["cursor"]}
        )
        for v1 in (False, True):
            async with feed_process(
                h, s, continuation, action="walk", transport="a2a", v1=v1, feedback=True
            ) as child:
                result = (await child.event("done"))["result"]
                feed_operation_3 = await asyncio.wait_for(child.process.wait(), 5)
                assert feed_operation_3 == 0
            assert result["pages"] == expected[0][1:]
            assert result["version"] == 1 and result["ownership_mode"] == "absent"
        assert without_feed(await h.image()) == before
