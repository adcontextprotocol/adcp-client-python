# Migrating from `ADAPTER_REGISTRY` / `AdServerAdapter` to `PlatformRouter` + `DecisioningPlatform`

Audience: maintainers of [Prebid salesagent](https://github.com/prebid/salesagent)
or any multi-tenant sales agent shaped the same way — an
`ADAPTER_REGISTRY` dict mapping adapter slugs to subclasses of an
`AdServerAdapter` ABC, picked per-request from a `Tenant.ad_server_config.adapter`
field. This guide is a translation table. Where your code does `X`,
the SDK target is `Y`.

The honest summary: **your business logic stays. The framework absorbs
the cross-cutting concerns.** HITL gating, sandbox toggles, mock
fixtures, compliance scaffolding, error projection, idempotency,
webhook emission, lifecycle state assertions, credential handling,
connection pooling — those move from inside your adapter classes into
SDK primitives. The adapter body itself shrinks to one job: translate
AdCP wire shapes onto your upstream API and back.

> The implementation this guide migrates *to* lands in parallel PR
> [`bokelley/feat-platform-router`](https://github.com/adcontextprotocol/adcp-client-python/pull/477)
> (issue [#477](https://github.com/adcontextprotocol/adcp-client-python/issues/477)).
> The `PlatformRouter` recipe is shipped as an example first; once it
> proves out we promote it into `adcp.decisioning.dispatch`. Examples
> in this doc reference primitives that are already on `main` —
> `DecisioningPlatform`, `Account.mode`, `upstream_for(ctx)`,
> `assert_media_buy_transition`, `compose_method`, `UpstreamHttpClient`,
> the F12 webhook auto-emit. The router itself is the only piece
> arriving alongside this doc.

## The high-level shift

```
salesagent today                            adcp Python SDK target
─────────────────                           ─────────────────
ADAPTER_REGISTRY: dict[str, Type]           PlatformRouter({
  → instantiated per-request                    "tenant_acme":   GAMPlatform(...),
  → tenant.ad_server_config.adapter             "tenant_globex": BroadstreetPlatform(...),
  → AdServerAdapter ABC                     })

Per-adapter, hand-rolled today:             Per-platform, SDK-handled:
  HTTP client + pooling                  →  adcp.decisioning.UpstreamHttpClient
  HITL gating in __init__ + each method  →  compose_method + ShortCircuit
  Sandbox toggles per deployment         →  Account.mode = "sandbox"
  ~3000 LOC mock_ad_server.py            →  Account.mode = "mock" + mock_upstream_url
  Compliance scaffolding (ADCP_SANDBOX)  →  comply_test_controller gate (Phase 1)
  Webhook emission                       →  F12 auto-emit
  Lifecycle state checks per adapter     →  assert_media_buy_transition
  Error projection per adapter           →  AdcpError + UpstreamHttpClient projection
  Per-tenant credentials in config dict  →  ApiKey / StaticBearer / DynamicBearer
```

The dispatch model inverts. Today, the registry hands you a class and
you instantiate it per-request with the tenant's config. After
migration, platforms are long-lived instances; the router resolves
which one handles each call from the wire account ref.

## Foundations: how `Principal` maps onto SDK concepts

Before the section-by-section translation, one foundational shift to
internalise: salesagent's `Principal` model
(`salesagent/src/core/database/models.py:533`) is monolithic. A single
row carries two distinct concerns:

* **Agent identity** — the credential the requester presents (auth
  signing key, OAuth client, HTTP-Sig key id). "Who is making this
  call?"
* **Account context** — the buyer/advertiser the agent is acting on
  behalf of, and the seller's view of that relationship. "What are
  they operating on?"

The SDK splits these onto two separate primitives, both first-class:

* `BuyerAgent` (`adcp.decisioning.registry.BuyerAgent`) — the verified
  agent identity, plus billing-mode allowlist, status, default terms.
  Resolved by `BuyerAgentRegistry.resolve(auth_info)` from the verified
  principal.
* `Account` (`adcp.decisioning.types.Account`) — the resolved account
  the request is operating on. Carries `mode` (live / sandbox / mock),
  `tenant_id` in metadata for multi-tenant deployments, and any
  upstream-specific identifiers the platform needs. Resolved by
  `AccountStore.resolve(ref, auth_info)`.

This mirrors the JS SDK's separation, and it means salesagent's
`Principal` table maps onto **two** SDK lookups during migration:

* Build a `BuyerAgentRegistry` impl that returns `BuyerAgent` objects
  from the agent-identity columns of your `Principal` rows
  (`access_key_hash`, `oauth_client_id`, etc.). The framework consults
  this once per request to verify and resolve the agent.
* Build an `AccountStore` impl that returns `Account` objects from the
  account-context columns of the same `Principal` rows
  (`tenant_id`, the upstream advertiser/account id, mode flags). The
  framework consults this when a tool needs the resolved account.

Both stores can read from the same `Principal` rows. They project
different shapes onto two different boundaries — agent-resolution at
auth time, account-resolution at tool-dispatch time.

You don't need a schema migration to do this — both lookups can be
small wrappers over your existing principals table.

## What this migration adds, not just translates

Salesagent today implements a meaningful subset of AdCP 3.0. The
migration is part-port, part-upgrade. Some surfaces translate cleanly;
others require porting *and* extending; a few don't exist in salesagent
yet and become greenfield work during the port.

**Already in salesagent (translates cleanly):**

* The `AdServerAdapter` ABC pattern → `DecisioningPlatform` +
  `SalesPlatform`.
* Per-tenant adapter registry (`ADAPTER_REGISTRY` keyed by
  `tenant.ad_server_config.adapter`) → `PlatformRouter` keyed by
  `account.metadata['tenant_id']`.
* `manual_approval_required` HITL gating → `compose_method` +
  `ShortCircuit`.
* `Product.implementation_config: JSONType`
  (`models.py:256` and `effective_implementation_config`,
  `models.py:428-448`) → already the right shape; the SDK formalizes
  the seam through `get_products` / `create_media_buy`.

**Salesagent has a partial implementation (port AND extend):**

* **Creative.** `CreativeEngineAdapter`
  (`src/adapters/creative_engine.py`) exposes one method,
  `process_creatives`, returning approval status. AdCP 3.0 splits
  creative across two Platform Protocols: `CreativeAdServerPlatform`
  (associate creatives with line items, host them, generate tags,
  per-creative delivery) and `CreativeBuilderPlatform` (build/refine
  creative assets per brief, including refinement via
  `build_creative` with a referencing `creative_id`). Salesagent's
  shape maps onto a slice of `CreativeAdServerPlatform`;
  `CreativeBuilderPlatform` is greenfield. See §3.4.
* **Signals.** `src/core/tools/signals.py` is a global tool
  implementation — one function dispatches across all tenants,
  with `signals_agent_registry` providing per-tenant agent lookup
  but the surface itself is tool-level. AdCP 3.0 expects
  `SignalsPlatform` to be per-tenant, sitting behind the
  `PlatformRouter` like every other specialism. The business
  logic translates; the dispatch model changes. See §3.5.

**Salesagent is missing entirely (greenfield work during migration):**

* **`get_products` refine flow.** AdCP 3.0 supports multi-turn
  product discovery via `buying_mode='refine'` plus a `refine[]`
  array of scoped change requests. Salesagent's request schema
  inherits the field from the adcp library
  (`src/core/schemas/product.py:231`), but no adapter consults it —
  `_get_products_impl` is one-shot. The framework explicitly names
  the threaded flow (`adcp.decisioning.state.find_proposal_by_id`
  resolves a `proposal_id` "threaded across `get_products → refine →
  create_media_buy` without platform code"). Wiring the refine
  handler is greenfield. See §3.3.

This guide covers both — the translation table for what ports cleanly
and the explicit "this is new work" callouts for what salesagent
doesn't have today. The goal is an honest map of the territory, not
an aspirational re-skin of the existing surface.

## Translation table

### 3.1 `ADAPTER_REGISTRY` → `PlatformRouter`

**Before** — `salesagent/src/adapters/__init__.py:17`:

```python
ADAPTER_REGISTRY = {
    "gam": GAMAdapter,
    "google_ad_manager": GAMAdapter,
    "broadstreet": BroadstreetAdapter,
    "kevel": KevelAdapter,
    "mock": MockAdapter,
    "triton": TritonAdapter,
    "creative_engine": CreativeEngineAdapter,
}

def get_adapter(adapter_type: str, config: dict, principal):
    adapter_class = ADAPTER_REGISTRY.get(adapter_type.lower())
    if not adapter_class:
        raise ValueError(f"Unknown adapter type: {adapter_type}")
    return adapter_class(config, principal)
```

Each request fetches the tenant, reads `tenant.ad_server_config.adapter`,
looks up the class, and instantiates it with the tenant's config.

**After**:

```python
from adcp.decisioning import PlatformRouter, serve

router = PlatformRouter(
    accounts=salesagent_account_store,  # ONE AccountStore for the whole router
    platforms={
        "tenant_acme":   GAMPlatform(...),
        "tenant_globex": BroadstreetPlatform(...),
    },
)
serve(router, transport="both")
```

The router's `accounts=` is a **single** `AccountStore` — not one
per tenant. Each tenant doesn't manage its own list externally; the
store IS the cross-tenant index. Internally it reads from your
existing per-tenant `Principal` rows (which today live keyed under
`Tenant`), but it presents one unified `resolve(ref, auth_info)` API
to the router so the framework can dispatch without knowing your
table topology.

What `resolve` returns is an `Account` object whose
`metadata['tenant_id']` tells the router which platform to delegate
to. The router looks up `platforms[account.tenant_id]` and forwards
the call.

Platforms are constructed once, at process start, and reused for every
request. Connection pools, OAuth token caches, and any platform-level
state amortise across the platform's lifetime — the per-request
instantiation overhead in the registry pattern goes away.

Platforms are constructed once, at process start, and reused for every
request. Connection pools, OAuth token caches, and any platform-level
state amortise across the platform's lifetime — the per-request
instantiation overhead in the registry pattern goes away.

### 3.2 `AdServerAdapter` ABC → `DecisioningPlatform` + `SalesPlatform`

**Before** — `salesagent/src/adapters/base.py:174` and the Kevel
implementation at `salesagent/src/adapters/kevel.py:13`:

```python
class AdServerAdapter(ABC):
    capabilities: AdapterCapabilities = AdapterCapabilities()
    connection_config_class: type[BaseConnectionConfig] | None = BaseConnectionConfig
    product_config_class: type[BaseProductConfig] | None = None

    def __init__(self, config, principal, dry_run=False, creative_engine=None, tenant_id=None):
        # ... 30 LOC of audit logger init, principal id resolution,
        # manual_approval_required flag setup ...

    @abstractmethod
    def create_media_buy(self, request, packages, start_time, end_time, package_pricing_info=None):
        ...

    @abstractmethod
    def add_creative_assets(self, media_buy_id, assets, today):
        ...

    # ... 7 more abstract methods, each with positional-arg signatures
```

**After**:

```python
from adcp.decisioning import DecisioningPlatform, DecisioningCapabilities
from adcp.decisioning.specialisms import SalesPlatform
from adcp.decisioning.upstream import StaticBearer

class GAMPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://googleads.googleapis.com/v202405"

    capabilities = DecisioningCapabilities(
        specialisms=["sales-guaranteed", "sales-non-guaranteed"],
        # ... structured wire-spec capability blocks
    )

    def __init__(self, *, oauth_token: str) -> None:
        self._auth = StaticBearer(token=oauth_token)

    async def create_media_buy(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth)
        # adapter logic — translate AdCP req → GAM REST → AdCP response
        ...
```

Note: in **multi-platform mode behind a `PlatformRouter`**, individual
platforms do not declare `accounts = ...`. The router owns the single
`AccountStore`; child platforms receive the resolved `Account` via
`ctx.account` after the router has looked it up. This is different
from single-platform mode (`examples/v3_reference_seller/`), where
the platform itself declares `accounts = ...` because it's the only
platform serving requests.

What changes:

* **Method signatures collapse** to `async (req, ctx) -> response`. The
  request is a typed Pydantic model; `ctx` carries the resolved
  `Account`, `auth_info`, and request metadata. The
  positional-argument explosion (`packages`, `start_time`, `end_time`,
  `package_pricing_info`) becomes attributes on `req`.
* **The adapter declares its production URL once**, on `upstream_url`.
  Per-tenant routing flows through `ctx.account.metadata`; per-tenant
  credentials flow through `ctx.auth_info`. Sandbox / mock variants
  are handled by `Account.mode`, not by the adapter.
* **Capabilities live on the platform, not on the class hierarchy.**
  `DecisioningCapabilities` mirrors the AdCP wire spec one-to-one;
  `validate_platform()` confirms at boot that every declared
  specialism has the methods it requires.

The translator pattern (translate-AdCP-wire-onto-upstream-and-back)
stays intact. The Kevel adapter's `_validate_targeting` and
`_build_targeting` helpers (`salesagent/src/adapters/kevel.py:61`,
`:102`) port across unchanged — they're business logic. What
disappears is the `__init__` boilerplate, the abstract-method
ceremony, and the dry-run plumbing.

### 3.3 Product discovery and the refine flow

Salesagent already has the right *idea* for product config — the
`Product.implementation_config: JSONType` column carries
adapter-specific config (line item template id, ad unit ids, GAM
defaults), and the GAM tool reads it back at `create_media_buy` time
to drive the upstream call. The SDK formalizes that exact seam, plus
adds two surfaces salesagent doesn't have today: `get_products` as a
Platform method, and the multi-turn `refine` flow.

#### A. The `get_products` → `create_media_buy` seam (the impl_config plumbing)

**Before** — salesagent's flow at
`src/core/tools/media_buy_create.py:2431-2464`. After receiving a
`create_media_buy` request, the tool fetches the products by id,
auto-generates default `implementation_config` if missing
(GAM-specific path), validates it, and hands it to the adapter:

```python
catalog = get_product_catalog(tenant_id=identity.tenant_id)
product_ids = req.get_product_ids()
products_in_buy = [p for p in catalog if p.product_id in product_ids]

if adapter.__class__.__name__ == "GoogleAdManager":
    gam_validator = GAMProductConfigService()
    for schema_product in products_in_buy:
        if not schema_product.implementation_config:
            schema_product.implementation_config = (
                gam_validator.generate_default_config(...)
            )
        is_valid, error_msg = gam_validator.validate_config(
            schema_product.implementation_config
        )
        # ... persist auto-generated config back to DB ...
```

`Product.effective_implementation_config`
(`models.py:428-448`) is GAM-shaped today — the property name says
"GAM" in the docstring. The pattern is right; it's just specialized
to one adapter.

**After** — the SDK pins down the boundary:

* The wire `Product` (`adcp.types.Product`) is buyer-visible only:
  formats, pricing options, delivery type, properties. It does
  *not* carry `implementation_config`. Anything the adapter needs
  to drive its upstream stays seller-side.
* `SalesPlatform.get_products(req, ctx)` returns wire-shaped
  `Product` objects. The adopter's catalog table can keep the same
  `implementation_config: JSONType` column — it just doesn't cross
  the wire.
* `SalesPlatform.create_media_buy(req, ctx)` looks up
  `implementation_config` by `product_id` from the same table. The
  auto-generation + validation logic salesagent already has stays
  intact — it moves into the platform method and runs sync, in
  the same transaction.

```python
class GAMPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://googleads.googleapis.com/v202405"

    async def get_products(self, req, ctx):
        rows = self._catalog.list_for_tenant(ctx.account.metadata["tenant_id"])
        # Project DB rows → wire-shaped Product. impl_config stays in the row;
        # the wire object has only buyer-visible fields.
        return GetProductsResponse(
            products=[self._row_to_wire_product(r) for r in rows],
        )

    async def create_media_buy(self, req, ctx):
        for pkg in req.packages:
            row = self._catalog.get(pkg.product_id)
            impl_config = row.implementation_config or self._defaults_for(row)
            self._validate_impl_config(impl_config)
            # ... drive the upstream call with impl_config + ctx.auth_info
```

The salesagent-side migration is minimal: the auto-generation +
validation block at `media_buy_create.py:2431-2464` moves into the
platform's `create_media_buy`, and the dispatcher around it (the
`if adapter.__class__.__name__ == "GoogleAdManager"` switch) goes
away — each platform owns its own impl_config policy.

#### B. The refine flow

This is the part salesagent doesn't have. AdCP 3.0's
`GetProductsRequest` exposes `buying_mode` with three values:
`'brief'`, `'wholesale'`, `'refine'`. When `buying_mode='refine'`,
the request carries a `refine: list[GetProductsRefineEntry]` field —
each entry declares a scope (`'request'`, `'product'`, or
`'proposal'`) plus what the buyer is asking to change. The seller
responds with `refinement_applied: list[...]` matched by position,
echoing each scope/id and what it actually did. The full schema
lives at `adcp.types.GetProductsRequest` (generated from
`schemas/cache/3.0.0/media-buy/get-products-request.json`).

**Before** — salesagent
(`src/core/schemas/product.py:231` and
`src/core/tools/products.py:789-846`):

```python
class GetProductsRequest(LibraryGetProductsRequest):
    """Library provides: account, brand, brief, buyer_campaign_ref, catalog,
    context, ext, fields, filters, pagination, property_list, refine."""
    # ... no field overrides for refine; no consumer for it either
```

The library types include `refine`, so the wire payload deserializes
without error. But `_get_products_impl` ignores it — `get_products`
is one-shot. Buyers attempting refinement get the same broad
catalog every time.

**After** — the platform's `get_products` branches on `buying_mode`
and consults the `refine` array when present:

```python
async def get_products(self, req, ctx):
    if req.buying_mode == "refine":
        return await self._refine_products(req, ctx)
    return await self._fresh_products(req, ctx)

async def _refine_products(self, req, ctx):
    applied: list[RefinementApplied] = []
    products: list[Product] = []
    for entry in req.refine or []:
        match entry.scope:
            case "product":
                # Narrow within an existing product the buyer named
                products.append(self._narrow(entry.product_id, entry.changes))
                applied.append(RefinementAppliedProduct(
                    scope="product",
                    product_id=entry.product_id,
                    summary="...",
                ))
            case "proposal":
                # Adjust an outstanding proposal — see find_proposal_by_id
                proposal = ctx.find_proposal_by_id(entry.proposal_id)
                # ... apply changes to the proposal ...
            case "request":
                # Re-run the original brief with adjusted constraints
                ...
    return GetProductsResponse(
        products=products,
        refinement_applied=applied,
    )
```

The framework names this flow explicitly — the `find_proposal_by_id`
helper on the proposal store
(`adcp.decisioning.state.find_proposal_by_id`) "resolve[s] a
`proposal_id` threaded across `get_products → refine →
create_media_buy` without platform code." That's the integration
seam: a `proposal_id` returned from `get_products` rides through
subsequent `refine` calls and into `create_media_buy` without the
adopter wiring its own correlation table.

A pragmatic migration target: start by accepting `buying_mode='refine'`
and returning narrowed product lists based on simple filtering of the
`refine[]` entries. The richer multi-turn flow with proposals
(`adcp.types.Proposal` with lifecycle status `'draft'` /
`'committed'`) can come later. The wire spec describes proposals as
"actionable — buyers can refine them via follow-up `get_products`
calls within the same session, or execute them directly via
`create_media_buy`"
(`schemas/cache/3.0.0/media-buy/get-products-response.json`).

This is genuinely new work in salesagent. There's no existing code
path to translate; the migration is to add a refine handler beside
the existing brief handler.

### 3.4 Creative specialisms — `CreativeEngineAdapter` → two Protocols

Salesagent has a single creative ABC with one method:

**Before** — `src/adapters/creative_engine.py`:

```python
class CreativeEngineAdapter(ABC):
    """Abstract base class for creative engine adapters."""

    @abstractmethod
    def process_creatives(
        self, creatives: list[Creative]
    ) -> list[CreativeApprovalStatus]:
        """Processes creative assets, returning their status."""
        pass
```

That's the entire creative surface — approval status only. The GAM
adapter does its own creative association inline; from
`src/adapters/google_ad_manager.py:853` (`add_creative_assets`) and
`:921` (`associate_creatives`), creating creatives upstream and
binding them to line items happens directly inside the
`AdServerAdapter`, not behind a separate creative interface.

**After** — AdCP 3.0 splits creative across two Platform Protocols
that target different vendor archetypes:

* **`CreativeAdServerPlatform`**
  (`adcp.decisioning.specialisms.creative_ad_server`) covers the
  `creative-ad-server` specialism — Innovid, Flashtalking,
  GAM-creative, CMP-style platforms. Stateful library, per-creative
  pricing, ad-server tag generation, per-creative delivery
  reporting. Required methods: `build_creative`,
  `preview_creative`, `list_creatives`, `get_creative_delivery`.
  Optional: `sync_creatives`.
* **`CreativeBuilderPlatform`**
  (`adcp.decisioning.specialisms.creative`) covers
  `creative-template` (stateless transform — Bannerflow, Celtra) and
  `creative-generative` (brief-to-creative AI — Pencil, Omneky,
  AdCreative.ai). Single required method: `build_creative`.
  **Refinement is via `build_creative` itself**, called with a
  `creative_id` referencing the prior build — there is no separate
  `refine_creative` method (the `creative.py:24-30` docstring is
  explicit: an earlier port of a `refine_creative` method was
  caught as a hallucinated wire surface and dropped).

Salesagent's `process_creatives` maps onto a slice of
`CreativeAdServerPlatform.sync_creatives` — the approval-status
return shape is the closest analogue. The GAM-side inline
association code at `google_ad_manager.py:853` becomes the body of
`CreativeAdServerPlatform.build_creative` (or
`sync_creatives`, depending on whether the platform builds tags or
just persists pre-built assets).

A sketch of the port:

```python
from adcp.decisioning.specialisms import CreativeAdServerPlatform

class GAMPlatform(
    DecisioningPlatform,
    SalesPlatform,
    CreativeAdServerPlatform,
):
    capabilities = DecisioningCapabilities(
        specialisms=[
            "sales-guaranteed",
            "sales-non-guaranteed",
            "creative-ad-server",
        ],
        # ...
    )

    async def sync_creatives(self, req, ctx):
        # The body of process_creatives + associate_creatives ports here.
        # Approval status, line-item association, tag generation —
        # all on one typed surface.
        ...

    async def build_creative(self, req, ctx):
        # Library lookup OR upload + tag generation, depending on req shape.
        ...

    async def list_creatives(self, req, ctx):
        ...

    async def get_creative_delivery(self, req, ctx):
        # Per-creative pacing data — new surface for salesagent.
        ...
```

`CreativeBuilderPlatform` is greenfield for salesagent — there's no
existing brief-to-creative or template-transform code path to port.
A salesagent tenant that wants to claim
`creative-template` / `creative-generative` adds a new platform that
implements `build_creative` against an upstream creative-gen
service; it doesn't translate from existing code.

The minimal first pass: claim `creative-ad-server` only, port
`process_creatives` + `associate_creatives` into the four required
methods, leave `CreativeBuilderPlatform` for later.

### 3.5 Signals: tool-shaped → platform-shaped

Salesagent's signals surface is structurally different from AdCP 3.0
in a way the other migrations aren't. Today it's a **tool**;
tomorrow it's a **per-tenant platform** behind the router.

**Before** — `src/core/tools/signals.py` (`_get_signals_impl`):

```python
async def _get_signals_impl(
    req: GetSignalsRequest,
    identity: ResolvedIdentity | None = None,
) -> GetSignalsResponse:
    """Shared implementation for get_signals (used by both MCP and A2A)."""
    assert identity is not None, "identity is required for signals"
    tenant = identity.tenant
    if not tenant:
        raise AdCPAuthenticationError("No tenant context available")

    # Mock implementation - in production, this would query from a signal
    # provider or the ad server's available audience segments
    signals = []
    sample_signals = [
        Signal(signal_agent_segment_id="auto_intenders_q1_2025", ...),
        # ...
    ]
    return GetSignalsResponse(signals=signals)
```

One function dispatches across all tenants. Per-tenant agent lookup
exists in `src/core/signals_agent_registry.py` — the registry
returns tenant-specific signal-agent configs and
`ADCPMultiAgentClient` queries them — but the dispatch surface
itself is tool-level. Multi-tenant deployments end up with one
`get_signals` body that has to know about every tenant's signal
sources.

**After** — `SignalsPlatform`
(`adcp.decisioning.specialisms.signals`) is per-tenant, sitting
behind the same `PlatformRouter` as every other specialism. Two
methods:

* `get_signals(req, ctx)` — sync catalog discovery
* `activate_signal(req, ctx)` — sync provisioning onto destination
  platforms (with long-running activation pipelines surfacing state
  via `ctx.publish_status_change(resource_type='signal', ...)`)

```python
from adcp.decisioning.specialisms import SignalsPlatform

class AcmeSignalsPlatform(DecisioningPlatform, SignalsPlatform):
    upstream_url = "https://api.acme-data.example.com/v1"

    capabilities = DecisioningCapabilities(
        specialisms=["signal-marketplace"],
        # ...
    )

    def __init__(self, *, api_key: str) -> None:
        self._auth = ApiKey(header_name="X-Acme-Key", value=api_key)

    async def get_signals(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth)
        upstream = await client.get("/segments", params=...)
        return GetSignalsResponse(
            signals=[self._project(s) for s in upstream["segments"]],
        )

    async def activate_signal(self, req, ctx):
        # ... provision onto Snap/Meta/TikTok per req.deployments ...
```

In multi-tenant deployments, tenant A might run `LiveRampPlatform`,
tenant B might run `AdsquarePlatform`, tenant C might not claim
signals at all. The router dispatches on
`account.metadata['tenant_id']` and the buyer hits the right one
without any tool-level branching.

Tenants that don't claim signals leave the platform out of the
router entirely. Buyers calling `get_signals` against those tenants
get `UNSUPPORTED_FEATURE` from the framework — the
`validate_platform()` boot check ensures specialism declaration and
method presence stay in sync.

The migration is more structural than line-by-line:

1. Move the signal-resolution logic out of `core/tools/signals.py`
   into a `SignalsPlatform` impl per tenant that supports signals.
2. Replace the global tool dispatch with the router's
   `account_metadata` resolution (which you're already wiring for
   sales).
3. Drop `core/tools/signals.py` once every tenant that claimed
   signals has its own platform behind the router.

The `signals_agent_registry.py` lookup logic — discovering which
upstream signal agent serves a tenant — survives essentially intact;
it just lives inside the `SignalsPlatform.__init__` (or a
per-request `upstream_for` resolver) rather than inside the global
tool body.

### 3.6 HITL gating → `compose_method` + `ShortCircuit`

**Before** — `salesagent/src/adapters/base.py:226` plumbs the flag into
every adapter, and each adapter checks it inline. From
`google_ad_manager.py:267` and `:571`:

```python
class AdServerAdapter:
    def __init__(self, config, principal, ...):
        self.manual_approval_required = config.get("manual_approval_required", False)
        self.manual_approval_operations = set(
            config.get("manual_approval_operations", [...])
        )

    def _requires_manual_approval(self, operation: str) -> bool:
        return self.manual_approval_required and operation in self.manual_approval_operations

# in each adapter method:
if self._requires_manual_approval("create_media_buy") and not already_approved:
    return self._send_to_approval_queue(...)
```

The check is repeated in `create_media_buy`, `add_creative_assets`,
and `update_media_buy`. Three places to keep in sync.

**After**:

```python
from adcp.decisioning import compose_method, ShortCircuit

async def hitl_gate(req, ctx) -> ShortCircuit | None:
    if salesagent_requires_approval(ctx.account, req):
        # async approval — return a Submitted task envelope
        return ShortCircuit(value=ctx.handoff_to_task(send_to_approval_queue))
    return None  # falls through to the wrapped method

class GAMPlatform(DecisioningPlatform, SalesPlatform):
    create_media_buy = compose_method(
        inner=_create_media_buy_impl,
        before=hitl_gate,
    )
    add_creative_assets = compose_method(
        inner=_add_creative_assets_impl,
        before=hitl_gate,  # same gate, different method
    )
```

HITL becomes declarative, not embedded. One gate function composes
across every method that needs it; one place to update when the
approval policy changes; the inner method body stays focused on
upstream translation. `ShortCircuit` is a discriminated wrapper —
returning a bare value instead of `ShortCircuit(value=...)` raises
`TypeError` at runtime, so adopters porting middleware between
languages can't accidentally short-circuit with `None`.

### 3.7 Sandbox toggles → `Account.mode`

**Before** — sandbox is a deployment-level concern in salesagent. A
config dict carries the flag; each adapter (and the middleware in
front of them) consults it independently:

```python
# in adapter __init__ or inline:
if config.get("sandbox", False):
    self.use_sandbox_credentials = True
    self.base_url = SANDBOX_URL
```

This means `mode='sandbox'` is implicit, scattered, and trivially
spoofable from request data — which is the salesagent footgun the SDK
deliberately closes.

**After** — sandbox is a property of the resolved account:

```python
class SalesagentAccountStore:
    async def resolve(self, ctx) -> Account[TenantMetadata]:
        tenant = self._db.get_tenant(ctx.principal_id)
        return Account(
            id=tenant.account_id,
            mode="sandbox" if tenant.sandbox else "live",
            metadata=TenantMetadata(
                tenant_id=tenant.id,
                advertiser_id=tenant.advertiser_id,
                # ...
            ),
        )
```

The trust boundary shifts. `mode` lives on the account, which is
resolved from the authenticated principal — never from request data,
headers, or `ctx_metadata`. Buyers can't promote themselves into
sandbox by setting a flag; sandbox is what *the seller's* account
store says it is.

The framework's sandbox gate
(`adcp.decisioning.account_mode.assert_sandbox_account`) refuses
test-only surfaces (`comply_test_controller`, `force_*`, `simulate_*`)
on `mode='live'` accounts. Resolvers that spread untrusted input into
the resolved account leak this gate; the docstring on
`assert_sandbox_account` calls this out explicitly.

### 3.8 Mock fixtures → `Account.mode='mock'`

**Before** — `salesagent/src/adapters/mock_ad_server.py:53` is a
~1,800-LOC in-memory ad server. It implements every abstract method of
`AdServerAdapter` against a hand-rolled state dict, simulates lifecycle
transitions on a timer, and ships as part of the adapter registry
keyed `"mock"`.

```python
class MockAdServer(AdServerAdapter):
    adapter_name = "mock"
    # ... 1786 lines of in-memory state, scenario logic,
    # and lifecycle simulation ...
```

This is the biggest deletion in the migration. Mock-mode is now
SDK-handled.

**After** — populate `mock_upstream_url` on mock-mode accounts in your
`AccountStore.resolve`:

```python
class SalesagentAccountStore:
    async def resolve(self, ctx) -> Account[TenantMetadata]:
        tenant = self._db.get_tenant(ctx.principal_id)
        if tenant.is_dev_tenant:
            return Account(
                id=tenant.account_id,
                mode="mock",
                metadata=TenantMetadata(
                    tenant_id=tenant.id,
                    mock_upstream_url="http://localhost:4500",
                ),
            )
        # ... live path
```

The platform's adapter code is unchanged. `self.upstream_for(ctx)`
inspects `ctx.account.mode` and routes the underlying
`UpstreamHttpClient` at the mock fixture URL when `mode='mock'`,
without touching the adapter body. The mock fixture itself ships in
`@adcp/client` (`bin/adcp.js mock-server <specialism>`) and serves
deterministic per-specialism upstream-API responses.

The `mock_ad_server.py` module deletes wholesale. ~1,800 LOC of
in-memory state machine becomes a dev-time fixture URL on the account.

### 3.9 Compliance scaffolding → SDK `comply_test_controller` gate

**Before** — salesagent's compliance scenarios mix into the adapters
through environment toggles, seeded state, and per-adapter scenario
hooks. Adopters wire `ADCP_SANDBOX=1` or similar, then each adapter
keeps its own seeded state for the deterministic-testing surface.

**After** — adopters write nothing. The SDK's compliance gate (Phase
1, `adcp.decisioning.account_mode.assert_sandbox_account`) handles
authority:

* `mode="live"` → `comply_test_controller` raises `PERMISSION_DENIED`
  with `details.scope='sandbox-gate'`.
* `mode="sandbox"` or `"mock"` → call admits.
* Scenario state, if you want it, is managed by an SDK
  `TestControllerStore` rather than per-adapter seeded fixtures.

The bedrock invariant: deterministic-testing surfaces never fire on
production traffic, regardless of how the adopter's compliance code
is wired. The gate is the contract.

### 3.10 Lifecycle state machine

**Before** — each adapter encodes the legal state graph itself. Inline
checks scattered through `update_media_buy` and similar:

```python
if media_buy.status == "active" and new_status == "pending_creatives":
    raise BadStateError(...)
```

The graph drifts across adapters. A buyer hitting two tenants with
different lifecycle behaviour gets different errors for the same
illegal transition.

**After**:

```python
from adcp.decisioning import assert_media_buy_transition

async def update_media_buy(self, req, ctx):
    current = await self._upstream_get_status(req.media_buy_id, ctx)
    assert_media_buy_transition(
        from_state=current.status,
        to_state=req.target_state,
        media_buy_id=req.media_buy_id,
    )
    # ... proceed with the upstream update
```

The legal graph is the spec graph
(`adcp.decisioning.state_machines.MEDIA_BUY_TRANSITIONS`); every
platform refuses the same illegal transitions with the same
`INVALID_STATE` / `recovery='correctable'` error shape. Buyers get
consistent semantics across tenants without the adopter touching the
state-graph code at all.

The same module ships `assert_creative_transition` for the creative
lifecycle.

### 3.11 Webhook emission → F12 auto-emit

**Before** — each adapter (or per-tenant middleware) hand-rolls
webhook delivery: format the payload, sign it, fire the request, retry
on transient failures, log on permanent failures.

**After** — wire a `WebhookSender` (or `WebhookDeliverySupervisor`)
once on `serve(...)`. The framework auto-emits a sync-completion
webhook after every mutating tool call when the buyer registered a
`push_notification_config`:

```python
from adcp.webhook_sender import WebhookSender

serve(
    router,
    transport="both",
    webhook_sender=WebhookSender(...),
    # auto_emit_completion_webhooks defaults to True
)
```

The framework owns shape, signing, retry, and logged-and-swallowed
failure semantics. Adopters who want manual control inside a handler
pass `auto_emit_completion_webhooks=False` and emit themselves —
but the auto-emit path is the default, so most adopters delete their
webhook plumbing entirely.

### 3.12 Per-adapter HTTP client → `UpstreamHttpClient`

**Before** — every adapter wires its own httpx client, auth scheme,
retry policy, JSON parsing, and 404→None handling. From
`salesagent/src/adapters/kevel.py:42`:

```python
def __init__(self, config, principal, ...):
    super().__init__(...)
    self.api_key = self.config.get("api_key")
    self.base_url = "https://api.kevel.co/v1"
    self.headers = {"X-Adzerk-ApiKey": self.api_key, ...}

# ... per-method:
response = requests.post(f"{self.base_url}/...", headers=self.headers, json=payload)
if response.status_code == 404:
    return None
if response.status_code >= 400:
    raise BadRequestError(...)
```

Repeated across six adapters with subtle variations in error
projection, retry behavior, and auth header shape.

**After**:

```python
from adcp.decisioning.upstream import ApiKey

class KevelPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://api.kevel.co/v1"

    def __init__(self, *, api_key: str) -> None:
        self._auth = ApiKey(header_name="X-Adzerk-ApiKey", value=api_key)

    async def create_media_buy(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth)
        order = await client.post("/campaigns", json=payload)
        # client handles connection pooling, retry, 404→None,
        # and projects non-2xx responses → AdcpError automatically
```

Auth strategies (`StaticBearer`, `DynamicBearer`, `ApiKey`) are
declarative dataclasses. `DynamicBearer` accepts an async token
factory for OAuth refresh — the resolver runs per-request and can key
on `ctx.account.metadata` for per-tenant credentials. The
`UpstreamHttpClient` itself is pooled per `(base_url, auth)` on the
platform instance, so multi-tenant credential fan-out scales without
adapter-level connection management.

### 3.13 Error projection

**Before** — each adapter wraps upstream errors in custom error types,
then a translation layer maps those onto wire shapes:

```python
try:
    response = self._client.post(...)
except SomeUpstreamError as e:
    raise BadRequestError(...) from e
```

The mapping drifts; adopters periodically discover a code path that
projects a vendor error directly to the buyer.

**After** — `UpstreamHttpClient` projects HTTP errors to spec-conformant
`AdcpError` codes automatically. Non-2xx responses raise:

* `401` → `AUTH_REQUIRED` (`recovery='terminal'`)
* `403` → `PERMISSION_DENIED` (`recovery='terminal'`)
* `404` on resource ops → `MEDIA_BUY_NOT_FOUND` (or per-call override
  via `not_found_code` for creatives, forecasts, etc.)
* `409` → `CONFLICT` (`recovery='terminal'`)
* `429` → `RATE_LIMITED` (`recovery='transient'`)
* `5xx` / network timeout / JSON decode → `SERVICE_UNAVAILABLE`
  (`recovery='transient'`)
* `4xx` other → `INVALID_REQUEST` (`recovery='terminal'`)

Adopters rarely need to wrap. Strict response validation
(`ValidationHookConfig(responses='strict')`, the default) catches any
non-enum code at the wire — vendor codes can't accidentally ship.

## What NOT to migrate

A few things in the salesagent shape don't translate cleanly. They're
either out of scope or stay where they are:

* **Adapter `dry_run` flag** (`base.py:199`). Useful for the salesagent
  CLI; not a wire concept. Keep your dry-run flow behind your existing
  CLI/test entry points; don't try to thread it onto the platform.
* **`audit_logger` mixed into adapters** (`base.py:222`). The SDK
  ships an `AuditSink` Protocol (`adcp.audit_sink.AuditSink`) with
  `LoggingAuditSink` and `SlackAlertSink` reference impls. Wrap your
  existing audit logger as an `AuditSink` impl rather than calling
  `audit_logger.log(...)` inline in every method — the sink fires
  from one cross-cutting seam, so adapter bodies stop carrying audit
  scaffolding.
* **Tenant DB schema and admin UI**. The SDK doesn't touch your
  persistence model. The `Tenant` and `Principal` tables stay where
  they are. As covered in **Foundations** above, your `Principal`
  rows project onto two SDK lookups: a `BuyerAgentRegistry` for
  agent identity and an `AccountStore` for account context. Both
  read your existing tables; no schema migration required.
* **Per-adapter UI registration** (`base.py:478`). The SDK isn't a UI
  framework; if your admin UI registers per-adapter Flask routes,
  keep that wiring exactly as-is.

## Migration order

A path through the change that preserves a working server at every
step:

1. **Pick one adapter to port.** Two adapters in salesagent ship
   against real clients today: GAM
   (`salesagent/src/adapters/google_ad_manager.py`, where ~99% of
   clients run) and Broadstreet
   (`salesagent/src/adapters/broadstreet/`). Either works as a
   starting point. **Broadstreet** is the smaller, faster
   proof-of-concept; **GAM** is where the actual deployment value
   lives. Skip the rest — Kevel, Xandr, and Triton are scaffolding
   from earlier iterations with no client deployments today, and
   `MockAdServer` deletes entirely once `Account.mode='mock'` is
   wired. The mock adapter is the wrong starting point because it
   has no real upstream behaviour to validate against; pick GAM or
   Broadstreet so storyboard conformance lands against a real
   integration.
2. **Convert abstract methods one at a time** using
   `examples/v3_reference_seller/` as the template. Each method body
   shrinks: drop the `manual_approval_required` check, drop the
   custom error wrapping, drop the dry-run logging.
3. **Wire `upstream_url` + auth.** Declare the production URL on the
   class; pass an `ApiKey` / `StaticBearer` to the platform's
   `__init__`.
4. **Convert `Tenant.ad_server_config.adapter` lookup into an
   `AccountStore`** that returns `Account(id=..., mode=..., metadata=...)`
   with `tenant_id` in metadata. The store's `resolve` reads your
   existing tenant table; nothing else in your DB changes.
5. **Validate against the AdCP storyboards.** The
   [`media_buy_seller`](https://adcontextprotocol.org/storyboards) story
   is the wire-shape contract — if it passes, your translator is
   correct on the wire. Run it as your conformance test for each
   ported platform.
6. **Move HITL gates into `compose_method`.** One gate function,
   composed onto every method that previously checked
   `manual_approval_required`. Delete the inline checks.
7. **Port `CreativeEngineAdapter.process_creatives` into the
   platform's `CreativeAdServerPlatform` surface.** The
   approval-status return shape ports to `sync_creatives`; the
   inline `associate_creatives` code at
   `google_ad_manager.py:921` ports to `build_creative` (or
   `sync_creatives`, depending on whether your platform builds
   ad-server tags). Add `list_creatives` and
   `get_creative_delivery` — the latter is a new surface for
   salesagent (per-creative pacing data the existing GAM adapter
   doesn't expose).
8. **Move signals from `core/tools/signals.py` into a
   `SignalsPlatform` impl per tenant that supports signals.** Not
   every tenant will claim signals — only the ones that have a
   real upstream signal source (LiveRamp, Adsquare, etc.) wire a
   `SignalsPlatform` behind the router. The
   `signals_agent_registry` lookup logic survives intact; it moves
   inside `SignalsPlatform.__init__` or the per-request
   `upstream_for` resolver. Drop `core/tools/signals.py` once every
   signals-claiming tenant is on the platform.
9. **Add the `get_products` refine handler.** Greenfield. Branch
   `SalesPlatform.get_products` on `req.buying_mode`: existing
   one-shot logic stays the default; the `'refine'` branch consults
   `req.refine[]` and returns `refinement_applied[]` matched by
   position. Start with simple per-product narrowing; the richer
   proposal flow (`adcp.types.Proposal` lifecycle, `find_proposal_by_id`
   threading) can come later.
10. **Delete `mock_ad_server.py`** once `mode='mock'` is wired and
    the storyboard passes. ~1,800 LOC in one PR.
11. **Repeat for remaining adapters** (Broadstreet, Triton,
    `creative_engine`, GAM). GAM last — it's the largest, and the
    ported infrastructure from earlier adapters lets you focus the
    GAM port on the upstream-translation logic alone.
12. **Stand up `PlatformRouter`** over all platforms. Wire the
    router's `accounts` to your existing `AccountStore`; the
    per-tenant dispatch becomes automatic. This is the last step
    on purpose — each platform validates standalone (single-platform
    mode, `examples/v3_reference_seller/` shape) before you flip
    the router on.

At any point in steps 1–11 you can run the storyboard against the
ported tenants while the rest of the registry still serves the
unported tenants — there's no flag day.

## What this doesn't solve

A few things this migration deliberately doesn't address:

* **Multi-protocol bridging.** If salesagent translates AdCP requests
  across multiple buyer protocols (OpenRTB, Prebid Server's PBS-Java
  shape, etc.), that's a separate seam. The translator pattern here
  goes one direction: AdCP wire ↔ upstream API. Buyer-side protocol
  fan-out is a different problem.
* **Production performance characteristics.** The SDK hasn't been
  load-tested at salesagent's scale. `UpstreamHttpClient` connection
  pooling, the per-platform-instance auth caching, and the router's
  dispatch overhead all look reasonable on paper, but real-world
  latency budgets at salesagent scale are unproven.
* **The salesagent admin UI.** The SDK has no opinions about your
  management console. The `AdServerAdapter.register_ui_routes` hook
  (`base.py:478`) doesn't have a counterpart on `DecisioningPlatform`
  because it shouldn't — keep your Flask routes where they are.
* **The CAPI semantic mismatch.** `provide_performance_feedback`
  carries an aggregate; per-event upstreams (Google CAPI, GAM-flavored
  conversion ingest) need a projection that loses fidelity. The v3
  reference seller's `MIGRATION.md` covers this in detail and the same
  guidance applies here.

A few AdCP 3.0 surfaces are genuinely not in salesagent today, and
this guide flags them but doesn't fully scope the build. They're
gaps from the migration, not flaws in the SDK:

* **`CreativeBuilderPlatform` is greenfield.** Salesagent has no
  brief-to-creative or template-transform code path. A tenant that
  wants to claim `creative-template` / `creative-generative` is
  building a new platform, not porting one. §3.4 covers the
  Protocol shape; the upstream integration is the adopter's call.
* **The multi-turn refine flow on `get_products` is greenfield.**
  §3.3 covers the wire shape and the `find_proposal_by_id` threading
  hook. The first pragmatic pass is per-product narrowing; the full
  proposal lifecycle (`'draft'` → `'committed'`, expiry,
  inventory-reservation semantics) is a larger build.
* **Per-tenant signals dispatch is a structural change.** §3.5
  covers the move from one global tool body to per-tenant
  `SignalsPlatform` instances behind the router. The signals
  business logic translates; the dispatch model doesn't. Tenants
  that claim signals each get their own platform; tenants that
  don't get `UNSUPPORTED_FEATURE` from the framework.

## See also

* [`examples/v3_reference_seller/MIGRATION.md`](../v3_reference_seller/MIGRATION.md)
  — the single-platform translator pattern, with the full method-by-method
  port checklist for the v3 wire spec.
* [`docs/proposals/lifecycle-state-and-sandbox-authority.md`](../../docs/proposals/lifecycle-state-and-sandbox-authority.md)
  — the three-mode design (`live`/`sandbox`/`mock`) this guide leans on.
* [Issue #477](https://github.com/adcontextprotocol/adcp-client-python/issues/477)
  — the multi-platform proof, the `PlatformRouter` recipe, and the
  acceptance criteria the parallel implementation PR satisfies.
