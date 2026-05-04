# Salesagent side-car experiment — Phase 2 reference implementation

The side-car runtime that runs alongside salesagent's existing
`adcp-server` container, serving one experiment tenant via SDK
primitives while leaving every other tenant on the legacy runtime.

This directory is the **reference implementation** in
adcp-client-python. The actual deployment is into a salesagent
fork worktree, where these files mount under `src/sdk_runtime/`
and import salesagent's `_impl` functions, models, and
workflow_manager.

See the [experiment plan](../../docs/proposals/salesagent-sidecar-experiment.md)
for the full design (Phases 1A, 1B, 2; Step 0 prereqs; learning
questions Q1–Q5; pre-registered falsification signals).

## What's here

| File | What it does |
|---|---|
| `account_store.py` | `SalesagentBuyerAgentRegistry` (Principal.access_token bearer lookup) + `SalesagentAccountStore` (Account row → SDK Account, AgentAccountAccess scoping) + `fetch_gam_manual_approval_required` (HITL flag) |
| `gam_platform.py` | `GAMPlatform` wraps salesagent's `_impl` functions: `_get_products_impl`, `_create_media_buy_impl`, `_update_media_buy_impl`, `_get_media_buy_delivery_impl`, `_sync_creatives_impl`. Builds `ResolvedIdentity` from SDK ctx |
| `hitl_gate.py` | `compose_method` `before` hook — checks `gam_manual_approval_required`, writes `WorkflowStep` + `MediaBuy(raw_request=...)` rows via salesagent's existing `WorkflowManager`, short-circuits with `status='pending_approval'`. Wire→GAM-internal operation name mapping for the approval-config check |
| `serve_sidecar.py` | Entrypoint — `adcp.serve(...)` with the platform + auth shim + `WebhookSender` configured |

## How it ties to salesagent

```
┌──────────────────────────────────────────────────────────────┐
│  nginx proxy (port 8000)                                     │
│   route by X-Tenant-Id header:                               │
│     experiment_tenant → adcp-sidecar (port 8081)             │
│     everyone else      → adcp-server (port 8080)             │
└──────────────────┬─────────────────────┬─────────────────────┘
                   │                     │
        ┌──────────▼─────────┐  ┌────────▼───────────┐
        │ adcp-server (8080) │  │ adcp-sidecar (8081)│
        │  legacy runtime:   │  │  this code:        │
        │  - FastMCP server  │  │  - adcp.serve(...) │
        │  - A2A server      │  │  - GAMPlatform     │
        │  - existing tools  │  │  - HITL gates      │
        └──────────┬─────────┘  └────────┬───────────┘
                   │                     │
                   ↓                     ↓
        ┌─────────────────────────────────────────────┐
        │  Postgres (shared) — Tenant, Principal,     │
        │  Account, Product, MediaBuy, WorkflowStep   │
        └─────────────────────────────────────────────┘
```

## Deployment recipe (salesagent fork only — local, no upstream PR)

In a salesagent worktree (created via
`git worktree add /Users/brianokelley/Developer/salesagent/.conductor/sidecar-experiment -b bokelley/sidecar-experiment main`):

### 1. Copy this directory in

```bash
cp -r /path/to/adcp-client-python/examples/salesagent_sidecar \
      /Users/brianokelley/Developer/salesagent/.conductor/sidecar-experiment/src/sdk_runtime
```

The imports inside the files use `from src.core.tools...` and
`from src.core.database.models...` — they'll resolve in the salesagent
container.

### 2. Patch the two cross-tenant schedulers (Step 0.4)

In the worktree, edit:

* `src/services/media_buy_status_scheduler.py` — add tenant filter
* `src/services/delivery_webhook_scheduler.py` — add tenant filter

```python
# At top of each scheduler module
import os
SKIP_TENANT_IDS = {
    t.strip()
    for t in os.environ.get("EXPERIMENT_TENANT_IDS", "").split(",")
    if t.strip()
}

# In the cross-tenant query, add:
stmt = stmt.where(MediaBuy.tenant_id.notin_(SKIP_TENANT_IDS))
```

(Two cross-tenant schedulers; per-tenant disable is local-fork only,
not pushed upstream.)

### 3. Add the side-car service to docker-compose

Append to `docker-compose.yml`:

