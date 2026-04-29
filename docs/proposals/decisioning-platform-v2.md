# RFC: Python port of DecisioningPlatform (v6.0) — v2

## Status

**Proposed** — open for review by the AdCP Python team and the salesagent team.

This RFC supersedes [`decisioning-platform-python-port.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-python-port.md) (v1). v1 was written before the round-2 hybrid-seller pivot and the round-3 `AdcpError` raise-path refactor; large parts of its surface (`AsyncOutcome[T]` discriminated union, `*Task` dual methods) are no longer the canonical TypeScript shape and shouldn't be ported. This v2 reflects what the TypeScript SDK actually ships on `bokelley/decisioning-platform-v1-scaffold` (PR #1005) after rounds 1-7 of expert review, plus salesagent-side feedback on Python ergonomics and operational reality.

## Background

The TypeScript scaffold at [`src/lib/server/decisioning/`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/) is the canonical surface. The Python port targets two adopter groups:

1. **The salesagent server** ([`adcontextprotocol/salesagent`](https://github.com/adcontextprotocol/salesagent)) — Flask + SQLAlchemy + Pydantic 2. Today it's a thin tool-decorator over per-adapter classes (GAM, Kevel, scope3 wrappers); idempotency, signing, sandbox, and status-change are hand-rolled per tool. The unified hybrid shape collapses 14 method names into 7, and the framework absorbs the cross-cutting concerns.
2. **Single-tenant Python adopters** (Innovid training-agent class, signals providers, retail-media networks). These run one platform impl, often with `'singleton'` account resolution; the framework's tenant-scoped invariants still apply.

The v6.0 framework owns wire mapping, account resolution, async tasks, idempotency, RFC 9421 signing, schema validation, sandbox routing, status-change projection, and lifecycle observability. Adopters describe their platform once via per-specialism `Protocol` classes; the framework does the rest.

### Existing `adcp` Python package

The `adcp` package ([`adcontextprotocol/adcp-client-python`](https://github.com/adcontextprotocol/adcp-client-python), PyPI: `adcp`, currently v4.0.0 with `ADCP_VERSION=3.0.0`) **already exposes a mature server surface**:

- `adcp.server` — `ADCPHandler` class-pattern, `adcp_server(...)` decorator builder, `serve()` entry point, MCP + A2A transport mounts (`mcp_tools.py`, `a2a_server.py`), auth middleware, governance/content-standards/brand/sponsored-intelligence handlers
- `adcp.signing` — RFC 9421 signer/verifier (`signer.py`, `verifier.py`, `jws.py`, `digest.py`), JWKS resolver (`jwks.py`), IP-pinned transport (`ip_pinned_transport.py`), JCS canonicalization (`canonical.py`, backed by `rfc8785`), replay protection (`replay.py`), revocation (`revocation.py`, `revocation_fetcher.py`), webhook signer/verifier
- `adcp.types` — generated wire types from `schemas/cache/3.0.0/`; ergonomic + alias layer; Pydantic v2 models
- `adcp._idempotency` + `adcp.server.idempotency/` — idempotency middleware
- `adcp.testing`, `adcp.validation`, `adcp.webhook_sender`, `adcp.webhook_receiver`, `adcp.protocols.{mcp,a2a}`

**v6.0 DecisioningPlatform is a successor pattern** to `ADCPHandler` — Protocol-driven instead of class-inheritance, hybrid `T | TaskHandoff[T]` returns instead of method-name-explosion, multi-tenant primitives. It lands as a new module **inside the existing `adcp` package**, not as a separate `adcp-server` package. The framework reuses the existing primitives in `adcp.signing` / `adcp._idempotency` / `adcp.server` / `adcp.types` rather than building parallel implementations.

### Module path

The framework lives at `adcp.decisioning.*`:

| Submodule | Contents |
|---|---|
| `adcp.decisioning` | Re-exports the public surface: `DecisioningPlatform`, `TaskHandoff`, `RequestContext`, `Account`, `serve`, `create_adcp_server_from_platform` |
| `adcp.decisioning.specialisms` | 12 `Protocol` classes — `SalesPlatform`, `AudiencePlatform`, `SignalsPlatform`, `CreativeAdServerPlatform`, `CreativeTemplatePlatform`, `CreativeGenerativePlatform`, `CampaignGovernancePlatform`, `PropertyListsPlatform`, `CollectionListsPlatform`, `ContentStandardsPlatform`, `BrandRightsPlatform`, plus `MeasurementVerificationPlatform` (preview) |
| `adcp.decisioning.dispatch` | Adapter seam from `Protocol`-impl methods to existing `adcp.server.serve` handler shape; `asyncio.to_thread` sync dispatch; `TaskHandoff` detection; `validate_platform()` |
| `adcp.decisioning.tenant_registry` | Multi-tenant primitive composing `adcp.server.serve(factory=...)` |
| `adcp.decisioning.task_registry` | `TaskRegistry` Protocol + `InMemoryTaskRegistry` + `SqlAlchemyTaskRegistry` (HITL task lifecycle — new, distinct from idempotency middleware) |
| `adcp.decisioning.status_changes` | `InMemoryStatusChangeBus` + `DbBackedStatusChangeBus` |
| `adcp.decisioning.delivery` | `McpWebhookDelivery` + `A2aTaskDelivery` (composed from existing `adcp.webhook_sender` + `adcp.server.a2a_server`) |
| `adcp.decisioning.testing`, `adcp.decisioning.dev` | `make_test_context`, `JwksFixture` |

### Reuse vs. build

The framework's foundation primitives are mostly already in `adcp`. Implementation work splits into:

- **Audit gaps** in existing primitives against this RFC's locked decisions — pin-and-bind SNI / redirects / port allowlist, RFC 9421 covered-fields lock + tenant-scoped JWKS, idempotency keying tuple `(key, account, tool, body_hash)`, Pydantic `extra='forbid'` default, webhook envelope `operation_id`, multi-tenant `factory=` shape on `serve()`. Each gap is a small fix PR against the existing module.
- **Add new layers** — `adcp.decisioning.*` modules listed above. The largest new piece is the HITL `task_registry` (idempotency middleware doesn't cover task lifecycle) and the multi-tenant `tenant_registry`.

**Reference reading:**

- [`docs/proposals/decisioning-platform-v1.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-v1.md) — original TS design proposal
- [`docs/proposals/decisioning-platform-v2-hitl-split.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-v2-hitl-split.md) — the HITL split that motivated unified hybrid
- [`skills/build-decisioning-platform/SKILL.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/skills/build-decisioning-platform/SKILL.md) — adopter-facing canonical surface (the Python SKILL must mirror this)
- [`.changeset/decisioning-platform-v1-scaffold.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/.changeset/decisioning-platform-v1-scaffold.md) — round-by-round design log
- [`src/lib/server/decisioning/specialisms/sales.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/specialisms/sales.ts) — the unified hybrid `SalesPlatform` interface
- [`src/lib/server/decisioning/async-outcome.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/async-outcome.ts) — `AdcpError` + `TaskHandoff` brand mechanism
- [`src/lib/server/decisioning/tenant-registry.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/tenant-registry.ts) — multi-tenant primitive
- [`examples/decisioning-platform-mock-seller.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/examples/decisioning-platform-mock-seller.ts) — gold-standard hybrid sample
- [`examples/decisioning-platform-broadcast-tv.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/examples/decisioning-platform-broadcast-tv.ts) — HITL-heavy hybrid sample

## What changed since v1

| v1 design | v2 design | Why |
|---|---|---|
| `AsyncOutcome[T]` discriminated union (`Sync` / `Submitted` / `Rejected`) | Plain `T \| TaskHandoff[T]` return + `raise AdcpError` | Round-2 hybrid feedback (salesagent): dual outcome union forced upfront sync-vs-HITL choice; hybrid sellers branch per call. Round-3: `AdcpError` raise-path matches Flask/FastAPI/tRPC idioms; LLM-generated adopter code consistently picked it on first try. |
| `*Task` dual methods (`createMediaBuy` + `createMediaBuyTask`) | One method per tool returning `Success \| TaskHandoff[Success]` | Salesagent flagged: a real publisher commonly sells both kinds of inventory through the same tool. Dual methods forced "always declare HITL, resolve immediately on fast path" anti-pattern that taxes the 99% programmatic case with `tasks_get` polling. |
| `ctx.task: TaskHandle \| None` field on `RequestContext` | `ctx.handoff_to_task(fn)` constructor returning `TaskHandoff[T]` marker | Marker is a plain `__slots__`-only class; framework dispatches via type-identity (`type(obj) is TaskHandoff`). No `WeakValueDictionary`, no module-private storage — the JS Symbol-keyed brand exists to defend against untrusted code in the same realm, which is not the Python threat model. |
| `AccountNotFoundError` thrown class | Same — keep, narrow-use only from `accounts.resolve()` | No change. |
| 30-value `ErrorCode` union | 45-value union matching `schemas/cache/3.0.0/enums/error-code.json` | Spec catch-up (round-3). |
| No `TenantRegistry` | Multi-tenant primitive with subdomain + path-prefix routing, JWKS validator, `'pending'` health state | Training-agent migration + adoption-validation rounds 4-5. |
| No `publish_status_change` | Server-scoped status-change bus exposed as `server.status_change.publish(event)`; `TenantRegistry.publish_status_change(tenant_id, event)` for cross-tenant code | Round-7 Emma sims surfaced cross-test-contamination via a module-level singleton. Killing the global removes the contamination class entirely; non-handler code (cron, webhook receivers) holds the server reference like any other dependency. |
| `partial_result` on `Submitted` | Removed — off-spec drift | Salesagent feedback round-2: partial result was an "ergonomic feature" that didn't validate against spec receivers. |
| `asyncpg`-only task registry | `TaskRegistry` Protocol + two impls in v6.0 (in-memory, SQLAlchemy); asyncpg deferred to v6.1 | Salesagent already runs SQLAlchemy + Alembic; forcing asyncpg means dual connection pools and dual migration tooling. Adopter picks the impl matching their stack. Asyncpg ships when a greenfield adopter asks for it — Protocol shape lets it slot in additively. |
| SSRF rebinding deferred to v6.1 | Pin-and-bind shipped in v6.0 alongside the validator | Webhook delivery to buyer-supplied URLs is exploitable on day one without pinning; "fix coming in v6.1" ships a known hole. |
| Sync method dispatch on the event loop | Sync methods run via `asyncio.to_thread` | A blocking sync handler on the event loop serializes every concurrent request. `to_thread` is the only safe dispatch for sync-method support. |
| `extra='ignore'` in production (silent-drop) | `extra='forbid'` always; `ADCP_FORWARD_COMPAT=permissive` is opt-in | Silent-drop is the worst failure mode for an LLM-generated buyer payload. Forward-compat is an explicit operator action. |
| Idempotency keyed `(idempotency_key, account_id)` | Keyed `(idempotency_key, account_id, tool_name, body_hash)` with same-key-different-body → `INVALID_REQUEST` | Spec-conforming idempotency must reject body divergence; cross-language wire compat with TS requires the full tuple. |
| Webhook `operation_id` field absent | `WebhookPayload` includes `operation_id` (buyer-supplied, echoed) | Spec compliance against `mcp-webhook-payload.json`. |
| MCP-only webhook delivery | `WebhookPayload` for MCP + `TaskStatusUpdateEvent` for A2A — both in v6.0 | Without A2A delivery, hybrid handoff is silently MCP-only — a wire-shape divergence from TS. |
| `TenantHealth` single status enum | Orthogonal `verification` + `operator_gate` axes | Real publisher onboarding has two independent gates (cryptographic + operator). |
| In-memory `StatusChangeBus` only | Adds `DbBackedStatusChangeBus` for audit-relevant deployments; in-memory labeled dev-only | Compliance reporting needs durable status transitions. |
| `update_media_buy` HITL workaround returned `status='pending_approval'` | Workaround returns success with `status` field omitted | `pending_approval` is not in `MediaBuyStatus` enum — the wire would reject it. |
| `task_id UUID PRIMARY KEY` + status CHECK | `task_id TEXT` + composite PK `(account_id, task_id)` + no CHECK | Multi-tenant prefix scheme emits strings; CHECK drifts when AdCP adds status values. |
| Pin-and-bind snippet rewrote `url.host` (broke TLS SNI) + followed redirects | SNI preserved via `extensions['sni_hostname']`; `follow_redirects=False`; all DNS answers checked; port allowlist | The earlier snippet was both functionally broken (TLS verify against IP) and SSRF-bypassable (redirect rebinding). |
| Cross-tenant `publish_status_change` trusted caller's `event.account_id` | Validates `account_id` belongs to `tenant_id` before forwarding | Prevented cross-tenant leak through MCP Resources subscribers. |
| Singleton-mode `Account.id='training-agent'` for every caller | Singleton synthesizes per-principal `Account.id` | Closed buyer-to-buyer idempotency-cache leak. |
| Flat `(key_id) -> jwk` JWKS resolver | Tenant-scoped `(tenant_id, key_id) -> jwk`; required signature components locked | Closed JWKS auth-confusion across tenants; spec-compliant covered-fields list. |
| `WebhookTransport` override accepted any `httpx.AsyncClient` | Override requires `WebhookTransport` Protocol with `enforces_ssrf_at_connect=True` | Operator override silently bypassing SSRF was a configuration footgun. |
| Separate `adcp-server` package on PyPI | Lands inside existing `adcp` package at `adcp.decisioning.*`; package version v4 → v5 | The `adcp` package already ships RFC 9421 signing, JCS canonicalization, IP-pinned transport, JWKS, idempotency middleware, generated wire types, MCP + A2A transports. Splitting them across two packages would mean duplicating half the foundation; in-package landing reuses the primitives instead. |

## Scope

**In-scope:**

- Framework primitives: server factory, dispatch seam, idempotency, signing, validation, sandbox boundary
- 12 per-specialism `Protocol` classes
- Account resolution (3-mode), tenant registry, observability hooks
- Wire-shape parity with TypeScript SDK (must round-trip the same `mcp-webhook-payload.json`, `tasks-get-response.json`, etc.)
- Adopter-experience parity: write one platform class, framework owns the rest
- Migration paths from existing salesagent shape
- `TaskRegistry` Protocol with in-memory and SQLAlchemy implementations (asyncpg deferred to v6.1)

**Out-of-scope:**

