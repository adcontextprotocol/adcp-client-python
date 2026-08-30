# Production seller path

This is the shortest route from a working AdCP seller to a durable,
multi-tenant deployment. The runnable implementation is
[`examples/v3_reference_seller`](../examples/v3_reference_seller/README.md);
this page explains which SDK layer to choose and who owns each lifecycle
transition.

## Choose the server abstraction

| Start with | Use it when | Move up when |
|---|---|---|
| `adcp_server()` decorators | You need a small stateless agent and prefer functions | You need custom inheritance or reusable handler behavior |
| `ADCPHandler` | You want direct control of AdCP request and response handlers | You need account resolution, specialism validation, upstream routing, or framework-managed tasks |
| `DecisioningPlatform` | You are building an operational seller with accounts, capabilities, async work, and one upstream | Keep it; add a `PlatformRouter` when routing differs by tenant or platform |
| `PlatformRouter` | One process serves multiple tenants or decisioning backends | This is the top-level composition layer |

The production wiring reference uses `DecisioningPlatform`. Its modules deliberately
separate transport wiring, tenant identity, business translation, persistence,
and webhook delivery so adopters can replace one boundary at a time.

## Runtime ownership

```text
buyer request
    │
    ▼
auth → tenant router → buyer registry → idempotency lock
    │
    ▼
DecisioningPlatform method → upstream ad server
    │
    ├─ terminal result ───────────────► return inline and cache result
    │
    └─ TaskHandoff / WorkflowHandoff ─► persist submitted task
                                             │
                          complete/fail ─────┤ one PostgreSQL transaction
                                             ▼
                                      task + outbox row
                                             │
                                      separate worker
                                             ▼
                                   signed webhook retry
```

The web process owns request validation and the atomic task/outbox commit. The
worker owns network delivery and retry. Both processes use the same PostgreSQL
database, 32-byte encryption key, signing key, and advertised retry horizon.
They construct separate pools and `WebhookSender` instances.

The bundled mock upstream completes its approval path inline, so it does not
pretend that a process-local poll loop is restart-safe. A production adapter
whose upstream approval can outlive the request must return
`WorkflowHandoff`, persist the framework-issued task id in its own durable
queue, and have that queue's consumer call `registry.complete()` or
`registry.fail()`. The PostgreSQL registry makes task state durable; it does
not make arbitrary in-process work durable. The reference includes a leased
PostgreSQL queue and restart-recovery test; adopters still provide the
business-specific approval handler.

The reference's `IdempotencyStore` wrapping is intentionally paired with its
inline terminal responses. Do not put the same method-level wrapper around a
method that returns a raw `TaskHandoff` or `WorkflowHandoff`: the wrapper runs
before framework task issuance and therefore cannot cache the projected
`{status: "submitted", task_id}` envelope. A durable workflow adapter must not
advertise method-level idempotency for that method unless an external durable
request-to-task mapping can reuse the prior task id. The reference queue's
uniqueness constraint is only on the framework-issued `task_id`, not the
buyer's idempotency key. A web-process
crash after enqueue commits but before the `submitted` response reaches the
buyer can therefore cause a retried request to issue a second task and queue
row. Fully closing that window requires SDK support for looking up or reusing
a workflow task id by buyer idempotency key.

For a mixed adapter, make the split explicit with
`method_level_idempotency_methods`: include only methods that return terminal
responses from the method-level cache, and implement the workflow method's
deduplication in the durable queue. The default reference includes
`create_media_buy` because its mock path is inline.

## Task transitions

| Handler outcome | Work owner | Persistence owner | What the buyer does next |
|---|---|---|---|
| Return a result | Request handler | Idempotency backend caches the terminal response | Consume the inline result |
| Raise `AdcpError` | Request handler | Framework projects the structured error; no task is created | Follow its recovery guidance |
| `ctx.handoff_to_task(fn)` | SDK runs `fn` in the web process | `PgTaskRegistry` records submitted, progress, and terminal state | Poll `tasks/get` or await a webhook |
| `ctx.handoff_to_workflow(enqueue)` | The adopter's queue/worker/HITL system | The enqueue callback stores the task id; that system later calls `registry.complete()` or `registry.fail()` | Poll `tasks/get` or await a webhook |
| Input-required response | The business workflow that needs clarification | That workflow must retain the continuation state and context id | Resume the same context with the requested input |

Use `TaskHandoff` only for bounded in-process work. A human review, Airflow
DAG, or long-running queue consumer belongs in `WorkflowHandoff`; otherwise a
web-process restart can strand the work even though the task row survived.

For push-configured tasks, `PgTaskRegistry.complete()` and `.fail()` write the
terminal task and encrypted outbox envelope atomically. The worker leases the
outbox row, sends the exact stored body, and retries it with a stable
idempotency key. Polling and callbacks therefore observe the same terminal
artifact.

## Durable WorkflowHandoff example

[`workflow_queue.py`](../examples/v3_reference_seller/src/workflow_queue.py)
is a PostgreSQL-backed adopter queue with expiring leases. The enqueue callback
stores the framework task id before `WorkflowHandoff` returns `submitted`; a
replacement worker reclaims an expired lease after a crash. Handler failures
retry with capped exponential backoff; after the configured attempt limit the
registry task fails and the queue row moves to `dead_lettered`. Jobs without a
matching account-scoped registry task dead-letter immediately.

