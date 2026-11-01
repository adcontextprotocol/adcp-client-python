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

`capability_block()` and installed capability discovery describe registered
reporting offerings and installed components throughout the service lifetime.
They are available before startup and remain stable while stopping, after
closure, and after worker failure. An unconfigured service declares no reporting
block. Use `service.ready` for local work admission; buyer tools report runtime
unavailability separately. Discovery does not start workers or instantiate lazy
platform tenants.

Managed-production discovery also retains registered destination, signing and
reconciliation declarations through shutdown or failure. Build-time discovery
does not construct the production graph. The development memory production
graph declares no durable production tier; Core memory services still declare
their inline offerings. `ReportingProductionSupport.reporting_delivery()` keeps
its runtime readiness check for operator and admission callers.

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
This is a change for existing PostgreSQL factory callers, including those that do not
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
generation's adapter before startup; an unavailable route or invalid resolved
context still fails startup. An account-context resolver failure is isolated to
its configuration so healthy accounts can start. The read-only
`initialization_errors` mapping exposes these failures by generation key, and
each Core worker turn includes unresolved failures in `configuration_errors`.
Temporary resolver failures are retried on later turns. A resolver that raises
`ReliableReportingConfigurationError` is not retried automatically; fix the
account and call `configure()` explicitly to recover it. Schema, connectivity
and source-registration failures still prevent startup. Each queued retry isolates
the entire binding attempt, including producer construction, so healthy accounts
continue in manual and background worker turns. Temporary construction failures
remain queued for later turns; `ReliableReportingConfigurationError` stops
automatic retries until explicit `configure()` repair. If a queued retry resolves
to an invalid context (including a wrong account, timezone, adapter route or
offering), that generation remains observable as a permanent error while healthy
accounts continue. Fix its resolved facts and call `configure()` explicitly;
initial invalid context validation and producer-construction failures remain fatal.
Direct `configure()` admission also propagates failures. Schema and store operations,
source freezing, and cancellation remain outside account retry isolation.
A service with no source registrations can still start for retained reads only.
Older custom stores without the enumeration operation can keep explicit
`configure()` startup. Enumeration
is for service recovery and is never exposed as a buyer task.

Scheduled source reads wait until the selected offering's
`expected_availability_lag` has elapsed after the period end. Authoritative
offerings also wait for `days_after_period_end` and `source_local_ready_time` in
the offering's `source_timezone`, falling back to the stored period's timezone
when it is omitted. Obligations still close at the period boundary;
waiting does not change `expected_at` or the advertised recovery deadline.
Retryable source failures do not escalate before the worst-case availability
window of the actual requested offering. Built-in stores resume frozen pending
acquisitions using that request's offering readiness, including a snapshot retry
that crosses an official-close boundary. Custom observation stores may implement
`PendingProvisionalAcquisitionStore` to provide the same lookup; without it, a
pending snapshot waits conservatively until the authoritative offering is ready.

Terminal failures still follow the existing recovery deadline.
Direct acquisition and manual replay keep their explicit-call behavior.

Pass `ReportingProductionOptions` to compose the managed materializer, status
projection, exact reads, consumer/receipt handlers, and optional signed
notification workers. Adopters supply domain declarations and their actual
destination, verifier registry, trusted account task and authorization callbacks.
The SDK constructs the component graph:

```python
from psycopg_pool import AsyncConnectionPool
from adcp.reporting import ReportingProductionOptions, ReportingServiceOffering
from adcp_reporting_storage import postgres_reliable_reporting_service

pool = AsyncConnectionPool(DATABASE_URL, open=False)
await pool.open()

reporting = postgres_reliable_reporting_service(
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

## Reporting subscriptions (AdCP 3.3, experimental)

`media_buy.reporting_subscriptions` lets a caller register reporting once on its
principal through `sync_principal`, rather than once per account through
`sync_accounts`. `adcp.reporting.subscriptions` holds both halves.

**Seller, managed path (recommended).** Add a `subscriptions` block to the
service you already run. Its presence enables the feature; the SDK derives the
rest from what the platform already serves.

```python
from adcp.reporting import ReliableReportingService, SubscriptionOptions

