# DecisioningPlatform dispatch design (post-review)

Pre-implementation reference for the `adcp.decisioning.{handler, dispatch,
serve, task_registry}` modules. Synthesizes 8 reviewer passes:

* **Round 1** (initial design): agentic-product-architect, python-expert
* **Round 2** (post-codegen-and-framing additions): agentic-product-architect
  (framing), python-expert (codegen mechanics), dx-expert (handler
  registration UX), code-reviewer (consistency)
* **Round 3** (post-design-doc-published, on PR #316): user feedback on
  Account.id leak boundary, cross-tenant probe regression, validation
  noise, codegen DX, executor configurability, field-name semantics,
  example coverage, kwarg unpacking
* **Round 4** (cross-language: TS team review of the parallel `@adcp/client`
  port + the TS team's "Python port v2" RFC + Yahoo's ask for typed
  metadata threading): RequestContext typed sub-readers (`state` /
  `resolve`), validate_platform tightening, `AdcpError` projection
  consistency, ErrorCode codegen, in-memory task gate, per-server
  status-change bus, examples-import lint

Authoritative through D15. Tracks "things deferred" for v6.1 and beyond.

## Decisions

### D1. Explicit `PlatformHandler` class — codegen from per-specialism Protocols

**Decision:** generate `src/adcp/decisioning/handler.py` from the
per-specialism Protocol classes (`SalesPlatform` and the future 11) via
`scripts/generate_decisioning_handler.py`. Don't hand-write; don't
synthesize at runtime via `type()`; don't read from `_HANDLER_TOOLS` as
the primary input.

**Rationale:** runtime synthesis breaks IDE go-to-definition, traceback
frames are unreadable, mypy types every shim as `Any`, pickling fails.
Hand-writing 25 typed shims is tedious and drifts when AdCP adds tools.
Codegen keeps the file regenerable and correct.

**Codegen source of truth: the per-specialism Protocols**, not
`_HANDLER_TOOLS`. The Protocols already encode exactly what codegen
needs:

* method name (`create_media_buy`)
* typed `req` annotation (`CreateMediaBuyRequest`)
* typed return (`SalesResult[CreateMediaBuySuccessResponse]` vs
  `MaybeAsync[GetProductsResponse]`)
* handoff-shape signal (`SalesResult[T]` means hybrid, `MaybeAsync[T]`
  means non-hybrid)

`_HANDLER_TOOLS` reduces to "the full tool list" and adds nothing —
it's consumed only for the `register_handler_tools(...)` call the
generated module emits at import time. Use
`typing.get_type_hints(SalesPlatform.create_media_buy, localns={...})`
per method, then `get_origin` / `get_args` to peel
`MaybeAsync` / `SalesResult` / `Awaitable` wrappers.

**Wire-shape ≠ Python-signature edge cases.** Some Protocol methods
take more than `(req, ctx)`. Example: `update_media_buy(media_buy_id,
patch, ctx)` is three positional args; the wire tool takes one JSON
object. **Codegen needs a per-method "arg-projection" lookup** for the
handful of tools where wire-shape differs from Python-method-shape.
Path: shim accepts `params: UpdateMediaBuyRequest`, dispatch helper
splits to `(media_buy_id, patch, ctx)` before calling the platform
method. Preserve the Protocol surface as adopters see it.

**Arg-projection MUST emit explicit kwargs, not positional**, so
adopters refactoring Protocol method signatures don't silently break
the shim. The codegen produces:

```python
# Generated arg-projection lookup — kwargs only
ARG_PROJECTION: dict[str, Callable[[BaseModel], dict[str, Any]]] = {
    "update_media_buy": lambda req: {
        "media_buy_id": req.media_buy_id,
        "patch": req,  # the full request minus media_buy_id, modeled per spec
    },
    # ... other arg-projecting tools
}

# Inside _invoke_platform_method:
projector = ARG_PROJECTION.get(method_name)
if projector is not None:
    method_kwargs = projector(params)
    method_kwargs["ctx"] = ctx
    result = await _call(method, **method_kwargs)
else:
    result = await _call(method, params, ctx)
```

If an adopter refactors `update_media_buy(self, media_buy_id, patch,
ctx)` to `(self, *, media_buy_id, patch, ctx)`, the kwargs path keeps
working; positional dispatch would silently break.

**Wire-name → Python-name mapping.** Add a `_WIRE_TO_PYTHON: dict[str,
str]` constant in the generator, default identity. Generator MUST fail
loudly if a wire name isn't a valid Python identifier (the `si_*`
namespace pattern is already non-uniform; this catches future drift).

**Shim return type: Success response only.** Drop the
`| dict[str, Any]` fallback. Wire-envelope projection (TaskHandoff →
`Submitted` envelope, AdcpError → structured-error envelope) happens
in dispatch AFTER the shim returns. The shim signature is
`-> CreateMediaBuySuccessResponse`, full stop. Cleaner public API,
better IDE completion, no defeated typing.

**Generator must fail-fast at codegen time** if a Protocol method
references a Pydantic Request type that doesn't exist in `adcp.types`.
Don't emit `params: Any` fallback — refuse to generate. CI regen-drift
catches contributor errors AFTER push; codegen-time fail-fast catches
them before commit.

**`get_adcp_capabilities` is a hand-templated special case** in the
codegen script — it's not a generic shim because it reads
`self._platform.capabilities` rather than delegating. Generated
alongside the generic shim template.

**`register_handler_tools` call** emitted at module level by codegen,
using `_HANDLER_TOOLS` as the source for the union of tool names
`PlatformHandler` covers (since it covers all specialisms).

**Generator output is `ruff format`'ed** post-emit (mirrors
`scripts/generate_registry_types.py:196`); also run `ruff check --fix`
for `from __future__ import annotations` ordering and unused-import
cleanup. Don't add to black-exclude — generated-but-formatted is
reviewer-friendly.

**Header comment is prescriptive**, not just timestamp:

```
# DO NOT EDIT — regenerated from scripts/generate_decisioning_handler.py
# Run `python scripts/sync_schemas.py` (or the explicit codegen step)
# after modifying _HANDLER_TOOLS, adcp.types, or any specialism Protocol.
# Source: src/adcp/decisioning/specialisms/{sales,...}.py
```

**Wire generator into the build pipeline AFTER `generate_types.py`**,
NOT inside `scripts/sync_schemas.py`. The `sync_schemas.py` script
only fetches the protocol bundle; `generate_types.py` produces the
Pydantic types the codegen depends on. Add the new step as
`scripts/generate_decisioning_handler.py`, called after Pydantic regen
in whatever invocation glue the project uses (`Makefile` /
pre-commit / etc.).

**CI regen-drift check** mirrors `tests/test_mcp_schema_drift.py`
(483-line precedent — reuse the regen-into-tempdir + textual-diff
helper). Diff-and-fail, NOT auto-write. Auto-write loses the explicit
commit signal. One combined check is fine — drift in any artifact is
equally a problem.

**Drift error message MUST be prescriptive.** A generic
`git diff --exit-code` failure forces every contributor to learn the
regen story from scratch. The pytest assertion message names the
exact regen command verbatim:

```
AssertionError: src/adcp/decisioning/handler.py is out of sync with the
per-specialism Protocols. Run:

    uv run python scripts/generate_decisioning_handler.py

then commit the result. Drift detected in: <list of changed methods>
```

Mirror the precedent at `tests/test_mcp_schema_drift.py` (which uses
the same prescriptive shape).

**Don't make `PlatformHandler` generic over `TMeta`.** Concrete base
typed as `DecisioningPlatform`; method bodies cast/narrow as needed.
Generic-over-`TMeta` complicates codegen for no DX win.

**Shim shape:**

```python
class PlatformHandler(ADCPHandler):
    def __init__(self, platform: DecisioningPlatform) -> None:
        super().__init__()
        self._platform = platform

    async def create_media_buy(
        self, params: CreateMediaBuyRequest, context: ToolContext | None = None,
    ) -> CreateMediaBuySuccessResponse | dict[str, Any]:
        return await _invoke_platform_method(
            self._platform, "create_media_buy", params, context,
        )
```

Per-method `params` is the typed Pydantic class (not `dict`) so the
framework's `create_tool_caller` path validates inbound JSON against the
typed model before the shim runs. Adopters who want the typed
`RequestContext[TMeta]` get it via `assert isinstance(context, RequestContext)`
inside their platform method body — the runtime check is cheap and
narrows for mypy on adopter side.

### D2. Context mutation, not replacement

**Decision:** mutate the existing `RequestContext` in place. Don't try to
swap context objects through `call_next`.

**Rationale:** the framework's `_dispatch_with_middleware`
(`serve.py:223-260`) closes over a single `context` and forwards it via
`call_next` which takes zero args. The framework explicitly rejects
context replacement (comment at `serve.py:111` "Middleware cannot mutate
what the next layer sees by mutating params"). The supported pattern —
seen in `helpers.py:268-336 resolve_account_into_context` — is in-place
mutation.

**Wiring:**

1. `adcp.decisioning.serve` passes `context_factory=lambda req_meta: RequestContext()`
   to `adcp.server.serve`. Per-call the framework calls the factory and
   gets a `RequestContext` instance (a `ToolContext` subclass) instead of
   a plain `ToolContext`.
2. `decisioning_dispatch_middleware` mutates fields on the existing
   context: `context.account = resolved`,
   `context.caller_identity = resolved.id`, `context.auth_info = auth`,
   `context.now = datetime.now(...)`.
3. `call_next()` runs the rest of the middleware chain + handler shim.
   The shim and the platform method see the populated `RequestContext`.
4. Inside the shim: `assert isinstance(context, RequestContext)` for
   mypy narrowing; pass to platform method.

### D3. Method discovery — reuse `_is_method_overridden`

**Decision:** reuse `mcp_tools.py:1336 _is_method_overridden`. Add
`DecisioningPlatform` and the per-specialism Protocol class names to the
existing `_SDK_BASE_CLASS_NAMES` set so the helper recognizes them as
"base, not override" sources.

**Rationale:** `hasattr` matches inherited Protocol stubs (returning
`...`) and silently passes validation for classes that didn't actually
implement a required method. The existing helper does
`__func__`-identity comparison against the SDK base set — exactly the
right check.

**Validation walk** (in `validate_platform`):

```python
def validate_platform(platform: DecisioningPlatform) -> None:
    missing: list[tuple[str, str]] = []
    for specialism in platform.capabilities.specialisms:
        for method_name in REQUIRED_METHODS_PER_SPECIALISM[specialism]:
            if not _is_method_overridden(platform, method_name):
                missing.append((specialism, method_name))
    if missing:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform claims specialisms but is missing "
                f"required methods: {missing}. Implement on your subclass "
                "or remove the specialism from capabilities."
            ),
            recovery="terminal",
            details={"missing": [{"specialism": s, "method": m} for s, m in missing]},
        )
```

### D4. `_HANDLER_TOOLS` allowlist — `advertised_tools` class attribute + public registration seam

**Decision:** ship two complementary surfaces:

1. **`ADCPHandler.__init_subclass__` reads an `advertised_tools: set[str]`
   class attribute and auto-registers** if set. This is the path codegen
   emits for `PlatformHandler`; it's what coding agents will reach for
   without prompting.
2. **`register_handler_tools(handler_name: str, tools: set[str]) -> None`**
   stays as the explicit escape hatch for adopters who can't (or
   won't) set the class attribute (e.g., they construct the handler
   class dynamically).

