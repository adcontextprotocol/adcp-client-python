# v3 reference seller — translator pattern

Canonical multi-tenant AdCP seller demonstrating the **translator
pattern**: AdCP wire on the inside, a real upstream ad server (the
JS mock-server shipped in `@adcp/client`, GAM-flavored) on the
outside. **Spec 3.0-compliant on the wire, 3.1-ready in architecture
and storage.**

Adopters fork this directory and replace `src/upstream.py` with their
own ad-server HTTP client. Everything else (Tier 2 commercial-identity
gate, tenant routing, audit trail, MCP+A2A serving, validation,
projection guards) is reusable scaffolding.

For migration guidance from a pre-v3 sales agent (e.g. Prebid's
salesagent), see [MIGRATION.md](MIGRATION.md).

| Component | Module | Source |
|---|---|---|
| Upstream HTTP client (translator seam) | `src/upstream.py` + `DecisioningPlatform.upstream_for` | `adcp.decisioning.UpstreamHttpClient` |
| Tier 2 commercial-identity gate | `src/buyer_registry.py` | `adcp.decisioning.BuyerAgentRegistry` |
| Subdomain tenant routing | `src/tenant_router.py` + `src/app.py` | `adcp.server.SubdomainTenantMiddleware` |
| Account v3 storage (bank-details column) | `src/models.py` | `Account.billing_entity` JSON column |
| Audit trail | `src/audit.py` | `adcp.audit_sink.AuditSink` |
| MCP + A2A on one binary | `src/app.py` | `serve(transport="both", asgi_middleware=...)` |
| Durable task state | `src/durable_tasks.py` | `adcp.decisioning.PgTaskRegistry` |
| Restart-safe external workflows | `src/workflow_queue.py` | `WorkflowHandoff` + leased PostgreSQL queue |
| Atomic signed webhook delivery | `src/durable_tasks.py` + `src/worker.py` | `PgTaskWebhookOutbox` |
| Durable request idempotency | `src/durable_tasks.py` | `adcp.server.idempotency.PgBackend` |
| Account v3 projection on read | `src/platform.py::list_accounts` | `adcp.types.project_account_for_response` |

## Architecture

```
┌──────────────┐        ┌─────────────────────────┐        ┌────────────────┐
│  AdCP buyer  │  MCP/  │  v3 reference seller    │  HTTP  │  JS mock-server │
│   (signed/   │  A2A   │  (this directory)       │  ────► │  (sales-        │
│   bearer)    │ ─────► │                         │        │   guaranteed)   │
└──────────────┘        │  • AdCP wire validation │        └────────────────┘
                        │  • Tier 2 identity gate │              ▲
                        │  • Account translation  │              │
                        │  • Postgres for IDs &   │              │
                        │    commercial relation  │              │
                        └─────────────────────────┘              │
                                  ▲                              │
                                  │                              │
                            ┌─────┴──────┐                       │
                            │ Postgres   │                       │
                            │ (tenants,  │                       │
                            │  agents,   │                       │
                            │  accounts) │                       │
                            └────────────┘                       │
                                                                 │
                              network_code + advertiser_id ──────┘
```

The local Postgres carries only the commercial-identity layer.
Ad-ops state — orders, line items, creatives, delivery, conversions —
lives upstream. Each `Account.ext` carries `{network_code,
advertiser_id}` so the translator can scope upstream calls correctly.

## Run it

You need two services running side-by-side: the JS mock-server (the
upstream) and the Python reference seller (the translator).

```bash
# 1. Boot the upstream
npx -y -p @adcp/client@latest \
    adcp mock-server sales-guaranteed --port 4503 --api-key test-key &

# 2. Start Postgres
cd examples/v3_reference_seller
docker compose up -d postgres

# 3. Seed dev fixtures (tenants + buyer agents + accounts with
#    upstream routing in account.ext)
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
  python -m seed

# 4. Boot the seller
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
  MOCK_AD_SERVER_URL=http://127.0.0.1:4503 \
  MOCK_AD_SERVER_API_KEY=test-key \
  python -m src.app
```

The seller binds `0.0.0.0:3001` and serves both transports.

