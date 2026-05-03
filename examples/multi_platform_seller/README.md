# multi_platform_seller — N tenants, N platforms, one process

A worked example of the multi-tenant pattern: one `serve()` call hosts
two tenants, each backed by a different `DecisioningPlatform`
subclass, dispatched by `PlatformRouter` based on the request's
resolved tenant.

## Why this exists

Multi-tenant SaaS sales agents (Prebid `salesagent` and adopters in its
shape) maintain a registry like

```python
ADAPTER_REGISTRY: dict[str, Type[AdServerAdapter]] = {
    "adserver_a": AdServerAAdapter,
    "adserver_b": AdServerBAdapter,
    "mock": MockAdapter,
    ...
}
```

…and instantiate the right adapter per-request based on the tenant's
configured backend. The `DecisioningPlatform` framework supports the
same shape via `PlatformRouter`: preload one platform per tenant,
let the router pick by `ctx.account.metadata['tenant_id']`. The
adopter swaps a runtime `dict[str, Type[Adapter]]` lookup for a
construction-time `dict[str, DecisioningPlatform]` injection — same
mental model, framework-managed dispatch.

## What it ships

| File | Role |
|---|---|
| `src/mock_guaranteed.py` | `sales-guaranteed` mock — fixed-CPM, capacity-bounded inventory, `pending_creatives → pending_start → active → completed` lifecycle. |
| `src/mock_non_guaranteed.py` | `sales-non-guaranteed` mock — programmatic remnant, always accepts, delivery scales with budget. |
| `src/account_store.py` | `MultiTenantAccountStore` — resolves wire account refs to `Account` instances with `metadata['tenant_id']` populated. Reads either the `Host`-header-set tenant contextvar or the wire `account.account_id` prefix (`tenant-a:...`). |
| `src/app.py` | Boot script. Constructs both platforms, the account store, the `PlatformRouter`, wires `SubdomainTenantMiddleware`, calls `serve()`. |

## Running locally

```bash
# 1. (Optional) Map the tenant subdomains to localhost:
echo "127.0.0.1 tenant-a.localhost tenant-b.localhost" | sudo tee -a /etc/hosts

# 2. Boot the seller:
python -m examples.multi_platform_seller.src.app

# 3. Hit either tenant via subdomain:
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Host: tenant-a.localhost:3001" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  http://127.0.0.1:3001/mcp

# 4. Or send an explicit account ref (storyboard convention):
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
    "name":"get_products",
    "arguments":{"buying_mode":"brief","brief":"display","account":{"account_id":"tenant-a:demo"}}
  }}' \
  http://127.0.0.1:3001/mcp
```

## How tenant routing works

```
   ┌──────────────────────┐     SubdomainTenantMiddleware reads
   │  POST /mcp           │ ──► the Host header, sets the tenant
   │  Host: tenant-a...   │     contextvar.
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐     MultiTenantAccountStore.resolve()
   │  PlatformRouter      │ ──► reads the contextvar (or the wire
   │  (DecisioningPlatform│     ref's tenant prefix) and stamps
   │   subclass)          │     metadata['tenant_id'] onto the
   └──────────┬───────────┘     resolved Account.
              │
              ▼
   ┌──────────────────────┐     Synthesized delegation for every
   │  router.get_products │ ──► specialism method looks up the
   │  router.create_…     │     child by tenant_id and forwards
   └──────────┬───────────┘     the call verbatim.
              │
              ▼
   ┌──────────────────────────────┐
   │ MockGuaranteedPlatform OR    │
   │ MockNonGuaranteedPlatform    │
   └──────────────────────────────┘
```

## Capabilities — union, not intersection

The router's `capabilities.specialisms` is the **union** of every
child platform's specialisms. `tools/list` advertises every tool any
child serves; calls to a tool the resolved tenant doesn't implement
raise a structured `UNSUPPORTED_FEATURE` error per spec.

Intersection-based capabilities would silently hide tools that some
tenants DO support, breaking the "one URL → many tenants" model.

## Adding a third tenant

1. Write a new `DecisioningPlatform` subclass for the new tenant's
   backend (or reuse one of the mocks).
2. Add `"tenant-c": NewPlatform()` to the `platforms=` mapping in
   `app.py`.
3. Add the new tenant to `MultiTenantAccountStore(tenants=...)` and to
   the `SubdomainTenantMiddleware` router.
4. If the new tenant adds a specialism (e.g. `audience-sync`), extend
   the router's `capabilities.specialisms` accordingly. The router's
   synthesized delegation already covers every framework-known
   specialism method — you don't have to update the router itself.

## Migration target — `ADAPTER_REGISTRY` adopters

A separate [`MIGRATION_FROM_ADAPTER_REGISTRY.md`](./MIGRATION_FROM_ADAPTER_REGISTRY.md)
walks through the salesagent translation step-by-step. The short version:

```python
# Before — runtime adapter instantiation per tenant
ADAPTER_REGISTRY: dict[str, Type[AdServerAdapter]] = {
    "adserver_a": AdServerAAdapter,
    "adserver_b": AdServerBAdapter,
}
adapter = ADAPTER_REGISTRY[tenant.backend](tenant.config, principal)
result = adapter.create_media_buy(request)

# After — boot-time platform construction, framework-managed dispatch
router = PlatformRouter(
    accounts=tenant_routing_account_store,
    platforms={
        tenant_id: build_platform_for(tenant)
        for tenant_id, tenant in load_tenants().items()
    },
    capabilities=DecisioningCapabilities(
        specialisms=union_of_tenant_specialisms(),
        ...,
    ),
)
serve(router, asgi_middleware=[(SubdomainTenantMiddleware, {...})])
```

## What this example deliberately leaves out

- **Real upstream HTTP.** Both mocks set `upstream_url = None` and
  serve in-process. Production adopters set `upstream_url` on each
  child platform and let `DecisioningPlatform.upstream_for(ctx)`
  thread the per-request client.
- **Multi-protocol fan-out.** Each request resolves to exactly one
  tenant. Fan-out (one buyer call → multiple sellers) is a different
  pattern and out of scope.
- **Cross-tenant state.** Each tenant is an island — its own
  inventory, its own buys, its own auth. The router never aggregates
  across tenants.
