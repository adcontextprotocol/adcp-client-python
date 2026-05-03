# Product architecture: layered model + two-platform composition

Status: **DRAFT** (conceptual scaffold). Foundational design for how the SDK
treats products, proposals, and the seam between them. Anchors the missing
prerequisite that the salesagent migration guide (PR #489) currently has
no answer for: *what is a product, actually, in this framework?*

This is a Python-first design. Once the picture is settled and reflected
in `adcp-client-python`, port to `@adcp/sdk` so cross-language semantics
stay aligned. Same pattern as the lifecycle-state-and-sandbox-authority
proposal (designed in JS, ported to Python). Direction reversed here
because the conceptual gap surfaced during the Python migration story.

> Sections marked **[citations pending]** await a follow-up pass against
> the salesagent and agentic-adapters reference codebases for concrete
> file:line examples. The conceptual scaffold here doesn't depend on those
> citations — it captures the layered model and the two-platform
> composition. Citations come second.

## Motivation

Today's `DecisioningPlatform` adopter contract treats products as a wire
concept: `get_products` returns a `Product[]` to the buyer; the adapter
ports its catalog onto that shape. Reviewer feedback (Brian O'Kelley,
salesagent author + AdCP spec author) on the migration guide surfaced
that this framing is incomplete. *"Products are actually a complex
internal data structure that publishers configure and that maps to
multiple things at multiple wire stages."*

Three concrete gaps the wire-only framing misses:

1. **Internal config plumbing.** Products carry adapter-specific
   configuration (line item template ids, ad unit ids, key-value
   targeting params, signal mappings) that's opaque to the buyer
   but essential at execute time. Salesagent calls this
   `implementation_config`. Every adopter writes this seam by hand.

2. **Capability overlap.** The wire schema lists capability flags
   (`pricing_options`, `delivery_type`, signal-targeting flavors,
   etc.); products selectively unlock subsets — these are
   buyer-configurable, those are publisher-locked. No framework
   primitive captures the overlap; adopters write per-product
   validation.

3. **The proposal workflow.** AdCP supports multi-turn product
   discovery (the `refine` flow, capability-gated by
   `buying_mode='refine'`), but the SDK has no first-class concept
   of *proposal* as a coherent buyer-state object. Salesagent has
   built a sophisticated proposal builder; the SDK provides no
   primitive for it.

This doc establishes the layered model that lets all three gaps be
addressed coherently, and proposes a **two-platform composition**
(`ProposalManager` + `DecisioningPlatform`) to anchor implementation
work.

## The four-layer model

Products are not a single thing. They live across four layers, each
with distinct ownership and stability characteristics:

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 1: Wire                                                    │
│    AdCP spec — get_products, create_media_buy, get_delivery       │
│    Buyer-visible. Stable contract.                                │
│    Owns: Product[], Package[], MediaBuy, DeliveryReport types     │
└──────────────────────────────────────────────────────────────────┘
                              ↑
                              │ thread typed config through
                              │
┌──────────────────────────────────────────────────────────────────┐
│  Layer 2: Product internal-config (the "recipe")                  │
│    Opaque to buyer. Typed-per-decisioning-platform.               │
│    Owns: implementation_config, key-value targeting, signal       │
│           mappings, reporting capability hints                    │
└──────────────────────────────────────────────────────────────────┘
                              ↑
                              │ filtered/projected by
                              │
┌──────────────────────────────────────────────────────────────────┐
│  Layer 3: Capability overlap                                      │
│    Which wire capabilities THIS product exposes for buyer         │
│    configuration vs. publisher-locked. Per-product subset of      │
│    the wire capability flags.                                     │
└──────────────────────────────────────────────────────────────────┘
                              ↑
                              │ assembled from
                              │
┌──────────────────────────────────────────────────────────────────┐
│  Layer 4: Supporting tables (publisher-side)                      │
│    Account, rate cards, internal audiences, properties,           │
│    placements, availability calendars.                            │
│    Entirely adopter-owned. SDK provides no primitives today.      │
└──────────────────────────────────────────────────────────────────┘
```

For each layer:

### Layer 1 — Wire

What the spec defines. Stable, versioned, validated by the SDK's
existing `validate_request` / `validate_response` pipeline. The SDK
ships `Product`, `Package`, `MediaBuy`, `DeliveryReport`, and the
request/response envelopes around them.

This layer is settled. No new framework concerns here.

### Layer 2 — Product internal-config (the "recipe")

The opaque-to-buyer JSON blob that threads from product setup through
proposal → product → media_buy → package → adapter at request time.

Brian's framing: *"there's just some blob of json — which ideally is
typed per adapter — that then the adapter can use to set targeting."*

What lives in the recipe:

* **Adapter-specific implementation hints** — line item template IDs
  (GAM), ad unit IDs (GAM), audience IDs (Meta), placement type
  bundles (LinkedIn). Each adapter has its own shape.
* **Key-value targeting parameters** — publisher-configured KV pairs
  that bake into ad calls. Targeting that the publisher commits to
  on behalf of every buyer using this product. Distinct from
  buyer-supplied targeting (which lives in the wire request).
* **Signal mappings** — when a buyer activates a signal, how does
  it map to upstream targeting? E.g., signal `auto_intender` →
  GAM key-value `intent=auto`. Per-product, because different
  products integrate signals differently.
* **Reporting capability hints** — which wire reporting fields the
  adapter can fulfill for *this* product. A guaranteed direct deal
  might report `viewable_impressions`; a programmatic remnant
  product might not. Per-product because the upstream reporting
  surface varies by product configuration.

The SDK's stance on this layer: doesn't care HOW the recipe is
assembled or threaded. Treats it as an opaque pipe. **But cares about
where it crosses Layer 1** (the seams) — reporting capability
declaration, capability overlap declaration, and the
`product_id → recipe` lookup at execute time.

The recipe is the contract between the two platforms. See
**The two-platform composition** below.

### Layer 3 — Capability overlap

Which wire capabilities are exposed for buyer configuration on
*this* product, vs. publisher-locked.

Concrete: AdCP wire defines `pricing_options` capability flags
(cpm, cpcv, etc.). A product might unlock `cpm` for buyer choice
but lock `cpcv` (publisher charges only CPM for this product).
The wire `Product` returned to the buyer should advertise only
the unlocked subset; the wire `create_media_buy_request` should
reject buyer choices outside the overlap.

Today this is hand-rolled per-adapter — every adopter writes the
same intersection logic. The SDK can ship this seam as a
capability-overlap declaration on the recipe; framework validates
buyer requests against it before adapter code runs. **Future
issue, not yet tracked in #491–#497.**

### Layer 4 — Supporting tables

Account, rate cards, internal audiences, properties, placements,
availability calendars. These are the data-model substrate
publisher/operator-side adopters maintain to assemble products.

Today entirely adopter-owned. The SDK provides no primitives for
these. They surface in the framework only indirectly — `Account`
appears in `AccountStore.resolve`, properties surface via
`PropertyListsPlatform` (for buyer-side authorization, not seller
inventory), but rate cards / internal audiences / availability are
adopter-internal black boxes.

**Open question** for future scoping: which of these warrant
framework primitives? Likely candidates:

* `RateCardStore` — pricing rules per buyer relationship
  per product. Currently adopter-internal; common shape across
  publishers.
* `AvailabilityStore` — capacity reservation for guaranteed
  inventory. Currently adopter-internal; the salesagent-side and
  social-side shapes differ enough to warrant separate scoping.
* `InternalAudienceStore` — publisher's audience taxonomy (vs.
  the buyer-facing audience signals exposed via `SignalsPlatform`).

Not designed in this doc. Flagged as the next domain after the
proposal workflow lands.

## The two-platform composition

Today the SDK has one platform shape: `DecisioningPlatform`. It conflates
two distinct concerns — *assembling proposals from briefs* vs.
*executing media buys against an upstream*.

The proposed split:

```
┌──────────────────────────────────────────────────────────────────┐
│  ProposalManager                                                  │
│    — receives buyer briefs (get_products, refine)                 │
│    — reads inventory + signals + rate cards + availability        │
│    — assembles products with typed recipes                        │
│    — produces proposals (collections of products + targeting +    │
│      pricing committed for THIS brief)                            │
│    — handles refine multi-turn lifecycle                          │
└────────────────────────┬─────────────────────────────────────────┘
                         │ Product carries implementation_config
                         │ (typed recipe per decisioning platform)
                         ↓
┌──────────────────────────────────────────────────────────────────┐
│  DecisioningPlatform                                              │
│    — receives create_media_buy / update / get_delivery            │
│    — reads recipe via product_id lookup                           │
│    — executes against upstream (GAM, Meta, Kevel, etc.)           │
│    — produces wire results                                        │
└──────────────────────────────────────────────────────────────────┘
```

The recipe (`implementation_config`) is the contract between the two.

### Three concrete shapes

**Shape 1 — Tight coupling (LinkedIn case).**

The same shop runs both. `LinkedInProposalManager` proposes LinkedIn
products; `LinkedInPlatform` executes against the LinkedIn API. Recipe
type is `LinkedInRecipe`, defined once, shared by direct import.

```python
class LinkedInRecipe(BaseModel):
    campaign_objective: Literal["LEAD_GEN", "AWARENESS", ...]
    placement_bundle: list[str]
    audience_ids: list[str]
    optimization_goal: str

class LinkedInPlatform(DecisioningPlatform):
    recipe_type: ClassVar[type[BaseModel]] = LinkedInRecipe

class LinkedInProposalManager(ProposalManager):
    def get_products(self, brief, ctx) -> ProposalsResponse:
        # imports LinkedInRecipe directly
        ...
```

Tenant config is one ProposalManager + one DecisioningPlatform.

**Shape 2 — Sophisticated proposal builder, multiple decisioning targets
(Prebid salesagent case).** [citations pending]

`PrebidProposalManager` is a complex builder — combines inventory,
signals, rate cards, availability — to produce recipes for *different*
DecisioningPlatforms depending on what the brief asks for. A single
brief might end up with packages routed to GAM (for guaranteed direct)
and Kevel (for non-guaranteed remnant), each with its own recipe type.

```python
class GAMRecipe(BaseModel):
    recipe_kind: Literal["gam"] = "gam"
    line_item_template_id: str
    ad_unit_ids: list[str]
    key_value_targeting: dict[str, list[str]]

class KevelRecipe(BaseModel):
    recipe_kind: Literal["kevel"] = "kevel"
    flight_id: str
    zone_ids: list[str]
    custom_targeting: dict[str, str]

class PrebidProposalManager(ProposalManager):
    def get_products(self, brief, ctx) -> ProposalsResponse:
        # Decides per-product whether to emit a GAMRecipe or KevelRecipe
        ...
```

Tenant config is one ProposalManager + a registry of DecisioningPlatforms
keyed by recipe kind.

**Shape 3 — Mock-backed (the v1 default for new adopters).**

The naive case is **already implemented** — by the mock seller backend
(`bin/adcp.js mock-server <specialism>`) the lifecycle-state work
introduced in Phase 2. The mock-server returns product fixtures with
trivial recipes; the SDK doesn't need to ship a separate
`SimpleCatalogProposalManager` reference impl.

**v1 of ProposalManager is just the wiring that forwards to the mock
backend.** Symmetric with DecisioningPlatform's `upstream_for(ctx)`
mock-mode dispatch: adopter declares a `mock_upstream_url`, framework
forwards `get_products` / `refine` to that endpoint, recipes flow back.
Same on-ramp pattern, applied to the proposal side.

```python
class MockProposalManager(ProposalManager):
    """v1 forwarder. Dispatches get_products / refine to a running
    bin/adcp.js mock-server. Adopters who don't yet have their own
    proposal logic point at this; the mock fixtures provide a working
    catalog with stub recipes."""
    capabilities = ProposalCapabilities(
        sales_specialism="sales-non-guaranteed",
    )
    mock_upstream_url: ClassVar[str | None] = None

    async def get_products(self, req, ctx):
        client = self.proposal_upstream_for(ctx)
        return await client.forward_get_products(req)
```

The on-ramp story: adopters who don't yet have proposal logic of their
own start with `MockProposalManager` pointed at the appropriate
mock-server specialism. Their first working seller agent runs against
the mock fixtures with zero adopter code on the proposal side. They
implement their own ProposalManager incrementally as they replace
mock-served slices with real assembly logic.

The two platforms run in independent modes:

| ProposalManager | DecisioningPlatform | Use case |
|---|---|---|
| Mock | Mock | Storyboard / conformance / cold-start dev |
| Mock | Live/sandbox | Adopter has their adapter ready before proposal logic |
| Live/sandbox | Mock | Adopter has proposal logic but no real upstream yet |
| Live/sandbox | Live/sandbox | Production |

This is the same pattern Phase 2 established for DecisioningPlatform
mock-mode, applied symmetrically to the proposal side. **What the SDK
ships as v1 is the framework wiring; the catalog content lives in the
mock-server.**

### How recipes are shared between the two platforms

Two technical paths, both viable:

**Path A — DecisioningPlatform exposes the recipe schema by import.**

```python
class GAMPlatform(DecisioningPlatform):
    recipe_type: ClassVar[type[BaseModel]] = GAMRecipe

# ProposalManager imports the type directly:
from .gam_platform import GAMRecipe

class MyProposalManager(ProposalManager):
    def get_products(self, brief, ctx):
        return ProposalsResponse(products=[
            Product(..., implementation_config=GAMRecipe(...).model_dump()),
        ])
```

Direct typed coupling. Simple in the LinkedIn case (one recipe type;
co-located code). Works in the Prebid case (multiple recipe types;
ProposalManager imports each).

**Path B — Discriminated-union recipe with a kind tag (recommended).**

```python
class Recipe(BaseModel):
    recipe_kind: str  # discriminator

class GAMRecipe(Recipe):
    recipe_kind: Literal["gam"] = "gam"
    # ...

# Tenant binds explicitly:
tenant_acme = TenantConfig(
    proposal_manager=PrebidProposalManager(...),
    decisioning_platforms={
        "gam":   GAMPlatform(...),
        "kevel": KevelPlatform(...),
    },
)
# Framework dispatcher routes packages by recipe.recipe_kind at create_media_buy
```

More explicit; supports the multi-decisioning case naturally; lets the
SDK validate the binding at tenant-setup time (`validate_platform`:
every `recipe_kind` a ProposalManager can emit must have a
corresponding DecisioningPlatform registered).

**Path B is recommended.** The kind tag makes routing structural
rather than type-import-based, and the validation catches
configuration drift at boot rather than at first request.

### What `ProposalManager` looks like (sketch)

```python
@runtime_checkable
class ProposalManager(Protocol):
    """Assembles proposals from buyer briefs.

    Reads inventory, signals, rate cards, availability. Produces
    proposals where each Product carries a typed implementation_config
    the bound DecisioningPlatform(s) can execute.

    Methods may be sync or async; the dispatch adapter detects via
    inspect.iscoroutinefunction.
    """

    capabilities: ClassVar[ProposalCapabilities]
    """What this ProposalManager can do — refine support, dynamic
    assembly, signal-driven products, etc."""

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext,
    ) -> MaybeAsync[GetProductsResponse]:
        """Initial product discovery from a buyer brief.

        Each returned Product MUST carry a typed
        ``implementation_config`` matching one of the bound
        DecisioningPlatform's recipe schemas.
        """
        ...

    # Optional refine surface — claimed by capability flag
    def refine_products(
        self,
        req: RefineProductsRequest,
        ctx: RequestContext,
    ) -> MaybeAsync[GetProductsResponse]: ...
