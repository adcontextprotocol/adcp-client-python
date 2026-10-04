# Non-bearer principal resolution

A seller can require bearer credentials for some tenants while accepting
identities established by a trusted proxy or mTLS boundary for others. Configure
one optional resolver on `BearerTokenAuth`; `serve(auth=..., transport="both")`
uses it on both MCP and A2A. Pass `context_factory=auth_context_factory` (from
`adcp.server`) to propagate the installed identity to handler contexts, as with
bearer auth. Leave `allow_unauthenticated=False` (the default) so
unrecognized callers still receive HTTP 401.

```python
from adcp.server import AuthRequest, BearerTokenAuth, Principal, PrincipalResolverError

async def resolve_principal(request: AuthRequest) -> Principal | None:
    if request.state.get("verified_proxy_forbidden") is True:
        raise PrincipalResolverError(403)
    principal = request.state.get("verified_proxy_principal")
    return principal if isinstance(principal, Principal) else None

auth = BearerTokenAuth(
    validate_token=validate_token,
    resolve_principal=resolve_principal,
)
```

The outer middleware in this example verifies the proxy connection or client
certificate before installing `verified_proxy_principal`. The SDK does not trust
caller identity headers automatically. A resolver can validate metadata itself,
and can await a database or identity provider lookup. Sync callbacks and callable
objects are also supported. See the [typed example](../examples/principal_resolver.py).

## Request view

`AuthRequest` provides `transport`, `method`, `url`, case-insensitive `headers`
(including `getlist` for duplicates), `query_params`, `client`, `state`, and optional
`tls` metadata from the ASGI server's TLS extension. It provides no body accessor,
receive channel, or raw mutable scope. Resolver construction does not read the
body. Existing MCP discovery inspection and opt-in A2A skill discovery continue
to buffer/replay their bodies as before.

`state` and `tls` are read-only snapshots of the top-level mappings. Values are
not deep-copied. Header/state/TLS/query values are omitted from the view's repr.
This view is identical on both legs; the callback does not receive Starlette's
`Request` on one leg and a raw ASGI dict on the other.

## Credential precedence and rejection

The resolver runs only when **every accepted credential header is absent**.
Authorization, configured aliases, and custom per-leg header names all count.
Valid bearer credentials select the bearer principal and skip the resolver;
invalid, empty, malformed, or conflicting credentials receive 401 and also skip
it. Identical decoded duplicate credentials are accepted. To authenticate using
non-bearer identity, omit bearer/alias headers entirely.

Returning `Principal` authenticates the request. Returning `None` applies the
usual missing-bearer policy, including explicitly configured discovery and
`allow_unauthenticated` behavior. Raising `PrincipalResolverError(401)` or
`PrincipalResolverError(403)` rejects regardless of that opt-in. Responses contain
only generic `unauthenticated`/`forbidden` error bodies. A 401 carries the shared
Bearer challenge; 403 does not. Other resolver exceptions and invalid return
values fail closed with generic 401; auth logs contain only coarse reason codes,
never callback exception text or credential values.

This deliberately chooses bearer-absent resolution over the issue's suggested
resolver-first order. It prevents invalid bearer credentials from obtaining a
fallback principal, and avoids a second identity and conflict/precedence policy
when bearer credentials are supplied. There is no policy dial that silently
accepts conflicting credentials.

## Identity propagation and lifecycle

The framework installs the caller identity, tenant and copied metadata in the
existing ContextVars and request state on both legs, and in A2A's `scope['user']`
and `scope['auth']`. With `auth_context_factory`, handlers receive the same
`ToolContext` and `(tenant_id, caller_identity)` idempotency scope on either leg,
including stateful MCP sessions. `AuthInfo.kind` is `derived` for resolved
principals and remains `bearer` for bearer principals. Credentials are not
synthesized or propagated upstream. ContextVars reset in `finally`, including
when handlers fail, and each HTTP request carries its own state.

Verified RFC 9421 signers retain their existing identity overlay and skip
non-bearer resolution. When signature verification admits an unsigned request
only on condition that bearer auth succeeds, a resolver cannot satisfy that
condition: a valid bearer is still required. Public MCP handshake methods and
A2A agent-card/CORS preflight routes retain their existing exemptions.

The separate MCP `allow_unauthenticated` repair preserves a principal explicitly
installed by outer middleware in request state or ContextVars. Request state,
including explicitly anonymous state, takes precedence. This compatibility
setting still permits anonymous requests globally; prefer a resolver for mixed
bearer/proxy tenants. Middleware establishing ContextVars must reset its own
values in `finally`.

## Migration

Replace process-local synthetic bearer tokens with a resolver that returns the
verified principal directly. Keep bearer validation for bearer tenants, omit
credential headers on the proxy path, and keep unauthenticated access disabled.
Async token validators now work on A2A as well as MCP; remove synchronous bridge
wrappers when they are no longer needed.
