# RFC: Python port of DecisioningPlatform (v6.0) — v2

## Status

**Proposed** — open for review by the AdCP Python team and the salesagent team.

This RFC supersedes [`decisioning-platform-python-port.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-python-port.md) (v1). v1 was written before the round-2 hybrid-seller pivot and the round-3 `AdcpError` raise-path refactor; large parts of its surface (`AsyncOutcome[T]` discriminated union, `*Task` dual methods) are no longer the canonical TypeScript shape and shouldn't be ported. This v2 reflects what the TypeScript SDK actually ships on `bokelley/decisioning-platform-v1-scaffold` (PR #1005) after rounds 1-7 of expert review, plus salesagent-side feedback on Python ergonomics and operational reality.

## Background

The TypeScript scaffold at [`src/lib/server/decisioning/`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/) is the canonical surface. The Python port targets two adopter groups:

1. **The salesagent server** ([`adcontextprotocol/salesagent`](https://github.com/adcontextprotocol/salesagent)) — Flask + SQLAlchemy + Pydantic 2. Today it's a thin tool-decorator over per-adapter classes (GAM, Kevel, scope3 wrappers); idempotency, signing, sandbox, and status-change are hand-rolled per tool. The unified hybrid shape collapses 14 method names into 7, and the framework absorbs the cross-cutting concerns.
2. **Single-tenant Python adopters** (Innovid training-agent class, signals providers, retail-media networks). These run one platform impl, often with `'singleton'` account resolution; the framework's tenant-scoped invariants still apply.

The v6.0 framework owns wire mapping, account resolution, async tasks, idempotency, RFC 9421 signing, schema validation, sandbox routing, status-change projection, and lifecycle observability. Adopters describe their platform once via per-specialism `Protocol` classes; the framework does the rest.

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

1. **Wire-shape parity** with the TypeScript SDK at the AdCP wire version (`schemas/cache/3.0.0/`). A buyer's MCP/A2A request that succeeds against `@adcp/client` must succeed against `adcp-server` with the same response payload, modulo serialization order. Verified by a wire-parity test suite (see § *Validation matrix*).
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
# datamodel-code-generator. Adopters import from adcp_server.types.
from adcp_server.types import (
    GetProductsRequest, GetProductsResponse,
    CreateMediaBuyRequest, CreateMediaBuySuccess,
    UpdateMediaBuyRequest, UpdateMediaBuySuccess,
    GetMediaBuyDeliveryRequest, GetMediaBuyDeliveryResponse,
    CreativeAsset,
)
from adcp_server.async_outcome import TaskHandoff
from adcp_server.context import RequestContext

TMeta = TypeVar("TMeta", default=dict)  # PEP 696 default; falls back to TypeVar without default on 3.10

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
    ) -> (
        Awaitable[CreateMediaBuySuccess | TaskHandoff[CreateMediaBuySuccess]]
        | CreateMediaBuySuccess
        | TaskHandoff[CreateMediaBuySuccess]
    ):
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
    ) -> Awaitable[UpdateMediaBuySuccess] | UpdateMediaBuySuccess:
        ...

    def sync_creatives(
        self,
        creatives: list[CreativeAsset],
        ctx: RequestContext[TMeta],
    ) -> (
        Awaitable[list[SyncCreativesRow] | TaskHandoff[list[SyncCreativesRow]]]
        | list[SyncCreativesRow]
        | TaskHandoff[list[SyncCreativesRow]]
    ):
        """Unified hybrid for creative review. Mixed approved/pending
        rows in a single sync response, OR hand off the whole batch to
        background S&P review."""
        ...

    def get_media_buy_delivery(
        self,
        filter: GetMediaBuyDeliveryRequest,
        ctx: RequestContext[TMeta],
    ) -> Awaitable[GetMediaBuyDeliveryResponse] | GetMediaBuyDeliveryResponse:
        ...

    # Optional methods — present-or-absent; framework detects via hasattr.
    # These are the four canonical sales tools that v6.0 added in rc.1
    # for retail-media + financials adopters.

    def get_media_buys(self, ...) -> ...: ...
    def provide_performance_feedback(self, ...) -> ...: ...
    def list_creative_formats(self, ...) -> ...: ...
    def list_creatives(self, ...) -> ...: ...

    # sales-catalog-driven / sales-retail-media specialism methods:
    def sync_catalogs(self, ...) -> ...: ...
    def log_event(self, ...) -> ...: ...
    def sync_event_sources(self, ...) -> ...: ...
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
# adcp_server/async_outcome.py
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
# adcp_server/context.py
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
    return await asyncio.to_thread(method, *args, **kwargs)
```

This matters because Flask salesagent is synchronous (sync DB drivers, sync request bodies). Forcing it to migrate to async-everywhere is a large rewrite that doesn't gate on this feature. FastAPI adopters get native async; both work in the same framework.

**`contextvars` propagation.** `asyncio.to_thread` does NOT propagate `contextvars` by default (it copies the current context but mutations in the thread don't propagate back). The framework wraps with `contextvars.copy_context().run(method, *args, **kwargs)` so request-scoped state (the active span, request id, tenant id) is visible inside sync handlers. Mutations to `ContextVar` objects inside a sync handler stay scoped to the thread, which matches what observability libraries expect.

**Tradeoffs:**

- **Concurrency model shifts under sync adopters.** Each sync handler costs a thread + GIL hops. For salesagent's adapter calls (mostly outbound HTTP via `requests`) the GIL releases during I/O; the practical concurrency ceiling is the thread-pool size. Default is `min(32, os.cpu_count() + 4)` per Python 3.13 — adjust via `serve(thread_pool_size=...)` for high-fanout deployments.
- **Static analysis of "did the adopter forget to await something" doesn't apply** when the sync method touches an async dependency. `mypy --strict` won't catch a missing `await` in a sync method calling an async-returning helper. Adopters who care opt into async-everywhere; adopters who don't accept the runtime cost.

**Status-change publishing inside `def`-methods:** `server.status_change.publish(event)` is sync (in-memory bus), so it works in both sync and async methods. `ctx.handoff_to_task(async_fn)` requires the handoff function itself to be async (the framework awaits it in a background task), but the method that returns the handoff can be sync.

### Error model (`AdcpError`)

```python
# adcp_server/errors.py
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

**Extra-field policy is environment-driven** to match salesagent's existing pattern:

```python
# adcp_server/types/_config.py
import os

def _default_extra() -> Literal['ignore', 'forbid']:
    """Production: ignore unknown fields (forward-compat with newer spec
    versions on the wire). Dev/CI: forbid (catch typos and stale schema)."""
    env = os.environ.get('ADCP_ENV', 'dev').lower()
    return 'ignore' if env == 'production' else 'forbid'

BASE_CONFIG = ConfigDict(
    extra=_default_extra(),
    populate_by_name=True,
    str_strip_whitespace=True,
)
```

Adopters can override per-model with `model_config = ConfigDict(...)`. The framework's wire types are versioned to a specific `ADCP_VERSION`; spec evolution within a major bumps the package version, not the wire types.

**Performance note:** Pydantic v2's Rust-backed validator is fast (~1µs per typical model). The per-request validation cost is negligible compared to the network and database round-trips dispatch already pays. No micro-optimization needed.

### Status-change bus

```python
# adcp_server/status_changes.py
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
    def __init__(self):
        self._subscribers: list[Callable[[StatusChangeEvent], None]] = []

    def publish(self, event: StatusChangeEvent) -> None:
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception as e:
                # Swallow — subscriber crashes must not break dispatch
                _logger.warning("status-change subscriber raised: %s", e)

    def subscribe(self, fn: Callable[[StatusChangeEvent], None]) -> Callable[[], None]:
        self._subscribers.append(fn)
        return lambda: self._subscribers.remove(fn)
```

Each `DecisioningAdcpServer` owns a `StatusChangeBus` accessible as `server.status_change`. There is **no module-level singleton** — earlier drafts shipped one and surfaced cross-test contamination + multi-server-in-process bugs that couldn't be cleanly isolated. Killing the global removes the bug class.

**Inside handlers:** call `ctx.publish_status_change(event)` — the framework wires the per-server bus through `ctx`. No imports needed.

**Outside handlers** (cron jobs, webhook receivers, queue workers): hold a `DecisioningAdcpServer` reference and call `server.status_change.publish(event)`. This is the same pattern as holding a DB session or any other long-lived dependency.

**For multi-tenant deployments** under `TenantRegistry`, the registry exposes a tenant-scoped helper that looks up the right server's bus:

```python
class TenantRegistry:
    def publish_status_change(self, tenant_id: str, event: StatusChangeEvent) -> None:
        result = self._tenants.get(tenant_id)
        if not result or result.health.status == 'disabled':
            _logger.warning("publish_status_change for unknown/disabled tenant %s", tenant_id)
            return
        result.server.status_change.publish(event)
```

Cron code that handles many tenants holds the `TenantRegistry` and calls `registry.publish_status_change(tenant_id, event)`. Cron code that's tenant-bound holds the `server` directly.

**Tenant scoping:** every event carries `account_id`; subscribers filter by tenant. The framework's MCP Resources subscription projector (rc.1+) reads from each server's bus independently — no fan-in across servers, because cross-server fan-in was the exact bug the module-level singleton caused.

### Idempotency

Framework persists response per `(idempotency_key, account_id)` and replays on duplicate keys. Persistence shape:

```sql
CREATE TABLE adcp_idempotency_keys (
    idempotency_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    response_payload JSONB NOT NULL,
    response_status SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (idempotency_key, account_id)
);
CREATE INDEX adcp_idempotency_keys_expires_at ON adcp_idempotency_keys (expires_at);
```

Framework reads `(idempotency_key, account.id)` at the top of dispatch; if hit and not expired, returns the cached response. Otherwise dispatches the method, captures the response, writes the key with `expires_at = now() + retention_ttl`.

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
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

def signed_request_verifier(public_key_resolver):
    """Returns a callable that verifies incoming RFC 9421 signatures and
    populates ctx.auth_info with the resolved principal."""
    verifier = HTTPMessageVerifier(
        signature_algorithm=...,
        key_resolver=public_key_resolver,
    )
    def verify(request) -> AuthInfo:
        result = verifier.verify(request)
        return AuthInfo(
            kind='signed_request',
            key_id=result.key_id,
            principal=result.label,
            scopes=result.metadata.get('scopes', []),
        )
    return verify

# Adopter wiring:
serve(
    create_adcp_server_from_platform(seller, ...),
    authenticate=signed_request_verifier(public_key_resolver=load_jwks),
)
```

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

def validate_push_notification_url(url: str) -> None:
    """Reject SSRF surfaces. Raises AdcpError(INVALID_REQUEST) for any
    of: non-https scheme (test/dev override via env),
    bare 'localhost'/'0', RFC 1918 (10/8, 172.16/12, 192.168/16),
    loopback (127/8, ::1), link-local (169.254/16 incl. AWS metadata,
    fe80::/10), CGNAT (100.64/10), IPv6 unique-local (fc00::/7),
    multicast/reserved, IPv4-mapped IPv6, bracketed IPv6 hosts.
    """
    parsed = urlparse(url)
    if parsed.scheme != 'https' and not _allow_http_test_override():
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'scheme {parsed.scheme!r} not allowed; must be https')
    host = parsed.hostname or ''
    # Strip IPv6 brackets
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    # IPv4-mapped IPv6: ::ffff:127.0.0.1 → recurse on dotted-quad
    if host.lower().startswith('::ffff:'):
        validate_push_notification_url(url.replace(host, host.split(':')[-1]))
        return
    if host in ('localhost', '0'):
        raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                        message=f'host {host!r} not allowed')
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_link_local or ip.is_private \
                or ip.is_multicast or ip.is_reserved \
                or ip in ipaddress.ip_network('100.64.0.0/10'):
            raise AdcpError('INVALID_REQUEST', field='push_notification_config.url',
                            message=f'host {host!r} resolves to disallowed range')
    except ValueError:
        # Hostname; DNS rebinding mitigated via pin-and-bind delivery — see below
        pass

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

