# Experiment: salesagent side-car runtime (full GAM)

Status: **PROPOSED**. Falsifies [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)'s
two-platform composition against a real adopter codebase + a live ad
server. Runs in a salesagent worktree, points at salesagent's existing
Postgres, drives real GAM. Admin UI untouched.

## Hypothesis

The four-layer model + two-platform composition (`ProposalManager` +
`DecisioningPlatform`) in [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)
holds up when ported on top of salesagent's actual code and database,
with GAM as the live decisioning target. Either it ports cleanly and
we ship the Protocol shape the experiment forces, or it doesn't and
we learn what's wrong before the architecture solidifies.

The narrative-level claim in [PR #489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
is that adopters port to `SalesPlatform` today and split along the
proposal/decisioning seam later when `ProposalManager` lands. The
experiment runs that exact split — except we force it now, with
`ProposalManager` designed inline against salesagent's complexity.

## Why GAM, not mock-mode

Mock-mode would tell us if the wire shape works. It wouldn't tell us
whether the SDK can host salesagent's actual upstream complexity —
which is the whole question. GAM is where ~99% of salesagent's
deployed value lives (per the migration guide §"Migration order").
The dynamic-product assembly, the `implementation_config` shape, the
line item / ad unit / KV-targeting plumbing, the lifecycle state
graph — all of it is GAM-shaped today. If our architecture survives
contact with GAM, it survives. If it doesn't, mock conformance was
never going to tell us.

## Slice

**One salesagent tenant, configured for GAM, serving the AdCP
`media_buy_seller` storyboard end-to-end against a sandbox GAM Network
ID.** Other tenants stay on the existing runtime. Same process, same
DB, routed by tenant.

In scope:

* **Salesagent admin UI** (`src/admin/`) — UNCHANGED. Operators
  configure tenants, products, principals through existing Flask
  blueprints.
* **Salesagent Postgres schema** (`src/core/database/models.py`) —
  UNCHANGED. The side-car runtime reads/writes the same tables.
* **A new `src/sdk_runtime/` directory** in salesagent, owning:
  - `BuyerAgentRegistry` impl projecting from `Principal` rows
    (`access_key_hash`, `oauth_client_id`)
  - `AccountStore` impl projecting account context from `Principal` +
    `Tenant` rows
  - `GAMProposalManager` — reads `Product` rows, runs the
    `dynamic_products.py` (`src/services/dynamic_products.py`)
    assembly logic, emits wire `Product[]` with
    `implementation_config` recipes
  - `GAMDecisioningPlatform` — wraps salesagent's existing
    `google_ad_manager.py` adapter; calls real GAM
  - `adcp.serve(...)` entrypoint mounted alongside the existing A2A
    + MCP servers, routing the experiment tenant only
* **Real GAM upstream** — actual orders/line items in a sandbox
  network. Real auth, connection pooling, error projection. The
  existing GAM adapter handles upstream calls; we wrap it.
* **HITL approval lifecycle** — the
  `compose_method` + `ShortCircuit` claim from #489 §3.9 against
  salesagent's actual approval flow.

  **How salesagent does HITL today.** Synchronous gate + DB-persisted
  re-entry, not a paused coroutine.

  1. Adapter's `create_media_buy` (`google_ad_manager.py:571`)
     checks `_requires_manual_approval(op) and not getattr(request,
     "_already_approved", False)`. If gated: writes a `WorkflowStep`
     row (status `"pending_approval"`), an `ObjectWorkflowMapping`
     linking step → media buy, and a `MediaBuy` row carrying
     `raw_request` as JSON. Returns 200 with `workflow_step_id`
     populated. **No GAM call has happened.**
  2. Operator approves through admin UI. Flask route
     `approve_workflow_step` (`admin/blueprints/workflows.py:155`)
     directly imports `execute_approved_media_buy`
     (`media_buy_create.py:458`) — same process, same DB.
  3. `execute_approved_media_buy` reconstructs the request from
     `raw_request`, sets `request._already_approved = True`
     (`media_buy_create.py:529`), re-calls the adapter. The gate
     skips this time; real GAM call fires.

  **What that means for the SDK runtime.** No paused-task
  resumption needed. Salesagent's `WorkflowStep` table IS its task
  registry; we keep using it. Mapping:

  - `before` hook on `create_media_buy` /
    `update_media_buy` / `add_creative_assets` checks
    `manual_approval_required`. If true: calls
    salesagent's existing `workflow_manager` to write the
    `WorkflowStep` + `MediaBuy(status='pending_approval',
    raw_request=...)` rows, returns
    `ShortCircuit(value=CreateMediaBuySuccess(
    status='pending_approval', workflow_step_id=...))`.
  - Admin UI unchanged. `approve_workflow_step` keeps calling
    `execute_approved_media_buy(...)`.
  - Rewrite the body of `execute_approved_media_buy`: reconstruct
    request, attach a resumption marker (the
    `_already_approved` sentinel, or a typed equivalent on `ctx`),
    call `sdk_runtime.create_media_buy(req, ctx)` directly. Gate
    sees the marker, falls through, real GAM call happens, SDK F12
    auto-emit fires the completion webhook.

  **The interesting design question.** Salesagent's
  `_already_approved` is an untyped `setattr` sentinel — fine for
  one codebase, fragile as an SDK seam. The experiment forces a
  decision: does the SDK ship a typed resumption marker (e.g.,
  `ctx.resumption_token: ResumptionToken | None`), or does the
  before-hook check on adopter-defined state? Whatever shape
  works in this experiment is the recommended Protocol seam.
* **Webhook delivery via SDK F12 auto-emit.** Disable salesagent's
  `protocol_webhook_service.py` for the experiment tenant; configure
  `WebhookSender` on `serve(...)`. We're testing whether buyer-
  registered `push_notification_config` gets sync-completion webhooks
  fired automatically, signed correctly, retried on transient
  failure, logged-and-swallowed on permanent failure — without
  adapter code participating. The migration guide §3.14 claims
  adopters delete their webhook plumbing wholesale; the experiment
  validates that against a real workload.

Out of scope (deliberate):

* Other adapters (Kevel, Broadstreet, Triton, mock — keep on the
  existing runtime).
* Other tenants.
* Refine flow / proposal lifecycle (`finalize`, `expires_at`,
  draft → committed). Wire later if v1 is stable.
* Push reporting (`reporting_webhook` / `reporting_bucket`).
* Creative agent / creative builder.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Salesagent admin (Flask blueprints) — unchanged   │
└──────────────────┬─────────────────────────────────┘
                   │ writes
                   ↓
┌────────────────────────────────────────────────────┐
│  Salesagent Postgres — unchanged schema            │
│   Tenant, Principal, Product, MediaBuy, ...        │
└──────────────────┬─────────────────────────────────┘
                   │ reads
                   ↓
┌────────────────────────────────────────────────────┐
│  src/sdk_runtime/  — the experiment                │
│   BuyerAgentRegistry   ← Principal rows            │
│   AccountStore         ← Principal + Tenant rows   │
│   GAMProposalManager   ← Product rows + dynamic    │
│   GAMDecisioningPlatform → wraps gam adapter       │
│   adcp.serve(transport='both')                     │
└──────────────────┬─────────────────────────────────┘
                   │
                   ↓
              real GAM upstream
```

The existing `adcp_a2a_server.py` (2,276 LOC) and
`mcp_server_enhanced.py` keep serving every other tenant. The
experiment tenant routes to `adcp.serve(...)` only.

Routing strategy: a tiny shim at the framework entry consults
`Principal.tenant_id` and dispatches to either the existing runtime
or the SDK runtime. One process, two runtimes, clean handoff at the
tenant boundary.

## Wrap, don't port

`media_buy_create.py` is 3,930 LOC. Porting it body-by-body into
`GAMDecisioningPlatform.create_media_buy` is the failure mode. The
discipline: **wrap the existing tool body**. The SDK runtime takes
the wire request, projects to salesagent's internal shape, calls the
existing tool function (or the GAM adapter directly), projects the
response back to wire shape.

Same rule for `dynamic_products.py` (505 LOC): wrap, don't port.
`GAMProposalManager.get_products` calls `dynamic_products.py` with
salesagent's expected inputs, gets back its native output, projects
to wire `Product[]`.

This isolates the experiment to **the seam**: where wire requests
become salesagent-shaped calls and back. We learn whether the SDK
abstractions can host salesagent's existing logic without re-porting
it. If a wrapper feels forced, that's the architecture telling us
something. If it ports as a thin shim, the architecture holds.

## Exit criteria (binary)

The AdCP `media_buy_seller` storyboard passes against the experiment
tenant, with:

1. Real GAM line items / orders created in the sandbox network.
2. Salesagent's existing Postgres schema as the data backing.
3. Salesagent admin UI untouched and functional.
4. No double-write conflicts: every write for the experiment tenant
   goes through the SDK runtime; the existing tools don't serve
   that tenant.

If the storyboard passes: the architecture holds. We have a working
`ProposalManager` shape that emerged from real adopter code, ready
to factor into the SDK Protocol.

If it fails: we learn precisely where #502's model breaks —
proposal-side (assembly factoring), decisioning-side (recipe shape),
seam (capability overlap, lifecycle, hydration), or supporting (auth
projection, schema mismatch).

## What we expect to learn

Three concrete questions the experiment answers:

1. **Does `dynamic_products.py` (`src/services/dynamic_products.py`,
   505 LOC) factor onto `ProposalManager.get_products`?**
   Salesagent's signal-driven assembly is the most complex piece of
   proposal logic in the wild. If it ports as a thin wrapping
   (target: <300 LOC of glue), the proposal-side abstraction is
   correct. If it requires gutting `dynamic_products.py` itself, the
   abstraction is wrong.

2. **Does the recipe carry enough?** GAM's `implementation_config`
   (`Product.implementation_config: JSONType`,
   `models.py:256`) is the most-evolved form: line item template
   ids, ad unit ids, KV targeting, frequency caps, signal mappings.
   If the recipe shape we settle on can carry it without escape
   hatches (no `extra: dict[str, Any]` smuggled fields), the
   typed-recipe model in #502 is sound.

3. **What does framework-owned proposal lifecycle actually need?**
   When `create_media_buy` runs, it needs to hydrate the recipe from
   somewhere. Three plausible answers (session cache, DB row, fresh
   lookup); the experiment forces a choice. The choice IS the
   design for the `ProposalManager` Protocol.

4. **What is the right shape for the resumption marker?** Salesagent's
   HITL model (gate → persist → admin approves → re-call same code
   path with sentinel) maps cleanly onto `compose_method` +
   `ShortCircuit` without needing the SDK's `TaskRegistry` to round-trip.
   The gate writes salesagent's `WorkflowStep` row; the admin UI keeps
   calling `execute_approved_media_buy`; that function re-enters the
   SDK runtime with a marker. The open seam is the marker itself —
   salesagent uses an untyped `setattr(request, "_already_approved",
   True)`, fine for one codebase, fragile as an SDK contract. Two
   plausible shapes: (a) typed `ctx.resumption_token: ResumptionToken
   | None` carried through framework dispatch, the before-hook checks
   it; (b) keep the marker adopter-side entirely, the SDK doesn't
   model it. The experiment forces a choice; the choice IS the
   recommended Protocol seam for cross-cutting `compose_method` gates
   in adopters with persisted-and-re-fired approval flows. Sub-question
   to resolve in passing: does `compose_method`'s `before` hook need
   read access to `ctx` rich enough to make this decision (it should).

5. **Does F12 webhook auto-emit hold up under real load?** The
   migration guide claims adopters delete their webhook plumbing.
   The experiment runs that — `protocol_webhook_service.py` off for
   the experiment tenant, `WebhookSender` on. If signing, retry,
   and failure handling all work without adapter code, the claim
   stands. If anything leaks through to adapter code, we revise
   §3.14.

## Risks

* **Scope creep on the wrap.** The temptation to "just port this
  small piece" compounds. Mitigation: every wrap stays a wrap until
  the storyboard passes. Re-porting is a follow-up, not part of v1.
* **GAM credential handling.** Need a sandbox GAM Network ID with
  real auth. Salesagent already has this in dev; experiment
  inherits. Document the env vars in the experiment README.
* **HITL resumption marker shape.** Salesagent's untyped
  `_already_approved` sentinel is what re-entry uses today. Lifting
  that to a typed SDK contract (or deciding to leave it adopter-side)
  is a real design call, not just plumbing. Wrong choice here means
  every adopter with a persisted-approval flow re-invents the
  marker. Mitigation: prototype both shapes in the experiment;
  whichever felt natural at the gate site wins.
* **Webhook signing config.** SDK `WebhookSender` and salesagent's
  existing `webhook_authenticator.py` need to agree on signing
  semantics, or the buyer rejects deliveries. Validate against a
  test buyer before the storyboard run.
* **Auth projection drift.** If `Principal.access_key_hash` doesn't
  project cleanly onto `BuyerAgentRegistry`, the foundation in
  #489 §"Foundations" is wrong. Catch this early — write the
  registry shim first, before anything else.
* **Two writers, one DB — controlled.** The experiment tenant is a
  test tenant we own and is not actively serving real buyers, so
  duplication is operationally fine. Tenant routing still enforces
  the boundary in code (experiment tenant never hits old runtime,
  old tenants never hit new runtime), but the consequence of a
  routing bug is "we notice and fix it," not "we corrupt a buyer's
  campaign."
* **The storyboard hits a feature we deferred.** If `media_buy_seller`
  exercises refine or push reporting on the happy path, scope
  expands. Pin the storyboard version up front; test against it
  locally before declaring v1 done.

## Workstream

1. **Set up the worktree.** New salesagent worktree for the
   experiment. Install adcp-client-python from `main` (or a working
   branch) into salesagent's venv.
2. **Auth shim first.** `BuyerAgentRegistry` + `AccountStore` reading
   from `Principal` + `Tenant`. Validate against existing test
   fixtures before anything else lands.
3. **`GAMDecisioningPlatform` wrapper.** Thinnest possible wrap of
   `google_ad_manager.py`. `create_media_buy` and
   `get_media_buy_delivery` only.
4. **`GAMProposalManager` wrapper.** Thinnest possible wrap of
   `dynamic_products.py` + the `Product` catalog read.
5. **HITL gate via `compose_method`.** `before` hook on
   `create_media_buy` / `add_creative_assets` / `update_media_buy`
   that consults salesagent's `manual_approval_required` config,
   writes the `WorkflowStep` + `MediaBuy(status='pending_approval',
   raw_request=...)` rows via salesagent's existing `workflow_manager`,
   and returns `ShortCircuit(value=CreateMediaBuySuccess(
   status='pending_approval', workflow_step_id=...))`. Rewrite the
   body of `execute_approved_media_buy` (`media_buy_create.py:458`)
   to reconstruct the request, attach the resumption marker, and
   call back into the SDK runtime's `create_media_buy`. Admin UI
   route untouched. Prototype both marker shapes (typed
   `ctx.resumption_token` vs. adopter-side sentinel); record which
   reads cleanly at the gate site.
6. **`WebhookSender` configured on `serve(...)`.** Disable
   `protocol_webhook_service.py` for the experiment tenant. Verify
   sync-completion webhooks fire after every mutating tool call,
   signed correctly, against a test buyer that validates signature.
7. **Routing shim.** Per-tenant dispatch between old and new
   runtimes inside the same process.
8. **Storyboard run.** AdCP `media_buy_seller` against the
   experiment tenant; sandbox GAM; admin UI verified untouched.
   Approval-required path exercised at least once.
9. **Findings doc.** What ported cleanly, what didn't, what the
   `ProposalManager` Protocol should look like based on what worked,
   and which resumption-marker shape (typed
   `ctx.resumption_token` vs. adopter-side sentinel) reads cleanly
   at the `compose_method` gate site.

## Next steps after experiment

If exit criteria pass:

1. Factor the `ProposalManager` Protocol from the experiment into
   adcp-client-python (separate PR).
2. Land `MockProposalManager` forwarder per #502.
3. Update [#489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
   §3.3 with experiment findings; remove "ProposalManager will own X"
   hedging where the experiment proved it.
4. Storyboard the experiment as a worked example in
   `examples/salesagent_sidecar/` (with credentials redacted).

If exit criteria fail:

1. Document where the model broke in
   `docs/proposals/product-architecture-revision-1.md`.
2. Revise [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)
   against the real failure mode.
3. Rerun the experiment against the revised model.

## References

* [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)
  / `docs/proposals/product-architecture.md` — the architecture this
  experiment falsifies.
* [#489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
  / `examples/multi_platform_seller/MIGRATION_FROM_ADAPTER_REGISTRY.md`
  — the migration narrative this experiment validates against real code.
* [`prebid/salesagent`](https://github.com/prebid/salesagent) — the
  adopter codebase the experiment runs inside. Key files:
  `src/core/database/models.py` (2,113 LOC),
  `src/services/dynamic_products.py` (505 LOC),
  `src/core/tools/media_buy_create.py` (3,930 LOC),
  `src/adapters/google_ad_manager.py`,
  `src/a2a_server/adcp_a2a_server.py` (2,276 LOC).
