# v3 reference seller

Canonical multi-tenant AdCP seller. **Spec 3.0-compliant on the wire,
3.1-ready in architecture and storage.** Adopters fork this directory
and replace the platform impl with their own business logic.

This directory wires every Tier 2 / v3-supporting component the SDK
ships into one runnable binary:

| Component | Module | Source |
|---|---|---|
| Tier 2 commercial-identity gate | `src/buyer_registry.py` | `adcp.decisioning.BuyerAgentRegistry` |
| Subdomain tenant routing | `src/tenant_router.py` + `src/app.py` | `adcp.server.SubdomainTenantMiddleware` |
| Account v3 storage (bank-details column) | `src/models.py` | `Account.billing_entity` JSON column |
| Audit trail | `src/audit.py` | `adcp.audit_sink.AuditSink` |
| MCP + A2A on one binary | `src/app.py` | `serve(transport="both", asgi_middleware=...)` |
| Durable HITL tasks (optional) | swap to `PgTaskRegistry` | `adcp.decisioning.pg.PgTaskRegistry` |
| Durable webhook delivery (optional) | swap to `PgWebhookDeliverySupervisor` | `adcp.webhook_supervisor_pg` |
| HTTP-Sig verifier → AuthInfo (TODO) | adopter middleware | `adcp.decisioning.AuthInfo.from_verified_signer` |
| Account v3 projection on read (TODO) | adopter wires in `sync_accounts` | `adcp.types.project_account_for_response` |

## Run it

```bash
# 1. Start Postgres
cd examples/v3_reference_seller
docker compose up -d postgres

# 2. Seed dev fixtures
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
  python -m seed

# 3. Boot the seller
DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \
  python -m src.app
```

The server binds `0.0.0.0:3001` and serves both transports.

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

Four tables — the spine of a multi-tenant v3 seller:

- `tenants` — one row per `<subdomain>.example.com`.
  `SubdomainTenantMiddleware` reads the request `Host` header and
  finds the matching row.
- `buyer_agents` — Tier 2 commercial-identity rows. The framework's
  dispatch gate reads this BEFORE the platform method runs and
  rejects suspended (transient) and blocked (terminal) agents with
  structured errors.
- `accounts` — buyer-side accounts under recognized agents. Carries
  the spec 3.1-ready `billing_entity` (write-only bank details on
  responses) and `reporting_bucket` (offline reporting target). The
  reference seller does not implement `sync_accounts`, so the
  bank-details projection is a column-level architectural seam, not
  an enforced runtime guard — adopters who add `sync_accounts`
  MUST project through `adcp.types.project_account_for_response`
  before returning the row.
- `media_buys` — terminal artifact of `create_media_buy`,
  idempotency-keyed for replay safety.

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

### Platform (`src/platform.py`)

`V3ReferenceSeller` implements `sales-non-guaranteed` — the five
required Sales methods (`get_products`, `create_media_buy`,
`update_media_buy`, `sync_creatives`, `get_media_buy_delivery`).
Every method body reads `ctx.buyer_agent` (the resolved Tier 2
record) and `ctx.account` (the resolved account); both are
populated by the framework's dispatch gate before the method runs.

This file is the bulk of what an adopter customizes. Everything
else is boilerplate the seller wires once.

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

These ship as separable follow-ups — the framework's components
exist; the reference seller wires the simpler defaults:

- **HTTP-Sig verifier middleware** — adopters add
  `verify_request_signature` in their `context_factory` once
  AAO publishes the brand.json registry. The Tier 1 SDK primitives
  ship in `adcp.signing`; this seller uses bearer auth in the seed.
- **Brand authorization (Tier 3)** — gated on ADCP spec issue
  #3690.
- **Postgres `TaskRegistry` / `WebhookDeliverySupervisor`** —
  swap `InMemoryTaskRegistry` → `PgTaskRegistry` and
  `InMemoryWebhookDeliverySupervisor` → `PgWebhookDeliverySupervisor`
  in `src/app.py` for production durability. Both classes ship in
  the SDK; this seller's `app.py` uses the in-memory variants for
  fast iteration.
- **Alembic migrations** — `Base.metadata.create_all` runs at boot
  (idempotent on table existence — it does NOT detect column
  renames or type changes on existing tables). Adopters who
  prototyped against earlier branches and pulled new column
  changes should drop and recreate the dev database; production
  sellers wire Alembic and version their schema changes.
- **Admin CRUD API** — separate Starlette app for tenant / agent
  CRUD. Patterns to come; for now use `seed.py` and direct SQL.

## Customization

Adopters typically change:

1. **`src/platform.py`** — the platform method bodies. Replace the
   stub product catalog, add your CMS query for `get_products`,
   route `create_media_buy` into your real DSP / ad-server, etc.
2. **`src/audit.py`** — extend `details` with adopter-specific
   fields (decision flags, fraud scores, A/B variant ids).
3. **Auth wiring in `src/app.py`** — wire your verifier middleware
   that constructs `AuthInfo`.

Adopters typically *don't* change:

- Models — the v3 schema is the contract.
- Tenant router logic — the Protocol shape is fixed.
- Audit middleware composition — the framework wires it.
- The unified MCP+A2A binary — `transport="both"` is one knob.

## Spec versioning

This seller is **3.0-compliant on the wire** — every field it sends
matches the AdCP 3.0 schemas. The schema and architecture is
**3.1-ready** (`billing_entity` + `reporting_bucket` columns on
`Account`; `invoice_recipient` column on `MediaBuy`; typed
`BillingMode`; write-only bank-details projection via
`BusinessEntityResponse`). Sellers running this code today serve
3.0 buyers; the same code serves 3.1 buyers when the spec lands.
