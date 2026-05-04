# Authoring an ADCP server handler

This guide is for teams building AdCP-compliant agents — sales agents,
creative agents, governance agents, signals agents — on top of
`adcp.server`. It captures the patterns that keep handlers spec-compliant
and production-grade, plus the hooks the SDK provides so you don't have
to rebuild middleware that already exists.

## 15-minute decision tree

- **Just want an agent running?** → Start with "The one-file starting
  point" below, then `serve()`.
- **Need auth in front of tools?** → If your proxy already validates
  credentials, use "Pattern 1 — reverse-proxy auth". Otherwise copy
  `examples/mcp_with_auth_middleware.py` — it covers the ContextVars
  pattern, the `DISCOVERY_METHODS` + `DISCOVERY_TOOLS` composed bypass
  (note: `tools/list` is pre-auth by default — see
  [tools/list is unauthenticated by default](#toolslist-is-unauthenticated-by-default)),
  and `hmac.compare_digest`.
- **Multi-tenant?** → Subclass `ToolContext`, populate `tenant_id` in
  your `context_factory`, and read the
  [Multi-tenant typing](#multi-tenant-typing) section. The idempotency
  middleware uses `(tenant_id, caller_identity)` for scope isolation —
  populating `tenant_id` is required for cross-tenant safety.
- **Full context?** → Keep reading.

## The one-file starting point

```python
from adcp.server import ADCPHandler, ToolContext, serve
from adcp.server.responses import capabilities_response, products_response

class MyAgent(ADCPHandler):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])

    async def get_products(self, params, context=None):
        return products_response(MY_PRODUCTS)

serve(MyAgent(), name="my-agent")
```

That's a complete AdCP agent. All 57+ other tools return `not_supported`
automatically via the `ADCPHandler` default methods; override only what
your agent actually implements.

**`tools/list` reflects your overrides, not the full spec surface.**
By default the SDK advertises only the tools whose methods your
subclass overrode (plus spec-mandated discovery). The minimal
``MySeller`` above would surface two tools to MCP clients
(``get_adcp_capabilities`` + ``get_products``), not 57 — dropping
~20-30K tokens of unused tool schemas from every client's context.
Pass ``advertise_all=True`` to ``serve()`` / ``create_mcp_server()`` /
``create_a2a_server()`` to restore the full surface (spec-compliance
storyboards, agents that deliberately signal
``not_supported`` on specific tools).

**Custom handler bases — declare ``advertised_tools``.** The override
filter works perfectly for direct ``ADCPHandler`` subclasses and the
specialized bases (``GovernanceHandler``, ``ContentStandardsHandler``,
etc.). But if you're authoring a *new* specialized base that
introduces its own focused tool set — typically a codegen target like
``adcp.decisioning.handler.PlatformHandler``, or a hand-rolled
``ReadOnlyAnalyticsHandler`` — declare the tool set on the class
itself::

    from typing import ClassVar
    from adcp.server import ADCPHandler

    class ReadOnlyAnalyticsHandler(ADCPHandler):
        advertised_tools: ClassVar[set[str]] = {
            "get_products",
            "get_media_buy_delivery",
        }

        # ... method bodies ...

The framework auto-registers ``ReadOnlyAnalyticsHandler ->
advertised_tools`` at class definition time via
``ADCPHandler.__init_subclass__``. The override filter then runs
against this set instead of inheriting ``ADCPHandler``'s full
surface.

Equivalent imperative form — same outcome::

    from adcp.server import register_handler_tools
    register_handler_tools("ReadOnlyAnalyticsHandler", {"get_products", ...})

Without either declaration, ``serve()`` emits a one-time
``UserWarning`` at boot pointing you at the registration paths. The
warning matters because the alternative — silent over-advertisement
of the full ADCPHandler surface — is exactly the discoverability
gap that bites operators in production: ``tools/list`` returns 57
tools when the agent only handles 2.

**What not to build:** don't use ``advertise_all=True`` as a
workaround for missing registration. The flag exists for legitimate
opt-in cases (storyboards, deliberate ``not_supported`` signaling);
using it to silence the registration warning over-advertises every
tool to every buyer.

## The `_impl` pattern (production-grade)

Production agents usually don't put business logic directly on handler
methods. Instead:

- Business logic lives in `src/core/_impl/` or similar — transport-free,
  takes typed domain objects, returns typed responses.
- `ADCPHandler` methods are thin delegations that pull identity /
  adapter config out of `ToolContext` and call the `_impl` function.

This keeps the tested surface independent of whether the caller came in
via MCP, A2A, HTTP, a background job, or a test. The SDK's server
framework is designed for this shape:

```python
from adcp.server import ADCPHandler, ToolContext
from myagent.impl.products import get_products_impl
from myagent.identity import ResolvedIdentity

class MyAgent(ADCPHandler):
    async def get_products(self, params, context: ToolContext | None = None):
        identity = _resolve_identity(context)
        if identity is None:
            return adcp_error("AUTH_REQUIRED", "Authentication required")
        return await get_products_impl(params, identity=identity)

def _resolve_identity(ctx: ToolContext | None) -> ResolvedIdentity | None:
    if ctx is None or ctx.caller_identity is None:
        return None
    return ResolvedIdentity(
        principal_id=ctx.caller_identity,
        tenant_id=ctx.tenant_id,
        # … adapter config, feature flags, etc. from your DB
    )
```

**Why `return None`, not raise.** Raising a non-``ADCPError`` exception
produces a 500 to the client (see *Error handling* below); the
``return None`` shape lets the handler turn the failure into a
spec-compliant ``adcp_error`` envelope. The next section shows the
DB-enrichment variant of the same pattern.

### ResolvedIdentity with DB enrichment

The `# … adapter config, feature flags, etc. from your DB` comment hides
a second DB hop that most production handlers need. `context_factory`
resolves `caller_identity` from the bearer token; `_resolve_identity`
enriches it with per-principal config that isn't available at auth time.
Return `None` on failure so the calling handler converts it to an error
dict (raising a non-`ADCPError` exception produces a 500 — see
[Troubleshooting](#troubleshooting)):

```python
async def _resolve_identity(ctx: ToolContext | None) -> ResolvedIdentity | None:
    if ctx is None or ctx.caller_identity is None:
        return None
    row = await pool.fetchrow(
        "SELECT tenant_id, db_url, feature_flags "
        "FROM principals WHERE id = $1",
        ctx.caller_identity,
    )
    if row is None:
        return None
    return ResolvedIdentity(
        principal_id=ctx.caller_identity,
        tenant_id=row["tenant_id"],
        db_url=row["db_url"],
        feature_flags=frozenset(row["feature_flags"] or ()),
    )
```

**Resolve once per request** at the top of the handler and check for
`None` before delegating to `_impl`:

```python
async def get_products(self, params, context: ToolContext | None = None):
    identity = await _resolve_identity(context)
    if identity is None:
        return adcp_error("AUTH_REQUIRED")
    return await get_products_impl(params, identity=identity)
```

Passing the resolved identity through avoids compounding DB round-trips
when a single handler call delegates to multiple `_impl`s.

## Typed handler params

Handler methods may declare their `params` as a Pydantic model instead
of `dict[str, Any]`. The dispatcher reads the annotation and
deserialises the incoming request before calling your method — you
get IDE autocomplete, Pydantic validation at the handler boundary, and
typed attribute access in exchange for a one-line signature change.

```python
from adcp.server import ADCPHandler, ToolContext
from adcp.types import GetProductsRequest, GetProductsResponse, Product


class MySeller(ADCPHandler):
    async def get_products(
        self,
        params: GetProductsRequest,
        context: ToolContext | None = None,
    ) -> GetProductsResponse:
        # params.buying_mode, params.promoted_offering, params.brief —
        # typed, validated, autocompleted. No params.get(...) anywhere.
        if params.buying_mode.value == "refine":
            ...
        return GetProductsResponse(products=[...])
```

**Validation errors surface as `INVALID_REQUEST`.** A Pydantic
`ValidationError` at the boundary is converted to a structured AdCP
error with the field path and validation detail — callers see the
spec-typed recovery classification (`correctable`), not a stack trace.
The raw offending value is stripped from the error (SDK sends
`include_input=False` to Pydantic) so mistyped secrets don't echo
back to multi-hop intermediaries.

> **Custom validator caveat.** If you layer `@field_validator` or
> `@model_validator` on a custom params model, **don't f-string the
> offending value into the `ValueError` message**
> (`raise ValueError(f"bad token {v}")`). The message text flows into
> the client-visible error — `include_input=False` only suppresses
> Pydantic's default echo, not your own. Stick to describing the
> constraint (`raise ValueError("token must match pk_… pattern")`).

**Back-compat is automatic.** Handlers that keep `params: dict[str, Any]`
work unchanged. The dispatcher falls back to the dict path when no
Pydantic model is in the annotation — migrate incrementally, one
method at a time. Sibling methods with mixed typed/dict signatures
coexist on the same handler.

**Unions with dict are supported.** `params: GetProductsRequest | dict[str, Any]`
(the shape the specialized SDK bases use internally) works — the
dispatcher picks the first Pydantic branch and deserialises. Existing
handlers that do defensive `GetProductsRequest.model_validate(params)`
inside the method still work: Pydantic's `model_validate` on an
already-typed instance is a no-op (returns the same object; field
validators are skipped — so a custom `@field_validator` layered on a
params model won't fire twice, and won't fire again on the defensive
re-call inside the handler).

**Custom models too.** You aren't restricted to the SDK's generated
request classes. Any `BaseModel` subclass declared on `params`
triggers typed dispatch — useful when you want to layer stricter
field constraints or business invariants on top of the spec shape.
Define the model at module top-level so forward-reference resolution
works (`from __future__ import annotations` stringifies all
annotations).

## Authentication

The SDK does not enforce authentication. There are two supported
integration patterns:

### Pattern 1 — reverse-proxy auth

The proxy (nginx, Caddy, Envoy) validates credentials and forwards only
authenticated requests. The SDK trusts the proxy's decision. Simplest,
and the right choice when your identity provider and tool endpoints run
behind the same gateway.

### Pattern 2 — in-process HTTP middleware (recommended)

Use `BearerTokenAuthMiddleware` and `auth_context_factory` from
`adcp.server`. The SDK owns the four security-critical concerns
(ContextVar carrier, `hmac.compare_digest`, discovery-method bypass,
reset-in-finally); you supply only `validate_token`:

```python
from adcp.server import (
    BearerTokenAuthMiddleware,
    Principal,
    auth_context_factory,
    create_mcp_server,
)

async def validate_token(token: str) -> Principal | None:
    row = await db.fetch_token(token)
    if row is None or row.revoked:
        return None
    return Principal(caller_identity=row.principal_id, tenant_id=row.tenant_id)

mcp = create_mcp_server(MyAgent(), context_factory=auth_context_factory)
app = mcp.streamable_http_app()
app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)
```

`validate_token` may be sync or async — whichever matches your token
store. Return `None` to reject; don't raise (exceptions become 500s
and leak the presence of an auth path to attackers).

Full worked example: `examples/mcp_with_auth_middleware.py`. Integration
test proving the composition: `tests/test_mcp_middleware_composition.py`.

#### Reading "who's calling?" — `auth_principal` vs `caller_identity`

The two identity-shaped fields adopters see have distinct purposes and
the wrong read silently misroutes authorization decisions.

- **`ctx.auth_principal`** — the verified caller's identity label.
  Read this for per-principal ACLs ("can principal X mutate this
  buy?"). Sourced from `AuthInfo.principal` on signed-request flows
  and from the `current_principal` ContextVar on bearer flows.
  `None` for unauthenticated dev fixtures.
- **`ctx.caller_identity`** — a cache scope key, not a principal.
  At the transport layer (`ToolContext.caller_identity`) it's the
  bare principal; the framework dispatch helper mutates it into a
  composite key (`<store_module>.<store_qualname>:<account_id>`)
  before the handler sees a decisioning `RequestContext`. The
  idempotency middleware reads the composite form. Treat as opaque
  inside dispatch handlers — log or forward, but don't parse,
  compare, or rewrite.

Skill-middleware and `context_factory` code (which run at the
transport layer, before dispatch hydration) still read
`context.caller_identity` for legitimate cache / rate-limit keying;
the composite mutation happens later, in `_build_request_context`.

To discriminate auth flows inside a handler — e.g. when a signed-request
buyer and a bearer-token buyer hit the same handler and you want
flow-specific authorization — read `ctx.auth_info.kind`. On bearer
flows the dispatch helper synthesizes
`AuthInfo(kind="bearer", principal=...)` from `current_principal`, so
`ctx.auth_info.kind == "bearer"` is the typed predicate (no
`ctx.auth_info is None` check needed for authenticated bearer
traffic). Signed-request flows carry `kind="signed_request"` /
`"http_sig"` directly from the verifier middleware.

#### Pattern 2a — custom middleware (when the shipped one doesn't fit)

Subclass `BearerTokenAuthMiddleware` to tighten the discovery bypass,
add extra headers, or customise the 401 response. For non-bearer auth
(mTLS, signed requests, API key via header), write a Starlette
middleware that populates `adcp.server.auth.current_principal` /
`current_tenant` yourself and keep using `auth_context_factory` — the
`ContextVar`s are the contract, not the middleware class.

#### Pattern 2b — tenant routing via subdomain (nginx → bearer)

Production multi-tenant deployments sometimes route to per-tenant
databases by subdomain (`acme.ads.example.com` → Postgres for tenant
`acme`) before validating the bearer token. The correct shape is two
separate middleware layers — not subdomain logic inside `validate_token`:

```python
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from adcp.server import BearerTokenAuthMiddleware, Principal

# Populated by SubdomainTenantMiddleware before BearerTokenAuthMiddleware runs.
_routing_tenant: ContextVar[str | None] = ContextVar("routing_tenant", default=None)


class SubdomainTenantMiddleware(BaseHTTPMiddleware):
    """Extracts tenant from the leftmost hostname label (acme.ads.example.com → 'acme')."""

    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        tenant = host.split(".")[0] if host.count(".") >= 2 else None
        token = _routing_tenant.set(tenant)
        try:
            return await call_next(request)
        finally:
            _routing_tenant.reset(token)


async def validate_token(token: str) -> Principal | None:
    routing_tenant = _routing_tenant.get()
    row = await db.fetchrow(
        "SELECT principal_id, tenant_id FROM tokens "
        "WHERE token_hash = digest($1, 'sha256') AND revoked_at IS NULL",
        token,
    )
    if row is None:
        return None
    # Reject if the subdomain tenant disagrees with the token's tenant —
    # guards against cross-tenant token replay.
    if routing_tenant and row["tenant_id"] != routing_tenant:
        return None
    return Principal(caller_identity=row["principal_id"], tenant_id=row["tenant_id"])


app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)
app.add_middleware(SubdomainTenantMiddleware)  # outermost → runs first
```

> **Middleware order.** Starlette applies `add_middleware` calls from
> bottom to top — `SubdomainTenantMiddleware` is added last so it wraps
> outermost and runs first, populating `_routing_tenant` before
> `BearerTokenAuthMiddleware` calls `validate_token`. Invert the order
> and `_routing_tenant.get()` returns `None` on every request.

### Discovery tools bypass auth

Per AdCP spec, `get_adcp_capabilities` is the handshake — clients MUST
be able to call it before authenticating. The SDK exports the list as a
frozenset:

```python
from adcp.server import DISCOVERY_METHODS, DISCOVERY_TOOLS

async def dispatch(self, request, call_next):
    method, tool_name = _peek_jsonrpc(request)
    is_discovery = method in DISCOVERY_METHODS or (
        method == "tools/call" and tool_name in DISCOVERY_TOOLS
    )
    if not is_discovery:
        self._require_valid_token(request)
    return await call_next(request)
```

Your agent may have additional public discovery tools outside the AdCP
spec (e.g. a public `list_public_formats`); extend with `DISCOVERY_TOOLS
| {"your_tool"}` rather than redefining the set. See also
[tools/list is unauthenticated by default](#toolslist-is-unauthenticated-by-default)
for the MCP-layer handshake methods this same gate covers.

Call `validate_discovery_set` at import time to guard against accidentally
including non-discovery tools in your extension (a common copy-paste error):

```python
from adcp.server import DISCOVERY_TOOLS, validate_discovery_set

MY_DISCOVERY_TOOLS = DISCOVERY_TOOLS | {"list_public_formats", "get_vendor_catalog"}
validate_discovery_set(MY_DISCOVERY_TOOLS)  # raises ValueError for unknown names or mutating tools
```

`validate_discovery_set` does not register the tools — it only validates
the set you pass to your middleware's discovery bypass.

### `tools/list` is unauthenticated by default

MCP's streamable-HTTP transport accepts three JSON-RPC methods as
pre-auth handshake: `initialize` (session setup),
`notifications/initialized` (handshake-completion notification), and
`tools/list` (inventory advertisement). All three are exported as
`DISCOVERY_METHODS` for the composed gate above. This is consistent
with the MCP spec — discovery is a handshake concern — and with the
AdCP spec, where `get_adcp_capabilities` is pre-auth.

An unauthenticated client POSTing `{"method": "tools/list"}` receives
the full tool inventory: names, input schemas, descriptions, and
annotations. The SDK treats tool names and input schemas as
non-sensitive — they are public AdCP spec surface, and AdCP's discovery
flow presumes clients can see them before deciding whether to
authenticate. Freeform description strings are the one leakage vector.
If your deployment:

- **Adds tools outside the AdCP spec** with custom descriptions that
  embed deployment hints (internal names, rollout flags,
  customer-specific surfaces), either scrub the descriptions or gate
  `tools/list`.
- **Ships only spec-defined tools**, the descriptions come from
  `ADCP_TOOL_DEFINITIONS` — already public upstream — and no
  scrubbing is needed.

To gate `tools/list` behind auth, remove it from `DISCOVERY_METHODS`
in your middleware and run the same credential check you run for
`tools/call`. Clients that support auth-on-handshake work fine;
clients that expect pre-auth discovery will break and need an
out-of-band tool manifest.

The integration test at
[`tests/test_mcp_middleware_composition.py`](../tests/test_mcp_middleware_composition.py)
locks the default posture with a positive assertion that `tools/list`
returns 200 without credentials and a negative control that the gate
still lets it through when an invalid bearer is present.

## Custom tools alongside ADCP tools

Some agents need to expose vendor-specific tools (an internal
`list_publishers` endpoint, a custom storyboard hook) that aren't part
of the AdCP spec. `create_mcp_server()` returns a bare FastMCP
instance — register custom tools on it with FastMCP's standard
`@mcp.tool()` decorator:

```python
from adcp.server import create_mcp_server

mcp = create_mcp_server(MyAgent(), name="my-agent")

@mcp.tool()
async def list_publishers(region: str) -> list[dict]:
    """Vendor-specific — not in the AdCP spec."""
    return await my_db.publishers_in(region)

mcp.run(transport="streamable-http")
```

Custom tools appear in `tools/list` alongside the ADCP tools, carry
whatever schema FastMCP generates from the function signature, and do
**not** run through ADCP's spec-driven validation or the `SkillMiddleware`
chain — they're off-spec by construction. Use them for genuinely
vendor-specific surfaces; don't use them to "extend" AdCP operations
(that's what discriminated-union request subclasses are for).

`tools/list` consumers that validate against the ADCP spec will flag
custom tools as unknown. Set expectations accordingly with clients
your agent talks to.

## Request-body size cap

`serve()` installs an ASGI middleware that caps incoming request
bodies at **10 MB by default**. Bodies above the cap are rejected with
HTTP 413 at the ASGI boundary — before FastMCP or a2a-sdk parses the
JSON, and before typed-dispatch runs `model_validate`. This is the
only guard against adversarial callers exhausting validation CPU or
memory with arbitrarily large payloads.

Two layers of enforcement:

1. **Content-Length fast-fail.** If the client advertises a body size
   over the cap, the middleware rejects immediately without reading a
   byte.
2. **Streaming accounting.** For chunked transfers (no
   Content-Length), the middleware totals bytes as they arrive and
   rejects the moment the total crosses the cap.

`GET`, `HEAD`, `OPTIONS` bypass the check (no request body).

Tune via `serve(..., max_request_size=N)`:

```python
# Legitimate multi-package media buys with embedded creative assets
# can run over 10 MB. Bump the cap for those deployments.
serve(MyAgent(), max_request_size=50 * 1024 * 1024)

# Public-facing deployments that only accept small payloads can
# tighten the cap.
serve(MyAgent(), max_request_size=256 * 1024)

# Sellers with genuinely unbounded payloads (not recommended) can
# opt out entirely. You become responsible for enforcing bounds at
# a different layer — usually your reverse proxy or WAF.
serve(MyAgent(), max_request_size=0)
```

Applies to both MCP (streamable-http, sse) and A2A transports. stdio
transport skips the cap since there's no HTTP body to police.

**What this cap does NOT bound.** The middleware caps **bytes** per
request, not **duration**. A slow-loris caller sending 1 byte every
30 seconds stays under the cap forever while tying up a worker. Bound
duration at the layer above:

- `uvicorn --timeout-keep-alive N` caps keep-alive connection idle
  time (but doesn't cover request-body reads).
- Reverse-proxy read timeouts do: nginx `client_body_timeout`, Envoy
  `request_timeout`, Caddy `timeouts.read`.
- Under serverless / platform-managed runtimes (Fly.io, Cloud Run),
  the platform's per-request timeout is the effective upper bound.

For adversarial-tenant deployments, also budget memory: the middleware
buffers the full body up to the cap before replaying to the handler,
so worst-case RSS runs `workers × concurrency × max_request_size`. An
upstream reverse proxy enforcing a smaller per-connection cap is the
right lever if this is too generous.

## Idempotency

The SDK ships an `IdempotencyStore` middleware that honors the
`Idempotency-Key` header per AdCP §idempotency. Requests with the same
`(caller_identity, idempotency_key)` return the cached response instead
of re-executing the handler.

The store keys on `ToolContext.caller_identity` — if your transport
doesn't populate it, per-principal scoping falls through and dedup is
skipped (with a `UserWarning`). A2A populates it automatically from
`ServerCallContext.user`; MCP requires you to wire `context_factory`.

Don't rebuild idempotency in your handler. Import the middleware.

### Wiring `@store.wrap` (production pattern)

Decorate the mutating handler methods — `create_media_buy`,
`update_media_buy`, and any other operation your agent implements that
has side effects — with `@idempotency.wrap`:

```python
from adcp.server import ADCPHandler, IdempotencyStore, MemoryBackend, ToolContext
from adcp.server.responses import capabilities_response

idempotency = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86_400)


class MySeller(ADCPHandler):
    @idempotency.wrap
    async def create_media_buy(self, params, context: ToolContext | None = None):
        return my_create_logic(params)

    @idempotency.wrap
    async def update_media_buy(self, params, context: ToolContext | None = None):
        return my_update_logic(params)

    async def get_adcp_capabilities(self, params, context: ToolContext | None = None):
        return capabilities_response(["media_buy"], idempotency=idempotency.capability())
```

For production, swap `MemoryBackend()` for `PgBackend` (note the
import path — `PgBackend` lives in `adcp.server.idempotency`, not the
top-level `adcp.server`):

```python
from adcp.server.idempotency import PgBackend
idempotency = IdempotencyStore(backend=PgBackend(pool=pg_pool), ttl_seconds=86_400)
```

The Pg-backed store survives restarts and is shared across workers.
`PgBackend` commits the cached response atomically with your handler's
business write when both run inside the same transaction — no window
where the side effect lands
but the cache entry doesn't.

**`caller_identity` + `tenant_id` must be populated.** The store keys
its cache on `(tenant_id, caller_identity, idempotency_key)`. If
`context.caller_identity` is `None`, the middleware emits a `UserWarning`
and falls through to your handler with no dedup — repeated requests
re-execute and can double-allocate. Always wire `context_factory` on MCP
servers so the auth middleware populates these fields before the handler
runs.

## Error handling

**Handler methods return error dicts — they do not raise.** Use
`adcp_error(code)` from `adcp.server`:

```python
from adcp.server import adcp_error

async def create_media_buy(self, params, context=None):
    if params.get("budget", 0) < 500:
        return adcp_error("BUDGET_TOO_LOW", "Budget must be ≥ $500",
                          field="budget", suggestion="Increase to at least $500")
    if rate_limiter.is_over_limit(context.caller_identity):
        return adcp_error("RATE_LIMITED", retry_after=30)
    return my_create_logic(params)
```

`adcp_error` builds the spec-mandated `{"errors": [...]}` dict and
auto-populates the `recovery` field from a 20+ code table — no
hand-maintaining recovery hints. The SDK translates the returned dict to
the correct wire shape: `ToolError` on MCP, `JSON-RPC error` on A2A.

### Error-code taxonomy

| Recovery | Codes (sample) | Client action |
|---|---|---|
| `transient` | `RATE_LIMITED`, `SERVICE_UNAVAILABLE` | Retry with backoff |
| `correctable` | `BUDGET_TOO_LOW`, `INVALID_REQUEST`, `MEDIA_BUY_NOT_FOUND`, `CONFLICT` | Fix the request and resubmit |
| `terminal` | `AUTH_REQUIRED`, `ACCOUNT_NOT_FOUND`, `ACCOUNT_SUSPENDED` | Stop; require human intervention |

Full list: `adcp.server.helpers.STANDARD_ERROR_CODES`.

### `adcp_error` vs `ADCPTaskError`

`ADCPTaskError` is the exception the **client SDK** raises when it
receives an error response. Server-side handler authors never construct
or raise it. The distinction matters when you're writing both sides:

```python
# SERVER — return a structured error dict:
async def create_media_buy(self, params, context=None):
    return adcp_error("PRODUCT_NOT_FOUND", field="product_id",
                      suggestion="Use get_products to discover available products")

# CLIENT — catch the exception the SDK raises on your behalf:
try:
    await client.create_media_buy(params)
except ADCPTaskError as exc:
    if "PRODUCT_NOT_FOUND" in exc.error_codes:
        products = await client.get_products(...)
```

Custom error codes (outside `STANDARD_ERROR_CODES`) default to
`recovery="terminal"`. Override with `adcp_error("MY_CODE", recovery="correctable")`.

## Response builders

Manual `model_dump()` on response Pydantic objects is error-prone —
you'll drift from the spec's required fields. Use the response builders:

```python
from adcp.server.responses import media_buy_response, products_response

return media_buy_response(
    media_buy_id="mb_123",
    status="active",  # auto-populates valid_actions from the state machine
)
```

One per AdCP operation. Read the `adcp.server.responses` docstrings.

## Multi-tenant typing

Production multi-tenant agents usually carry `tenant + principal +
adapter + testing hooks` in their own identity type. `ToolContext`
exposes the fields those handlers need:

- `ToolContext.tenant_id: str | None` — first-class field; populate from
  your `context_factory`. **Required** for multi-tenant deployments
  whose principal IDs are only unique within a tenant (Okta group-scoped,
  SCIM per-tenant, seller-internal employee IDs) — the idempotency
  store keys its cache on `(tenant_id, caller_identity)`, so leaving
  `tenant_id` unset collapses distinct tenants into the same scope and
  enables cross-tenant response replay.
- `ToolContext.metadata: dict[str, Any]` — escape hatch for adapter
  instance handles, testing hooks, per-tenant config blobs.
- Subclassing `ToolContext` is supported — return the subclass from your
  `context_factory` and your handler methods `isinstance(context,
  MyContext)` (or `cast(MyContext, context)` if you've established the
  invariant via the factory) to reach the extra fields.
- `AccountAwareToolContext` is a shipped subclass that adds
  `account_id` + `account` for handlers that need per-request account
  scope. Pair it with `resolve_account_into_context(params, context,
  resolver)` to collapse the standard three-line boilerplate.

When in doubt, subclass: `metadata: dict[str, Any]` loses type safety.

For the full set of scope invariants — what each field means, how
cache keys are composed, what leaks if you populate fields wrong — see
[docs/multi-tenant-contract.md](./multi-tenant-contract.md).

## Account modes and mock-mode upstream routing

The framework recognizes three operationally distinct account modes
([proposal](https://github.com/adcontextprotocol/adcp-client/blob/main/docs/proposals/lifecycle-state-and-sandbox-authority.md)):

- `mode='live'` — production traffic. Adopter's upstream is truth.
- `mode='sandbox'` — adopter's own test infrastructure (test DB, test
  GAM tenant). Test-only surfaces (`comply_test_controller`) are
  admitted; everything else flows through the adopter's normal code
  path.
- `mode='mock'` — points the adapter's HTTP client at a per-tenant
  mock-server fixture URL. Adapter business logic runs unchanged;
  only the upstream URL changes per request.

Adopters mark accounts with their mode in `AccountStore.resolve`. The
framework reads `account.mode` to gate test-only surfaces and route
upstream HTTP.

### Adapter URL contract

Adapters declare their production upstream URL on the platform class.
The URL is fixed per platform — credentials and per-tenant routing
flow through `ctx.auth_info` and `ctx.account.metadata`, not through
the URL.

```python
class GAMPlatform(DecisioningPlatform, SalesPlatform):
    upstream_url = "https://googleads.googleapis.com"
    ...

    async def get_products(self, req, ctx):
        client = self.upstream_for(ctx, auth=self._auth_for(ctx))
        # client._base_url == upstream_url for mode='live'/'sandbox';
        # client._base_url == account.metadata['mock_upstream_url']
        # for mode='mock'.
        data = await client.get("/v1/products")
        ...
```

`upstream_for(ctx)` returns a cached `UpstreamHttpClient` keyed by
`(base_url, id(auth))`. Connection pooling stays correct across
requests; different tenants with different auth strategies get
distinct clients.

### Mock-mode account fixture URLs

Mock-mode accounts populate `metadata['mock_upstream_url']` in their
`AccountStore.resolve` return value:

```python
ExplicitAccounts(loader=lambda aid: Account(
    id=aid,
    mode="mock",
    metadata={"mock_upstream_url": "http://localhost:4500"},
))
```

The mock-server is per-specialism (`bin/adcp.js mock-server
<specialism>`, default port 4500). Adopters or CI start it before
running storyboards. The SDK does not manage the mock-server's
lifecycle — it just points the adapter at the URL.

### Fail-closed cases

`upstream_for(ctx)` raises `AdcpError(code='CONFIGURATION_ERROR')`
when:

- `mode='mock'` and `metadata['mock_upstream_url']` is missing /
  empty / non-string. Fix in `AccountStore.resolve`.
- `mode='live'` / `mode='sandbox'` and `platform.upstream_url is
  None`. Set the class attribute, or mark the account `mode='mock'`.

`recovery='terminal'` — buyers can't fix this; only the adopter's
deployment can.

### Worked example

See `examples/hello_mock_seller.py` for a single platform with four
accounts demonstrating all four routes (live, sandbox, mock-A,
mock-B).

## A2A transport

`serve(MyAgent(), transport="a2a")` wires the same handler through the
A2A protocol with auto-generated agent card (`/.well-known/agent.json`)
derived from the `ADCPHandler` methods your class overrides.

### Durable task storage

A2A tracks each long-running operation as a `Task` — the default
`InMemoryTaskStore` keeps them in a process-local dict. That's fine for
demos but tasks vanish on restart and don't share across workers.
Production agents inject a durable `TaskStore`:

```python
from adcp.server import serve
from examples.a2a_db_tasks import SqliteTaskStore

serve(
    MyAgent(),
    transport="a2a",
    task_store=SqliteTaskStore("/var/lib/myagent/tasks.db"),
)
```

The `task_store=` kwarg accepts any `a2a.server.tasks.task_store.TaskStore`
subclass. `examples/a2a_db_tasks.py` is a runnable reference SQLite
implementation; swap in asyncpg / aiomysql / Redis for multi-node
deployments. For maximum correctness, implement the store against the
same engine/transaction as your handler's business writes so
"handler success → task save" happens atomically.

**Four things a durable `TaskStore` MUST do — the
`InMemoryTaskStore` got away with ignoring these because crash =
reset; your persistent store can't:**

1. **Filter every read, write, and delete by the authenticated
   principal.** The `TaskStore` ABC hands you a `ServerCallContext` on
   every call; a2a-sdk's `DefaultRequestHandler` always passes it. If
   your `get(task_id, context)` ignores `context.user`, any principal
   that learns another tenant's task id retrieves that tenant's task —
   history, artifacts, PII, all of it. The reference `SqliteTaskStore`
   derives a `scope` column from `context.user.user_name`; override
   `_scope_from_context` if you carry richer identity.
2. **Protect the database file.** Tasks include buyer-supplied
   `Message.parts` content and artifact metadata. On a shared host the
   default umask leaves the database world-readable. Set `0o600` on
   creation (reference does this), mount on an encrypted volume, and
   treat backups as the same trust boundary as the live DB.
3. **Handle concurrent writes explicitly.** Two workers saving the
   same task interleave. `INSERT OR REPLACE` is last-writer-wins and
   will silently revert state (`completed` → `working`). Add a version
   column, a `WHERE updated_at < ?` guard, or wrap updates in a
   transaction with explicit conflict handling.
4. **Garbage-collect terminal tasks.** Without a TTL / sweeper, your
   database grows unbounded and every completed task is retained
   forever — an ever-expanding exfiltration target. Add a periodic
   sweep deleting tasks in `completed` / `canceled` / `failed` states
   older than your retention policy.

### Durable push-notification config storage

Clients subscribe to task progress by calling
`tasks/pushNotificationConfig/set`. a2a-sdk's default behavior is
**push-notif disabled** — the endpoint surfaces
`UnsupportedOperationError` until you wire a store. Sellers that accept
push-notif subscriptions pass one:

```python
from adcp.server import serve
from examples.a2a_db_tasks import (
    SqliteTaskStore,
    SqlitePushNotificationConfigStore,
)

serve(
    MyAgent(),
    transport="a2a",
    task_store=SqliteTaskStore("/var/lib/myagent/tasks.db"),
    push_config_store=SqlitePushNotificationConfigStore(
        "/var/lib/myagent/push_configs.db"
    ),
)
```

**Three things a durable push-notification config store MUST do —
beyond the four from the TaskStore section above:**

1. **Validate the client-supplied `url` against an allowlist before
   persisting.** a2a-sdk's push-notif sender POSTs full task JSON to
   whatever URL is stored, with no built-in validation. An attacker
   registering `url=http://169.254.169.254/…` (cloud metadata) or
   `http://localhost:5432/` (internal services) gets SSRF +
   exfiltration in one call — the task JSON that lands on the
   attacker's server includes `history` and `artifacts`. The
   reference impl does NOT validate URLs; the seller's store (or
   a pre-persist hook) must. Reject non-https, reject RFC 1918 /
   IPv6 link-local, and require the host match an egress allowlist
   before `set_info` writes anything.
2. **Treat `PushNotificationConfig.authentication.credentials` and
   `PushNotificationConfig.token` as secrets at rest.** Clients pass
   bearer tokens / shared secrets so the agent's callbacks can
   authenticate. The reference impl serialises them to plaintext JSON
   under `chmod 0o600` — safe on a single-user host but that
   guarantee doesn't survive backups, Docker bind mounts with wrong
   umask, DB-to-Postgres migrations, or shared-volume mounts.
   Production stores should envelope-encrypt those fields, or persist
   opaque references and keep the secrets in a dedicated backend
   (Vault, AWS KMS, GCP Secret Manager).
3. **Scope by principal, not just by tenant.** a2a-sdk's ABC doesn't
   pass a `ServerCallContext` to push-config methods, so scoping has
   to happen out-of-band. The reference `SqlitePushNotificationConfigStore`
   reads a `ContextVar` your auth middleware populates and writes a
   `scope` column on every row. Cross-scope isolation works; **within
   a scope, multiple principals can still overwrite each other's
   configs** (same `(scope, task_id)`, client omits `config_id`, PK
   collision). For multi-principal-per-tenant deployments, widen the
   scope to include the principal (e.g. `f"{tenant}:{principal}"`) or
   require clients to supply an explicit `config_id`.

**Scoping caveat.** The reference impl's ContextVar approach has a
known gap: a2a-sdk's push-notif sender runs in a background
`asyncio.Task` that inherits the ContextVar snapshot from
task-creation time. If the seller's auth middleware has already reset
the ContextVar before the sender reads it, `get_info` returns empty
and notifications silently drop. Sellers running non-blocking
push-notifs must propagate scope into the sender path explicitly —
either capture the scope at `set_info` time and stash it alongside
the config, or override a2a-sdk's `BasePushNotificationSender` to
re-set the ContextVar before calling `get_info`. Not yet addressed in
the SDK.

**Operator-facing failure modes.** When `scope_provider` returns
`None`, the reference store falls through to an `__anonymous__`
bucket and emits a one-time `UserWarning`. Silent fall-through would
share one push-notif bucket across every unauthenticated caller. The
warning is the signal your auth middleware isn't populating the
ContextVar — treat it as a P0.

### Per-skill middleware (audit, activity feeds, rate limiting, tracing)

Every skill dispatch — on **both** the MCP and A2A transports — can be
wrapped in a chain of middleware callables. Pass them as
`middleware=[...]` to `create_mcp_server` / `create_a2a_server` /
`serve` — first entry wraps outermost, matching Starlette/ASGI
ordering. The same list works across transports; write once, apply to
both:

```python
from adcp.server import SkillMiddleware, ToolContext, serve

async def audit_middleware(
    skill_name: str,
    params: dict,
    context: ToolContext,
    call_next,
) -> Any:
    started = time.monotonic()
    try:
        result = await call_next()
    except Exception as exc:
        audit_log.failure(skill_name, context.caller_identity, exc)
        raise
    audit_log.success(
        skill_name,
        context.caller_identity,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    return result

# Works on MCP:
serve(MyAgent(), middleware=[audit_middleware])

# Same middleware list, A2A transport:
serve(MyAgent(), transport="a2a", middleware=[audit_middleware])
```

**Semantics worth knowing:**

- **Composition — put audit outermost.** `middleware=[Audit(),
  RateLimit(), Metrics()]` runs `Audit → RateLimit → Metrics →
  handler` on the way in and unwinds in the opposite order. **If you
  put rate-limiting before audit, rejected requests disappear from
  your audit log** — often the most interesting events for security
  review. Audit always outermost.
- **Short-circuit — cache keys MUST include principal + tenant.** A
  middleware that returns without calling `call_next()` stops the
  chain; its return value becomes the dispatch result. Rate limiters
  / feature flags use this. **Caching middleware that short-circuits
  must key on `(skill_name, params, context.caller_identity,
  context.tenant_id)`** — a cache keyed only on `skill_name + params`
  serves principal A's data to principal B on a matching-params call.
- **Exception observation — never swallow an `ADCPError`.** Catch
  around `await call_next()` to log failures. Re-raise to let the
  executor's normal error path take over (`ADCPError` → failed task
  with `adcp_error` DataPart; other exceptions → opaque failed task).
  Swallowing an `ADCPError` (especially `IdempotencyConflictError` or
  `ADCPTaskError`) and returning a fake-success dict silently converts
  a rejected mutation into a "completed" task — double-billing,
  double-allocation, duplicated side effects. Don't.
- **Exception messages end up in server logs.** Middleware-raised
  exceptions flow through `logger.exception` in the executor before
  client-facing sanitisation. Don't format `params` or
  `context.caller_identity` into exception text — operators read those
  logs.
- **Retry is supported.** Call `call_next()` more than once (e.g.
  retry-on-transient-error middleware). Each call gets a fresh
  inner chain — composition is re-entrant by design.
- **Transform on return, not on input.** `params` passed in is the
  same dict every middleware sees. Mutating it doesn't change what
  the next layer receives. Transforms happen on the *return* side by
  modifying the value of `await call_next()`.
- **Context access**: the middleware sees the `ToolContext` produced
  by the `context_factory` (or the a2a-sdk fallback). Tenant id,
  caller identity, anything your factory populates. `ContextVar`s set
  before `call_next()` propagate to the handler — no `asyncio.create_task`
  needed.

**Security — middleware is a data processor for the full skill
payload.** `params` carries decoded buyer briefs, budgets, brand
refs, proposal text, PII in message parts. `context` carries
`caller_identity` + `tenant_id`. Installing a third-party middleware
(SaaS audit, observability vendor, bespoke tracing) hands that vendor
the complete skill surface. Treat it as a data processor under your
GDPR/CCPA controller-processor agreements.

`SkillMiddleware` applies on both transports — pass the same list to
`create_mcp_server(middleware=...)` and `create_a2a_server(middleware=...)`,
or to `serve(middleware=...)`. Per-transport HTTP middleware (the
`BearerTokenAuthMiddleware` from Pattern 2 above, for instance) is a
separate concern — HTTP middleware runs before JSON-RPC decode;
`SkillMiddleware` runs after skill dispatch is resolved.

### Alternative A2A wire formats

The default `ADCPAgentExecutor` parses incoming messages expecting
`DataPart(data={"skill": "<name>", "parameters": {...}})` with a
TextPart JSON fallback. Sellers fronting clients that send a
different shape (JSON-RPC 2.0 bodies, vendor-specific DataParts, bare
TextPart with a different skill layout) can pass a custom
`message_parser`:

```python
from adcp.server import MessageParser, create_a2a_server

def my_parser(context):
    # Parse your wire shape; return (skill_name, params) or (None, {}).
    msg = context.message
    ...
    return skill_name, params

app = create_a2a_server(MyAgent(), message_parser=my_parser)
```

Compose with the default when accepting both shapes — call
`ADCPAgentExecutor._default_parse_request` as a fallback after your
parser returns `(None, {})` for legacy clients.

### Known gaps

All three Phase-2 A2A hooks (#224 TaskStore, #225 PushNotificationConfigStore,
#226 SkillMiddleware) have landed. A2A adoption now reaches parity with
MCP for production agents.

## Webhooks

When `auto_emit_completion_webhooks=True` (the default), the framework fires a
sync-completion webhook after every successfully-dispatched tool call whose task
type is in the spec's webhook-eligible set (`create_media_buy`, `activate_signal`,
and their siblings). Buyers who register `push_notification_config.url` receive
these notifications automatically.

The framework requires a sender or supervisor at boot — it raises `AdcpError`
rather than silently dropping notifications if neither is wired and auto-emit is on.
Set `auto_emit_completion_webhooks=False` only if you emit webhooks manually inside
your platform methods.

### Sender constructors

Pick one per `WebhookSender` instance. All three share the same
`send_mcp(url, task_id, status, ...)` delivery API.

| Constructor | Auth mode | When to use |
|---|---|---|
| `WebhookSender.from_jwk(jwk)` | RFC 9421 HTTP-signature | AdCP-conformant buyers; spec baseline (`kid`/`alg`/`adcp_use` live in the JWK dict) |
| `WebhookSender.from_bearer_token(token)` | `Authorization: Bearer` | Simplest; no key management; requires TLS |
| `WebhookSender.from_standard_webhooks_secret(secret, key_id=...)` | Standard Webhooks v1 | Svix / Resend / standardwebhooks.com receivers |

### Sender vs. supervisor

`WebhookSender` is the transport layer — it constructs and signs one HTTP POST.
`InMemoryWebhookDeliverySupervisor` wraps a sender and adds retry with exponential
backoff, per-endpoint circuit breakers, and an audit log. Pass
`webhook_supervisor=` in production so transient receiver outages don't cause
missed notifications.

```python
import os
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor
from adcp.decisioning import serve

sender = WebhookSender.from_bearer_token(os.environ["WEBHOOK_BEARER_TOKEN"])
supervisor = InMemoryWebhookDeliverySupervisor(sender=sender)
serve(my_platform, webhook_supervisor=supervisor)
```

For the full constructor reference and a migration table from legacy HMAC / bare
`requests.post` patterns, see
[`docs/webhooks/migration-from-fragmented-senders.md`](webhooks/migration-from-fragmented-senders.md).
See `examples/hello_seller_with_webhooks.py` for a runnable end-to-end wiring example.

## Testing

The integration test pattern in `tests/test_mcp_middleware_composition.py`
is the shape you can copy for your own middleware tests. Key pieces:

- `create_mcp_server(..., context_factory=build_context)` wires the
  context factory.
- `mcp.settings.stateless_http = True` + `mcp.settings.json_response = True`
  disables the session manager so tests don't need a TaskGroup.
- `mcp.settings.transport_security.allowed_hosts = ["localhost"]` allows
  in-process `httpx.ASGITransport` requests through the DNS-rebinding
  guard.
- Run the app's lifespan manually if you're exercising HTTP endpoints.

## Testing hooks — storyboard + header-driven composition

Two orthogonal test-runtime shapes exist in the wild. Compose them via
the same `context_factory` you already wire for auth:

**Storyboard-driven** (SDK-native). Sellers register a
`TestControllerStore` and clients invoke the `comply_test_controller`
skill with a scenario name (`force_media_buy_status`, `simulate_delivery`,
etc.). This is the AdCP spec's compliance-test shape and what the
conformance suite exercises.

**Header-driven** (downstream pattern, e.g. salesagent's
`AdCPTestContext.from_headers(request.headers)`). Clients pass HTTP
headers like `X-AdCP-Test-Mode: slow` and the server adjusts mock
behavior. Useful for scenario-wide state that doesn't fit the
storyboard frame — "every update in this request returns pending",
"this request simulates a delayed ad server".

Before SDK 3.x you had to pick one. As of #227 both compose through
the existing `context_factory`:

```python
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware

from adcp.server import RequestMetadata, ToolContext, create_mcp_server
from adcp.server.test_controller import (
    TestControllerStore,
    register_test_controller,
)

# 1. ContextVar the HTTP middleware populates from request headers.
_test_context: ContextVar[AdCPTestContext | None] = ContextVar(
    "test_context", default=None
)


# 2. Starlette middleware reads headers into the ContextVar per request.
#    Always reset the token in a finally block — otherwise the set
#    value leaks into the next request that reuses this asyncio task
#    (cross-request state bleed; see PR #232's cross-tenant idempotency
#    scoping for the analogous failure mode).
class TestHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        token = _test_context.set(AdCPTestContext.from_headers(request.headers))
        try:
            return await call_next(request)
        finally:
            _test_context.reset(token)


# 3. context_factory snapshots the ContextVar onto ToolContext.
def build_context(meta: RequestMetadata) -> ToolContext:
    return ToolContext(
        metadata={"test_context": _test_context.get()},
    )


# 4. Store methods that want header-driven state accept `context`.
class MyStore(TestControllerStore):
    async def force_media_buy_status(
        self,
        media_buy_id: str,
        status: str,
        rejection_reason: str | None = None,
        *,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        test_ctx = (context.metadata.get("test_context") if context else None)
        if test_ctx and test_ctx.slow_ad_server:
            status = "pending"  # header-driven behavior override
        self.media_buys[media_buy_id] = status
        return {"previous_state": "active", "current_state": status}


# 5. Wire the same factory into both create_mcp_server AND
#    register_test_controller. Regular handler methods and
#    comply_test_controller both see the same context.
mcp = create_mcp_server(MySeller(), name="my-agent", context_factory=build_context)
register_test_controller(mcp, MyStore(), context_factory=build_context)

app = mcp.streamable_http_app()
app.add_middleware(TestHeaderMiddleware)
```

**Backward compatibility**: stores whose methods don't declare
`context` keep working. The dispatcher inspects the signature and
only passes `context` to methods that opt in. `serve(..., test_controller=...)`
automatically threads `context_factory` through, so no extra wiring is
needed if you use the `serve()` helper.

**When to pick which**: the storyboard skill is for spec-level
compliance tests (scenarios named by the AdCP test suite). Headers are
for your own mock-ad-server behaviors that sit outside the spec.
Sellers typically need both.

## What not to build

- Don't write per-tool `@mcp.tool()` wrappers. `create_mcp_server()`
  registers all ADCP tools from a handler automatically.
- Don't hand-maintain an agent card. A2A auto-derives it from the
  handler methods you override.
- Don't reinvent `IdempotencyStore`, response builders, or error
  classification. Use the shipped helpers.
- Don't import from `adcp.types.generated_poc.*`. Everything public
  lives at `adcp.types` or `adcp` — and the internal paths renumber
  between releases (see `MIGRATION_v3_to_v4.md`).

## Troubleshooting

**Idempotency dedup isn't firing — repeated creates still execute.**

Check that `context.caller_identity` is non-`None` when the handler
runs. The idempotency middleware silently falls through (with a
`UserWarning` in server logs) when it can't scope the cache namespace.
On MCP servers, this means `context_factory` is absent or returns a
`ToolContext` without `caller_identity`. On A2A servers, it means the
request arrived without a `ServerCallContext.user`. Fix: wire
`context_factory=auth_context_factory` on `create_mcp_server`, and
ensure your `validate_token` returns a `Principal` with
`caller_identity` set.

**`context_factory` returned a plain `dict` and now the handler explodes
with `AttributeError: 'dict' object has no attribute 'caller_identity'`.**

`context_factory` must return a `ToolContext` instance (or a subclass),
not a dict. The SDK's dispatcher reads `context.caller_identity`,
`context.tenant_id`, and any subclass fields as attributes. Returning a
dict is a type error at dispatch time. Fix: return
`ToolContext(caller_identity=..., tenant_id=...)` or your own subclass.

**`tools/list` returns an empty tool list (or just `get_adcp_capabilities`).**

By default the SDK only advertises tools whose handler methods your
subclass actually overrides. A handler that overrides only
`get_adcp_capabilities` + `get_products` surfaces exactly those two.
If you expect all 57 spec tools to appear for a storyboard client,
pass `advertise_all=True` to `serve()` / `create_mcp_server()`.

**`validate_discovery_set` raises `ValueError` listing a tool I know is valid.**

The function checks that every name in the extended set is either in
`DISCOVERY_TOOLS` or an AdCP-defined pre-auth name it recognises. If
you added a vendor-specific handshake tool, the function can't
auto-classify it. Pass the validated set directly to your middleware's
discovery bypass and skip `validate_discovery_set` for your extension
names, or file an issue to add the name to the shipped default.

**Handler raises `AuthenticationRequired` but the client sees `500 Internal Server Error`.**

`AuthenticationRequired` (or any exception that isn't an `ADCPError`
subclass) is translated to an opaque 500 by the executor — intentional,
to avoid leaking server internals. Return `adcp_error("AUTH_REQUIRED")`
instead; the SDK maps it to an authenticated-but-rejected error shape the
client can handle programmatically.

## Where to look next

- `examples/minimal_sales_agent.py` — handler-only starting point.
- `examples/mcp_with_auth_middleware.py` — full auth + typed context
  via `BearerTokenAuthMiddleware`. Foundation for Pattern 2b; bring
  your own subdomain-routing middleware on top.
- `src/adcp/server/responses.py` — response builder reference.
- `src/adcp/server/helpers.py` — error codes, state machine, account
  resolution.
- `tests/test_mcp_middleware_composition.py` — the integration test
  that protects this contract.