```

`ProposalCapabilities` is **sales-axis-scoped** — proposal handling is
a sales-specialism concern, not a generic platform-wide concept. Two
flavors map to the existing AdCP sales specialisms:

```python
@dataclass
class ProposalCapabilities:
    # Major axis: which sales specialism this manager serves
    sales_specialism: Literal["sales-guaranteed", "sales-non-guaranteed"]

    # Sophisticated-assembly flags (typically only on guaranteed)
    refine: bool = False                  # supports buying_mode='refine'
    dynamic_products: bool = False        # signal-driven assembly
    rate_card_pricing: bool = False       # consults rate cards
    availability_reservations: bool = False  # reserves capacity at proposal time

    # Cross-cutting
    multi_decisioning: bool = False       # emits >1 recipe_kind
```

Two recognized flavors, naming matches the spec specialisms:

* **Guaranteed-mode proposal manager** — sophisticated assembly. Reads
  rate cards, reserves availability, supports refine, may assemble
  dynamic products from signals. The Prebid salesagent shape.
* **Non-guaranteed-mode proposal manager** — simple product
  enumeration. Returns products from a catalog with stub recipes;
  adapter does the upstream work. The naive on-ramp shape.

Adopters declare which they implement via `sales_specialism`. The
SDK's v1 reference impl is `MockProposalManager` (forwards to
`bin/adcp.js mock-server`); adopters opting into guaranteed proposal
flow implement their own ProposalManager.

(Other AdCP specialisms — creative, signals, governance — don't have
a proposal-shaped lifecycle; they don't need a ProposalManager.)

### What `DecisioningPlatform` keeps

Mostly unchanged from today. The methods that consume the recipe gain
a typed parameter:

```python
class DecisioningPlatform(...):
    capabilities: ClassVar[DecisioningCapabilities]
    accounts: ClassVar[AccountStore]
    upstream_url: ClassVar[str | None] = None
    recipe_type: ClassVar[type[Recipe] | None] = None  # NEW

    async def create_media_buy(self, req, ctx) -> CreateMediaBuySuccess:
        # Per package: framework has already looked up the recipe by
        # package.product_id and validated it against self.recipe_type.
        # Adapter consumes the typed recipe directly:
        for package in req.packages:
            recipe: GAMRecipe = ctx.recipes[package.product_id]
            # adapter executes against upstream using recipe.line_item_template_id, etc.