```python
queue = task_wiring.workflow_queue

async def create_media_buy(self, req, ctx):
    upstream_order = await create_upstream_order(req)
    payload = {
        "upstream_order_id": upstream_order["id"],
        "downstream_idempotency_key": req.idempotency_key,
    }

    async def enqueue(task_ctx):
        await queue.enqueue_from_handoff(
            task_ctx,
            account_id=ctx.account.id,
            workflow_type="manual_media_buy_approval",
            payload=payload,
        )

    return ctx.handoff_to_workflow(enqueue)

async def handle_approval(job):
    # Any external write here must deduplicate on the stored buyer key.
    return await approve_and_build_result(job.payload)

# In the separately supervised worker entrypoint (includes SIGTERM handling):
await run_with_signals(workflow_handler=handle_approval)
```

The queue completes the original `PgTaskRegistry` record only after the
handler returns. A crash after an external side effect but before queue
acknowledgement causes deliberate re-execution after lease expiry, so the
business effect must be independently idempotent. The PostgreSQL conformance
test kills the first logical worker after claim, creates fresh queue/registry
objects, and verifies that the replacement completes the same task id.
Queue payloads are ordinary JSONB: store only minimal continuation state and
never copy push-notification credentials or other secrets into them.

## Run the reference deployment

Install the PostgreSQL extra and start the development database:

```bash
pip install -e '.[dev,pg]'
cd examples/v3_reference_seller
docker compose up -d postgres
```

Generate distinct webhook-signing and outbox-encryption keys. Keep both in a
secret manager in production; the environment variables below are for the
local runnable path.

```bash
adcp-keygen --alg ed25519 --purpose webhook-signing \
  --kid reference-webhook-key \
  --out /tmp/adcp-reference-webhook-signing.pem

export ADCP_TASK_DATABASE_URL=postgresql://postgres@localhost/adcp
export ADCP_TASK_WEBHOOK_ENCRYPTION_KEY="$(openssl rand -base64 32)"
export ADCP_WEBHOOK_SIGNING_KEY_PATH=/tmp/adcp-reference-webhook-signing.pem
export ADCP_WEBHOOK_SIGNING_KEY_ID=reference-webhook-key
export ADCP_WEBHOOK_SIGNING_ALG=ed25519
export ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS=86400
```

For a local smoke test, start the mock upstream and web process as background
jobs, then leave the worker in the foreground. The exported configuration is
shared by all three processes:

```bash
npx -y -p @adcp/client@latest \
  adcp mock-server sales-guaranteed --port 4503 --api-key test-key &

DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp python -m seed

ADCP_ENV=development \
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
MOCK_AD_SERVER_URL=http://127.0.0.1:4503 \
MOCK_AD_SERVER_API_KEY=test-key \
python -m src.app &

python -m src.worker
```

That block is a loopback smoke test using public fixture credentials; never
expose it or reuse its token. A real production entrypoint must replace the
seeded bearer map with OAuth or RFC 9421 verification, use managed TLS
PostgreSQL and a non-mock upstream, and inject database credentials and signing
material from a secret manager. Set `ADCP_ENV=production` only for that real
configuration; it makes the durable bundle mandatory at boot.

In production, supervise those long-lived commands separately and inject the
same secret-manager values into both the web and worker processes. The example
validates the complete durable configuration before binding the HTTP listener.
A partial key, database, encryption, or retry configuration fails with the
missing field names. The retry horizon is projected into capabilities and must
match the outbox value.

`DurableTaskWiring.startup()` calls `create_schema()` for a convenient local
bootstrap. The workflow example performs the one additive upgrade shown here,
but these runtime DDL calls are not a general schema migration system and do
not detect or safely evolve an arbitrarily mismatched table.
For production, copy the SDK-owned SQL files (`decisioning_tasks.sql` and
`task_webhook_outbox.sql`), the `PgBackend.create_schema()` DDL, and the
reference workflow-queue DDL into reviewed, versioned migrations and apply
them before either process starts. Runtime bootstrap can remain a safety net,
but migrations own schema evolution and rollback.

The worker installs `SIGTERM` and `SIGINT` handlers, cancels its polling loops,
awaits their cleanup, and then closes the sender and PostgreSQL pools. This is
the shutdown path used by ordinary container and process supervisors.

## Production checklist

- Replace bearer fixture authentication with your OAuth or RFC 9421 verifier.
- Use managed PostgreSQL with TLS and credential authentication; never deploy
  the example Compose file or seed data.
- Publish the public webhook JWK and keep request-signing and webhook-signing
  keys distinct.
- Run at least one separately supervised outbox worker and alert on expired or
  quarantined rows.
- Route human or long-running approval work through `WorkflowHandoff` and a
  durable queue; reserve `TaskHandoff` for bounded work that may safely fail on
  web-process restart.
- Put a uniqueness constraint on business effects keyed by the buyer's
  idempotency key. The SDK cache cannot make a separate upstream transaction
  atomic.
- Schedule `PgBackend.delete_expired()` (or equivalent SQL/pg_cron cleanup) so
  expired idempotency rows do not accumulate.
- Apply URL challenge and SSRF validation before accepting durable callback
  destinations.
- Run the in-process tests and the media-buy seller storyboard before deploy.

`DurableTaskWiring` remains example-owned so its configuration surface can be
validated by adopters first. Once the registry, queue, signing, migration, and
shutdown contracts stabilize together, it is a candidate for an SDK-supported
production builder rather than copyable scaffolding.

For constructor details and multi-tenant sender resolution, continue with
[`handler-authoring.md`](handler-authoring.md#webhooks). For tenant scoping
invariants, see [`multi-tenant-contract.md`](multi-tenant-contract.md).
