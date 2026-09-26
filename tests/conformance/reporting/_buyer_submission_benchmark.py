"""Reproducible buyer capacity/lock measurements, without timing assertions.

Run with an isolated real PostgreSQL database via ADCP_PG_TEST_URL:
  python -m tests.conformance.reporting._buyer_submission_benchmark

Each cell starts with empty validation caches and a new store/schema. Inputs are
already constructed; elapsed time includes preparation, reservation, every
confirmation, and outcome access. The responder is in-process with zero network
delay; PostgreSQL cells exercise actual buyer persistence. No extrapolation is
reported as a measured result. Concurrent original gates may share the host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from contextlib import asynccontextmanager

from adcp.reporting.submissions import (
    InMemoryReportingSubmissionIntentStore,
    PgReportingSubmissionIntentStore,
    models,
    prepare_reporting_receipt_submission,
    submit_reporting_receipts,
)
from adcp.reporting.submissions._validation_cache import ValidationCache
from adcp.types import ReportingReceipt

from ._buyer_submission_support import SCOPE, ReceiptClient, TrustedAuthorizer, mixed, response_for
from ._generation_support import isolated_reporting_pool


def cold_caches():
    models.PLANS = ValidationCache(entries=16, byte_budget=32 * 1024 * 1024)
    models.CONFIRMATIONS = ValidationCache(entries=256, byte_budget=32 * 1024 * 1024)


class MeasuredConnection:
    def __init__(self, connection, measurements):
        self.connection, self.measurements = connection, measurements
        self.lock_started = self.lock_returned = None

    @asynccontextmanager
    async def transaction(self):
        try:
            async with self.connection.transaction():
                yield
        finally:
            if self.lock_returned is not None:
                released = time.perf_counter()
                self.measurements.append(
                    {
                        "lock_query_seconds": self.lock_returned - self.lock_started,
                        "critical_section_seconds": released - self.lock_started,
                        "after_lock_return_to_release_seconds": released - self.lock_returned,
                    }
                )

    async def execute(self, query, params=None):
        timed = query.startswith("SELECT canonical_identity,current_submission_id") and (
            "FOR UPDATE" in query
        )
        if timed:
            self.lock_started = time.perf_counter()
        result = await self.connection.execute(query, params)
        if timed:
            self.lock_returned = time.perf_counter()
        return result


class MeasuredPool:
    def __init__(self, pool):
        self.pool, self.measurements = pool, []

    @asynccontextmanager
    async def connection(self):
        async with self.pool.connection() as connection:
            yield MeasuredConnection(connection, self.measurements)


def lock_summary(measurements):
    # Client-observed critical section includes lock acquisition/query transfer
    # through committed transaction exit. It is an upper bound on lock hold when
    # there is no external blocker, not a server-clock lock trace.
    result = {"transactions": len(measurements)}
    for name in (
        "lock_query_seconds",
        "critical_section_seconds",
        "after_lock_return_to_release_seconds",
    ):
        ordered = sorted(item[name] for item in measurements)
        result[name] = {
            "min": min(ordered),
            "median": ordered[len(ordered) // 2],
            "max": max(ordered),
            "total": sum(ordered),
        }
    return result


async def measure(backend, count, store, measured_pool=None, *, wide=False):
    cold_caches()
    inputs, client = mixed(count), ReceiptClient()
    if wide:
        for item in inputs:
            item.reporting_receipt_id += "x" * (255 - len(item.reporting_receipt_id))
            if isinstance(item, ReportingReceipt):
                item.consumer_commit_ref = "c" * 255
    started, cpu = time.perf_counter(), time.process_time()
    result = await submit_reporting_receipts(
        client, authorizer=TrustedAuthorizer(client), store=store, receipts=inputs
    )
    assert not result.pending and len(result.outcomes) == count
    assert len(result.submitted_receipts) == count
    expected = (count + 99) // 100
    assert len(client.requests) == result.submission.confirmed_chunks == expected
    assert all(
        1 <= len(request.get("receipts", [])) + len(request.get("adjustment_receipts", [])) <= 100
        for request in client.requests
    )
    record = {
        "backend": backend,
        "items": count,
        "wide_identifiers": wide,
        "chunks": expected,
        "seconds": time.perf_counter() - started,
        "buyer_process_cpu_seconds": time.process_time() - cpu,
        "plan_bytes": len(result.submission._plan),
        "confirmed_bytes": models._confirmation_bytes(result.submission._confirmed),
        "cache_entries": [len(cache._values) for cache in (models.PLANS, models.CONFIRMATIONS)],
        "cache_retained_bytes": [
            cache._retained_bytes for cache in (models.PLANS, models.CONFIRMATIONS)
        ],
    }
    if measured_pool is not None:
        record["locks"] = lock_summary(measured_pool.measurements)
    print(json.dumps(record), flush=True)


async def paused_validator_waiter():
    async with isolated_reporting_pool() as pool:
        measured = MeasuredPool(pool)
        store = PgReportingSubmissionIntentStore(pool=measured)
        await store.create_schema()
        state = await store.reserve(prepare_reporting_receipt_submission(SCOPE, mixed(2000)))
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        validation = []

        class Paused(PgReportingSubmissionIntentStore):
            def _validate_snapshot(self, scope, snapshot):
                started = time.perf_counter()
                result = super()._validate_snapshot(scope, snapshot)
                validation.append(time.perf_counter() - started)
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(20)
                return result

        response = response_for(state.request(0))
        cold_caches()
        measured.measurements.clear()
        task = asyncio.create_task(
            Paused(pool=measured).confirm(SCOPE, state.submission_id, 0, response)
        )
        try:
            await asyncio.wait_for(entered.wait(), 15)
            started = time.perf_counter()
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "SELECT 1 FROM reporting_buyer_submission_scopes"
                    " WHERE scope_sha256=%s FOR UPDATE",
                    (SCOPE.storage_key,),
                )
            elapsed = time.perf_counter() - started
            assert not release.is_set() and not task.done()
            release.set()
            assert (await task).confirmed_chunks == 1
            print(
                json.dumps(
                    {
                        "proof": "independent_pg_waiter_acquired_while_validator_paused",
                        "items": 2000,
                        "validation_seconds": validation,
                        "waiter_transaction_seconds": elapsed,
                        "locks": lock_summary(measured.measurements),
                    }
                ),
                flush=True,
            )
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 2000, 10000])
    parser.add_argument("--wide-identifiers", action="store_true")
    parser.add_argument(
        "--backends", nargs="+", choices=["memory", "postgres"], default=["memory", "postgres"]
    )
    args = parser.parse_args()
    print(json.dumps({"python": sys.version, "executable": sys.executable}), flush=True)
    for backend in args.backends:
        for count in args.sizes:
            if backend == "memory":
                await measure(
                    backend,
                    count,
                    InMemoryReportingSubmissionIntentStore(),
                    wide=args.wide_identifiers,
                )
            else:
                async with isolated_reporting_pool() as pool:
                    measured = MeasuredPool(pool)
                    store = PgReportingSubmissionIntentStore(pool=measured)
                    await store.create_schema()
                    await measure(backend, count, store, measured, wide=args.wide_identifiers)
    if "postgres" in args.backends:
        await paused_validator_waiter()


if __name__ == "__main__":
    asyncio.run(main())
