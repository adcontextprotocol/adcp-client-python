# Reliable Reporting service

`ReliableReportingService` is the adopter-facing composition layer for Reliable
Reporting. An ad-server adapter declares effective-account source capabilities
and fetches one frozen slice; the SDK owns staging, replay seals, obligation
creation, immutable revisions, snapshot refresh and official close, status,
exact revision reads, capability advertisement, and worker lifecycle.

For an upgrade, begin with the
[reporting release notes and deployment boundaries](reporting-release-notes.md)
and the [production composition guide](reporting-production.md). The examples
below explain the service API; complete service and cross-language release
acceptance remain pending.

The minimum adapter has no AdCP transport methods:

```python
class GAMAdapter:
    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    def fetch_slice(self, request: ReportingSourceSliceRequestV1):
        report = self.client.run_report(
            account_id=request.identity.source_scope["network_code"],
            start=request.period.start,
            end=request.period.end,
            metrics=request.requested_metrics,
        )
        return InlineFetchResult(
            rows=normalize_gam_rows(report.rows),
            data_through=report.data_through,
            provisional_until=report.provisional_until,
        )
```

Synchronous provider clients such as the example above run in a worker thread.
They must set their own network timeout because Python cannot interrupt a
running thread. An asynchronous FreeWheel-like client can use the same API:

```python
class FreeWheelAdapter:
    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self._capabilities

    async def fetch_slice(self, request: ReportingSourceSliceRequestV1):
        rows = await self.client.fetch_delivery(
            network_id=request.identity.source_scope["network_id"],
            start=request.period.start,
            end=request.period.end,
        )
        return normalize_freewheel_rows(rows)
```

`None` means the slice is not ready. `[]` is a real, fully observed zero-row
period. `InlineFetchResult` carries watermarks, partial coverage, warnings, and
source-specific settling evidence. These meanings are deliberately distinct.

## Configure trusted routes

Register adapters before initialization, then configure each immutable delivery
generation. The resolver is called once for a generation; its account, currency,
timezone, adapter route, source scope, and offering choices are frozen together.

```python
from datetime import timedelta

from adcp.reporting import ReliableReportingService, ReportingAccountContext


async def resolve_reporting_context(configuration):
    account = await accounts.require(configuration.account_id)
    return ReportingAccountContext(
        account_id=account.id,
        adapter=account.ad_server,              # "gam" or "freewheel"
        currency=account.reporting_currency,
        account_timezone=account.timezone,
        source_scope=account.reporting_source_scope,
        snapshot_offering_id=account.snapshot_offering_id,
        official_offering_id=account.official_offering_id,
        capability_offering=account.adcp_reporting_offering,
        publication_namespace=account.reporting_namespace,
    )


reporting = ReliableReportingService.memory(
    account_context=resolve_reporting_context,
    worker_interval=timedelta(minutes=1),
)
reporting.sources.register("gam", GAMAdapter(gam_client, gam_capabilities))
reporting.sources.register(
    "freewheel", FreeWheelAdapter(freewheel_client, freewheel_capabilities)
)

for generation in await load_active_reporting_configurations():
    await reporting.configure(generation)

platform = reporting.install(platform)  # use the returned instance
async with reporting:
    await serve(platform)
```

The default caller resolver accepts only authenticated transport context with
`account_id` and `caller_identity`. Supply `caller_resolver` when an application
uses a different trusted identity model; never route status from an account ID
asserted only in the request body.

## PostgreSQL production wiring

```python
from psycopg_pool import AsyncConnectionPool
from adcp.reporting import ReliableReportingService

pool = AsyncConnectionPool(DATABASE_URL, open=False)
await pool.open()

reporting = ReliableReportingService.postgres(
    pool=pool,
    account_context=resolve_reporting_context,
    caller_resolver=resolve_authenticated_caller,
    worker_interval=timedelta(minutes=1),
    materialization_worker=managed_delivery_worker,   # optional tier
    notification_worker=notification_worker,          # optional extension
    notification_attempt_store=notification_attempts,
    receipt_handler=receipt_handler,                   # optional tier
    reconciled_billing=True,
    worker_error_handler=report_reporting_worker_error,
)
```

The PostgreSQL factory makes the ledger durable; it does not make the default
adapter staging or replay-seal stores durable. Production adapters should pass
durable `staging=` and `seals=` implementations to `sources.register`, or use
`sources.register_executor` for a custom executor and object reader. Committed revision rows are retained in the ledger for exact reads. Durable
staging and seals are needed to recover interrupted acquisitions and replay
previously sealed source results across a restart.

Managed delivery, notification, and receipt components are replaceable
extensions. Startup rejects combinations that cannot be advertised honestly,
including notifications without attempt storage and reconciled billing without
both managed delivery and receipts. Capability output is derived from the
components actually installed.

Unexpected failures are isolated by configuration or extension, logged, and
included on `ReliableReportingTurn.configuration_errors` or `extension_errors`.
Other configurations and extensions still run, and the background scheduler
retries on its next turn. These failures do not change service readiness.
`worker_error_handler(component, error)` receives the original exception and
the component name: `configuration:{account_id}:{delivery_config_id}@{version}`,
`materialization`, or `notification`. SDK logs contain only the component kind;
adopter callbacks control any further diagnostics. A callback failure is logged
without interrupting the remaining work.

Failures outside an individual configuration or extension turn stop the
background scheduler, notify the callback as `service` with the original error,
and withdraw readiness. Startup, scheduler, and resource cleanup failures are
terminal: after admitted work settles, `wait()` raises a sanitized
`ReliableReportingServiceError`. Restart those services with a new instance.

`run_worker()` is also available for an external scheduler. Calls within one
service process are serialized. Ledger writes are convergent, but deployments
running multiple service schedulers must provide one account/configuration lease
owner until the account-scoped lease integration is enabled.

## Adapter conformance

Use the reusable harness in the adapter's own test suite:

```python
from adcp.reporting import (
    ScriptedReportingAdapter,
    run_reporting_adapter_conformance,
)
from adcp.reporting.fixtures import redacted_capabilities, redacted_snapshot_request


async def test_my_adapter_conforms():
    adapter = MyAdapter(...)
    await run_reporting_adapter_conformance(adapter, redacted_snapshot_request())
```

The harness wraps sync or async fetches in the production inline executor, uses
deterministic in-memory staging/seals, executes the same source key twice, and
checks replay identity plus every staged byte. `DeterministicReportingClock`
and `ScriptedReportingAdapter` support settling-window and failure-injection
tests without sleeping.

See [`examples/reliable_reporting_adapters.py`](../examples/reliable_reporting_adapters.py)
for compact GAM-like and FreeWheel-like adapter definitions.
