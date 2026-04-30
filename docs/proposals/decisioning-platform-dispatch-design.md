# DecisioningPlatform dispatch design (post-review)

Pre-implementation reference for the `adcp.decisioning.{handler, dispatch,
serve, task_registry}` modules. Synthesizes 7 reviewer passes:

* **Round 1** (initial design): agentic-product-architect, python-expert
* **Round 2** (post-codegen-and-framing additions): agentic-product-architect
  (framing), python-expert (codegen mechanics), dx-expert (handler
  registration UX), code-reviewer (consistency)
* **Round 3** (post-design-doc-published, on PR #316): user feedback on
  Account.id leak boundary, cross-tenant probe regression, validation
  noise, codegen DX, executor configurability, field-name semantics,
  example coverage, kwarg unpacking

Authoritative through D14. Tracks "things deferred" for v6.1 and beyond.

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

**Foundation PR total:** ~2100 lines (~250 generated, ~700 tests).
After prep PR + this lands: ~3500 lines on top of 1500-line foundation
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
