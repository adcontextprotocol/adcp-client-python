# sales_proposal_mode_seller — proposal lifecycle end-to-end

A worked example of the v1.5 ProposalManager surface. One process, one
tenant, one wired `InMemoryProposalStore` — passes the
`proposal_finalize.yaml` storyboard scenario end-to-end.

## What it ships

| File | Role |
|---|---|
| `src/recipe.py` | `ProposalModeRecipe(Recipe)` — typed implementation_config with `CapabilityOverlap` declaration. |
| `src/proposal_manager.py` | `ProposalModeProposalManager` — `get_products` / `refine_products` / `finalize_proposal`. Declares `finalize=True`. |
| `src/platform.py` | `ProposalModeDecisioningPlatform` — reads `ctx.recipes[product_id]` on `create_media_buy` / `update_media_buy` / `get_media_buy_delivery`. |
| `src/app.py` | Boot script. Constructs router with `proposal_managers` + `proposal_stores`. |

## What the framework does

The adopter writes one method per lifecycle phase. Everything between the
methods is the framework's:

```
buyer.get_products(brief)
    ↓ handler.get_products
    ↓ router.get_products → manager.get_products()
    ↓ proposal_dispatch.maybe_persist_draft_after_get_products  ← FRAMEWORK
    ↓     proposal_store.put_draft(proposal_id, recipes, payload)
    ↓ wire response

buyer.get_products(buying_mode='refine', refine=[{action:'finalize', ...}])
    ↓ handler.get_products
    ↓ proposal_dispatch.maybe_intercept_finalize  ← FRAMEWORK
    ↓     proposal_store.get(proposal_id) → ProposalRecord
    ↓     manager.finalize_proposal(req, ctx)
    ↓     proposal_store.commit(proposal_id, expires_at, payload)
    ↓ wire response with proposal_status='committed'

buyer.create_media_buy(proposal_id=..., total_budget=...)
    ↓ handler.create_media_buy
    ↓ proposal_dispatch.maybe_hydrate_recipes_for_create_media_buy  ← FRAMEWORK
    ↓     enforce_proposal_expiry(proposal_id) — D7
    ↓     validate_capability_overlap(packages, recipes) — D4
    ↓     ctx.recipes = record.recipes
    ↓ platform.create_media_buy(req, ctx) — adapter reads ctx.recipes
    ↓ proposal_dispatch.mark_proposal_consumed  ← FRAMEWORK
    ↓     proposal_store.mark_consumed(proposal_id, media_buy_id) — single write
    ↓ wire response

buyer.update_media_buy / get_media_buy_delivery
    ↓ proposal_dispatch.maybe_hydrate_recipes_for_media_buy_id  ← FRAMEWORK
    ↓     proposal_store.get_by_media_buy_id(media_buy_id) — reverse-index
    ↓     ctx.recipes = record.recipes
    ↓ platform.update_media_buy / get_media_buy_delivery
```

Adopter writes `~50 LOC` of substantive lifecycle logic on the manager
side; the platform reads `ctx.recipes[product_id]` at the start of every
buy method.

## Running locally

```bash
python -m examples.sales_proposal_mode_seller.src.app
```

Then run the storyboard:

```bash
adcp storyboard run http://127.0.0.1:3003/mcp media_buy_seller \
    --json --allow-http
```

The `media_buy_seller/proposal_finalize` scenario walks the full
lifecycle (brief → refine → finalize → create_media_buy).

## What this example deliberately leaves out

- **TaskHandoff finalize.** The inline `FinalizeProposalSuccess` path is
  wired; the `TaskHandoff[FinalizeProposalSuccess]` HITL path is a
  v1.5 follow-up.
- **Real upstream.** The platform runs entirely in process with synthetic
  delivery scaling.
- **Multi-tenant.** Single-tenant router; multi-tenant follows the
  `multi_platform_seller` pattern.
- **Durable store.** Uses `InMemoryProposalStore` — process-local, lost
  on restart. Production adopters wire a Postgres / Redis backing per
  the `ProposalStore` Protocol.