**DNS rebinding: pin-and-bind ships in v6.0.** The validator inspects the literal hostname, but a buyer can register `https://rebind.attacker.com/` with a TTL-0 A-record that returns `8.8.8.8` at validate time and `127.0.0.1` at fetch time. v6.0 ships pin-and-bind delivery via a custom `httpx.AsyncHTTPTransport` so the IP resolved at validation time is the IP the framework connects to:

```python
# adcp_server/webhooks/_pin_and_bind.py
import socket
import ipaddress
import httpx

def create_pin_and_bind_session() -> httpx.AsyncClient:
    """httpx client that resolves each host once at request time, applies
    the same SSRF-range checks against the resolved IP, and connects to
    that exact IP — mitigating DNS rebinding between validation and
    delivery."""

    class PinnedTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            host = request.url.host
            try:
                # Resolve once; apply SSRF range checks against the resolved IP
                addr_info = await asyncio.get_event_loop().getaddrinfo(
                    host, request.url.port or 443, type=socket.SOCK_STREAM)
                ip = addr_info[0][4][0]
                _check_ip_against_ssrf_ranges(ip)  # raises if disallowed
                # Rewrite request to use resolved IP, preserve Host header
                request.url = request.url.copy_with(host=ip)
                request.headers['Host'] = host
            except Exception as e:
                raise httpx.RequestError(f'pin-and-bind rejected {host}: {e}')
            return await super().handle_async_request(request)

    return httpx.AsyncClient(transport=PinnedTransport(verify=True))
```