- Per-adopter migration of GAM / Kevel / scope3 / Innovid adapters (each adopter writes its own `SalesPlatform` impl; the salesagent's existing per-adapter classes become the bodies of those impls)
- MCP Resources subscription wire projection (parked behind AdCP 3.1)
- Compile-time enforcement (Python doesn't have `RequiredPlatformsFor<S>`)
- Symbol-keyed brand types for `TaskHandoff` (Python uses type-identity instead — see § *Hybrid handoff*)

## Goals / Non-goals

**Goals:**

1. **Wire-shape parity** with the TypeScript SDK at the AdCP wire version (`schemas/cache/3.0.0/`). A buyer's MCP/A2A request that succeeds against `@adcp/client` must succeed against `adcp` with the same response payload, modulo serialization order. Verified by a wire-parity test suite (see § *Validation matrix*).
2. **Adopter-experience parity.** The Python SKILL has the same canonical example as the TypeScript SKILL, same fields, same error codes, same migration sketch.
3. **Migration path** from the salesagent's current Flask + per-adapter shape that doesn't require a rewrite — `@tool` decorators stay, per-adapter classes become `SalesPlatform` impls, framework absorbs idempotency / signing / sandbox / status-change.
4. **Async-or-sync method support.** Adopter methods can be either; the framework awaits async handlers natively and runs sync handlers via `asyncio.to_thread`. Flask salesagent is sync today; FastAPI adopters are async; both must work without forking.
5. **Operationally honest.** Default impls compose with adopters' existing stacks (SQLAlchemy + Alembic for salesagent) rather than forcing a parallel persistence story.

**Non-goals:**

1. Compile-time gates. The TS-side `RequiredPlatformsFor<'sales-broadcast-tv'> = SalesPlatformHitl` design-time signal does not have a Python equivalent; runtime `validate_platform()` fires the same diagnostic at server boot, but the gap is real and adopters should expect it.
2. Symbol-keyed brand types for `TaskHandoff`. The TS-side `Symbol.for('@adcp/decisioning/task-handoff')` brand is replaced by a plain class with `__slots__`; the framework dispatches via type-identity. Python's threat model doesn't justify the JS-side ceremony.
3. New protocol shapes. Nothing in this RFC adds wire surface that doesn't already exist in AdCP 3.0 GA. If the spec evolves, Python and TypeScript track it together.

## Design

### Specialism Protocol classes

Twelve specialisms map to twelve `Protocol` classes:

| Specialism | Protocol class | Notes |
|---|---|---|
| `sales-non-guaranteed`, `sales-guaranteed`, `sales-broadcast-tv`, `sales-streaming-tv`, `sales-social`, `sales-exchange`, `sales-proposal-mode`, `sales-catalog-driven`, `sales-retail-media` | `SalesPlatform[TMeta]` | One unified hybrid shape covers all 9 sales specialisms |
| `audience-sync` | `AudiencePlatform[TMeta]` | |
| `signal-marketplace`, `signal-owned` | `SignalsPlatform[TMeta]` | |
| `creative-ad-server` | `CreativeAdServerPlatform[TMeta]` | HITL S&P review hybrid |
| `creative-template` | `CreativeTemplatePlatform[TMeta]` | |
| `creative-generative` | `CreativeGenerativePlatform[TMeta]` | |
| `governance-spend-authority`, `governance-delivery-monitor` | `CampaignGovernancePlatform[TMeta]` | |
| `property-lists` | `PropertyListsPlatform[TMeta]` | |
| `collection-lists` | `CollectionListsPlatform[TMeta]` | |
| `content-standards` | `ContentStandardsPlatform[TMeta]` | |
| `brand-rights` | `BrandRightsPlatform[TMeta]` | |
| `signed-requests` | (cross-cutting; no Protocol) | Wired on `serve(authenticate=...)` |
| `measurement-verification` | (preview; no Protocol yet) | |

`TMeta` is the per-platform metadata generic — `Account[TMeta]` carries `metadata: TMeta` so adopter-defined fields (`affiliate_id`, `network_id`, etc.) typecheck inside method bodies without casting. Defaults to `dict[str, Any]` for adopters who don't care.

**Reference: full `SalesPlatform` shape** — mirrors [`specialisms/sales.ts:127-220`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/specialisms/sales.ts#L127-L220):

```python
from __future__ import annotations
from typing import Protocol, Generic, TypeVar, Awaitable, Union
from collections.abc import Awaitable as _Awaitable

# Wire types — auto-generated from schemas/cache/3.0.0/*.json via
# datamodel-code-generator. Adopters import from adcp.types.
from adcp.types import (
    GetProductsRequest, GetProductsResponse,
    CreateMediaBuyRequest, CreateMediaBuySuccess,
    UpdateMediaBuyRequest, UpdateMediaBuySuccess,
    GetMediaBuyDeliveryRequest, GetMediaBuyDeliveryResponse,
    CreativeAsset, SyncCreativesRow,
)
from adcp.decisioning.async_outcome import TaskHandoff
from adcp.decisioning.context import RequestContext

TMeta = TypeVar("TMeta", default=dict)  # PEP 696 default via typing_extensions on 3.10-3.12

# Named result aliases — coding agents (Cursor, Claude Code) handle
# one named alias far better than a nested four-way union. Adopters
# read SalesResult[CreateMediaBuySuccess] and immediately understand
# "this is sync OR async OR a handoff," instead of decoding
# Awaitable[T | TaskHandoff[T]] | T | TaskHandoff[T] inline.
T = TypeVar("T")
MaybeAsync = Awaitable[T] | T
SalesResult = MaybeAsync[T] | TaskHandoff[T] | MaybeAsync[TaskHandoff[T]]

class SalesPlatform(Protocol, Generic[TMeta]):
    """Unified hybrid SalesPlatform — one method per tool. Methods may be
    sync (return T directly) or async (return Awaitable[T]); framework
    detects via inspect.iscoroutinefunction at dispatch time and runs
    sync methods on a thread pool via asyncio.to_thread.

    Hybrid sellers (programmatic remnant + guaranteed inventory in one
    tenant) branch per call: return Success directly for the sync fast
    path, return ctx.handoff_to_task(fn) for the HITL slow path.

    Throw AdcpError for buyer-fixable rejection; framework projects to
    wire envelope (code, recovery, field, suggestion, retry_after,
    details).
    """

    def get_products(
        self,
        req: GetProductsRequest,
        ctx: RequestContext[TMeta],
    ) -> Awaitable[GetProductsResponse] | GetProductsResponse:
        """Sync catalog read — no HITL even on broadcast/proposal-mode.
        Brief-based proposal generation rides on a separate verb
        (adcp#3407 request_proposal); proposal-mode adopters surface
        the eventual products via publish_status_change(resource_type=
        'proposal').
        """
        ...

    def create_media_buy(
        self,
        req: CreateMediaBuyRequest,
        ctx: RequestContext[TMeta],
    ) -> SalesResult[CreateMediaBuySuccess]:
        """Unified hybrid. Return CreateMediaBuySuccess directly for sync
        fast path; return ctx.handoff_to_task(fn) for HITL slow path.

        Pre-flight runs sync regardless of path so bad budgets reject
        before allocating a task id.

        Buyer pattern-matches on the response: media_buy_id field →
        sync; task_id + status='submitted' → poll tasks_get or webhook.
        """
        ...

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: UpdateMediaBuyRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[UpdateMediaBuySuccess]:
        """Mutate an in-flight media buy. v6.0 returns sync only;
        adcp#3392 + v6.1 expand this signature to SalesResult[...] so
        re-approval flows can hand off cleanly."""
        ...

    def sync_creatives(
        self,
        creatives: list[CreativeAsset],
        ctx: RequestContext[TMeta],
    ) -> SalesResult[list[SyncCreativesRow]]:
        """Unified hybrid for creative review. Mixed approved/pending
        rows in a single sync response, OR hand off the whole batch to
        background S&P review."""
        ...

    def get_media_buy_delivery(
        self,
        filter: GetMediaBuyDeliveryRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetMediaBuyDeliveryResponse]:
        """Sync delivery read — pacing, spend, impressions per package."""
        ...

    # Optional methods — present-or-absent; framework detects via
    # hasattr at validate_platform() time. Each method's docstring
    # declares which specialism gates it; coding agents reading the
    # Protocol can reason about coverage from the stub alone.

    def get_media_buys(
        self,
        filter: GetMediaBuysRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[GetMediaBuysResponse]:
        """List media buys for the resolved account.
        Required when claiming any sales specialism in v6.0 rc.1+."""
        ...

    def provide_performance_feedback(
        self,
        feedback: ProvidePerformanceFeedbackRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ProvidePerformanceFeedbackResponse]:
        """Buyer-supplied performance signal back to the seller.
        Required when claiming any sales specialism in v6.0 rc.1+."""
        ...

    def list_creative_formats(
        self,
        filter: ListCreativeFormatsRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativeFormatsResponse]:
        """Catalog of accepted creative formats.
        Required when claiming any sales specialism in v6.0 rc.1+."""
        ...

    def list_creatives(
        self,
        filter: ListCreativesRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[ListCreativesResponse]:
        """List the seller's view of buyer-uploaded creatives.
        Required when claiming any sales specialism in v6.0 rc.1+."""
        ...

    def sync_catalogs(
        self,
        catalogs: list[CatalogEntry],
        ctx: RequestContext[TMeta],
    ) -> SalesResult[list[SyncCatalogsRow]]:
        """Required when claiming 'sales-catalog-driven' or
        'sales-retail-media'. v6.0 returns sync rows; v6.1 + adcp#3392
        adds hybrid-handoff arm for review-gated catalog ingestion."""
        ...

    def log_event(
        self,
        event: LogEventRequest,
        ctx: RequestContext[TMeta],
    ) -> MaybeAsync[LogEventResponse]:
        """Required when claiming 'sales-retail-media'. Buyer-side
        delivery / conversion event logging."""
        ...

    def sync_event_sources(
        self,
        sources: list[EventSource],
        ctx: RequestContext[TMeta],
    ) -> SalesResult[list[SyncEventSourcesRow]]:
        """Required when claiming 'sales-retail-media'. Onboarding
        of buyer-side event sources (pixels, server-to-server)."""
        ...
```

**`AudiencePlatform` shape** — same hybrid pattern; `sync_audiences` returns sync rows, lifecycle flows through `publish_status_change`:

```python
class AudiencePlatform(Protocol, Generic[TMeta]):
    def sync_audiences(
        self,
        audiences: list[Audience],
        ctx: RequestContext[TMeta],
    ) -> Awaitable[list[SyncAudiencesRow]] | list[SyncAudiencesRow]:
        """Sync acknowledgment with status changes via server.status_change.publish(...).
        Return per-audience result rows immediately ('processing' is fine);
        match-rate computation and activation pipeline run in background."""
        ...

    def get_audience_status(
        self,
        audience_id: str,
        ctx: RequestContext[TMeta],
    ) -> Awaitable[AudienceStatus] | AudienceStatus:
        ...
```

**No more `*Task` methods.** v1's dual-method shape is dropped.

### Hybrid handoff (`ctx.handoff_to_task`)

The TypeScript brand-marker uses `Symbol.for(...)` because JS sometimes runs untrusted code in the same realm as the framework. Python doesn't have that threat — adopter code is trusted, and the marker is a return type, not a request body, so the buyer can never construct one. The brand exists for type-safety and dispatch identification, not adversarial protection.

The Python implementation is a small class with a single private slot:

```python
# adcp/decisioning/async_outcome.py
from typing import Generic, TypeVar, Callable, Awaitable

TResult = TypeVar("TResult")

class TaskHandoff(Generic[TResult]):
    """Marker the framework recognizes as 'promote this call to a task.'
    Adopters obtain instances via ctx.handoff_to_task(fn); the framework
    dispatches based on type-identity (type(obj) is TaskHandoff).
    """
    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[["TaskHandoffContext"], "Awaitable[TResult] | TResult"]):
        self._fn = fn

    def __repr__(self) -> str:
        return "TaskHandoff(<sealed>)"

# Framework dispatch — type-identity check (not isinstance) so adopter
# subclasses never accidentally trigger the handoff path:
def _is_task_handoff(obj) -> bool:
    return type(obj) is TaskHandoff
```

No `WeakValueDictionary`, no module-level storage, no strong-ref bookkeeping. The function lives on the instance; the framework reads it via `handoff._fn` at dispatch time. Standard GC cleans up when the handoff goes out of scope.

`ctx.handoff_to_task(fn)`:

```python
# adcp/decisioning/context.py
class RequestContext(Generic[TMeta]):
    account: Account[TMeta]
    state: StateReader  # workflow steps, proposals, governance JWS
    resolve: Resolver  # property/collection-list + format fetchers (rc.1+)

    def handoff_to_task(
        self,
        fn: Callable[[TaskHandoffContext], Awaitable[TResult]] | Callable[[TaskHandoffContext], TResult],
    ) -> TaskHandoff[TResult]:
        """Promote this call to a background task. Buyer sees
        {status: 'submitted', task_id} on the immediate response;
        framework runs fn after returning, persists fn's terminal
        artifact to the task registry, and emits push-notification
        webhook on terminal state.

        fn receives TaskHandoffContext carrying:
          - id: framework-issued task UUID
          - update(progress): write progress payload, transition
            'submitted' → 'working'
          - heartbeat(): liveness signal (v6.1 stub)
        """
        return TaskHandoff(fn)
```

**Why `__slots__`?** Keeps the class minimal, prevents accidental field addition by adopters who subclass, and the framework's `type(obj) is TaskHandoff` check rejects any subclass anyway.

**Threat model.** The "forgery resistance" the JS brand provides — preventing untrusted code in the same realm from constructing a marker — has no Python analog. Adopter code is trusted. Buyer-supplied request bodies can never reach this type because `TaskHandoff` is a return type, never deserialized from the wire. The adversary doesn't exist; the ceremony to defend against them shouldn't either.

### Account resolution (3-mode)

Same shape as TypeScript; the modes cover the deployment patterns we've seen across adopters:

```python
from typing import Literal, TypeVar, Generic
from collections.abc import Awaitable

class AccountStore(Protocol, Generic[TMeta]):
    resolution: Literal['explicit', 'from_auth', 'singleton']

    def resolve(
        self,
        ref: AccountReference | None,
        ctx: ResolveContext | None = None,
    ) -> Awaitable[Account[TMeta]] | Account[TMeta]:
        """Resolve an Account from the wire reference + transport-level
        auth context. The framework calls this for every tool dispatch;
        adopters in 'explicit' mode use ref.account_id; 'from_auth' mode
        reads ctx.auth_info to look up the principal-bound account;
        'singleton' mode ignores ref and returns the one account."""
        ...

    def upsert(self, ...) -> ...: ...
    def list(self, ...) -> ...: ...
    def report_usage(self, ...) -> ...: ...
    def get_account_financials(self, ...) -> ...: ...
```

**Mode rename note.** v1 used `'explicit' | 'implicit' | 'derived'`. The new names are concrete: `'from_auth'` makes the auth-derived path explicit; `'singleton'` says what it does ("there's one account"). `'derived'` was opaque on first read.

**Cross-language parity.** TypeScript SDK PR [#1005](https://github.com/adcontextprotocol/adcp-client/pull/1005) renames in lockstep — both SDKs ship `'explicit' | 'from_auth' | 'singleton'` at the v6.0 / v3.0.0-protocol cut. If a TS-side review re-litigates the rename and lands different names, the Python `AccountStore.resolution` Literal must follow; this is a wire-adjacent contract, not a Python-side stylistic choice. Adopters maintaining both SDKs see identical mode names; no conversion table required.

Old → new mode mapping for adopters porting from v1:

| v1 (TypeScript or Python pre-rename) | v6.0 (both SDKs) | Notes |
|---|---|---|
| `'explicit'` | `'explicit'` | unchanged |
| `'implicit'` | `'from_auth'` | adopters using `'implicit'` were always reading auth-derived state; the new name makes that intent visible |
| `'derived'` | `'singleton'` | the only `'derived'` in production was for single-account agents (Innovid training-agent); `'singleton'` says what it does |

**Salesagent migration:**

The salesagent today reads `g.tenant` from a Flask `before_request` hook (`tenants/<tenant_id>/...` URL pattern). That stays — but the body of the `@tool` decorator becomes:

```python
# Before (salesagent today):
@tool('create_media_buy')
def create_media_buy_handler(req):
    tenant = g.tenant
    adapter = tenant.adapter  # GAMAdapter, KevelAdapter, etc.
    return adapter.create_media_buy(req)

# After (v6.0 framework):
class SalesAgentSeller(SalesPlatform):
    accounts = SalesAgentAccounts(resolution='explicit')

    def create_media_buy(self, req, ctx):
        # ctx.account is the resolved tenant — same shape as g.tenant
        # was, with metadata: TenantMeta carrying adapter + config
        adapter = ctx.account.metadata.adapter
        return adapter.create_media_buy(req, ctx)

class SalesAgentAccounts:
    resolution = 'explicit'

    def resolve(self, ref, ctx=None):
        tenant_id = ref.account_id if ref else None
        if not tenant_id:
            raise AccountNotFoundError(...)
        # Existing salesagent tenant lookup
        return tenant_to_account(load_tenant(tenant_id))
```

The `@tool` decorator goes away; `serve(create_adcp_server_from_platform(seller, ...))` registers all wire tools the platform's specialisms claim.

### Async/sync method support

Python adopters can write methods as either `def` or `async def`. The framework detects at dispatch time and runs sync methods on a thread pool to avoid blocking the event loop:

```python
import asyncio
import inspect

async def _dispatch(method, *args, **kwargs):
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    # Sync method — run on a worker thread so blocking I/O (sync DB
    # drivers, requests, etc.) doesn't serialize the event loop.
    # asyncio.to_thread already runs via contextvars.copy_context().run
    # internally (CPython asyncio/threads.py), so contextvars propagate
    # automatically — no extra wrapping needed.
    return await asyncio.to_thread(method, *args, **kwargs)
```

This matters because Flask salesagent is synchronous (sync DB drivers, sync request bodies). Forcing it to migrate to async-everywhere is a large rewrite that doesn't gate on this feature. FastAPI adopters get native async; both work in the same framework.

**`contextvars` propagation.** `asyncio.to_thread` snapshots the current context and runs the callable inside `Context.run(...)`. Request-scoped state (active span, request id, tenant id) is visible inside sync handlers automatically. Mutations to `ContextVar` objects inside a sync handler stay scoped to that thread's context copy — they don't propagate back to the loop, which matches what observability libraries expect.

**Earlier drafts wrapped with `contextvars.copy_context().run(method, *args, **kwargs)`** — that's wrong on two counts: (a) `Context.run(callable, *args)` accepts only positional args, so `**kwargs` is a `TypeError`; (b) `to_thread` already does the snapshot, so the outer `copy_context` is a no-op. Drop the manual ceremony.

**Custom thread pool for `serve(thread_pool_size=...)`.** `asyncio.to_thread` dispatches to the loop's default executor. `serve()` MUST install a custom `ThreadPoolExecutor` via `loop.set_default_executor(...)` *before* the first dispatch, otherwise the kwarg is silently ignored and the runtime uses Python's stdlib default (`min(32, os.cpu_count() + 4)` on 3.13):

```python
# adcp/decisioning/serve.py
def serve(server, *, thread_pool_size: int | None = None, **kwargs):
    loop = asyncio.new_event_loop()
    if thread_pool_size is not None:
        executor = ThreadPoolExecutor(
            max_workers=thread_pool_size,
            thread_name_prefix='adcp-dispatch',
        )
        loop.set_default_executor(executor)
    asyncio.set_event_loop(loop)
    # ... mount transport, run forever
```

**Tradeoffs:**

- **Concurrency model shifts under sync adopters.** Each sync handler costs a thread + GIL hops. For salesagent's adapter calls (mostly outbound HTTP via `requests`) the GIL releases during I/O; the practical concurrency ceiling is the thread-pool size. Adjust via `serve(thread_pool_size=...)` for high-fanout deployments — otherwise you inherit the stdlib default.
- **Static analysis of "did the adopter forget to await something" doesn't apply** when the sync method touches an async dependency. `mypy --strict` won't catch a missing `await` in a sync method calling an async-returning helper. Adopters who care opt into async-everywhere; adopters who don't accept the runtime cost.

**Status-change publishing inside `def`-methods:** `server.status_change.publish(event)` is sync (in-memory bus), so it works in both sync and async methods. `ctx.handoff_to_task(async_fn)` requires the handoff function itself to be async (the framework awaits it in a background task), but the method that returns the handoff can be sync.

### Error model (`AdcpError`)

```python
# adcp/decisioning/errors.py
from typing import Literal, TypedDict, NotRequired

# 45 spec error codes from schemas/cache/3.0.0/enums/error-code.json
ErrorCode = Literal[
    'BUDGET_TOO_LOW', 'BUDGET_INVALID', 'INVALID_REQUEST',
    'POLICY_VIOLATION', 'PRODUCT_NOT_AVAILABLE',
    # ... (full 45-value list)
]

Recovery = Literal['retry_with_changes', 'transient', 'terminal', 'correctable']

class AdcpStructuredErrorDict(TypedDict):
    code: str  # ErrorCode | str (forward-compat for vendor codes)
    message: str
    recovery: Recovery
    field: NotRequired[str]
    suggestion: NotRequired[str]
    retry_after: NotRequired[int]
    details: NotRequired[dict]

class AdcpError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        *,
        message: str = "",
        recovery: Recovery = 'terminal',
        field: str | None = None,
        suggestion: str | None = None,
        retry_after: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(message or code)
        self.code = code
        self.recovery = recovery
        self.field = field
        self.suggestion = suggestion
        self.retry_after = retry_after
        self.details = details or {}

    def __str__(self) -> str:
        # Override mirrors AdcpError.toString() in TS — surfaces code +
        # recovery in default repr() / logging output
        return f"AdcpError[{self.code} / {self.recovery}]: {self.args[0]}"

    @property
    def is_known_code(self) -> bool:
        return self.code in _KNOWN_ERROR_CODES
```

**Multi-error preflight** — same pattern as TS:

```python
def preflight(req, config) -> list[AdcpStructuredErrorDict]:
    errors = []
    if total_budget(req) < config.floor_cpm * 1000:
        errors.append({
            'code': 'BUDGET_TOO_LOW',
            'message': f'total_budget below floor ({config.floor_cpm} CPM × 1000 imp)',
            'recovery': 'correctable',
            'field': 'total_budget',
        })
    return errors

def reject_preflight(errors):
    raise AdcpError(
        'INVALID_REQUEST',
        recovery='correctable',
        message=errors[0]['message'],
        field=errors[0].get('field'),
        details={'errors': errors},
    )
```

The framework catches `AdcpError` at the dispatch seam and projects to the wire `adcp_error` envelope. Generic `Exception` falls through to `SERVICE_UNAVAILABLE`.

### Wire types — Pydantic 2 + extra policy

Wire types come from `schemas/cache/<version>/*.json` via codegen (`datamodel-code-generator`). Pydantic v2 `BaseModel` is the runtime type — runtime validation, automatic serialization, ergonomic `model.field` access.

**Extra-field policy defaults to `'forbid'`** in every environment. Forward-compat with newer spec versions is an explicit operator decision via `ADCP_FORWARD_COMPAT=permissive` (or `serve(strict_wire_validation=False)`), tied to a known spec-rev rollout — not an env-name heuristic.

```python
# adcp/decisioning/types/_config.py
import os

def _default_extra() -> Literal['ignore', 'forbid']:
    """Default: forbid unknown fields. Operators opt into ignore-extra
    explicitly during a documented spec-rev rollout."""
    if os.environ.get('ADCP_FORWARD_COMPAT', '').lower() == 'permissive':
        return 'ignore'
    return 'forbid'

BASE_CONFIG = ConfigDict(
    extra=_default_extra(),
    populate_by_name=True,
    # Note: we do NOT set str_strip_whitespace=True. TS does not strip,
    # and silent-mutating buyer-supplied strings is a wire-shape divergence
    # that costs golden-file parity. Adopters who want trimming on a
    # specific field declare it on the model.
)
```

**Why not `'ignore'` in production.** `'ignore'` silently strips unknown fields. For an LLM-generated buyer payload that hallucinates a field, "field silently dropped" is the worst possible failure mode — the buyer agent thinks the field was honored, downstream behavior diverges, and there's no error trail. Strict forbid surfaces the mismatch as a wire-level rejection the buyer can act on. The forward-compat case (rolling spec upgrade with new fields appearing on the wire) is real but rare; gating it on `ADCP_FORWARD_COMPAT=permissive` makes it a deliberate operator action rather than a default that protects against an unlikely scenario at the cost of routine debuggability.

**Discriminated unions and forward-compat.** `extra='ignore'` does NOT buy tolerance for new discriminator values (Pydantic raises `union_tag_invalid` regardless of `extra`). New response arms require either a fallback variant in the union or a `model_validator(mode='before')` that maps unknown discriminators to a generic `UnknownArm` shape. The framework's generated unions ship a generic fallback when the schema declares the union as open (per spec).

**`model_validate_json(bytes)` over `model_validate(json.loads(...))`.** The dispatch seam parses the wire body via `model_validate_json` directly so Pydantic's faster JSON path runs and error messages reference the byte offset, not a re-parsed dict. Adopters never see this boundary; they receive parsed `BaseModel` instances.

**Production strictness asymmetry vs TS.** Python in default mode is **stricter** than TS (Zod's default `.strip()` is closer to `'ignore'`). A request accepted by Python's default is accepted by TS in any mode; the asymmetry is one-directional and safe. Cross-language test suites (`tests/test_wire_parity.py`) run Python in `permissive` mode against TS-produced golden files to avoid spurious failures; production buyers see the strict behavior.

**Operator runbook (must ship in the SKILL).** Production logs will fill with `INVALID_REQUEST` rejections during AdCP spec-rev rollouts as buyers update to schemas with new fields the deployed framework version doesn't know. The runbook for an oncall operator seeing that spike:

1. **Confirm the spike is field-shape-related.** Filter logs for `code: INVALID_REQUEST` with messages mentioning extra/unknown fields. If the spike is a different code (`BUDGET_TOO_LOW`, `POLICY_VIOLATION`, etc.), this is not the right runbook.
2. **Identify the spec-rev rollout.** Check `@adcp/spec` release notes for the timeframe; cross-check with buyer-side announcements. If a new minor spec version shipped recently and adds optional fields, this is the cause.
3. **Flip the gate.** Set `ADCP_FORWARD_COMPAT=permissive` (or `serve(strict_wire_validation=False)`) on the deployment. The framework will start ignoring extras within minutes (or on next deploy if the var is read at boot — confirm the deployment reads it at boot vs per-request).
4. **Plan the framework upgrade.** Permissive mode is a temporary bridge. Schedule an upgrade to the framework version that knows the new fields; flip back to strict afterward so you're not silently dropping fields buyers care about.

This runbook lives in the SKILL alongside the configuration reference; alert rules on the deployment should fire on `INVALID_REQUEST` rate exceeding a baseline so the oncall sees the spike before buyers escalate.

**Performance note:** Pydantic v2's Rust-backed validator is fast (~1µs per typical model). The per-request validation cost is negligible compared to the network and database round-trips dispatch already pays. No micro-optimization needed.

### Status-change bus

```python
# adcp/decisioning/status_changes.py
import threading
from typing import Callable, Literal, TypedDict

ResourceType = Literal[
    'media_buy', 'creative', 'audience', 'signal', 'proposal',
    'plan', 'rights_grant', 'delivery_report',
    'property_list', 'collection_list',
    # Vendor-specific keys allowed via 'x-' prefix per JSDoc convention
] | str

class StatusChangeEvent(TypedDict):
    account_id: str
    resource_type: ResourceType
    resource_id: str
    payload: dict  # freeform JSON — wire-validation off here
    timestamp: NotRequired[str]

class StatusChangeBus:
    """In-memory pub-sub. Thread-safe — sync handlers running on the
    asyncio.to_thread worker pool can publish concurrently with subscribe/
    unsubscribe calls from the loop. Subscriber failures never break
    dispatch; the framework logs error type only (not stringified payload)
    to avoid leaking tenant data into operator log streams."""

    def __init__(self):
        self._subscribers: list[Callable[[StatusChangeEvent], None]] = []
        self._lock = threading.Lock()  # guards _subscribers mutations

    def publish(self, event: StatusChangeEvent) -> None:
        # Snapshot under lock so concurrent subscribe/unsubscribe doesn't
        # mutate the list during iteration.
        with self._lock:
            subscribers = tuple(self._subscribers)
        for sub in subscribers:
            try:
                sub(event)
            except Exception as e:
                # Log error class only; payloads may carry tenant data.
                _logger.warning(
                    "status-change subscriber raised: %s",
                    type(e).__name__,
                )

    def subscribe(
        self, fn: Callable[[StatusChangeEvent], None]
    ) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)
        def unsubscribe():
            with self._lock:
                try:
                    self._subscribers.remove(fn)
                except ValueError:
                    pass  # idempotent
        return unsubscribe
```

Each `DecisioningAdcpServer` owns a `StatusChangeBus` accessible as `server.status_change`. There is **no module-level singleton** — earlier drafts shipped one and surfaced cross-test contamination + multi-server-in-process bugs that couldn't be cleanly isolated. Killing the global removes the bug class.

**Two implementations ship in v6.0:**

| Impl | Durability | When to use |
|---|---|---|
| `InMemoryStatusChangeBus` | In-process; subscribers see events fire-and-forget. Crashes lose the event. | Dev, tests, single-process deployments where event delivery is best-effort and audit isn't compliance-relevant. **Default.** |
| `DbBackedStatusChangeBus(engine, table='adcp_status_change_events')` | Writes the event to a dedicated table inside the dispatching transaction; subscribers read via **outbox-poll** (default — works on every supported dialect: Postgres, MySQL, SQLite). Postgres deployments can opt into `LISTEN/NOTIFY` for sub-second wake-up via `DbBackedStatusChangeBus(engine, notify=True)`. | Adopters where status transitions are audit-relevant (compliance reporting, billing trails, regulated markets). **Recommended for production.** |

```python
# Wire the durable bus alongside an existing SQLA stack:
from adcp.decisioning.status_changes import DbBackedStatusChangeBus
from salesagent.db import engine

server = create_adcp_server_from_platform(
    platform=seller,
    task_registry=SqlAlchemyTaskRegistry(engine),
    status_change_bus=DbBackedStatusChangeBus(engine),
)
```

The default `InMemoryStatusChangeBus` is labeled dev-only in the SKILL — production adopters who care about audit pick `DbBackedStatusChangeBus` or implement their own `StatusChangeBus` Protocol against their queue stack (Kafka, SQS, Pub/Sub). The Protocol shape:

```python
class StatusChangeBus(Protocol):
    def publish(self, event: StatusChangeEvent) -> None: ...
    def subscribe(
        self, fn: Callable[[StatusChangeEvent], None]
    ) -> Callable[[], None]: ...
```

**Inside handlers:** call `ctx.publish_status_change(event)` — the framework wires the per-server bus through `ctx`. No imports needed.

**Outside handlers** (cron jobs, webhook receivers, queue workers): hold a `DecisioningAdcpServer` reference and call `server.status_change.publish(event)`. This is the same pattern as holding a DB session or any other long-lived dependency.

**For multi-tenant deployments** under `TenantRegistry`, the registry exposes a tenant-scoped helper that looks up the right server's bus:

```python
class TenantRegistry:
    async def publish_status_change(self, tenant_id: str, event: StatusChangeEvent) -> None:
        result = self._tenants.get(tenant_id)
        if not result or result.health.status == 'disabled':
            _logger.warning("publish_status_change for unknown/disabled tenant %s", tenant_id)
            return
        # Validate event.account_id belongs to tenant_id BEFORE forwarding —
        # a caller (cron worker, queue handler) holding the registry can
        # otherwise leak tenant-B account events into tenant-A's bus and
        # downstream MCP Resources subscribers, since each per-server bus
        # trusts what it's given.
        platform = result.config.platform
        if not await _account_belongs_to_tenant(platform, event['account_id']):
            _logger.warning(
                "publish_status_change rejected: account_id=%s does not "
                "belong to tenant=%s",
                event['account_id'], tenant_id,
            )
            return
        result.server.status_change.publish(event)
```

Same gate runs on `server.status_change.publish` — the per-server bus calls back into its `AccountStore` to confirm `event.account_id` resolves before fanning out to subscribers. Cost is one cache-friendly account lookup per publish; wins are containment of bus-level mistakes (cron iterating tenants and crossing the loop variable) and a hard floor under cross-tenant data leakage through the MCP Resources subscription projector.

`SingletonAccounts.resolve` returns the same account regardless of `ref`, so the validation collapses to "is this the singleton account_id"; multi-tenant `from_auth` and `explicit` modes do an actual `AccountStore.get(account_id)`.

Cron code that handles many tenants holds the `TenantRegistry` and calls `registry.publish_status_change(tenant_id, event)`. Cron code that's tenant-bound holds the `server` directly.

**Tenant scoping:** every event carries `account_id`; subscribers filter by tenant. The framework's MCP Resources subscription projector (rc.1+) reads from each server's bus independently — no fan-in across servers, because cross-server fan-in was the exact bug the module-level singleton caused.

### Idempotency

Framework persists response per `(idempotency_key, account_id, tool_name, body_hash)` and replays on exact-tuple duplicates. Persistence shape:

```sql
CREATE TABLE adcp_idempotency_keys (
    idempotency_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    body_hash BYTEA NOT NULL,  -- sha256(canonical_json(arguments))
    response_payload JSONB NOT NULL,
    response_status SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (idempotency_key, account_id, tool_name, body_hash)
);
CREATE INDEX adcp_idempotency_keys_expires_at ON adcp_idempotency_keys (expires_at);
```

**Wire-compatible keying tuple (locked, matches TypeScript SDK PR #1005):**

| Component | Source | Notes |
|---|---|---|
| `idempotency_key` | Buyer-supplied; matches `^[A-Za-z0-9_.:-]{16,255}$` (`mcp-webhook-payload.json` regex) | Receiver does NOT assume UUIDv4. |
| `account_id` | `Account.id` returned by `AccountStore.resolve` (singleton mode synthesizes per-principal — see § *From Innovid training-agent*) | Lowercased + stripped of leading/trailing whitespace before insertion. |
| `tool_name` | The dispatched tool (e.g., `create_media_buy`) | Replay-across-tools is rejected — the same key on a different tool is a fresh request that races the cache as a normal mutating call. |
| `body_hash` | `sha256(canonical_json(arguments))` where `canonical_json` is RFC 8785 JCS (sort keys, no whitespace, normalize numbers) | Reusing the same key with a different body returns `INVALID_REQUEST` with `recovery='terminal'` (matches TS spec-conforming behavior — "same key, different body" MUST NOT silently replay the cached response). |

**JCS canonicalization is non-trivial.** Pydantic v2's serializer doesn't emit JCS natively; the framework ships a `canonical_json()` helper built on a vetted JCS library (currently `rfc8785` on PyPI; pin a version floor) and adds a wire-parity test (`tests/test_jcs_parity.py`) that runs the same payload through Python and TypeScript and asserts byte-equal hashes. Number normalization is the failure mode worth flagging — `1.0` vs `1`, scientific notation, integer-vs-float, NaN/Infinity rejection all have to match between ports, and the test catches drift before adopters do. Budget ~3-4 days of careful implementation for this slice; it's not visible from the surface API but is load-bearing for cross-language idempotency.

**TTL:** `expires_at = created_at + retention_ttl`; first-write wall-clock, NOT bumped on hit. Cache returns the originally-stored response until expiry.

Framework reads the full tuple at the top of dispatch; if hit and not expired, returns the cached response. Body-hash mismatch on the same `(key, account_id, tool_name)` raises `AdcpError('INVALID_REQUEST', recovery='terminal', message='idempotency_key reused with different body')`. Otherwise dispatches the method, captures the response, writes the row with `expires_at = now() + retention_ttl`.

**Defaults:**

- Retention: 7 days (configurable via `serve(idempotency_retention=timedelta(days=7))`)
- Response payload cap: 4 MB — same cap as the task registry (`adcp_decisioning_tasks.result`), enforced before insert
- Cleanup: framework ships `vacuum_idempotency_keys()` that deletes `expires_at < now()`. Adopters either run it as a cron, or wire it into their existing scheduler. No automatic background sweep — the framework doesn't own the deployment's job runner.

The 4MB payload cap rejects oversized responses with `INTERNAL` rather than corrupting the registry. Adopters returning large payloads need to switch to `TaskHandoff` so the result lands in the task registry's `result` JSONB (also 4MB, but task results are typically smaller because they're terminal-state artifacts).

**Mutating tools that require `idempotency_key`** are listed in `MUTATING_TASKS` (mirrors the TS-side constant): `create_media_buy`, `update_media_buy`, `sync_accounts`, `sync_creatives`, `sync_audiences`, `sync_catalogs`, `sync_event_sources`, `sync_plans`, `sync_governance`, `provide_performance_feedback`, `acquire_rights`, `activate_signal`, `log_event`, `report_usage`, `report_plan_outcome`, plus the property-list / collection-list / content-standards CRUD operations.

The framework rejects mutating requests without an `idempotency_key` with `INVALID_REQUEST` at the dispatch seam (matches TS behavior).

### HTTP signatures (RFC 9421)

**Library choice: [`http-message-signatures`](https://pypi.org/project/http-message-signatures/)** by woodruffw, the most actively maintained pure-Python implementation as of 2026. Built on `cryptography`. Supports the signature subset AdCP requires (Ed25519 + RSA-PSS).

Wired on `serve(authenticate=...)` — same boundary as TypeScript. The platform never sees raw signatures; the verifier resolves the principal and threads it onto `ctx.account.auth_info`:

```python
from http_message_signatures import HTTPMessageSigner, HTTPMessageVerifier

# Required signature components on every inbound request — locked in v6.0.
# No component MAY be omitted by the signer; the verifier rejects on absence.
REQUIRED_COMPONENTS: tuple[str, ...] = (
    "@method",
    "@target-uri",
    "@authority",
    "content-digest",  # body integrity — without this, a valid signature does not bind to the body
    "created",         # signature timestamp — gates skew rejection
    "expires",         # explicit expiry — bounds replay window
)
MAX_CLOCK_SKEW = timedelta(seconds=60)

def signed_request_verifier(jwks_resolver, *, nonce_store=None):
    """Returns a callable that verifies incoming RFC 9421 signatures
    against the active TENANT's JWKS only, and populates ctx.auth_info
    with the resolved principal.

    jwks_resolver(tenant_id, key_id) -> public_jwk  — tenant-scoped:
    the resolver MUST refuse to return a key whose key_id is not
    published in the active tenant's JWKS. Cross-tenant key reuse
    (key_id collision across tenants) is therefore not exploitable —
    a buyer signing for tenant B and presenting tenant A's transport
    fails resolution.

    nonce_store: optional cache for non-mutating tools (which don't
    require idempotency_key — see § Idempotency). Mutating tools get
    replay protection from the idempotency table; non-mutating tools
    get it from the nonce cache or accept the unbounded-replay risk
    when nonce_store is None.
    """
    def verify(request, tenant_id: str) -> AuthInfo:
        sig_input = _parse_signature_input(request)
        missing = [c for c in REQUIRED_COMPONENTS if c not in sig_input.covered]
        if missing:
            raise AdcpError('AUTH_INVALID',
                message=f'signature missing required components: {missing}',
                recovery='terminal')
        now = datetime.now(timezone.utc)
        if abs(now - sig_input.created) > MAX_CLOCK_SKEW:
            raise AdcpError('AUTH_INVALID',
                message=f'signature created skew exceeds {MAX_CLOCK_SKEW}',
                recovery='transient')
        if now > sig_input.expires:
            raise AdcpError('AUTH_INVALID',
                message='signature expired', recovery='terminal')
        public_jwk = jwks_resolver(tenant_id, sig_input.key_id)
        if public_jwk is None:
            raise AdcpError('AUTH_INVALID',
                message=f'key_id {sig_input.key_id!r} not in tenant {tenant_id!r} JWKS',
                recovery='terminal')
        verifier = HTTPMessageVerifier(
            signature_algorithm=_alg_from_jwk(public_jwk),
            key_resolver=lambda kid: _jwk_to_pubkey(public_jwk),
        )
        result = verifier.verify(request)
        if nonce_store is not None and 'nonce' in sig_input.covered:
            if not nonce_store.consume(tenant_id, sig_input.nonce, sig_input.expires):
                raise AdcpError('AUTH_INVALID',
                    message='nonce already consumed', recovery='terminal')
        return AuthInfo(
            kind='signed_request',
            key_id=sig_input.key_id,
            principal=result.label,
            scopes=result.metadata.get('scopes', []),
        )
    return verify

# Adopter wiring:
serve(
    create_adcp_server_from_platform(platform=seller),
    authenticate=signed_request_verifier(
        jwks_resolver=load_tenant_jwks,  # signature: (tenant_id, key_id) -> public_jwk
        nonce_store=RedisNonceStore(...),  # optional; for non-mutating-tool replay protection
    ),
)
```

**Why tenant-scoped JWKS resolution.** A flat `(key_id) -> jwk` resolver shared across tenants creates auth confusion: a buyer signing for tenant B who knows tenant A's `key_id` (predictable for human-readable IDs like `acme-key-v1`) can present a request to A's transport, the verifier finds the key, the signature validates, and the principal flows through A's `AccountStore.from_auth` to A-side data. Requiring the resolver to take `(tenant_id, key_id)` and reject keys outside that tenant's published JWKS closes the gap. Adopters MUST publish opaque key IDs (UUIDs, not vendor names) so collisions across tenants are statistically impossible.

**Why these covered components.** Without `content-digest`, a valid signature does not bind to the request body — an attacker MITM swapping the body to a different valid request keeps the signature intact. Without `created` + `expires`, the replay window is bounded only by the signing key's lifetime. The list above matches the TS verifier's required cover; the Python verifier does NOT accept signatures that omit any of them.

**Outgoing webhook signing** uses the same library — when `signed-requests` is claimed, push-notification webhooks emit RFC 9421-signed `Signature` + `Signature-Input` headers. The framework owns this; adopters write zero signing code.

**Salesagent migration:** today the salesagent has hand-rolled signature verification (or none, depending on tenant config). Migration is: install `http-message-signatures`, wire `serve(authenticate=...)`, delete the per-tool verification code. Idempotency-key + signing become framework concerns.

### Webhook delivery

Push-notification config rides on the buyer's mutating request:

```python
class PushNotificationConfig(TypedDict):
    url: str  # MUST be https:// (or test-env override)
    token: NotRequired[str]  # MUST be ≤ 255 chars, no control characters
```

Framework owns the SSRF guard. Port the TypeScript validator from [`runtime/from-platform.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/runtime/from-platform.ts):

```python
import ipaddress
from urllib.parse import urlparse

_ALLOWED_WEBHOOK_PORTS: frozenset[int] = frozenset({443, 8443})

def validate_push_notification_url(url: str) -> None:
    """Reject SSRF surfaces. Raises AdcpError(INVALID_REQUEST) for any
    of: non-https scheme (test/dev override via env),
    disallowed port (anything outside {443, 8443}),
    bare 'localhost'/'0', RFC 1918 (10/8, 172.16/12, 192.168/16),
    loopback (127/8, ::1), link-local (169.254/16 incl. AWS metadata,
    fe80::/10), CGNAT (100.64/10), IPv6 unique-local (fc00::/7),
    multicast/reserved, IPv4-mapped IPv6, bracketed IPv6 hosts,
    IPv6 zone identifiers, or hostnames whose DNS A/AAAA records
    resolve to any disallowed range at validate time.
    """
    parsed = urlparse(url)
    if parsed.scheme != 'https' and not _allow_http_test_override():
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'scheme {parsed.scheme!r} not allowed; must be https')
    port = parsed.port or 443
    if port not in _ALLOWED_WEBHOOK_PORTS:
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'port {port} not allowed; must be one of {sorted(_ALLOWED_WEBHOOK_PORTS)}')
    host = parsed.hostname or ''
    # Strip IPv6 brackets
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    # Strip IPv6 zone identifier (fe80::1%eth0) before parsing
    if '%' in host:
        host = host.split('%', 1)[0]
    if host in ('localhost', '0'):
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'host {host!r} not allowed')
    try:
        ip = ipaddress.ip_address(host)
        # IPv4-mapped IPv6 normalization
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        _reject_if_disallowed(ip, host)
    except ValueError:
        # Hostname — resolve at validate time and reject if ANY answer
        # is in a disallowed range. Pin-and-bind defends against later
        # DNS rebinding; this defends against operators discovering the
        # bad URL only at first delivery (after it's already in storage).
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, port,
                type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(sockaddr[0])
            _reject_if_disallowed(ip, host)

def _reject_if_disallowed(ip: ipaddress._BaseAddress, host: str) -> None:
    if ip.is_loopback or ip.is_link_local or ip.is_private \
            or ip.is_multicast or ip.is_reserved \
            or ip.version == 4 and ip in ipaddress.ip_network('100.64.0.0/10') \
            or ip.version == 6 and ip in ipaddress.ip_network('fc00::/7'):
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'host {host!r} resolves to disallowed range ({ip})')

def validate_push_notification_token(token: str) -> None:
    if len(token) == 0:
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.token',
                        message='token is empty')
    if len(token) > 255:
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.token',
                        message='token exceeds 255 chars')
    if any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.token',
                        message='token contains control characters')
