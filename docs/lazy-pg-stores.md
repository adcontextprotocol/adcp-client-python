# Lazy PostgreSQL decisioning stores

`adcp.decisioning.pg` exports `LazyProposalStore`, `LazyTaskRegistry`, and
`LazyTaskWebhookOutbox`. Each accepts a synchronous or asynchronous zero-argument
factory returning its original concrete PostgreSQL store. Task factories may
also return an original `(PgTaskRegistry, PgTaskWebhookOutbox)` pair.

Factories are called on the serving event loop, on the first async operation or
explicit `await wrapper.resolve()`. Concurrent calls share one successful
resolution. Failed or canceled initialization is not cached, so later calls
retry. Factories own pool opening and cleanup on failed initialization; wrappers
never open, close, replace or reopen their caller-owned pools. Schema setup stays
explicit: do it in the factory or call the wrapper's `create_schema()`.

```python
from psycopg_pool import AsyncConnectionPool
from adcp.decisioning.pg import (
    LazyTaskRegistry, LazyTaskWebhookOutbox, PgTaskRegistry, PgTaskWebhookOutbox,
)

pool = AsyncConnectionPool("postgresql://localhost/seller", open=False)

async def task_stores() -> tuple[PgTaskRegistry, PgTaskWebhookOutbox]:
    await pool.open()
    outbox = PgTaskWebhookOutbox(
        pool=pool, sender=signed_sender, encryption_key=encryption_key,
        delivery_retry_horizon_seconds=86400,
    )
    registry = PgTaskRegistry(pool=pool, task_webhook_outbox=outbox)
    await registry.create_schema()
    await outbox.create_schema()
    return registry, outbox

registry = LazyTaskRegistry(task_stores)
worker_outbox = LazyTaskWebhookOutbox.from_registry(registry)
# await worker_outbox.run_worker() and registry.issue(...) share the same pair.
# Cancel/join workers before closing pool during application shutdown.
```

Construct the original registry and outbox together, passing the same pool to
both. Their existing constructor checks (including signing-scope wiring) remain
authoritative, and a returned pair is checked again for identity. The worker
facade from `from_registry()` resolves the same original outbox, rather than
running an independent pool factory. Do not pass an unresolved worker facade to
the concrete `PgTaskRegistry` constructor.

The wrappers declare the concrete stores' durability before resolution and
reject a factory returning a lossy store. `LazyTaskRegistry` retains the concrete
SDK registry's optional listing protocol; no arbitrary delegate attributes or
optional APIs are forwarded. Its additive observer methods can be registered or
removed before and after resolution. They receive the first submitted event and
retain the concrete registry's commit/no-op/failure semantics.

`resolved` inspects the cached store without running the factory. A task registry
exposes its original `task_webhook_outbox` and atomic-outbox marker after
resolution. Servers advertising SDK task-webhook signing must await resolution
before synchronous server construction: the boot validator needs the actual
sender, retry horizon and shared-pool proof. Resolved exact SDK wrappers are
accepted by that proof; unaudited subclasses remain rejected. Polling-only
servers can resolve on first use. Outbox synchronous registration/crypto helpers
and retry-horizon inspection also require explicit resolution; they raise a
clear error when called too early.

After success, a wrapper is bound to the event loop that resolved it. A different
loop raises an error, even if the previous loop has closed; create a fresh wrapper
and pool for the new loop. A failed initialization with no cached store may retry
on a new loop after the old loop closes. Closing the borrowed pool causes normal
pool errors on later operations, rather than rerunning the factory.

No database migration or change to the existing `LazyBackend` is required. The
new wrappers reuse the concrete stores' current SQL and schema assets.
