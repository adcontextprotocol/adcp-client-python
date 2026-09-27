# Testing reporting adapters and service recovery

`adcp.reporting.testing` supplies a deterministic clock, scripted sync/async
adapters, named failure points, barriers, and assertions for exact reads after
service recovery. These helpers use the same public contracts as adopter
implementations. They also have lazy exports from `adcp.reporting`.

## Start with adapter conformance

Pass an adapter and a frozen request to `run_reporting_adapter_conformance`.
It wraps `fetch_slice` in `InlineReportingSource`, executes the same source key
twice, and verifies identical manifests and every staged byte. By default it
uses fresh `InMemoryStagingStore` and `InMemorySealStore` instances. An optional
`clock` drives both execution timestamps and conformance deadline checks.

```python
from adcp.reporting.fixtures import redacted_snapshot_request
from adcp.reporting.testing import (
    DeterministicReportingClock,
    run_reporting_adapter_conformance,
)


async def test_adapter(adapter):
    request = redacted_snapshot_request()
    clock = DeterministicReportingClock(request.period.source_read_cutoff_at)
    await run_reporting_adapter_conformance(adapter, request, clock=clock)
```

Use a request/capability fixture that matches the adapter's declared offering,
scope, currency, metrics, and source-local period. The SDK's `redacted_*`
fixtures describe a sample provider, not every provider's contract.

To test source replay through PostgreSQL, install `adcp[pg]`, use an isolated
test database/schema, and inject the public stores. Initialize the schema before
wrapping a store; failure wrappers borrow it and do not own DDL or the pool.

```python
from adcp.reporting.inline_storage import PgReportingSealStore, PgReportingStagingStore
from adcp.reporting.testing import run_reporting_adapter_conformance


async def test_pg_replay(pool, adapter, fresh_adapter, request, clock):
    staging = PgReportingStagingStore(pool=pool)
    await staging.create_schema()  # Includes the replay-seal schema.
    first = await run_reporting_adapter_conformance(
        adapter, request, clock=clock,
        staging=staging, seals=PgReportingSealStore(pool=pool),
    )
    replay = await run_reporting_adapter_conformance(
        fresh_adapter, request, clock=clock,
        staging=PgReportingStagingStore(pool=pool),
        seals=PgReportingSealStore(pool=pool),
    )
    assert replay == first
```

## Inject failures at commit boundaries

`ReportingFailurePlan.at(point, *steps)` queues an exception, a
`ReportingTestBarrier`, or `None` for a successful visit. Steps are consumed in
order; visits after the queue is exhausted proceed normally. Call
`assert_consumed()` to detect an unreached or misspelled point. The `hits` tuple
records all visits. Use synthetic errors without real provider bodies or
credentials.

| Component | Points | Effect of an `after` failure |
| --- | --- | --- |
| `ScriptedReportingAdapter(..., failures=plan)` | `fetch.before`, `fetch.after` | The queued answer has been consumed. |
| `FaultInjectingReportingStagingStore(store, plan)` | `stage.before`, `stage.after` | The underlying object write remains. |
| Same staging wrapper | `read.before`, `read.after` | The read completed before the injected error. |
| `FaultInjectingReportingSealStore(store, plan)` | `seal.get.before`, `seal.get.after` | Seal lookup completed. |
| Same seal wrapper | `seal.before`, `seal.after` | The durable seal remains despite the lost acknowledgement. |

For example, `plan.at("seal.after", OSError("lost acknowledgement"))` exercises
the case where a retry must reuse an existing sealed publication. Register the
wrappers through `service.sources.register(..., staging=..., seals=...)`.
They accept memory, PostgreSQL, or adopter stores and never copy or repair
private state. Source fetch errors appear as failed slices; unexpected storage
errors appear in the service turn's `configuration_errors`. Both leave the
service available to retry other configurations.

Adopter wrappers can add boundaries around their own operations with
`await plan.hit("destination.publish.after")` or
`plan.hit_sync("notification.send.before")`. These names are chosen by the
test; they do not install hooks in SDK production workers. Queue access is
thread-safe. Use a barrier when a test needs a particular order between
concurrent calls.

## Hold an in-flight call during shutdown

Create a `ReportingTestBarrier` inside the test's event loop. It supports async
operations and synchronous provider SDKs running in worker threads. Await
`wait()` to observe the boundary and always `release()` it in cleanup. Its
finite wall-clock timeout is a deadlock watchdog, separate from the reporting
clock. Calling `pause_sync()` on the event-loop thread raises instead of
blocking the loop.

```python
import asyncio

from adcp.reporting import ReliableReportingShutdownTimeoutError, ReliableReportingState
from adcp.reporting.testing import ReportingFailurePlan, ReportingTestBarrier


async def assert_shutdown_drains(service, plan):
    # Configure the service and give its ScriptedReportingAdapter this plan.
    barrier = ReportingTestBarrier()
    plan.at("fetch.after", barrier)
    work = asyncio.create_task(service.run_worker())
    try:
        await barrier.wait()
        try:
            await service.close(timeout=0)
        except ReliableReportingShutdownTimeoutError:
            assert service.state is ReliableReportingState.STOPPING
        else:
            raise AssertionError("shutdown completed while a fetch was held")
    finally:
        barrier.release()
        try:
            await work
        finally:
            await service.close()
```

An owned resource must remain open while that call is held and close exactly
once after settlement. Extend this pattern to test revocation during a fetch,
failed callbacks, or cancellation of a caller. Use explicit events rather than
sleeping to guess whether work has started.

## Assert stable publication after recovery

`capture_reporting_lifecycle(store, account_id=..., reporting_obligation_id=...)`
captures a published obligation and all its revisions. It checks each revision's
exact read, walks every page with identity/count/cursor bounds, recomputes the
Core content hash (including managed typed totals), and retains canonical row
bytes. Set `page_size=1` to exercise pagination with small fixtures.

After closing the first service, construct a fresh service and adapters, retry
the same configuration, and call
`await assert_reporting_lifecycle_replay(snapshot, restarted.store)`. It fails
on missing, changed, or extra publications. Keep workers quiescent during both
captures, and hold or advance the test clock deliberately so replay targets the
same work rather than a newly due period.

For in-memory tests, retain the same `InMemoryReportingLedgerStore`, staging,
and seal instances across services. This tests service recreation within one
process. For PostgreSQL, construct fresh ledger/staging/seal instances over the
retained test database and run with both pool transaction modes. The assertions
also work in a separate process; the kit does not label memory as durable or
certify a reporting tier.

Managed pipeline tests can reuse `InMemoryReportingReconciliationStore`,
`InMemoryReportingOutbox`, `ReferenceReportingDestinationWriter`, and
`ReferenceReportingResolver` from the existing reporting packages. Reference
destination implementations are test fixtures and are not production-eligible.
Use the failure plan in adopter wrappers to test destination, notification, or
receipt retry boundaries, then assert the corresponding public records as well
as the source lifecycle snapshot. This snapshot covers obligations/revisions
and exact rows, not the entire notification or receipt state machine.