```

**DNS rebinding: pin-and-bind ships in v6.0.** The validator inspects the literal hostname, but a buyer can register `https://rebind.attacker.com/` with a TTL-0 A-record that returns `8.8.8.8` at validate time and `127.0.0.1` at fetch time. v6.0 ships pin-and-bind delivery via a custom `httpx.AsyncHTTPTransport` so the IP resolved at request time (after re-checking SSRF ranges) is the IP the framework connects to — while preserving the original hostname for TLS SNI verification:

```python
# adcp/decisioning/webhooks/_pin_and_bind.py
import asyncio
import socket
import httpx

def create_pin_and_bind_session() -> httpx.AsyncClient:
    """httpx client that resolves each host at request time, applies SSRF
    range checks against ALL returned answers (rejecting if any are
    disallowed), connects to the first allowed IP while keeping the
    original hostname in TLS SNI, and refuses redirects to close the
    redirect-rebinding loophole."""

    class PinnedTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            host = request.url.host
            port = request.url.port or 443
            if port not in _ALLOWED_WEBHOOK_PORTS:  # {443, 8443}
                raise httpx.RequestError(
                    f'pin-and-bind rejected port {port} on {host}')
            loop = asyncio.get_running_loop()
            addr_info = await loop.getaddrinfo(
                host, port, type=socket.SOCK_STREAM)
            if not addr_info:
                raise httpx.RequestError(f'pin-and-bind: no DNS answers for {host}')
            # Reject if ANY answer is disallowed — defends against
            # multi-record rebinding races (private + public split).
            for entry in addr_info:
                _check_ip_against_ssrf_ranges(entry[4][0])
            # Pick first allowed IP; keep original host for TLS SNI.
            ip = addr_info[0][4][0]
            request.url = request.url.copy_with(host=ip)
            request.headers['Host'] = host
            request.extensions['sni_hostname'] = host  # httpx 0.27+: TLS verifies cert against original hostname
            return await super().handle_async_request(request)

    return httpx.AsyncClient(
        transport=PinnedTransport(verify=True),
        follow_redirects=False,  # redirects to a re-resolved different IP are the textbook rebinding bypass; reject and let the buyer fix their endpoint
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
    )

_ALLOWED_WEBHOOK_PORTS: frozenset[int] = frozenset({443, 8443})
```

