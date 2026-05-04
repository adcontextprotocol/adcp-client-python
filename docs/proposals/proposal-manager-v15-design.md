# ProposalManager v1.5 design — proposal lifecycle, capability validation, recipe persistence

Status: **DRAFT** — design for review before implementation. v1
(PR #504) shipped the Protocol skeleton + per-tenant binding +
`MockProposalManager` forwarder. v1.5 ships the *lifecycle* the
skeleton can't validate today: session cache, `finalize` transition
with `expires_at` + optional HITL, capability-overlap declaration on
`Recipe` + framework validation, recipe persistence through the buy
lifecycle.

The end-state v1.5 unlocks: a mock platform claiming
`sales-proposal-mode` + `media_buy.supports_proposals: true` passes
`media_buy_seller/proposal_finalize.yaml` and
`media_buy_seller/refine_products.yaml` end-to-end through the SDK,
and an adopter writes minimal-LOC code (target ≤ 350 LOC for a
working proposal-mode mock — see § "Storyboard pass criteria + LOC
budget") to do it.

This is a Python-first design. Once settled, port to `@adcp/sdk` so
the cross-language semantics stay aligned. Same direction as the
architecture doc and the v1 PR — proposals live first in the Python
SDK, JS follows.

## Motivation

The architecture doc (`docs/proposals/product-architecture.md`) §
"Proposal lifecycle: `finalize` is the acceptance handshake"
identifies four framework responsibilities at the proposal seam:

* **Session cache for in-flight proposals** — recipes for draft
  proposals live in framework state keyed by `proposal_id`; refine
  iterations update the cache; finalize promotes the entry to a
  persisted store.
* **`finalize` transition handling** — detect `buying_mode='refine'`
  + `refine[i].action='finalize'`, lock pricing, set `expires_at`
  hold window, optionally route to HITL approval, persist the
  committed proposal.
* **`expires_at` enforcement** — `create_media_buy(proposal_id)`
  after the hold window expired returns a structured error;
  framework handles this without adapter participation.
* **Recipe persistence through the buy lifecycle** — once a proposal
  is accepted via `create_media_buy`, the recipe persists.
  `update_media_buy` / `get_delivery` / `pause_media_buy` calls
  hydrate the same recipe from storage.

To which v1.5 adds, completing Layer 3 of the four-layer model:

* **Capability-overlap declaration on `Recipe` + framework
  validation** — `Recipe` carries a `capability_overlap` declaring
  which wire capabilities the buyer can configure on this product.
  Framework validates buyer requests against it before adapter code
  runs.

The storyboard gap that drives the urgency:
`static/compliance/source/specialisms/sales-proposal-mode/index.yaml`
declares `requires_scenarios: media_buy_seller/proposal_finalize`
(plus six others). Today no SDK-built mock seller can pass that
storyboard end-to-end — `MockProposalManager` forwards `get_products`
verbatim to the mock-server, but there's no framework path that
recognizes `refine[i].action='finalize'`, locks pricing, sets
`expires_at`, or persists the committed proposal so a subsequent
`create_media_buy(proposal_id=...)` resolves cleanly. Adopters
attempting to claim `media_buy.supports_proposals: true` would have
to hand-roll the entire lifecycle today.

The salesagent reference codebase confirms the gap from the other
direction. Surveying `Developer/salesagent/.conductor/tallahassee-v8`
(latest worktree) for proposal-related state:

* `src/core/database/models.py` — no `Proposal` table; the only
  hits for "proposal" are conversation-history docstrings on the
  `Context` model (line 1651: *"this just tracks the conversation
  history for clarifications and refinements"*).
* `src/core/tools/products.py` — no `proposal_id`, no `finalize`,
  no `expires_at` handling. Line 756 just notes that
  `implementation_config` is excluded from `model_dump()`.
* `src/core/schemas/product.py` (lines 224-242) — `GetProductsRequest`
  widens `buying_mode` from `Literal['wholesale']` to `str | None`
  *"so callers aren't forced into a single mode"*. Refine and brief
  modes pass through as plain strings; no dispatch by mode.
* `src/core/tools/media_buy_create.py` — no `proposal_id` parameter
  acknowledged anywhere; `create_media_buy` consumes a `packages[]`
  array, not a proposal reference.
* `grep -rn "proposal_id" src/` returns zero matches.

salesagent stores `implementation_config` on the products table
(`gam_implementation_config_schema.py:4`, `gam_inventory_service.py:1206-1211`,
`mock_ad_server.py:1377-1424`) and reads it at execute time. It
does **not** persist committed proposals, hold inventory, lock
pricing, or accept `proposal_id` on `create_media_buy`. Every adopter
faces the same gap; v1.5 exists to close it.

The contrast with `MediaBuyStore` (already shipped at
`src/adcp/decisioning/media_buy_store.py:1-80`) is instructive: that
module solved the same shape of problem for `targeting_overlay` echo
on `get_media_buys` — opt-in framework wiring that gates a wire-spec
contract on the seller's declared specialisms. v1.5's `ProposalStore`
is the symmetric move on the proposal axis.

## Decisions

### D1. Session cache shape — adopter-supplied `ProposalStore` Protocol with an in-memory reference

**Decision:** ship a `ProposalStore` Protocol (mirroring `MediaBuyStore`
at `media_buy_store.py:62`) that adopters supply per-tenant via the
existing `PlatformRouter` (`platform_router.py:294-307`). The framework
ships `InMemoryProposalStore` as the reference implementation —
non-durable, suitable for development, gated behind the same
`ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`-style escape hatch as
`InMemoryTaskRegistry` (see `task_registry.py:131-141`). Production
adopters wire a durable backing (Postgres / Redis / their existing
proposal table) by implementing the Protocol.

The cache is **adopter-owned, framework-managed**: the framework
calls store methods at the right lifecycle moments; the adopter
controls persistence semantics, eviction, multi-process visibility.

```python
@runtime_checkable
class ProposalStore(Protocol):
    """Per-tenant proposal lifecycle persistence.

    Methods may be sync or async — the framework awaits at call time
    (mirrors MediaBuyStore at media_buy_store.py:80).
    """

    is_durable: ClassVar[bool]
    """Drives the production-mode gate. False for InMemoryProposalStore;
    True for adopter-supplied durable backings."""

    # Lifecycle: draft (in-flight) → committed (post-finalize) → consumed
    # (post-create_media_buy). The framework drives every transition.

    def put_draft(
        self,
        *,
        proposal_id: str,
        account_id: str,
        recipes: Mapping[str, Recipe],   # product_id → concrete Recipe subclass
        proposal_payload: Mapping[str, Any],   # the wire Proposal shape
    ) -> MaybeAsync[None]:
        """Store / replace a draft proposal. Refine iterations call
        this with the same proposal_id to overwrite."""
        ...

    def get(
        self,
        proposal_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> MaybeAsync[ProposalRecord | None]:
        """Cross-tenant safety mirrors TaskRegistry.get
        (task_registry.py:251-268). Return None on tenant mismatch,
        not the raw record."""
        ...

    def commit(
        self,
        proposal_id: str,
        *,
        expires_at: datetime,
        proposal_payload: dict[str, Any],   # locked-pricing wire shape
    ) -> MaybeAsync[None]:
        """Promote draft → committed. Idempotent on re-call with
        equal expires_at + payload; raises on conflict (committing
        a proposal already at a different expires_at is a developer
        bug)."""
        ...

    def mark_consumed(
        self,
        proposal_id: str,
        *,
        media_buy_id: str,
    ) -> MaybeAsync[None]:
        """Called by the framework after a successful
        create_media_buy(proposal_id=...) path. Adopter typically
        keeps the row but marks it as terminal so subsequent
        create_media_buy calls get a structured 'already accepted'
        error rather than racing."""
        ...

    def discard(self, proposal_id: str) -> MaybeAsync[None]:
        """Rollback path — symmetric with TaskRegistry.discard
        (task_registry.py:270-290). Used when finalize allocation
        succeeds but the persisted commit raises before projection."""
        ...
```

`ProposalRecord` is a typed dataclass carrying `proposal_id`,
`account_id`, `state`, `recipes`, the wire `Proposal` payload, and
`expires_at: datetime | None` (None while draft, set on commit):

```python
@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    account_id: str
    state: Literal["draft", "committed", "consumed"]
    recipes: Mapping[str, Recipe]
    proposal_payload: Mapping[str, Any]   # the wire Proposal shape
    expires_at: datetime | None = None    # set on commit
    consumed_by_media_buy_id: str | None = None   # set on mark_consumed
    recipe_schema_version: int = 1        # see D3 — recipe migration story
```

`recipe_schema_version` is captured at `put_draft` time to support
durable-store migrations. v1.5 ships at version 1; adopters whose
`Recipe` subclasses gain required fields later bump the schema and
write a migration (or evict pre-bump records). The framework reads
the field but doesn't enforce — adopter posture.

**Eviction model.** Drafts are short-lived; committed proposals are
durable until `expires_at` + a grace window. The Protocol does not
prescribe eviction — adopters decide. The reference
`InMemoryProposalStore` evicts drafts older than 24h (configurable
via constructor) and committed proposals 7 days past `expires_at`.
Production adopters typically scope retention to their compliance /
audit posture.

**Multi-process visibility.** The framework does not prescribe a
shared cache. The Protocol's *contract* is "writes from any worker
are visible to subsequent reads from any worker". Adopters running
single-process get this for free with the in-memory ref;
multi-process deployments wire a durable backing (this is the
salesagent posture today for everything else they persist —
SQLAlchemy-backed, multi-worker-safe). The framework's only
obligation: never assume one ProposalStore instance is process-local.

**Why not TaskRegistry?** Tempting to reuse — both store
account-scoped IDs with a state machine. Three reasons it's separate:

1. **Lifecycle shape differs.** TaskRegistry's terminal state is
   `completed` / `failed` once. Proposals have *three* states with
   distinct semantics: `draft` (mutable, refine iterates),
   `committed` (immutable + `expires_at` enforcement), `consumed`
   (post-acceptance). Forcing this onto TaskRegistry's
   `submitted/working/completed/failed` enum loses the
   semantically-important `draft → committed → consumed` transitions.
2. **Refine iteration is a write, not a state transition.** Each
   `buying_mode='refine'` iteration overwrites the draft's recipes
   and proposal payload. TaskRegistry has no "overwrite progress"
   semantics — `update_progress` merges, doesn't replace. Modeling
   refine as a series of progress updates muddles both contracts.
3. **`expires_at` semantics differ.** TaskRegistry has its own
   `expires_at` (`registry.py:95` on the buyer-agent OAuth side; the
   task registry's expiry is a TTL). Proposal `expires_at` is a
   *seller-issued inventory hold window* with wire-level visibility.
   The buyer reads it; the framework enforces it on
   `create_media_buy`. Different concept, same word.

Better to ship a separate `ProposalStore` Protocol that mirrors
`MediaBuyStore`'s shape (which is also separate from TaskRegistry,
for similar reasons) than to overload TaskRegistry.

**Per-tenant binding.** Extend `PlatformRouter`'s constructor with
`proposal_stores: Mapping[str, ProposalStore] | None = None`. Same
shape as the existing `proposal_managers=` kwarg
(`platform_router.py:294`); same orphan-key validation
(`platform_router.py:347-352`). Single-tenant adopters use a
one-entry router; multi-tenant adopters wire one store per tenant
(salesagent's posture: one SQLAlchemy-backed store per tenant
schema). When a tenant has no proposal_store wired, the framework
declines to dispatch finalize / persistence paths for that tenant
and surfaces `UNSUPPORTED_FEATURE` on `refine[i].action='finalize'`
requests.

**Auto-allocate `InMemoryProposalStore`?** No. When a tenant wires a
finalize-capable `ProposalManager` but no `ProposalStore`, the framework
raises at construction (see D5's cross-store consistency check). The error
names the exact kwarg to add:

```
ValueError: Tenant 'default' wired a ProposalManager declaring
finalize=True, but no ProposalStore was registered for that tenant.
Wire one via proposal_stores={"default": InMemoryProposalStore()}, or
remove the finalize capability.
```

Auto-allocate-with-warning was considered and rejected. Multi-worker
deployments (gunicorn, uvicorn workers, k8s replicas) that silently get
an in-memory store lose every in-flight proposal at the first worker that
didn't see `put_draft` — a debugging session, not a startup warning. The
30 seconds of copy-paste from a hard error is the better trade. Aligns with
the `ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1` posture for
`InMemoryTaskRegistry` (`task_registry.py:131-141`).

**Awaiting `MaybeAsync[None]`.** Adopters returning either coroutines or
plain values from store methods is the same pattern as `MediaBuyStore`. The
framework awaits via the `_await_maybe` helper in `media_buy_store.py:100-109`;
v1.5's lifecycle code reuses it (don't roll a new bridge).

### D2. Finalize lifecycle — both sync and async surfaces, capability-declared

**Decision:** `ProposalManager` v1.5 declares finalize support via two
new methods, both optional, capability-gated. The names below are chosen
to disambiguate from Python's `async def` — both methods accept either
coroutines or plain returns via `MaybeAsync`, so the suffixes describe
the *protocol semantic* (handoff or no handoff), not the call shape.

* `finalize_proposal` — inline commit. Returns the committed proposal
  in the same response. Suitable for sellers whose pricing + inventory
  hold can be issued without human review (programmatic remnant,
  rate-card-driven guaranteed where the capacity check is deterministic).
* `finalize_proposal_with_handoff` — HITL handoff. Returns
  `TaskHandoff[FinalizeProposalSuccess]` (the existing pattern from
  `decisioning-platform-dispatch-design.md` § D6 et seq.); the buyer
  receives a `Submitted` envelope with `task_id`; the human approval
  flow lands the committed proposal via the standard TaskRegistry
  completion path, and the buyer either polls `tasks/get` or receives
  a webhook (per the existing `webhook_emit.py` pattern at lines 1-39).

Capability declaration on `ProposalCapabilities`:

```python
@dataclass(frozen=True)
class ProposalCapabilities:
    sales_specialism: SalesSpecialism

    refine: bool = False
    finalize: bool = False               # NEW — D2 (inline commit)
    finalize_with_handoff: bool = False  # NEW — D2 (HITL handoff)
    expires_at_grace_seconds: int = 0    # NEW — D3 (post-expiry slack)

    # Existing (v1):
    dynamic_products: bool = False
    rate_card_pricing: bool = False
    availability_reservations: bool = False
```

`multi_decisioning` from v1 is dropped from new declarations per
Resolutions §6 — the framework stops reading it. Existing v1 adopters
who set it pass through harmlessly.

Adopter declares whichever they support; the framework validates that
the corresponding method is implemented at `serve()` time (mirrors
the `_is_method_overridden` walk from
`decisioning-platform-dispatch-design.md` § D3). Adopters MAY declare
both — when both are declared, the framework dispatches to
`finalize_proposal` (inline) by default. This matches the storyboard
(`proposal_finalize.yaml` asserts inline commit), is the boring choice,
and mirrors how adopters first validate against the spec. Sellers who
want per-tenant or per-request handoff dispatch override by raising
`AdcpError("HUMAN_REVIEW_REQUIRED", recovery="handoff")` from
`finalize_proposal` — the framework re-dispatches to
`finalize_proposal_with_handoff` for that request.

**Why both surfaces?** The compliance YAML scenarios cover both:

* `protocols/media-buy/scenarios/proposal_finalize.yaml` (lines
  156-200) is sync-flavoured: the test sends
  `refine[{action: finalize}]`, expects `proposals[0].proposal_status:
  committed` + `expires_at` *in the same response*. No A2A
  Submitted task envelope is asserted.
* `specialisms/sales-guaranteed/index.yaml` (lines 254-331) shows
  the HITL pattern on `create_media_buy` (not finalize), where the
  seller returns a `Submitted` task and the human approval flow
  lands the committed buy via `push_notification_config`. The
  `sales-proposal-mode` storyboard inherits `proposal_finalize` (sync)
  but real publishers running guaranteed inventory frequently *do*
  require human IO sign-off before the inventory hold is real — the
  `expires_at` window doesn't bind until a human approves.

Modeling only the sync path forces HITL-shaped sellers to either
fake a sync commit (bad — pricing isn't actually locked yet) or
reject finalize entirely (bad — the storyboard fails). Both surfaces
exist because both shapes exist in production.

**Wire-level finalize routing.** The dispatcher's existing
`get_products` shim handles the routing decision. When all three
hold —

1. request has `buying_mode='refine'`
2. any `refine[i].action == 'finalize'`
3. tenant has a wired `ProposalManager` declaring `finalize` or
   `finalize_with_handoff`

— the framework intercepts before calling `refine_products`. It
extracts the `proposal_id` from the entry, hydrates the draft from
the `ProposalStore`, and calls the relevant `finalize_proposal*`
method. Result projection:

* `finalize_proposal` return → wire response with the committed
  `Proposal` and `expires_at`; framework calls
  `proposal_store.commit(...)` before returning.
* `TaskHandoff` return (from `finalize_proposal_with_handoff`) →
  existing dispatch path
  (`decisioning-platform-dispatch-design.md` § D7) projects
  `Submitted`. The handoff fn, when it completes, calls
  `proposal_store.commit(...)` via a framework helper exposed on the
  `TaskHandoffContext` (`task_registry.py:470`).

```python
class ProposalManager(Protocol):
    # ... v1 methods (get_products, refine_products) ...

    def finalize_proposal(
        self,
        req: FinalizeProposalRequest,   # NEW — see below
        ctx: RequestContext[Any],
    ) -> MaybeAsync[FinalizeProposalSuccess]: ...

    def finalize_proposal_with_handoff(
        self,
        req: FinalizeProposalRequest,
        ctx: RequestContext[Any],
    ) -> MaybeAsync[
        FinalizeProposalSuccess
        | TaskHandoff[FinalizeProposalSuccess]
    ]: ...
```

`FinalizeProposalRequest` is a framework-internal type carrying
the resolved draft: `proposal_id`, the hydrated `recipes` dict, the
buyer's per-entry refine `ask` (e.g., "lock pricing on the CTV
allocation"), and the parent `GetProductsRequest`. Adopter doesn't
parse the wire envelope; the framework projects.

`FinalizeProposalSuccess` carries `expires_at: datetime` and the
committed `Proposal` payload (the spec wire shape). Framework
threads the result back into the parent `get_products` response —
the wire scenarios assert `proposals[0]` on the response, so the
projection lands it there.

**Inline-vs-handoff dispatch** (resolved per Brian's review): the seller
declares which finalize surfaces it supports via
`ProposalCapabilities.finalize` / `finalize_with_handoff`. The
framework dispatches to whatever the seller declared. **Time-budget
is NOT an inline/handoff signal** — Brian: *"i don't think time budget
is necessarily a signal for sync vs async — i think a fast async
beats a slow sync!"*

When a seller declares only one mode, dispatch is unambiguous. When a
seller declares both, the framework calls `finalize_proposal` (inline)
by default — matches the storyboard, is the boring choice, gives the
adopter a single mental model on first run. The seller escalates to
the handoff path for a specific request by raising
`AdcpError("HUMAN_REVIEW_REQUIRED", recovery="handoff")` from
`finalize_proposal`; the framework catches that exact code and
re-dispatches to `finalize_proposal_with_handoff`.

If the spec eventually adds a per-request `finalize_mode` hint on
`refine[i]`, the framework reads it and lets the buyer drive the
choice; until then, the seller's declaration + escalation pattern
governs.

### D3. Recipe persistence — `ProposalStore` for committed-but-unconsumed; `MediaBuyStore` extended for consumed

**Decision:** the recipe lives in two stores across its lifecycle:

* **`ProposalStore`** — owns the recipe from `put_draft` (initial
  `get_products`) through `commit` (finalize) until `mark_consumed`
  (post-`create_media_buy`).
* **`MediaBuyStore`** — owns the recipe for the duration of the buy.
  Extend `MediaBuyStore` (`media_buy_store.py:62`) with
  `recipes` alongside the existing overlay-echo methods:

```python
@runtime_checkable
class MediaBuyStore(Protocol):
    # Existing (shipped):
    async def persist_from_create(self, ...) -> None: ...
    async def merge_from_update(self, ...) -> None: ...
    async def backfill(self, ...) -> ...

    # NEW — D3:
    async def persist_recipes(
        self,
        media_buy_id: str,
        *,
        account_id: str,
        recipes: Mapping[str, Recipe],   # product_id → concrete Recipe subclass
    ) -> None: ...

    async def hydrate_recipes(
        self,
        media_buy_id: str,
        *,
        expected_account_id: str | None = None,
    ) -> Mapping[str, Recipe]: ...
```

**Why split between two stores?** The recipe's *visibility window*
differs across lifecycle stages:

* During `draft` and `committed` it's keyed by `proposal_id` and
  scoped to the proposal lifecycle. Buyer references a proposal_id;
  framework looks up via `ProposalStore.get`.
* After `create_media_buy(proposal_id)` it's keyed by `media_buy_id`
  and scoped to the buy lifecycle. Buyer references a media_buy_id
  on every subsequent `update_media_buy` / `get_delivery` /
  `pause_media_buy`; framework looks up via `MediaBuyStore.hydrate_recipes`.

Routing recipe lookups through the wrong key (`MediaBuyStore` for a
draft proposal; `ProposalStore` for an active buy) would either
require dual-key lookups (cost: every store roundtrip costs two
hits) or copy-by-write semantics that drift. Cleaner to scope each
store to one lifecycle stage.

**Hand-off at `create_media_buy(proposal_id=...)`.** Framework
sequence:

1. Validate `proposal_id` exists, is `committed`, hasn't expired.
   Hydrate the proposal from `ProposalStore.get`.
2. Validate the buyer's `create_media_buy_request` against the
   recipe's `capability_overlap` declaration (D4).
3. Call `DecisioningPlatform.create_media_buy(req, ctx)` with
   `ctx.recipes` populated from the proposal.
4. On success: call `MediaBuyStore.persist_recipes(media_buy_id,
   account_id=..., recipes=...)`, then call
   `ProposalStore.mark_consumed(proposal_id, media_buy_id=...)`.

The `ctx.recipes` field on `RequestContext` is **new for v1.5** —
typed as `Mapping[str, Recipe]` (product_id → concrete `Recipe`
subclass). Adapters down-cast to their declared subclass:

```python
class GAMRecipe(Recipe):
    line_item_template_id: int
    creative_placeholder_ids: list[int]

# In the adapter:
recipe = cast(GAMRecipe, ctx.recipes[package.product_id])
template_id = recipe.line_item_template_id   # typed; mypy + Cursor happy
```

Typing as `Mapping[str, Recipe]` rather than `dict[str, Any]` keeps
adapters in the typed lane — autocomplete on recipe fields, mypy
catches typos, agent-generated adapters don't hallucinate keys.

Today the framework threads `implementation_config` via
`Product.implementation_config` on the get_products response;
v1.5 adds `ctx.recipes` as the explicit threading point on every
downstream call where the recipe matters. Adapter code reads
`ctx.recipes[package.product_id]` rather than rummaging through
the request.

**Subsequent buy operations.** On `update_media_buy(media_buy_id,
...)`, `get_media_buy_delivery(media_buy_ids=[...])`,
`pause_media_buy(media_buy_id, ...)`, the framework hydrates
recipes via `MediaBuyStore.hydrate_recipes(media_buy_id)` before
dispatching to the platform method, attaches them to `ctx.recipes`,
and the adapter consumes the same shape it saw on `create_media_buy`.
Framework restart mid-buy is durable as long as `MediaBuyStore` is
durable (production adopters wire SQLAlchemy / equivalent).

**Restart safety.** The combination of durable `ProposalStore` +
durable `MediaBuyStore` means the framework can crash and restart
between `finalize` and `create_media_buy` (proposal_store has the
committed proposal + `expires_at`) and between `create_media_buy`
and the next `update_media_buy` (media_buy_store has the recipes).
Adopters running the in-memory references lose state on restart —
acceptable for development, gated for production via the
durability flag.

**Open question** (flagged for review): should `MediaBuyStore` be
split into two Protocols (`OverlayStore` + `RecipeStore`) for
separation-of-concerns, or extended in place? **Recommended
posture:** extend in place. Adopters already wire one
`MediaBuyStore` per platform; forcing them to wire two for the
same media_buy_id is friction without value. The two surfaces
(overlay echo, recipe hydration) both key on `media_buy_id` and
both serve the same lifecycle; co-locating them is a feature, not
a coupling problem.

### D4. Capability-overlap on `Recipe` — adopter declares, framework validates pre-adapter

**Decision:** `Recipe` (`recipe.py:62`) gains a `capability_overlap`
field carrying a typed declaration of which wire capabilities the
buyer can configure on this product. Framework validates buyer
requests against it before calling adapter code; mismatches surface
as `INVALID_REQUEST` with `field` pointing at the offending wire
path.

```python
class Recipe(BaseModel):
    capability_overlap: CapabilityOverlap | None = None
    # ... subclass adds typed implementation fields ...

@dataclass(frozen=True)
class CapabilityOverlap:
    """Per-product subset of wire capability flags. Buyer requests
    asking for capabilities outside this overlap are rejected by
    the framework before the adapter sees them.

    Each field is None | frozenset[str]:
      - None      → framework does not gate this axis (legacy / open).
      - frozenset → buyer choices must be subsets of this set.
                    frozenset() (empty) means deny-all on this axis.

    The None vs empty-set distinction matches Python set intuition:
    'no constraint' is None; 'allowed set is empty' is frozenset().
    """

    pricing_models: frozenset[str] | None = None
    """Subset of wire pricing_models the buyer can choose."""

    targeting_dimensions: frozenset[str] | None = None
    """Subset of wire targeting dimensions (geo, device_type, etc.)."""

    delivery_types: frozenset[str] | None = None
    """guaranteed | non_guaranteed subset."""

    signal_types: frozenset[str] | None = None
    """If the seller integrates signals, which signal types this
    product accepts. frozenset() means seller explicitly refuses
    all signals on this product; None means no framework gate."""

    # Reserved for future extension. Adopters with novel capability
    # axes file a tracking issue rather than ad-hoc adding fields.
    extras: Mapping[str, frozenset[str] | None] = field(default_factory=dict)
    """Adopter-private namespaced extensions. Framework ignores;
    adopter-side validators can read for custom gating."""
```

**Validation seam — pre-adapter.** When the framework dispatches
`create_media_buy(proposal_id=...)` and hydrates the proposal:

1. Pull `recipes` from `ProposalStore.get`.
2. For each `package` in the buyer's request, look up
   `recipe = recipes[package.product_id]`.
3. If `recipe.capability_overlap` is not None, walk the package's
   request shape and reject any field whose value lies outside the
   declared overlap. Per axis: `None` skips the gate; an explicit
   `frozenset` (including the empty set) is enforced.
   * `package.pricing_option_id` → if `capability_overlap.pricing_models`
     is set, the matching `PricingOption.pricing_model` must be a member.
   * `package.targeting_overlay` keys → if
     `capability_overlap.targeting_dimensions` is set, must be a subset.
   * etc.
4. Reject with `INVALID_REQUEST` and a `field` path pointing at
   the offending wire location:

```python
raise AdcpError(
    "INVALID_REQUEST",
    message=(
        f"Buyer requested pricing_model={requested!r} on package "
        f"{package.product_id!r}, but this product's recipe declares "
        f"capability_overlap.pricing_models={overlap!r}. The seller "
        "did not enable that pricing model for this product."
    ),
    recovery="terminal",
    field=f"packages[{i}].pricing_option_id",
)
```

**Why pre-adapter, not in-adapter?** Three reasons:

1. **Consistency.** Every adopter writes the same intersection logic
   today. The architecture doc § Layer 3 names this exact seam:
   *"every adopter writes the same intersection logic; the framework
   should own this"* (`product-architecture.md:179-184`).
2. **Error projection uniformity.** `INVALID_REQUEST` with `field`
   path matching the wire schema is the framework's existing
   validation pattern (mirrors the `validate_request` pipeline). An
   in-adapter check produces ad-hoc errors; the framework gate
   produces buyer-facing errors that look like the rest of the
   validation surface.
3. **Lower adapter LOC.** Pre-adapter validation is the single most
   leveraged piece of v1.5 for the LOC budget (D.G). Each
   capability axis the framework gates is an `if`-block the adapter
   doesn't write.

**`capability_overlap = None` is back-compat.** v1 adopters whose
recipes don't declare overlap get the v1 behaviour (no framework
gating; adapter validates if it wants to). v1.5's new code path
*activates* when the field is set — additive, never breaking.

**Reporting capability is a separate axis.** The architecture doc
flags reporting-capability declaration as future work
(`product-architecture.md:733-740`); v1.5 keeps that out of scope
to avoid scope-creep. Recipe carries `capability_overlap` for
configuration-time gating; reporting-capability declaration lands in
v1.6.

**Open question** (flagged for review): should `capability_overlap`
also gate `update_media_buy` requests (where the buyer changes
package config mid-flight)? **Recommended posture:** yes, for
symmetry. The buyer can't escape the overlap by deferring it to
update; the framework re-runs the same validation on every
`update_media_buy` against the recipes hydrated from
`MediaBuyStore`. Pure additive — no v1 adopter currently runs
in-adapter overlap checks for `update_media_buy`, so framework
gating doesn't displace anything.

### D5. Tenant binding — additive, two new optional kwargs on `PlatformRouter`

**Decision:** extend `PlatformRouter`'s constructor with two new
optional kwargs:

```python
class PlatformRouter(DecisioningPlatform):
    def __init__(
        self,
        *,
        accounts: AccountStore[Any],
        platforms: Mapping[str, DecisioningPlatform],
        capabilities: DecisioningCapabilities,
        proposal_managers: Mapping[str, ProposalManager] | None = None,  # v1
        # NEW — v1.5:
        proposal_stores: Mapping[str, ProposalStore] | None = None,
        media_buy_stores: Mapping[str, MediaBuyStore] | None = None,
    ) -> None:
        ...
```

`media_buy_stores` is also new at the router level — `MediaBuyStore`
exists in v1 (`media_buy_store.py`) but is wired today via direct
attribute assignment on the platform instance
(`platform.media_buy_store = create_media_buy_store(...)`, per
`media_buy_store.py:25-27`). For the multi-tenant router, that
shape forces adopters to mutate child platforms after construction.
Lifting both stores to constructor kwargs gives a single
configuration surface.

**Orphan-key validation** mirrors v1's `proposal_managers`
(`platform_router.py:347-352`):

```python
if self._proposal_stores:
    orphans = set(self._proposal_stores) - set(self._platforms)
    if orphans:
        raise ValueError(
            f"proposal_stores keys must be a subset of platforms "
            f"keys; orphan tenant_id(s): {sorted(orphans)}"
        )
# Same for media_buy_stores.
```

**Cross-store consistency check.** A tenant that wires a
`ProposalManager` declaring `finalize` or `finalize_with_handoff` MUST
also wire a `ProposalStore` (the framework can't run finalize without
somewhere to commit the proposal). The router validates this at
construction:

```python
for tenant_id, manager in self._proposal_managers.items():
    caps = manager.capabilities
    needs_store = caps.finalize or caps.finalize_with_handoff
    if needs_store and tenant_id not in (self._proposal_stores or {}):
        raise ValueError(
            f"Tenant {tenant_id!r} wired a ProposalManager declaring "
            f"finalize={caps.finalize!r}, "
            f"finalize_with_handoff={caps.finalize_with_handoff!r}, "
            f"but no ProposalStore was registered for that tenant. "
            f"Wire one via proposal_stores={{{tenant_id!r}: "
            "InMemoryProposalStore()}}, or remove the finalize capabilities."
        )
```

This catches the most common configuration mistake at boot rather
than at first finalize request. Adopters running a non-finalizing
ProposalManager (catalog mode) wire no store; adopters running
finalize wire one explicitly.

**Single-tenant adopters.** Same one-entry-router pattern as v1
(see `examples/hello_proposal_manager.py:15-21`). Wire
`{"default": ...}` for each map; same code path, no branching:

```python
router = PlatformRouter(
    accounts=...,
    platforms={"default": MyPlatform()},
    proposal_managers={"default": MyProposalManager()},
    proposal_stores={"default": InMemoryProposalStore()},
    media_buy_stores={"default": create_media_buy_store(MyMediaBuyStore(), ...)},
    capabilities=...,
)
```

**Why not auto-wire defaults?** Tempting to have the router
allocate `InMemoryProposalStore()` for any tenant that has a
finalize-capable manager but no store. Risks:

* Hides the durability decision from adopters — they don't realize
  they're running non-durable in production until something crashes.
* Conflicts with the `ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1`
  precedent for `InMemoryTaskRegistry` (`task_registry.py:131-141`).

Better posture: explicit wiring required, with a `UserWarning` at
boot when in-memory stores are used (per D1). Production adopters
get the hard error via the existing production gate; development
adopters get the mock-style on-ramp via explicit wiring of the
in-memory ref.

### D6. Backward compatibility — v1.5 is purely additive

**Decision:** every v1.5 surface is opt-in. v1 adopters who don't
touch the new APIs see zero behavioural change.

The compatibility matrix:

| Adopter posture | v1.5 behaviour |
|---|---|
| No `proposal_managers` wired (v0 single-platform) | Identical to v1. `get_products` dispatches to platform; nothing else triggers. |
| `proposal_managers` wired, no `finalize*` capability declared | Identical to v1. Refine routes via existing `refine_products`; framework doesn't intercept finalize because the manager doesn't claim it. |
| `proposal_managers` wired, `finalize` or `finalize_with_handoff` declared, no `proposal_stores` wired | **Hard error at construction.** The cross-store consistency check (D5) catches the misconfiguration. Adopter wires a store. |
| `proposal_managers` + `proposal_stores` wired, recipes have no `capability_overlap` | Finalize works; no per-product capability gating (adapter still validates if it wants to). |
| Full v1.5 stack | All five lifecycle pieces active. |
| **Mixed-version tenants behind one router** (tenant A on v1, tenant B on v1.5) | Supported. The orphan-key validation only requires `proposal_stores` keys to be a subset of `platforms` keys. A tenant without a `proposal_store` entry simply doesn't get finalize dispatch — the v1 path applies. Common during gradual rollout. |

**Shipped v1 examples are untouched.** `examples/hello_proposal_manager.py`
wires `MockProposalManager` with `sales_specialism="sales-non-guaranteed"`
and no finalize capabilities; v1.5 doesn't change its behaviour.
`examples/multi_platform_seller/` and the v3 reference seller don't
claim `media_buy.supports_proposals: true` (per the architecture doc
§ "What v1.5 does NOT propose"), so they stay on the v1 path.

**Wire-level back-compat.** v1.5 doesn't change `GetProductsRequest`
/ `GetProductsResponse` schemas; the spec already supports
`buying_mode='refine'`, `refine[i].action='finalize'`, and the
`proposals[]` response array. v1.5 wires the framework path that
makes those wire shapes work; adopters claiming
`media_buy.supports_proposals: true` *gain* a working stack rather
than migrating away from a working alternative.

**Mock-server fixtures.** The `MockProposalManager` forwarder today
points at `bin/adcp.js mock-server <specialism>`. The mock-server
side is responsible for emitting the right `proposal_status: committed`
+ `expires_at` payload when it sees `refine[{action: finalize}]`;
v1.5 doesn't change that side of the protocol. Adopters running
`MockProposalManager` against a finalize-capable mock-server fixture
*do* need to wire a `ProposalStore` for v1.5 finalize to round-trip
properly (otherwise the framework can't validate `expires_at` at
`create_media_buy`). The forwarder gains a small enhancement: when
the upstream mock-server returns a committed proposal, the forwarder
calls `proposal_store.commit(...)` before projecting. This is the
*one* MockProposalManager change needed; the upstream URL contract
is unchanged.

### D7. `expires_at` enforcement — framework-owned, no adapter participation

**Decision:** when a buyer calls `create_media_buy(proposal_id=...)`,
the framework (not the adapter) enforces the `expires_at` window.
After the hold expires, the framework returns a structured
`PROPOSAL_EXPIRED` error before dispatching to the adapter. The
error code is new — added to the AdCP spec error catalog as part of
v1.5 implementation. (See § Open Questions for the spec-coordination
note.)

```python
async def _enforce_proposal_expiry(
    proposal_id: str,
    proposal_store: ProposalStore,
    now: datetime,
    grace_seconds: int,
) -> ProposalRecord:
    record = await _await(proposal_store.get(proposal_id, expected_account_id=...))
    if record is None:
        raise AdcpError("PROPOSAL_NOT_FOUND", ..., field="proposal_id")
    if record.state != "committed":
        raise AdcpError(
            "PROPOSAL_NOT_COMMITTED",
            message=(
                f"Proposal {proposal_id!r} is in state {record.state!r}; "
                "only committed proposals can be accepted via "
                "create_media_buy. Call get_products with "
                "buying_mode='refine' and action='finalize' first."
            ),
            recovery="correctable",
            field="proposal_id",
        )
    if record.expires_at is not None:
        deadline = record.expires_at + timedelta(seconds=grace_seconds)
        if now > deadline:
            raise AdcpError(
                "PROPOSAL_EXPIRED",
                message=(
                    f"Proposal {proposal_id!r} expired at "
                    f"{record.expires_at.isoformat()}; create_media_buy "
                    "must be called within the inventory hold window. "
                    "Call get_products with buying_mode='refine' and "
                    "action='finalize' to request a fresh hold."
                ),
                recovery="terminal",
                field="proposal_id",
            )
    return record
```

**Grace window** (`expires_at_grace_seconds` on `ProposalCapabilities`):
adopter-declared slack between `expires_at` and the hard rejection.
Default 0 (strict). Adopters running async finalize with webhook
notifications often want a small grace window (typically 60-300s)
to absorb clock skew between the seller's `expires_at` and the
buyer's `create_media_buy` call. Strictly an adopter signal; the
framework reads `manager.capabilities.expires_at_grace_seconds`.

**No adapter participation.** The adapter's
`create_media_buy(req, ctx)` runs only after expiry validation
passes; expired proposals never reach the adapter. This is the
same design posture as `MediaBuyStore.backfill` for overlay echo
(`media_buy_store.py:75-78`) — framework owns the enforcement; the
adapter focuses on upstream translation.

## Storyboard pass criteria + adopter LOC budget

The end-state v1.5 unlocks: an adopter writes a mock seller that
passes both `proposal_finalize.yaml` and `refine_products.yaml`
end-to-end through the SDK. Quantified target:

* **Total LOC for the proposal-mode mock**: ≤ 350 LOC (Python source,
  excluding imports and tests).
* **Comparison anchor**: `examples/hello_proposal_manager.py` is 279
  LOC for the v1 forwarder demo. v1.5's full proposal-mode mock
  should be in the same ballpark + a small fixed overhead for
  finalize / store wiring.

Concretely, the adopter writes:

| Component | Estimated LOC |
|---|---|
| `MyProposalManager(ProposalManager)` with `get_products` + `refine_products` + `finalize_proposal` | ~150 |
| Concrete `Recipe` subclass with one `capability_overlap` declaration | ~30 |
| `MyDecisioningPlatform(DecisioningPlatform)` with `create_media_buy` + `update_media_buy` + `get_media_buy_delivery` reading `ctx.recipes` | ~120 |
| Wiring (router + stores + capabilities) | ~50 |
| **Total** | **~350 LOC** |

**Indicative skeleton** for `examples/hello_proposal_manager_v15.py` —
not the implementation, but the shape the python-expert builds against.
If this skeleton crosses ~150 LOC at the wiring layer, the budget claim
is wrong and the design must be revisited:

```python
# --- Recipe (one concrete subclass per platform implementation) ---
class GAMRecipe(Recipe):
    recipe_kind: Literal["gam"] = "gam"
    line_item_template_id: int
    creative_placeholder_ids: list[int]
    capability_overlap: CapabilityOverlap = CapabilityOverlap(
        pricing_models=frozenset({"cpm"}),
        delivery_types=frozenset({"guaranteed"}),
    )

# --- ProposalManager (15-line surface; rest is forwarder body) ---
class MyProposalManager:
    capabilities = ProposalCapabilities(
        sales_specialism="sales-proposal-mode",
        refine=True,
        finalize=True,
    )

    async def get_products(self, req, ctx): ...        # ~40 LOC
    async def refine_products(self, req, ctx): ...    # ~40 LOC
    async def finalize_proposal(self, req, ctx):      # ~40 LOC
        return FinalizeProposalSuccess(
            proposal=req.draft.with_committed_pricing(...),
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )

# --- DecisioningPlatform (reads ctx.recipes; never writes) ---
class MyDecisioningPlatform:
    async def create_media_buy(self, req, ctx):
        recipe = cast(GAMRecipe, ctx.recipes[req.packages[0].product_id])
        # ... call upstream with recipe.line_item_template_id ...
    async def update_media_buy(self, req, ctx): ...
    async def get_media_buy_delivery(self, req, ctx): ...

# --- Wiring (12 lines; the LOC budget's load-bearing claim) ---
router = PlatformRouter(
    accounts=ExplicitAccounts({"acct_demo": Account(...)}),
    platforms={"default": MyDecisioningPlatform()},
    proposal_managers={"default": MyProposalManager()},
    proposal_stores={"default": InMemoryProposalStore()},
    media_buy_stores={"default": create_media_buy_store(MyMediaBuyStore(), account_id_resolver=...)},
    capabilities=DecisioningCapabilities(
        media_buy=MediaBuyCapabilities(supports_proposals=True),
    ),
)
serve(router)
```

The python-expert agent owns the LOC validation: if the resulting
example crosses 500 LOC, the implementation is more friction than
the design promised and the implementer flags it before merging.

**Testing primitives.** The framework's in-memory references —
`InMemoryProposalStore`, the existing `InMemoryTaskRegistry`, the
adopter's own `MyMediaBuyStore` for tests — ARE the test primitives.
v1.5 doesn't ship a separate `FakeProposalStore`. Adopter pytest
fixtures instantiate the in-memory ref + a recorded-calls
`MyDecisioningPlatform` and assert on the recorded sequence.

**Observability.** The framework emits structured log records at
each lifecycle transition with a stable schema:

* `proposal.draft_persisted` — `proposal_id`, `account_id`, `recipes_count`
* `proposal.finalized` — `proposal_id`, `account_id`, `expires_at`,
  `path` (`"inline"` | `"handoff"`)
* `proposal.expired` — `proposal_id`, `account_id`, `now`,
  `expires_at`, `grace_seconds`
* `proposal.consumed` — `proposal_id`, `account_id`, `media_buy_id`

Adopters route these through their existing log pipeline (the SDK
uses standard `logging.getLogger("adcp.decisioning.proposal_lifecycle")`).
v1.5 does NOT add new metric instruments — adopters who want
counters/histograms wrap the lifecycle code with their own instrumentation.

If the design forces this above 500 LOC, it has too much friction
and must be revisited before implementation. The python-expert
agent in the next phase validates against this budget; storyboard
test harness measures against the actual adopter implementation.

**Storyboard pass criteria** — `proposal_finalize.yaml`:

* Phase `setup` (sync_accounts) — already passes via existing
  `AccountStore` wiring; no v1.5 work needed.
* Phase `brief_with_proposals` (`get_products` brief) — passes via
  existing v1 dispatch; ProposalManager returns proposals + recipes;
  framework calls `proposal_store.put_draft(...)` automatically.
* Phase `refine_proposal` (`get_products` refine) — passes via v1
  refine surface + framework `proposal_store.put_draft(...)` overwrite
  on each iteration.
* Phase `finalize_proposal` (`get_products` refine + finalize) —
  framework intercepts via D2 dispatch; calls
  `finalize_proposal`; commits via D3; returns `Proposal` with
  `proposal_status: committed` + `expires_at`.
* Phase `accept_proposal` (`create_media_buy(proposal_id=...)`) —
  framework enforces D7 expiry; hydrates via D3; validates
  D4 capability overlap; dispatches to platform; persists recipes
  to `MediaBuyStore`; marks proposal consumed.

`refine_products.yaml` is a strict subset (no finalize phase); v1.5
ships this for free as a side effect of D1 + D2.

**`pending_creatives_to_start.yaml`** (cited in the requires_scenarios
of `sales-proposal-mode/index.yaml:13`) is orthogonal — covers the
guaranteed-creative-lifecycle gating, not the proposal lifecycle.
v1.5 doesn't address it; that's existing creative-platform territory.

## Implementation seams

Where the v1.5 work lands:

| Concern | Module | Existing? | Notes |
|---|---|---|---|
| ProposalStore Protocol + InMemoryProposalStore ref impl | `src/adcp/decisioning/proposal_store.py` (NEW) | No | Mirror `media_buy_store.py:62-130` shape |
| ProposalCapabilities new fields | `src/adcp/decisioning/proposal_manager.py:93-146` | Yes (extend) | Add `finalize`, `finalize_with_handoff`, `expires_at_grace_seconds` |
| ProposalManager Protocol new methods | `src/adcp/decisioning/proposal_manager.py:167-264` | Yes (extend) | Add `finalize_proposal` / `finalize_proposal_with_handoff` |
| Recipe capability_overlap field | `src/adcp/decisioning/recipe.py:62-91` | Yes (extend) | Add `capability_overlap: CapabilityOverlap \| None` |
| CapabilityOverlap dataclass | `src/adcp/decisioning/recipe.py` (same module) | No | Co-located with Recipe |
| MediaBuyStore recipe persistence | `src/adcp/decisioning/media_buy_store.py:62-...` | Yes (extend) | Add `persist_recipes` / `hydrate_recipes` |
| PlatformRouter store kwargs | `src/adcp/decisioning/platform_router.py:319-352` | Yes (extend) | Add `proposal_stores=` / `media_buy_stores=` + cross-store consistency check |
| Finalize dispatch interception | `src/adcp/decisioning/refine.py` (extend) or new `src/adcp/decisioning/proposal_lifecycle.py` | Partial | Refine.py at lines 200-219 already detects proposal-scope refines; finalize action lands here |
| Capability-overlap validation | `src/adcp/decisioning/proposal_lifecycle.py` (NEW) | No | Pre-adapter validation seam; called from create_media_buy + update_media_buy dispatch |
| `expires_at` enforcement | `src/adcp/decisioning/proposal_lifecycle.py` (same NEW module) | No | Called from create_media_buy dispatch before adapter |
| `ctx.recipes` field | `src/adcp/decisioning/context.py` | Yes (extend) | Add `recipes: Mapping[str, Recipe] = field(default_factory=dict)` |
| `PROPOSAL_EXPIRED` / `PROPOSAL_NOT_FOUND` / `PROPOSAL_NOT_COMMITTED` / `HUMAN_REVIEW_REQUIRED` error codes | `src/adcp/decisioning/types.py` | Yes (extend) | Add to `KNOWN_NON_SPEC_CODES` allowlist (Resolutions §3); spec issue [adcp#4043](https://github.com/adcontextprotocol/adcp/issues/4043) tracks 3.1 inclusion. `HUMAN_REVIEW_REQUIRED` is the inline-→-handoff escalation signal (see D2). |

The new module `proposal_lifecycle.py` is the natural home for the
finalize / expiry / capability-overlap framework code — it sits
parallel to `refine.py` (which handles the buyer-side refine echo)
and `webhook_emit.py` (which handles capability-gated post-adapter
side effects). Same architectural pattern: framework intercepts at a
seam, does its work, dispatches.

## What v1.5 does NOT propose

* **Reporting-capability declaration on Recipe.** Architecture doc
  flags this as future work (`product-architecture.md:735-740`);
  v1.5 keeps it out of scope.
* **InventoryStore / SignalStore / RateCardStore / AvailabilityStore
  primitives.** Layer 4 of the four-layer model
  (`product-architecture.md:90-96`); architecture doc explicitly
  defers (`product-architecture.md:199-212`).
* **Multi-decisioning recipe routing.** v1's `multi_decisioning`
  capability is informational. v1.5 keeps a single
  `DecisioningPlatform` per tenant; routing per-recipe-kind to
  multiple platforms within a tenant lands later. This is
  consistent with the architecture doc's
  `product-architecture.md:131-138` posture.
* **Wire-spec changes.** The `proposal_finalize.yaml` storyboard
  asserts wire shapes that already exist
  (`buying_mode='refine'`, `refine[i].action='finalize'`,
  `proposals[].proposal_status='committed'`, `proposals[].expires_at`).
  v1.5 ships the framework path; doesn't add wire fields.
* **Spec error codes.** `PROPOSAL_EXPIRED` / `PROPOSAL_NOT_FOUND`
  are new error codes; D7 calls for them. Per Brian's review: the
  spec catalog won't include these until AdCP 3.1, so v1.5 ships
  them via the `KNOWN_NON_SPEC_CODES` allowlist (same path as
  `CONFIGURATION_ERROR`, see `adcp/decisioning/types.py` allowlist).
  Parallel workstream files a spec issue against
  `adcontextprotocol/adcp` requesting inclusion in 3.1; v1.5 PR
  description must cite the spec issue URL.
* **Static-catalog reference `ProposalManager` impl.** v1's
  `MockProposalManager` forwarder is the on-ramp; v1.5 doesn't ship
  a separate reference implementation. The architecture doc's
  rationale (`product-architecture.md:303-322`) holds.
* **Persistent-store reference impl with adopter-supplied SQL/Redis.**
  The framework ships only `InMemoryProposalStore`; durable backings
  are adopter responsibility. Same posture as `TaskRegistry`
  (`task_registry.py:293-296`).
* **`update_proposal` / `cancel_proposal` wire surfaces.** Spec
  doesn't define these today; the `refine[i].action` taxonomy
  (`include` / `omit` / `finalize`) is the only proposal-mutation
  path. If the spec adds them later, v1.6+ extends.

## Resolutions (post-Brian-O'Kelley review)

Open questions resolved before implementation. Capturing here for the
implementer's record.

1. **Auto-allocate `InMemoryProposalStore` when missing?**
   **RESOLVED — explicit wiring with hard error.** Brian: *"not sure
   — i can argue both ways too. let's see if we get any feedback."*
   DX review (round 2) recommended explicit wiring: multi-worker
   deployments that get a silent in-memory store lose every in-flight
   proposal at the first worker that didn't see `put_draft`. The
   30 seconds of copy-paste from a hard error is the better trade
   than a debugging session. The cross-store consistency check at
   D5 names the exact kwarg in the error message. Revisit if adopter
   feedback hits the friction argument.

2. **Inline-vs-handoff finalize hint.** **RESOLVED — not inferred
   from `time_budget`.** Brian: *"i don't think time budget is
   necessarily a signal for sync vs async — i think a fast async
   beats a slow sync!"* Drop the `time_budget`-inference path. v1.5
   ships **explicit finalize-mode declaration** on
   `ProposalCapabilities` (`finalize: bool`, `finalize_with_handoff:
   bool` — adopter declares what their seller supports; framework
   dispatches accordingly). The methods are named `finalize_proposal`
   and `finalize_proposal_with_handoff` to disambiguate from `async
   def` (DX review feedback). If the spec later adds a per-request
   `finalize_mode` hint, the framework reads it; until then, the
   seller's declaration + a `HUMAN_REVIEW_REQUIRED` escalation from
   the inline path governs. See § Decision 2.

3. **`PROPOSAL_EXPIRED` / `PROPOSAL_NOT_FOUND` error codes.**
   **RESOLVED — ship now, ask spec for 3.1.** Brian: *"won't get it
   until 3.1. so we have to make do until then. but we should ask for
   it."* v1.5 ships these codes via the existing `KNOWN_NON_SPEC_CODES`
   allowlist (same path as `CONFIGURATION_ERROR` from earlier). Parallel
   workstream files spec issue against `adcontextprotocol/adcp` for
   3.1 inclusion. v1.5's PR description must cite the spec issue URL.

4. **`MediaBuyStore` extension in place vs. new `RecipeStore`
   Protocol.** **RESOLVED — in place.** Brian: *"in place"*. Extend
   `MediaBuyStore` with `persist_recipes` / `hydrate_recipes`. Keep
   the surface unified by `media_buy_id`.

5. **`update_media_buy` capability-overlap re-validation cost.**
   **RESOLVED — re-validate.** Brian: *"we should re validate"*. Drop
   the recipe-identity-cache complexity; v1.5 re-runs validation on
   every `update_media_buy` (and friends). Performance can be revisited
   when an adopter reports it as a bottleneck — premature optimization
   otherwise.

6. **`multi_decisioning` field on `ProposalCapabilities`.**
   **RESOLVED — remove for v1.5; add when routing-by-recipe-kind
   ships.** Brian: *"i say no, we can always add later"*. v1's
   informational flag stays in v1's compatibility surface (no breaking
   removal); v1.5 doesn't surface it on new declarations and the
   capability-validate path stops checking it.

7. **Optional Protocol method detection.** **DEFERRED — escalate
   only if it surfaces as a wire-protocol question.** Brian: *"don't
   care but if this is a protocol question we can escalate"*. v1.5
   mirrors v1's `hasattr` detection for `refine_products` per
   `proposal_manager.py:239-264`; same pattern for new optional
   methods. No design change.

8. **`InMemoryProposalStore` location.** **RESOLVED — `src/adcp/decisioning/`.**
   Brian: *"src"*. Mirrors `InMemoryTaskRegistry` (`task_registry.py:299`).

## Cross-references

* **Architecture anchor.**
  `docs/proposals/product-architecture.md` — especially § "The
  proposal workflow", § "Proposal lifecycle: `finalize` is the
  acceptance handshake" (lines 613-669), § "Open questions" (lines
  829-861), and Layers 2-3 (lines 109-185).
* **v1 ProposalManager (PR #504).**
  `src/adcp/decisioning/proposal_manager.py:1-456` — the Protocol
  shape this design extends.
* **v1 Recipe.** `src/adcp/decisioning/recipe.py:1-91` — adds
  `capability_overlap` field per D4.
* **PlatformRouter.** `src/adcp/decisioning/platform_router.py:267-589`
  — extended per D5; the per-tenant binding model is preserved.
* **MediaBuyStore precedent.**
  `src/adcp/decisioning/media_buy_store.py:1-130` — the canonical
  shape v1.5's `ProposalStore` mirrors.
* **TaskRegistry precedent.**
  `src/adcp/decisioning/task_registry.py:127-290` — the durability /
  cross-tenant patterns v1.5 reuses.
* **Refine scaffold.**
  `src/adcp/decisioning/refine.py:1-284` — already handles
  proposal-scope refine echoes (lines 205-213); v1.5 extends to
  intercept finalize.
* **Webhook auto-emit (canonical "framework intercepts at a seam"
  reference).** `src/adcp/decisioning/webhook_emit.py:1-39`.
* **Dispatch design voice.**
  `docs/proposals/decisioning-platform-dispatch-design.md` — the
  Decisions-as-prose pattern v1.5 mirrors.
* **Compliance scenarios driving v1.5 (adcp-1 spec repo).**
  `static/compliance/source/protocols/media-buy/scenarios/proposal_finalize.yaml`
  (canonical finalize scenario);
  `static/compliance/source/protocols/media-buy/scenarios/refine_products.yaml`
  (multi-turn refine);
  `static/compliance/source/specialisms/sales-proposal-mode/index.yaml:1-521`
  (the dedicated specialism storyboard);
  `static/compliance/source/specialisms/sales-guaranteed/index.yaml:1-505`
  (the HITL pattern that informs D2's async finalize surface).
* **Salesagent gap citations** (latest worktree:
  `tallahassee-v8`).
  `src/core/database/models.py` — no Proposal table;
  `src/core/schemas/product.py:224-249` — `buying_mode` widened to
  `str | None` with no dispatch;
  `src/core/tools/products.py:756` — `implementation_config`
  excluded from `model_dump()` (storage-only, not lifecycle);
  `src/core/tools/media_buy_create.py` — no `proposal_id`
  parameter;
  `src/services/default_products.py:38-309` —
  `implementation_config` lives on product templates, not
  proposals;
  `src/adapters/gam_implementation_config_schema.py:4-52` — the
  adapter-specific config schema lives on the products table per
  product, not per proposal.
* **Related issues** (architecture doc § "Already filed (#491–#497)"):
  `#496` (refine flow scaffold) is the v1 ancestor of v1.5's finalize
  work; `#497` (`implementation_config` lookup helper for
  `create_media_buy`) is the v1 ancestor of D3's recipe hydration.
* **Existing example LOC anchor.**
  `examples/hello_proposal_manager.py` (279 LOC) — the v1
  proposal-manager demo. v1.5's proposal-mode mock target ≤ 350
  LOC builds on this.