Plus a third change: **`adcp.server.serve()` emits a `UserWarning` at
boot** when an `ADCPHandler` subclass isn't in `_HANDLER_TOOLS`, doesn't
set `advertised_tools`, and doesn't have `advertise_all=True` set. The
silent-fallback (today's behavior at `mcp_tools.py:1466`'s `else`
branch) is the load-bearing DX bug.

**`register_handler_tools` semantics:**
* Re-registering the same `(handler_name, tools)` set: no-op + DEBUG
  log
* Conflicting re-registration (same name, different tools):
  `ValueError` with both sets in the message
* Unknown tool names: `ValueError` at registration time with the
  closest-match suggestion (mirrors the init-time assertion at
  `mcp_tools.py:1027-1029`):
  ```
  ValueError: register_handler_tools("MyAgent", ...) references unknown
  tool 'fake_tool_name'. Did you mean 'sync_creatives'? Valid tool
  names: see adcp.types.ADCP_TOOL_DEFINITIONS.
  ```

**Frame as a `PlatformHandler` enabler, not a "general framework
feature."** Both reviewer passes pushed back on the original framing.
Searching the codebase confirms: every `class … (ADCPHandler)` outside
tests is in `examples/`, and every one uses a built-in handler base or
accepts the discovery-only fallback. There is **no GitHub issue, no
adopter pattern, no sample code that motivates "general framework
feature."** The honest framing: "this is the registration mechanism
`PlatformHandler` uses; it happens to be a clean public seam for the
narrow case of custom `ADCPHandler` subclasses that implement a
non-standard tool subset."

**Documentation placement** — extend the existing `tools/list reflects
your overrides` paragraph at `docs/handler-authoring.md:47-56`. Don't
add a new top-level section near the bottom; it'll be missed. Lead the
new prose with: "*You probably don't need this.* If you inherit from
a framework handler class (`SalesHandler`, `GovernanceHandler`, etc.),
tool filtering is already correct. Read on only if you're writing a
custom `ADCPHandler` subclass that implements a non-standard subset
of tools."