**`_check_ip_against_ssrf_ranges` mirrors the validator** — same loopback / RFC 1918 / link-local / CGNAT / IPv6 ULA / multicast / IPv4-mapped-IPv6 rejections. Connection reuse across different webhook recipients is intentionally limited: each request re-resolves and re-checks. Pool reuse within a single recipient stays warm for `keepalive_expiry` seconds.

**IPv6 edge cases:** `ipaddress.ip_address('fe80::1%eth0')` raises `ValueError`; the validator strips zone identifiers (`host.split('%')[0]`) before parsing so link-local literals don't fall through to "hostname; mitigated via pin-and-bind." IPv4-mapped IPv6 in hex form (`::ffff:c0a8:0001`) is normalized via `ipaddress.ip_address(host).ipv4_mapped`, not string-splitting on `:`.

**Operator override.** `serve(webhook_client=...)` accepts a `WebhookTransport` (defined below), not a raw `httpx.AsyncClient` — the override MUST satisfy a Protocol the framework checks at boot. A naive override bypasses pin-and-bind silently if all the framework asks for is "an httpx client"; the Protocol gate prevents that.

```python
# adcp/decisioning/webhooks/transport.py
class WebhookTransport(Protocol):
    """Protocol the framework requires of any webhook delivery transport.
    Implementations MUST enforce SSRF range checks against the connection
    target IP at request time and reject redirects (or re-validate every
    hop). The framework checks shape at boot via runtime_checkable."""

    async def send(
        self, request: httpx.Request, *, account_id: str
    ) -> httpx.Response: ...

    @property
    def enforces_ssrf_at_connect(self) -> bool: ...
    """Must return True. Booleans on a Protocol are an explicit
    statement that the implementation has thought about SSRF; an
    operator override that returns False fails server boot."""
```

