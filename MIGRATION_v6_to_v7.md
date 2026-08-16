# Migrating from Python SDK 6 to 7

This guide applies to applications upgrading from any Python SDK 6.x release
to 7.x. SDK 7 makes canonical creatives the primary application contract,
updates the bundled protocol schemas to AdCP 3.1.15, and tightens several
security and concurrency boundaries.

The SDK package version and negotiated AdCP protocol version are independent.
Installing `adcp==7.0.0` does not force peers to use a particular wire version;
SDK 7 continues to interoperate with AdCP 3.0 and 3.1 agents.

## Upgrade checklist

1. Install SDK 7 in a branch and run your test suite with deprecation warnings
   visible:

   ```bash
   pip install --upgrade "adcp>=7,<8"
   python -W default::DeprecationWarning -m pytest
   ```

2. Replace legacy creative identity in normal application code with canonical
   `Format` declarations and `format_options`.
3. Move any intentionally legacy creative calls and types to the explicit
   `Legacy*` and `*_legacy` APIs.
4. Add an authorization callback to every `create_roster_account_store` call.
5. If you provide a custom decisioning executor, set
   `timed_sync_get_products_limit`.
6. If you use `PgBackend`, create a separate connection pool for advisory-lock
   transactions and pass it as `lock_pool`.
7. Confirm that synchronous callers consume terminal results inline instead of
   waiting for a duplicate completion webhook.
8. Exercise callback validation and multi-tenant isolation in staging before
   production rollout.
9. Configure `webhook_secret` on every public MCP callback receiver. If an
   endpoint is isolated from untrusted networks and must temporarily accept
   unsigned callbacks, opt in explicitly with
   `allow_unauthenticated_webhooks=True`.

## Canonical creatives are the primary API

Use `Format`, `Product.format_options`, `format_kind`, and
`format_option_refs` in normal application code. Products, packages,
creatives, filters, delivery reads, callbacks, generic task execution,
multi-agent clients, server handlers, response builders, and asset helpers all
enforce the canonical boundary.

The main renames for code that still needs named-format compatibility are:

| SDK 6 surface | SDK 7 compatibility surface |
|---|---|
| `FormatId` | `LegacyFormatId` |
| `BuildCreativeRequest` | `LegacyBuildCreativeRequest` |
| `PreviewCreativeRequest` | `LegacyPreviewCreativeRequest` |
| `ListCreativeFormatsRequest` | `LegacyListCreativeFormatsRequest` |
| `client.get_products(...)` for raw legacy rows | `client.get_products_legacy(...)` |
| `client.build_creative(...)` | `client.build_creative_legacy(...)` |
| `client.preview_creative(...)` | `client.preview_creative_legacy(...)` |
| `client.list_creative_formats(...)` | `client.list_creative_formats_legacy(...)` |
| legacy server handler names | handler names ending in `_legacy` |

`Product` and creative filters no longer expose `format_ids`. Use canonical
`format_options`, `format_kind`, and `format_option_refs` instead. Do not import
from `adcp.types._generated` or `adcp.types.generated_poc`; those modules are
internal and regenerated from the protocol schemas.

If a workflow must continue using the legacy wire shape during migration, make
that boundary explicit:

```python
from adcp.types.legacy import LegacyGetProductsRequest

raw = await client.get_products_legacy(LegacyGetProductsRequest(...))
```

Legacy methods emit `DeprecationWarning` and are scheduled for removal with
AdCP 4.0. Treat them as a temporary interoperability layer rather than the
default API for new code.

### Converting between legacy and canonical formats

For seller-owned named formats, configure `legacy_format_converter`. For
canonical selections persisted across a JSON or process boundary, configure a
separate `canonical_format_legacy_resolver`; the SDK deliberately does not
reverse-guess a legacy tuple.

Catalog snapshots can build both adapters:

```python
from adcp.canonical_formats import projection_adapters_from_catalog_snapshots

adapters = projection_adapters_from_catalog_snapshots(snapshots)
client = ADCPClient(
    agent,
    legacy_format_converter=adapters.legacy_format_converter,
    canonical_format_legacy_resolver=adapters.canonical_format_legacy_resolver,
)
```

AdCP 3.0 payloads are upgraded on reads and downgraded on writes. For AdCP 3.1,
the framework advertises `media_buy.features.canonical_creatives: true` for
canonical-capable sellers. If you build capability responses yourself, include
that feature or provide unambiguous request-local canonical evidence.

## Roster account stores require authorization

`create_roster_account_store` no longer treats possession of an account ID as
authorization. Every store must receive a synchronous or asynchronous callback
that binds the verified `AuthInfo` principal to the candidate account.