The framework's webhook emitter uses this client by default. Operators can override via `serve(webhook_client=custom_httpx_client)` if they have a different egress shape (proxy with allowlist, mTLS to a known set of buyers, etc.) but the secure default is on.

**Webhook envelope** matches `mcp-webhook-payload.json`:

```python
class WebhookPayload(BaseModel):
    idempotency_key: str  # UUID v4, framework-generated
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

Webhook delivery is gated to spec-listed task types (closed enum at AdCP 3.0 GA); the framework skips webhook emission with an explanatory log for tools outside the enum and uses `server.status_change.publish(event)` instead.

### Task registry

Framework-owned. Three implementations ship with v6.0; adopters pick the one matching their stack.

```python
# adcp_server/task_registry.py
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
| `SqlAlchemyTaskRegistry(engine)` | Adopters with an existing SQLAlchemy stack (salesagent, Innovid) | Composes with the adopter's connection pool, Alembic migrations, transaction boundaries. Ships migration scripts as Alembic revisions adopters merge into their migration tree. |
| `AsyncpgTaskRegistry(pool)` | **Deferred to v6.1.** Slots in additively when a greenfield async-everywhere adopter asks for it; the Protocol shape doesn't change. | Reserved name; not shipped in v6.0. |

Salesagent wires `SqlAlchemyTaskRegistry` so the registry shares the existing connection pool, gets backed up by the same retention/replication story as the rest of salesagent's data, and migrations land via Alembic alongside everything else.

