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

These must land before any wrapping code is written. Several were
investigations whose output shapes the rest of the work; results
folded in below. Items still TBD are flagged.

**Constraint: local fork edits only.** All salesagent-side changes
this experiment requires (scheduler skips, the `execute_approved_media_buy`
rewrite to call into the side-car runtime) live as patches in our
experiment fork. **No upstream PRs to salesagent.** This makes the
experiment fully revertible (`git checkout main` in the fork resets
everything) and lets us be more aggressive with local changes than
upstream review would allow.

### Investigated (results below)

* **`_impl` seams in salesagent — confirmed clean.** All four
  required `_impl` functions exist as transport-agnostic functions
  (Pydantic request + `ResolvedIdentity`, no FastMCP/Flask types in
  signature):
  - `_create_media_buy_impl` (`media_buy_create.py:1270`, async)
  - `_update_media_buy_impl` (`media_buy_update.py:117`, sync)
  - `_get_products_impl` (`products.py:145`, async)
  - `_get_media_buy_delivery_impl` (`media_buy_delivery.py:67`, sync)
  - `_sync_creatives_impl` (`creatives/_sync.py:29`, sync) —
    **substitutes for the imagined `_add_creative_assets_impl`**

  `_add_creative_assets_impl` doesn't exist. The HITL gate at
  `google_ad_manager.py:880` checks the GAM-internal operation name
  `add_creative_assets`; at the wire level the equivalent is
  `sync_creatives`. **Implication:** the SDK runtime's HITL
  before-hook needs an operation-name mapping (GAM-internal →
  AdCP-wire) since salesagent's `manual_approval_operations` config
  keys on GAM-internal names. Small, but not zero.

  Bottom line: **zero salesagent-side refactor required** before
  Phase 2. Wrap targets exist. The pattern is real and pervasive
  (14 `_impl` functions across `src/core/tools/`).

* **Structural guards — much smaller than feared.** `make quality`
  runs ruff format/check, mypy, `check_code_duplication.py`, and
  unit tests (`Makefile:8-13`). ~25 custom guards live in
  `.pre-commit-hooks/` + `.pre-commit-config.yaml`. **Most are
  path-filtered out** of `src/sdk_runtime/`:
  - `check-tenant-context-order` — `^src/core/tools/.*\.py$` only
  - `enforce-jsontype` — `models.py` only
  - `mcp-contract-validation` / `mcp-schema-alignment` —
    `schemas.py` / `main.py` only
  - migration guards — alembic only
  - test guards (`no-skip-tests`, `ast-grep-bdd-guards`) — tests only

  Guards that DO apply to `src/sdk_runtime/`: project-hygiene only
  (sqlalchemy 2.0 patterns, no `hasattr(x, 'root')`, no `.fn()`
  calls, import usage, type:ignore count, code duplication). All
  trivially satisfiable with normal coding.

  One worth inspecting before relying on the prediction:
  **`check-parameter-alignment`** verifies MCP/A2A wrappers pass
  aligned params to `_impl`. The side-car is a *third* caller of
  `_impl`. Whether the guard accepts a third call site or assumes
  exactly two needs a quick read of
  `.pre-commit-hooks/check_parameter_alignment.py`.

  Bottom line: **zero allowlist additions likely needed**, possibly
  one. Keep `src/sdk_runtime/` inside `src/`.