```yaml
  adcp-sidecar:
    build:
      context: .
      dockerfile: Dockerfile
    entrypoint: []
    env_file:
      - path: .env
        required: false
    environment:
      DATABASE_URL: postgresql://adcp_user:secure_password_change_me@postgres:5432/adcp?sslmode=disable
      EXPERIMENT_TENANT_IDS: "tenant_acme_test"
      SIDECAR_PORT: "8081"
      SIDECAR_WEBHOOK_SECRET: "test-secret-bytes-for-adcp-legacy"
      PYTHONPATH: "/app/.venv/lib/python3.12/site-packages:/app"
    depends_on:
      postgres:
        condition: service_healthy
      db-init:
        condition: service_completed_successfully
    volumes:
      - .:/app
      - /app/.venv
      # Mount adcp-client-python source for live changes
      - ../../adcp-client-python/src/adcp:/app/.venv/lib/python3.12/site-packages/adcp:ro
    command: ["python", "-m", "src.sdk_runtime.serve_sidecar"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### 4. Update nginx config to route by tenant

In `config/nginx/nginx-development.conf`, add a `map` block at the
top to detect the experiment tenant from headers and route:

```nginx
map $http_x_tenant_id $upstream_runtime {
    default            adcp-server:8080;
    tenant_acme_test   adcp-sidecar:8081;
}

# Then in the server block, replace the upstream proxy_pass with:
proxy_pass http://$upstream_runtime;
```

### 5. Bring it up

```bash
cd /Users/brianokelley/Developer/salesagent/.conductor/sidecar-experiment
docker compose up --build
```

Both adcp-server (legacy) and adcp-sidecar (SDK) come up. Nginx routes
by `X-Tenant-Id` header.

### 6. Configure the experiment tenant

Through salesagent's admin UI at http://localhost:8000/:

* Create tenant `tenant_acme_test`
* Create at least one Principal under it (note the `access_token`)
* Create at least one Account with `sandbox=True`
* Configure GAM credentials (real sandbox Network ID + service account JSON)
* Set `gam_manual_approval_required=False` for first run; flip to `True`
  for HITL exercise

### 7. Run the storyboards

```bash
# Happy path (instant approval)
npx -y -p @adcp/client adcp storyboard run \
    -H "X-Tenant-Id: tenant_acme_test" \
    -H "Authorization: Bearer <principal_access_token>" \
    http://localhost:8000/mcp media_buy_seller --json

# HITL exercise (approval flow) — set gam_manual_approval_required=True first
npx -y -p @adcp/client adcp storyboard run \
    -H "X-Tenant-Id: tenant_acme_test" \
    -H "Authorization: Bearer <principal_access_token>" \
    http://localhost:8000/mcp media_buy_guaranteed_approval --json
```

## Exit criteria (from PR #506)

1. Both storyboards pass against sandbox GAM
2. Recipe carries `implementation_config` without escape hatches
   (✅ already confirmed in Phase 1B —
   [`examples/recipe_falsification/`](../recipe_falsification/))
3. Glue LOC under ratio thresholds (proposal-side ≤60%,
   decisioning-side ≤30%)
4. Zero structural-guard allowlist additions (per Step 0.3, no
   guards fire on `src/sdk_runtime/`)
5. At least one finding contradicts a #502 prior (✅ already
   satisfied — Q1.5 in Phase 1A)
6. Webhook signature verified by a subscribed test buyer (per
   Step 0.6, parity is SDK→SDK only since salesagent's scheme
   is incompatible)

## Open work in this scaffold

The reference implementation here is structurally complete but has
gaps that surface during deployment. Document each as a finding when
you hit it:

* **`_build_resolved_identity` may need more fields** — `_impl`s
  read `identity.tenant.gemini_api_key`, `identity.tenant.advertising_policy`,
  etc. The minimal projection here may need expanding.
* **`_create_media_buy_impl` returns `CreateMediaBuyResult`** (a
  wrapper with `.response` and `.status`) — ensure the return-type
  projection matches the wire shape.
* **`WorkflowManager` constructor signature** varies across
  salesagent versions — pin to a specific commit and adjust if
  needed.
* **`_already_approved` setattr survival** — verified against
  `compose_method` in Step 0.5, but make sure no salesagent-side
  request projection re-validates between admin UI approval and
  the sidecar's gate check.
* **F12 webhook test against subscribed buyer** uses
  `adcp.WebhookReceiver` with matching secret (per Step 0.6) —
  not real salesagent buyers.

## Why local-fork, not upstream PR

The user explicitly scoped this experiment as local-fork only
(no PRs to salesagent). The scheduler patches and `src/sdk_runtime/`
directory live as local commits in the salesagent worktree;
`git checkout main` reverts everything cleanly.

## See also

* [Experiment plan](../../docs/proposals/salesagent-sidecar-experiment.md)
* [Recipe falsification (Phase 1B)](../recipe_falsification/)
* [Product architecture (revised post-experiment)](../../docs/proposals/product-architecture.md)