```python
from adcp.decisioning import create_roster_account_store

async def authorize(account, auth_info):
    return await access_policy.can_access(
        principal=auth_info,
        account_id=account.id,
    )

accounts = create_roster_account_store(
    roster=roster,
    authorize=authorize,
)
```

Return exactly `True` to allow access. Missing authentication, `False`, or an
exception denies access. Avoid a blanket allow callback outside isolated test
fixtures; it defeats the security boundary this change introduces.

## Custom executors require an admission limit

When a decisioning server receives a custom `executor=`, it cannot safely infer
the executor's capacity. SDK 7 therefore requires an explicit positive
`timed_sync_get_products_limit`:

```python
from concurrent.futures import ThreadPoolExecutor
from adcp.decisioning import serve

executor = ThreadPoolExecutor(max_workers=16)
serve(
    platform,
    executor=executor,
    timed_sync_get_products_limit=8,
)
```

Choose a value that leaves capacity for other tools. If the SDK creates the
pool through `thread_pool_size=`, the admission limit defaults to half the
worker count, with a minimum of one, so no change is required.

The caller still owns the lifecycle of a custom executor and must shut it down
cleanly.

## PostgreSQL idempotency requires a distinct lock pool

`PgBackend` now requires both `pool` and `lock_pool`. They must be different
pool objects: the lock pool holds advisory-lock transactions while adopter code
runs, and sharing it with business or cache queries can deadlock under
saturation.

```python
from psycopg_pool import AsyncConnectionPool
from adcp.server.idempotency import IdempotencyStore, PgBackend

pool = AsyncConnectionPool(database_url, min_size=2, max_size=10)
lock_pool = AsyncConnectionPool(database_url, min_size=2, max_size=10)

backend = PgBackend(pool=pool, lock_pool=lock_pool)
await backend.create_schema()
store = IdempotencyStore(backend=backend, ttl_seconds=86_400)
```

Open and close both caller-owned pools with the application lifecycle. The
same requirement applies when constructing `PgBackend` through `LazyBackend`.
Passing the same object for both arguments fails at construction.

## Synchronous completion webhooks default to off

`auto_emit_completion_webhooks` now defaults to `False`. AdCP forbids a task
webhook when the initial response is already terminal: the result is available
inline, and no registry task exists for a webhook `task_id`.

If an existing buyer temporarily depends on receiving both copies, pass
`auto_emit_completion_webhooks=True` to `serve()` or
`create_adcp_server_from_platform()`. This preserves the SDK 6 behavior as a
non-conformant compatibility extension with a synthetic, unpollable `sync-*`
task ID. Update the buyer to consume the inline result, then remove the opt-in.

This setting controls only synthetic synchronous-completion delivery. Terminal
webhooks for real `TaskHandoff` requests remain enabled when the request
supplies `push_notification_config` and a webhook sender or supervisor is
configured. Adopters that deliver terminal task webhooks themselves can set
the independent `auto_emit_task_webhooks=False` ownership flag.

## Callback and tenant boundaries are stricter

SDK 7 validates and canonicalizes A2A callback destinations with a fail-closed
default policy. Deployments that accept dynamic callback destinations or use
DNS pinning must provide a custom sender and enforce resolution at connection
time. A push-configured handoff with no available delivery transport is now
rejected before task creation instead of being accepted and silently dropped.

`ADCPClient.handle_webhook()` also fails closed for unsigned MCP callbacks when
the client has no `webhook_secret`. The previous fail-open behavior is
available only through the explicit `allow_unauthenticated_webhooks=True`
compatibility escape. Do not enable that option on an Internet-reachable
receiver.

Account registries, sessions, proposals, notification stores, and reference
seller state now enforce tenant ownership. Test fixtures or application code
that relied on a cross-tenant fallback must be updated to carry the authenticated
tenant/account scope explicitly. Notification credentials are no longer
returned through typed or generic response paths.

## Recommended rollout

1. Deploy SDK 7 to staging with the legacy creative adapters only where they
   are still required.
2. Run buyer and seller storyboards for every supported wire version.
3. Test two separate tenants using the same external identifiers and verify
   that neither can read the other's accounts, sessions, proposals, or tasks.
4. Exercise allowed and denied callback destinations, including redirects and
   DNS changes if your sender supports them.
5. Load-test timed synchronous `get_products` calls and PostgreSQL idempotency
   under pool saturation.
6. Remove temporary legacy adapters and
   `auto_emit_completion_webhooks=True` after all callers have migrated.

See the [7.0.0 release](https://github.com/adcontextprotocol/adcp-client-python/releases/tag/v7.0.0)
and [full changelog](CHANGELOG.md) for the complete change list.