**Worked example:** a hypothetical `ReadOnlyAnalyticsHandler(ADCPHandler)`
implementing only `get_media_buy_delivery` + `get_media_buys` — the
minimum case that demonstrates value via subset-of-existing-spec, not
"composition of two specialisms" (which reads as "this is composition,
why would I need a registration call").

**Add to `docs/handler-authoring.md` "What not to build" (line 817):**
"Don't pass `advertise_all=True` as a workaround for missing
registration." Today `advertise_all` is positioned as a legitimate
escape hatch and adopters reach for it; this stops that.

**Decisioning's use:** codegen emits
`class PlatformHandler(ADCPHandler): advertised_tools = {…}` —
`__init_subclass__` registers automatically at import time. Per-
instance, the framework's existing `_is_method_overridden` filter
then trims to the methods the platform actually overrode.

**Each `specialisms/*.py`** exports a `TOOLS: set[str]` constant
(`SalesPlatform.TOOLS = {"get_products", "create_media_buy", ...}`).
Codegen unions these into `advertised_tools` on `PlatformHandler`.

**Land in foundation PR? Reversed: split as a prep PR.** Both reviewer
passes recommended splitting because framework-shared code deserves a
different review lens than decisioning-specific code. ~150-line prep PR
(`__init_subclass__` + `register_handler_tools` + UserWarning + tests +
docs subsection) lets the framework-feature framing get scrutinized on
its own merits and shrinks the foundation PR's review surface.
Reviewer's exact words: "splitting *this* piece is the highest-leverage
split available because it's the one piece that touches framework-
shared code."

### D5. Sync-method dispatch — explicit executor + contextvars + configurable

**Decision:** allocate a `ThreadPoolExecutor` in `adcp.decisioning.serve`.
Pass it explicitly via `loop.run_in_executor(executor, ctx_snapshot.run, ...)`.
Don't `set_default_executor` (process-global side effect).

```python
ctx_snapshot = contextvars.copy_context()
result = await loop.run_in_executor(
    self._executor,
    functools.partial(ctx_snapshot.run, method, req, ctx),
)
```

**Detection:** `asyncio.iscoroutinefunction`, not `inspect.iscoroutinefunction`
(the latter doesn't unwrap `functools.partial` until 3.12).

**Configurable on `serve()` — three knobs, mutually exclusive:**

```python
def serve(
    platform: DecisioningPlatform,
    *,
    executor: ThreadPoolExecutor | None = None,    # custom executor (operator escape hatch)
    thread_pool_size: int | None = None,            # size the default executor
    # ... other kwargs
) -> None:
    if executor is not None and thread_pool_size is not None:
        raise ValueError(
            "Pass either executor= or thread_pool_size=, not both. "
            "thread_pool_size sizes the default executor; executor= is for "
            "operators who need a vetted threadpool (e.g., audit-instrumented)."
        )
    if executor is None:
        # Default: min(32, cpu+4) — fine for hello-world, surprises adopters
        # under load. thread_pool_size= bumps the ceiling for high-fanout
        # sync deployments (salesagent's Flask + sync DB drivers profile).
        size = thread_pool_size if thread_pool_size is not None else min(32, (os.cpu_count() or 1) + 4)
        executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix="adcp-decisioning")
    # ... wire executor into dispatch middleware
```

**Default surprises adopters under load.** `ThreadPoolExecutor()` with
no args defaults to `min(32, cpu+4)` per Python 3.13 stdlib. That's
fine for local dev / hello-world; production deployments running
salesagent-style sync DB drivers will saturate the pool quickly.
Document on `thread_pool_size`: "Bump for high-fanout sync deployments
(SQLAlchemy + Flask + per-request sessions). For async-everywhere
deployments, the default is fine."

**Lifecycle:** `executor.shutdown(wait=True)` registered via the
existing framework shutdown hook so it cleans up on graceful exit.
Operator-supplied executors are NOT shut down by the framework — the
operator owns the lifecycle on their side (matches the
`WebhookSender(client=...)` operator-trust contract from PR #297).

### D6. TaskHandoff — `asyncio.create_task` already snapshots contextvars; sync path needs explicit copy

**Decision:** routing detected via `asyncio.iscoroutinefunction(fn)`
only.

* Async handoff fn (`async def`): `asyncio.create_task(_runner())`.
  Don't manually `copy_context` — `create_task` does it internally
  (CPython 3.7+).
* Sync handoff fn: route through `loop.run_in_executor(executor,
  ctx_snapshot.run, fn, handoff_ctx)` with **explicit
  `contextvars.copy_context()` snapshot at the dispatch site** (D5
  pattern). Without the explicit snapshot, the sync body loses the
  request's tracing IDs / tenant IDs.
* **`Awaitable`-returning sync callables (coroutine factories not
  declared `async def`) are unsupported** and rejected at registration
  time. Adopters who want this either declare `async def` or wrap
  manually. Document explicitly to avoid the silent-routing bug.

```python
async def _project_handoff(handoff: TaskHandoff[T], ctx: RequestContext, registry, executor) -> dict:
    task_id = await registry.issue(account_id=ctx.account.id, skill_name=ctx._skill)
    handoff_ctx = TaskHandoffContext(id=task_id, _registry=registry)

    if asyncio.iscoroutinefunction(handoff._fn):
        # create_task copies contextvars internally; the background
        # task sees the request's tracing IDs / tenant ID for free.
        asyncio.create_task(_run_handoff_async(handoff._fn, handoff_ctx, registry, task_id))
    else:
        # run_in_executor does NOT snapshot contextvars — capture explicitly.
        ctx_snapshot = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        asyncio.create_task(_run_handoff_sync_via_executor(
            handoff._fn, handoff_ctx, registry, task_id, executor, ctx_snapshot, loop,
        ))

    return {"task_id": task_id, "status": "submitted", "task_type": ctx._skill, ...}
```

`_run_handoff_sync_via_executor` body:

```python
async def _run_handoff_sync_via_executor(fn, handoff_ctx, registry, task_id, executor, ctx_snapshot, loop):
    try:
        result = await loop.run_in_executor(
            executor, functools.partial(ctx_snapshot.run, fn, handoff_ctx),
        )
        await registry.complete(task_id, result=_serialize(result))
    except AdcpError as e:
        await registry.fail(task_id, error=e.to_wire())
    except Exception as e:
        await registry.fail(task_id, error={"code": "INTERNAL_ERROR", "message": str(e), "recovery": "terminal"})
```

### D7. TaskHandoff in scope — `InMemoryTaskRegistry` stub with pinned shape contracts

**Decision:** ship the `TaskRegistry` Protocol + an
`InMemoryTaskRegistry` stub (~100 lines) in the foundation PR. Don't
defer to v6.1.

**Rationale:** `SalesPlatform.create_media_buy` returns `SalesResult[T]`
— if `TaskHandoff` raises `NotImplementedError` on first use, the
hybrid headline feature is broken on day one.

**Pinned Protocol shape** (all five methods carry contract docstrings,
not just types):

```python
class TaskRegistry(Protocol):
    async def issue(self, *, account_id: str, skill_name: str) -> str:
        """Allocate a new task_id, persist `(account_id, skill_name,
        status='submitted', created_at=now)`. Return the task_id."""

    async def update(
        self, task_id: str, *, status: str, progress: dict[str, Any] | None = None,
    ) -> None:
        """Transition the task. ``status`` is from
        ``schemas/cache/3.0.0/enums/task-status.json``. ``progress`` is
        adopter-defined JSON the buyer can poll via ``tasks/get``."""

    async def complete(self, task_id: str, *, result: dict[str, Any]) -> None:
        """Mark terminal-success. ``result`` MUST be the JSON-serialized
        spec response payload (e.g.,
        ``CreateMediaBuySuccessResponse.model_dump(mode='json')``).
        Buyer's ``tasks/get`` returns this verbatim."""

    async def fail(self, task_id: str, *, error: dict[str, Any]) -> None:
        """Mark terminal-failure. ``error`` MUST be the
        ``AdcpError.to_wire()`` shape:
        ``{code, message, recovery, [field], [suggestion],
        [retry_after], [details]}``."""

    async def get(
        self, task_id: str, *, account_id: str,
    ) -> dict[str, Any] | None:
        """Account-scoped lookup. Cross-tenant probes (probing a
        task_id that doesn't belong to the requesting account)
        MUST return None, not raise. Returned shape:
        ``{task_id, account_id, skill_name, status, progress, result,
        error, created_at, updated_at, completed_at}``.
        Missing fields are JSON-null; ``progress`` is the most-recent
        update; ``result`` is set only when ``status == 'completed'``;
        ``error`` is set only when ``status in {'failed', 'rejected'}``."""
```

**`InMemoryTaskRegistry`** stores rows in a `dict[str, TaskRecord]`,
keyed by `task_id`. `get(task_id, account_id)` returns None when the
row's `account_id` doesn't match (account-scoped invariant). Lost on
process restart.

Document loudly: "in-memory; lost on restart; production deployments
swap in `SqlAlchemyTaskRegistry` (v6.1)."

### D8. Public API — both `serve()` wrapper and seam

**Decision:** export both `adcp.decisioning.serve(platform, ...)` (wrapper)
and `adcp.decisioning.create_adcp_server_from_platform(platform) -> (handler, middleware, context_factory)` (seam).

**Rationale:** wrapper covers 90% of adopters; seam is required for
adopter middleware composition + test ergonomics. Wrapper docstring
points at the seam for advanced cases.

### D9. Account-scoped cache key — structural isolation, not adopter discipline

**Decision:** stop treating `Account.id` uniqueness as adopter
responsibility. The failure mode is silent cross-tenant data leakage
through the idempotency cache; documentation alone is too hands-off
for a security boundary.

**Compose the cache scope key from `(account_store qualname,
account.id)`**, not `account.id` alone. Two adopters using different
`AccountStore` impls — or the same impl with colliding `account.id`
values across deployments sharing infra — cannot cross-leak through
the framework's cache.

```python
# Inside decisioning_dispatch_middleware:
account = await _maybe_await(platform.accounts.resolve(ref, auth_info))
store_qualname = type(platform.accounts).__qualname__
context.caller_identity = f"{store_qualname}:{account.id}"
context.account = account                       # typed access (D2)
context.auth_principal = auth_info.principal if auth_info else None
context.metadata["adcp_decisioning.auth_principal"] = auth_info.principal if auth_info else None
context.metadata["adcp_decisioning.account_store"] = store_qualname
```

**What this prevents:**

* Cross-store leakage: `SingletonAccounts(account_id="hello")`
  resolving to `account.id="hello:buyer-a"` and `ExplicitAccounts`
  resolving (via a buggy loader) to `account.id="hello:buyer-a"`
  produce different scope keys (`SingletonAccounts:hello:buyer-a`
  vs `ExplicitAccounts:hello:buyer-a`). Cache hits cannot cross.
* Within-store collision (one adopter, identical `account.id` for
  two distinct accounts) is still an adopter bug at
  `AccountStore.resolve`. The framework can't structurally prevent
  this case without a runtime registry that costs more than it buys.

**Why not a runtime uniqueness registry:** distributed registries are
hard to implement correctly across processes, require coordination,
and don't help when the same store class is used by cooperating
processes with different account spaces. The composite scope key
gets the same protection at zero coordination cost.

**Belt-and-suspenders defense in depth:**

* `Account.id` docstring: "MUST be unique within the adopter's
  deployment surface. Best practice: prefix with a deployment-stable
  namespace (`f'acme-prod-{tenant_id}'`) rather than raw tenant
  slugs. The framework composes the idempotency cache scope key as
  `(AccountStore.__qualname__, account.id)`, so cross-store
  collisions are structurally blocked; within-store collisions are
  the adopter's responsibility."
* DEBUG log line on every dispatch:
  `dispatched skill=%s scope_key=%s account_store=%s`. Operators
  investigating a leak report grep across account-store boundaries.

**Field-name clarification (round-3 concern that `caller_identity`
now misleads):** `caller_identity` carries the composite scope key
(framework-internal, read by `IdempotencyStore`). Adopter platform
methods that want the auth principal read **`ctx.auth_principal`**
(typed `str | None` attribute on `RequestContext`); adopter
middleware that consumes the raw `ToolContext` reads
`ctx.metadata["adcp_decisioning.auth_principal"]` (string key for
non-decisioning code paths).

**`RequestContext` schema gains `auth_principal`** as a typed
attribute alongside `account: Account[TMeta]`:

```python
@dataclass
class RequestContext(ToolContext, Generic[TMeta]):
    account: Account[TMeta] = field(default_factory=lambda: Account(id="<unset>"))
    auth_info: AuthInfo | None = None
    auth_principal: str | None = None  # ← NEW: typed access for adopter methods
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

### D10. Idempotency middleware ordering — wrapper builds correctly; runtime assert dropped

**Decision:** `decisioning_dispatch_middleware` MUST run before any
idempotency middleware. The **wrapper-`serve()` builds the list in the
right order**; no runtime assert is needed.

```python
def serve(platform, *, middleware=None, ...):
    # Dispatch outermost — sets caller_identity = account.id BEFORE
    # idempotency reads it for cache scoping.
    composed = [decisioning_dispatch_middleware(platform)]
    if middleware:
        composed.extend(middleware)
    adcp.server.serve(handler, middleware=composed, ...)
```

**Earlier draft tried to runtime-assert ordering** when adopters pass
their own composed list to `create_adcp_server_from_platform` (the
seam). The assertion logic was buggy
(`composed[len(composed):]` slices end-of-list — always empty) and
fixing it adds runtime cost for a deploy-time bug. **Drop the runtime
assert.** Document the ordering invariant on
`create_adcp_server_from_platform` instead: "the returned middleware
list MUST run outermost in your composed serve(middleware=...) list,
or idempotency cache scoping breaks." Adopters using the wrapper
(`adcp.decisioning.serve`) get the right order automatically; adopters
using the seam read the docs.

### D11. `__init_subclass__` — fail-fast on missing `accounts`/`capabilities`

**Decision:**

```python
class DecisioningPlatform:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "capabilities" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare a `capabilities = DecisioningCapabilities(...)` "
                "attribute. See adcp.decisioning.DecisioningCapabilities."
            )
        if "accounts" not in cls.__dict__ or cls.accounts is None:
            raise TypeError(
                f"{cls.__name__} must declare an `accounts = ...` attribute "
                "(SingletonAccounts, ExplicitAccounts, FromAuthAccounts, or a "
                "custom AccountStore impl). See adcp.decisioning.AccountStore."
            )
```

Fail-fast at class-definition time beats fail-mysteriously at first
request.

**Pydantic-`BaseModel` MRO conflict footgun.** Add a one-line note to
the `DecisioningPlatform` docstring: "Don't inherit from Pydantic
`BaseModel`; metaclass conflicts. Use a `pydantic.dataclass` field
or wrap a `BaseModel` instance if you need validation on adopter
state." The validator only inspects `cls.__dict__` (not MRO) so it
won't trip MRO walking, but combining `BaseModel` + `DecisioningPlatform`
fails at class-creation due to incompatible metaclasses.

### D12. `get_adcp_capabilities` — synthesized from `platform.capabilities`

**Decision:** the `PlatformHandler` synthesizes the `get_adcp_capabilities`
response from `platform.capabilities` so adopters don't implement it.
Always-advertised per `_PROTOCOL_TOOLS`.

```python
async def get_adcp_capabilities(
    self, params: GetAdcpCapabilitiesRequest, context: ToolContext | None = None,
) -> GetAdcpCapabilitiesResponse:
    caps = self._platform.capabilities
    return GetAdcpCapabilitiesResponse(
        adcp_version=ADCP_VERSION,
        specialisms=caps.specialisms,
        channels=caps.channels,
        pricing_models=caps.pricing_models,
        creative_agents=caps.creative_agents,
        config=caps.config,
        # ... whatever else the schema requires
    )
```

### D13. Vertical-slice examples: two runnable files + integration tests

**Decision:** ship **two** runnable single-file examples plus matching
integration tests. The TaskHandoff projection (D6) is the most novel
piece of the foundation and the highest-risk for adopter
mis-implementation; covering it via a single integration test inside
`hello_seller.py` is too thin a guard.

**`examples/hello_seller.py`** — sync flow only. Demonstrates:

* `DecisioningPlatform` subclass with `capabilities` + `accounts`
* `get_products` sync read returning typed `GetProductsResponse`
* `create_media_buy` sync success returning typed
  `CreateMediaBuySuccessResponse`
* `serve()` boot

**`examples/hello_seller_async_handoff.py`** — hybrid flow.
Demonstrates:

* `create_media_buy` returns `ctx.handoff_to_task(self._review)` for
  unfamiliar buyers, sync success for pre-approved
* The `_review` async handoff fn updates progress mid-flight, then
  completes
* Buyer can poll `tasks/get` and see `Submitted` → `Working` →
  `Completed` lifecycle
* `AdcpError` raise from inside the platform method gets projected to
  the wire `adcp_error` envelope

**Two integration tests:**

* `tests/test_hello_seller_integration.py` — boots
  `hello_seller.py` via ASGI, MCP `tools/call` round-trip
* `tests/test_hello_seller_async_handoff_integration.py` — boots the
  handoff example, exercises the full `Submitted` envelope
  serialization, registry has the task, terminal-completion path
  surfaces via `tasks/get`

**Rationale:** the foundation PR's value claim is "the seams compose
end-to-end." Without working examples the claim is unverified. Two
examples instead of one because TaskHandoff is the headline novel
feature; one example exercising both sync + handoff would mix
concerns and be harder for adopters to read as a template.

### D14. `_invoke_platform_method` contract + `REQUIRED_METHODS_PER_SPECIALISM` tolerance

**Decision:** spell out two helpers the file plan listed without a
backing decision.

**`_invoke_platform_method(platform, method_name, params, ctx)` contract:**

```python
async def _invoke_platform_method(
    platform: DecisioningPlatform,
    method_name: str,
    params: BaseModel,  # the typed Pydantic request, already validated
    ctx: RequestContext,
) -> BaseModel | dict[str, Any]:
    """Invoke a platform method, projecting hybrid returns.

    Returns:
        - A typed Pydantic response on the sync path. The caller
          (the shim) returns it as-is; the framework's existing
          ``model_dump`` codepath serializes to the wire.
        - A dict on the TaskHandoff path: the projected ``Submitted``
          envelope ``{task_id, status, task_type, ...}`` ready for
          serialization.

    Raises:
        AdcpError: re-raised from the platform method body. The dispatch
            middleware catches at the outer wrapper and projects to the
            wire structured-error envelope.
        AdcpError("INTERNAL_ERROR", recovery="terminal"): wraps any
            non-AdcpError exception so the wire response never leaks a
            stack trace. Adopter logs the original exception via the
            framework's observability hooks.
    """
    method = getattr(platform, method_name)
    if asyncio.iscoroutinefunction(method):
        result = await method(params, ctx)  # plus arg-projection if needed (D1)
    else:
        ctx_snapshot = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _executor, functools.partial(ctx_snapshot.run, method, params, ctx),
        )

    if type(result) is TaskHandoff:
        return await _project_handoff(result, ctx, _registry, _executor)
    return result
```

**`REQUIRED_METHODS_PER_SPECIALISM` lookup tolerance:**

```python
def validate_platform(platform: DecisioningPlatform) -> None:
    missing: list[tuple[str, str]] = []
    for specialism in platform.capabilities.specialisms:
        # Tolerate unknown specialisms (forward-compat with v6.1+ specs)
        # — but UserWarning, not DEBUG. A typo like "sales-non-guarateed"
        # (missing 'n') silently disables required-method checking
        # otherwise. UserWarning gets the forward-compat benefit AND
        # catches typos at server boot. Same severity as the
        # missing-handler-registration UserWarning in D4.
        required = REQUIRED_METHODS_PER_SPECIALISM.get(specialism)
        if required is None:
            warnings.warn(
                f"DecisioningPlatform claims unknown specialism {specialism!r}. "
                "Either this is a typo (compare against the AdCP 3.0 specialism "
                f"enum: {sorted(REQUIRED_METHODS_PER_SPECIALISM.keys())}), "
                "or your framework version predates the spec. Required-method "
                "validation is skipped for this specialism.",
                UserWarning,
                stacklevel=3,
            )
            continue
        for method_name in required:
            if not _is_method_overridden(platform, method_name):
                missing.append((specialism, method_name))
    if missing:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningPlatform claims specialisms but is missing "
                f"required methods: {missing}. Implement on your subclass "
                "or remove the specialism from capabilities."
            ),
            recovery="terminal",
            details={"missing": [{"specialism": s, "method": m} for s, m in missing]},
        )