* **Cross-tenant background services — only two, not four.**
  Investigation found that `protocol_webhook_service`,
  `background_approval_service`, `order_approval_service`,
  `background_sync_service` all fire per-request or per-order, NOT
  cross-tenant. They don't need per-tenant disable — the side-car
  routing decides when they fire.

  Two genuinely cross-tenant schedulers, both started from
  `core/main.py` lifespan:
  - **`media_buy_status_scheduler.py`**
    (`core/main.py:95`, 60s cadence): auto-transitions `MediaBuy`
    lifecycle (pending_activation → active → completed) by flight
    dates, cross-tenant. **Would race with the side-car's
    lifecycle handling.**
  - **`delivery_webhook_scheduler.py`**
    (`core/main.py:85`, daily): sends `reporting_webhook` reports
    cross-tenant. **Would fire duplicate webhooks alongside SDK F12
    auto-emit.**

  **Concrete prereq (local fork patch, not upstream PR):** add a
  per-tenant skip in our experiment fork. Two viable shapes:
  - (a) hardcoded constant in each scheduler:
    `EXPERIMENT_TENANT_IDS = {"tenant_acme_test"}` consulted in the
    cross-tenant query
  - (b) env var: `SKIP_TENANT_IDS=tenant_acme_test` parsed at
    scheduler start

  Either is ~6 lines per scheduler. Option (b) is slightly cleaner;
  doesn't matter since neither leaves the fork. Without this, the
  experiment tenant fights its own DB on a 60s cadence.

### Investigated this round

* ✅ **`check_parameter_alignment.py` analyzed.** The guard
  enumerates pairs of `(mcp_wrapper, a2a_raw)` from a hardcoded
  `tools` list (`.pre-commit-hooks/check_parameter_alignment.py:36-78`)
  and checks signature alignment for those specific named functions.
  It does NOT enumerate "all callers of `_impl`." A side-car's
  `GAMDecisioningPlatform.create_media_buy` calling
  `_create_media_buy_impl` is invisible to the guard. **Confirmed:
  zero allowlist additions needed.**

