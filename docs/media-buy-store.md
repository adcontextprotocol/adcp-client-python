# Media buy store: what `get_media_buys` echoes

AdCP 3.2 `get_media_buys` echoes values the seller persisted when the buy was
created. `create_media_buy_store` wires that persistence at the framework
boundary, so a `DecisioningPlatform` doesn't have to persist and echo by hand.

```python
from adcp.decisioning import create_media_buy_store, serve
from adcp.decisioning.pg import PgMediaBuyContextStore

context_store = PgMediaBuyContextStore(pool=pool)
await context_store.create_schema()  # idempotent; safe on every boot

serve(
    platform,
    media_buy_store=create_media_buy_store(
        capabilities=platform.capabilities,
        context_store=context_store,
    ),
)
```

## `context`: on for every seller

`get_media_buys` must echo, unchanged:

- on each media buy (`media_buys[].context`), the `context` of the
  `create_media_buy` request that created it, and
- on each package (`media_buys[].packages[].context`), the `context` of the
  package request that created it (`core/package.json`: "echoed unchanged in
  responses, webhooks, and read surfaces").

The store records both levels when `create_media_buy` completes, whether
inline or after a task handoff. On `get_media_buys` it fills in any media buy
or package whose `context` the platform left out. A `context` the platform
returns itself is never replaced.

Each response package is paired with its request in this order:

1. A `product_id` that only one package request carries.
2. Otherwise, position, when the response lists as many packages as the
   request and no echoed `product_id` contradicts the pairing.
3. Otherwise, a `context` the platform echoed on that response package.

Echo `product_id` on created packages if you reorder them. The stored objects
are the create request's: `update_media_buy` doesn't change them, and packages
added by `update_media_buy` aren't recorded.

Context echo is on by default. The default `InMemoryMediaBuyContextStore` is
process-local, so a restart or another worker loses the echo. Production
sellers pass `PgMediaBuyContextStore`, or any object implementing the
`MediaBuyContextStore` protocol (`save_contexts` / `load_contexts`, sync or
async, scoped by `account_id`, first write wins). Pass `echo_context=False`
if the platform persists and echoes `context` itself.

The SDK has already created the media buy upstream when the store writes, so a
store failure is logged, not raised: the buyer still gets the created buy, and
`get_media_buys` returns it without the persisted `context`. A failed read is
logged the same way.

### PostgreSQL table

`PgMediaBuyContextStore` adds one new table (default `adcp_media_buy_contexts`)
and changes nothing that already exists, so there's no data migration. Create
it in one of three ways:

- Call `create_schema()` at boot. It's idempotent.
- Use `PgMediaBuyContextStore.migration_sql(table_name=...)`, which returns
  `upgrade` / `downgrade` statements for Alembic, dbmate and similar tools.
- Apply `src/adcp/decisioning/pg/media_buy_context_store.sql` (default table
  name only).

The table holds one row per echoed object. Its `context` column is `JSON`, not
`JSONB`, so the stored text round-trips as written: key order, `null` values
and number spelling (`7.0` stays `7.0`). Every query filters on `account_id`.

## `targeting_overlay`: for property-list and collection-list sellers

A seller claiming `property-lists` or `collection-lists` must also echo
persisted `property_list` / `collection_list` references inside
`packages[].targeting_overlay`. You supply that persistence as a
`MediaBuyStore`, the first argument:

```python
create_media_buy_store(
    overlay_store,
    capabilities=platform.capabilities,
    context_store=context_store,
)
```

The wrapper delegates `persist_from_create`, `merge_from_update` and
`backfill` to it only for those specialisms. A seller that claims one of them
without supplying an overlay store gets a `ValueError` at construction. Sellers
that claim neither may omit the overlay store.

## `null` values in buyer `context`

A buyer's `context` is opaque. `ContextObject` (and the opaque `ext` objects)
keep their `null` values on every dump, including the default
`exclude_none=True`. A buyer's `{"a": null, "b": 1}` is echoed as sent, both
by the persisted echo above and by any platform that returns a typed response
carrying the request's `context`. Keys you exclude by name stay excluded.
