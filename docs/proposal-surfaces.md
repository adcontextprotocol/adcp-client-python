# Proposal surfaces — when to use which

The SDK ships two related surfaces with overlapping names. This guide
maps what you want to do to which one to reach for.

| If you want to… | Reach for | Module |
|---|---|---|
| Stand up a sales agent that doesn't generate proposals | `proposals_not_supported()` | `adcp.server.proposal` |
| Build a proposal inline in a `get_products` handler | `ProposalBuilder` (returns `AllocationBuilder` from `.add_allocation`) | `adcp.server.proposal` |
| Manage proposal logic per-tenant (multi-tenant) | `ProposalManager` Protocol + `MockProposalManager` | `adcp.decisioning` |
| Run mock-backed proposals while building the real implementation | `MockProposalManager` against a `bin/adcp.js mock-server <specialism>` (from the [`adcontextprotocol/adcp`](https://github.com/adcontextprotocol/adcp) repo) | `adcp.decisioning` |
| Preflight, call, and verify `refine_proposals` | `ADCPClient.refine_proposals_verified()` | `adcp.client` |
| Execute a seller refinement batch safely | `execute_refinement_batch()` | `adcp.decisioning` |
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

## Buyer-side response verification

Generated models validate each proposal's shape. Cross-object guarantees need
the request and response together:

```python
result = await client.refine_proposals_verified(
    request,
    capabilities.media_buy.proposal_refinement,
    source_proposals={source.proposal_id: source},
)
if result.pending:
    result = await client.wait_for_refinement_verified(
        request,
        result,
        capabilities.media_buy.proposal_refinement,
        source_proposals={source.proposal_id: source},
    )
elif not result.valid:
    if result.unsupported_dimension:
        # Rebuild from the seller's echoed supported dimensions.
        print(result.unsupported_dimension)
    else:
        print(result.task_result.adcp_error)
```

Pass the complete `TaskResult`, as above. The client retains the original wire
response there so digest verification uses the exact JSON values received from
the seller. Do not pass a separately parsed Pydantic proposal to
`compute_terms_digest()`; parsing may normalize timestamps or insert defaults.
Keep source proposals as their original wire mappings when possible. A parsed
source model is supported only through its retained `terms_digest`, because its
normalized `commercial_terms` cannot be rehashed wire-faithfully.

The verifier checks ordered result correlation, proposal lineage, RFC 8785
`terms_digest` values, distinct alternatives, typed hard constraints, product
changes, reason-code applicability, unchanged finalize terms, live hold expiry,
and `constraint_unsatisfiable` precedence. A submitted response has
`result.pending == True` and is not yet `valid`; use the polling helper above or
call `verify_refinement_result()` on a terminal webhook. The client retains raw
terminal refinement payloads on both transport and webhook paths so digest
verification remains wire-faithful.

Preflight treats an absent seller declaration as unknown, while an explicit
`supported_dimensions` list and `max_alternatives` value are enforced before
transport.

## Seller-side batch execution

Seller callbacks own commercial decisions. The framework helper owns batch
preflight, ordered correlation, immutable lineage, missing terms digests, and
completed-response validation:

```python
from adcp.decisioning import execute_refinement_batch

async def refine_proposals(self, request, context):
    return await execute_refinement_batch(
        request,
        self.capabilities.media_buy.proposal_refinement,
        self.evaluate_refinement,
        context=context,
        finalize_transaction=self.inventory.refinement_transaction,
        source_proposals=await self.load_source_proposals(request),
    )
```

`finalize_transaction(request, context)` is an async context manager. It must
stage every requested hold and commit only on clean exit. The helper requires
that boundary for finalize batches, runs all callbacks inside it, and validates
the complete response before exit; callback or validation failures therefore
roll the whole batch back. Revision-only batches may omit the transaction
because they do not reserve inventory or mutate their source proposals.

`PlatformHandler` also applies capability preflight before invoking adopter
code and validates completed output before serialization. This protects direct
implementations that do not use the batch helper, while the explicit
transaction remains the seller's proof of atomic hold behavior.
Framework-managed `TaskHandoff` completions pass through the same validator.
`WorkflowHandoff` is rejected for this task because an external writer calling
the registry directly would bypass response verification; use `TaskHandoff` or
validate in an adopter-owned registry wrapper before exposing a custom flow.

## Also see

- `proposals/product-architecture.md` — design rationale for the
  two-platform split and the four-layer product model.
- `examples/hello_proposal_manager.py` — runnable
  `PlatformRouter(proposal_managers={...})` example.
- `examples/negotiation_workflow.py` — runnable buyer verification and seller
  atomic-finalize example.

## Legacy refinement fallback limits

The older `get_products(buying_mode="refine")` path is not a lossless fallback
for `refine_proposals`. It can carry a prose refinement loop, but it cannot
represent the 3.2 task's ordered multi-proposal batch, portable typed
constraints, requested alternative count, immutable parent lineage and terms
digests, all-or-none finalize holds, or exact idempotent replay semantics. Use
that path only when discovery shows `media_buy.lifecycle_tools` does not contain
`refine_proposals`; `proposal_refinement` describes typed dimensions but is not
the task-support discriminator. Do not silently translate a typed constraint or
`finalize` action into prose. Before transport, return a local
`PRE_MUTATION_UNSUPPORTED` result with `unsupported_fields`, `lost_guarantee`,
and `dispatched: false`, then let the buyer explicitly rebuild a genuinely
legacy request.