```

**Rationale:** unknown specialisms shouldn't break server boot —
they just mean the deployment is on a framework version that predates
spec evolution. The buyer pays for that with a `tools/list` that
doesn't include the new specialism's tools, which is the right
fail-soft behavior.

### D15. `RequestContext` typed sub-readers — `state` and `resolve`

**Decision:** widen `RequestContext[TMeta]` from
`{account, auth_info, now, handoff_to_task}` to add two typed
framework-owned sub-readers:

```python
@dataclass
class RequestContext(ToolContext, Generic[TMeta]):
    account: Account[TMeta]
    auth_info: AuthInfo | None = None
    auth_principal: str | None = None  # round-3 D9
    now: datetime = field(default_factory=...)

    state: StateReader = field(default_factory=...)      # NEW (Round 4)
    resolve: ResourceResolver = field(default_factory=...) # NEW (Round 4)

    def handoff_to_task(self, fn) -> TaskHandoff[T]: ...
```

`StateReader` exposes sync reads on framework-owned in-flight workflow
state (no DB hit on the platform side):

```python
class StateReader(Protocol):
    def find_by_object(
        self,
        type: WorkflowObjectType,  # 'media_buy' | 'creative' | 'product' | 'plan' | 'audience' | 'rights_grant' | 'task'
        id: str,
    ) -> Sequence[WorkflowStep]:
        """Chronological steps that touched this object on this account."""
        ...

    def find_proposal_by_id(self, proposal_id: str) -> Proposal | None:
        """Resolve a proposal_id threaded across get_products → refine
        → create_media_buy without platform code."""
        ...

    def governance_context(self) -> GovernanceContextJWS | None:
        """Currently in-flight verified governance context (JWS string)
        or None for non-governance flows. Framework verifies signature,
        plan-binding, seller-binding, phase-binding before exposure;
        platform code can trust the value."""
        ...

    def workflow_steps(self) -> Sequence[WorkflowStep]:
        """All chronological steps for this request's account.
        Audit-read shape."""
        ...
```

`ResourceResolver` exposes async framework-mediated fetches with cache
+ validation built-in:

```python
class ResourceResolver(Protocol):
    async def property_list(self, list_id: str) -> PropertyList:
        """Validates the id against the seller's declared lists before
        returning."""
        ...

    async def collection_list(self, list_id: str) -> CollectionList: ...

    async def creative_format(
        self,
        format_id: FormatReferenceStructuredObject,
        *,
        revalidate: bool = False,
    ) -> Format:
        """Routes through ``capabilities.creative_agents`` declaration
        with a framework-managed cache; self-hosted formats hit the
        local CreativePlatform.list_formats(). Returns the resolved
        Format with full asset slot definitions.

        :param revalidate: If True, bypasses the framework cache and
            re-fetches from the upstream creative-agent. Adopters with
            freshness needs (e.g., creative submission validating
            against the most-recent format spec) pass ``revalidate=True``;
            most reads should use the default (False) to amortize the
            agent round-trip.

        Cache TTL is implementation detail (defaults to 1h on the
        reference impl); adopters who need stricter freshness use
        ``revalidate=True`` rather than depending on the TTL value.
        """
        ...
```

**Why this matters (Yahoo's ask):** without these readers, every
platform method that needs prior workflow context (e.g.,
`update_media_buy` checking what creative state the media buy is in,
`refine_products` reading proposal context, `get_media_buy_delivery`
reading governance bindings) has to re-query the platform's own DB,
duplicating state the framework already owns and re-validating
references the framework already validated. The TS-side approach
gives platforms typed read-only views and Yahoo specifically asked
for parity in the Python SDK.

**Why typed sub-readers, not flat `ctx.workflow_steps()` /
`ctx.property_list(...)` methods:** the namespacing is
load-bearing for adopter mental model. `state.*` = sync, "what does
the framework know"; `resolve.*` = async, "fetch + validate". Coding
agents pattern-match the namespace. Flattening loses that.

**Why `Protocol`-typed sub-readers, not concrete classes:** lets
adopters substitute test doubles in unit tests via dataclass replacement
(`replace(ctx, state=fake_state_reader)`). Concrete classes would
force monkey-patching.

**v6.0 ship scope:** ship the `Protocol`-typed surface AND every type
it references in the foundation PR. Backing impls land in v6.1; the
typed *contract* (Protocol shape + every referenced type) is
foundation-stable. Do NOT block foundation on the workflow-step
backing store, BUT do NOT punt the type definitions to v6.1 either —
adopters write the right shape from day one only if every type is
locked.

**Type-stability table (concern from round-4 review):**

| Type | Source | v6.0 status |
|---|---|---|
| `Account[TMeta]` | `adcp.decisioning.types` | locked |
| `AuthInfo` | `adcp.decisioning.context` | locked |
| `WorkflowStep` | NEW in `adcp.decisioning.state` (framework-internal, not on the wire) | locked in foundation as a frozen `@dataclass` |
| `WorkflowObjectType` | NEW in `adcp.decisioning.state` (framework-internal `Literal`) | locked in foundation |
| `Proposal` | `adcp.types.generated_poc.core.proposal` (already exists from spec codegen) | locked (generated) |
| `GovernanceContextJWS` | NEW in `adcp.decisioning.state` (`NewType('GovernanceContextJWS', str)`) | locked in foundation |
| `PropertyList` | `adcp.types.generated_poc.core.property_list_ref` (re-export `PropertyListReference` + the resolved-list type) | locked (generated) |
| `CollectionList` | `adcp.types.generated_poc.collection.collection_list` (already exists) | locked (generated) |
| `Format` | `adcp.types.generated_poc.core.format` (already exists) | locked (generated) |
| `FormatReferenceStructuredObject` | `adcp.types.generated_poc.core.format_id` (already exists) | locked (generated) |

The framework-internal types (`WorkflowStep`, `WorkflowObjectType`,
`GovernanceContextJWS`) ship as foundation-stable dataclasses /
literals so adopter code that pattern-matches on them doesn't refactor
when v6.1 lands. The wire-spec types are already in the generated
`adcp.types` package — just re-exported under `adcp.decisioning.state`
for one-stop import.

**Stub posture (UserWarning on first call) — concern from round-4 review:**

Two failure modes drove the design before round-4:
1. *Silent-empty* (TS-side `findByObject: () => []`) reads an empty
   sequence in v6.0; adopter writes
   `if not state.workflow_steps(): proceed_without_history`; v6.1
   wires the backing store and the platform's branch flips silently.
2. *Eager-raise* (TS-side `resolve.propertyList: throw ...`) crashes
   the request the moment any platform method touches the resolver,
   forcing adopters to defensively guard every read.

The round-4 fix splits the difference: **both `state` and `resolve`
emit a one-time `UserWarning` on first call to a not-yet-wired stub
method**, then return the type-correct empty value (state) or raise
(resolve). The asymmetry between empty-return (state) and raise
(resolve) is justified:

* `state.*` reads are read-only inspections of framework-owned
  in-flight state. An empty workflow-steps list IS the correct answer
  when no steps have been emitted yet (a fresh tenant has no history).
  Raising here would force adopters to wrap every audit-read in
  try/except, including paths that are valid in production. The
  UserWarning catches the "I forgot to wire the backing store"
  deployment bug; the empty return preserves the legitimate
  "no-history-yet" semantics.
* `resolve.*` fetches are validated lookups. An empty PropertyList in
  v6.0 vs. a real one in v6.1 is a divergence the framework cannot
  silently paper over. Raising forces adopters to either (a) opt out
  by not calling `resolve.*` on the v6.0 stub, or (b) wire a real
  resolver themselves.

Stub impls:

```python
import warnings