```

Framework responsibilities at the seam:

* Look up `recipe = product.implementation_config` by `product_id`
* Validate against `recipe_type`
* Inject into `ctx.recipes` for adapter consumption
* If `recipe_type` is None, treat the recipe as opaque
  `dict[str, Any]` (back-compat for adopters who haven't migrated)

## The proposal workflow

Where the layers play out across a proposal lifecycle:

```
1. Buyer sends GetProductsRequest with brief
   ↓
2. ProposalManager assembles proposal:
   - Reads supporting tables (Layer 4): rate cards, audiences, availability
   - Applies capability overlap (Layer 3): which wire capabilities this
     product exposes for THIS brief
   - Builds recipe (Layer 2): typed config the bound DecisioningPlatform
     will execute
   - Returns wire response (Layer 1): Product[] with implementation_config
   ↓
3. (optional) Buyer refines via RefineProductsRequest
   - ProposalManager re-assembles narrowed proposal
   - Same layered flow
   ↓
4. Buyer sends CreateMediaBuyRequest referencing product IDs
   - Framework looks up recipes by product_id (the seam)
   - Validates packages against capability overlap
   - Routes per-package to the right DecisioningPlatform (in multi-
     decisioning shape) by recipe_kind
   - DecisioningPlatform executes against upstream
   ↓