reporting = ReliableReportingService(
    store=store,
    account_context=resolve_account_context,  # also receives subscription generations
    subscriptions=SubscriptionOptions(max_accounts=500),
)
reporting.sources.register("gam", gam_source, capability_offerings=[daily_offering])
platform = reporting.install(MySeller())  # then serve the platform as usual
```

| The SDK derives | From | Override on `SubscriptionOptions` |
|---|---|---|
| The principal runtime, `get_principal`/`sync_principal` and the principal section of capabilities | the service's own `PrincipalService` | `principals`, `principal_store` |
| The accounts a principal may read | the platform's `list_accounts` (a decisioning platform's `accounts.list`), read under the principal's identity | `accounts(principal, *, cursor)` |
| Each account's buys | the platform's `get_media_buys`, under the same identity | `buys(principal, account_id)` |
| Each package's offerings | the package's `reporting_delivery_offering_ids` snapshot from `get_media_buys` (`[]` means none), else every registered offering | `buy_offerings(media_buy)` |
| Offerings | the registered `capability_offerings`, each publishing `standard_dimensions` | none |
| Report content per generation | `account_context` | `configure(generation)` |
| The authenticated principal | the caller the reporting tasks resolve, with `ToolContext.tenant_id` | `resolve_principal(request, context)` |

- **Identity outside a request.** Reads made for a principal outside a request
  carry its last authenticated binding, recorded at `sync_principal`: the
  tenant and a copy of its `AuthInfo` with no credential material (key ids,
  client ids and verified signers only). A seller whose accounts are only
  reachable through a live upstream token supplies `accounts`. A listing that
  fails raises `SUBSCRIPTION_ROSTER_UNAVAILABLE` and is reported through
  `planning_error_handler`; it never reads as an empty roster, so nothing is
  revoked.
- **Accounts must carry `timezone`.** Coverage never substitutes a zone.
  `Account(timezone=..., operator=...)` serves both on a decisioning platform.
- **Scheduling.** `sync_principal`, `sync_accounts`, `create_media_buy` and
  `update_media_buy` served through the installed platform reconcile the
  principals concerned (`reconcile_on_write`, on by default). The reconcile
  runs after the response, on a background drain, so a write never waits for
  it (`await reporting.drain_subscription_triggers()` waits, for tests). Each
  read of the platform is bounded by `read_timeout` (30 seconds by default).
  Each worker turn
  first reconciles every principal a period boundary has passed for since its
  last reconcile, then plans. Call `await reporting.touch(account_id=...)` after
  a change the SDK did not serve, such as a seller UI edit.
- **`replace_account_configs`.** Supply `account_delivery_configs` and
  `end_account_configs(principal, account_id, ids, *, at)`. The flag is then
  applied once per `subscription_version`, or when it changes to true.
  Without them it is refused with `UNSUPPORTED_FEATURE`.
- **Configuration errors.** Startup refuses an offering without
  `standard_dimensions` (`SUBSCRIPTIONS_REQUIRE_STANDARD_DIMENSIONS`) and a
  platform that cannot list accounts (`SUBSCRIPTION_ROSTER_UNAVAILABLE`). Both
  are `ReliableReportingConfigurationError` with `code` and
  `recovery="correctable"`, and each message ends with the fix.
- **Grants and `authorized_at`.** The SDK journals every grant it learns of and
  is the only source of `authorization.authorized_at`. It applies the chain
  rule: a re-grant before `unauthorized_at + status_retention_days` continues
  the chain, so `authorized_at` does not move; a later re-grant starts a new
  one. Served `list_accounts` and `sync_accounts` responses carry the
  journal's value, the same one coverage uses. A host value is adopted on
  first observation; a later one that moves backwards is rejected and logged
  as `authorized_at_regressed`. Those responses also update the journal, as
  does `await reporting.grants.changed(principal, account_id, at=...,
  authorized=..., allowed_tasks=...)` for sellers whose entitlement service
  emits events. Grant checks read the journal, so a reduction shown to the
  caller is enforced on its next reporting task.
- **Wind down, don't revoke.** Revocation means no access at all, receipts and
  statuses included. To end a relationship while the buyer finishes
  reconciling, set `account.authorization = wind_down_grant(account.authorization)`
  (from `adcp.server`). It lists only `REPORTING_WIND_DOWN_TASKS`
  (`get_reporting_status`, `sync_reporting_receipts`, `sync_reporting_status`)
  and keeps `authorized_at`, so coverage continues.
- **Storage: one decision.** Pass `storage=memory_storage()` or
  `storage=postgres_storage(dsn=...)` (or `pool=`) from `adcp.storage`. One
  object holds the ledger, staged source objects and seals, principal records
  and the subscription engine state; durability is a property of the storage,
  never a flag.
  - Process-local storage is refused for reporting subscriptions outside
    development with `REPORTING_STORAGE_NOT_DURABLE`; pytest and
    `ADCP_ENV=development` or `test` allow it. Without `storage=`, every store
    the managed path uses must declare `is_durable = True` (the SDK's
    PostgreSQL stores do).
  - The PostgreSQL schema is managed by versioned migration files, applied in
    order by `python -m adcp migrate [--dsn URL] [--status | --dry-run]`
    (`--dsn` defaults to `$ADCP_DATABASE_URL`, then `$DATABASE_URL`). Startup
    never changes the schema: it refuses one that is behind with
    `REPORTING_SCHEMA_BEHIND`, naming the command and the pending versions.
  - `0002 reporting_subscription_generations` widens the generation key and
    is a stopped-worker upgrade: stop every reporting worker before running
    it. `--status` flags it.
- **Principals.** Reporting APIs name a principal with
  `PrincipalRef(tenant_id, principal_id)`; `grants_task` and
  `authorizes_delivery` take it. One service serves one tenant.

The rest of this section describes the building blocks the managed path
composes, for sellers who wire them by hand.

**Buyer.** `owed_subscription_periods(...)` is the coverage rule as a pure
function over plain data. Pass it the `get_principal` subscription readback,
the buys you can see (`SubscriptionBuy`, with earlier flights when a buy was
extended or reinstated), the offerings with the seller's dimension mapping
(`SubscriptionOffering`), the account timezone, the account's grant history,
and the `subscription_coverage[]` exceptions and gaps. It returns
`OwedSubscriptionPeriod` values, which are `ExpectedReportingPeriod`s, so
they feed `evaluate_reporting_ledger` and the consumer-status planner as they
are.

`ReportingSubscriptionTracker` keeps those records for you. Register through
`tracker.sync(...)` so every version's configuration is kept (the readback
keeps only the current one), record what `list_accounts` shows with
`tracker.observe_account(...)`, and pass each `principal.changed` payload to
`tracker.handle_notification(...)`; it re-reads `get_principal` and folds
`accounts_unauthorized[]` into the grant history.
`tracker.expected_periods(subscription_id, account_id, buys=..., now=...,
coverage=...)` then returns what the seller owes. A withheld period carries no
`expected_at`, so it is never claimed missing while authority is absent.
`sync_reporting_subscriptions`, `read_reporting_subscriptions` and
`load_subscription_coverage` are the underlying client helpers.

**Seller, advanced composition.** Opt in on the principal runtime and link it to the engine. Without
`reporting_subscriptions=`, `PrincipalService` rejects the section with
`UNSUPPORTED_FEATURE`, as the spec requires for an unsupported section.
`replace_account_configs: true` is also rejected with `UNSUPPORTED_FEATURE`
unless the engine is built with `end_account_configs`.

```python
from adcp.reporting.subscriptions import (
    ReportingSubscriptionEngine,
    ReportingSubscriptionSupport,
    SubscriptionOffering,
)
from adcp.server import PrincipalService

