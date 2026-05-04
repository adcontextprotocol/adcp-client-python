# Experiment: salesagent side-car runtime (full GAM)

Status: **PROPOSED v2** (revised after self-review on PR #506).
Falsifies [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)'s
two-platform composition against a real adopter codebase + a live ad
server. Two phases — a cheap recipe-shape falsification first, full
runtime gated on it passing.

## Hypothesis

The four-layer model + two-platform composition (`ProposalManager` +
`DecisioningPlatform`) in [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)
holds up when ported on top of salesagent's actual code and database,
with GAM as the live decisioning target. Either it ports cleanly and
we have a Protocol shape that emerged from real complexity, or it
doesn't and we learn what's wrong before the architecture solidifies.

The narrative-level claim in [PR #489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
is that adopters port to `SalesPlatform` today and split along the
proposal/decisioning seam later when `ProposalManager` lands. The
experiment runs that exact split — except we force it now, with
`ProposalManager` designed inline against salesagent's complexity.

## Two phases

Self-review surfaced that the original single-phase plan front-loaded
3-5 days of expensive infrastructure work (GAM credentials, admin UI
isolation, webhook signing, runtime routing) before testing the
question that's most likely to falsify #502 — whether the recipe
model can carry salesagent's dynamic-product assembly without escape
hatches. The cheapest available falsifier doesn't need any of that
infrastructure.

**Phase 1 — recipe falsification (~1 day, no infrastructure).**
Port `dynamic_products.py` to a `ProposalManager.get_products`
wrapper in isolation. No GAM, no admin UI, no DB rewiring, no
runtime routing. Drive it from recorded signal-agent fixtures.
Assert the output recipe carries every signal-driven variant
salesagent generates without typed escape hatches. If this dies,
the side-car experiment doesn't run; #502 needs revision first.

**Phase 2 — full side-car runtime (3-5 days, gated on Phase 1).**
Salesagent worktree, side-car process driving real GAM, HITL
exercised through `media_buy_guaranteed_approval`, webhooks
delivered through SDK F12 auto-emit and verified by a real
subscribed buyer.

Phase 1 gates Phase 2. Phase 2 only runs if Phase 1 produced findings
consistent with #502.

## Reframing: salesagent is a GAM agent

Salesagent's multi-adapter abstraction is vestigial. GAM is the only
adapter with real deployments (~99% of clients per the migration
guide §"Migration order"); Kevel, Broadstreet, Triton, Xandr are
scaffolding from earlier iterations with no client traffic; mock is
a test fixture, not a backend. Treating salesagent as a GAM agent
that happens to ship dead code simplifies the experiment in three
concrete ways.

1. **The wrap target is unconditional.** Salesagent today carries
   `if adapter.__class__.__name__ == "GoogleAdManager"` switches in
   the `_impl` layer (e.g.,
   `media_buy_create.py:2431-2464` for GAM-specific
   `implementation_config` auto-generation + validation). Reframing
   collapses these to unconditional GAM logic. The wrap doesn't
   need to preserve the registry abstraction — there's no
   compatibility surface to preserve.

2. **Single recipe type.** Salesagent contributes only the GAM
   recipe shape to #502's typed-recipe model. The
   discriminated-union-over-multiple-recipes question (Path B in
   #502) stays real for the SDK in general — adopters with
   heterogeneous upstreams (Prebid-style) are the exercise — but
   salesagent doesn't test that axis. Phase 1 falsification narrows
   to "does the GAM recipe shape carry without escape hatches" —
   sharper question, fewer variables.

3. **`MockAdServer` migration sharpens.** The ~1,800 LOC
   `mock_ad_server.py` deletion was a follow-up; reframed, it joins
   the post-experiment cleanup story alongside Kevel/Broadstreet/
   Triton/Xandr deletion. v1 of the experiment uses SDK
   `Account.mode='mock'` for the experiment tenant; the legacy
   `MockAdServer` keeps serving everyone else until the cutover.

What doesn't change: the two-platform composition seam (proposal-side
dynamic assembly + decisioning-side GAM execution), the recipe
falsification target (Phase 1), the HITL/webhook/auth shim work. The
reframing simplifies the success path; it doesn't shrink the
experiment's questions.

What this implies for the migration guide.
[#489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
§3.1 maps `ADAPTER_REGISTRY` → `PlatformRouter`. For GAM-only
adopters (salesagent, anyone with a single live adapter),
`PlatformRouter` is vestigial — the migration is "delete the
registry, instantiate one `GAMPlatform`," not "translate registry
into router." The router pattern is the right primitive for
heterogeneous adopters; single-adapter adopters skip it. §3.1
should carry a note. Tracked separately from this experiment.

## Why GAM live (Phase 2 only)

Mock-mode would tell us if the wire shape works. It wouldn't tell us
whether the SDK can host salesagent's actual upstream complexity —
which is the whole question for Phase 2. GAM is where ~99% of
salesagent's deployed value lives. The dynamic-product assembly,
recipe shape, lifecycle state graph, HITL plumbing — all of it is
GAM-shaped today. If our architecture survives contact with GAM, it
survives. Mock conformance was never going to tell us.

## Step 0 — Prereqs (both phases)

These must land before any wrapping code is written. Several are
themselves unscoped today; some are concrete prereqs, some are
investigations whose output shapes the rest of the work.

* **Pin SHAs.** `adcp-client-python@<sha>`, storyboard
  `media_buy_seller@<sha>`, storyboard
  `media_buy_guaranteed_approval@<sha>`, GAM sandbox Network ID
  `<id>` documented in the experiment README. Without pins, Phase 2's
  "is it the storyboard or our wrap" debugging burns days.
* **Identify the `_impl` seams in salesagent.** Self-review correctly
  flagged that wrapping `google_ad_manager.py` is a port disguised as
  a wrap — the adapter doesn't own buyer/principal resolution, tenant
  config, currency validation, signal lookup, audit logs, workflow
  rows, or webhook scheduling. Salesagent's CLAUDE.md Pattern #5
  establishes `_impl` functions as the transport-agnostic seam:
  `_create_media_buy_impl`, `_update_media_buy_impl`,
  `_add_creative_assets_impl`, `_get_products_impl`. **The wrap target
  is the `_impl`, not the adapter.** If any of these `_impl`s don't
  exist or aren't transport-agnostic enough today, that's a
  prerequisite refactor in salesagent before the side-car experiment
  starts.
* **Enumerate AST structural guards** that fire on `src/sdk_runtime/`.
  Salesagent has ~25 guards via `make quality` — no raw `select()`
  outside repos, no `get_db_session()` in `_impl`, schema inheritance
  from adcp library, repository pattern enforcement, single alembic
  head. Decide: (a) earn allowlist entries for each (and document
  each as an architectural finding), or (b) move `src/sdk_runtime/`
  outside `src/` so the side-car isn't really inside salesagent.
  Either is defensible; not deciding is not.
* **Enumerate cross-tenant background services to disable** for the
  experiment tenant: `background_sync_service`,
  `background_approval_service`, `delivery_webhook_scheduler`,
  `protocol_webhook_service`. Per-tenant disable is doable but
  invisible in the test tenant's behavior unless explicitly listed.
  Document the disable mechanism (env var? tenant-scoped config?
  process-level kill switch?) before relying on it.
* **Validate `_already_approved` survives SDK re-validation.**
  Salesagent uses `setattr(request, "_already_approved", True)` on a
  Pydantic model (`media_buy_create.py:529`). Salesagent runs
  `extra="forbid"` (Pattern #7); the SDK runtime presumably
  re-validates on inbound. Confirm the sentinel survives the
  projection round-trip. If it doesn't, prototype the typed
  alternative (`ctx.resumption_token`) on day 1, before the HITL gate
  is wired.
* **Document a test buyer that validates HMAC signatures.** §3.14's
  claim that adopters delete their webhook plumbing is only validated
  if a real subscribed buyer accepts SDK-signed webhooks. Identify the
  test buyer up front; verify signature parity between salesagent's
  `webhook_authenticator.py` scheme and SDK F12 / `WebhookSender`
  before the storyboard run, not during.

## Phase 1 — `dynamic_products.py` recipe falsification

**Slice.** Port `dynamic_products.py`
(`src/services/dynamic_products.py`, 505 LOC plus the AI services in
`src/services/ai/`) to a `ProposalManager.get_products` wrapper as a
self-contained unit. No GAM, no admin UI, no DB rewiring, no runtime
routing. Drive it from recorded inputs:

* Recorded buyer briefs (synthetic or pulled from prod transcripts)
* Recorded signal-agent outputs (the
  `signals_agent_registry.SignalsAgent.get_signals` returns)
* Recorded `Product` table rows (a fixture catalog)

The wrapper takes a brief, calls into the existing
`dynamic_products.py` body, projects the resulting signal-driven
variant `Product` rows into wire `Product[]` with
`implementation_config` recipes per #502.

**Exit criteria for Phase 1.**

1. Recipe shape carries `implementation_config` for both static
   catalog products AND signal-driven variants without
   `extra: dict[str, Any]` typed escape hatches. Salesagent's actual
   `implementation_config` content (line item template ids, ad unit
   ids, KV targeting, signal mappings, frequency caps) is the
   ground truth.
2. Glue LOC ≤ 60% of `dynamic_products.py` body. Source is 505 LOC,
   so glue target is ≤ 300 LOC. **The 60% ratio is the falsifiable
   threshold; the 300 absolute is a derived target. If the source
   grows, the ratio holds.**
3. At least one finding that **contradicts or materially refines** a
   #502 prior. The point of a falsification is to find something
   wrong; an experiment that confirms every prior is uninformative.
   Pre-register the candidate contradictions before running:
   - Q1.5 (recipe assembly vs. lookup): if salesagent generates
     signal-driven variants at brief time, that's proposal-time
     *assembly*, not lookup of pre-existing recipes — and #502's
     "framework session cache against `proposal_id`" model may be
     too restrictive.
   - Q2 (recipe escape hatches): if the wire-shape recipe can't
     carry GAM's full `implementation_config` without `extra`, the
     typed-recipe model is wrong.

**If Phase 1 dies.** Document findings. Side-car experiment doesn't
run; #502 needs revision first. Cheap, fast falsification done its
job.

**If Phase 1 passes.** Proceed to Phase 2 with the recipe model
validated. The side-car experiment focuses on the runtime, HITL,
webhook, and decisioning seams — questions Phase 1 can't answer.

## Phase 2 — side-car runtime (gated on Phase 1)

**One salesagent test tenant we own**, configured for GAM, serving
two storyboards end-to-end against a sandbox GAM Network ID:

* `media_buy_seller` — the 9 core lifecycle scenarios, instant
  approval path, exercises the wire shape end-to-end
* `media_buy_guaranteed_approval` — exercises HITL approval flow,
  the `compose_method` + `ShortCircuit` claim against salesagent's
  actual `WorkflowStep` mechanism

In scope:

* **Salesagent admin UI** (`src/admin/`) — UNCHANGED. Operators
  configure tenants, products, principals, approval queues through
  existing Flask blueprints.
* **Salesagent Postgres schema** (`src/core/database/models.py`) —
  UNCHANGED. The side-car runtime reads/writes the same tables.
  Self-review correctly flagged this as a cross-runtime contract on
  shared rows, not a tenant-isolated boundary: `WorkflowStep` rows
  written by the SDK's `before` hook must be re-readable by admin
  UI's `approve_workflow_step` and re-callable into the SDK runtime
  via the rewritten `execute_approved_media_buy`.
* **A new side-car process** running adcp-client-python's
  `adcp.serve(...)` with:
  - `BuyerAgentRegistry` projecting from `Principal` rows
    (`access_token` bearer lookup; ~80 LOC)
  - `AccountStore` projecting from `Account` rows (already
    AdCP-shaped — ~70 LOC)
  - `GAMProposalManager` wrapping `_get_products_impl` (the
    transport-agnostic seam, after Phase 1 validated the recipe
    shape)
  - `GAMDecisioningPlatform` wrapping `_create_media_buy_impl`,
    `_update_media_buy_impl`, `_get_media_buy_delivery_impl`
  - HITL `before` hook on all three mutating ops, plus
    `_add_creative_assets_impl` (the **third** approval re-entry
    surface — see HITL section below)
  - `WebhookSender` configured with signing parity verified at
    Step 0 against the test buyer
* **Real GAM upstream** — actual orders/line items in a sandbox
  network. Real auth (read from `AdapterConfig`), connection
  pooling, error projection.

* **HITL approval lifecycle (three surfaces).**
  Self-review surfaced that `_already_approved` is one of three
  re-entry surfaces, plus creative approval has its own re-entry
  through `order_approval_service.py` and
  `background_approval_service.py`. Either we exercise all of them
  or we scope the others out explicitly.

  **Decision: in scope** — `create_media_buy`, `update_media_buy`,
  `add_creative_assets`. Creative-approval-specific re-entry through
  `order_approval_service.py` is **out of scope** for v1 (creative
  flows are deferred regardless); revisit if `media_buy_guaranteed_approval`
  storyboard happens to exercise it.

  **How salesagent does HITL today** (verified, file:line):

  1. `_create_media_buy_impl` (or the GAM adapter at
     `google_ad_manager.py:571`) checks
     `gam_manual_approval_required` from `AdapterConfig`
     (`models.py:1129`, **tenant-scoped, not account-scoped**) AND
     `not getattr(request, "_already_approved", False)`. If gated:
     writes `WorkflowStep(status="pending_approval")` +
     `ObjectWorkflowMapping` linking step → media buy + `MediaBuy`
     row carrying `raw_request` as JSON. Returns 200 with
     `workflow_step_id`. **No GAM call has happened.**
  2. Operator approves through admin UI. Flask route
     `approve_workflow_step` (`admin/blueprints/workflows.py:155`)
     directly imports `execute_approved_media_buy`
     (`media_buy_create.py:458`) — same process, same DB.
  3. `execute_approved_media_buy` reconstructs the request from
     `MediaBuy.raw_request`, sets `request._already_approved = True`
     (`media_buy_create.py:529`), re-calls. Gate skips; real GAM
     call fires.

  **What that means for the SDK runtime.** No paused-task resumption
  needed. Salesagent's `WorkflowStep` table IS its task registry; we
  keep using it. The before-hook writes the workflow row, the admin
  UI route stays untouched, and `execute_approved_media_buy` gets
  rewritten to call back into the SDK runtime instead of the legacy
  tool function.

* **Webhook delivery via SDK F12 auto-emit.** Per-tenant disable for
  `protocol_webhook_service.py` (verified at Step 0); `WebhookSender`
  configured with signing scheme that matches salesagent's
  `webhook_authenticator.py`. Verified end-to-end against the test
  buyer at Step 0, not just "did something fire."

Out of scope (deliberate):

* Other adapters (Kevel, Broadstreet, Triton, Xandr, mock) — keep
  serving on the existing runtime FOR THE EXPERIMENT. Per the
  reframing above, they're slated for post-experiment deletion;
  the experiment doesn't preserve compatibility, just doesn't
  break them mid-run.
* Other tenants. We control the experiment tenant; nothing real
  rides on it.
* Refine flow / proposal lifecycle (`finalize`, `expires_at`,
  draft → committed). Wire later if v1 is stable.
* Push reporting (`reporting_webhook` / `reporting_bucket`).
* Creative agent / creative builder.
* Creative-approval-specific re-entry through
  `order_approval_service.py` / `background_approval_service.py`
  unless the HITL storyboard happens to exercise it.

## Architecture

Self-review correctly flagged that "same process, two runtimes,
tenant-routed" buries the hard problem. Both runtimes own MCP+A2A
transport mounts, FastMCP tool registries, identity resolution, and
ResolvedIdentity construction. Routing must happen *before* either
runtime's transport layer claims the request.

**Decision: separate process, nginx-level routing by tenant header.**

```
┌────────────────────────────────────────────────────────┐
│  nginx — routes by `X-Tenant-Id` header                 │
│   experiment-tenant → :3001 (side-car)                  │
│   everything else   → :3000 (existing runtime)          │
└────────────────┬──────────────────────┬─────────────────┘
                 │                      │
        ┌────────▼────────┐      ┌──────▼────────────┐
        │ existing salesagent │   │  side-car process │
        │ runtime (port 3000) │   │  (port 3001)      │
        │  adcp_a2a_server.py │   │  adcp.serve(...)  │
        │  mcp_server_*.py    │   │   GAMProposalMgr  │
        └────────┬────────────┘   │   GAMDecisioning  │
                 │                └──────┬────────────┘
                 │                       │
                 ↓                       ↓
        ┌────────────────────────────────────────────────┐
        │  Salesagent admin UI (Flask) — unchanged       │
        │  Salesagent Postgres — shared schema           │
        │   Tenant, Principal, Account, Product,         │
        │   MediaBuy, WorkflowStep, ObjectWorkflowMapping│
        └────────────────────────────────────────────────┘
                                 │
                                 ↓
                        real GAM upstream
                       (Phase 2 only)
```

Why separate process beats in-process dispatch:

* No dual MCP+A2A transport mounts in one ASGI app
* No FastMCP tool registry collisions
* No double-initialization of `ResolvedIdentity` construction (a
  structural guard in salesagent)
* Independent lifecycle: side-car restarts without touching the
  existing runtime
* Killable: turn off the side-car port, traffic falls back to the
  existing runtime, experiment is over
* No SQLAlchemy model / metadata import-time conflicts (single
  alembic head stays single)

The cost: nginx config change in dev, plus an env-var or tenant
metadata flag for the `X-Tenant-Id` header injection. Cheap.

## Wrap, don't port (Phase 2)

The discipline is to wrap **the `_impl` functions**, not adapters.
Salesagent's `_impl` seam (Pattern #5 in salesagent's CLAUDE.md) is
already transport-agnostic — `_create_media_buy_impl` takes a
typed request + tenant context and returns a typed response. The
SDK runtime calls `_impl` directly with projected requests; the
adapter underneath stays untouched.

The wrong target — `google_ad_manager.py` — would force the side-car
to re-implement principal resolution, tenant config loading,
currency validation, signal lookup, audit logger wiring, workflow
row creation, webhook scheduling. That's the 3,930 LOC of
`media_buy_create.py` getting smuggled into the wrap. The right
target — `_create_media_buy_impl` — already absorbs all of that.

For Step 0: confirm `_create_media_buy_impl`,
`_update_media_buy_impl`, `_add_creative_assets_impl`,
`_get_products_impl` exist and are transport-agnostic. If not,
introducing them is a salesagent-side prerequisite refactor.

## Exit criteria (multi-criteria, not binary)

A passing storyboard with wrappers that grew enormous tells us
nothing about whether the architecture works. The exit criteria
must be co-equal — all of them, not just (1).

1. **Both storyboards pass** — `media_buy_seller` (9 core happy
   path) AND `media_buy_guaranteed_approval` (HITL exercise),
   against a sandbox GAM Network ID, with real GAM line items
   created.
2. **Recipe carries `implementation_config` without escape
   hatches** — no `extra: dict[str, Any]`, no untyped passthrough.
   GAM's full implementation_config (line item template ids, ad
   unit ids, KV targeting, frequency caps, signal mappings) fits
   the typed recipe shape.
3. **Glue LOC under ratio thresholds** — proposal-side glue ≤ 60%
   of `dynamic_products.py` body (≤ 300 LOC against 505 source);
   decisioning-side glue ≤ 30% of `_create_media_buy_impl` body.
   Pre-register thresholds before measuring.
4. **Zero structural-guard allowlist additions** — or every
   addition is documented as an architectural finding requiring a
   spec response. Salesagent's guards encode design constraints;
   bypassing them silently means the side-car is violating
   constraints we should be debating.
5. **At least one of the five learning questions has an answer
   that contradicts a #502 prior** — pre-register the candidate
   contradictions before running. An experiment that confirms
   every prior is tautological; pre-registration prevents
   post-hoc rationalization of "what we found."
6. **Webhook signature verified by a subscribed test buyer** —
   not just "WebhookSender fired something." Buyer must validate
   HMAC and accept the delivery, end-to-end.

If any of (1)-(6) fails, the experiment is informative — it tells
us where the architecture breaks. Don't paper over a partial pass
by relaxing a criterion mid-run.

## What we expect to learn (five questions + Q1.5)

Pre-register which signal would falsify each prior, before running.
Self-review's "one author wearing three hats" warning applies — if I
don't commit upfront to what would tell me I'm wrong, I'll find what
I'm looking for.

1. **Does `dynamic_products.py` factor onto
   `ProposalManager.get_products`?** Phase 1 answers this in
   isolation. Falsifier: the wrap exceeds 60% of source LOC, or
   requires escape hatches in the recipe.

2. **Does the recipe carry enough?** GAM's
   `implementation_config` (`Product.implementation_config: JSONType`,
   `models.py:256`) is the most-evolved form. Falsifier: any
   `extra: dict[str, Any]` field on the recipe, or an untyped
   passthrough mechanism.

3. **What does framework-owned proposal lifecycle actually need?**
   Three plausible answers (session cache, DB row, fresh lookup);
   the experiment forces a choice. Falsifier: none of the three
   work — the framework needs primitives #502 doesn't anticipate.

4. **What is the right shape for the HITL resumption marker?**
   Salesagent's HITL model (gate → persist → admin approves →
   re-call same code path with sentinel) maps cleanly onto
   `compose_method` + `ShortCircuit` without needing the SDK's
   `TaskRegistry` to round-trip. The open seam is the marker
   itself. Two candidate shapes:
   - (a) typed `ctx.resumption_token: ResumptionToken | None`
     carried through framework dispatch
   - (b) keep the marker adopter-side entirely, the SDK doesn't
     model it

   **Sub-question on the resumption substrate.** Salesagent's
   `MediaBuy.raw_request` JSON is the actual resumption payload.
   The SDK has no concept of it. The "typed
   `ctx.resumption_token`" option only works if the SDK can carry
   the equivalent through dispatch — or if the adopter is
   responsible for rebuilding the request before calling back in.
   The experiment forces a decision here too; it's not a free
   choice.

   **Important caveat (self-review N=1).** Salesagent's pattern
   is DB-persisted re-call with a sentinel — unrepresentative of
   paused-coroutine resumption shapes other adopters might use.
   The experiment can answer "does the SDK seam accommodate
   salesagent's shape." It **cannot** answer "what's the right
   Protocol seam for `ProposalManager` in general." Decoupling
   matters: the experiment informs a Protocol RFC; the spec
   decision lives in a separate doc after the experiment, not
   inherited from what felt natural in salesagent.

5. **Does F12 webhook auto-emit hold up under real load?** Per
   the §3.14 claim. Falsifier: signing semantics don't match the
   subscribed test buyer's expectations, or anything leaks
   through to adapter code.

**Q1.5 — Does the recipe model allow proposal-time *assembly*,
not just lookup?** Self-review surfaced this. PR #502 says the
recipe "lives in framework session cache against `proposal_id`"
during refine, then "framework persists it" on finalize.
Salesagent's `implementation_config` is on the *Product* row
(`models.py:256`), and dynamic products generate signal-driven
variant Product rows at brief time. Those are new Product rows
the framework doesn't know about until the brief lands. The
recipe-as-framework-managed-state model probably has to allow
proposal-time recipe *assembly*, not just lookup.

Phase 1 is the cheapest place to falsify this. If the wire shape
can't carry signal-driven variants without escape hatches, #502's
recipe model needs revision.

## Risks (revised)

* **Wrap target drift.** Mitigated by Step 0 `_impl` identification
  and the discipline that wraps wrap `_impl` not adapters. If any
  needed `_impl` doesn't exist in transport-agnostic form, that's a
  salesagent-side refactor before the side-car experiment runs.
* **Webhook signature parity is a real risk, not paperwork.**
  Mitigated by Step 0 dry-run against a subscribed test buyer.
* **`_already_approved` may not survive `extra="forbid"` projection.**
  Mitigated by Step 0 validation. If it doesn't survive, prototype
  typed marker on day 1.
* **Cross-runtime contract on shared rows.** `WorkflowStep` and
  `MediaBuy.raw_request` are written by the SDK runtime's before-hook
  and read/re-fired by the admin UI's approval handler. This is a
  contract on a shared schema, not a tenant-isolated boundary. The
  test tenant being controlled doesn't change the contract; it just
  makes a contract bug recoverable instead of catastrophic.
* **Three HITL re-entry surfaces, not one.** `create_media_buy`,
  `update_media_buy`, `add_creative_assets` — all in scope.
  Creative-specific re-entry through `order_approval_service.py`
  out of scope for v1. Easy to invisibly skip one; risk is "looks
  like it works" with one path missed.
* **HITL marker decision is N=1.** Mitigated by decoupling — the
  experiment informs but doesn't settle the Protocol seam. Spec PR
  comes after, with the experiment as one data point among multiple.
* **One author wearing three hats** (SDK author, salesagent adopter,
  reviewer). Mitigated by pre-registering falsification signals
  before running. Self-review caught this; pre-registration is the
  enforcement mechanism.
* **Storyboard test-controller methods + real GAM are in tension.**
  Test-controller methods (`force_*`, `simulate_delivery`) bypass
  upstream determinism by design. Hybrid mode: real GAM for
  `create_media_buy` / `update_media_buy` mutations; salesagent's
  existing `delivery_simulator.py` wired into the side-car for
  delivery simulation and state forcing. The hybrid maintains
  storyboard determinism without sacrificing the GAM-live test for
  mutations.
* **Two writers, one DB — controlled, but with a contract.** The
  experiment tenant is a test tenant we own; duplication is
  operationally fine. But salesagent's SQLAlchemy models in
  `models.py` (2,113 LOC) and the side-car's schema view must agree
  exactly on cascade/relationship semantics. Single alembic head
  stays salesagent's.

## Workstream

Step 0 is required for both phases. Phase 1 runs to completion before
Phase 2 starts; Phase 2 is gated on Phase 1's findings being
consistent with #502.

**Step 0 — Prereqs (~1-2 days, mostly investigation).**

0.1. Pin `adcp-client-python@<sha>`, both storyboard SHAs, GAM
     Network ID; document in experiment README.
0.2. Identify (or refactor for) `_impl` seams:
     `_create_media_buy_impl`, `_update_media_buy_impl`,
     `_add_creative_assets_impl`, `_get_products_impl`. Confirm
     transport-agnostic.
0.3. Run salesagent's `make quality` against a stub
     `src/sdk_runtime/` directory; enumerate every guard that fires;
     decide allowlist-with-justification or alternate location.
0.4. Enumerate cross-tenant background services to disable for
     experiment tenant; document the disable mechanism.
0.5. Write a unit test that validates `setattr(request,
     "_already_approved", True)` survives the SDK's request
     projection round-trip under `extra="forbid"`. If it doesn't,
     prototype the typed-marker alternative.
0.6. Identify a test buyer that validates HMAC signatures; verify
     SDK F12 / `WebhookSender` signing parity with salesagent's
     `webhook_authenticator.py` against the test buyer.
0.7. Pre-register the candidate contradictions for each of the five
     learning questions (which finding would tell us each prior is
     wrong).

**Phase 1 — `dynamic_products.py` recipe falsification (~1 day).**

1.1. Build `ProposalManager.get_products` wrapper around
     `dynamic_products.py`. No GAM, no admin UI, no DB rewiring.
     Drive from recorded fixtures.
1.2. Assert the output recipe carries every variant Product without
     escape hatches.
1.3. Measure glue LOC against `dynamic_products.py` body; assert
     ratio ≤ 60%.
1.4. Document findings against the pre-registered contradictions.
1.5. **Decision point: proceed to Phase 2 or stop and revise #502.**

**Phase 2 — side-car runtime (~3-5 days, gated on Phase 1).**

2.1. Auth shim — `BuyerAgentRegistry` reading `Principal`
     (`access_token` bearer; ~80 LOC) + `AccountStore` reading
     `Account` (already AdCP-shaped; ~70 LOC).
     `AgentAccountAccess` cross-check for access scoping.
2.2. `GAMDecisioningPlatform` wrapping `_create_media_buy_impl`
     and `_update_media_buy_impl`. `GAMProposalManager` wrapping
     `_get_products_impl` (now informed by Phase 1's findings).
2.3. HITL gate via `compose_method`. `before` hook on all three
     mutating ops, consulting
     `AdapterConfig.gam_manual_approval_required` (tenant-scoped).
     Rewrite `execute_approved_media_buy` body to reconstruct
     request, attach resumption marker, call back into side-car
     runtime. Admin UI route untouched.
2.4. `WebhookSender` configured on `serve(...)` with the signing
     parity verified at Step 0. Per-tenant disable for
     `protocol_webhook_service.py`.
2.5. Test-controller hybrid: implement `simulate_delivery`,
     `force_*` methods using salesagent's existing
     `delivery_simulator.py`; real GAM for `create_media_buy` /
     `update_media_buy`.
2.6. Nginx routing config: `X-Tenant-Id` header → side-car port
     for experiment tenant.
2.7. Storyboard runs:
     - `media_buy_seller` against the experiment tenant
     - `media_buy_guaranteed_approval` against the experiment
       tenant (HITL exercise)
2.8. Findings doc — what ported cleanly, what didn't, which
     resumption-marker shape reads cleanly at the gate site, plus
     any of the five questions that produced a contradicting
     finding.

## Next steps after experiment

If exit criteria (1)-(6) all pass:

1. **`ProposalManager` Protocol RFC** — separate spec PR, informed
   by but not inherited from the experiment. Open the design call
   to other adopters before settling shape.
2. **Land `MockProposalManager` forwarder** per #502.
3. **Update [#489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
   §3.3** with experiment findings; remove "ProposalManager will
   own X" hedging where the experiment proved it.
4. **Storyboard the experiment as a worked example** in
   `examples/salesagent_sidecar/` (with credentials redacted).
5. **Adapter deprecation roadmap.** Salesagent is a GAM agent; the
   registry pattern is vestigial. Sequenced deletion: (a) delete
   Kevel, Broadstreet, Triton, Xandr adapter packages; (b) collapse
   `media_buy_create.py`'s GAM-specific switches into unconditional
   logic; (c) delete `mock_ad_server.py` (~1,800 LOC, replaced by
   SDK `Account.mode='mock'` + `bin/adcp.js mock-server`); (d)
   delete the `ADAPTER_REGISTRY` itself once no callers remain.
   Each step lands as its own PR with storyboard validation. Total:
   ~3,500-4,000 LOC deletion.
6. **Promote the side-car to primary runtime** for salesagent.
   With other adapters deleted, the existing `adcp_a2a_server.py`
   (2,276 LOC) and `mcp_server_enhanced.py` paths can be retired in
   favor of `adcp.serve(...)`. Cutover happens tenant by tenant; the
   side-car shape stays revertible until the last tenant migrates.

If any criterion fails:

1. Document where the model broke in
   `docs/proposals/product-architecture-revision-1.md`.
2. Revise [#502](https://github.com/adcontextprotocol/adcp-client-python/pull/502)
   against the real failure mode.
3. Decide whether to rerun the experiment against the revised
   model or whether the next falsifier should come from a
   different adopter (agentic-adapters social shapes).

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
  `src/core/tools/media_buy_create.py:458` (`execute_approved_media_buy`),
  `src/core/tools/media_buy_create.py:529` (`_already_approved` sentinel),
  `src/adapters/google_ad_manager.py:571` (HITL gate),
  `src/admin/blueprints/workflows.py:155` (`approve_workflow_step`),
  `src/core/database/models.py:1129` (`gam_manual_approval_required`),
  salesagent's CLAUDE.md Pattern #5 (`_impl` seam) and Pattern #7
  (`extra="forbid"`).