_STATE_STUB_WARNED: set[str] = set()  # one-time per method-name

class _NotYetWiredStateReader:
    """v6.0 stub. Returns type-correct empty values; emits a
    one-time UserWarning per method on first call so adopters notice
    they're reading uninitialized state."""

    def _warn_once(self, method_name: str) -> None:
        if method_name in _STATE_STUB_WARNED:
            return
        _STATE_STUB_WARNED.add(method_name)
        warnings.warn(
            f"ctx.state.{method_name}() called against the v6.0 stub "
            "StateReader; backing store lands in v6.1. Reading empty "
            "results — adopter code branching on this state will see "
            "different values once the backing store is wired. See "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15",
            UserWarning,
            stacklevel=3,
        )

    def find_by_object(self, type, id):
        self._warn_once("find_by_object")
        return ()

    def find_proposal_by_id(self, proposal_id):
        self._warn_once("find_proposal_by_id")
        return None

    def governance_context(self):
        # See "governance opt-in" subsection below — this branch is
        # only reachable when no specialism declares
        # capabilities.governance_aware=True. Server boot fails fast
        # otherwise.
        self._warn_once("governance_context")
        return None

    def workflow_steps(self):
        self._warn_once("workflow_steps")
        return ()


class _NotYetWiredResolver:
    """v6.0 stub. Raises with a pointer to the wire-up follow-up so
    adopters who reach for resolve.* know exactly which v6.1 task
    unblocks them."""

    async def property_list(self, list_id):
        raise NotImplementedError(
            f"ResourceResolver.property_list({list_id!r}) called against "
            "the v6.0 stub. Backing fetcher lands in v6.1 — see "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15. "
            "Foundation-PR adopters should not invoke ctx.resolve.* yet."
        )

    async def collection_list(self, list_id):
        raise NotImplementedError(...)  # same shape

    async def creative_format(self, format_id, *, revalidate=False):
        raise NotImplementedError(...)  # same shape
```

The UserWarning emits via the same `warnings` filter chain as the
unknown-specialism warning (D14) — adopters running pytest with
`filterwarnings = error` get a hard-fail on accidental stub reads;
production deployments get one log line per method per process.

**`governance_context()` security stub (concern from round-4 review):**

Returning `None` from `governance_context()` in v6.0 is a load-bearing
security stub: governance-aware adopter code reads
`ctx.state.governance_context()` to gate plan-binding / spend-authority
checks, and a v6.0 `None` skips the gate. v6.1 wires the gate and the
adopter's gate-skipping branch evaluates against real plans.

**Fix: opt-in capability declaration with server-boot fail-fast.**
Add `governance_aware: bool = False` to `DecisioningCapabilities`. At
server boot, `validate_platform` walks specialisms; if any specialism
that requires governance threading is claimed (`governance-spend-authority`,
`governance-delivery-monitor`) AND `capabilities.governance_aware`
is not explicitly True AND no real `StateReader` is wired,
`validate_platform` raises:

```python
raise AdcpError(
    "INVALID_REQUEST",
    message=(
        "Platform claims governance-* specialism(s) but the v6.0 "
        "StateReader stub does not provide governance_context(). "
        "Either: (a) set capabilities.governance_aware=False and drop "
        "the governance-* specialism claim until v6.1, or (b) wire a "
        "custom StateReader on serve(state_reader=...) that returns "
        "real GovernanceContextJWS values, or (c) wait for the v6.1 "
        "backing-store impl. Silent governance-gate skipping is a "
        "security boundary; the framework refuses to ship that."
    ),
    recovery="terminal",
    details={"specialisms": [...claimed governance specialisms...]},
)
```

**Why the explicit opt-in:** the alternative (raise on every
`governance_context()` call) is correct but louder than necessary for
the 90% non-governance flow. The opt-in puts the decision at server
boot (one place, fail-fast) rather than at every dispatched method.
Non-governance adopters get the empty-return + UserWarning path
unchanged; governance-claiming adopters fail to ship until they wire
real governance threading.

`capabilities.governance_aware` doc:
```python
@dataclass
class DecisioningCapabilities:
    # ... existing fields ...

    governance_aware: bool = False
    """Set True ONLY when the platform implements governance-* specialisms
    AND has wired a custom StateReader that returns real
    GovernanceContextJWS values. Setting this True with the v6.0 stub
    StateReader is a fail-fast at server boot: silent governance-gate
    skipping is a security regression the framework refuses to allow.
    Defaults False — non-governance adopters never touch this flag."""