principals = PrincipalService(emit_change=emit_principal_changed)
engine = ReportingSubscriptionEngine(
    principals=principals,
    host=my_host,  # list_authorized_accounts, account_buys[, account_delivery_configs]
    offerings=[
        SubscriptionOffering.from_offering(daily_offering),  # reads standard_dimensions
    ],
    support=ReportingSubscriptionSupport(max_accounts=500),
)
principals.set_reporting_subscriptions(
    engine.principal_options(delivery_config_ids=caller_config_ids)
)
```

The host supplies only what it alone knows: the accounts a principal may read
(`list_authorized_accounts`, paged), each with its IANA `timezone` and the
`authorized_at` that `list_accounts` serves (the engine never substitutes
either); each account's buys with their products'
`reporting_delivery_offering_ids` (`account_buys`); and optionally the caller's
own active `sync_accounts` configurations (`account_delivery_configs`), which
take precedence. Give each configuration its `active_from` so the `overridden`
exception opens at that instant; otherwise run `engine.reconcile(principal)`
synchronously with every `sync_accounts` write. Call `engine.reconcile` on a
schedule and after anything changes, and `engine.authorization_changed(...)`
when you learn of a grant or revocation directly. A subscription that cannot
be evaluated (for example an account with no timezone yet) is isolated and
listed in `engine.evaluation_errors`; the others carry on. `subscription_coverage`
answers from the engine's last-reconciled authority, so re-check authority
before serving it. Use `render_path_template(...)` to expand a destination
`path_template`, and `PrincipalService.reporting_subscription_ids(...)` to make
`sync_accounts` reject a `delivery_config_id` that equals a subscription id. The engine records exceptions as cutover
intervals, keeps `accounts_with_exceptions[]` and `accounts_unauthorized[]`
current in `get_principal` (each change fires `principal.changed` with reason
`reporting_subscription_changed`), serves `subscription_coverage(...)` for
`get_reporting_status`, and computes `owed_periods(...)` with the same function
the buyer runs.

Under `max_accounts`, accounts already served keep their slots and the rest are
filled in `account_id` byte order; the remainder get a `capacity` exception.

**Engine state.** The engine keeps each principal's state (grants and losses,
timezone changes, every version's configuration and destination snapshot,
`max_accounts` slots, exceptions and withheld counts) as one JSON document in a
`ReportingSubscriptionStateStore`, written by compare-and-swap. On a conflicting
concurrent write it reloads and reapplies the change, and it raises
`ReportingSubscriptionStateConflictError` if the document keeps changing. The
default `InMemoryReportingSubscriptionStore` is lost on restart; pass a durable
`store=` in production, and share it, with the `PrincipalService` record store,
across every worker. Version configurations are never pruned, so
`prior_versions` stay answerable while obligations cite them; exceptions are
dropped `status_retention_days` after they close.

`subscription_capabilities(...)` and `advertise_reporting_subscriptions(...)`
project `configuration_tasks`, the deprecated `configuration_task`, the
`subscriptions` block, the feature id and the principal section.

**Serving subscriptions from the service.** Pass
`reporting_subscriptions=ReportingSubscriptionServiceOptions(...)` to
`ReliableReportingService`. Without it, capabilities, responses and worker
turns are unchanged.

```python
from adcp.reporting.service_subscriptions import ReportingSubscriptionServiceOptions

