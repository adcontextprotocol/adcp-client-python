# Best-effort task webhooks with PostgreSQL

`PgWebhookDeliverySupervisor` is a retry queue for task webhooks you send
explicitly. It shares circuit-breaker state across processes and keeps queued
jobs through a restart. It does not keep delivery proofs for the AdCP 3.2 retry
horizon, and it does not commit task state in the same transaction as the
queue, so it cannot back SDK-managed TaskHandoff push. For that, pair
`PgTaskWebhookOutbox` with `PgTaskRegistry`. For non-task notifications, use
[`PgNotificationOutbox`](notification-outbox.md).

Install `adcp[pg]`. `send_mcp` only enqueues a job; `run_worker` sends it, so
start the worker or nothing is delivered.

```python
import asyncio

from psycopg_pool import AsyncConnectionPool

from adcp.decisioning import serve
from adcp.webhook_supervisor_pg import PgWebhookDeliverySupervisor

pool = AsyncConnectionPool("postgresql://...", min_size=4, max_size=20, open=False)
await pool.open()
supervisor = PgWebhookDeliverySupervisor(pool, sender)
await supervisor.create_schema()  # idempotent; also migrates older tables
asyncio.create_task(supervisor.run_worker())

serve(my_handler, webhook_supervisor=supervisor, auto_emit_task_webhooks=False)
```

## Queue columns

Each row in `adcp_webhook_delivery_queue` keeps two separate statuses:

| Column | Holds | Values |
| --- | --- | --- |
| `status_str` | The task status the payload carries | `submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`, `rejected`, `auth-required`, `unknown` |
| `delivery_state` | Whether the worker should claim the row | `pending`, `retry` |

Workers claim rows with `FOR UPDATE SKIP LOCKED`, so each job goes to one
worker at a time. A successful or finally failed job is deleted from the
queue. Each attempt is recorded in `adcp_webhook_delivery_log`. A retry after an
HTTP failure replays the first attempt's bytes. A retry after a transport error
has no bytes to replay, so it rebuilds the payload with the same
`idempotency_key`. In both cases the receiver can dedupe.

## Upgrading from 9.2.0 and earlier: stuck rows are delivered

Through 9.2.0, `status_str` held both the task status and the queue state.
`send_mcp` wrote the task status (for example `completed`), but the worker only
claimed rows whose `status_str` was `pending` or `retry`. No task webhook
enqueued through `send_mcp` was ever sent
([#1606](https://github.com/adcontextprotocol/adcp-client-python/issues/1606)).
The bug has been present since at least 9.0.0b4.

From 9.2.1, `create_schema()` migrates the table in place:

```sql
ALTER TABLE adcp_webhook_delivery_queue
    ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending';
CREATE INDEX IF NOT EXISTS adcp_webhook_delivery_queue_delivery_idx
    ON adcp_webhook_delivery_queue (delivery_state, scheduled_at)
    WHERE delivery_state IN ('pending', 'retry');
DROP INDEX IF EXISTS adcp_webhook_delivery_queue_work_idx;
```

**Rows already stuck in the queue are delivered automatically.** The new
column's default marks every existing row `pending`, and the next worker poll
sends it with the task status it was enqueued with. Rows go out oldest first.
No worker ever claimed these rows, so none has been sent before, and each is
delivered once. On PostgreSQL 11 and later, adding a column with a constant
default does not rewrite the table.

Rolling deploys are safe in both directions. A 9.2.0 process that enqueues
during the rollout omits `delivery_state`, so its rows take the default and new
workers deliver them. A 9.2.0 worker still claims on `status_str`, so it never
picks up a row written by 9.2.1.

The stuck rows may be old, and the buyer may already have read the task state
by polling. To see what will go out, run this before upgrading:

```sql
SELECT id, task_id, task_type, status_str, created_at
FROM adcp_webhook_delivery_queue
WHERE status_str NOT IN ('pending', 'retry')
ORDER BY created_at;
```

To discard some or all of those webhooks instead, delete the rows before the
first 9.2.1 process calls `create_schema()`. If you manage the schema with a
migration tool, apply the three statements above in your own migration.
