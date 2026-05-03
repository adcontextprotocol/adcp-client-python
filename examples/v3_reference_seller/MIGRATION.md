# Migrating an existing AdCP seller to the v3 framework + translator pattern

Audience: maintainers of existing pre-v3 sales agents — Prebid's
[salesagent](https://github.com/prebid/salesagent), GAM-fronting middleware,
FreeWheel-fronting middleware, in-house seller adapters — who want to
adopt the AdCP Python SDK without rewriting their ad-ops integration.

## Pre-v3 → v3 model/method mapping (Prebid salesagent porting checklist)

This is a checklist for porting your existing AdCP 3.0.0-beta.2 sales
agent to v3. Each row maps a thing in your old code to a thing in this
template. The column on the right calls out the gotchas.

| Pre-v3 (3.0.0-beta.2) shape | v3 location | Notes |
|---|---|---|
| `MediaBuy` model (local DB) | upstream `POST /v1/orders` | Drop your `MediaBuy` table; the upstream owns this. The translator never persists media-buy rows locally. |
| `Creative` model (local DB) | upstream `POST /v1/creatives` | Drop your `Creative` table. `sync_creatives` translates AdCP creatives onto the upstream's create call. |
| `PerformanceFeedback` model | upstream `POST /v1/orders/{id}/conversions` (CAPI) | **Semantic gap** — see "CAPI semantic mismatch" below. AdCP perf feedback is an aggregate; CAPI is per-event. The reference seller accepts only `metric_type='conversion_rate'`. |
| `Account` model | local DB (commercial-identity layer) | **KEEP**. Add `ext.network_code` + `ext.advertiser_id` columns so the translator can route per-call. The translator's `_make_account_store` reads these onto `ctx.account.metadata`. |
| `Tenant` / `BuyerAgent` | local DB | **KEEP** — these are the v3 commercial-identity layer. Strict tenant isolation runs in the framework's `SubdomainTenantMiddleware`. |
| `seller_agent_v1.py` entrypoint | `examples/v3_reference_seller/src/app.py` template | Rewrite around `serve(transport='both', ...)`. Drop your hand-rolled MCP/A2A request parsing. |
| `get_products(req, ctx)` body | translator method calling upstream | Replace inline catalog logic with HTTP translation. Fall back to your CMS / planner / forecasting service if your upstream's products endpoint isn't enough. |
| `create_media_buy(req, ctx)` body | translator method | Now async with `TaskHandoff` for HITL approval flows. Sync fast path returns `CreateMediaBuySuccessResponse` directly; slow path returns `ctx.handoff_to_task(fn)` and the framework projects the wire `Submitted` envelope. |
| `update_media_buy(req, ctx)` body | translator method (or `UNSUPPORTED_FEATURE`) | Wire to your upstream's order-update endpoint (GAM `LineItemService.performLineItemAction`, FreeWheel `updateOrder`). The reference seller raises `UNSUPPORTED_FEATURE` because the JS mock has no update endpoint. Don't ship the shim. |
| `sync_creatives(req, ctx)` body | translator method | One upstream `POST /v1/creatives` per creative; AdCP `creative_id` passes through as `client_request_id` for upstream dedup. |
| `get_media_buy_delivery(req, ctx)` body | translator method | The upstream's `DeliveryReport` schema may not carry order status — the reference seller double-fetches `get_order` so AdCP `MediaBuyStatus` reflects the actual state (completed / canceled / rejected don't surface as `active`). |
| `provide_performance_feedback(req, ctx)` body | translator method | See "CAPI semantic mismatch". |
| `list_creative_formats(req, ctx)` body | translator method | Static catalog in the reference seller. Real publishers drive this from their format registry. |
| Hand-rolled idempotency tracking | framework `RequestContext` + `idempotency_key` | The framework persists `idempotency_key → response_hash`; replays are constant-time. |
| Hand-rolled task lifecycle | framework `TaskRegistry` + `TaskHandoff` | Adopters call `ctx.handoff_to_task(fn)` and the framework manages submitted → working → completed/failed. Adopter coroutine can `raise AdcpError(...)` to signal terminal failure — the framework projects to wire-shape `failed`. |

### Specialism declaration upgrade

