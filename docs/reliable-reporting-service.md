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

An adapter may also return an SDK `GetMediaBuyDeliveryResponse`, including its
`totals` or `by_package` metrics. The inline wrapper converts finite float and
Decimal metrics into decimal strings before staging or revision hashing (for
example, `1.25` becomes `"1.25"`). It rejects non-finite values. This is an SDK
model conversion, not a change to the canonical row contract: adapters returning
their own rows must still use integers or decimal strings, never binary floats.

## Configure trusted routes

Register adapters before initialization. Supply `capability_offerings` with each
registration so a fresh service can advertise its public contracts before any
account has a delivery configuration. Each entry is a `ReportingDeliveryOffering`
or its wire mapping, including the buyer-facing schedule and SLA. These cannot
be inferred from the source's safe fetch cadence. Registration validates the
offering's pinned definition/schema and finality against the adapter, and freezes
the declaration. Conflicting declarations for the same public ID are rejected.

Configure each new immutable delivery generation. The resolver is called once
per generation in each service process; its account, currency, timezone, adapter
route, source scope, and offering choices are frozen together. Its
`capability_offering` must match a registered declaration. Existing adopters that
only supply offerings with account contexts retain that behavior, but must add
registration-time declarations to enable discovery before first configuration.

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
reporting.sources.register(
    "gam", GAMAdapter(gam_client, gam_capabilities),
    capability_offerings=[gam_public_offering],
)
reporting.sources.register(
    "freewheel", FreeWheelAdapter(freewheel_client, freewheel_capabilities),
    capability_offerings=[freewheel_public_offering],
)

for generation in await load_active_reporting_configurations():
    await reporting.configure(generation)

platform = reporting.install(platform)  # use the returned instance
```

Start and close the service in your application's lifespan. Capabilities appear
after `start()`, including on a database with no reporting
generations. Core defaults advertise a six-hour automated recovery window and
400-day status retention; use the service's `automated_recovery_window` and
`status_retention_days` options for different first-boot policy, consistent with
the configurations you admit.

For an `ADCPHandler`, the default caller resolver accepts authenticated transport
context with `account_id` and `caller_identity`. For a decisioning
`RequestContext`, it uses the resolved `context.account.id` and the bearer
`adcp.server.auth.current_principal`, or `context.auth_principal` backed by the
request's verified `AuthInfo` for signed requests. Legacy contexts keep their
`account_id` and `caller_identity` even when their opaque `account` is populated.
The decisioning `caller_identity` is a cache key, not the buyer principal.
Missing authentication is rejected. Supply
`caller_resolver` for a different trusted identity model; never choose consumer
identity from the request body.

## Decisioning platforms and lazy routers

Core `install()` accepts a `DecisioningPlatform`, including `LazyPlatformRouter`:

```python
from adcp.decisioning import serve

platform = reporting.install(platform)
# Use reporting.start / reporting.close in your application's lifespan.
# Keep your usual decisioning auth, registry and other serve() arguments.
serve(platform, name="my-seller")
```

The normal dispatcher resolves and authorizes the account before reporting
status, consumer status, receipts, or exact revision reads. Your `AccountStore`
continues to own principal-to-account authorization. Consumer status is exposed
only when enabled; receipts require an installed handler. Aggregate delivery
and unrelated methods retain their usual routing and thread-pool behavior.
Reporting calls and first-boot discovery do not instantiate lazy tenants.
Decisioning validation, including the idempotency-wiring boot guard, still runs.
Repeated installation of the same service is a no-op; replacing it with a
different service on the same platform is rejected.

When using `production=`, the service still returns its production `ADCPHandler`.
For a decisioning application, first call `create_adcp_server_from_platform`,
pass that handler to `reporting.install`, then mount the returned handler with
`adcp.server`'s MCP/A2A factory. Retain and close the decisioning executor and
registry as usual. Do not pass that production handler to `adcp.decisioning.serve`.

## PostgreSQL production wiring

The PostgreSQL factory installs ledger, source staging, and replay-seal schemas
in the caller's pool. Ordinary adapter registrations use those durable stores;
restart replay reads the sealed bytes without refetching the source. Open the
pool before starting the service and close it after service shutdown settles.
This is a change for existing `postgres()` callers, including those that do not
enable `production`: `initialize()` now bootstraps `reporting_inline_objects`
and `reporting_inline_seals` in addition to the ledger schema. Before upgrading,
ensure the pool's startup role can run that source-schema DDL. A DDL-restricted
runtime role cannot use this factory's schema bootstrap until its deployment
grants or startup sequence are updated.

Core startup also restores persisted configurations automatically. With adapters
registered, `initialize()` calls the built-in ledger's trusted
`list_all_configurations()` operation once, across all accounts and generations,
then resolves account context for each recovered generation. It preserves stored
activation/retirement boundaries, revisions and producer checkpoints; no new
`configure()` call or adopter-maintained account enumeration is needed. Retired
generations remain available for outstanding periods. Register every retained
generation's adapter before startup; an unavailable route or invalid context
fails startup instead of silently losing scheduled work. A service with no source
registrations can still start for retained reads only. Older custom stores without
the enumeration operation can keep explicit `configure()` startup. Enumeration
is for service recovery and is never exposed as a buyer task.

Pass `ReportingProductionOptions` to compose the managed materializer, status
projection, exact reads, consumer/receipt handlers, and optional signed
notification workers. Adopters supply domain declarations and their actual
destination, verifier registry, trusted account task and authorization callbacks.
The SDK constructs the component graph:

```python
from psycopg_pool import AsyncConnectionPool
from adcp.reporting import (
    ReliableReportingService,
    ReportingProductionOptions,
    ReportingServiceOffering,
)