The commands above use the lightweight local path: async tasks are pollable
but intentionally in-memory, and push-configured handoffs are rejected. For
the production-shaped PostgreSQL registry, idempotency cache, signed atomic
outbox, and separately supervised worker, follow the
[production seller path](../../docs/production-seller.md#run-the-reference-deployment).

The bundled mock upstream resolves approvals quickly, so
`create_media_buy` completes inline. When adapting the example to a human or
long-running approval system, return `ctx.handoff_to_workflow(...)` and let a
durable queue consumer call `registry.complete()` or `registry.fail()`; do not
move a long polling loop into an in-process `TaskHandoff`. That queue must also
deduplicate workflow issuance before enabling the idempotency capability; the
reference's method wrapper is for its inline terminal-response path.
The queue and restart-recovery test are runnable reference infrastructure;
adopters supply the workflow handler that talks to their approval system.

> ⚠️ **Local-dev only.** `docker-compose.yml` uses
> `POSTGRES_HOST_AUTH_METHOD=trust` and exposes 5432 on
> `0.0.0.0`. Do not run this compose file on a host reachable from
> an untrusted network. `seed.py` plants a literal dev bearer
> token (`dev-bearer-token-acme-1`) — do not run `seed.py` against
> a production `DATABASE_URL`. Production deployments point
> `DATABASE_URL` at managed Postgres with scram-sha-256 + a real
> password, and seed via the admin API (not this script).

## What's wired

### Schema (`src/models.py`)

Three tables — the spine of a multi-tenant translator-pattern seller:

- `tenants` — one row per `<subdomain>.example.com`.
  `SubdomainTenantMiddleware` reads the request `Host` header and
  finds the matching row.
- `buyer_agents` — Tier 2 commercial-identity rows. The framework's
  dispatch gate reads this BEFORE the platform method runs and
  rejects suspended (transient) and blocked (terminal) agents with
  structured errors.
- `accounts` — buyer-side accounts under recognized agents. Carries
  the spec 3.1-ready `billing_entity` (write-only bank details on
  responses) and `reporting_bucket`. The `ext` JSON column carries
  the translator-pattern routing (`network_code`, `advertiser_id`).

No `media_buys` / `creatives` / `performance_feedback` tables — that
data lives upstream.

### Upstream client (`src/upstream.py`)

`src/upstream.py` is a set of module-level **domain helpers** mirroring
the JS mock-server's openapi.yaml 1:1 (`list_products`, `create_order`,
`get_delivery`, etc.). Each helper takes a pooled
[`UpstreamHttpClient`](../../src/adcp/decisioning/upstream.py)
(the SDK's httpx wrapper handling auth + 4xx/5xx → `AdcpError`
projection) plus the per-call `network_code`, and returns the parsed
upstream JSON. Adopters fork the helpers and replace the URL paths /
payload shapes with their real ad-server's API; the SDK client handles
the boilerplate.

Auth and routing flow through `DecisioningPlatform.upstream_for(ctx)`:

```python
class V3ReferenceSeller(DecisioningPlatform, SalesPlatform):
    # Adopters with a real production upstream replace this URL.
    upstream_url = "https://sales-guaranteed.example.invalid/v1"

    async def get_products(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._upstream_auth)
        payload = await upstream_helpers.list_products(
            client, network_code=ctx.account.metadata["network_code"]
        )
        # ... project to AdCP shapes
```

The framework picks the URL from `ctx.account.mode`:

| `account.mode` | Upstream URL source |
|---|---|
| `live` / `sandbox` | `V3ReferenceSeller.upstream_url` (production) |
| `mock` | `account.metadata["mock_upstream_url"]` (per-account fixture URL) |

The reference seller is **mock-mode by design** — its only upstream
is the JS mock-server fixture, so `_make_account_store` stamps every
resolved account with `mode="mock"` and
`metadata["mock_upstream_url"]` (sourced from the
`MOCK_AD_SERVER_URL` env). Adopters with a real production upstream:

* Declare `upstream_url` on their `V3ReferenceSeller` subclass to the
  production URL.
* Default new accounts to `mode="live"` in `AccountStore.resolve`.
* Reserve `mode="mock"` (with `mock_upstream_url`) for conformance /
  storyboard accounts only — the same adapter code path runs against
  either URL with no per-call branching.

### Platform (`src/platform.py`)

`V3ReferenceSeller` claims **both** `sales-non-guaranteed` and
`sales-guaranteed` (the mock supports `delivery_type:
guaranteed/non_guaranteed` — real GAM-shaped publishers sell both).

Each method calls the upstream over HTTP and translates the response
to AdCP wire shapes. The bundled mock's `create_media_buy` approval path
polls briefly and returns an inline terminal response. A real approval that
can outlive the request belongs in an adopter-owned durable queue entered via
`WorkflowHandoff`, with completion surfaced through `tasks/get` and webhooks.

`update_media_buy` raises `UNSUPPORTED_FEATURE` because the JS mock
has no order-update endpoint. Real adopters wire their PATCH / per-
line-item update flow there.

`sync_accounts` and `list_accounts` are the exception — they read
and write the local Postgres. The AdCP account → upstream
`network_code` mapping is the durable record this seller owns.

### Tenant routing (`src/tenant_router.py`)

`SqlSubdomainTenantRouter` implements the framework's
`SubdomainTenantRouter` Protocol. The middleware sets the
`current_tenant()` contextvar; downstream stores
(`buyer_registry.py`, `platform.py`) read it without explicit
plumbing.

### Commercial-identity gate (`src/buyer_registry.py`)

`TenantScopedBuyerAgentRegistry` extends the framework's
`PgBuyerAgentRegistry` pattern with tenant scoping — the same
`agent_url` can have different commercial postures across tenants.
Implements both `resolve_by_agent_url` (signed traffic) and
`resolve_by_credential` (bearer / OAuth).

### Audit trail (`src/audit.py`)