```

**Field ordering in `RequestContext`:** `state` and `resolve` come
AFTER `account` / `auth_info` / `now` (existing fields) so existing
test fixtures and downstream code that constructs `RequestContext`
positionally don't break. New fields use `field(default_factory=...)`
defaults pointing at the stub impls above.

**Rationale for shipping the surface now even with stub backings:**
adopters write platform method bodies that read `ctx.state.*` and
`ctx.resolve.*`. If the surface lands in v6.1 instead of v6.0,
every adopter's method bodies need to be rewritten to thread state
through `ctx.account.metadata` (or worse, through their own
re-implementation of the workflow store). Locking the typed surface
+ all referenced types in v6.0 lets adopters write the right shape
from day one; the UserWarning + governance opt-in keep the silent-
divergence failure modes off the table.

**Framework-only construction (parity with TS `to-context.ts`).**
The `RequestContext` is supplied by the framework, never by the
adopter. The TS port pins this in `to-context.ts`'s file docstring
("Adopters should never construct a `RequestContext` themselves; the
framework supplies one to every specialism method call."). Mirror in
Python:

* `RequestContext.__init__` is left as the dataclass-generated default
  (necessary for `dataclasses.replace(ctx, ...)` in tests), but the
  class docstring carries an `@internal-construction` note: "Adopter
  code receives a `RequestContext` from the framework on every dispatch.
  Direct construction is supported for tests only — production code that
  builds one from outside the dispatch seam is a bug."
* The dispatch seam's hydration helper —
  `_build_request_context(tool_ctx, account)` in `dispatch.py` — is the
  ONE production path. Adopter wrappers / middleware that need to
  modify the context use `dataclasses.replace(ctx, ...)`, not raw
  construction. Documented on the helper's docstring with a worked
  example for the `state` / `resolve` test-double substitution case.
* The `_NotYetWiredStateReader` and `_NotYetWiredResolver` defaults
  exist *only* so test fixtures and `examples/hello_seller.py` can
  construct a `RequestContext()` without the framework. Production
  dispatch always supplies real (or real-stub-but-framework-instantiated)
  readers via the hydration helper. This matches the TS shape where
  the stub resolvers/readers live inside `buildRequestContext`, not
  on adopter-construction paths.

This pin matters because adopters who construct their own `RequestContext`
get neither the framework's `auth_principal` plumbing (D9) nor the
hydration helper's future v6.1 backing store. Silent divergence between
the framework path and ad-hoc adopter path is exactly the failure mode
the typing-driven safety principle is supposed to prevent.

## File plan

**Two PRs**, splitting the framework-shared code from the
decisioning-specific code per reviewer recommendation.

### Prep PR: framework handler-registration seam

| File | Lines (est) | Notes |
|---|---|---|
| `adcp/server/base.py` | +20 | `ADCPHandler.__init_subclass__` reads `advertised_tools: set[str]` class attr, calls `register_handler_tools(cls.__name__, advertised_tools)` if set. |
| `adcp/server/mcp_tools.py` | +30 | New `register_handler_tools(handler_name, tools) -> None` public seam. Idempotent on equal input, raises `ValueError` on conflicting input or unknown tool names (with closest-match suggestion). |
| `adcp/server/serve.py` | +15 | Boot-time `UserWarning` when handler subclass isn't in `_HANDLER_TOOLS`, has no `advertised_tools`, and no `advertise_all=True`. Closes the silent-fallback DX bug. |
| `docs/handler-authoring.md` | +30 | Subsection extending lines 47-56 for the narrow custom-`ADCPHandler`-subclass case. Worked example: `ReadOnlyAnalyticsHandler` advertising 2 of 9 sales tools. "What not to build" line 817 gains "Don't use `advertise_all=True` as a workaround for missing registration." |
| `tests/test_register_handler_tools.py` | ~80 | Idempotent re-registration; conflict detection; unknown-tool validation; `__init_subclass__` auto-registration; UserWarning on missing registration. |

**Prep PR total:** ~175 lines. Lands as `feat(server):` (additive
public surface — minor bump).

### Foundation PR: `adcp.decisioning.*`

| File | Lines (est) | Notes |
|---|---|---|
| `scripts/generate_decisioning_handler.py` | ~200 | Codegen script: walks per-specialism Protocols via `typing.get_type_hints`, emits `handler.py` with typed shims. `_WIRE_TO_PYTHON` map + arg-projection for `update_media_buy`-shape tools. Fail-fast on missing Pydantic types. Post-emit `ruff format` + `ruff check --fix`. Wired AFTER `generate_types.py`, NOT inside `sync_schemas.py`. |
| `adcp/decisioning/handler.py` | ~250 (generated) | `PlatformHandler(ADCPHandler)` with one typed shim per spec tool. Hand-templated `get_adcp_capabilities` synthesis special-case. `advertised_tools = {…full union…}` class attr (auto-registered via prep-PR's `__init_subclass__`). Prescriptive `# DO NOT EDIT` header. |
| `adcp/decisioning/dispatch.py` | ~350 | `decisioning_dispatch_middleware`, `_invoke_platform_method`, `validate_platform` (with tolerant `REQUIRED_METHODS_PER_SPECIALISM.get`), executor lifecycle (allocate in `serve()`, shutdown via existing framework hook), `_project_handoff` (sync needs explicit `copy_context`; async gets it free from `create_task`). |
| `adcp/decisioning/task_registry.py` | ~150 | `TaskRegistry` Protocol with pinned shape contracts (D7) + `InMemoryTaskRegistry` stub + `TaskHandoffContext` (consumed by handoff fns; carries `id` + `update(progress)` + `heartbeat()` stub). |
| `adcp/decisioning/serve.py` | ~150 | Wrapper around `adcp.server.serve`. Builds handler + middleware + context_factory (returns `RequestContext`, NOT `ToolContext`) + executor. `create_adcp_server_from_platform` seam returns `(handler, middleware, context_factory)` 3-tuple. |
| `adcp/decisioning/state.py` | ~80 | **D15** — `StateReader` Protocol + `_NotYetWiredStateReader` no-op default + `WorkflowStep` / `WorkflowObjectType` / `Proposal` / `GovernanceContextJWS` types. |
| `adcp/decisioning/resolve.py` | ~80 | **D15** — `ResourceResolver` Protocol + `_NotYetWiredResolver` raise-with-pointer default + `PropertyList` / `CollectionList` / `Format` typed return types (re-exported from `adcp.types`). |
| `adcp/decisioning/context.py` | (existing, +30) | **D15** — add `state: StateReader` and `resolve: ResourceResolver` fields with stub defaults. Round-3: `auth_principal: str \| None` typed attribute. |
| `adcp/decisioning/specialisms/sales.py` | (existing, +10) | Add `TOOLS: set[str]` constant. |
| `adcp/decisioning/platform.py` | (existing, +25) | Add `__init_subclass__` validator (D11) + `BaseModel` MRO-conflict docstring note. |
| `examples/hello_seller.py` | ~50 | Sync flow vertical slice (D13). |
| `examples/hello_seller_async_handoff.py` | ~80 | Hybrid flow vertical slice — TaskHandoff projection + Submitted envelope round-trip + AdcpError path (D13). |
| `tests/test_decisioning_dispatch.py` | ~500 | Middleware-mutation correctness; D9 composite `caller_identity = f"{store_qualname}:{account.id}"` (cross-store leak regression); D9 `auth_principal` typed attribute population; AdcpError catch + wire projection (including from sync executor branch); TaskHandoff projection (sync + async paths); sync handoff body sees ContextVar set in request scope (D6 sync-context propagation regression); validate_platform fail-fast; D14 unknown-specialism `UserWarning` (typo regression); `_invoke_platform_method` contract (D14); arg-projection kwargs path (D1 — verifies `update_media_buy` shim refactor-safety). |
| `tests/test_decisioning_task_registry.py` | ~100 | `TaskRegistry` Protocol shape; `InMemoryTaskRegistry` issue/update/complete/fail; concurrent issue (no task_id collision). |
| `tests/test_decisioning_task_registry_cross_tenant.py` | ~80 | **Hostile-probe regression (round-3 finding):** account A creates a task; account B with different `account_id` probes for it via `get(task_id=A's_id, account_id=B)`; expect None, NOT raw_record. Adopter regressing to `if not found: return raw_record` would surface in production without this test. Plus: `complete()` then cross-tenant `get` still returns None; `fail()` then cross-tenant `get` still returns None. |
| `tests/test_decisioning_platform_validation.py` | ~50 | D11: platform without `capabilities` fails at class definition; platform without `accounts` fails at class definition; valid platform passes. |
| `tests/test_decisioning_capabilities_synthesis.py` | ~80 | D12 unit test: synthesized `get_adcp_capabilities` response matches `platform.capabilities` field-for-field. Cheaper than driving via integration test. |
| `tests/test_decisioning_handler_codegen.py` | ~80 | Regen-drift: regen `handler.py` into tempdir, `git diff --exit-code`. Mirrors `tests/test_mcp_schema_drift.py` pattern. **Drift error message asserts the prescriptive form** (round-3 finding) — names `uv run python scripts/generate_decisioning_handler.py` verbatim. Codegen-time fail-fast on missing Pydantic Request type. |
| `tests/test_hello_seller_integration.py` | ~150 | End-to-end sync: boot example via ASGI, MCP `tools/call` hits sync `get_products` + sync `create_media_buy`, response round-trips. AdcpError path: hostile budget rejected with structured-error envelope. |
| `tests/test_hello_seller_async_handoff_integration.py` | ~180 | End-to-end hybrid: boot the handoff example, MCP `tools/call` to `create_media_buy` returns `TaskHandoff`, Submitted envelope serializes correctly, `tasks/get` returns Submitted → Working → Completed lifecycle, registry has the terminal artifact. |
| `tests/test_decisioning_context_state_resolve.py` | ~150 | **D15** — `StateReader` / `ResourceResolver` Protocol structural match; default `_NotYetWiredStateReader` returns empty sequences AND emits one-time `UserWarning` per method on first call (warning suppressed on subsequent calls — module-level set); `_NotYetWiredResolver.*` raises `NotImplementedError` with the design-doc anchor; substituting test doubles via `dataclasses.replace(ctx, state=fake)` works; **governance opt-in fail-fast (D15 round-4):** platform claiming `governance-spend-authority` with default stub `StateReader` raises `AdcpError("INVALID_REQUEST")` at server boot; same platform with `capabilities.governance_aware=False` and no governance specialism passes; same platform with custom `StateReader` returning real `GovernanceContextJWS` passes; **`creative_format(revalidate=True)` parameter regression** — calling stub with `revalidate=True` raises with the same message as `revalidate=False` (parameter is part of Protocol contract, not gated on stub). |
| `tests/test_decisioning_validate_platform_strict.py` | ~120 | **Round-4 (Emma #6 + #16):** specialism enum-coverage check (declaring a known specialism that has no `REQUIRED_METHODS_PER_SPECIALISM` entry must NOT silently pass — must fail server boot pointing at the spec drift); validator throws are caught and surface as `AdcpError("INVALID_REQUEST", ...)` rather than crashing the server boot. |
| `tests/test_decisioning_in_memory_registry_prod_gate.py` | ~80 | **Round-4 (Emma #8):** `serve()` + `InMemoryTaskRegistry` + `production` env raises `AdcpError` unless `ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1` set. Sales-broadcast-tv adopter forced into HITL path is the regression case. |
| `tests/test_decisioning_status_change_isolation.py` | ~80 | **Round-4 (Emma #17):** two `serve()` instances in the same process route their own `publish_status_change` events to per-instance subscribers, NOT a module-level singleton. Concurrent test files don't clobber each other's bus. |

**Foundation PR total:** ~2510 lines (~250 generated, ~1130 tests).
After prep PR + this lands: ~3885 lines on top of 1500-line foundation
skeleton already committed.

## Things deferred (track separately)

- **`SqlAlchemyTaskRegistry`** — v6.1; replaces `InMemoryTaskRegistry`
  without changing dispatch. Track in foundation-audit follow-ups.
- **`A2aTaskDelivery` for A2A buyers** — currently TaskHandoff projects
  to MCP `Submitted` envelope only. A2A delivery wraps the same payload
  in `Task` + `TaskStatusUpdateEvent`. Add when first A2A adopter needs
  it; same Protocol surface.
- **`tenant_registry`** — multi-tenant primitive composing
  `serve(factory=...)`. Out of foundation-PR scope; v6.1.
- **`status_changes` (DbBackedStatusChangeBus)** — adopters with
  audit-relevant status transitions need this; in-memory bus ships
  with foundation, durable bus is v6.1.
- **`delivery` module** — McpWebhookDelivery + A2aTaskDelivery composed
  on top of `adcp.webhook_sender` + `adcp.server.a2a_server`. Add when
  TaskHandoff projection moves from in-memory stub to real persistence.
- **Other 11 specialism Protocols** — only `SalesPlatform` ships in the
  foundation PR (the vertical slice); others template-and-fan-out
  after merge.
- **Hand-written → codegen for `PlatformHandler` shims** — ~600 lines
  is auto-generatable from `_HANDLER_TOOLS` + spec response types.
  Hand-written for foundation; codegen task is a separate PR.

## Round-2 review changelog

Items the round-2 reviewers (agentic-product, python-expert, dx-expert,
code-reviewer) revised or strengthened from the round-1 design:

* **D1 codegen source-of-truth changed** from `_HANDLER_TOOLS` to
  per-specialism Protocols (Protocols carry typed Pydantic Request +
  return-shape signal; `_HANDLER_TOOLS` reduces to "every spec tool"
  and adds nothing).
* **D1 wire-shape ≠ Python-signature edge case** added (e.g.
  `update_media_buy(media_buy_id, patch, ctx)`); arg-projection lookup
  required.
* **D1 shim return type narrowed** to Success-only; dropped
  `| dict[str, Any]` fallback. Wire projection happens in dispatch
  AFTER shim returns.
* **D1 codegen pipeline ordering corrected** — runs after
  `generate_types.py`, NOT inside `sync_schemas.py`. Doc previously
  conflated the two.
* **D1 generator must fail-fast** on missing Pydantic Request type
  (don't emit `Any` fallback).
* **D1 prescriptive header comment** required (not just timestamp);
  CI uses `tests/test_mcp_schema_drift.py` precedent.
* **D4 framing reversed** from "general framework feature" to
  "`PlatformHandler` enabler that happens to be a clean public seam."
  Search of the codebase: every existing custom `ADCPHandler` subclass
  is in `examples/`; none would benefit from the seam beyond what
  `advertise_all=True` provides today. Don't oversell the framing.
* **D4 surface upgraded** with `ADCPHandler.__init_subclass__` reading
  `advertised_tools: set[str]` class attribute — codegen emits this
  on `PlatformHandler`; coding agents will pattern-match the
  registration without prompting. Explicit
  `register_handler_tools(...)` call stays as the escape hatch.
* **D4 `serve()` UserWarning** at boot when handler subclass is in
  none of `_HANDLER_TOOLS` / `advertised_tools` / `advertise_all=True`.
  Closes the silent-fallback discoverability gap (today's
  `mcp_tools.py:1466` else branch).
* **D4 doc placement corrected** — extends the existing `tools/list
  reflects your overrides` paragraph at `handler-authoring.md:47-56`,
  NOT a new top-level section.
* **D4 split into prep PR** (reversed from the "land in foundation"
  call). Framework-shared code deserves a different review lens than
  decisioning-specific code; the prep PR is ~175 lines and lets the
  framework-feature framing get scrutinized on its own merits.
* **D6 `Awaitable`-returning sync callable case** added — coroutine
  factories not declared `async def` are unsupported and rejected at
  registration; document explicitly.
* **D6 sync-handoff contextvars** require explicit
  `contextvars.copy_context()` snapshot at the dispatch site
  (`run_in_executor` does NOT auto-snapshot, unlike `to_thread`).
* **D7 TaskRegistry Protocol shape pinned** — every method has a
  contract docstring spelling out arg/return types and account-scoping
  invariants. `complete(result)` MUST be JSON-serialized spec response;
  `fail(error)` MUST be `AdcpError.to_wire()` shape; cross-tenant `get`
  returns None.
* **D10 broken assertion dropped** — runtime ordering check had a slice
  bug (`composed[len(composed):]` is always empty); the wrapper builds
  the right order anyway. Document the invariant on
  `create_adcp_server_from_platform` instead.
* **D11 `BaseModel` MRO conflict footgun** documented — adopters can't
  inherit from both `DecisioningPlatform` and Pydantic `BaseModel`.
* **D13 added** — vertical-slice example + integration test as
  first-class deliverables (previously implicit in the file plan).
* **D14 added** — `_invoke_platform_method` contract pinned;
  `REQUIRED_METHODS_PER_SPECIALISM.get(s, set())` made tolerant of
  unknown specialisms (forward-compat with v6.1+ specs).
* **File plan split** into prep PR + foundation PR. Total grew from
  ~1900 to ~2275 lines (extra tests for round-2-surfaced cases).

## Round-3 review changelog

User feedback on the published design doc (PR #316). Eight items in
priority order; all resolved by tightening D1 / D5 / D9 / D13 / D14
and adding cross-tenant + arg-projection regression tests.

* **D9 (Item 1) — Account.id uniqueness elevated to a framework-enforced
  security boundary.** Round-2 left global uniqueness as adopter
  responsibility; one buggy `AccountStore` would silently leak
  idempotency-cache entries across stores. Cache scope key composed as
  `f"{account_store.__class__.__qualname__}:{account.id}"` so two stores
  collision-prone on `id` alone (e.g. `SingletonAccounts(account_id="x")`
  vs. `ExplicitAccounts` returning `Account(id="x")`) get structural
  isolation. The framework enforces; adopters can't downgrade.
* **D9 (Item 6) — `RequestContext.auth_principal` typed attribute.**
  `caller_identity = account.id` is correct *semantically* but the
  middleware-facing field name now misleads (it's the cache scope key,
  not the auth principal). Added typed `auth_principal: str | None` on
  `RequestContext` (sourced from `AuthInfo.principal` when present) so
  middleware reading "who authenticated this request" has a
  load-bearing field name.
* **D14 (Item 3) — Unknown specialisms now `UserWarning`, not DEBUG.**
  Round-2 made `REQUIRED_METHODS_PER_SPECIALISM.get(s, set())` tolerant
  for forward-compat. But typos like `sales-non-guarateed` (missing 'n')
  silently pass tolerance and reach buyers as a no-method platform.
  `UserWarning` at boot catches typos in CI without breaking
  v6.1+ forward compat (warnings are non-fatal and logged once per
  specialism per process).
* **D1 (Item 4) — Codegen drift error is prescriptive.**
  `tests/test_decisioning_codegen_drift.py` failure message names the
  exact command (`uv run python scripts/generate_decisioning_handler.py`)
  and links the rationale (`docs/proposals/decisioning-platform-dispatch-design.md#d1`).
  CI failures should tell a contributor *what to type next*, not just
  *what's wrong*.
* **D1 (Item 8) — Arg-projection emits explicit kwargs.** `**kwargs`
  unpack would silently swallow Pydantic field renames. The generator
  emits the kwargs by name (`platform.update_media_buy(media_buy_id=req.media_buy_id, patch=req, ctx=ctx)`)
  so a future Pydantic field rename trips a `NameError` at codegen time
  rather than a runtime KeyError post-deploy.
* **D5 (Item 5) — `ThreadPoolExecutor` configurability.** Three knobs
  on `create_adcp_server_from_platform`:

  * `executor=` — bring-your-own (instrumentation, custom pool)
  * `thread_pool_size=int` — convenience override
  * default — `ThreadPoolExecutor(max_workers=min(32, os.cpu_count() * 4))`
    with `thread_name_prefix="adcp-decisioning-"`

  `executor` and `thread_pool_size` are mutually exclusive (raises
  `ValueError` at server construction). Lifecycle: framework-owned
  pools shut down via the existing serve-loop teardown hook; BYO pools
  are the adopter's responsibility (documented).
* **D13 (Item 7) — Two example files, not one.** Original plan had a
  single `examples/hello_seller.py` covering the sync path. Added
  `examples/hello_seller_async_handoff.py` exercising:

  * The hybrid `SalesResult[T]` return shape (sync fast path *or*
    `ctx.handoff_to_task(fn)`)
  * `AdcpError(code='BUDGET_TOO_LOW', recovery='correctable',
    field='total_budget')` raise-and-catch round-trip through the
    dispatcher

  Two examples make the hybrid pattern concrete; one example would
  bury the harder case in commentary.
* **File plan additions for items 1, 2, 3, 6, 8:**

  * `tests/test_decisioning_task_registry_cross_tenant.py` — hostile
    probe regression: account A creates task `t_xyz`, account B calls
    `tasks_get(task_id="t_xyz")`, must get 404 not B's view of A's
    task. (Item 2.)
  * `tests/test_hello_seller_async_handoff_integration.py` — wire-shape
    assertions for both hybrid arms + AdcpError envelope. (Item 7.)
  * `tests/test_decisioning_dispatch.py` extended with: composite
    `caller_identity` cache-scope-key construction (Item 1),
    `auth_principal` attribute population from `AuthInfo` (Item 6),
    UserWarning emission for unknown specialism (Item 3), arg-projection
    explicit-kwargs path including Pydantic field-rename simulation
    (Item 8).

  Foundation PR total grew from ~2275 to ~2475 lines.

## Round-4 review changelog

Cross-language review pass — synthesizes (a) the TS team's review of
the parallel TypeScript port (`adcontextprotocol/adcp-client` PR #1005,
EmmaLouise2018 round-1), (b) the TS team's `decisioning-platform-python-port-v2.md`
RFC for what the Python SDK should ship, and (c) Yahoo's specific ask
for typed metadata + framework-owned state threading on
`RequestContext`.

**Guiding principle the TS port adopted, ported here:** "make it
impossible for an implementer to screw up via typing." Python can't
match TS's compile-time `RequiredPlatformsFor<S>` gate, but per-method
typed surfaces, runtime `validate_platform` boot-time checks, typed
`RequestContext` sub-readers, and `Protocol` structural matching close
most of the gap. Where TS got compile-time enforcement we get
boot-time fail-fast; where TS got "buyer-supplied data can't reach
this type" we get the same property via dispatch type-identity.

### What's structurally avoided in our Python design

The TS team's round-1 review surfaced bugs that are **structurally
unrepresentable in our hybrid `SalesResult[T]` design**:

* **Emma #2 — `validatePlatform` allows "neither defined" path
  → runtime crash.** Python uses one method per tool returning
  `SalesResult[T]`, not dual `create_media_buy` + `create_media_buy_task`.
  No "both defined" or "neither defined" failure modes exist.
* **Emma #3 — Missing `*Task` arms for 4 of 6 Submitted-bearing
  tools.** Same reason — every mutating tool is hybrid via
  `SalesResult[T]`. Python's structural confirmation: schemas show
  Submitted arms on `update_media_buy`, `get_products`, `build_creative`,
  `sync_catalogs` (in addition to `create_media_buy` and `sync_creatives`).
* **Emma #13 — Compile-time XOR for dual-method via TS discriminated
  unions.** N/A — single method per tool.
* **Emma's design concern #14 — "Always declare HITL, resolve
  immediately" anti-pattern that taxes every sync buyer with `tasks_get`
  polling.** Python's `TaskHandoff[T]` is exactly the pattern Emma
  asked for (`throw RequiresReviewError` from sync, framework converts
  to `submitted` envelope). Worth calling out in the foundation PR
  description so the framework-design choice gets the credit.

### Items applied to the Python design

* **D14 (Emma #6) — specialism enum coverage check.** Round-3 caught
  *unknown* specialisms with `UserWarning`. Round-4 catches the inverse:
  declaring a *known* specialism (in the wire enum) that has no
  `REQUIRED_METHODS_PER_SPECIALISM` entry must NOT silently pass — must
  fail server boot pointing at the spec drift. Test:
  `test_decisioning_validate_platform_strict.py`.
* **D7 + serve() (Emma #8) — production gate on `InMemoryTaskRegistry`.**
  `serve()` refuses to start when wired with `InMemoryTaskRegistry` and
  the existing SDK convention `ADCP_ENV in {"prod", "production"}`
  (case-insensitive — same logic as `adcp.validation.client_hooks._default_response_mode`
  reads at `src/adcp/validation/client_hooks.py:68`) unless
  `ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1` opt-in is set.
  Sales-broadcast-tv adopters are *structurally forced* into the HITL
  path which depends on the registry — silent in-memory fallback is a
  real prod foot-gun. Reuses the existing prod-detection helper to
  avoid drift between two env-var conventions; do not introduce a
  new variable. Test: `test_decisioning_in_memory_registry_prod_gate.py`.
* **Dispatch (Emma #10) — `AdcpError` projection consistency.**
  Every code path that can raise `AdcpError` (specialism methods,
  account resolver, validators, capability synthesis,
  `list_accounts`-shape reads) goes through the same wire-projection
  in dispatch. No path falls back to generic `SERVICE_UNAVAILABLE`.
  Pinned in D14 `_invoke_platform_method` contract; verified via
  `test_decisioning_dispatch.py` extension (every code path covered).
* **D6 (Emma #11) — sync-handoff register-before-cleanup race.**
  TS-side bug: `taskFn` resolving synchronously runs `composed.then`
  cleanup before `_registerBackground` registers, leaking the entry.
  Python equivalent in our `loop.run_in_executor` + `copy_context()` path:
  if the handoff fn resolves before `task_registry.register()` writes
  the entry, the cleanup hook may delete a non-existent record. Add
  regression test in `test_decisioning_task_registry_cross_tenant.py`
  asserting register-before-resolve ordering even for synchronously
  completing handoff fns.
* **`validate_platform` (Emma #16) — catch validator throws.**
  Wrap each per-specialism validator in try/except; on raise, surface
  as `AdcpError("INVALID_REQUEST", ...)` rather than crashing server
  boot or leaving the platform marked stuck-unverified. Test:
  same file as #6 above.
* **Dispatch (Emma #17) — per-server status-change bus, not
  module-level singleton.** Module-level `publishStatusChange` is hostile
  to multi-tenant test isolation (concurrent `serve()` instances clobber
  each other's bus). Use a per-server bus on the wrapper returned by
  `create_adcp_server_from_platform`; `publish_status_change` is bound
  via the per-server `RequestContext` (or via explicit `server.bus`
  reference passed to background workers). Test:
  `test_decisioning_status_change_isolation.py`.
* **`AdcpError` (Emma #18) — `ACCOUNT_NOT_FOUND` semantics.**
  Document that `ACCOUNT_NOT_FOUND` is reserved for the resolver path
  (`AccountStore.resolve` → `AdcpError(code='ACCOUNT_NOT_FOUND')`).
  Specialism methods raising `ACCOUNT_NOT_FOUND` get re-mapped to
  `INVALID_REQUEST` with a `field='account_id'` hint, so adopter misuse
  doesn't pollute the error code's meaning to buyers. Update
  `AdcpError` docstring + add a dispatch test.
* **`AdcpError` (Emma #19) — codegen `ErrorCode` literal.**
  Currently `AdcpError(code: str)` is free-form. Generate an `ErrorCode`
  Literal type from `schemas/cache/3.0.0/enums/error-code.json` so
  `AdcpError(code='BUDGET_TOO_LO')` (typo) trips mypy at adopter
  edit-time. Vendor codes outside the enum stay accepted via
  `ErrorCode | str` union. Tracked as deferred (codegen task on the
  drift-script PR after foundation).
* **CI lint (Emma #5) — examples can't reach into `src/`.**
  `examples/hello_seller.py` MUST import from `adcp.decisioning`, not
  `src/adcp/decisioning`. Add a lint to CI: any `from adcp.` import in
  `examples/` rejecting `from src.adcp.` paths. Avoids the TS-side
  three-source-of-truth bug.

### D15 added — typed `RequestContext` sub-readers (Yahoo's ask)

The TS team's `decisioning-platform-python-port-v2.md` RFC + Yahoo's
explicit request: widen `RequestContext[TMeta]` to include framework-
owned typed sub-readers `state` (sync workflow-state reads) and
`resolve` (async framework-mediated fetches). Without this, every
platform method that needs prior workflow context has to re-query its
own DB, duplicating state the framework already owns and re-validating
references the framework already validated. **Surface ships in v6.0
with no-op stub backings; impls fill in for v6.1**, so adopters can
write the right shape from day one without rewriting later. See D15
above for the full Protocol definitions and rationale.

**D15 round-4 review tightenings (post-publish):**

* **Stub asymmetry fixed.** Original D15 had `state.*` returning empty
  silently and `resolve.*` raising — different posture in two readers
  doc'd in the same paragraph. Round-4 review caught the asymmetry as
  a real adopter foot-gun (silent-empty masks the stub state until
  v6.1 wires the backing store and the platform's branch flips
  silently). Fix: both stubs emit a one-time `UserWarning` per method
  on first call. `state.*` still returns type-correct empty values
  (an empty workflow-steps list IS legitimate for fresh tenants);
  `resolve.*` still raises (an empty `PropertyList` is divergence
  the framework cannot silently paper over). The asymmetry is now
  justified per-reader rather than left undocumented.
* **`governance_context()` fail-fast at server boot.** Returning
  `None` from `governance_context()` in v6.0 was a load-bearing
  security stub — adopters claiming governance-* specialisms get
  `None` and skip the gate; v6.1 wires the gate and the
  gate-skipping branch evaluates against real plans. Fix: add
  `capabilities.governance_aware: bool = False`. At server boot,
  `validate_platform` raises `AdcpError("INVALID_REQUEST")` if any
  `governance-*` specialism is claimed AND no real `StateReader` is
  wired AND `governance_aware` isn't explicitly opted into. The
  framework refuses to ship silent governance-gate skipping;
  adopters must wire real governance threading or drop the claim.
* **Type-stability table added.** Round-4 surfaced "lock all
  D15-referenced types in v6.0, not just the Protocols." D15 now
  includes a per-type table: `Account`, `AuthInfo`, `Proposal`,
  `PropertyList`, `CollectionList`, `Format`,
  `FormatReferenceStructuredObject` are all already in
  `adcp.types.generated_poc/`; `WorkflowStep`, `WorkflowObjectType`,
  `GovernanceContextJWS` are framework-internal types defined fresh
  in `adcp.decisioning.state` and shipped foundation-stable. Adopter
  code that pattern-matches on these types doesn't refactor when v6.1
  lands.
* **`creative_format(revalidate: bool = False)` parameter pinned in
  the Protocol contract.** Round-4 caught the 1h cache TTL doc'd as
  Protocol contract — adopters with freshness needs would be stuck.
  Pinning `revalidate=` at the Protocol level moves the cache TTL
  to impl detail and gives adopters an opt-out without depending on
  any specific TTL value. Test: stub raises identically with
  `revalidate=True` so the parameter contract is enforced even before
  the v6.1 backing impl ships.
* **Env var convention reused.** Original Round-4 referenced
  `ADCP_ENV=production` as a free-form string; round-4 review caught
  the drift risk vs. existing SDK convention. Fix: reuse
  `_default_response_mode` logic from
  `src/adcp/validation/client_hooks.py:68` —
  `ADCP_ENV in {"prod", "production"}` (case-insensitive). One
  prod-detection mechanism, no drift.

### File plan additions

* `adcp/decisioning/state.py` (~80 lines) — `StateReader` Protocol +
  stub
* `adcp/decisioning/resolve.py` (~80 lines) — `ResourceResolver`
  Protocol + stub
* `adcp/decisioning/context.py` (+30 lines) — wire `state` + `resolve`
  fields with stub defaults (D15)
* `tests/test_decisioning_context_state_resolve.py` (~120 lines) —
  D15 Protocol structural match + test-double substitution regression
* `tests/test_decisioning_validate_platform_strict.py` (~120 lines) —
  Emma #6 enum coverage + Emma #16 validator-throws fail-soft
* `tests/test_decisioning_in_memory_registry_prod_gate.py` (~80 lines) —
  Emma #8 prod-gate regression
* `tests/test_decisioning_status_change_isolation.py` (~80 lines) —
  Emma #17 per-server bus regression
* CI: examples-import lint rule (Emma #5) — added to ruff config
  (`tool.ruff.lint.flake8-tidy-imports` ban-relative-imports for
  `examples/**`)

Foundation PR total grew from ~2475 to ~2965 lines (D15 + Round-4
tests + Emma items).

### Items deferred to follow-up PRs (not foundation-blocking)

* **`ErrorCode` Literal codegen** (Emma #19) — separate codegen-script
  PR after foundation. Tracking issue.
* **Workflow-step / proposal / governance backing store** for `state`
  reader (D15 v6.1 backing impls). Foundation ships the no-op stub.
* **`tasks/get` wire surface** for adopter HITL polling — the framework
  has the registry from foundation, but the wire endpoint that buyers
  hit lands with `task_registry` follow-up PR.

### TS-only items, no Python equivalent

* Emma #1 (JWKS material comparison) — Python uses `cryptography`
  full-key import; the bug is structurally unrepresentable.
* Emma #12 (`<P extends DecisioningPlatform<any, any>>` cast widening)
  — Python `TypeVar` with `default=` preserves narrowing through
  `Protocol` parameterization.
* Emma #15 (`resolveByHost` O(N) parsing) — Python doesn't have that
  surface yet.
* Emma #20 (`typesVersions` missing) — npm-only.