```python
# Salesagent wiring:
from adcp_server.task_registry import SqlAlchemyTaskRegistry
from salesagent.db import engine

serve(
    create_adcp_server_from_platform(seller, task_registry=SqlAlchemyTaskRegistry(engine)),
    ...,
)
```

**Schema (mirrors TS Postgres migration):**

```sql
CREATE TABLE adcp_decisioning_tasks (
    task_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'submitted', 'working', 'input-required',
        'completed', 'canceled', 'failed', 'rejected',
        'auth-required', 'unknown'
    )),
    progress JSONB,
    result JSONB,
    error JSONB,
    has_webhook BOOLEAN NOT NULL DEFAULT FALSE,
    push_notification_url TEXT,
    push_notification_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX adcp_decisioning_tasks_account_id ON adcp_decisioning_tasks (account_id);
```

Framework writes `submitted` on task creation, `working` on first `update(progress)`, `completed` / `failed` on terminal. Other 5 enum values reserved for adopter-emitted transitions via the v6.1 `task_registry.transition()` API.

**Result/error JSONB cap at 4 MB** — same as TypeScript; adopter handoff functions returning oversized payloads are rejected at registry boundary before OOMing the Python process or the database.

**Multi-tenant deployments** sharing one registry: namespace task IDs as `tenant_{tenant_id}_{account_id}_{uuid}` so cross-tenant `get(task_id)` probes return null even when the same UUID was minted for multiple tenants. Defense in depth on top of the framework's tenant-scoped `get(task_id, account_id)` enforcement.

