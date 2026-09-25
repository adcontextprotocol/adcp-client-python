"""B2.4 shared execution: actual stores, no forged readiness certificate."""

from contextlib import asynccontextmanager

import pytest

from adcp.reporting.projection.memory import (
    InMemoryReportingProjectionStore,
    InMemoryReportingStatusProjection,
)

from ._durable_materializer_support import DurableHarness
from ._generation_support import isolated_reporting_pool
from ._reconciliation_support import Clock


@asynccontextmanager
async def projection_harness(backend, *, notifications=False, feedback=False, ownership=True):
    clock = Clock()
    if backend == "memory":
        store = InMemoryReportingProjectionStore(clock=clock, notifications=notifications)
        h = DurableHarness(store, clock)
        h.projection = InMemoryReportingStatusProjection(
            store, consumer_status_enabled=feedback, revision_ownership=ownership
        )
        yield h
    else:
        from adcp.reporting.projection.pg import (
            PgReportingProjectionStore,
            PgReportingStatusProjection,
        )

        async with isolated_reporting_pool(autocommit=True) as pool:
            store = PgReportingProjectionStore(pool=pool, notifications=notifications)
            await store.create_schema()
            h = DurableHarness(store, clock, pool)
            h.projection = PgReportingStatusProjection(
                store, consumer_status_enabled=feedback, revision_ownership=ownership
            )
            yield h


@pytest.fixture(
    params=[("memory", False), ("memory", True), ("postgres", False), ("postgres", True)]
)
async def projections(request):
    backend, notifications = request.param
    async with projection_harness(backend, notifications=notifications) as h:
        yield h


async def drain(projection, account_id):
    turns = []
    for _ in range(100):
        result = await projection.project_one(account_id=account_id)
        if not result.did_work:
            return turns
        turns.append(result)
    raise AssertionError("projection failed to converge")


async def inputs(h, account_id):
    if h.pool is None:
        return tuple(h.store._projection_accounts[account_id].inputs)
    from adcp.reporting.projection.capture import decode_projection_input

    async with h.pool.connection() as c:
        rows = await (
            await c.execute(
                "SELECT input FROM reporting_projection_inputs"
                " WHERE account_id=%s ORDER BY sequence",
                (account_id,),
            )
        ).fetchall()
    return tuple(decode_projection_input(r[0]) for r in rows)
