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
not make arbitrary in-process work durable. This repository supplies the task
registry and callback-delivery worker, not an adopter-specific approval queue.

The reference's `IdempotencyStore` wrapping is intentionally paired with its
inline terminal responses. Do not put the same method-level wrapper around a
method that returns a raw `TaskHandoff` or `WorkflowHandoff`: the wrapper runs
before framework task issuance and therefore cannot cache the projected
`{status: "submitted", task_id}` envelope. A durable workflow adapter must
deduplicate task issuance in its queue/business transaction (and advertise
idempotency only when it does so) until that projection boundary is supported
directly by the SDK.

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

For constructor details and multi-tenant sender resolution, continue with
[`handler-authoring.md`](handler-authoring.md#webhooks). For tenant scoping
invariants, see [`multi-tenant-contract.md`](multi-tenant-contract.md).
