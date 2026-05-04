# Proposal surfaces — when to use which

The SDK ships two related surfaces with overlapping names. This guide
maps what you want to do to which one to reach for.

| If you want to… | Reach for | Module |
|---|---|---|
| Stand up a sales agent that doesn't generate proposals | `proposals_not_supported()` | `adcp.server.proposal` |
| Build allocations inline inside a handler method | `AllocationBuilder` | `adcp.server.proposal` |
| Manage proposal logic per-tenant (multi-tenant) | `ProposalManager` Protocol + `MockProposalManager` | `adcp.decisioning` |
| Wire mock-backed proposals while you build the real impl | `MockProposalManager` against a `bin/adcp.js mock-server <specialism>` | `adcp.decisioning` |
| Read the design rationale for the two-platform split | `product-architecture.md` § "The two-platform composition" | docs |

## The two surfaces

### `adcp.server.proposal` — request-time helpers

Imperative helpers an `ADCPHandler` calls *during* a single request:

- `AllocationBuilder` — fluent builder for `Proposal.allocations` entries
  (one product → percentage of budget). Chain `.with_pricing_option(...)`
  and `.with_rationale(...)` calls before `.build()`.
- `proposals_not_supported(reason=...)` — drop-in `ProposalNotSupported`
  response for sales agents that *don't* generate proposals. Returns the
  `PROPOSALS_NOT_SUPPORTED` error code per spec.

These have no state and no lifecycle. They're shape helpers — like
`pydantic.BaseModel` constructors — used inside the body of a handler.

### `adcp.decisioning.ProposalManager` — Protocol contract

The Protocol contract for the *proposal-side* platform in the
two-platform composition. A `ProposalManager` owns proposal assembly
(`get_products`, `refine_products`); the `DecisioningPlatform` it
composes with owns execution (`create_media_buy`, `update_media_buy`,
`get_delivery`).

- `ProposalManager` — the Protocol (sync or async, detected at boot).
- `ProposalCapabilities` — capability declaration (sales specialism,
  refine support, dynamic products, multi-decisioning).
- `MockProposalManager` — v1 forwarder that delegates to a running
  `bin/adcp.js mock-server` mock fixture. Use it when you don't yet have
  proposal logic; you get a working catalog with stub recipes.

Bind a `ProposalManager` per tenant via
`PlatformRouter(proposal_managers={tenant_id: manager})`. Tenants
without a manager fall through to their `DecisioningPlatform.get_products`
(backward-compatible).

## Quick decision rule

- **Single tenant, no proposal generation** → `proposals_not_supported()`
  in your `get_products` handler. Done.
- **Single tenant, simple proposals** → Build allocations inline with
  `AllocationBuilder`. Don't reach for `ProposalManager` until you
  outgrow this.
- **Multi-tenant, different proposal logic per tenant** →
  `PlatformRouter` + per-tenant `ProposalManager`. The router wires the
  two-platform composition for you.

The two surfaces compose: a `ProposalManager` implementation can use
`AllocationBuilder` internally to assemble allocations.

## Also see

- `docs/proposals/product-architecture.md` — design rationale for the
  two-platform split and the four-layer product model.
- `examples/hello_proposal_manager.py` — runnable
  `PlatformRouter(proposal_managers={...})` example.
