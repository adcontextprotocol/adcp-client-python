# Migrating an existing AdCP seller to the v3 framework + translator pattern

Audience: maintainers of existing pre-v3 sales agents — Prebid's
[salesagent](https://github.com/prebid/salesagent), GAM-fronting middleware,
FreeWheel-fronting middleware, in-house seller adapters — who want to
adopt the AdCP Python SDK without rewriting their ad-ops integration.

## Why the translator pattern

A real publisher already has an ad server. GAM, FreeWheel, Kevel,
Beeswax, an in-house DSP — wherever your inventory and order state
lives, that's the source of truth for ad-ops.

The translator pattern keeps that intact. Your existing ad server
stays where it is. The AdCP wire layer becomes a thin adapter that
translates AdCP shapes onto your upstream's API and back. Two layers,
clear separation:

* **AdCP wire** — protocol envelopes, validation, idempotency,
  task lifecycle, structured errors. The framework owns this.
* **Ad-ops upstream** — your existing API. Orders, line items,
  creatives, delivery, billing. You own this.

The local Postgres in this reference seller stores only the
*commercial-identity* layer — which buyer agent is allowed to talk to
us, which AdCP account they map to upstream, what billing terms apply.
Everything else is a passthrough to the upstream.

This is the deliberate inverse of the "build an ad server inside your
seller" pattern. Real adopters have an ad server already. Don't
duplicate its persistence; translate to it.

## What you keep

* **Your existing upstream API client.** All your code that already
  calls GAM / FreeWheel / your in-house ad server — order creation,
  delivery reporting, creative upload, conversion ingestion — keeps
  running. The reference seller's `src/upstream.py` is a worked
  example of the shape we expect; replace it with your real client.
* **Your business logic for product catalog generation.** The
  reference seller's `get_products` translates a single upstream
  endpoint to AdCP `Product[]`. Real adopters whose product catalog
  comes from a CMS / planning tool / forecasting service plug that
  business logic into the platform's `get_products` — call your
  existing query, project the result onto AdCP shapes.
* **Your reporting integration.** `get_media_buy_delivery` and
  `provide_performance_feedback` are pure projections — your
  existing delivery / pacing / CAPI flows feed them.
* **Your tenant model, RBAC, and audit trail.** The framework's
  `SubdomainTenantMiddleware` + `AuditSink` Protocols compose with
  your existing models.

## What you replace

* **The AdCP wire layer.** Stop hand-rolling MCP / A2A request
  parsing, schema validation, and response shaping. Use
  `adcp.decisioning.serve(...)` + the `SalesPlatform` Protocol.
* **Hand-coded idempotency, task envelopes, error envelopes.** The
  framework projects `TaskHandoff` / `WorkflowHandoff` / `AdcpError`
  onto the wire shapes for you. Your platform method bodies stay
  shape-agnostic.

## What's new in the v3 framework

* **Tier 2 `BuyerAgentRegistry`.** Commercial-identity gate that
  runs *before* the platform method. Suspended / blocked agents are
  rejected with structured errors at dispatch — your method body
  never sees them.
* **Projection guards on `list_accounts`.** The spec's
  write-only `billing_entity.bank` field is stripped from response
  payloads via `project_account_for_response`. Adopters who
  persist full bank coordinates for invoicing get the projection
  for free; the projection failing is a fail-fast in tests rather
  than a leak in prod.
* **Validation defaults.** `serve(...,
  validation=ValidationHookConfig(requests="strict",
  responses="strict"))` validates every payload against the bundled
  AdCP JSON schemas at boot and at every call. Spec drift surfaces
  immediately, not at first buyer storyboard run.
* **Capabilities response invariants.** The framework auto-projects
  your `DecisioningCapabilities` onto
  `account.supported_billing` (required by the spec when
  `media_buy` is in `supported_protocols`). Adopters can't ship
  spec-divergent capability responses.

## Step-by-step

### 1. Fork this directory as your starting point

```bash
cp -r examples/v3_reference_seller my-seller
cd my-seller
```

You'll edit `src/upstream.py`, `src/platform.py`, and the seed data.
The other modules (`models.py`, `tenant_router.py`, `buyer_registry.py`,
`audit.py`, `app.py`) are reusable scaffolding — change them only if
your tenant / RBAC / audit story differs.

### 2. Replace `MockUpstreamClient` with your real upstream client

`src/upstream.py` is a thin httpx-based client over the JS mock-server.
Replace it with your existing ad-server client:

```python
# src/upstream.py — your version
class MyAdServerClient:
    def __init__(self, *, base_url: str, oauth_token: str) -> None:
        ...

    async def list_orders(self, *, advertiser_id: str) -> list[Order]:
        ...

    async def create_order(self, *, payload: CreateOrderPayload) -> Order:
        ...

    # ... mirrors of your existing API surface
```

The shape doesn't have to match the JS mock's HTTP API — it has to
match your real upstream. The shape that matters is what comes *out*
of these methods (the data the platform translates into AdCP wire
shapes).

### 3. Reseed the BuyerAgent / Account tables with your tenant config

`seed.py` plants two tenants and two buyer agents for local
development. Replace it with your tenant fixtures (or, in production,
populate via your admin API).

The key field is `Account.ext` — this is where the AdCP-account →
upstream-account mapping lives:

```python
Account(
    account_id="signed-buyer-main",
    name="Signed Buyer — Main",
    ext={
        # Replace these keys with whatever your upstream needs to
        # scope a request — GAM networkCode + advertiserId,
        # FreeWheel customerId + advertiserId, etc.
        "network_code": "net_premium_us",
        "advertiser_id": "adv_volta_motors",
    },
)
```

The platform's `_make_account_store` reads `ext` onto
`ctx.account.metadata`, where every translator method picks it up.

### 4. Translate your upstream onto the `SalesPlatform` Protocol

`src/platform.py` shows the full mapping. The shape:

```python
class MyAdServerSeller(DecisioningPlatform, SalesPlatform):
    async def get_products(self, req, ctx):
        upstream_payload = await self._upstream.list_products(
            advertiser_id=ctx.account.metadata["advertiser_id"],
        )
        # translate to AdCP Product[]
        return GetProductsResponse(products=[...])

    async def create_media_buy(self, req, ctx):
        order = await self._upstream.create_order(...)
        if order.status == "pending_approval":
            # async approval path — return a Submitted envelope
            # and poll the upstream in the background
            return ctx.handoff_to_task(self._poll_until_approved)
        # sync fast path
        return CreateMediaBuySuccessResponse(...)

    # ... and so on for each method
```

### 5. Wire validation in strict mode (the default)

```python
serve(
    platform=platform,
    validation=ValidationHookConfig(requests="strict", responses="strict"),
    ...,
)
```

Strict on both sides. Drop to `responses="warn"` only if you have a
deliberate reason to ship spec-divergent responses.

### 6. Deploy

The framework serves both MCP and A2A on one binary
(`transport="both"`). MCP at `/mcp`, A2A at `/`. Behind your normal
ingress / load balancer.

## Common pitfalls

### Non-spec error codes

`AdcpError(code=...)` accepts any string — but only the canonical
[error-code enum](https://adcontextprotocol.org/schemas/v1/enums/error-code.json)
gets first-class buyer handling. Vendor codes outside the enum are
accepted but buyers won't have UI / retry semantics for them. Stick
to the spec codes.

### Missing required methods

The `SalesPlatform` Protocol has both required and optional methods.
v6.0 rc.1+ requires *all* of these on any sales-* claiming platform:

* `get_products` / `create_media_buy` / `update_media_buy` /
  `sync_creatives` / `get_media_buy_delivery` (always required)
* `get_media_buys` / `provide_performance_feedback` /
  `list_creative_formats` / `list_creatives` (required for sales-*)
* `sync_accounts` / `list_accounts` (required for v3)

Missing methods fail server boot via `validate_platform`, not at
runtime — fix the missing method, don't catch the boot failure.

### Strict validation catching shape drift

If your upstream returns shapes that don't quite match your
hand-written translation (`pricing.cpm` is sometimes a string,
sometimes a number; `delivery_type` is sometimes uppercase), strict
validation surfaces this at first call. Don't silence with
`responses="warn"`; fix the projection. The spec shape is the
contract.

### `update_media_buy` against an ad server that doesn't support it

The reference seller raises `UNSUPPORTED_FEATURE` for
`update_media_buy` because the JS mock has no order-update endpoint.
Real ad servers (GAM, FreeWheel) DO support order updates — wire your
PATCH / per-line-item update flow there. Don't leave the
`UNSUPPORTED_FEATURE` shim in production.

### Async approval — `handoff_to_task` vs `handoff_to_workflow`

The reference seller uses `handoff_to_task` because the mock auto-
approves after ~2 seconds (so a single coroutine polling a few times
is fine). Real human-in-the-loop trafficker review can take hours —
use `handoff_to_workflow` for that, where your trafficker UI calls
`registry.complete(task_id, result)` when the human signs off.
