# Atomic PostgreSQL idempotency

`@IdempotencyStore.wrap` keeps its existing backend contract: it locks the key,
executes the handler, and writes the replay afterward. Business writes on another
connection remain a separate transaction. Keep independent business deduplication
when using that decorator; `raise_on_persist_error` does not undo business commits.

For business SQL that must commit atomically with its replay, use the explicit
`IdempotencyStore.reserve` API with a direct `PgBackend`:

```python
from typing import Any
from adcp.server.idempotency import IdempotencyStore

async def create_media_buy(store: IdempotencyStore, params: Any, context: Any) -> dict[str, Any]:
    async with store.reserve(params, context, operation="create_media_buy") as slot:
        if slot.replayed:
            response = slot.response
            assert response is not None
            return response
        cursor = await slot.connection.execute(
            "INSERT INTO media_buys (buyer_key) VALUES (%s) RETURNING id",
            (params["idempotency_key"],),
        )
        row = await cursor.fetchone()
        assert row is not None
        response = {"media_buy_id": str(row[0])}
        await slot.record(response)
    return response  # business and replay are committed before returning
```

Scope and hash match the decorator: the authenticated caller (plus tenant when
present), the key, and the store's canonical hash function. Missing keys or missing
identity fail before business execution. Authorization checks still belong before
reservation lookup. Reservations neither register a handler with `is_wrapped` nor
change capability advertisement. A platform using explicit reservations instead
of the decorator can use the existing `_adcp_idempotency_external = True` wiring
opt-out and declare its idempotency capability accurately. Do not combine `reserve`
with `@store.wrap`; nesting in an active `hold` is rejected.

## Ordering proof

The implementation nests the contexts in this order:

```text
backend.hold(scope, key):                 # reserve/lock on lock_pool
    business_connection:
        business_connection.transaction():
            lookup on business_connection
            business writes on slot.connection
            slot.record(response)        # replay write on business_connection
            verify response still exists
        business COMMIT completes        # or rollback/failed commit
    return/restore business connection
release advisory lock                    # lock transaction exits last
```

Every cooperating process uses the same transaction advisory lock as the existing
decorator. A retry cannot perform its lookup before the previous business commit
completes. If that commit succeeds, both business and replay become visible; if it
rolls back, neither is visible. A process dying before commit loses both writes.
Dying after commit but before lock release leaves the replay durable, and PostgreSQL
releases the dead session's lock so a retry replays. No placeholder reservation row
is needed: an unrecorded or crashed reservation leaves no durable claim.

## Transaction ownership and failure semantics

- By default the reservation borrows a business connection *after* acquiring the
  lock, opens a top-level transaction, and commits it on successful context exit.
  A separate caller-owned lock pool remains required, as for `PgBackend` today.
- `PgBackend.reserve(scope, key, payload_hash, ...)` is the lower-level API for
  adopters that already compose authenticated scope and canonical hash. Both APIs
  accept `connection=` for a real, idle `psycopg.AsyncConnection` using the same
  PostgreSQL database and resolving the same replay table as the lock pool.
  Connections resolving a different schema table or a temporary table shadow
  are rejected before business code runs. The caller keeps ownership of that
  connection; the reservation owns its transaction. Different database/server
  fingerprints and non-psycopg objects are rejected. The replay table is pinned
  through commit: changing its resolution with `SET LOCAL search_path` before
  or after `record()` aborts the business transaction.
- **An already active outer transaction is rejected**, including an implicit
  transaction opened by earlier SQL. This API cannot safely release its lock while
  some caller-owned outer transaction will commit after handler return. Commit or
  roll back that transaction first, then enter a reservation. Pool factories must
  yield idle connections. The reservation sets READ COMMITTED isolation so the
  lookup uses a fresh snapshot after lock acquisition.
- Completed nested business savepoints are supported. If a nested savepoint rolls
  back a replay record, the final verification rejects the operation and rolls
  back the entire business transaction. Do not issue raw transaction-control SQL,
  commit/rollback manually, or keep a savepoint open beyond reservation exit.
- On a miss, `record()` must succeed once. Normal exit without a record rolls back
  business writes and raises `IdempotencyReservationError`. The `recorded` property
  means a record was written, not that commit has completed. Use context exit as
  the success boundary. A response must contain JSON-safe values.
- Handler exceptions, explicit `psycopg.Rollback`, cancellation during execution,
  serialization failure, and persistence failure roll back the transaction.
  A caught persistence failure still poisons the reservation, preventing success.
  Failed commits propagate; retry the same key to resolve an uncertain commit
  acknowledgement. Business/replay atomicity holds even if that acknowledgement
  is lost. Exceptions are not converted into a successful unrecorded response.
- On a hit, `response` is an independent copy with `replayed: true`, the transaction
  is read-only, and `slot.connection`/`record()` reject business execution.
  Reservations are bound to the owning task and close at context exit; child tasks
  may not record or obtain the business connection through the slot.

This guarantee covers SQL on the reservation's business transaction. External
ad-server calls, email, and writes through unrelated database connections require
independent deduplication or a transactional outbox. Caller code must cooperate
with the context's transaction ownership.

## TTL and migration

Replay expiry uses `clock_timestamp()` on the business PostgreSQL server when
`record()` executes; application clocks cannot shorten or extend the replay window.
The existing one-hour to seven-day bounds apply. A long transaction consumes part
of its TTL before commit; keep it shorter than the replay window. Schedule the
existing `delete_expired()` sweep as usual.

**No schema migration is required.** The reservation uses the existing replay table,
JSONB response, TIMESTAMPTZ expiry, ASCII/C-collated scoped primary key and conditional
first-writer-wins insert. Existing decorator/backend subclasses and `LazyBackend`
remain unchanged. Lazy backends and non-PostgreSQL backends do not expose transactional
reservations; construct a direct `PgBackend` when opting into this API.

## Acceptance evidence

`tests/conformance/decisioning/test_pg_idempotency_reservation.py` uses real
PostgreSQL and independent spawned processes with their own pools. It checks
concurrent retry blocking using `pg_locks`, SIGKILL before and after actual business
commit, normal commit/replay, rollback, unrecorded exit, serialization/write/commit
failure, cancellation, authenticated tenant scoping, database TTL, outer transaction
rejection, nested savepoints and task ownership. Run with a private database:

```bash
ADCP_PG_TEST_URL=postgresql://.../private_idempotency_test \
  python scripts/reporting_test_harness.py pytest \
  tests/conformance/decisioning/test_pg_idempotency_reservation.py -v -ra
mypy --strict tests/type_checks/idempotency_reservations.py
```