* ✅ **`_already_approved` sentinel works as-is** for the SDK
  runtime. Verified via:
  - `compose_method` (`src/adcp/decisioning/compose.py:173-194`)
    passes `req` through unchanged from before-hook to inner; no
    re-validation, no `model_copy`, no `model_dump` on the request
    side.
  - The dispatcher (`src/adcp/decisioning/dispatch.py`) only
    `model_dump`s on the response side (lines 1234, 1306-1307);
    request objects flow through as-is.
  - Generated request models use `extra='forbid'` (validation-time
    only) without `frozen=True` or `validate_assignment=True`, so
    `setattr(req, "_already_approved", True)` lands in `__dict__`
    and persists through Python-level dispatch.

  The sentinel is stripped on JSON serialization (which is
  intentional — buyers can't smuggle it). In-process resumption via
  Python function call preserves it. **Salesagent's existing pattern
  ports to the SDK runtime without a typed marker for this
  experiment.** The Q4 design question (typed vs untyped resumption
  marker) remains open as a Protocol RFC, but the experiment can run
  with the untyped pattern that already works.

* ⚠️ **Webhook signing parity does NOT hold** between salesagent's
  scheme and SDK `from_adcp_legacy_hmac`. Salesagent
  (`src/core/webhook_authenticator.py:14-47`) emits:
  - Headers: `X-Webhook-Signature: sha256=<hex>` + `X-Webhook-Timestamp`
  - Canonicalization: `f"{timestamp}.{json.dumps(payload, separators=(",",":"), sort_keys=True)}"`

  SDK `from_adcp_legacy_hmac` (`src/adcp/webhook_sender.py:404`,
  `src/adcp/webhook_auth.py`) emits:
  - Headers: `X-AdCP-Signature` + `X-AdCP-Timestamp` + `X-AdCP-Key-Id`
  - Different canonicalization (per `adcp.signing.standard_webhooks`)

  Different headers, different canonicalization, different scheme
  entirely. **§3.14's claim that "adopters delete their webhook
  plumbing wholesale" doesn't hold cleanly** — production cutover
  requires buyer migration to the SDK signing scheme.

  For the experiment: SDK→SDK signing works (use
  `adcp.WebhookReceiver` as the test buyer with the same secret).
  This validates that SDK signing is internally consistent. It does
  NOT validate that buyers subscribed to salesagent today will
  accept SDK signatures — they won't. **§3.14 of the migration
  guide needs a correction**: webhook plumbing deletion is
  contingent on buyer migration, not unconditional.

### Still TBD before Phase 2 starts

* **Pin SHAs.** `adcp-client-python@<sha>`, storyboard
  `media_buy_seller@<sha>`, storyboard
  `media_buy_guaranteed_approval@<sha>`, GAM sandbox Network ID
  `<id>` documented in the experiment README. Without pins, Phase 2's
  "is it the storyboard or our wrap" debugging burns days.
* **Patch the two cross-tenant schedulers in the experiment fork**
  (per Step 0.4 above): hardcoded skip or env-var skip for the
  experiment tenant ID. Local fork patch only.
* **Pre-register the candidate contradictions** for each of the
  five learning questions (Step 0.7 in workstream).

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
    `_update_media_buy_impl`, `_get_media_buy_delivery_impl`,
    `_sync_creatives_impl`
  - HITL `before` hook on all three mutating ops (the
    `_add_creative_assets_impl` referenced in earlier drafts doesn't
    exist; the wire-level equivalent is `sync_creatives` and the
    GAM-internal HITL operation name `add_creative_assets` maps to
    it via a small operation-name table on the before-hook)
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
  `sync_creatives` (the wire surface; salesagent's HITL config
  references it as `add_creative_assets` per its GAM-internal
  operation name, mapped via a small table on the before-hook).
  Creative-approval-specific re-entry through
  `order_approval_service.py` is **out of scope** for v1 (creative
  flows are deferred regardless); revisit if
  `media_buy_guaranteed_approval` storyboard happens to exercise it.

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

## Phase 1 — early findings from code reading (1A)

Phase 1 is in two parts: **1A** is a careful read of `dynamic_products.py`
to surface findings that don't require a running harness; **1B** is the
empirical run with fixtures + projection to wire shape.

1A complete, 1B TBD. Here's what 1A produced.

### What `dynamic_products.py` actually does

* `generate_variants_for_brief(tenant_id, brief, our_agent_url)`
  (`src/services/dynamic_products.py:28`, async).
* Reads `Product` rows where `is_dynamic=True` from the DB
  (the "templates").
* Calls the singleton `SignalsAgentRegistry.get_signals(brief, ...)`
  with all configured signal agents for the tenant.
* For each (template, signal) pair, generates a variant `Product`
  via `generate_variants_from_signals` (`:133`):
  - Computes deterministic `variant_id =
    f"{template_id}__variant_{md5(activation_key)[:8]}"` (`:267`)
  - Looks up existing variant by `variant_id`; if found, extends
    `expires_at` and returns it
  - If not, creates a new `Product(**variant_data)` with
    `implementation_config` copied verbatim from the template
    (`:303`)
* `session.add(variant)` then `session.commit()` — variants are
  full DB rows.

### Five structural facts that bear on the experiment

1. **Variants are persistent DB rows**, not session-scoped state.
   They live in the same `products` table as static products,
   indexed and queryable globally.
2. **Variants share the template's `implementation_config`
   verbatim** (`:303`). The recipe shape doesn't differ between
   template and variant.
3. **Variant identity is globally deterministic.** Multiple briefs
   from any buyer hitting the same signal segment converge on the
   same `variant_id` via the md5 hash of the activation key.
4. **Variants carry signal-specific data on the Product row, NOT
   in `implementation_config`.** `signal_metadata`,
   `activation_key`, `parent_product_id`, `expires_at`,
   `is_dynamic_variant` are top-level Product columns.
5. **Variants have an independent lifecycle.** TTL via
   `expires_at`; archival via `archive_expired_variants()`
   (`:475`). The lifecycle has nothing to do with proposal
   acceptance, finalization, or buy creation.

### Falsifiers fired by 1A (no harness needed)

**Q1.5 — Does the recipe model allow proposal-time *assembly*?**
Three pre-registered falsifiers; **two confirmed**, one partial:

* ✅ **"Variant Products require new schema rows."**
  Confirmed. Salesagent variants are full `Product` rows with
  signal-derived columns (`signal_metadata`, `activation_key`,
  `parent_product_id`, `is_dynamic_variant`, `expires_at`)
  that don't fit a "recipe in session cache" abstraction.
* ✅ **"Hash-dedup state crosses sessions."**
  Confirmed. `generate_variant_id` is a deterministic hash;
  variants are deduplicated globally across all briefs from all
  buyers, not per-session.
* ⚪ **"Recipe schema requires `proposal_id` lookup."**
  Not directly fired (#502 doesn't strictly require this in its
  current draft), but related: salesagent's recipe is
  *Product-scoped*, not proposal-scoped. The framework
  abstraction in #502 conflates two layers — the recipe content
  (Product-scoped, stable) and the proposal/session state
  (per-buyer-session, transient). They don't need to share a
  cache.

**Implication for #502.** The "framework-managed recipe state
against `proposal_id`" framing in #502 is the wrong shape for
salesagent's pattern. The corrected model:

* Recipe lives on the Product (or its equivalent in adopter
  storage). Adopter-owned, not framework-owned.
* Framework's job at the seam is **typing** the recipe contract,
  not **caching** it. `recipe_type: ClassVar[type[Recipe]]` on
  `DecisioningPlatform` is the contract; the framework validates
  the shape at adapter boundaries, doesn't manage the storage.
* Proposal-time *assembly* — generating new Product rows that
  share a template's recipe — is adopter logic. The framework
  shouldn't try to cache "proposal recipes" because proposals
  don't own them.

**This is a contradicting finding for #502 — exit criterion (5)
satisfied early.** The recipe-as-framework-managed-state model
in the current draft of #502 needs revision. The simpler shape
(framework types the recipe contract; storage is adopter-owned)
fits salesagent without escape hatches.

### What Q1 looks like from reading

Q1 asks whether `dynamic_products.py` factors onto
`ProposalManager.get_products` via a thin wrapping. Reading the
code suggests the wrapper is **small** for the dynamic-products
subset:

```python
# Sketch — not run yet, awaiting 1B
async def get_products(
    self, req: GetProductsRequest, ctx: RequestContext[Any]
) -> GetProductsResponse:
    tenant_id = ctx.account.metadata["tenant_id"]
    our_agent_url = self.our_agent_url

    # Static catalog (existing path)
    static_products = await self._fetch_static_catalog(tenant_id, req)

    # Dynamic variants (the salesagent path)
    if req.brief:
        variant_products = await generate_variants_for_brief(
            tenant_id=tenant_id,
            brief=req.brief,
            our_agent_url=our_agent_url,
        )
    else:
        variant_products = []

    # Project all (static + variants) to wire shape
    all_products = [
        self._project_to_wire(p) for p in static_products + variant_products
    ]
    return GetProductsResponse(products=all_products)
```

**Predicted glue: ~50-80 LOC** for the dynamic-products subset
(request projection in, ORM-to-wire projection out). Well under
the 60% / 303 LOC threshold for the whole `dynamic_products.py`
body. **Q1 unlikely to fire on this subset.**

Caveat: the full `_get_products_impl` wrap (Phase 2) does more —
brand manifest filtering, brand-policy gates, AI ranking. That's
where Q1 might bite. 1A doesn't speak to that.

### Q2 (recipe carries enough) — preliminary read

Variants share template's `implementation_config` verbatim. So
the question reduces to: does the typed `GAMRecipe` shape carry
salesagent's actual `implementation_config` content? That's a
**1B** question — needs running variants and projecting their
`implementation_config` field through a typed Pydantic recipe.

Pre-registered falsifiers stand: any `extra: dict[str, Any]`,
any `# type: ignore`, any lossy round-trip. 1B will produce the
verdict.

### Phase 1B — harness still TBD

The empirical run requires:

* **Salesagent worktree** with a writable test DB (sqlite is
  fine).
* **Seeded `Product` template rows** with `is_dynamic=True` and
  configured `signals_agent_ids`. ~3 templates covering the
  shapes we expect (key_value activation, segment_id activation,
  null-activation fallback).
* **Mocked `signals_agent_registry`** — replace the singleton
  with a fixture that returns deterministic signal lists. The
  registry currently lives in `src/core/signals_agent_registry.py`
  as a module-level singleton; mock via patching the module
  attribute or via the `get_signals_agent_registry()` accessor.
* **The wrapper module** (sketched above) at
  `src/sdk_runtime/proposal_manager_wrapper.py` in the salesagent
  worktree.
* **The typed `GAMRecipe` Pydantic model** — needs writing,
  informed by reading actual `implementation_config` JSON from
  salesagent fixtures (or a dev DB).
* **The test harness** — pytest test that calls the wrapper with
  recorded inputs, asserts the output, measures glue LOC,
  documents any escape hatches encountered.

Not done in this session. The setup is concrete and small (~2
hours of work in a salesagent worktree), but it requires
salesagent fixtures and a running `SignalsAgentRegistry` mock —
both outside the scope of an adcp-client-python doc-writing
session. **Next session in a salesagent worktree completes 1B.**

### Phase 1A net result

* **Q1.5 contradicting finding for #502** — confirmed without
  running anything. The "framework-managed recipe state" model
  is wrong shape; recipe is Product-scoped and adopter-owned;
  framework's job is to type the contract.
* **Q1 prediction** — wrapper is small (~50-80 LOC) for the
  dynamic-products subset. 1B will measure exactly.
* **Q2 still pending** — needs 1B run with real
  `implementation_config` values projected through a typed recipe.
* **Exit criterion (5) satisfied early** — at least one
  contradicting finding, pre-registered, fired before 1B.

The experiment can proceed to 1B / Phase 2 with the recipe model
revision in mind. **Or**, given the contradicting finding, we
revise #502 first and then run 1B against the revised model. The
falsifier already fired; running 1B confirms the empirical edges
but doesn't change the structural conclusion.

## Pre-registered falsification signals

Self-review's "one author wearing three hats" warning applies — if I
don't commit upfront to what would tell me each prior is wrong, I'll
find what I'm looking for. For each learning question, the specific
finding that would falsify the prior is named here, before the
experiment runs. **A finding that contradicts any of these is a
positive result — it's what the experiment is for.**

### Q1 — Does `dynamic_products.py` factor onto `ProposalManager.get_products`?

Prior: salesagent's signal-driven assembly fits the
`ProposalManager.get_products` shape via a thin wrapping that calls
into the existing 505-LOC body without re-implementing it.

Falsified if any of:

* **LOC budget exceeded.** Glue exceeds 60% of source body
  (>303 LOC against 505). Hard threshold; pre-registered.
* **Wrap-as-port.** The wrapper has to re-execute logic from inside
  `dynamic_products.py` rather than calling it as-is — e.g.,
  re-running `signals_agent_registry` lookup, rebuilding variant
  products from intermediate state, or duplicating the de-dup hash
  logic.
* **Monkey-patching required.** The wrapper has to inject into
  `dynamic_products` module-level state, replace function references,
  or modify globals to make it work in a `ProposalManager` shape. If
  this happens, the abstraction is a leaky shim, not a clean factor.
* **Identity-shaped impedance.** `dynamic_products.py` requires
  `ResolvedIdentity` shaped exactly the way salesagent's MCP wrapper
  builds it; the SDK's projection from `BuyerAgent` + `Account` to
  the equivalent loses information the assembly logic depends on.

If any falsifier fires: #502's claim that proposal-side assembly is
a clean wrap-of-`_impl` shape is wrong. Adopters with non-trivial
proposal logic would have to choose between (a) restructuring their
assembly to fit the SDK shape, or (b) sticking with their existing
runtime. Either is a real finding that revises #502.

### Q1.5 — Does the recipe model allow proposal-time *assembly*?

Prior: #502's "framework session cache against `proposal_id`" model
accommodates dynamic products. Salesagent generates signal-driven
variant `Product` rows at brief time; the SDK's session-cache
abstraction can carry these.

Falsified if any of:

* **Recipe schema requires `proposal_id` lookup.** Signal-driven
  variants generated at brief time have no committed `proposal_id`
  yet; if the recipe schema requires one to validate or hydrate,
  the model is too late-bound.
* **Variant Products require new schema rows.** Salesagent's
  dynamic products land as new `Product` rows with TTL
  (`expires_at`); the SDK's session-cache model assumes recipes
  are looked up against pre-existing Products, not assembled
  alongside them. If we have to forge `Product` rows the framework
  doesn't know about to make this work, the abstraction is wrong
  — recipes must support proposal-time *assembly*, not just lookup.
* **Hash-dedup state crosses sessions.** `dynamic_products.py`
  hashes inputs to dedup variants; if the hash state can't fit
  the framework's session-scoped cache (because dedup is global
  cross-session), the session-scoped model is wrong.