**Why a Protocol + multiple impls instead of asyncpg-only.** Earlier drafts of this RFC defaulted to asyncpg with a "salesagent runs SQLAlchemy elsewhere, separating concerns" rationale. That reasoning was inverted: forcing asyncpg means salesagent runs two connection pools, two migration systems (Alembic + framework DDL), two transaction boundaries that can't share a unit-of-work. The 2-3x perf delta on raw inserts doesn't matter when the registry hot path is bounded by HTTP latency to deliver webhooks. Adopter picks the impl that composes with their stack.

### Tenant registry

Multi-tenant primitive — same surface as TS [`tenant-registry.ts`](https://github.com/adcontextprotocol/adcp-client/blob/main/src/lib/server/decisioning/tenant-registry.ts):

```python
from typing import Literal, Callable, Awaitable

class TenantConfig(TypedDict, Generic[TPlatform]):
    agent_url: str  # https://acme.example.com or https://example.com/acme
    signing_key: TenantSigningKey
    platform: TPlatform
    label: str | None = None
    server_options: ServerOptions | None = None

class TenantSigningKey(TypedDict):
    key_id: str
    public_jwk: dict  # JWKS public-key shape
    private_jwk: dict  # JWKS private-key shape

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

    def resolve_by_host(
        self, host: str
    ) -> tuple[str, TenantConfig, DecisioningAdcpServer] | None:
        """Subdomain routing — convenience wrapper for resolve_by_request(host, '/')."""
        ...

    def resolve_by_request(
        self, host: str, pathname: str
    ) -> tuple[str, TenantConfig, DecisioningAdcpServer] | None:
        """Path-based routing — matches host AND longest-path-prefix.
        Strips query strings and fragments before matching."""
        ...

    def publish_status_change(
        self, tenant_id: str, event: StatusChangeEvent
    ) -> None:
        """Tenant-scoped status-change publish for cross-tenant non-handler
        code (cron, queue workers). Looks up the tenant's server bus and
        dispatches; logs + drops on unknown/disabled tenant."""
        ...

    # ... unregister, recheck, list_tenants
```

**Health states:** `'pending'` (just registered, awaiting first JWKS validation), `'healthy'` (validation succeeded), `'unverified'` (was healthy, hit a transient recheck failure — still serves for graceful degradation), `'disabled'` (validation failed permanently).

**JWKS race window:** `register()` lands tenants in `'pending'`; `resolve_by_host` returns null for `'pending'`; host transport responds 503 + Retry-After until first validation succeeds. Earlier drafts dropped tenants in `'unverified'` (serve immediately, validate later), which served signed responses no buyer could verify for ~60s. The `'pending'` gate closes the race.

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
    factory=lambda ctx: registry.resolve_by_host(ctx.host)[2],  # the server
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

## Migration paths

### From salesagent (Flask + per-adapter classes)

**Realistic scope.** Full salesagent migration is calendar-months of engineering, not weeks. The migration touches every tool dispatch path, the tenant model, the audit-log integration, OAuth callbacks, and the existing per-adapter abstraction layer. The plan below is staged so each stage ships independently — the merge seam in `serve()` accepts v5-style handler entries alongside v6 platforms, so half-migrated states are deployable.

**Stage 1 — Foundation (1-2 weeks).** Install `adcp-server`, wire the auth boundary, register a single tenant.

```python
from adcp_server import serve, create_adcp_server_from_platform
from adcp_server.task_registry import SqlAlchemyTaskRegistry
from salesagent.db import engine

# At this stage, server still routes everything through the existing
# v5 handler bag — no platforms yet. Just gets the framework alongside
# the existing app for a known-quiet tool.
v5_handlers = load_v5_handlers()

server = create_adcp_server_from_platform(
    platform=None,  # all-handler mode
    handlers=v5_handlers,
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
serve(factory=lambda ctx: registry.resolve_by_host(ctx.host)[2], ...)
```

**Stage 4 — Cleanup (1-2 weeks).** Delete superseded code:

- Hand-rolled idempotency middleware (framework owns it)
- Hand-rolled signature verifier (framework owns it via `authenticate=...`)
- Hand-rolled sandbox routing (framework owns it via `Account.metadata.sandbox`)
- Hand-rolled status-change emitter (replaced with `ctx.publish_status_change(event)` or `server.status_change.publish(event)`)
- The `@tool` decorator and its registration table
- Per-tool Flask routes that wrapped handler invocations

**Things the migration does NOT eliminate.** Salesagent has many consumers of `g.tenant` outside the AdCP wire surface — admin UI, audit logs, OAuth callbacks, internal cron jobs. These don't go away. The migration adds a `g.tenant`-shim that reads from `ctx.account.metadata` for AdCP request handlers; non-AdCP routes continue using Flask's request context as before.

**Estimated total effort:** 2-3 months calendar time for the full salesagent. Smaller adopters with fewer tools and a single tenant can finish in 2-3 weeks.

### From Innovid training-agent

Single-tenant agent. `'singleton'` resolution returns a synthetic singleton account; the framework's tenant-scoped invariants (idempotency, status-change `account_id`, workflow steps) all work without forcing the adopter to model multi-tenancy:

```python
class TrainingAgentAccounts:
    resolution = 'singleton'

    def resolve(self, ref, ctx=None):
        # Singleton — ignore ref, always return the one account
        return Account(
            id='training-agent',
            name='Innovid Training Agent',
            status='active',
            metadata={'kind': 'training_agent'},
            auth_info={'kind': 'derived'},
        )

class TrainingAgentSeller(SalesPlatform):
    accounts = TrainingAgentAccounts()
    # ... single platform, no per-tenant lookup
```

See [`docs/proposals/decisioning-platform-training-agent-migration.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/decisioning-platform-training-agent-migration.md) for the full migration plan.

### From scratch (new adopter)

Three-step intro mirrors the TS SKILL:

```python
# 1. Declare capabilities
class MySellerSeller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=['sales-non-guaranteed'],
        creative_agents=[CreativeAgent(agent_url='https://creative.example.com/mcp')],
        channels=['display', 'olv'],
        pricing_models=['cpm'],
        config={...},
    )

    accounts = MyAccounts(resolution='singleton')

    sales = SalesPlatform(...)  # impl below

# 2. Implement specialism methods
class MySalesPlatform:
    def get_products(self, req, ctx): ...
    def create_media_buy(self, req, ctx): ...
    def update_media_buy(self, mb_id, patch, ctx): ...
    def sync_creatives(self, creatives, ctx): ...
    def get_media_buy_delivery(self, filter, ctx): ...

# 3. Serve
if __name__ == '__main__':
    seller = MySellerSeller()
    serve(create_adcp_server_from_platform(seller, name='my-seller', version='0.0.1'))
```

The Python SKILL ships a single canonical example mirroring [`skills/build-decisioning-platform/SKILL.md`](https://github.com/adcontextprotocol/adcp-client/blob/main/skills/build-decisioning-platform/SKILL.md) — same fields, same error codes, same migration sketch.

## Open questions

These need decisions before the Python port lands `rc.1`:

### 1. Async-vs-sync method dispatch

Should the framework **detect** sync/async at dispatch time (`inspect.iscoroutinefunction`) and run sync methods on `asyncio.to_thread`, or **force** adopters to write async-everywhere?

- **Detect + thread-pool** — easier migration for sync codebases (Flask salesagent), correct behavior under load (no event-loop blocking), but loses `mypy --strict` "did you forget an `await`" check inside sync methods that touch async I/O. Concurrency ceiling shifts to thread-pool size.
- **Force async** — cleaner type story, but forces salesagent to migrate to `asgiref.sync.async_to_sync` shims everywhere a sync DB driver is touched, which is invasive.

**RFC recommendation: detect + thread-pool, with `contextvars.copy_context()` propagation.** The migration cost of forced async is too high; the type-checker gap is real but bounded; the thread-pool cost is bounded by the configurable pool size.

### 2. Pydantic 2 — extra policy default

Wire types are Pydantic 2 `BaseModel`. The remaining question is `extra` policy default:

- **`'ignore'` always** — forward-compat with newer spec versions, but masks typos in adopter-supplied payloads
- **`'forbid'` always** — catches typos in dev, breaks production rolling upgrades when buyers send fields from a newer spec
- **Env-driven** (production: `'ignore'`, dev: `'forbid'`) — matches salesagent's existing pattern; opt-out per-model possible

**RFC recommendation: env-driven default**, mirroring salesagent. The framework reads `ADCP_ENV` (with `'dev'` as the safe-default fallback). Adopters override per-model via `model_config = ConfigDict(...)` for sensitive fields where strict validation is required regardless of environment.

### 3. Library naming + packaging cadence

Two naming schemes:

- `pip install adcp-server` — short, idiomatic Python, no namespacing
- `pip install @adcp/python-server` — matches TypeScript scope, but `@scope/name` packages don't render naturally on PyPI

**RFC recommendation: `adcp-server`** on PyPI; document the scope correspondence in the README.

**Version pinning:** the Python SDK ships its own version independent of the TypeScript SDK, but both pin to the same `ADCP_VERSION` (currently `3.0.0`). When AdCP 3.1 ships, both SDKs cut new majors that bump `ADCP_VERSION`; adopters who pin `adcp-server>=3.0,<4.0` and `@adcp/client@>=3.0.0 <4.0.0` get the same wire surface.

### 4. CI matrix — Python 3.10 / 3.11 / 3.12 / 3.13?

**RFC recommendation: 3.10 minimum** (PEP 604 union syntax `int | str`, `match` statement). Drop 3.9 — it's EoL October 2025; the salesagent is already on 3.11. Test 3.10, 3.11, 3.12, 3.13.

PEP 696 (`TypeVar` defaults — `TMeta = TypeVar("TMeta", default=dict)`) needs 3.13 for runtime support; on 3.10-3.12 we ship via `typing_extensions.TypeVar`.

### 5. Type-checker support — mypy strict, pyright strict, both?

**RFC recommendation: framework runs mypy strict + pyright strict on every PR; adopter expectations are advisory.** Pydantic v2's mypy plugin generates noise under `--strict` (especially `Self` return-type propagation through inheritance) that adopters shouldn't have to absorb. The framework SHOULD type-check clean under both; the SKILL's example and the migration template SHOULD type-check clean under non-strict; adopter codebases set their own bar.

### 6. Submitted-arm spec consolidation (adcp#3392) — port wait or land alongside?

Currently TypeScript SDK ships hybrid handoff only for the two tools whose per-tool `xxx-response.json` schema includes the `Submitted` arm (`create_media_buy`, `sync_creatives`). The other 4 HITL-eligible tools (`update_media_buy`, `build_creative`, `sync_catalogs`, `get_products`) have inconsistent spec response schemas — `Submitted` is in `async-response-data.json` only.

[adcp#3392](https://github.com/adcontextprotocol/adcp/issues/3392) proposes spec consolidation so all 6 tools have rolled-in `Submitted` arms. When that lands, the SDK adds hybrid-handoff support for the other 4.

**RFC recommendation: Python port lands the same shape as TypeScript** — only `create_media_buy` + `sync_creatives` have hybrid handoff support in v6.0; the other 4 tools surface long-running state via `server.status_change.publish(event)` until adcp#3392 lands.

**Adopter impact.** Salesagent's `update_media_buy` re-approval flow needs HITL today. Until #3392 lands, the workaround is: return `UpdateMediaBuySuccess` synchronously with `status='pending_approval'`, then drive the lifecycle via `publish_status_change` + buyer-side polling on `getMediaBuy`. Less ergonomic than a `TaskHandoff`, but functional.

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
| `test/server-decisioning-postgres-task-registry.test.js` | `tests/test_postgres_task_registry.py` | 9-value status enum; `progress` JSONB transitions; 4 MB result/error cap; `has_webhook` field; tenant-prefix namespacing. Runs against the two v6.0 impls (in-memory, SqlAlchemy) via parameterized fixture; asyncpg fixture added when impl ships in v6.1. |
| `test/server-decisioning-task-webhooks.test.js` | `tests/test_webhooks.py` | RFC 9421 signed delivery; SSRF guard rejections (50+ surfaces); pin-and-bind defeats DNS rebinding (validate-time IP ≠ delivery-time IP scenario); failed-task error envelope; `task_type` closed-enum gate; idempotency-key UUIDv4 |
| `test/server-decisioning-status-changes.test.js` | `tests/test_status_changes.py` | Per-server bus isolation; cross-server publish does NOT leak (regression test for the deleted module-level singleton); 10-value resource-type enum + `'x-'` forward-compat; tenant scoping via `TenantRegistry.publish_status_change` |
| `test/server-decisioning-validate-platform.test.js` | `tests/test_validate_platform.py` | "Claimed X; missing Y" diagnostic; specialism-method coverage matrix; runtime check at server boot |
| `test/server-decisioning-idempotency.test.js` | `tests/test_idempotency.py` | Replay on duplicate `(key, account_id)`; expiry honors `expires_at`; payload cap rejection; vacuum cleanup deletes expired rows |
| (new) | `tests/test_wire_parity.py` | TS-produced golden files load + deep-equal against Python responses for the same logical inputs. Field ordering, null-vs-omitted, datetime serialization (ISO 8601 with `Z` suffix), float precision. Catches drift between ports before it reaches buyers. |

## Decision summary

1. **Async-or-sync method dispatch:** detect, run sync methods via `asyncio.to_thread` with `contextvars.copy_context()` propagation.
2. **Wire types:** Pydantic 2 BaseModel; env-driven `extra` policy default (production: `'ignore'`, dev: `'forbid'`).
3. **`TaskHandoff` brand:** plain class with `__slots__ = ("_fn",)`; framework dispatches via type-identity (`type(obj) is TaskHandoff`). No `WeakValueDictionary`, no module-private storage.
4. **Task registry:** `TaskRegistry` Protocol with two impls in v6.0 — in-memory (dev/tests) and SQLAlchemy (salesagent + adopters with existing SQLA stack). `AsyncpgTaskRegistry` deferred to v6.1; the Protocol shape supports it additively when a greenfield adopter asks.
5. **Status-change bus:** server-scoped only; `ctx.publish_status_change(event)` inside handlers, `server.status_change.publish(event)` for code that holds the server, `TenantRegistry.publish_status_change(tenant_id, event)` for cross-tenant non-handler code. No module-level singleton.
6. **Webhook delivery:** SSRF validator + pin-and-bind delivery in v6.0 (not v6.1); DNS rebinding mitigated at request time, not just validation time.
7. **Idempotency:** 7-day default retention with configurable TTL; 4 MB payload cap; framework-shipped `vacuum_idempotency_keys()` cleanup function adopters wire into their scheduler.
8. **HTTP signatures:** `http-message-signatures` (woodruffw).
9. **Library name:** `adcp-server` on PyPI.
10. **Python versions:** 3.10 minimum; CI 3.10 / 3.11 / 3.12 / 3.13.
11. **Type checking:** framework runs mypy strict + pyright strict on every PR; adopter expectations advisory.
12. **Spec consolidation (adcp#3392):** Python ships same shape as TypeScript; hybrid handoff for `create_media_buy` + `sync_creatives` only in v6.0; other 4 HITL tools surface via `publish_status_change` until consolidation lands.
13. **Account resolution modes:** `'explicit' | 'from_auth' | 'singleton'` (renamed from v1's `'explicit' | 'implicit' | 'derived'` for clarity).

## Next moves

If the salesagent team and Python team accept this RFC:

1. Create `adcontextprotocol/adcp-python-server` repo with the SKILL, generated types, and core framework primitives (`AdcpError`, `TaskHandoff`, `RequestContext`, observability hooks).
2. Port `validate_platform()` + the 12 specialism `Protocol` classes.
3. Port `tenant_registry` + JWKS validator.
4. Port `task_registry` Protocol + the two v6.0 impls (in-memory, SQLAlchemy). Reserve the `AsyncpgTaskRegistry` name; ship in v6.1 when an adopter needs it.
5. Port the `mock-seller`, `broadcast-tv`, and `identity-graph` worked examples — same shape as TypeScript, idiomatic Python.
6. Wire `serve(authenticate=signed_request_verifier(...), webhooks=...)` with pin-and-bind delivery default.
7. Open the salesagent migration PR — convert one adapter end-to-end as a proof point.
8. CI parity — ensure the Python `tests/test_*.py` matrix above passes against the same `schemas/cache/3.0.0/` cache the TypeScript SDK uses, including the new `tests/test_wire_parity.py` against TS-produced golden files.

Track progress at `adcontextprotocol/adcp-client-python` — RFC adoption tracker + sub-issues per locked decision.