pool = AsyncConnectionPool(DATABASE_URL, open=False)
await pool.open()

reporting = ReliableReportingService.postgres(
    pool=pool,
    account_context=resolve_reporting_context,
    consumer_status_enabled=True,
    production=ReportingProductionOptions(
        offerings=(
            ReportingServiceOffering(
                adapter="gam",
                offering=gam_public_offering,       # ReportingDeliveryOffering
                profile=gam_execution_profile,      # ProducerOfferings
                source_offering_id=gam_source_offering_id,
                verification_key=gam_verifier.key,
            ),
        ),
        destination=warehouse_provider,             # ReportingProductionDestination
        registry=verifier_registry,                 # ReportingRevisionVerifierRegistry
        configuration_task=account_task,            # ReportingProductionConfigurationTask
        resolve_account=authorize_reporting_account,
    ),
)
reporting.sources.register("gam", gam_adapter)
platform = reporting.install(platform)
# Mount the returned handler on MCP/A2A before reporting.start().
# Use reporting.start / reporting.close as the application's lifespan hooks.
```

Each registered production adapter needs at least one public offering. Multiple
public offerings for an adapter share its fixed execution profile and verifier;
different adapters may reuse a local source offering ID. Profiles include the
source scope, currency, metric/dimension sets, and snapshot/official selections.
Admission checks trusted account context against the selected profile and
persists it once per generation. Restart recovery uses the stored route.

Managed adapters also implement the existing
`ReportingProductionSource.configuration_binding(configuration)` method. Return
the current source binding, including the exact media-buy/product mapping, or
`None` when unauthorized. The wrapper forwards this callback before dispatch and
again under the account lock before seal/publication. Revocation or remapping
discards in-flight results; restoring the admitted mapping allows the next turn
to retry. **Revocation takes effect at the next dispatch or publish.** Adopters
own any caching and latency inside the callback. A fetch already in progress is
allowed to return. Buyer-facing feed and destination authorization still run on
each request and session.

To enable signed push, set `notifications=True` and provide `subscriptions`,
`cipher` (`ReportingEnvelopeCipher`), and `signing` (`ReportingProductionSigning`)
on the options. All three are required together. The factory owns the outboxes,
attempt storage and workers for source, materialization and status events.
With only `notifications=True`, events are retained for polling without claiming
push delivery. Consumer status is controlled by the service's
`consumer_status_enabled` argument. Reconciled Billing follows the validated
official offering, receipt method, and destination contracts; a flag cannot
promote an unsupported provider. The memory factory accepts the same options
for conformance, but never advertises durable Managed/Reconciled guarantees.

Use the typed `sync_accounts` admission callback for this composition, rather
than `configure()`. Production workers belong to `start()`/`close()`; the Core
`run_worker()` entry point is not used. See the
[production guide](reporting-production.md) for account admission and provider
contracts, and [the wiring example](../examples/reporting_service_production.py).

## Advanced composition

`postgres()` without production options remains the Core factory. Advanced
adopters can still inject workers and receipt handlers, pass explicit
`staging=`/`seals=` to `sources.register`, use `constituent_of=` for a custom row
identity, or register a complete executor and object reader. Explicit stores are
borrowed; initialize their schemas yourself. `from_production()` continues to
own an already composed production graph. Keep injected components separate
from `production=` options, which own that graph themselves.

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

The [adapter testing guide](reporting-adapter-testing.md) adds named failure
points, sync/async barriers, injectable PostgreSQL staging/seals, and reusable
exact-read assertions after service recreation or durable recovery.

See [`examples/reliable_reporting_adapters.py`](../examples/reliable_reporting_adapters.py)
for compact GAM-like and FreeWheel-like adapter definitions.