If any falsifier fires: #502 needs a revision adding proposal-time
recipe assembly as a first-class concern. The session-cache model
becomes one shape among multiple.

### Q2 — Does the recipe carry enough?

Prior: GAM's `implementation_config` (the most-evolved recipe shape
in salesagent) fits a typed Pydantic recipe without escape hatches.

Falsified if any of:

* **`extra: dict[str, Any]` field on the recipe.** Any typed escape
  hatch — including `vendor_specific: dict`, `__pydantic_extra__`
  carrying GAM data, or `Annotated[Any, Field(extra=True)]` — is
  a tell that the typed recipe doesn't actually carry GAM's full
  shape.
* **`# type: ignore` to make recipe construction work.** If we
  have to bypass mypy to build the recipe from salesagent's
  `Product.implementation_config` JSON, the typed shape isn't
  capturing what's there.
* **Lossy projection.** Round-trip from
  `Product.implementation_config: JSONType` (salesagent) →
  `GAMRecipe` (typed) → `dict` (passed to `_create_media_buy_impl`)
  loses any field. A literal dict comparison after round-trip
  must be equal.

If any falsifier fires: #502's typed-recipe model is wrong, or
incomplete, or needs an escape-hatch design (`unstructured: dict`
field with documented semantics, like Kubernetes annotations).
Worth surfacing in a Protocol RFC.

