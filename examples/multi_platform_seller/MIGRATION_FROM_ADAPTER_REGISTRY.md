# Migrating from `ADAPTER_REGISTRY` / `AdServerAdapter` to `PlatformRouter` + `DecisioningPlatform`

Audience: maintainers of [Prebid salesagent](https://github.com/prebid/salesagent)
or any multi-tenant sales agent shaped the same way — an
`ADAPTER_REGISTRY` dict mapping adapter slugs to subclasses of an
`AdServerAdapter` ABC, picked per-request from a `Tenant.ad_server_config.adapter`
field. This guide is a translation table. Where your code does `X`,
the SDK target is `Y`.

The honest summary: **your business logic stays. The framework absorbs
the cross-cutting concerns.** HITL gating, sandbox toggles, mock
fixtures, compliance scaffolding, error projection, idempotency,
webhook emission, lifecycle state assertions, credential handling,
connection pooling — those move from inside your adapter classes into
SDK primitives. The adapter body itself shrinks to one job: translate
AdCP wire shapes onto your upstream API and back.

> The implementation this guide migrates *to* lands in parallel PR
> [`bokelley/feat-platform-router`](https://github.com/adcontextprotocol/adcp-client-python/pull/477)
> (issue [#477](https://github.com/adcontextprotocol/adcp-client-python/issues/477)).
> The `PlatformRouter` recipe is shipped as an example first; once it
> proves out we promote it into `adcp.decisioning.dispatch`. Examples
> in this doc reference primitives that are already on `main` —
> `DecisioningPlatform`, `Account.mode`, `upstream_for(ctx)`,
> `assert_media_buy_transition`, `compose_method`, `UpstreamHttpClient`,
> the F12 webhook auto-emit. The router itself is the only piece
> arriving alongside this doc.

## The high-level shift

```
salesagent today                            adcp Python SDK target
─────────────────                           ─────────────────
ADAPTER_REGISTRY: dict[str, Type]           PlatformRouter({
  → instantiated per-request                    "tenant_acme":   GAMPlatform(...),
  → tenant.ad_server_config.adapter             "tenant_globex": KevelPlatform(...),
  → AdServerAdapter ABC                     })

Per-adapter, hand-rolled today:             Per-platform, SDK-handled:
  HTTP client + pooling                  →  adcp.decisioning.UpstreamHttpClient
  HITL gating in __init__ + each method  →  compose_method + ShortCircuit
  Sandbox toggles per deployment         →  Account.mode = "sandbox"
  ~3000 LOC mock_ad_server.py            →  Account.mode = "mock" + mock_upstream_url
  Compliance scaffolding (ADCP_SANDBOX)  →  comply_test_controller gate (Phase 1)
  Webhook emission                       →  F12 auto-emit
  Lifecycle state checks per adapter     →  assert_media_buy_transition
  Error projection per adapter           →  AdcpError + UpstreamHttpClient projection
  Per-tenant credentials in config dict  →  ApiKey / StaticBearer / DynamicBearer
```

The dispatch model inverts. Today, the registry hands you a class and
you instantiate it per-request with the tenant's config. After
migration, platforms are long-lived instances; the router resolves
which one handles each call from the wire account ref.

## Translation table

### 3.1 `ADAPTER_REGISTRY` → `PlatformRouter`

**Before** — `salesagent/src/adapters/__init__.py:17`:

```python
ADAPTER_REGISTRY = {
    "gam": GAMAdapter,
    "google_ad_manager": GAMAdapter,
    "broadstreet": BroadstreetAdapter,
    "kevel": KevelAdapter,
    "mock": MockAdapter,
    "triton": TritonAdapter,
    "creative_engine": CreativeEngineAdapter,
}

def get_adapter(adapter_type: str, config: dict, principal):
    adapter_class = ADAPTER_REGISTRY.get(adapter_type.lower())
    if not adapter_class:
        raise ValueError(f"Unknown adapter type: {adapter_type}")
    return adapter_class(config, principal)
```

Each request fetches the tenant, reads `tenant.ad_server_config.adapter`,
looks up the class, and instantiates it with the tenant's config.

**After**:

```python
from adcp.decisioning import PlatformRouter, serve

router = PlatformRouter(
    accounts=salesagent_account_store,  # your AccountStore
    platforms={
        "tenant_acme":   GAMPlatform(...),
        "tenant_globex": KevelPlatform(...),
        "tenant_initech": BroadstreetPlatform(...),
    },
)
serve(router, transport="both")
```

Per-tenant dispatch is automatic. `AccountStore.resolve` maps the wire
account reference (subdomain, header, or auth principal) to a
`tenant_id`; the router delegates each method to the platform keyed by
that id.

Platforms are constructed once, at process start, and reused for every
request. Connection pools, OAuth token caches, and any platform-level
state amortise across the platform's lifetime — the per-request
instantiation overhead in the registry pattern goes away.

### 3.2 `AdServerAdapter` ABC → `DecisioningPlatform` + `SalesPlatform`

**Before** — `salesagent/src/adapters/base.py:174` and the Kevel
implementation at `salesagent/src/adapters/kevel.py:13`:

```python
class AdServerAdapter(ABC):
    capabilities: AdapterCapabilities = AdapterCapabilities()
    connection_config_class: type[BaseConnectionConfig] | None = BaseConnectionConfig
    product_config_class: type[BaseProductConfig] | None = None

    def __init__(self, config, principal, dry_run=False, creative_engine=None, tenant_id=None):
        # ... 30 LOC of audit logger init, principal id resolution,
        # manual_approval_required flag setup ...

    @abstractmethod
    def create_media_buy(self, request, packages, start_time, end_time, package_pricing_info=None):
        ...

    @abstractmethod
    def add_creative_assets(self, media_buy_id, assets, today):
        ...

    # ... 7 more abstract methods, each with positional-arg signatures
```

**After**:

```python
from adcp.decisioning import DecisioningPlatform, DecisioningCapabilities
from adcp.decisioning.specialisms import SalesPlatform
from adcp.decisioning.upstream import StaticBearer

class GAMPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://googleads.googleapis.com/v202405"

    capabilities = DecisioningCapabilities(
        specialisms=["sales-guaranteed", "sales-non-guaranteed"],
        # ... structured wire-spec capability blocks
    )

    accounts = salesagent_account_store

    def __init__(self, *, oauth_token: str) -> None:
        self._auth = StaticBearer(token=oauth_token)

    async def create_media_buy(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth)
        # adapter logic — translate AdCP req → GAM REST → AdCP response
        ...
```

What changes:

* **Method signatures collapse** to `async (req, ctx) -> response`. The
  request is a typed Pydantic model; `ctx` carries the resolved
  `Account`, `auth_info`, and request metadata. The
  positional-argument explosion (`packages`, `start_time`, `end_time`,
  `package_pricing_info`) becomes attributes on `req`.
* **The adapter declares its production URL once**, on `upstream_url`.
  Per-tenant routing flows through `ctx.account.metadata`; per-tenant
  credentials flow through `ctx.auth_info`. Sandbox / mock variants
  are handled by `Account.mode`, not by the adapter.
* **Capabilities live on the platform, not on the class hierarchy.**
  `DecisioningCapabilities` mirrors the AdCP wire spec one-to-one;
  `validate_platform()` confirms at boot that every declared
  specialism has the methods it requires.

The translator pattern (translate-AdCP-wire-onto-upstream-and-back)
stays intact. The Kevel adapter's `_validate_targeting` and
`_build_targeting` helpers (`salesagent/src/adapters/kevel.py:61`,
`:102`) port across unchanged — they're business logic. What
disappears is the `__init__` boilerplate, the abstract-method
ceremony, and the dry-run plumbing.

### 3.3 HITL gating → `compose_method` + `ShortCircuit`

**Before** — `salesagent/src/adapters/base.py:226` plumbs the flag into
every adapter, and each adapter checks it inline. From
`google_ad_manager.py:267` and `:571`:

```python
class AdServerAdapter:
    def __init__(self, config, principal, ...):
        self.manual_approval_required = config.get("manual_approval_required", False)
        self.manual_approval_operations = set(
            config.get("manual_approval_operations", [...])
        )

    def _requires_manual_approval(self, operation: str) -> bool:
        return self.manual_approval_required and operation in self.manual_approval_operations

# in each adapter method:
if self._requires_manual_approval("create_media_buy") and not already_approved:
    return self._send_to_approval_queue(...)
```

The check is repeated in `create_media_buy`, `add_creative_assets`,
and `update_media_buy`. Three places to keep in sync.

**After**:

```python
from adcp.decisioning import compose_method, ShortCircuit

async def hitl_gate(req, ctx) -> ShortCircuit | None:
    if salesagent_requires_approval(ctx.account, req):
        # async approval — return a Submitted task envelope
        return ShortCircuit(value=ctx.handoff_to_task(send_to_approval_queue))
    return None  # falls through to the wrapped method

class GAMPlatform(DecisioningPlatform, SalesPlatform):
    create_media_buy = compose_method(
        inner=_create_media_buy_impl,
        before=hitl_gate,
    )
    add_creative_assets = compose_method(
        inner=_add_creative_assets_impl,
        before=hitl_gate,  # same gate, different method
    )
```

HITL becomes declarative, not embedded. One gate function composes
across every method that needs it; one place to update when the
approval policy changes; the inner method body stays focused on
upstream translation. `ShortCircuit` is a discriminated wrapper —
returning a bare value instead of `ShortCircuit(value=...)` raises
`TypeError` at runtime, so adopters porting middleware between
languages can't accidentally short-circuit with `None`.

### 3.4 Sandbox toggles → `Account.mode`

**Before** — sandbox is a deployment-level concern in salesagent. A
config dict carries the flag; each adapter (and the middleware in
front of them) consults it independently:

```python
# in adapter __init__ or inline:
if config.get("sandbox", False):
    self.use_sandbox_credentials = True
    self.base_url = SANDBOX_URL
```

This means `mode='sandbox'` is implicit, scattered, and trivially
spoofable from request data — which is the salesagent footgun the SDK
deliberately closes.

**After** — sandbox is a property of the resolved account:

```python
class SalesagentAccountStore:
    async def resolve(self, ctx) -> Account[TenantMetadata]:
        tenant = self._db.get_tenant(ctx.principal_id)
        return Account(
            id=tenant.account_id,
            mode="sandbox" if tenant.sandbox else "live",
            metadata=TenantMetadata(
                tenant_id=tenant.id,
                advertiser_id=tenant.advertiser_id,
                # ...
            ),
        )
```

The trust boundary shifts. `mode` lives on the account, which is
resolved from the authenticated principal — never from request data,
headers, or `ctx_metadata`. Buyers can't promote themselves into
sandbox by setting a flag; sandbox is what *the seller's* account
store says it is.

The framework's sandbox gate
(`adcp.decisioning.account_mode.assert_sandbox_account`) refuses
test-only surfaces (`comply_test_controller`, `force_*`, `simulate_*`)
on `mode='live'` accounts. Resolvers that spread untrusted input into
the resolved account leak this gate; the docstring on
`assert_sandbox_account` calls this out explicitly.

### 3.5 Mock fixtures → `Account.mode='mock'`

**Before** — `salesagent/src/adapters/mock_ad_server.py:53` is a
~1,800-LOC in-memory ad server. It implements every abstract method of
`AdServerAdapter` against a hand-rolled state dict, simulates lifecycle
transitions on a timer, and ships as part of the adapter registry
keyed `"mock"`.

```python
class MockAdServer(AdServerAdapter):
    adapter_name = "mock"
    # ... 1786 lines of in-memory state, scenario logic,
    # and lifecycle simulation ...
```

This is the biggest deletion in the migration. Mock-mode is now
SDK-handled.

**After** — populate `mock_upstream_url` on mock-mode accounts in your
`AccountStore.resolve`:

```python
class SalesagentAccountStore:
    async def resolve(self, ctx) -> Account[TenantMetadata]:
        tenant = self._db.get_tenant(ctx.principal_id)
        if tenant.is_dev_tenant:
            return Account(
                id=tenant.account_id,
                mode="mock",
                metadata=TenantMetadata(
                    tenant_id=tenant.id,
                    mock_upstream_url="http://localhost:4500",
                ),
            )
        # ... live path
```

The platform's adapter code is unchanged. `self.upstream_for(ctx)`
inspects `ctx.account.mode` and routes the underlying
`UpstreamHttpClient` at the mock fixture URL when `mode='mock'`,
without touching the adapter body. The mock fixture itself ships in
`@adcp/client` (`bin/adcp.js mock-server <specialism>`) and serves
deterministic per-specialism upstream-API responses.

The `mock_ad_server.py` module deletes wholesale. ~1,800 LOC of
in-memory state machine becomes a dev-time fixture URL on the account.

### 3.6 Compliance scaffolding → SDK `comply_test_controller` gate

**Before** — salesagent's compliance scenarios mix into the adapters
through environment toggles, seeded state, and per-adapter scenario
hooks. Adopters wire `ADCP_SANDBOX=1` or similar, then each adapter
keeps its own seeded state for the deterministic-testing surface.

**After** — adopters write nothing. The SDK's compliance gate (Phase
1, `adcp.decisioning.account_mode.assert_sandbox_account`) handles
authority:

* `mode="live"` → `comply_test_controller` raises `PERMISSION_DENIED`
  with `details.scope='sandbox-gate'`.
* `mode="sandbox"` or `"mock"` → call admits.
* Scenario state, if you want it, is managed by an SDK
  `TestControllerStore` rather than per-adapter seeded fixtures.

The bedrock invariant: deterministic-testing surfaces never fire on
production traffic, regardless of how the adopter's compliance code
is wired. The gate is the contract.

### 3.7 Lifecycle state machine

**Before** — each adapter encodes the legal state graph itself. Inline
checks scattered through `update_media_buy` and similar:

```python
if media_buy.status == "active" and new_status == "pending_creatives":
    raise BadStateError(...)
```

The graph drifts across adapters. A buyer hitting two tenants with
different lifecycle behaviour gets different errors for the same
illegal transition.

**After**:

```python
from adcp.decisioning import assert_media_buy_transition

async def update_media_buy(self, req, ctx):
    current = await self._upstream_get_status(req.media_buy_id, ctx)
    assert_media_buy_transition(
        from_state=current.status,
        to_state=req.target_state,
        media_buy_id=req.media_buy_id,
    )
    # ... proceed with the upstream update
```

The legal graph is the spec graph
(`adcp.decisioning.state_machines.MEDIA_BUY_TRANSITIONS`); every
platform refuses the same illegal transitions with the same
`INVALID_STATE` / `recovery='correctable'` error shape. Buyers get
consistent semantics across tenants without the adopter touching the
state-graph code at all.

The same module ships `assert_creative_transition` for the creative
lifecycle.

### 3.8 Webhook emission → F12 auto-emit

**Before** — each adapter (or per-tenant middleware) hand-rolls
webhook delivery: format the payload, sign it, fire the request, retry
on transient failures, log on permanent failures.

**After** — wire a `WebhookSender` (or `WebhookDeliverySupervisor`)
once on `serve(...)`. The framework auto-emits a sync-completion
webhook after every mutating tool call when the buyer registered a
`push_notification_config`:

```python
from adcp.webhook_sender import WebhookSender

serve(
    router,
    transport="both",
    webhook_sender=WebhookSender(...),
    # auto_emit_completion_webhooks defaults to True
)
```

The framework owns shape, signing, retry, and logged-and-swallowed
failure semantics. Adopters who want manual control inside a handler
pass `auto_emit_completion_webhooks=False` and emit themselves —
but the auto-emit path is the default, so most adopters delete their
webhook plumbing entirely.

### 3.9 Per-adapter HTTP client → `UpstreamHttpClient`

**Before** — every adapter wires its own httpx client, auth scheme,
retry policy, JSON parsing, and 404→None handling. From
`salesagent/src/adapters/kevel.py:42`:

```python
def __init__(self, config, principal, ...):
    super().__init__(...)
    self.api_key = self.config.get("api_key")
    self.base_url = "https://api.kevel.co/v1"
    self.headers = {"X-Adzerk-ApiKey": self.api_key, ...}

# ... per-method:
response = requests.post(f"{self.base_url}/...", headers=self.headers, json=payload)
if response.status_code == 404:
    return None
if response.status_code >= 400:
    raise BadRequestError(...)
```

Repeated across six adapters with subtle variations in error
projection, retry behavior, and auth header shape.

**After**:

```python
from adcp.decisioning.upstream import ApiKey

class KevelPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://api.kevel.co/v1"

    def __init__(self, *, api_key: str) -> None:
        self._auth = ApiKey(header_name="X-Adzerk-ApiKey", value=api_key)

    async def create_media_buy(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth)
        order = await client.post("/campaigns", json=payload)
        # client handles connection pooling, retry, 404→None,
        # and projects non-2xx responses → AdcpError automatically
```

Auth strategies (`StaticBearer`, `DynamicBearer`, `ApiKey`) are
declarative dataclasses. `DynamicBearer` accepts an async token
factory for OAuth refresh — the resolver runs per-request and can key
on `ctx.account.metadata` for per-tenant credentials. The
`UpstreamHttpClient` itself is pooled per `(base_url, auth)` on the
platform instance, so multi-tenant credential fan-out scales without
adapter-level connection management.

### 3.10 Error projection

**Before** — each adapter wraps upstream errors in custom error types,
then a translation layer maps those onto wire shapes:

```python
try:
    response = self._client.post(...)
except SomeUpstreamError as e:
    raise BadRequestError(...) from e
```

The mapping drifts; adopters periodically discover a code path that
projects a vendor error directly to the buyer.

**After** — `UpstreamHttpClient` projects HTTP errors to spec-conformant
`AdcpError` codes automatically. Non-2xx responses raise:

* `401` → `AUTH_REQUIRED` (`recovery='terminal'`)
* `403` → `PERMISSION_DENIED` (`recovery='terminal'`)
* `404` on resource ops → `MEDIA_BUY_NOT_FOUND` (or per-call override
  via `not_found_code` for creatives, forecasts, etc.)
* `409` → `CONFLICT` (`recovery='terminal'`)
* `429` → `RATE_LIMITED` (`recovery='transient'`)
* `5xx` / network timeout / JSON decode → `SERVICE_UNAVAILABLE`
  (`recovery='transient'`)
* `4xx` other → `INVALID_REQUEST` (`recovery='terminal'`)

Adopters rarely need to wrap. Strict response validation
(`ValidationHookConfig(responses='strict')`, the default) catches any
non-enum code at the wire — vendor codes can't accidentally ship.

## What NOT to migrate

A few things in the salesagent shape don't translate cleanly. They're
either out of scope or stay where they are:

* **Adapter `dry_run` flag** (`base.py:199`). Useful for the salesagent
  CLI; not a wire concept. Keep your dry-run flow behind your existing
  CLI/test entry points; don't try to thread it onto the platform.
* **`audit_logger` mixed into adapters** (`base.py:222`). Audit is
  cross-cutting and per-adopter; the framework doesn't manage it.
  Wire your existing audit sink at the `serve(...)` middleware seam.
* **Tenant DB schema and admin UI**. The SDK doesn't touch your
  persistence model. `Tenant`, `Principal`, `BuyerAgent` tables stay;
  the `AccountStore.resolve` body reads them.
* **Per-adapter UI registration** (`base.py:478`). The SDK isn't a UI
  framework; if your admin UI registers per-adapter Flask routes,
  keep that wiring exactly as-is.

## Migration order

A path through the change that preserves a working server at every
step:

1. **Pick one adapter to port.** Kevel
   (`salesagent/src/adapters/kevel.py`, ~700 LOC) is the smallest
   real production adapter — start there. The mock adapter is the
   wrong starting point because it deletes entirely; you want a port
   you can validate against real upstream behaviour.
2. **Convert abstract methods one at a time** using
   `examples/v3_reference_seller/` as the template. Each method body
   shrinks: drop the `manual_approval_required` check, drop the
   custom error wrapping, drop the dry-run logging.
3. **Wire `upstream_url` + auth.** Declare the production URL on the
   class; pass an `ApiKey` / `StaticBearer` to the platform's
   `__init__`.
4. **Convert `Tenant.ad_server_config.adapter` lookup into an
   `AccountStore`** that returns `Account(id=..., mode=..., metadata=...)`
   with `tenant_id` in metadata. The store's `resolve` reads your
   existing tenant table; nothing else in your DB changes.
5. **Validate against the AdCP storyboards.** The
   [`media_buy_seller`](https://adcontextprotocol.org/storyboards) story
   is the wire-shape contract — if it passes, your translator is
   correct on the wire. Run it as your conformance test for each
   ported platform.
6. **Move HITL gates into `compose_method`.** One gate function,
   composed onto every method that previously checked
   `manual_approval_required`. Delete the inline checks.
7. **Delete `mock_ad_server.py`** once `mode='mock'` is wired and the
   storyboard passes. ~1,800 LOC in one PR.
8. **Repeat for remaining adapters** (Broadstreet, Triton,
   `creative_engine`, GAM). GAM last — it's the largest, and the
   ported infrastructure from earlier adapters lets you focus the
   GAM port on the upstream-translation logic alone.
9. **Stand up `PlatformRouter`** over all platforms. Wire the router's
   `accounts` to your existing `AccountStore`; the per-tenant
   dispatch becomes automatic.

At any point in steps 1–8 you can run the storyboard against the
ported tenants while the rest of the registry still serves the
unported tenants — there's no flag day.

## What this doesn't solve

A few things this migration deliberately doesn't address:

* **Multi-protocol bridging.** If salesagent translates AdCP requests
  across multiple buyer protocols (OpenRTB, Prebid Server's PBS-Java
  shape, etc.), that's a separate seam. The translator pattern here
  goes one direction: AdCP wire ↔ upstream API. Buyer-side protocol
  fan-out is a different problem.
* **Production performance characteristics.** The SDK hasn't been
  load-tested at salesagent's scale. `UpstreamHttpClient` connection
  pooling, the per-platform-instance auth caching, and the router's
  dispatch overhead all look reasonable on paper, but real-world
  latency budgets at salesagent scale are unproven.
* **The salesagent admin UI.** The SDK has no opinions about your
  management console. The `AdServerAdapter.register_ui_routes` hook
  (`base.py:478`) doesn't have a counterpart on `DecisioningPlatform`
  because it shouldn't — keep your Flask routes where they are.
* **The CAPI semantic mismatch.** `provide_performance_feedback`
  carries an aggregate; per-event upstreams (Google CAPI, GAM-flavored
  conversion ingest) need a projection that loses fidelity. The v3
  reference seller's `MIGRATION.md` covers this in detail and the same
  guidance applies here.

## See also

* [`examples/v3_reference_seller/MIGRATION.md`](../v3_reference_seller/MIGRATION.md)
  — the single-platform translator pattern, with the full method-by-method
  port checklist for the v3 wire spec.
* [`docs/proposals/lifecycle-state-and-sandbox-authority.md`](../../docs/proposals/lifecycle-state-and-sandbox-authority.md)
  — the three-mode design (`live`/`sandbox`/`mock`) this guide leans on.
* [Issue #477](https://github.com/adcontextprotocol/adcp-client-python/issues/477)
  — the multi-platform proof, the `PlatformRouter` recipe, and the
  acceptance criteria the parallel implementation PR satisfies.