The 3.0.0-beta.2 capability shape declared specialism inline on the
agent card. v3 (currently pinned to `3.0.5` — see
[`src/adcp/ADCP_VERSION`](../../src/adcp/ADCP_VERSION) for the canonical
pin) consolidates this onto `DecisioningCapabilities`:

```python
capabilities = DecisioningCapabilities(
    specialisms=("sales-non-guaranteed", "sales-guaranteed"),
    channels=("display", "video"),
    pricing_models=("cpm",),
    supported_billing=("operator", "agent"),  # required when 'media_buy' is in supported_protocols
)
```

What changed:

* **`DecisioningCapabilities` is the single home** for specialisms,
  channels, pricing_models, and supported_billing. Don't hand-roll
  the agent card — `serve(...)` projects this object onto the wire.
* **`validate_platform()` enforcement** ([PR #423](https://github.com/adcontextprotocol/adcp-client-python/pull/423))
  warns at boot if your platform claims a specialism but is missing
  required methods. Treat the warning as an error in CI.
* **`validate_capabilities_response_shape()`** ([PR #422](https://github.com/adcontextprotocol/adcp-client-python/pull/422))
  catches drift between your declared capabilities and what the
  framework projects on the wire. Spec-divergent capability responses
  fail validation rather than ship.

### Strict validation gotchas

`serve(validation=ValidationHookConfig(requests='strict', responses='strict'))`
is now the default ([PR #439](https://github.com/adcontextprotocol/adcp-client-python/pull/439)).
Common shape regressions when porting from 3.0.0-beta.2:

* **`pricing_options[].pricing_model`**, not `type`. The v3 schema
  renamed the discriminator field; old code using `{"type": "cpm",
  ...}` fails strict validation.
* **`pricing_options[].fixed_price`**, not `rate`. The CPM rate field
  was renamed to `fixed_price` for consistency across pricing models.
* **`format_id` is structured** (`{"agent_url": ..., "id": ...}`), not
  a bare string. Pre-v3 `format_id: "display_300x250"` fails.
* **`AdcpError(recovery=...)` accepts `'transient'` / `'terminal'` /
  `'retry_with_changes'` / `'correctable'`** only. The legacy
  `recovery='retry'` string is not in the AdCP enum and fails
  type-checking.

If you're seeing `responses='warn'` regressions during port, fix the
projection — don't relax validation. The spec shape is the contract.

### Spec error codes — what to use

The canonical enum ships at
[`src/adcp/types/generated_poc/enums/error_code.py`](../../src/adcp/types/generated_poc/enums/error_code.py).
Common codes the translator emits:

| Code | When to use | `recovery` |
|---|---|---|
| `INVALID_REQUEST` | Buyer sent a malformed request, or upstream rejected the translated payload (400). | `terminal` |
| `MEDIA_BUY_NOT_FOUND` | Upstream 404 on a known-media-buy operation (`get_order`, `get_delivery`, `post_conversions`). | `terminal` |
| `ACCOUNT_NOT_FOUND` | Upstream 404 on an account-scoped operation (`get_products`, `list_creatives`, `list_accounts`). | `terminal` |
| `SERVICE_UNAVAILABLE` | Upstream 5xx, network timeout, JSON decode failure, server-side onboarding misconfig (account missing `ext.network_code`), or polling timeout on async approval. | `transient` |
| `PERMISSION_DENIED` | Upstream 403, OR human approver rejected the order during HITL review. | `terminal` |
| `RATE_LIMITED` | Upstream 429. | `transient` |
| `AUTH_REQUIRED` | Upstream 401, missing tenant context, missing credentials. | `terminal` |
| `UNSUPPORTED_FEATURE` | Method exists on the Protocol but this upstream doesn't support it (e.g. `update_media_buy` against an upstream with no order-update endpoint). | `terminal` |
| `POLICY_VIOLATION` | Buyer's request fails a policy check upstream (brand-safety, traffic-quality). | `terminal` |
| `CONFLICT` | Upstream 409 (e.g. duplicate idempotency_key with a different body). | `terminal` |

**DO NOT raise on the wire**:

* `INTERNAL_ERROR` — SDK-internal allowlisted; the dispatcher uses it
  to wrap unhandled exceptions. Platform code MUST NOT emit it.
  Replace with `SERVICE_UNAVAILABLE` (transient) or `INVALID_REQUEST`
  (terminal).
* `AUTH_INVALID` — not in the spec enum. Replace with `AUTH_REQUIRED`.

Strict response validation rejects non-enum codes at boot, so the
translator can't accidentally ship a vendor code on the wire.

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

## Spec error codes — what to use

Adopters MUST emit only codes from the canonical
[error-code enum](https://adcontextprotocol.org/schemas/v1/enums/error-code.json)
on the wire. With strict response validation (the framework default),
non-enum codes fail validation and never reach buyers.

Two legacy SDK-internal codes used to leak through `AdcpError(...)`
calls in older translator code:

* `INTERNAL_ERROR` — **not in the spec enum**. The framework's
  dispatcher uses it internally to wrap unhandled exceptions, but
  platform code MUST NOT emit it directly. Replace with:

  * `SERVICE_UNAVAILABLE` (`recovery='transient'`) for upstream
    transient failures (5xx, network timeout, mock unreachable,
    JSON decode errors) and for server-side onboarding issues the
    buyer can't fix themselves (e.g. account is missing
    `ext.network_code`).
  * `INVALID_REQUEST` (`recovery='terminal'`) when the upstream
    rejects the translated payload (400) — the buyer needs to fix
    the request.

* `AUTH_INVALID` — **not in the spec enum**. Replace with
  `AUTH_REQUIRED` (`recovery='terminal'`) for missing or rejected
  bearer / `X-Network-Code` credentials.

The canonical codes the reference seller emits today:
`AUTH_REQUIRED`, `INVALID_REQUEST`, `PERMISSION_DENIED`,
`MEDIA_BUY_NOT_FOUND`, `CONFLICT`, `RATE_LIMITED`,
`SERVICE_UNAVAILABLE`, `POLICY_VIOLATION`, `UNSUPPORTED_FEATURE`,
`ACCOUNT_NOT_FOUND`. Anything outside the enum is a bug.

The valid `recovery` values are `'retry_with_changes'`,
`'correctable'` (legacy alias), `'transient'`, and `'terminal'`. The
string `'retry'` is **not** valid and will fail type-checking.

## CAPI semantic mismatch — perf feedback aggregates vs CAPI per-event ingest

AdCP `provide_performance_feedback` carries an aggregate over a
measurement window: `(media_buy_id, metric_type, value)` where
`metric_type` is one of `overall_performance`, `conversion_rate`,
`brand_lift`, `click_through_rate`, `completion_rate`, `viewability`,
`brand_safety`, `cost_efficiency`. CAPI (Google's Conversion API, the
GAM-flavored equivalent) ingests **per-event records**, not
aggregates.

The two shapes don't round-trip cleanly. The reference seller's
mapping accepts `metric_type='conversion_rate'` only — that's the
single AdCP metric whose semantics map even loosely onto CAPI
(a measured rate that can be projected as a single dedup'd event).
Other metric_types raise `INVALID_REQUEST` with a pointer to this
section rather than fabricating a synthetic event.

Adopters whose ad server has a richer feedback surface (Amazon's
`ProvidePerformanceFeedback`, FreeWheel's pacing-feedback API, or
in-house ML-feedback ingest) replace the projection with one that
preserves the aggregate semantics.

## What this seller doesn't yet support upstream

The JS mock-server is a deliberately minimal upstream. Some methods
on the `SalesPlatform` Protocol have no corresponding upstream
endpoint and the reference seller raises `UNSUPPORTED_FEATURE`
rather than fake the call:

* **`update_media_buy`** — the mock has no order-update endpoint.
  Real GAM has `LineItemService.performLineItemAction` (pause /
  resume / archive) plus per-line-item budget / flight updates;
  FreeWheel has `updateOrder` + `updatePlacement`. Wire your
  upstream's update flow into `update_media_buy` and remove the
  `UNSUPPORTED_FEATURE` shim.

Buyers calling these methods get a structured `UNSUPPORTED_FEATURE`
error with `recovery='terminal'`, so retries don't loop. Don't
ship the shim in production — wire your real upstream.