### Q3 — What hydration model does `create_media_buy` need?

Prior: framework hydrates the recipe at `create_media_buy` time
from one of three sources (session cache, persisted DB row, fresh
lookup); the experiment forces a choice.

Falsified if:

* **None of the three work.** Hydration requires re-running the
  proposal-side assembly logic at `create_media_buy` time
  (because assembly depends on signal-time-of-day, signal agent
  state at brief moment, or other non-idempotent inputs).
* **Framework-owned hydration is the wrong primitive.** The right
  answer is "framework owns no hydration; adopter handles it
  inside `_create_media_buy_impl`" — meaning the SDK's framework
  abstraction is incorrectly drawn.

If any falsifier fires: #502's framework-managed-recipe-state
model is wrong. The recipe is adopter-owned data the SDK doesn't
need to mediate; the SDK's job is just to type the contract.

### Q4 — What is the right shape for the HITL resumption marker?

Prior: the experiment can answer "does the SDK seam accommodate
salesagent's setattr-sentinel pattern" with the SDK as it ships
today.

**Step 0 partially answered this:** the setattr pattern works as-is
(`compose_method` passes `req` through unchanged; setattr on a
Pydantic model with `extra='forbid'` survives Python-level
dispatch). So the prior holds for this experiment.

The deeper question — "what is the right Protocol seam for
resumption markers across multiple adopters?" — is **N=1 from
this experiment**. Falsifiers for the broader claim:

* **Salesagent's pattern doesn't map cleanly to a paused-coroutine
  shape** another adopter might use. If a future adopter with
  TaskRegistry-style resumption can't reuse the experiment's
  marker shape, the typed seam needs to be different.
* **The setattr survives only because no transport boundary
  intervenes.** If the experiment's SDK runtime ever needs to
  re-validate, re-project, or serialize the request between gate
  and inner, the sentinel dies. (This isn't true today — verified
  in Step 0.5 — but it's a fragile invariant.)

If any falsifier fires: the Protocol RFC should propose a typed
`ctx.resumption_token: ResumptionToken | None` that's robust to
re-projection. **The experiment can't choose between shapes; it
just shows the untyped pattern works for one adopter.**

### Q5 — Does F12 webhook auto-emit hold up under real load?

Prior, original: `WebhookSender` configured on `serve(...)` fires
sync-completion webhooks automatically, signed correctly, retried
on transient failure, logged-and-swallowed on permanent failure —
without adapter code participating. §3.14's claim that adopters
delete their webhook plumbing wholesale.

**Step 0.6 already partially falsified this.** Salesagent's
`X-Webhook-Signature` scheme and SDK's `X-AdCP-Signature` scheme
are incompatible. §3.14 needs a correction. So the prior is
already known wrong — the question now is which of three cutover
paths the experiment recommends:

(a) Buyers migrate to SDK signing.
(b) SDK ships a salesagent-compatible signing mode alongside
    `from_adcp_legacy_hmac`.
(c) Side-car preserves salesagent's `webhook_authenticator.py`
    rather than using F12 auto-emit.

Falsifiers for the SDK→SDK signing path (the only one the
experiment validates):

* **`WebhookSender` → `WebhookReceiver` round-trip fails** with
  matching secrets (extremely unlikely — well-tested in
  conformance suite, but worth running once on day 1).
* **Auto-emit doesn't fire** after a successful mutating tool
  call (means F12 framework wiring is broken or our `serve(...)`
  config is wrong).
* **Retry / failure-swallow doesn't behave per spec** — would
  require buyer-side observation of retried deliveries.

If any falsifier fires: F12 isn't ready as the default path even
for SDK→SDK signing.

## Risks (revised)

* **Wrap target drift.** Mitigated by Step 0 `_impl` identification
  and the discipline that wraps wrap `_impl` not adapters. If any
  needed `_impl` doesn't exist in transport-agnostic form, that's a
  salesagent-side refactor before the side-car experiment runs.
* **Webhook signing schemes don't match — confirmed in Step 0.**
  Salesagent's `X-Webhook-Signature` scheme and SDK's
  `X-AdCP-Signature` scheme are incompatible. The experiment
  validates SDK→SDK signing with `WebhookReceiver` as the test
  buyer; it does NOT validate that buyers subscribed to salesagent
  today will accept SDK signatures. **§3.14 of the migration guide
  needs correction**: webhook plumbing deletion is contingent on
  buyer migration, not unconditional. The cutover-path decision
  (buyers migrate / SDK adds salesagent-compatible mode / side-car
  passes through legacy signer) belongs in the findings doc, not
  this experiment.
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
  `update_media_buy`, `sync_creatives` (the wire surface; salesagent
  HITL config keys it as the GAM-internal `add_creative_assets`,
  mapped via a small table on the before-hook) — all in scope.
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

**Step 0 — Prereqs (~1-2 days, mix of investigation already done +
remaining concrete work).**

Investigations 0.2, 0.3, 0.4 are complete; results in the Step 0
section above. Remaining items are concrete prereqs.

0.1. Pin `adcp-client-python@<sha>`, both storyboard SHAs, GAM
     Network ID; document in experiment README.
0.2. ✅ `_impl` seams identified — all four exist transport-agnostic.
     Note: HITL operation-name mapping needed (GAM-internal
     `add_creative_assets` → AdCP-wire `sync_creatives`).
0.3. ✅ Structural-guard story scoped + verified — ~5-7 hygiene
     guards apply; `check_parameter_alignment.py` checks named
     MCP/A2A pairs from a hardcoded list, not all `_impl` callers,
     so the side-car is invisible to it. Zero allowlist additions
     needed.
0.4. ✅ Cross-tenant scheduler audit — only two genuinely
     cross-tenant. **Remaining (local fork patch only):** hardcoded
     skip or env-var skip in `media_buy_status_scheduler.py` and
     `delivery_webhook_scheduler.py` for the experiment tenant ID.
0.5. ✅ `_already_approved` sentinel works as-is. Verified
     `compose_method` passes `req` through unchanged; dispatcher
     only `model_dump`s on response side; setattr lands in
     `__dict__` and persists through Python-level dispatch. No
     typed marker prototype needed for this experiment.
0.6. ⚠️ Webhook signing parity does NOT hold between salesagent's
     `webhook_authenticator.py` (X-Webhook-Signature scheme) and
     SDK's `from_adcp_legacy_hmac` (X-AdCP-Signature scheme). For
     the experiment, use SDK→SDK signing only (test buyer is
     `adcp.WebhookReceiver` with the same secret). Production
     cutover requires buyer migration as separate work.
0.7. ✅ Falsification signals pre-registered for each of the five
     (six, with Q1.5) learning questions. See "Pre-registered
     falsification signals" section above.

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
2.2. `GAMDecisioningPlatform` wrapping `_create_media_buy_impl`,
     `_update_media_buy_impl`, `_get_media_buy_delivery_impl`,
     `_sync_creatives_impl`. `GAMProposalManager` wrapping
     `_get_products_impl` (now informed by Phase 1's findings).
2.3. HITL gate via `compose_method`. `before` hook on the three
     mutating ops (`create_media_buy`, `update_media_buy`,
     `sync_creatives`), consulting
     `AdapterConfig.gam_manual_approval_required` (tenant-scoped) +
     a small operation-name mapping table (GAM-internal
     `add_creative_assets` → AdCP-wire `sync_creatives`). Rewrite
     `execute_approved_media_buy` body to reconstruct request,
     attach resumption marker, call back into side-car runtime.
     Admin UI route untouched.
2.4. `WebhookSender` configured on `serve(...)` using
     `from_adcp_legacy_hmac` with a controlled experiment-tenant
     secret. Test buyer is `adcp.WebhookReceiver`
     (`src/adcp/webhook_receiver.py`) with the same secret —
     SDK→SDK signing parity, not parity against salesagent's
     existing scheme (which is incompatible per Step 0
     investigation). **No** per-tenant disable needed for
     `protocol_webhook_service.py` (Step 0.4 — fires per-event
     in the active request, not cross-tenant). The two cross-tenant
     schedulers (`media_buy_status_scheduler`,
     `delivery_webhook_scheduler`) are skipped via the local-fork
     patch (hardcoded tenant ID or env var) per Step 0.4.
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
3a. **Correct [#489](https://github.com/adcontextprotocol/adcp-client-python/pull/489)
   §3.14** — webhook plumbing deletion is contingent on buyer
   signing-scheme migration, not unconditional. Salesagent's existing
   `X-Webhook-Signature` scheme is incompatible with SDK's
   `X-AdCP-Signature` scheme; production cutover requires either
   (a) buyers migrate to SDK signing, (b) SDK ships a salesagent-
   compatible signing strategy, or (c) the side-car runtime
   passes through salesagent's `webhook_authenticator.py` rather
   than using F12 auto-emit. Findings doc names which path is
   recommended based on subscribed-buyer count.
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