reporting = ReliableReportingService(
    store=store,
    account_context=resolve_account_context,  # also receives subscription generations
    reporting_subscriptions=ReportingSubscriptionServiceOptions(
        engine=engine,
        list_principals=list_subscribing_principals,
        resolve_principal=resolve_authenticated_principal,
        definitions={"daily_v1": daily_definition},  # content-addressed, per offering
    ),
)
```

What the service does with it:

- **Planning.** Each worker turn first plans every listed principal's frozen
  owed set, then executes those obligations alongside the account
  configurations.
  - A subscription generation is one per (account, principal,
    `subscription_id`, version, resolved `offering_id`). Its `consumer_id` is
    the principal's `subject`.
  - Each generation is installed through the same trusted path as
    `configure()`. `account_context` receives it with
    `configuration.subscription` set and returns the source route, currency
    and advertised offering. The service validates these as it does for an
    account configuration.
  - Obligations are written only for periods ending by
    `min(engine through, turn clock)`. Each carries exactly that period's
    media buys and is never rewritten.
  - A subscription that cannot be planned is recorded in
    `turn.subscription_errors` and reported to `planning_error_handler` (or
    the worker error handler). It never stops another subscription or an
    account configuration.
- **Capabilities.** The service adds `configuration_tasks`, the
  `subscriptions` block, `standard_dimensions` on every offering, the feature
  id and the principal section, at the release each caller is served.
  - Callers served below 3.3 see the pre-3.3 block.
  - Callers served a superseded 3.3 beta see the pre-3.3 block too: the
    subscription surface is served from 3.3.0-beta.3 on
    (`adcp.reporting.subscriptions.serves_subscriptions`).
  - Advertise `adcp.principal` yourself. The service refuses to advertise
    subscriptions without it.
- **Status.** `get_reporting_status` summary and periods views gain
  `subscription_coverage[]`.
- **Visibility.** Ledger reads and `sync_reporting_status` are scoped by the
  caller's `consumer_id`, so a principal sees only its own subscription
  periods. The caller resolver must therefore yield the principal's `subject`;
  the service checks `resolve_principal` against it.

**Grants: access means access.** `AuthorizedAccount.allowed_tasks` carries the
grant's `authorization.allowed_tasks`.

- Coverage continues only while the grant lists `get_reporting_status`; a grant
  reduced below that ends coverage exactly like a revocation.
- For a caller holding subscription generations on the account, the service
  checks each reporting task against the current grant:
  - `get_reporting_status`;
  - `get_media_buy_delivery` exact reads;
  - `sync_reporting_status`;
  - `sync_reporting_receipts`.

  A task the grant does not list fails with `SCOPE_INSUFFICIENT` while the
  grant keeps the account covered (adcp#8619), and with `ACCOUNT_NOT_FOUND`
  once no covering grant is held.
- There is no window after a loss of authority: a revoked account is unknown to
  every task, receipts and statuses included.
- To let a buyer finish reconciliation, keep a reduced grant (for example
  `get_reporting_status`, `sync_reporting_receipts` and `sync_reporting_status`)
  rather than revoking.

AdCP 3.3.0-beta.3 states this rule (adcontextprotocol/adcp#8607); beta.2 still
described a post-revocation window.

**Managed delivery (production).** With `production=` options, subscriptions
are delivered through managed delivery, following adcontextprotocol/adcp-client#3523.
Supply `bind_managed_generation(configuration, version)`.

- **Binding.** The service calls the hook once per (version, offering)
  generation, before that generation's first obligation is written. The hook
  resolves the version's `destination.destination_id` to the owner's current
  destination generation, authorizes it for the account and returns the
  `ReportingDestinationBinding`. Its `path_template` must equal the version
  destination's. A failure writes nothing, and the next turn retries.
- **Constituents.** Each owed obligation freezes the constituents of exactly its
  media buys from the source's `configuration_binding(configuration)`, the
  host's authorized pool for the generation.
  - A media buy outside the pool fails that subscription's planning for the
    turn, and nothing is written.
  - At each dispatch and publication, the frozen constituents must still lie
    within the current pool.
- **Delivery.**
  - Owed obligations are produced and materialized like account obligations,
    whatever the subscription's later state.
  - Objects land under the expanded `path_template`: each delivery's
    `ReportingPreparedRevision.delivery_path`. Destination writers must place a
    subscription's objects under it.
  - Every row carries `account_id`, so the report definition's row schema must
    admit it.
- **Authority.** `engine.authorizes_delivery` is re-checked before preparing,
  before writing and after writing each attempt. A loss holds the delivery: it
  stays pending under the same identity and is retried.
- **Requests.** The mounted production handler checks each task against the
  grant and adds `subscription_coverage`.
- **Stores.** The memory and PostgreSQL production stores both serve managed
  subscription delivery, with or without status notifications. PostgreSQL
  keeps each owed set's enrollment, and each owed obligation's frozen
  constituents, in the private `adcp_reporting_production_owed_sets` and
  `adcp_reporting_production_owed_constituents` tables. Its reconciliation
  records, materializer discovery and status checkpoints record the
  generation's offering. Running `create_schema` with this release lifts
  those tables' account-only checks and widens the discovery key in place.

**Limits.** These are not served yet:

- subscriptions that resolve a receipt-reconciled (`consumer_receipt`) offering;
- more than one offering of one subscription version for the same account and
  owner under production. Status checkpoints hold one configuration scope per
  version, so admitting a second offering fails that subscription's planning
  with `reporting_production_offering_conflict`;
- status notifications for subscription generations outside production: a
  `PgReportingLedgerStore` with them enabled refuses an owed set. It otherwise
  holds owed sets, after the generation-key widening described in
  [ledger migrations](reporting-ledger-migration.md).

Keep the engine's `status_retention_days` equal to the service's.

## Advanced composition

`postgres_reliable_reporting_service()` (from `adcp_reporting_storage`) without
production options remains the Core factory. Advanced
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

Installed Core and managed production reporting use one error policy at service
admission. A created, stopping, closed, or failed service returns
`SERVICE_UNAVAILABLE` with transient recovery. A missing authenticated caller
returns `AUTH_REQUIRED`; explicit buyer configuration validation can raise
`ReliableReportingConfigurationError(message, kind="invalid_request")`.
Composition errors retain `INTERNAL_ERROR` and terminal recovery, with internal
configuration details omitted from buyer responses. Ledger codes are preserved
and receive the SDK's recovery classification for that code. Reporting cursor/
input extensions are correctable. Storage-unavailability extensions are transient,
as are a pending status lifecycle (`STATUS_PROJECTION_UNAVAILABLE`) and retained
history the seller still owes but has not yet materialized or repaired
(`HISTORY_UNAVAILABLE`): the request is valid, and the same retry succeeds once
the seller catches up. A reporting reference that does not resolve for the caller
(`LOOKUP_UNAVAILABLE`) answers the standard, nondisclosing `REFERENCE_NOT_FOUND`
with correctable recovery. Unknown extension or integrity failures default to
terminal. Disabled optional methods and aggregate delivery
continue to delegate to the application.

## Source identity and automatic read timing

Adapters receive the authenticated owner in `request.identity.consumer_id`.
The SDK copies it from the obligation and binds it into the request fingerprint
and sealed manifest; use that identity for any buyer-specific source routing.
It is distinct from `account_id`, because several buyers can share an account.
See the [source adapter contract](reporting-source-adapters.md#authenticated-consumer-identity)
and [retained acquisition recovery](reporting-caller-ownership-migration.md#retained-source-acquisitions).

Automatic workers wait for the actual source offering's availability, then spread
each period's first read over a deterministic window of up to five minutes by default.
The offset includes the account, consumer, configuration generation, period and
offering. It is bounded by delivery/recovery headroom and one tenth of the period
and applicable restatement cadence. A short period or no available headroom can
reduce the offset to zero.

Successful snapshots persist their checked-at plus cadence schedule; restarting
the service does not add the offset again or accumulate drift. Existing retry
deadlines, explicit acquisitions and official-close boundaries retain their
timing. A successful manual snapshot also bypasses the initial automatic offset
when its official close becomes due. Every automatic read still waits for its actual
offering to be ready.

For Core service composition, set `read_jitter_window=timedelta(...)` on
`ReliableReportingService.memory(...)`, `postgres_reliable_reporting_service(...)`,
or the constructor.
For managed production, set the same option on `ReportingProductionOptions`.
Direct `ReportingProducer` users can set it on the producer. A nonnegative
duration is required; `timedelta(0)` opts out. Managed production owns this
option in `ReportingProductionOptions`, so do not also pass the Core option.

An existing Core `producer_factory` keeps its previous keyword signature when
`read_jitter_window` is omitted. When explicitly configuring that option, the
custom factory must accept and forward the `read_jitter_window` keyword.
