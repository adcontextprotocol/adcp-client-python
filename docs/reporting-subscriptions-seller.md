# Serving reporting subscriptions (seller)

AdCP 3.3 reporting subscriptions (`media_buy.reporting_subscriptions`, experimental) let a buyer register reporting once on its principal through `sync_principal`, instead of once per account through `sync_accounts`. A subscription follows the buyer's grants: when you grant an account, it is covered; when you revoke it, coverage stops.

This guide covers the managed path, where the SDK derives everything from surfaces you already serve. The [reliable reporting service guide](reliable-reporting-service.md#reporting-subscriptions-adcp-33-experimental) documents the building blocks underneath.

```python
from adcp.reporting import ReliableReportingService, SubscriptionOptions
from adcp.storage.postgres import postgres_storage

reporting = ReliableReportingService(
    storage=postgres_storage(dsn=os.environ["DATABASE_URL"]),
    account_context=resolve_account_context,
    subscriptions=SubscriptionOptions(max_accounts=500),
)
reporting.sources.register("gam", gam_source, capability_offerings=[daily_offering])
platform = reporting.install(MySeller())
```

## Prerequisites

The SDK reads these surfaces under each principal's identity. A principal sees exactly what it could see through `get_media_buys`.

| Input | Read from | Must be present | Override |
|---|---|---|---|
| Authorized accounts | `list_accounts` (a decisioning platform's `accounts.list`) | `account_id`, `timezone` (IANA), `operator`, `authorization.allowed_tasks` | `SubscriptionOptions.accounts` |
| Visible buys | `get_media_buys` | `confirmed_at`, `start_time`, `end_time`, `cancellation.canceled_at`, `cancellation_effective_at`, `completed_at`, `flight_history`, `packages[].package_id` | `SubscriptionOptions.buys` |
| Package offerings | `packages[].reporting_delivery_offering_ids` (fixed at confirmation), else every registered offering | | `SubscriptionOptions.buy_offerings` |
| Offerings | `capability_offerings` on each registered source | `standard_dimensions` on every offering; `available_from` / `withdrawn_at` when they apply; `account_id` in `reporting_profile.primary_keys` on every offering delivered to destinations (`method`), required by 3.3.0-beta.3 and logged as `reporting.subscription.offering_primary_keys` when missing | none |
| Report content | `account_context`, called with `configuration.subscription` set | | `SubscriptionOptions.configure` |

A decisioning `Account` carries `timezone=` and `operator=`. An account without a timezone is refused rather than read as UTC.

### Capabilities and serving

The service fills in what it serves and leaves the rest to you:
- **What the service adds.**
  - The `media_buy.reporting_delivery` block.
  - The `adcp.principal` block, with `optimistic_concurrency: true` and `reporting_subscriptions` in `supported_sections`.
  - `adcp.supported_versions`, when your capabilities do not state it. Without it a buyer negotiates down from `major_versions` and never reaches 3.3.
- **What your capabilities must carry.**
  - `adcp.idempotency`, which says whether you honor `idempotency_key` replay. The schema requires it, and only you know it.
  - To accept `reporting_destinations`, `adcp.principal.max_reporting_destinations`, `reporting_destination_offerings` and `suspension_interval_seconds`. Without all three, that section is not advertised.
- **Serving over MCP.** Pass `serve(..., context_factory=auth_context_factory)` (or your own factory), so `list_accounts` and `get_media_buys` see the authenticated `caller_identity`. On the managed path, a reporting task with no resolved account reads the account the request names. The subscription grant check then refuses an account the caller holds no grant on, so no `caller_resolver` is needed for `get_reporting_status` over plain MCP.

The reference seller (`python -m adcp reference-seller reporting`) is a complete example.

## AdCP 3.3.0-beta.3 rules the engine applies

- **Admission under `max_accounts`.** Candidates are ordered by `authorization.authorized_at`, then `account_id` in byte order, and an admitted account keeps its slot. A waiting candidate carries a `capacity` exception.
- **Several feeds per purpose.** A caller may hold several pacing or analytics subscriptions for one account. Where two resolve the same offering for a package, the lowest `subscription_id` owes it and the other records an `overridden` exception naming the `package_ids`. Billing stays one per account at a scope level.
- **Membership in place.** Membership edits (`account_ids`, `exclude_account_ids`) keep the version. A removed account stays listed, `excluded`, with an `excluded_by_buyer` entry from the cutover. An added account gets a closed entry covering the time before it joined.
- **`destination_overrides`.** An account named by an override, by `account_ids` or `operator_domains`, resolves against the override's destination method. Its owed sets carry the override as the version's `destination`, so `bind_managed_generation` binds and checks the override's `path_template`.
- **Bounded readback.** `prior_versions` lists the 16 newest inline, and the account lists 1,000, each with a total count. `get_principal` `reporting_subscription_page` pages the rest from the engine's state.
- **Partial grants.** A grant that keeps the account covered refuses a task it does not list with `SCOPE_INSUFFICIENT`.
- **Billing stubs and `regranted_at`.** A billing cutover owes a stub obligation (`stub: true`), and `list_accounts` serves `authorization.regranted_at`.

## What the SDK derives

- **The principal runtime.** `install()` serves `get_principal` and `sync_principal` and advertises the principal section. The service's `PrincipalService` is `reporting.principals`.
- **Capabilities.** `configuration_tasks`, the `subscriptions` block, `standard_dimensions` on every offering, and the feature id, at the release each caller is served.
- **Scheduling.**
  - `sync_principal`, `sync_accounts`, `create_media_buy` and `update_media_buy` queue a reconcile that runs after the response.
  - Each worker turn reconciles every principal a period boundary has passed for, then plans obligations.
  - Call `await reporting.touch(account_id=...)` after a change the SDK did not serve.
- **Grants.** The grant journal (see [Grants and wind-down](#grants-and-wind-down)).

## Overrides

Each `SubscriptionOptions` field replaces one derivation:
- `accounts(principal, *, cursor)` and `buys(principal, account_id)` replace the roster and the buys;
- `buy_offerings(media_buy)` and `configure(generation)` replace package offerings and report content;
- `account_delivery_configs(principal, account_id)` and `end_account_configs(...)` enable precedence and `replace_account_configs`;
- `is_period_delivered(principal, account_id, period, *, by)` gives exact withheld counts;
- `resolve_principal(request, context)` replaces principal resolution.

If your account access depends on a live upstream token, supply `accounts`. A listing that fails under the service context raises `SUBSCRIPTION_ROSTER_UNAVAILABLE`, and is never read as an empty roster.

## Bring your own producer and ledger

Not yet on the managed path in Python. Use the advanced composition: build `ReportingSubscriptionEngine` yourself and pass `reporting_subscriptions=ReportingSubscriptionServiceOptions(...)`. The bring-your-own-producer option (`producer`) arrives in a later beta.

## Grants and wind-down

The SDK is the only source of `authorization.authorized_at`.
- **Chain rule.** A re-grant before `unauthorized_at + status_retention_days` continues the chain, so `authorized_at` does not move. A later re-grant starts a new chain.
- **Projection.** Served `list_accounts` and `sync_accounts` carry the journal's value, which is the value coverage uses. A host value that moves backwards is rejected (`authorized_at_regressed`).
- **Push.** `await reporting.grants.changed(principal, account_id, at=..., authorized=..., allowed_tasks=...)` records a change the moment your entitlement service knows it.

`authorized_at` is when the *current authorization chain* began, not when access was last granted. Never set it to "now" on a re-grant, and never move it earlier.

**Wind down, don't revoke.** Revocation means no access at all, receipts and statuses included. To end a relationship while the buyer finishes reconciling:

```python
from adcp.server import wind_down_grant

account.authorization = wind_down_grant(account.authorization)
```

## Storage and migrations

Pass one storage object: `memory_storage()` in development, `postgres_storage(dsn=... | pool=...)` in production. Process-local storage is refused outside development (`REPORTING_STORAGE_NOT_DURABLE`).

Apply schema changes with `python -m adcp migrate [--status | --dry-run]` before starting a release. Startup refuses a schema that is behind (`REPORTING_SCHEMA_BEHIND`). `0002 reporting_subscription_generations` is a stopped-worker upgrade: stop every reporting worker first.

## Workers and scaling

Run `reporting.run_worker()` on a schedule, or `start()` with `worker_interval`. Workers share the stores; engine state is written by compare-and-swap, so several workers agree. Each platform read is bounded by `SubscriptionOptions.read_timeout`.

## Telemetry

Today: `planning_error_handler`, `turn.subscription_errors`, the engine's `evaluation_errors` and `authorized_at_regressions`, and structured log events. OpenTelemetry metrics and `reporting.health()` arrive in a later beta.

## Troubleshooting with explain

`reporting.explain_coverage(...)` and the shared `CoverageTrace` arrive in a later beta. Until then, `engine.owed_periods(principal, subscription_id, account_id)` lists the owed periods. The coverage reason codes are in [Coverage, explained](reporting-subscriptions-coverage.md#reason-codes).
