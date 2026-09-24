"""SIGKILL across external-effect and outcome commit boundaries; durable restart."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from adcp.reporting.ledger._delivery_state import payload

from ._durable_materializer_support import durable_case, durable_harness


class Child:
    def __init__(self, process):
        self.process = process

    async def event(self, point):
        line = await asyncio.wait_for(self.process.stdout.readline(), 30)
        assert line, "materializer worker exited before its boundary"
        result = json.loads(line)
        assert result["point"] == point, result
        return result

    async def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        await self.process.stdin.drain()

    async def kill(self):
        if self.process.returncode is None:
            self.process.kill()
            await asyncio.wait_for(self.process.wait(), 5)


async def visible(h):
    """MVCC observation while the child deliberately holds the account lock."""
    async with h.pool.connection() as connection:
        return await (
            await connection.execute(
                "SELECT (SELECT count(*) FROM reporting_reconciliation_records"
                " WHERE record_kind='materialization'),"
                " (SELECT count(*) FROM reporting_materializer_status_boundaries)"
            )
        ).fetchone()


@asynccontextmanager
async def worker(
    h, case, directory, *, pause=None, notifications=True, installed=None, python=None, script=None
):
    process = await asyncio.create_subprocess_exec(
        str(python or sys.executable),
        *(["-I"] if installed else []),
        str(script or Path(__file__).with_name("_materializer_process.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    child = Child(process)
    try:
        await child.send(
            {
                "conninfo": h.pool.conninfo,
                "kwargs": h.pool.kwargs,
                "binding": payload(case.binding),
                "destination": str(directory),
                "pause": pause,
                "notifications": notifications,
                "installed": installed,
                "legacy_definition": not case.config.definition.monetary_metric_units,
            }
        )
        yield child
    finally:
        await child.kill()


@pytest.mark.parametrize("notifications", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "reserved",
        "before_write",
        "after_write",
        "after_readback",
        "after_outcome",
        "after_capture",
        "after_commit",
    ],
)
async def test_real_process_crash_resume_preserves_external_identity_and_atomic_finish(
    boundary, notifications, tmp_path
):
    async with durable_harness("postgres", notifications=notifications) as h:
        case = await durable_case(h.store, count=501)
        async with worker(h, case, tmp_path, pause=boundary, notifications=notifications) as child:
            await child.event(boundary)
            work_before = await h.works()
            assert len(work_before) == 1
            committed = boundary == "after_commit"
            assert await visible(h) == (int(committed), int(committed))
            assert len((await h.queue())[0]) == int(committed and notifications)
            await child.kill()
        await h.expire()
        async with worker(h, case, tmp_path, notifications=notifications) as child:
            result = await child.event("done")
            assert result["state"] in (
                {"idle", "parked", "discovered"} if committed else {"verified"}
            )
            materializer_operation_1 = await asyncio.wait_for(child.process.wait(), 5)
            assert materializer_operation_1 == 0
        after = await h.works()
        assert len(after) == 1 and after[0][0] == work_before[0][0] and after[0][1] == "acked"
        assert len(tuple(tmp_path.glob("rwm_*"))) == 1
        assert len(await case.outcomes()) == 1
        assert len(await h.store.read_materializer_boundaries(caller=case.scope.principal)) == 1
        events, expansions = await h.queue()
        assert len(events) == int(notifications)
        assert expansions == (("quarantined",) if notifications else ())


async def test_crash_after_actual_enabled_logical_enqueue_rolls_back_before_restart(tmp_path):
    async with durable_harness("postgres", notifications=True) as h:
        case = await durable_case(h.store)
        async with worker(h, case, tmp_path, pause="after_event") as child:
            await child.event("after_event")
            assert await visible(h) == (0, 0)
            assert await h.queue() == ((), ())
            await child.kill()
        assert not await h.store.read_materializer_boundaries(caller=case.scope.principal)
        await h.expire()
        async with worker(h, case, tmp_path) as child:
            materializer_operation_2 = await child.event("done")
            assert (materializer_operation_2)["state"] == "verified"
            materializer_operation_3 = await asyncio.wait_for(child.process.wait(), 5)
            assert materializer_operation_3 == 0
        assert len(tuple(tmp_path.glob("rwm_*"))) == 1
        assert len((await h.queue())[0]) == 1


async def test_two_real_workers_reserve_once_without_global_candidate_locks(tmp_path):
    async with durable_harness("postgres", notifications=True) as h:
        case = await durable_case(h.store)
        async with worker(h, case, tmp_path, pause="reserved") as first:
            await first.event("reserved")
            async with worker(h, case, tmp_path) as second:
                materializer_operation_6 = await second.event("done")
                assert (materializer_operation_6)["state"] in {"idle", "discovered"}
                materializer_operation_7 = await asyncio.wait_for(second.process.wait(), 5)
                assert materializer_operation_7 == 0
            assert not tuple(tmp_path.glob("rwm_*"))
            await first.send({"continue": True})
            materializer_operation_4 = await first.event("done")
            assert (materializer_operation_4)["state"] == "verified"
            materializer_operation_5 = await asyncio.wait_for(first.process.wait(), 5)
            assert materializer_operation_5 == 0
        assert len(await h.works()) == 1