`serve(webhook_client=custom)` validates `custom.enforces_ssrf_at_connect is True` and emits a `WARNING` at boot naming the override so the operator audit log captures who turned off the secure default. If an operator genuinely needs to deliver through a vetted egress proxy with allowlist + mTLS, they implement `WebhookTransport` and own the SSRF guarantees explicitly — no silent bypass via "I just passed an httpx.AsyncClient with `proxies=...`."

**Webhook envelope** matches `mcp-webhook-payload.json`:

```python
class WebhookPayload(BaseModel):
    idempotency_key: str  # framework-generated; matches mcp-webhook-payload.json regex ^[A-Za-z0-9_.:-]{16,255}$
    operation_id: str  # client-generated, embedded in webhook URL by buyer; framework echoes back per mcp-webhook-payload.json
    task_id: str
    task_type: str  # tool name
    status: Literal['completed', 'failed', ...]
    timestamp: str  # ISO 8601
    protocol: Literal['media-buy', 'creative', 'signals',
                       'governance', 'brand', 'sponsored-intelligence']
    message: str | None = None  # populated on failed
    result: dict | None = None  # success arm body for completed
    error: dict | None = None  # {errors: [structured_error]} for failed
```

`operation_id` is buyer-supplied and threaded through the webhook URL path (per the buyer's push-notification config); the framework parses it on emission and echoes it in the payload so buyers correlate notifications without re-parsing the URL. Omitting it is a spec violation against `mcp-webhook-payload.json` — buyers MAY reject deliveries that don't carry the field they put in the URL.

Webhook delivery is gated to spec-listed task types (closed enum at AdCP 3.0 GA); the framework skips webhook emission with an explanatory log for tools outside the enum and uses `server.status_change.publish(event)` instead.

### A2A delivery path

The `WebhookPayload` envelope above is the **MCP** wire shape. A2A buyers receive the same logical lifecycle through a different transport: native A2A `Task` + `TaskStatusUpdateEvent` messages, with the AdCP payload nested in `status.message.parts[].data` (per `mcp-webhook-payload.json` line 4).

Framework dispatches at the transport layer:

```python
# adcp/decisioning/delivery/__init__.py
class TerminalDelivery(Protocol):
    """Carries a TaskRecord's terminal state to the buyer's transport."""

    async def deliver(self, task: TaskRecord, account: Account) -> None: ...

class McpWebhookDelivery:
    """Default for MCP buyers — emits WebhookPayload via pin-and-bind httpx."""
    async def deliver(self, task, account): ...

class A2aTaskDelivery:
    """For buyers who established an A2A session — emits TaskStatusUpdateEvent
    against the buyer's A2A endpoint with the AdCP payload nested in
    status.message.parts[].data. Reuses the same RFC 9421 signing path as
    McpWebhookDelivery."""
    async def deliver(self, task, account): ...
```

The framework selects the delivery based on the transport that originated the task (recorded on `TaskRecord.transport`); buyers don't have to declare it. Adopters who serve A2A wire `serve(transports=['mcp', 'a2a'], ...)` and the framework routes accordingly.

**Why this is in v6.0, not deferred to v6.1.** Any adopter shipping A2A buyers (Innovid training-agent's a2a-only deployment, salesagent's a2a-on-by-default tenants) cannot use `TaskHandoff` at all without the A2A delivery path. Shipping MCP-only delivery in v6.0 effectively makes hybrid handoff MCP-only, which is a wire-shape divergence from TS.

### Task registry

Framework-owned. Three implementations ship with v6.0; adopters pick the one matching their stack.

```python
# adcp/decisioning/task_registry.py
from typing import Protocol, Generic, TypeVar
from datetime import datetime

class TaskRegistry(Protocol):
    """Persistence boundary for HITL task state. Framework writes
    'submitted' on creation, 'working' on first progress update,
    'completed'/'failed' on terminal."""

    async def create(self, task: TaskRecord) -> None: ...
    async def update_progress(self, task_id: str, progress: dict) -> None: ...
    async def complete(self, task_id: str, result: dict) -> None: ...
    async def fail(self, task_id: str, error: dict) -> None: ...
    async def get(self, task_id: str, account_id: str) -> TaskRecord | None: ...
    async def list_by_account(self, account_id: str, ...) -> list[TaskRecord]: ...
```

**Two impls in v6.0, Protocol-shape ready for a third:**

| Impl | When to use | Notes |
|---|---|---|
| `InMemoryTaskRegistry` | Dev, tests, single-process toy deployments | Default. Loses state on restart. |
| `SqlAlchemyTaskRegistry(engine)` | Adopters with an existing SQLAlchemy stack (salesagent, Innovid) | Composes with the adopter's connection pool, transaction boundaries. Ships migrations under a **separate Alembic version table** (`version_table='adcp_alembic'`); adopters mount the framework's migrations independently of their own tree. |
| `AsyncpgTaskRegistry(pool)` | **Deferred to v6.1.** Slots in additively when a greenfield async-everywhere adopter asks for it; the Protocol shape doesn't change. | Reserved name; not shipped in v6.0. |

Salesagent wires `SqlAlchemyTaskRegistry` so the registry shares the existing connection pool and gets backed up by the same retention/replication story as the rest of salesagent's data.

**Alembic mounting (no migration-tree merging).** Earlier drafts proposed shipping migrations as Alembic revisions adopters merge into their existing tree — that breaks because Alembic chains via `down_revision`, and adopter heads differ. Instead, the framework ships its migrations under an isolated `version_table='adcp_alembic'` that runs independently. Adopters mount with one block in their `alembic.ini` (or programmatically):

```python
# adopter's migration runner — runs once per deploy, independent of
# the adopter's own migration tree:
from alembic.config import Config
from alembic import command
from adcp.decisioning.migrations import alembic_dir

cfg = Config()
cfg.set_main_option('script_location', str(alembic_dir()))
cfg.set_main_option('version_table', 'adcp_alembic')
cfg.set_main_option('sqlalchemy.url', adopter_db_url)
command.upgrade(cfg, 'head')
```

Adopter's own Alembic tree is untouched. Framework upgrades bump the framework's version table; adopter rollbacks of their own tree don't accidentally run framework downgrades.

```python
# Salesagent wiring:
from adcp.decisioning.task_registry import SqlAlchemyTaskRegistry
from salesagent.db import engine

serve(
    create_adcp_server_from_platform(
        platform=seller,
        task_registry=SqlAlchemyTaskRegistry(engine),
    ),
)
```

**Schema (mirrors TS Postgres migration):**

```sql
CREATE TABLE adcp_decisioning_tasks (
    task_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,  -- validated at the Python boundary against schemas/cache/<version>/enums/task-status.json; no DB CHECK so adding a status in AdCP 3.x doesn't reject framework-correct writes
    progress JSONB,
    result JSONB,
    error JSONB,
    has_webhook BOOLEAN NOT NULL DEFAULT FALSE,
    push_notification_url TEXT,
    push_notification_token TEXT,
    transport TEXT NOT NULL,  -- 'mcp' | 'a2a'; selects TerminalDelivery impl
    operation_id TEXT,  -- buyer-supplied, echoed in webhook payload (mcp transport) or A2A status update
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, task_id)
);
CREATE INDEX adcp_decisioning_tasks_account_id ON adcp_decisioning_tasks (account_id);
```

`task_id` is `TEXT` (not `UUID`) because the multi-tenant prefix scheme (`tenant_{tenant_id}_{account_id}_{uuid}`) emits strings, not UUIDs. The composite primary key `(account_id, task_id)` enforces tenant scoping at the DB layer — `get(task_id)` without `account_id` cannot collide with another account's task.

Framework writes `submitted` on task creation, `working` on first `update(progress)`, `completed` / `failed` on terminal. Other status values are emitted by adopters via the v6.1 `task_registry.transition()` API; the column accepts any value the Python validator passes, so spec evolution in `task-status.json` doesn't require a schema migration.

**Result/error JSONB cap at 4 MB** — same as TypeScript; adopter handoff functions returning oversized payloads are rejected at registry boundary before OOMing the Python process or the database.

**Multi-tenant deployments** sharing one registry rely on the composite primary key `(account_id, task_id)` for tenant isolation at the DB layer — `get(task_id)` without an `account_id` is not a valid framework call, and `get(task_id, account_id)` returns null when the row's `account_id` doesn't match. Adopters who want belt-and-suspenders namespacing can prefix task IDs as `tenant_{tenant_id}_{uuid}`, but the composite PK already enforces isolation; no second mechanism is required.

**Why a Protocol + multiple impls instead of asyncpg-only.** Earlier drafts of this RFC defaulted to asyncpg with a "salesagent runs SQLAlchemy elsewhere, separating concerns" rationale. That reasoning was inverted: forcing asyncpg means salesagent runs two connection pools, two migration systems (Alembic + framework DDL), two transaction boundaries that can't share a unit-of-work. The 2-3x perf delta on raw inserts doesn't matter when the registry hot path is bounded by HTTP latency to deliver webhooks. Adopter picks the impl that composes with their stack.

### Tenant registry

Multi-tenant primitive — same surface as TS [`tenant-registry.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/tenant-registry.ts):

```python
from dataclasses import dataclass, field
from typing import Literal, Callable, Awaitable, Generic

# Dataclass, not TypedDict: TypedDict + Generic is a runtime TypeError on
# 3.10 (TypedDict generics land in 3.11). Dataclass-with-slots is the
# 3.10-compatible replacement and matches Python's "config object" idiom.

@dataclass(slots=True)
class TenantSigningKey:
    key_id: str
    public_jwk: dict   # JWKS public-key shape
    private_jwk: dict  # JWKS private-key shape

@dataclass(slots=True)
class TenantConfig(Generic[TPlatform]):
    agent_url: str  # https://acme.example.com or https://example.com/acme
    signing_key: TenantSigningKey
    platform: TPlatform
    label: str | None = None
    server_options: ServerOptions | None = None

class TenantRegistry:
    def register(
        self,
        tenant_id: str,
        config: TenantConfig,
        *,
        await_first_validation: bool = False,
    ) -> Awaitable[None] | None:
        """Register a tenant. Lands in 'pending' health until JWKS
        validation succeeds. await_first_validation=True returns the
        validation outcome synchronously so deploy scripts can gate."""
        ...

    def unregister(self, tenant_id: str) -> None: ...

    def resolve_by_host(self, host: str) -> TenantResolution | None:
        """Subdomain routing — convenience wrapper for resolve_by_request(host, '/')."""
        ...

    def resolve_by_request(
        self, host: str, pathname: str
    ) -> TenantResolution | None:
        """Path-based routing — matches host AND longest-path-prefix.
        Strips query strings and fragments before matching."""
        ...

# Named return so adopters access .server / .config instead of
# tuple-index [2] / [1] — refactor-safe, type-checker friendly.
@dataclass(slots=True, frozen=True)
class TenantResolution:
    tenant_id: str
    config: TenantConfig
    server: DecisioningAdcpServer

    def publish_status_change(
        self, tenant_id: str, event: StatusChangeEvent
    ) -> None:
        """Tenant-scoped status-change publish for cross-tenant non-handler
        code (cron, queue workers). Looks up the tenant's server bus and
        dispatches; logs + drops on unknown/disabled tenant."""
        ...

    # ... unregister, recheck, list_tenants
```

**Health states (two orthogonal axes).** Real publisher onboarding has two independent gates: cryptographic verification (does the JWKS resolve and validate?) and operational gating (has ops/finance enabled the tenant?). Earlier drafts collapsed both into one `status` enum, which forced adopters to overload `'disabled'` to mean either "validation failed" or "operator turned off the account."

```python
@dataclass(slots=True)
class TenantHealth:
    verification: Literal['pending', 'healthy', 'unverified', 'failed']
    operator_gate: Literal['enabled', 'disabled']
    last_checked: datetime | None = None
    last_error: str | None = None
```

| Verification | Meaning |
|---|---|
| `'pending'` | Just registered, awaiting first JWKS validation. `resolve_by_host` returns null. |
| `'healthy'` | Validation succeeded. |
| `'unverified'` | Was healthy, transient recheck failure. Still serves for graceful degradation; framework retries on a backoff. |
| `'failed'` | Validation failed permanently (signing key revoked, JWKS endpoint gone). `resolve_by_host` returns null. |

| Operator gate | Meaning |
|---|---|
| `'enabled'` | Ops has activated the tenant. (Default after `register(..., enable=True)`.) |
| `'disabled'` | Ops has paused the tenant — JWKS may still validate, but the framework refuses to dispatch. |

`resolve_by_host` returns the tenant only when `verification in {'healthy', 'unverified'}` AND `operator_gate == 'enabled'`. Either gate failing returns null and the host transport responds 503 + Retry-After (`Retry-After` set from `last_checked + recheck_interval` for verification, omitted for operator-disabled).

**`register()` defaults.** New tenants land with `verification='pending'` and `operator_gate='enabled'` (no manual ops step required for the common case); deployments that want a separate ops gate call `register(..., enable=False)` and flip via `registry.enable(tenant_id)` after their internal workflow approves the tenant.

**JWKS race window:** `register()` lands tenants in `verification='pending'`; `resolve_by_host` returns null until first validation succeeds. Earlier drafts dropped tenants directly in `'unverified'` (serve immediately, validate later), which served signed responses no buyer could verify for ~60s. The `'pending'` gate closes the race.

**Admin-API auth:** `register()` JSDoc explicitly notes that any caller invoking `register()` can introduce a tenant that signs outbound webhooks; hosts wiring an HTTP/RPC endpoint in front MUST gate it with operator-level auth (mTLS, signed JWT, etc.). Framework doesn't ship admin-HTTP scaffolding because the right auth shape varies by deployment.

**Salesagent migration:** salesagent is currently single-tenant per process (or proxy-based multi-tenant). For multi-tenant deployments under the registry pattern, the migration is:

```python
# Before (salesagent today):
@app.before_request
def load_tenant():
    g.tenant = lookup_tenant_from_subdomain(request.host)

# After (v6.0):
registry = create_tenant_registry(default_server_options=...)
for tenant in load_all_tenants():
    registry.register(
        tenant.id,
        TenantConfig(
            agent_url=tenant.agent_url,
            signing_key=tenant.signing_key,
            platform=SalesAgentSeller(tenant_metadata=tenant.metadata),
        ),
    )

# Wire the framework's host-routing factory:
serve(
    factory=lambda ctx: registry.resolve_by_host(ctx.host).server,
    authenticate=signed_request_verifier(...),
)
```

### Observability hooks

Decision: **`dataclass` of optional callable fields**, not a `Protocol` class. Reasoning:

1. **Python convention** — sklearn, FastAPI, httpx all expose hooks as callable bags, not Protocol classes. Adopters wire one hook without subclassing or implementing every method.
2. **Optionality is cleaner** — Protocol with optional methods requires `# type: ignore[empty-body]` or `...` stubs. Dataclass with `Callable | None = None` reads naturally.
3. **Forward-compat** — adding a new hook to a dataclass is non-breaking (default `None`); adding a new method to a Protocol breaks every implementer.

```python
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class DecisioningObservabilityHooks:
    on_account_resolve: Callable[[AccountResolveEvent], None] | None = None
    on_task_create: Callable[[TaskCreateEvent], None] | None = None
    on_task_transition: Callable[[TaskTransitionEvent], None] | None = None
    on_webhook_emit: Callable[[WebhookEmitEvent], None] | None = None
    on_status_change_publish: Callable[[StatusChangePublishEvent], None] | None = None
    # Per-tool dispatch latency hooks land in v6.1
    # on_dispatch_start: ...
    # on_dispatch_end: ...

@dataclass
class AccountResolveEvent:
    tenant_id: str | None
    account_id: str | None  # may be None on resolution failure
    duration_ms: float
    from_auth: bool  # True when auth-derived path
    success: bool
    error_code: str | None = None

# ... similar for the other 4
```

**Throw-safe** — adopter telemetry mistakes are caught + logged via the framework logger, never break dispatch:

```python
def _safe_fire(hook, event):
    if hook is None:
        return
    try:
        result = hook(event)
        if inspect.iscoroutine(result):
            # Schedule on the event loop; warn on rejection
            asyncio.ensure_future(result).add_done_callback(_log_hook_rejection)
    except Exception as e:
        _logger.warning("observability hook raised: %s", e)
```

## Adopter integration

### `serve()` configuration reference

Every kwarg `serve()` accepts, defaults, and one-line semantics. (The same kwargs are accepted by `create_adcp_server_from_platform` for the server-shape concerns; transport-shape concerns live on `serve()` directly.)

| Kwarg | Type | Default | Effect |
|---|---|---|---|
| `platform` (positional via factory) | `DecisioningPlatform` | required | The adopter platform impl. Provided either as `create_adcp_server_from_platform(platform=...)` or via `serve(factory=lambda ctx: ...)` for multi-tenant routing. |
| `factory` | `Callable[[ServeContext], DecisioningAdcpServer]` | `None` | Multi-tenant routing — receives the request context (host, path, auth) and returns the right server. Mutually exclusive with passing a single server. |
| `authenticate` | `Callable[[Request, str], AuthInfo]` | `None` (no auth) | RFC 9421 verifier wired via `signed_request_verifier(...)`, or any callable producing `AuthInfo`. |
| `task_registry` | `TaskRegistry` | `InMemoryTaskRegistry()` | Override with `SqlAlchemyTaskRegistry(engine)` in production. |
| `status_change_bus` | `StatusChangeBus` | `InMemoryStatusChangeBus()` | Override with `DbBackedStatusChangeBus(engine)` for audit-relevant deployments. |
| `webhook_client` | `WebhookTransport` | `PinAndBindWebhookTransport()` | Override only when you have a vetted egress proxy with mTLS allowlist; the override MUST satisfy the `WebhookTransport` Protocol with `enforces_ssrf_at_connect=True`. |
| `thread_pool_size` | `int` | stdlib default (`min(32, os.cpu_count() + 4)`) | Sizes the executor `to_thread` dispatches sync handlers onto. Bump for high-fanout sync deployments. |
| `idempotency_retention` | `timedelta` | `timedelta(days=7)` | TTL for cached idempotent responses. |
| `strict_wire_validation` | `bool \| None` | `None` (reads `ADCP_FORWARD_COMPAT`) | When `True`, Pydantic models reject extras; when `False`, ignore. `None` → reads env. |
| `transports` | `list[Literal['mcp', 'a2a']]` | `['mcp']` | Which transports the framework binds. A2A requires `task_registry` to record `transport='a2a'` for proper terminal-delivery routing. |
| `name` | `str` | required | MCP server `serverInfo.name`. |
| `version` | `str` | required | MCP server `serverInfo.version`. |
| `observability_hooks` | `DecisioningObservabilityHooks` | `DecisioningObservabilityHooks()` | Adopter-supplied dataclass of optional callable hooks. |

### Picking the right account-resolution mode

| If your deployment is... | Pick | Why |
|---|---|---|
| Multi-tenant where the URL or request body identifies the account (`/tenants/<id>`, `account.account_id` in body) | `'explicit'` | The `ref` argument carries the account ID; `resolve(ref, ctx)` reads it. |
| Multi-tenant or single-tenant where the verified auth principal identifies the account (signed-request-bound, OAuth-bearer-bound) | `'from_auth'` | `ctx.auth_info.principal` is the lookup key; `ref` is ignored. |
| One process serving exactly one platform (Innovid training-agent, single-publisher proof-of-concept) | `'singleton'` | `resolve` returns one synthesized `Account`; idempotency keys MUST scope to verified principal (see § *From Innovid training-agent*). |

If you have **both** signed-request auth and `account_id` in the body (rare — only DSPs that expose multi-account-per-principal flows), pick `'explicit'`: the body is the source of truth and the auth principal becomes a scope check inside `resolve`.

### Picking the right `publish_status_change` surface

| You hold... | Call | Notes |
|---|---|---|
| `RequestContext` (inside a handler) | `ctx.publish_status_change(event)` | Framework wires the per-server bus through `ctx`. No imports needed. |
| `DecisioningAdcpServer` (cron, queue worker scoped to one tenant) | `server.status_change.publish(event)` | Same pattern as holding any other long-lived dependency. |
| `TenantRegistry` (cron, queue worker scoped to many tenants) | `await registry.publish_status_change(tenant_id, event)` | Async because the registry validates `event.account_id` belongs to `tenant_id` before fanning out. |
| Nothing — you're outside the framework | Don't publish; return through a normal request and let the framework publish from the dispatch seam | The bus is server-scoped on purpose. |

### Testing handlers

`adcp.decisioning.testing` ships fixtures so adopter unit tests don't need a running server:

```python
# tests/test_my_seller.py
from adcp.decisioning.testing import make_test_context
from my_seller import HelloSeller

def test_create_media_buy_returns_sync_success():
    seller = HelloSeller()
    ctx = make_test_context(
        account_id='hello',
        metadata={'kind': 'test'},
        # auth_info defaults to a synthetic principal; override for
        # principal-scoped tests
    )
    req = CreateMediaBuyRequest(...)
    result = seller.create_media_buy(req, ctx)
    assert result.media_buy_id.startswith('mb_')
    assert result.status == 'pending_creatives'
```

`make_test_context(...)` returns a `RequestContext` with stub `state`, `resolve`, `now`, and `auth_info` — adopters override exactly the fields their test cares about. The status-change bus is a `MemoryStatusChangeBus` you can introspect via `ctx._test_bus.events`.

### Local-dev JWKS

Production wires `signed_request_verifier(jwks_resolver=...)` against the tenant's published JWKS. Local dev has two escape hatches:

```python
# Option 1: skip auth entirely for dev
serve(server, authenticate=None)  # framework dispatches without verifying signatures

# Option 2: dev JWKS fixture — generates an ephemeral keypair, hosts
# the JWKS at localhost:9999/.well-known/jwks.json, and configures
# the verifier against it. Use to test the signed-request code path
# end-to-end without external infrastructure.
from adcp.decisioning.dev import JwksFixture
with JwksFixture(port=9999) as fixture:
    serve(
        server,
        authenticate=signed_request_verifier(
            jwks_resolver=fixture.tenant_resolver(),
        ),
    )
```

`JwksFixture` is a context manager — it's intentionally not exported from the top-level module so production deployments can't accidentally use it.

## Migration paths

### From salesagent (Flask + per-adapter classes)

**Realistic scope.** Full salesagent migration is calendar-months of engineering, not weeks. The migration touches every tool dispatch path, the tenant model, the audit-log integration, OAuth callbacks, and the existing per-adapter abstraction layer. The plan below is staged so each stage ships independently — the merge seam in `serve()` accepts v5-style handler entries alongside v6 platforms, so half-migrated states are deployable.

**Stage 1 — Foundation (1-2 weeks).** Bump the salesagent's `adcp` pin to `>=5.0,<6.0`, switch the existing tool entry points from `ADCPHandler`-style registration to the `create_adcp_server_from_platform` adapter in handler-bag mode (no `Protocol` impls yet — the merge seam keeps existing `@tool` handlers running). This proves the framework loads against the salesagent's existing wire surface without any per-tool migration.

```python
from adcp.decisioning import serve, create_adcp_server_from_platform
from adcp.decisioning.task_registry import SqlAlchemyTaskRegistry
from salesagent.db import engine

# At this stage, server still routes everything through the existing
# salesagent handler bag — no platforms yet. The framework runs
# alongside the existing app and inherits the existing adcp.signing /
# adcp._idempotency primitives unchanged.
existing_handlers = load_existing_handlers()

server = create_adcp_server_from_platform(
    platform=None,  # all-handler mode
    handlers=existing_handlers,
    task_registry=SqlAlchemyTaskRegistry(engine),
)
serve(server, authenticate=signed_request_verifier(...))
```

**Stage 2 — Per-specialism conversion (2-4 weeks per specialism).** Convert one specialism (e.g., sales) to the new shape:

```python
# Migrate sales tools to a SalesPlatform impl while keeping audiences,
# signals, etc. on v5 handlers. The merge seam allows this.
class SalesAgentSales(SalesPlatform):
    def __init__(self, tenant):
        self._tenant = tenant
        self._gam = GoogleAdsClient(...)

    def create_media_buy(self, req, ctx):
        adapter = ctx.account.metadata.adapter  # GAMAdapter, etc.
        if self._is_pre_approved(req, ctx.account):
            return adapter.create_immediate(req)
        return ctx.handoff_to_task(async lambda task_ctx:
            adapter.create_with_review(req, task_ctx))

# Wire alongside surviving v5 handlers:
server = create_adcp_server_from_platform(
    platform=SalesAgentSeller(sales=SalesAgentSales(tenant)),
    handlers={'audiences': v5_audience_handlers, ...},  # not yet ported
    task_registry=SqlAlchemyTaskRegistry(engine),
)
```

**Stage 3 — Multi-tenant (1-2 weeks).** Switch from per-process tenants to `TenantRegistry`:

```python
registry = create_tenant_registry()
for tenant in load_all_tenants():
    registry.register(tenant.id, TenantConfig(
        agent_url=tenant.agent_url,
        signing_key=tenant.signing_key,
        platform=make_seller(tenant),
    ))
serve(factory=lambda ctx: registry.resolve_by_host(ctx.host).server, ...)
```

**Stage 4 — Cleanup (1-2 weeks).** Delete superseded code:

- Hand-rolled idempotency middleware (framework owns it)
- Hand-rolled signature verifier (framework owns it via `authenticate=...`)
- Hand-rolled sandbox routing (framework owns it via `Account.metadata.sandbox`)
- Hand-rolled status-change emitter (replaced with `ctx.publish_status_change(event)` or `server.status_change.publish(event)`)
- The `@tool` decorator and its registration table
- Per-tool Flask routes that wrapped handler invocations

**Things the migration does NOT eliminate.** Salesagent has many consumers of `g.tenant` outside the AdCP wire surface — admin UI, audit logs, OAuth callbacks, internal cron jobs. These don't go away. The migration adds a `g.tenant`-shim that reads from `ctx.account.metadata` for AdCP request handlers; non-AdCP routes continue using Flask's request context as before.

**Estimated total effort:**

The earlier 18-21-week estimate assumed a greenfield port building all primitives from scratch. Landing inside `adcp` reuses the existing `adcp.signing` / `adcp._idempotency` / `adcp.server` / `adcp.types` foundation, which compresses the budget substantially.

- **Audit + gap-fixes against existing primitives:** ~2-3 weeks total. Each audit issue (pin-and-bind, RFC 9421 verifier, idempotency keying, `extra='forbid'`, webhook `operation_id`, multi-tenant `factory=`) is a small fix-PR; some may already be compliant.
- **New `adcp.decisioning.*` layers:** ~8-10 weeks. The largest pieces are `task_registry` with `SqlAlchemyTaskRegistry` (~1.5 weeks), `tenant_registry` (~1 week), 12 Protocol classes (~2 weeks parallelizable), `dispatch` adapter seam (~1 week), `DbBackedStatusChangeBus` (~3 days), A2A delivery (~1 week), JCS parity test (~3-4 days), examples + SKILL (~1 week). Calendar: ~3-4 months with one engineer + AI assistance, ~6-8 weeks if work parallelizes across 2-3 contributors after the audit phase.
- **Salesagent migration:** ~8-12 weeks calendar after framework lands. Stage 2 (per-specialism conversion) is the long pole; Stage 1 + 3 + 4 are 1-2 weeks each.
- **Total runway from PR #290 merge to "salesagent on v6.0 in production":** **~4-6 months** (down from 6-8).

Smaller adopters with fewer tools and a single tenant can finish their migration in 2-3 weeks once the framework ships.

### From Innovid training-agent

Single-tenant agent. `'singleton'` resolution returns a synthetic singleton account; the framework's tenant-scoped invariants (idempotency, status-change `account_id`, workflow steps) all work without forcing the adopter to model multi-tenancy:

```python
class TrainingAgentAccounts:
    resolution = 'singleton'

    def resolve(self, ref, ctx=None):
        # Singleton — ignore ref. Synthesize a per-principal account
        # ID from the verified auth principal so idempotency / status-
        # change / observability scope correctly across distinct callers.
        # Hard-coding id='training-agent' for every caller would alias
        # all callers into one idempotency cache, leaking the first
        # caller's response_payload to anyone hitting the same UUID.
        principal = (ctx.auth_info.principal if ctx and ctx.auth_info
                     else 'anonymous')
        return Account(
            id=f'training-agent:{principal}',
            name=f'Innovid Training Agent ({principal})',
            status='active',
            metadata={'kind': 'training_agent', 'principal': principal},
            auth_info=ctx.auth_info if ctx else {'kind': 'anonymous'},
        )

class TrainingAgentSeller(SalesPlatform):
    accounts = TrainingAgentAccounts()
    # ... single platform, no per-tenant lookup
```

**Why singleton must scope to principal.** The framework idempotency table is keyed `(idempotency_key, account_id, tool_name, body_hash)`. With singleton hard-coding `id='training-agent'`, every caller across the entire deployment shares one idempotency namespace; UUID collision (random or engineered) returns another caller's cached `response_payload` — a buyer-to-buyer data leak. Synthesizing the account ID from the verified auth principal restores per-caller scoping while keeping the "one platform, no per-tenant lookup" ergonomics singleton mode is for.

**Singleton in multi-tenant deployments.** `validate_platform()` rejects `'singleton'` resolution when registered into a `TenantRegistry` containing >1 tenant — singleton mode is for one process serving one platform. Adopters who want per-tenant singletons should use `'from_auth'` with an `AccountStore` that maps verified principals to accounts.

See [`docs/proposals/decisioning-platform-training-agent-migration.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-training-agent-migration.md) for the full migration plan.

### From scratch (new adopter)

Three-step intro mirrors the TS SKILL:

A complete runnable version ships at [`examples/hello_seller.py`](https://github.com/adcontextprotocol/adcp-client-python/blob/main/examples/hello_seller.py) — 30 lines, copy-paste-runnable on `localhost:8080`. The framework's CI runs it on every PR so it can't drift from the surface.

```python
# examples/hello_seller.py — full runnable file
from adcp.decisioning import (
    DecisioningPlatform, DecisioningCapabilities, SingletonAccounts,
    create_adcp_server_from_platform, serve,
)
from adcp.types import (
    GetProductsRequest, GetProductsResponse,
    CreateMediaBuyRequest, CreateMediaBuySuccess,
)

class HelloSeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=['sales-non-guaranteed'],
        channels=['display'],
        pricing_models=['cpm'],
    )
    accounts = SingletonAccounts(account_id='hello')

    def get_products(self, req: GetProductsRequest, ctx) -> GetProductsResponse:
        return GetProductsResponse(products=[...])

    def create_media_buy(
        self, req: CreateMediaBuyRequest, ctx,
    ) -> CreateMediaBuySuccess:
        return CreateMediaBuySuccess(
            media_buy_id=f'mb_{req.idempotency_key}',
            status='pending_creatives',
            confirmed_at=ctx.now.isoformat(),
            packages=[],
        )

if __name__ == '__main__':
    serve(create_adcp_server_from_platform(
        platform=HelloSeller(), name='hello-seller', version='0.0.1',
    ))
```

`SingletonAccounts(account_id='hello')` is the framework-shipped `AccountStore` for the singleton case — adopters who need per-principal scoping subclass and override `resolve` (see § *From Innovid training-agent*).

The Python SKILL ships a single canonical example mirroring [`skills/build-decisioning-platform/SKILL.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/skills/build-decisioning-platform/SKILL.md) — same fields, same error codes, same migration sketch.

## Open questions

These need decisions before the Python port lands `rc.1`:

### 1. Async-vs-sync method dispatch

Should the framework **detect** sync/async at dispatch time (`inspect.iscoroutinefunction`) and run sync methods on `asyncio.to_thread`, or **force** adopters to write async-everywhere?

- **Detect + thread-pool** — easier migration for sync codebases (Flask salesagent), correct behavior under load (no event-loop blocking), but loses `mypy --strict` "did you forget an `await`" check inside sync methods that touch async I/O. Concurrency ceiling shifts to thread-pool size.
- **Force async** — cleaner type story, but forces salesagent to migrate to `asgiref.sync.async_to_sync` shims everywhere a sync DB driver is touched, which is invasive.

**RFC recommendation: detect + thread-pool, with `contextvars.copy_context()` propagation.** The migration cost of forced async is too high; the type-checker gap is real but bounded; the thread-pool cost is bounded by the configurable pool size.

### 2. Pydantic 2 — extra policy default

Wire types are Pydantic 2 `BaseModel`. `extra` policy default:

- **`'ignore'` always** — forward-compat with newer spec versions, but **silently drops** any field the buyer (or the buyer's LLM) sends, including hallucinated fields the buyer thinks were honored. For an agent-consumed protocol this is the worst failure mode.
- **`'forbid'` always** — surfaces unknown fields as wire-level rejections the buyer can act on. Forward-compat costs an explicit operator action during spec rollouts.
- **Env-driven** (production: `'ignore'`, dev: `'forbid'`) — convenient, but ties forward-compat to the env name rather than a deliberate spec-rev decision; production debugging suffers.

**RFC recommendation: `'forbid'` default in all environments**, with `ADCP_FORWARD_COMPAT=permissive` (or `serve(strict_wire_validation=False)`) as the explicit opt-in for rolling spec upgrades. Adopters override per-model via `model_config = ConfigDict(...)` if they need different behavior on a specific field. Python is one-directionally stricter than TS (whose Zod default is `.strip()`), which is safe — anything Python accepts, TS accepts.

### 3. Packaging — in-package landing

**RFC recommendation: land DecisioningPlatform inside the existing `adcp` package** at `adcp.decisioning.*`. The `adcp` package on PyPI is at v4.0.0; v5.0.0 introduces the `DecisioningPlatform` Protocol-driven pattern alongside the existing `ADCPHandler` class-pattern (no removal in v5; `ADCPHandler` deprecation handled in a later major when the migration path proves out).

Earlier RFC drafts proposed a separate `adcp-server` package — that's rejected for three reasons:

1. **The foundation primitives are already in `adcp`.** RFC 9421 signing, JCS canonicalization, IP-pinned transport, JWKS resolver, idempotency middleware, generated wire types, MCP + A2A transports — all already shipped at v4. Splitting them across two packages would mean duplicating or vendoring half of `adcp.signing` into `adcp-server`, which creates two parallel implementations adopters have to keep in sync.
2. **Cross-package version coupling is a worse failure mode** than in-package version coupling. With a single package, `pip install adcp>=5.0,<6.0` pins everything wire-relevant atomically. With two packages, adopters hit the "did I update both?" footgun.
3. **The existing `ADCPHandler` adopters need a migration story, not a fork.** A separate package would force them to choose: stay on `adcp` v4 or switch to `adcp-server`. Landing inside the package lets them keep their existing imports while adopting the new pattern incrementally.

**Version pinning:** the Python package version (`5.0.0`) ships its own cadence independent of the TypeScript SDK, but both pin to the same `ADCP_VERSION` (currently `3.0.0`, file at `src/adcp/ADCP_VERSION`). When AdCP 3.1 ships, both SDKs cut new majors that bump `ADCP_VERSION`; adopters who pin `adcp>=5.0,<6.0` and `@adcp/client@>=3.0.0 <4.0.0` get the same wire surface.

### 4. CI matrix — Python 3.10 / 3.11 / 3.12 / 3.13?

**RFC recommendation: 3.10 minimum** (PEP 604 union syntax `int | str`, `match` statement). Drop 3.9 — it's EoL October 2025; the salesagent is already on 3.11. Test 3.10, 3.11, 3.12, 3.13.

PEP 696 (`TypeVar` defaults — `TMeta = TypeVar("TMeta", default=dict)`) needs 3.13 for runtime support; on 3.10-3.12 we ship via `typing_extensions.TypeVar`.

### 5. Type-checker support — mypy strict, pyright strict, both?

**RFC recommendation: framework runs mypy strict + pyright strict on every PR; adopter expectations are advisory.** Pydantic v2's mypy plugin generates noise under `--strict` (especially `Self` return-type propagation through inheritance) that adopters shouldn't have to absorb. The framework SHOULD type-check clean under both; the SKILL's example and the migration template SHOULD type-check clean under non-strict; adopter codebases set their own bar.

### 6. Submitted-arm spec consolidation (adcp#3392) — port wait or land alongside?

Currently TypeScript SDK ships hybrid handoff only for the two tools whose per-tool `xxx-response.json` schema includes the `Submitted` arm inline (`create_media_buy`, `sync_creatives`). The other 4 HITL-eligible tools (`update_media_buy`, `build_creative`, `sync_catalogs`, `sync_event_sources`) have inconsistent spec response schemas — `Submitted` is in `async-response-data.json` only.

[adcp#3392](https://github.com/adcontextprotocol/adcp/issues/3392) proposes spec consolidation so all 6 tools have rolled-in `Submitted` arms. When that lands, the SDK adds hybrid-handoff support for the other 4.

**RFC recommendation: track adcp#3392 as a v6.1 release blocker, not a deferrable.** Real publisher operations make HITL eligible on every guaranteed-deal mutation (budget changes, flight shifts, daypart adjustments, creative rotation, package swaps), not just create. Salesagent's GAM adapter today routes `update_media_buy` through trafficker review. Forcing buyer agents into two divergent state-machine shapes — task-poll for create, status-subscription for update — for the same logical workflow produces buyer-side bugs and is harder to reason about than the dual-method shape it replaced.

For v6.0, ship the wire-spec-conformant subset (hybrid handoff on `create_media_buy` + `sync_creatives`) plus the workaround-shaped lifecycle for the other 4 tools; for v6.1, land hybrid handoff on `update_media_buy`, `build_creative`, `sync_catalogs`, `sync_event_sources` simultaneously with adcp#3392 consolidation. The Protocol shape doesn't change between versions — only the dispatcher's "which arms are valid for this tool's response" check does.

**Alternative path if adcp#3392 stalls.** The framework can ship hybrid handoff on the other 4 tools by emitting a `tasks/submit` task-router envelope (already valid per `async-response-data.json`) instead of inlining the `Submitted` arm in each tool's response. The buyer pattern-match is identical (`status === 'submitted'`); only the JSON envelope shape differs. This is an SDK-side projection, not a wire-protocol change, and it lets v6.1 ship hybrid handoff on schedule even if spec consolidation is late. Document the projection clearly so buyers reading the SDK don't think the wire shape changed.

**Adopter impact.** Salesagent's `update_media_buy` re-approval flow needs HITL today. Until #3392 lands, the workaround is to return `UpdateMediaBuySuccess` synchronously with the `status` field **omitted** (the schema description at `update-media-buy-response.json` says `status` is "Present when the update changes the media buy's status" — leaving it absent on in-flight re-approval is in-spec), then drive the lifecycle via `publish_status_change`. Less ergonomic than a `TaskHandoff`, but spec-valid.

**Buyer-side polling visibility cost.** While a re-approval is in-flight against an active media buy, a buyer polling `getMediaBuy` sees `status: 'active'` — there is no wire-level signal that an update is pending. Lifecycle visibility is push-only via status-change subscriptions. Buyers subscribed to status changes get the full progression; buyers who only poll see no diff until the update completes. This is a regression for poll-only buyers and a one-line cost worth surfacing to the salesagent migration: platforms whose existing re-approval surface set `status: 'pending_approval'` on the resource itself were giving polling buyers a signal that v6.0 cannot preserve until #3392 lands.

`status='pending_approval'` is **not valid** on `update-media-buy-response`: `MediaBuyStatus` enum is `[pending_creatives, pending_start, active, paused, completed, rejected, canceled]` (`pending_approval` belongs to `Account.status`, not `MediaBuy.status`). Adopters porting from earlier drafts that emitted `pending_approval` here MUST drop the field instead.

**Tools impacted by adcp#3392 (corrected).** The 4 HITL-eligible tools awaiting consolidation are `update_media_buy`, `build_creative`, `sync_catalogs`, `sync_event_sources`. (Earlier drafts listed `get_products` here — `get_products` is a sync read whose async / proposal-mode surface rides on a separate `request_proposal` verb, not on a `Submitted` arm.) `sync_audiences` lifecycle flows through `publish_status_change` regardless of #3392 because audience match-rate computation is a true background pipeline, not a HITL gate.

## Appendix: Wire payload examples

### `create_media_buy` (sync fast path)

**Request (buyer → seller):**

```json
{
  "method": "tools/call",
  "params": {
    "name": "create_media_buy",
    "arguments": {
      "account": { "account_id": "acme_tenant_42" },
      "buyer_ref": "pre_approved",
      "products": [{ "product_id": "prod_premium_video" }],
      "total_budget": { "amount": 50000, "currency": "USD" },
      "idempotency_key": "8f4e2a1c-d6b8-4f9e-9a3c-7b1d5e8f2a4d"
    }
  }
}
```

**Response (TypeScript SDK + Python SDK identical):**

```json
{
  "structuredContent": {
    "media_buy_id": "mb_acme_1714271234",
    "status": "pending_creatives",
    "confirmed_at": "2026-04-28T13:47:14Z",
    "packages": []
  }
}
```

### `create_media_buy` (HITL slow path)

**Request:** same as above, but `buyer_ref` not pre-approved.

**Response (Submitted envelope):**

```json
{
  "structuredContent": {
    "task_id": "5b1e9a8c-3d2f-4f1e-8b9d-6a7c5f3d2b1a",
    "task_type": "create_media_buy",
    "status": "submitted",
    "timestamp": "2026-04-28T13:47:14Z",
    "protocol": "media-buy"
  }
}
```

Buyer polls via `tasks_get` or receives webhook on terminal state.

### `sync_audiences` (sync ack + status-change)

**Request:**

```json
{
  "method": "tools/call",
  "params": {
    "name": "sync_audiences",
    "arguments": {
      "account": { "account_id": "idg_acc_1" },
      "audiences": [{ "audience_id": "aud_42", "identifiers": ["e1", "e2", "e3", "e4"] }],
      "idempotency_key": "8f4e2a1c-d6b8-4f9e-9a3c-7b1d5e8f2a4d"
    }
  }
}
```

**Sync response:**

```json
{
  "structuredContent": {
    "audiences": [{
      "audience_id": "aud_42",
      "action": "created",
      "status": "processing",
      "matched_count": 0,
      "effective_match_rate": 0
    }]
  }
}
```

**Status-change events (later, via MCP Resources subscription or `tasks_get`):**

```json
{ "resource_type": "audience", "resource_id": "aud_42",
  "payload": { "stage": "matched", "status": "processing", "matched_count": 1680, "match_rate": 0.42 } }
{ "resource_type": "audience", "resource_id": "aud_42",
  "payload": { "stage": "activating", "status": "processing" } }
{ "resource_type": "audience", "resource_id": "aud_42",
  "payload": { "stage": "active", "status": "ready" } }
```

### `tasks_get` (Submitted task lifecycle)

**Request:**

```json
{
  "method": "tools/call",
  "params": {
    "name": "tasks_get",
    "arguments": {
      "task_id": "5b1e9a8c-3d2f-4f1e-8b9d-6a7c5f3d2b1a",
      "account": { "account_id": "acme_tenant_42" }
    }
  }
}
```

**Response (in-progress):**

```json
{
  "structuredContent": {
    "task_id": "5b1e9a8c-3d2f-4f1e-8b9d-6a7c5f3d2b1a",
    "task_type": "create_media_buy",
    "status": "working",
    "timestamp": "2026-04-28T13:48:30Z",
    "protocol": "media-buy",
    "has_webhook": true,
    "progress": { "stage": "trafficker_review", "step": "2_of_3" }
  }
}
```

**Response (completed):**

```json
{
  "structuredContent": {
    "task_id": "5b1e9a8c-3d2f-4f1e-8b9d-6a7c5f3d2b1a",
    "task_type": "create_media_buy",
    "status": "completed",
    "timestamp": "2026-04-28T15:22:11Z",
    "completed_at": "2026-04-28T15:22:11Z",
    "protocol": "media-buy",
    "has_webhook": true,
    "result": {
      "media_buy_id": "mb_acme_1714271234",
      "status": "active",
      "confirmed_at": "2026-04-28T15:22:11Z",
      "packages": [...]
    }
  }
}
```

## Appendix: Validation matrix

The Python port MUST pass equivalents of these TypeScript test files. Wire-shape parity is non-negotiable:

| TypeScript test | Python equivalent | What it pins |
|---|---|---|
| `test/server-decisioning-mock-seller.test.js` | `tests/test_mock_hybrid_seller.py` | Unified hybrid sync + HITL branch per call; `ctx.handoff_to_task` produces a marker the framework dispatches; pre-approved buyer fast-path returns wire `Success` directly |
| `test/server-decisioning-from-platform.test.js` | `tests/test_dispatch.py` | `AdcpError` raise-path projects to wire envelope; multi-error preflight `details.errors`; sandbox routing; idempotency-key replay; merge-seam collision warnings; auth-derived account resolution; sync-handler thread-pool dispatch |
| `test/server-decisioning-tenant-registry.test.js` | `tests/test_tenant_registry.py` | Subdomain + path-prefix routing; `'pending'` health gate; JWKS fetch timeout; admin-API auth contract; query-string stripping |
| `test/server-decisioning-identity-graph.test.js` | `tests/test_audience_sync.py` | Sync ack + multi-stage `publish_status_change`; rich-internal-stage → wire-flat-status collapse; `effective_match_rate` field; rejection without status-change events |
| `test/server-decisioning-postgres-task-registry.test.js` | `tests/test_postgres_task_registry.py` | Status validated at the Python boundary against `task-status.json` (no DB CHECK — spec evolution doesn't require migration); composite PK `(account_id, task_id)` enforces tenant isolation at DB layer; `progress` JSONB transitions; 4 MB result/error cap; `has_webhook` + `transport` + `operation_id` fields. Runs against the two v6.0 impls (in-memory, SqlAlchemy) via parameterized fixture; asyncpg fixture added when impl ships in v6.1. |
| `test/server-decisioning-task-webhooks.test.js` (MCP arm) | `tests/test_webhooks_mcp.py` | MCP `WebhookPayload` envelope shape; RFC 9421 signed delivery with locked covered-fields list; SSRF guard rejections (50+ surfaces incl. port allowlist + IPv6 zone IDs + DNS-resolved private ranges); pin-and-bind defeats DNS rebinding (validate-time IP ≠ delivery-time IP scenario); TLS SNI preserved on rewrite; redirects rejected; multi-record getaddrinfo all-or-none rejection; `WebhookTransport` Protocol gate on operator override; failed-task error envelope; `task_type` closed-enum gate; `operation_id` echoed back; idempotency-key matches spec regex (not UUIDv4-assumed) |
| `test/server-decisioning-task-webhooks.test.js` (A2A arm) | `tests/test_webhooks_a2a.py` | A2A `Task` + `TaskStatusUpdateEvent` envelope; AdCP payload nested at `status.message.parts[].data`; same RFC 9421 signing path as MCP arm; same SSRF + pin-and-bind transport reused; `transport='a2a'` recorded on `TaskRecord` selects `A2aTaskDelivery` impl |
| (new) | `tests/test_jcs_parity.py` | RFC 8785 JCS canonicalization byte-equal between Python and TypeScript on a corpus of payloads exercising number normalization (`1.0` vs `1`, scientific notation, integer-vs-float boundary), key ordering, nested objects, unicode escapes, and NaN/Infinity rejection. Cross-language idempotency keying depends on this hash matching exactly. |
| `test/server-decisioning-status-changes.test.js` | `tests/test_status_changes.py` | Per-server bus isolation; cross-server publish does NOT leak (regression test for the deleted module-level singleton); thread-safe subscriber list under sync-handler concurrency; 10-value resource-type enum + `'x-'` forward-compat; tenant scoping via `TenantRegistry.publish_status_change` validates `event.account_id` belongs to `tenant_id` before forwarding (cross-tenant leak regression); `DbBackedStatusChangeBus` durability test |
| `test/server-decisioning-validate-platform.test.js` | `tests/test_validate_platform.py` | "Claimed X; missing Y" diagnostic; specialism-method coverage matrix; runtime check at server boot; rejects `'singleton'` resolution under `TenantRegistry` with >1 tenant |
| `test/server-decisioning-idempotency.test.js` | `tests/test_idempotency.py` | Replay on duplicate `(idempotency_key, account_id, tool_name, body_hash)` tuple; same key + different body returns `INVALID_REQUEST` (not silent-replay); expiry honors `expires_at`; payload cap rejection; vacuum cleanup deletes expired rows; singleton-mode keys scope per-principal (buyer-to-buyer leak regression) |
| (new) | `tests/test_wire_parity.py` | TS-produced golden files load + deep-equal against Python responses for the same logical inputs. Field ordering, null-vs-omitted, datetime serialization (ISO 8601 with `Z` suffix), float precision. Catches drift between ports before it reaches buyers. |

## Decision summary

1. **Async-or-sync method dispatch:** detect, run sync methods via `asyncio.to_thread` (which already runs callables under `Context.run` internally — contextvars propagation is automatic, no extra ceremony). `serve(thread_pool_size=...)` installs a custom `ThreadPoolExecutor` via `loop.set_default_executor` before first dispatch.
2. **Wire types:** Pydantic 2 BaseModel; `extra='forbid'` default in all environments. Forward-compat for rolling spec upgrades is opt-in via `ADCP_FORWARD_COMPAT=permissive` or `serve(strict_wire_validation=False)`.
3. **`TaskHandoff` brand:** plain class with `__slots__ = ("_fn",)`; framework dispatches via type-identity (`type(obj) is TaskHandoff`). No `WeakValueDictionary`, no module-private storage. Subclasses are explicitly unsupported and rejected at dispatch.
4. **Task registry:** `TaskRegistry` Protocol with two impls in v6.0 — in-memory (dev/tests) and SQLAlchemy (salesagent + adopters with existing SQLA stack). `AsyncpgTaskRegistry` deferred to v6.1; the Protocol shape supports it additively when a greenfield adopter asks. Migrations ship under a separate Alembic version table (`adcp_alembic`); composite PK `(account_id, task_id)` with `task_id TEXT` enforces tenant isolation; status validated at the Python boundary, not via DB CHECK.
5. **Status-change bus:** server-scoped only; `ctx.publish_status_change(event)` inside handlers, `server.status_change.publish(event)` for code that holds the server, `await registry.publish_status_change(tenant_id, event)` for cross-tenant non-handler code. The registry helper validates `event.account_id` belongs to `tenant_id` before forwarding (closes cross-tenant leak). Two impls ship: `InMemoryStatusChangeBus` (default, dev) and `DbBackedStatusChangeBus` (production, audit-relevant). Subscriber list is thread-safe.
6. **Webhook delivery:** SSRF validator (port allowlist `{443, 8443}`, DNS-resolved-range rejection at validate time, IPv6 zone-ID handling) + pin-and-bind delivery (`PinnedTransport` preserves TLS SNI on host rewrite, all `getaddrinfo` answers checked, redirects rejected). Operator override `serve(webhook_client=...)` requires a `WebhookTransport` Protocol impl with `enforces_ssrf_at_connect=True` — naive `httpx.AsyncClient(proxies=...)` overrides fail server boot.
7. **Idempotency:** keying tuple is `(idempotency_key, account_id, tool_name, body_hash)` where `body_hash = sha256(canonical_json(arguments))` (RFC 8785 JCS). Same-key-different-body returns `INVALID_REQUEST`, not silent-replay. 7-day default retention; 4 MB payload cap; framework-shipped `vacuum_idempotency_keys()` adopters wire into their scheduler. Singleton-mode `Account.id` synthesizes per-principal so buyer-to-buyer leak via shared cache is closed.
8. **HTTP signatures (RFC 9421):** `http-message-signatures` (woodruffw). Required covered components locked: `("@method", "@target-uri", "@authority", "content-digest", "created", "expires")`. Max clock skew 60s. JWKS resolver is tenant-scoped — `(tenant_id, key_id) -> public_jwk`, rejecting keys outside the active tenant's JWKS.
9. **Webhook envelope:** matches `mcp-webhook-payload.json` including `operation_id` (buyer-supplied, echoed). MCP transport uses `WebhookPayload`; A2A transport uses native `Task` + `TaskStatusUpdateEvent` with the AdCP payload nested in `status.message.parts[].data`. Both ship in v6.0 — A2A delivery is not v6.1-deferred.
10. **Tenant health:** two orthogonal axes — `verification: 'pending' | 'healthy' | 'unverified' | 'failed'` and `operator_gate: 'enabled' | 'disabled'`. `resolve_by_host` returns `TenantResolution(tenant_id, config, server)` (named, not tuple-indexed) only when verification is healthy/unverified AND operator_gate is enabled.
11. **Packaging:** lands inside the existing `adcp` package at `adcp.decisioning.*`; package version bumps to v5.0.0 when DecisioningPlatform ships. `ADCP_VERSION` pinned in lockstep with `@adcp/client` (file at `src/adcp/ADCP_VERSION`). No separate `adcp-server` package — foundation primitives already live in `adcp.signing`, `adcp._idempotency`, `adcp.server`, `adcp.types`; the framework reuses them rather than building parallel implementations.
12. **Python versions:** 3.10 minimum; CI 3.10 / 3.11 / 3.12 / 3.13. PEP 696 TypeVar defaults via `typing_extensions>=4.12`. `TenantConfig` is a dataclass (not `TypedDict + Generic` — that combination is a runtime `TypeError` on 3.10).
13. **Type checking:** framework runs mypy strict + pyright strict on every PR; adopter expectations advisory. Public return types use named aliases (`MaybeAsync[T]`, `SalesResult[T]`) instead of inline four-way unions for coding-agent legibility.
14. **Spec consolidation (adcp#3392):** v6.0 ships hybrid handoff on `create_media_buy` + `sync_creatives` only (the two tools whose response schemas inline the `Submitted` arm). The other 4 HITL-eligible tools (`update_media_buy`, `build_creative`, `sync_catalogs`, `sync_event_sources`) surface lifecycle via `publish_status_change` in v6.0 with `update_media_buy` returning sync-success without the `status` field for in-flight re-approval (NOT `status='pending_approval'` — that value is not in `MediaBuyStatus`). adcp#3392 is a v6.1 release blocker; if spec consolidation stalls, v6.1 ships hybrid handoff via the `tasks/submit` task-router envelope projection, not deferred.
15. **Account resolution modes:** `'explicit' | 'from_auth' | 'singleton'` — renamed from v1's `'explicit' | 'implicit' | 'derived'` for clarity, in lockstep with TypeScript SDK PR #1005.

## Next moves

If the salesagent team and Python team accept this RFC:

**Audit existing primitives** (small fix-PRs against modules already in `adcp`):

1. **`adcp.signing.ip_pinned_transport` audit** — confirm SNI preservation on host rewrite, `follow_redirects=False`, port allowlist `{443, 8443}`, IPv6 zone-ID handling, all-`getaddrinfo`-answers SSRF check. Fix any gaps found.
2. **`adcp.signing.verifier` + `adcp.signing.jwks` audit** — confirm RFC 9421 covered-fields lock (`@method`, `@target-uri`, `@authority`, `content-digest`, `created`, `expires`), 60s skew bound, tenant-scoped `(tenant_id, key_id) -> jwk` resolver shape. Fix any gaps.
3. **`adcp._idempotency` + `adcp.server.idempotency/` audit** — confirm keying tuple is `(idempotency_key, account_id, tool_name, sha256(canonical_json(body)))`, not just `(key, account)`. Wire `tests/test_jcs_parity.py` against TS golden files. Fix any gaps.
4. **`adcp.types` audit** — confirm `BASE_CONFIG.extra='forbid'`; surface `ADCP_FORWARD_COMPAT=permissive` as the opt-in.
5. **`adcp.webhook_sender` audit** — confirm `WebhookPayload` includes `operation_id` (buyer-supplied, echoed). Fix if missing.
6. **`adcp.server.serve` audit** — confirm `factory=` kwarg supports per-tenant routing (the `TenantRegistry` use case). Add if missing.

**Add new layers** at `adcp.decisioning.*`:

7. `adcp.decisioning.types` — `TaskHandoff`, `RequestContext[TMeta]`, `Account[TMeta]`, `AccountStore` Protocol + `SingletonAccounts`/`ExplicitAccounts`/`FromAuthAccounts` reference impls, `MaybeAsync[T]` / `SalesResult[T]` aliases.
8. `adcp.decisioning.specialisms.*` — 12 `Protocol` classes (one PR per specialism — parallelizable across contributors once one lands as a template).
9. `adcp.decisioning.dispatch` — adapter seam from `Protocol` impls to existing `adcp.server.serve` handler shape; `asyncio.to_thread` sync dispatch with custom executor; `TaskHandoff` type-identity detection; `validate_platform()`.
10. `adcp.decisioning.task_registry` — `TaskRegistry` Protocol + `InMemoryTaskRegistry` + `SqlAlchemyTaskRegistry` (separate Alembic version table `adcp_alembic`). Reserve `AsyncpgTaskRegistry`; ship in v6.1.
11. `adcp.decisioning.status_changes` — `InMemoryStatusChangeBus` (default) + `DbBackedStatusChangeBus` (outbox-poll default; Postgres `LISTEN/NOTIFY` opt-in).
12. `adcp.decisioning.delivery` — `McpWebhookDelivery` (composes `adcp.webhook_sender`) + `A2aTaskDelivery` (composes `adcp.server.a2a_server`).
13. `adcp.decisioning.tenant_registry` — multi-tenant primitive with `verification` + `operator_gate` orthogonal axes; `TenantResolution` named return.
14. `adcp.decisioning.testing` (`make_test_context`) + `adcp.decisioning.dev` (`JwksFixture`).
15. `examples/hello_seller.py` (gates on first `SalesPlatform` PR), then `mock_seller`, `broadcast_tv`, `identity_graph` worked examples.
16. SKILL document (`skills/build-decisioning-platform/SKILL.md`) mirroring TS SKILL, including operator runbook for `ADCP_FORWARD_COMPAT`.
17. `tests/test_wire_parity.py` against TS-produced golden files.
18. Open salesagent migration PR — Stage 1 first (handler-bag mode under the new framework, no per-tool migration), then Stages 2-4 incrementally.

Track progress at `adcontextprotocol/adcp-client-python` — RFC adoption tracker + sub-issues per audit and per layer. Audit issues fire first (#1-6) since their findings shape the layers built on top.