`DbAuditSink` writes one `audit_events` row per skill dispatch.
Failures are swallowed by the framework's audit middleware (the
sink is fire-and-forget by Protocol contract). Adopters with Slack
alerting compose with `adcp.audit_sink.SlackAlertSink` via
`CompositeAuditSink`.

## Auth modes

The seller supports both v3 signed-request and pre-trust beta
bearer auth simultaneously — the `BuyerAgentRegistry` dispatches
on credential kind. Adopter middleware constructs `AuthInfo` two
ways:

```python
# Signed (v3) — produces typed HttpSigCredential
auth = AuthInfo.from_verified_signer(
    signer,                       # from adcp.signing.verify_request_signature
    max_verified_age_s=300.0,     # reject stale signers
)

# Bearer (pre-trust beta) — typed ApiKeyCredential
auth = AuthInfo(
    kind="bearer",
    credential=ApiKeyCredential(kind="api_key", key_id=token_id),
)
```

Both put the typed credential into `ctx.metadata['adcp.auth_info']`
where the framework picks it up.

## What's NOT wired (yet)

- **HTTP-Sig verifier middleware** — adopters add
  `verify_request_signature` in their `context_factory` once
  AAO publishes the brand.json registry.
- **Brand authorization (Tier 3)** — gated on ADCP spec issue
  #3690.
- **Admin CRUD API** — separate Starlette app for tenant / agent
  CRUD. Patterns to come; for now use `seed.py` and direct SQL.

## Migrations

The app boots by running `alembic upgrade head` — column renames,
type changes, and new columns on existing tables all propagate
through the migration scripts under `alembic/versions/`. The same
path runs from `seed.py`, `migrate.py`, and CI, so the schema you
develop against matches what ships.

### Install Alembic

```bash
pip install alembic
# or, if using a requirements file:
echo "alembic" >> requirements.txt && pip install -r requirements.txt
```

### Apply migrations

```bash
cd examples/v3_reference_seller

# Apply all pending migrations (run after every git pull that touches models).
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp python -m migrate

# Equivalent direct alembic invocation:
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp alembic upgrade head
```

### Generate a new migration after changing models

```bash
cd examples/v3_reference_seller
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
  alembic revision --autogenerate -m "describe your change"
```

Alembic compares the live database to `Base.metadata` and emits a
migration file under `alembic/versions/`.  **Always review the generated
file before committing** — autogenerate misses some constructs (partial
index predicates, custom CHECK constraints, server defaults).

> ⚙️ **Adding a new model file?** Import it in `alembic/env.py` alongside
> `src.models` and `src.audit`, or autogenerate will silently omit its
> tables from the migration.

### Roll back

```bash
# Roll back one step.
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp alembic downgrade -1

# Roll back to before any migrations (drops all tables defined in this schema).
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp alembic downgrade base
```

> ⚠️ **`downgrade` in production is irreversible without a data backup.**
> Take a snapshot before running downgrade against any database that
> holds real data.

### Run migration integration tests

```bash
# Uses a throw-away database (adcp_test) so the migration run starts clean.
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp_test \
  pytest examples/v3_reference_seller/tests/test_migrations.py -m integration -v
```

## Customization

Adopters typically change:

1. **`src/upstream.py`** — fork the per-endpoint domain helpers and
   replace the paths / payloads with your real ad server's API. Keep
   the helpers as functions taking `UpstreamHttpClient` + per-call
   routing kwargs; the SDK client handles pooling, auth injection,
   and `AdcpError` projection.
2. **`V3ReferenceSeller.upstream_url`** — replace the placeholder with
   your production upstream URL once you migrate accounts to
   `mode="live"`.
3. **`src/platform.py::_make_account_store`** — branch on the row's
   lifecycle to set `mode`: `live` for production accounts, `sandbox`
   for your own test infra, `mock` (with `mock_upstream_url`) for
   conformance / storyboard accounts only.
4. **`src/platform.py`** method bodies — adjust the AdCP ↔ upstream
   translation (mostly type-mapping). The structure of each method
   stays identical (resolve client via `upstream_for`, call domain
   helper, project upstream JSON onto AdCP shapes).
5. **`src/audit.py`** — extend `details` with adopter-specific
   fields (decision flags, fraud scores, A/B variant ids).
6. **Auth wiring in `src/app.py`** — wire your verifier middleware
   that constructs `AuthInfo`.

Adopters typically *don't* change:

- Models — the v3 schema (tenants / buyer_agents / accounts) is
  the contract.
- Tenant router logic — the Protocol shape is fixed.
- Audit middleware composition — the framework wires it.
- The unified MCP+A2A binary — `transport="both"` is one knob.

## Spec versioning

This seller is **3.0-compliant on the wire** — every field it sends
matches the AdCP 3.0 schemas. The schema and architecture is
**3.1-ready** (`billing_entity` + `reporting_bucket` columns,
typed `BillingMode`, write-only bank-details projection). Sellers
running this code today serve 3.0 buyers; the same code serves
3.1 buyers when the spec lands.
