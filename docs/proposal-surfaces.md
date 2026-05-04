# Proposal surfaces — when to use which

The SDK ships two related surfaces with overlapping names. This guide
maps what you want to do to which one to reach for.

| If you want to… | Reach for | Module |
|---|---|---|
| Stand up a sales agent that doesn't generate proposals | `proposals_not_supported()` | `adcp.server.proposal` |
| Build a proposal inline in a `get_products` handler | `ProposalBuilder` (returns `AllocationBuilder` from `.add_allocation`) | `adcp.server.proposal` |
| Manage proposal logic per-tenant (multi-tenant) | `ProposalManager` Protocol + `MockProposalManager` | `adcp.decisioning` |
| Run mock-backed proposals while building the real implementation | `MockProposalManager` against a `bin/adcp.js mock-server <specialism>` (from the [`adcontextprotocol/adcp`](https://github.com/adcontextprotocol/adcp) repo) | `adcp.decisioning` |
| Read the design rationale for the two-platform split | `proposals/product-architecture.md` § "The two-platform composition" | docs |

## The two surfaces

### `adcp.server.proposal` — request-time helpers

Imperative helpers an `ADCPHandler` calls *during* a single request:

- `ProposalBuilder` — the higher-level fluent builder. Chain
  `.with_description(...)`, `.add_allocation(product_id, pct)`,
  `.with_rationale(...)`, `.with_budget_guidance(...)`, `.expires_in(days)`,
  then `.build()`.
- `AllocationBuilder` — what `ProposalBuilder.add_allocation` hands back
  for per-allocation chaining (`.with_pricing_option(...)`,
  `.with_rationale(...)`). You typically don't construct it directly.
- `proposals_not_supported(reason=...)` — returns a typed
  `ProposalNotSupported` model with the spec's
  `PROPOSALS_NOT_SUPPORTED` error code. Return it directly from your
  handler when you support `get_products` but not proposal generation.

These have no state and no lifecycle. They're shape helpers used inside
the body of a handler.

### `adcp.decisioning.ProposalManager` — Protocol contract

The Protocol contract for the *proposal-side* platform in the
two-platform composition. A `ProposalManager` owns proposal assembly
(`get_products`, `refine_products`); the `DecisioningPlatform` it
composes with owns execution (`create_media_buy`, `update_media_buy`,
`get_delivery`).

- `ProposalManager` — the Protocol (sync or async, detected at boot).
- `ProposalCapabilities` — capability declaration (sales specialism,
  refine support, dynamic products, multi-decisioning).
- `MockProposalManager` — a forwarder that delegates to a running
  mock-server fixture. Use it when you don't yet have proposal logic;
  you get a working catalog with stub recipes.

Bind a `ProposalManager` per tenant via
`PlatformRouter(proposal_managers={tenant_id: manager})`. Tenants
without a manager fall through to their `DecisioningPlatform.get_products`
(backward-compatible). Single-tenant adopters use a one-entry router
(`{"default": manager}`) — same code path, no branching.

## Quick decision rule

- **Don't generate proposals?** → Return `proposals_not_supported()` from
  `get_products`. Done.
- **Generate proposals inline, in-process?** → Use `ProposalBuilder` in
  your handler. Reach for it before reaching for `ProposalManager`.
- **Want mock-backed proposals while you build, OR different proposal
  logic per tenant?** → `PlatformRouter(proposal_managers={...})` with
  `MockProposalManager` (mock-backed) or your own `ProposalManager` impl
  (per-tenant). Single-tenant adopters use a one-entry router.

The two surfaces compose: a `ProposalManager` implementation can use
`ProposalBuilder` internally to assemble allocations.

## Also see

- `proposals/product-architecture.md` — design rationale for the
  two-platform split and the four-layer product model.
- `examples/hello_proposal_manager.py` — runnable
  `PlatformRouter(proposal_managers={...})` example.