5. Buyer polls GetMediaBuyDelivery
   - DecisioningPlatform consults the recipe's reporting capability hints
     to know which wire fields it can fulfill
   - Framework validates the response against declared capability
```

Where the SDK helps today vs. where it doesn't:

| Step | SDK helps | Adopter still owns |
|---|---|---|
| 1 | request validation | proposal assembly logic |
| 2 | recipe shape (Path B kind tag) | inventory / rate cards / availability |
| 3 | refine handler scaffold (#496) | narrowing logic |
| 4 | recipe lookup by product_id (#497) | upstream execution |
| 5 | get_delivery wire shape | which fields adapter can produce |

The seams marked "SDK helps" are the issues already filed in #491–#497.
The seams marked "adopter still owns" are the candidates for future
framework primitives — `RateCardStore`, `AvailabilityStore`,
`InventoryStore`, etc. — which Layer 4 of the model names but does
not yet design.

### What about `accept_proposal`?

Open question. AdCP wire today has `get_products` (with refine) and
`create_media_buy`. There's no explicit "accept proposal" handshake;
the buyer's `create_media_buy` referencing the proposal's product
IDs implicitly accepts.

A future SDK primitive could surface proposal acceptance as its own
state transition (lock pricing, reserve availability, etc.) — but
that requires a wire-level proposal lifecycle which the spec doesn't
have today. **Out of scope for this doc.**

## Concrete examples — publisher-side and social-side

[citations pending — agent follow-up]

This section will walk through:

* **Publisher-side (salesagent shape)**: GAM example. Static product
  catalog with `implementation_config` (line item templates, ad units,
  key-value pairs). Dynamic product assembly from signal agents
  (`src/services/dynamic_products.py` shape — currently
  salesagent-invented, a candidate for future SDK primitive). Tenant +
  Principal + Property + RateCard table relationships.

* **Social-side (agentic-adapters shape)**: Meta as the canonical
  example. Different recipe content (campaign objective, ad set
  targeting, audience IDs from Meta business manager). Different
  supporting-table structure (account hierarchies, conversion event
  mappings, optimization goals).

The two examples should establish: the LAYERS are the same, the
CONTENT differs. The SDK's job is to formalize the layers without
prescribing the content.

## The seams where the SDK helps

Inventory of seams + status. Each seam is independently scopable; the
issues already filed (#491–#497) cover most of the immediate-term
items. The architecture-level seams below are the candidates for the
next round of issue filing.

### Already filed (#491–#497)

* `fields` projection on get_products responses (#492)
* Cursor-based pagination helper (#493)
* `property_list` filter — intersect buyer list against product
  properties (#494)
* `time_budget` deadline + `incomplete[]` projection (#495)
* `refine[]` flow scaffold (#496)
* `implementation_config` lookup helper for `create_media_buy` (#497)

These are all wire-facing helpers — every adopter would write the same
code for each. They live above the layer model (Layer 1 ↔ Layer 2 seam).

### Candidate future issues (architecture-level)

These come out of the four-layer model + two-platform composition.
**Not yet filed; flagged here so they fit the bigger picture.**

* **`ProposalManager` Protocol.** Formalize the second platform
  shape. Capabilities, surface methods (`get_products`,
  `refine_products`), tenant binding to DecisioningPlatform(s) by
  `recipe_kind`. Big issue — requires design + breadth.

* **`Recipe` discriminated-union base + `recipe_kind` routing.**
  Implementation detail of Path B. Should land alongside
  `ProposalManager`.

* **`MockProposalManager` (v1 default).** Forwarder that dispatches
  `get_products` / `refine` to the running `bin/adcp.js mock-server`
  for the relevant specialism. Symmetric with DecisioningPlatform's
  Phase 2 mock-mode dispatch. Doesn't ship a separate static-catalog
  impl — the mock-server already provides product fixtures, so v1
  ProposalManager work is just the framework wiring.

* **Capability-overlap declaration on Recipe.** Layer 3 seam.
  Recipe carries a `capability_overlap: ProductCapabilityOverlap`
  field; framework validates buyer requests against it before
  adapter code runs.

* **Reporting-capability declaration on Recipe.** Same shape as
  capability overlap, on the reporting axis. Recipe declares which
  delivery wire fields it can fulfill; framework validates
  `get_media_buy_delivery` requests + provides default null/empty
  fallbacks for fields the adapter can't produce.

* **`InventoryStore` / `SignalStore` for dynamic-product
  assembly.** Salesagent's `dynamic_products.py` pattern. Currently
  adopter-invented; the SDK could ship primitives that
  `ProposalManager` consults at request time. Open question — would
  benefit from a second adopter doing this independently before the
  SDK takes a position.

* **`RateCardStore`** (Layer 4). Pricing rules per buyer
  relationship per product. Common shape across publishers but
  varies enough that the SDK should carefully scope before
  prescribing.

* **`AvailabilityStore`** (Layer 4). Capacity reservation for
  guaranteed inventory. Publisher-side and social-side shapes
  differ; warrants separate scoping.

These don't all need to land. Some may belong as adopter patterns,
not framework primitives. The architecture model gives a vocabulary
for arguing about which.

## What this doc does NOT propose

* **Specific implementations beyond the conceptual sketch.** The
  `ProposalManager` Protocol shape above is a sketch, not a final
  API. Implementation issues file the actual contract.

* **Spec changes.** No wire-shape proposals here. Where the doc
  notes a missing wire concept (e.g., explicit proposal acceptance),
  it flags it as out-of-scope rather than proposing.

* **Decisions on which Layer 4 primitives ship.** Names the
  candidates; defers to per-primitive scoping.

* **How the proposal workflow's state lives in `TaskRegistry`.**
  Open question — the existing `TaskHandoff` model handles async
  task lifecycles; whether proposals are tasks or a separate
  lifecycle warrants its own design pass.

* **Naming.** `ProposalManager` confirmed (Brian: *"could be an
  agent, a platform, whatever"*). The name is deliberately neutral
  on what kind of thing implements it.

## Migration impact

The migration guide (PR #489) currently has a §3.3 "Product
discovery and the refine flow" section that's *under-scoped* against
this picture. It treats products as a wire concern + an opaque
`implementation_config` lookup. After this design lands:

* §3.3 should be updated to introduce the two-platform composition.
  Adopters porting from `ADAPTER_REGISTRY` should understand they're
  splitting their existing adapter code along the proposal /
  decisioning seam, not just porting it as one block.

* §3.5 (signals) and the open architectural question about
  `inventory_store` / `signal_store` resolves into "this is a
  ProposalManager concern; SDK may or may not ship these
  primitives, but they live on the proposal side, not the
  decisioning side."

* §3.7 (governance) is unaffected — governance enforcement is at
  the wire-tool level, orthogonal to the proposal/decisioning split.

The migration guide doesn't need to wait for `ProposalManager` to
ship; it needs to acknowledge the model so adopters reading it
understand the seam they'll be migrating across.

## Cross-implementation note

This is a Python-first design. Once the picture is settled and
`ProposalManager` lands as a working SDK primitive in
`adcp-client-python`, port to `@adcp/sdk` so JS adopters get the
same composition. Direction reversed from the lifecycle-state
proposal because this conceptual gap surfaced during the Python
migration story.

The wire layer (Layer 1) is shared by definition — both SDKs
implement against the same AdCP spec. Recipe shapes (Layer 2) are
adopter-defined either way. The `ProposalManager` Protocol itself
needs identical shape across SDKs; we'll mirror the JS port pattern
already established for `DecisioningPlatform` and the lifecycle-state
work.

## Open questions

1. **Tenant binding model.** Path B above proposes
   `decisioning_platforms: dict[recipe_kind, DecisioningPlatform]`.
   Does this fit cleanly into the existing `PlatformRouter`
   (multi-platform-per-process work in #477), or does it need a new
   binding type? The two are orthogonal — `PlatformRouter` routes
   by tenant; proposal-side binding routes by recipe_kind within a
   tenant — but they may share a conceptual seam worth unifying.

2. **Default `recipe_kind` for legacy adopters.** Adopters who
   haven't yet migrated to typed recipes pass opaque
   `dict[str, Any]`. Default `recipe_kind = "legacy"`? Or require
   explicit migration? Affects deprecation cycle.

3. **Where does dynamic-product assembly live in the SDK contract?**
   Salesagent has it; agentic-adapters social shapes might or might
   not. If the SDK ships `InventoryStore` + `SignalStore` primitives,
   they live on `ProposalManager`. But shipping them requires more
   adopter signal than one codebase. Defer until a second adopter
   independently implements the pattern.

4. **Reporting capability declaration: per-product on the recipe,
   or per-platform on `DecisioningCapabilities`?** Probably
   per-product — different products on the same platform may have
   different upstream reporting fidelity. But adds recipe-shape
   complexity. Worth a focused design pass.

## References

* **Reviewer feedback that prompted this doc** — comments on PR #489
  (migration guide). Key threads: §3.3 implementation_config plumbing,
  §3.5 signals architectural question, §3.7 governance scope, and
  the meta-comment about "things the SDK can do to help users not
  build all these utilities."

* **Salesagent reference codebase** — `prebid/salesagent`. Key files
  for this doc (citations to land in follow-up pass): `src/core/database/models.py`,
  `src/core/tools/products.py`, `src/services/dynamic_products.py`,
  `src/core/tools/media_buy_create.py`, the GAM and Kevel adapters.

* **agentic-adapters reference codebase** — 12 social/programmatic
  adapter packages plus `shared`. Key packages for this doc
  (citations follow-up): `packages/shared`, `packages/adapter-meta`,
  `packages/adapter-tiktok`, `packages/adapter-google`.

* **Existing SDK proposals**:
  `docs/proposals/decisioning-platform-dispatch-design.md` — the
  current `DecisioningPlatform` design (post-Round-4 review).
  `docs/proposals/v3-identity-bundle-design.md` — identity model.
  This doc fits between them in the design hierarchy.

* **Issues #491–#497** — buyer-side request-shape helpers, the
  immediate implementation work that fits within the layered model
  established here.

* **Migration guide PR #489** — the doc this prerequisite enables.
