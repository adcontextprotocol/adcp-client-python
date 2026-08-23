"""Bearer-token HTTP authentication middleware for ADCP MCP servers.

`examples/mcp_with_auth_middleware.py` is the full, load-bearing
recipe for multi-tenant sellers. Five things have to be right at the
same time — a ``request.state`` carrier that survives the stateful
streamable-http session boundary, a ContextVar fallback for stateless
mode and A2A, constant-time token compare, the AdCP/MCP
discovery-method bypass, and reset-in-finally to prevent cross-request
leak. Getting any of them wrong is a security incident. This module
factors that recipe into a middleware class + matching
``context_factory`` so sellers write four lines of wiring instead of
four pages of auth code.

Typical usage::

    from adcp.server import create_mcp_server
    from adcp.server.auth import (
        BearerTokenAuthMiddleware,
        Principal,
        auth_context_factory,
    )

    async def validate_token(token: str) -> Principal | None:
        row = await db.fetch_token(token)
        if row is None or row.revoked:
            return None
        return Principal(
            caller_identity=row.principal_id,
            tenant_id=row.tenant_id,
        )

    mcp = create_mcp_server(MyAgent(), context_factory=auth_context_factory)
    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)

The middleware populates module-level ``ContextVar``s that
``auth_context_factory`` reads to build a
:class:`~adcp.server.ToolContext` per call. The same module-level
vars compose with any other auth layer a seller writes on top — e.g.,
an additional role-check middleware that reads
:data:`current_principal`.

Security invariants the middleware enforces:

* Tokens are compared with :func:`hmac.compare_digest` over SHA-256
  hashes, not raw string equality — :meth:`dict.__contains__` leaks
  match-prefix timing.
* ``initialize`` and ``tools/list`` (MCP handshake) plus
  ``get_adcp_capabilities`` (AdCP handshake) are exempt per spec;
  every other request requires a valid bearer token.
* ``ContextVar``s are reset in ``finally`` so a later task sharing the
  context can't read a stale principal.
* The JSON-RPC body is peeked but not consumed — downstream handlers
  still read the same bytes (Starlette caches the body via the
  ``_body`` attribute on the request).

What this middleware does NOT do:

* **Token storage.** You supply ``validate_token``; where tokens live
  (Postgres, Redis, Vault, an IdP) is yours to design.
* **Authorization.** The middleware answers "who is this?", not "can
  they do X?". Authorization checks run on the authenticated principal
  inside your handlers or as :data:`~adcp.server.SkillMiddleware`.
* **A2A auth.** A2A uses a different transport; the same
  :class:`BearerTokenAuth` config object drives both legs when wired
  via :func:`adcp.server.serve`'s ``auth=`` kwarg. The A2A side is
  authenticated by a :class:`BearerTokenContextBuilder` plumbed into
  ``a2a-sdk``'s ``create_jsonrpc_routes(context_builder=...)`` seam,
  not by a Starlette middleware — that placement bypasses the
  ``/.well-known/agent-card.json`` route automatically (which is
  registered separately and never invokes the builder), satisfying
  A2A spec §4.1's mandate that the agent card be publicly accessible.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import warnings
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

_V = TypeVar("_V")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from adcp.server.base import ToolContext
from adcp.server.mcp_tools import DISCOVERY_METHODS, DISCOVERY_TOOLS

logger = logging.getLogger("adcp.server.auth")


def _parse_bearer_header(header: str) -> str | None:
    """Parse ``Authorization: Bearer <token>`` per RFC 7235.

    Scheme comparison is case-insensitive and tolerates folded
    whitespace (any run of spaces, tabs, or newlines) between the
    scheme and the token — some clients send ``bearer`` (lowercase),
    ``Bearer\\t``, or ``Bearer  <token>`` (double space). Returns
    ``None`` when the scheme doesn't match or the token is empty /
    whitespace-only.
    """
    parts = header.split(maxsplit=1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


if TYPE_CHECKING:
    from starlette.requests import Request

    from adcp.server.serve import RequestMetadata


# RFC 6750 §3 challenge string emitted on every 401 from MCP and A2A
# legs. Realm value is shared (``"adcp"``) because per RFC 7235 §2.2
# the realm identifies the protection space, and both transports
# proxy to the same ``BearerTokenAuth.validate_token`` — a buyer agent
# caching credentials by realm should treat the two as one space and
# not re-prompt on transport switch. Error code is ``invalid_token``;
# the missing-token vs invalid-token distinction (RFC 6750 §3.1) is a
# separate refinement deferred to a follow-up.
_WWW_AUTHENTICATE_CHALLENGE = 'Bearer realm="adcp", error="invalid_token"'


@dataclass(frozen=True)
class Principal:
    """An authenticated principal — the result of token validation.

    Returned by a :data:`TokenValidator` on success. Used to populate
    the transport-layer ``ContextVar``s that :func:`auth_context_factory`
    reads when building per-call :class:`~adcp.server.ToolContext`.

    :param caller_identity: Stable, globally-unique principal id within
        the tenant. See the
        :class:`~adcp.server.ToolContext.caller_identity` docstring for
        the stability contract and the failure mode when this is
        reused across logical principals.
    :param tenant_id: Tenant the principal belongs to. Populate unless
        your principal ids are globally unique across tenants — the
        server-side idempotency store scopes cache keys on
        ``(tenant_id, caller_identity)``. See
        :doc:`/multi-tenant-contract` for the full invariants.
    :param metadata: Optional extra fields the context_factory should
        propagate into :class:`~adcp.server.ToolContext.metadata`.
    """

    caller_identity: str
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SyncTokenValidator(Protocol):
    """Synchronous token validator — ``def validate_token(token) -> Principal | None``."""

    def __call__(self, token: str) -> Principal | None: ...


class AsyncTokenValidator(Protocol):
    """Asynchronous token validator —
    ``async def validate_token(token) -> Principal | None``."""

    def __call__(self, token: str) -> Awaitable[Principal | None]: ...


TokenValidator = SyncTokenValidator | AsyncTokenValidator
"""Seller-supplied callable that validates a bearer token.

Called with the raw token string (``Authorization: Bearer <token>``
with the prefix already stripped). Return a :class:`Principal` on
success, ``None`` to reject. Sync and async callables are both
accepted — the middleware awaits the result when it's awaitable.

Declared as a union of two Protocols (rather than a
``Callable[[str], Principal | None | Awaitable[...]]`` alias)
because mypy narrows Protocol unions per-call-site: downstream code
using ``async def validate_token`` gets the async branch without
``type: ignore`` noise. Either protocol is a valid ``TokenValidator``.

**Do not raise on invalid tokens.** Exceptions become ``500 Internal
Server Error`` responses, which leak the presence of an auth path
to attackers who can't know a valid token. Return ``None`` instead.
"""


# Module-level ``ContextVar``s populated by the middleware, read by the
# matching ``context_factory``. Exported so sellers can read them from
# their own composed middleware layers (rate-limiter keyed by
# principal, per-tenant feature flags, etc.) without re-authenticating.
#
# Named ``current_*`` to match the FastAPI / Starlette convention for
# per-request state carriers. Keep ``default=None`` so a pre-auth or
# discovery-exempt request reads ``None`` instead of raising
# ``LookupError``.
current_principal: ContextVar[str | None] = ContextVar("adcp_auth_principal", default=None)
current_tenant: ContextVar[str | None] = ContextVar("adcp_auth_tenant", default=None)
current_principal_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
    "adcp_auth_principal_metadata", default=None
)
current_transport: ContextVar[Literal["mcp", "a2a"] | None] = ContextVar(
    "adcp_transport", default=None
)


# Well-known ``request.state`` attribute names. The middleware writes
# these alongside the ContextVars; ``auth_context_factory`` reads them
# off the request reachable via :class:`RequestMetadata.request_context`.
# The state path is the only auth-propagation channel that survives the
# stateful streamable-http session boundary — the session task is a
# separate async task from the middleware's, so the ContextVars set
# above are invisible during dispatch in stateful mode. Stateless mode
# happens to share the dispatch context, so the ContextVar path also
# works there. The factory reads request.state first and falls back to
# the ContextVar so adopters running on stateless mode don't have to
# change anything.
REQUEST_STATE_PRINCIPAL = "adcp_auth_principal"
REQUEST_STATE_TENANT = "adcp_auth_tenant"
REQUEST_STATE_PRINCIPAL_METADATA = "adcp_auth_principal_metadata"
REQUEST_STATE_ROUTED_TENANT = "adcp_routed_tenant"

# Private metadata keys used by tenant-aware adopters at the skill-dispatch
# boundary.  They deliberately distinguish an authenticated principal whose
# token omitted ``tenant_id`` from an unauthenticated request: the former has
# ``AUTHENTICATED_TENANT_METADATA_KEY`` present with a value of ``None``.
AUTHENTICATED_TENANT_METADATA_KEY = "adcp.authenticated_tenant_id"
ROUTED_TENANT_METADATA_KEY = "adcp.routed_tenant_id"

# The stateful MCP session manager uses this request-scope bit to distinguish
# the middleware's deliberately anonymous discovery bypass from an anonymous
# non-discovery request.  Keeping the decision here preserves per-instance
# discovery overrides and subclassed ``is_discovery_request`` policies.
REQUEST_SCOPE_DISCOVERY = "adcp_auth_discovery_request"


def _current_routed_tenant_id() -> str | None:
    """Return the tenant selected by subdomain routing, when installed."""
    from adcp.server.tenant_router import current_tenant as routed_tenant  # noqa: PLC0415

    tenant = routed_tenant()
    return tenant.id if tenant is not None else None


def _set_request_state(
    request: Any,
    principal_identity: str | None,
    tenant_id: str | None,
    principal_metadata: dict[str, Any] | None,
) -> None:
    """Write the auth triple onto ``request.state``.

    Defensive: silently no-ops if ``request`` lacks a ``state``
    attribute (e.g., a test double). Real Starlette ``Request`` objects
    always have it.
    """
    state = getattr(request, "state", None)
    if state is None:
        return
    setattr(state, REQUEST_STATE_PRINCIPAL, principal_identity)
    setattr(state, REQUEST_STATE_TENANT, tenant_id)
    setattr(state, REQUEST_STATE_PRINCIPAL_METADATA, principal_metadata)
    setattr(state, REQUEST_STATE_ROUTED_TENANT, _current_routed_tenant_id())


def _read_request_state_auth(
    request: Any,
) -> tuple[str | None, str | None, dict[str, Any] | None] | None:
    """Read the auth triple off ``request.state``, or ``None`` if not set.

    ``None`` means the middleware never ran for this request (e.g., the
    factory was invoked outside an HTTP path) — the caller should fall
    back to the ContextVars.
    """
    state = getattr(request, "state", None)
    if state is None:
        return None
    if not hasattr(state, REQUEST_STATE_PRINCIPAL):
        return None
    return (
        getattr(state, REQUEST_STATE_PRINCIPAL, None),
        getattr(state, REQUEST_STATE_TENANT, None),
        getattr(state, REQUEST_STATE_PRINCIPAL_METADATA, None),
    )


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Starlette HTTP middleware that gates every non-discovery JSON-RPC
    request on a valid bearer token.

    Instantiate via ``app.add_middleware`` with a seller-supplied
    :data:`TokenValidator`::

        app.add_middleware(
            BearerTokenAuthMiddleware,
            validate_token=my_validate_token,
        )

    On success, populates :data:`current_principal`,
    :data:`current_tenant`, and :data:`current_principal_metadata`
    for the duration of the downstream call. On failure, returns
    ``401`` without invoking the handler.

    **Discovery bypass.** ``initialize``, ``notifications/initialized``,
    and ``tools/list`` (MCP handshake) plus ``get_adcp_capabilities``
    (AdCP handshake) are always exempt — these run before any client
    has credentials. Pass ``discovery_tools=`` to widen the
    ``tools/call`` gate beyond the spec default — useful for sellers
    that want product discovery (or other read-only surfaces)
    callable pre-auth without subclassing. Operators who consider
    their tool surface sensitive can still subclass and override
    :meth:`is_discovery_request` to tighten the bypass (e.g. require
    auth on ``tools/list``).

    **Body is peeked, not consumed.** The middleware reads the
    JSON-RPC payload to identify the ``method`` / ``tool`` name for
    the discovery gate; Starlette caches the body on the request so
    handlers still read it normally.

    :param app: The inner ASGI app. Passed by Starlette —
        ``app.add_middleware`` supplies it automatically.
    :param validate_token: Your token lookup. See :data:`TokenValidator`.
    :param unauthenticated_response: Optional override for the 401
        response body. Default is ``{"error": "unauthenticated"}``.
    :param legacy_header_aliases: Optional list of legacy header names
        to accept in addition to the spec-canonical ``Authorization:
        Bearer``. Resolution order on every request: ``Authorization:
        Bearer <token>`` first; if absent, each alias in order; first
        non-empty wins. The aliases path is for adopters mid-migration
        from a custom header (e.g. ``x-adcp-auth``) — both work
        simultaneously so no flag-day cutover is needed. **The
        spec-canonical header is always accepted; aliases are
        purely additive.**
    :param legacy_aliases_bearer_prefix_required: Whether legacy alias
        headers must carry a ``"Bearer "`` prefix. Default ``False``
        — legacy custom-header schemes (``x-adcp-auth: <token>``)
        carry the raw token. RFC 6750 ``Authorization`` always
        requires the prefix regardless of this flag.
    :param header_name: **DEPRECATED.** Set to a custom header name to
        accept that header as a legacy alias. Maps internally to
        ``legacy_header_aliases=[header_name]``. The historical
        EXCLUSIVE behavior — "accept only this header, reject
        Authorization" — was a foot-gun: every spec-compliant client
        (browsers, every off-the-shelf HTTP library, the SDK's
        ``security_baseline/probe_api_key`` probe) sends
        ``Authorization: Bearer`` and was getting 401 against valid
        tokens. The new behavior is ADDITIVE; pass
        ``legacy_header_aliases=[...]`` explicitly for new code.
        See #720.
    :param bearer_prefix_required: **DEPRECATED.** Maps to
        ``legacy_aliases_bearer_prefix_required`` when ``header_name``
        is also set. Ignored when ``header_name`` is the canonical
        ``"authorization"`` (which always requires ``Bearer``).
    :param discovery_tools: Optional override for the per-instance
        ``tools/call`` discovery set. ``None`` (default) keeps the
        spec-mandated default
        (:data:`adcp.server.mcp_tools.DISCOVERY_TOOLS`, currently
        ``{"get_adcp_capabilities"}``). Pass an extended set to
        allow more tools through unauthenticated. Common pattern::

            from adcp.server.mcp_tools import DISCOVERY_TOOLS

            BearerTokenAuthMiddleware(
                app,
                validate_token=...,
                discovery_tools=DISCOVERY_TOOLS | {"get_products"},
            )

        **No validation happens at this layer** — adopters wiring
        the middleware directly are trusted to know what they're
        unauthenticating. The dataclass path
        (:attr:`BearerTokenAuth.mcp_discovery_tools`) runs
        :func:`adcp.server.mcp_tools.validate_discovery_set` for you,
        refusing to add mutating or unknown ADCP tools. **Prefer
        the dataclass path** unless you have a custom non-ADCP
        read-only tool that the spec validator rejects.

        The handshake methods always bypass regardless — this only
        widens ``tools/call``. An empty collection here means
        literally "no tools/call bypasses auth" (the spec default
        is restored only when the kwarg is ``None``); the dataclass
        path rejects empty collections so adopters don't hit this
        accidentally.
    """

    def __init__(
        self,
        app: Any,
        *,
        validate_token: TokenValidator,
        unauthenticated_response: dict[str, Any] | None = None,
        legacy_header_aliases: list[str] | None = None,
        legacy_aliases_bearer_prefix_required: bool = False,
        header_name: str | None = None,
        bearer_prefix_required: bool | None = None,
        discovery_tools: frozenset[str] | None = None,
        allow_unauthenticated: bool = False,
    ) -> None:
        super().__init__(app)
        self._validate_token = validate_token
        self._allow_unauthenticated = allow_unauthenticated
        self._unauth_body = unauthenticated_response or {"error": "unauthenticated"}
        # Per-instance discovery-tool set delivers on the extension hook
        # promised in ``adcp.server.mcp_tools`` (see ``DISCOVERY_TOOLS``
        # docstring): adopters who want product-discovery callable without
        # onboarding pass ``discovery_tools=DISCOVERY_TOOLS | {"get_products"}``
        # without having to subclass and override ``is_discovery_request``.
        # ``None`` preserves the spec-mandated default — a single tool,
        # ``get_adcp_capabilities``, available pre-auth.
        self._discovery_tools = discovery_tools

        # Back-compat shim for ``header_name`` / ``bearer_prefix_required``:
        # the old EXCLUSIVE semantics ("only this header is accepted")
        # broke every spec-compliant client because the middleware
        # silently ignored ``Authorization: Bearer``. The new ADDITIVE
        # model treats a custom ``header_name`` as a legacy alias — the
        # canonical ``Authorization: Bearer`` stays accepted alongside.
        # Emit a deprecation warning so adopters migrate to the explicit
        # ``legacy_header_aliases`` kwarg.
        aliases: list[str] = list(legacy_header_aliases or [])
        alias_prefix_required = legacy_aliases_bearer_prefix_required
        if header_name is not None and header_name.lower() != "authorization":
            warnings.warn(
                "BearerTokenAuthMiddleware(header_name=...) is deprecated. "
                "Pass legacy_header_aliases=[header_name] instead. The new "
                "shape is ADDITIVE — ``Authorization: Bearer`` is always "
                "accepted alongside the alias, fixing the silent-401 bug "
                "spec-compliant clients hit against the old EXCLUSIVE "
                "behavior. See #720.",
                DeprecationWarning,
                stacklevel=2,
            )
            if header_name not in aliases:
                aliases.insert(0, header_name)
            if bearer_prefix_required is not None:
                alias_prefix_required = bearer_prefix_required
        elif bearer_prefix_required is not None:
            warnings.warn(
                "BearerTokenAuthMiddleware(bearer_prefix_required=...) is "
                "deprecated. Pass legacy_aliases_bearer_prefix_required "
                "for alias headers; ``Authorization: Bearer`` always "
                "requires the ``Bearer`` prefix regardless. See #720.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._alias_header_names = tuple(h.lower() for h in aliases)
        self._alias_prefix_required = alias_prefix_required

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        method, tool = await self._peek_jsonrpc(request)
        is_discovery = self.is_discovery_request(method, tool)
        request.scope[REQUEST_SCOPE_DISCOVERY] = is_discovery

        principal_token = None
        tenant_token = None
        metadata_token = None
        try:
            bearer = self._extract_bearer(request)
            if is_discovery and not bearer:
                principal_token = current_principal.set(None)
                tenant_token = current_tenant.set(None)
                metadata_token = current_principal_metadata.set(None)
                _set_request_state(request, None, None, None)
                return await call_next(request)

            if not bearer:
                if self._allow_unauthenticated:
                    # Network-trust deployment: no bearer is expected on this
                    # leg — the agent is reachable only via the host's
                    # authenticated proxy, which propagates identity downstream
                    # (e.g. X-Identity-* / X-Principal-Id). Pass through with no
                    # principal, exactly like the discovery bypass; the app
                    # resolves and enforces identity. A token that IS present but
                    # invalid still falls through to rejection below.
                    principal_token = current_principal.set(None)
                    tenant_token = current_tenant.set(None)
                    metadata_token = current_principal_metadata.set(None)
                    _set_request_state(request, None, None, None)
                    return await call_next(request)
                return self._unauthenticated()

            try:
                raw = self._validate_token(bearer)
                principal: Principal | None
                if inspect.isawaitable(raw):
                    principal = await raw
                else:
                    principal = raw
            except Exception:
                # Validator failure must not leak stack info to the caller.
                # Fail closed — a buggy validator is an auth failure, not a
                # 500. Logged for operators.
                logger.exception("token validator raised")
                return self._unauthenticated()

            if principal is None:
                return self._unauthenticated()

            principal_metadata = dict(principal.metadata) if principal.metadata else None
            principal_token = current_principal.set(principal.caller_identity)
            tenant_token = current_tenant.set(principal.tenant_id)
            metadata_token = current_principal_metadata.set(principal_metadata)
            # Mirror onto ``request.state`` so the dispatch-side
            # ``context_factory`` can read the principal even when the
            # MCP server is in stateful mode (where the session task is a
            # separate async task than this middleware's task and does
            # not see the ContextVar set above). ``request.state`` is the
            # standard Starlette per-request scratchpad and travels with
            # the request through any nested ASGI app.
            _set_request_state(
                request,
                principal.caller_identity,
                principal.tenant_id,
                principal_metadata,
            )
            return await call_next(request)
        finally:
            # Reset unconditionally so a later task sharing this context
            # doesn't read a stale principal. Matches the idempotency
            # store's "fail fast on missing caller_identity" contract.
            if principal_token is not None:
                current_principal.reset(principal_token)
            if tenant_token is not None:
                current_tenant.reset(tenant_token)
            if metadata_token is not None:
                current_principal_metadata.reset(metadata_token)

    def _extract_bearer(self, request: Request) -> str | None:
        """Resolve the token from incoming headers.

        Per RFC 6750 §2.1 the canonical carrier is ``Authorization:
        Bearer <token>``; check that first. If absent, walk the
        configured ``legacy_header_aliases`` in order — first non-empty
        wins. Legacy aliases carry raw tokens (no scheme prefix) unless
        ``legacy_aliases_bearer_prefix_required=True``. Both paths
        coexist so adopters mid-migration can move clients from a
        custom header to ``Authorization: Bearer`` without a flag day
        (#720).
        """
        # 1. Spec-canonical first.
        canonical = request.headers.get("authorization", "")
        bearer = _parse_bearer_header(canonical)
        if bearer:
            return bearer

        # 2. Legacy aliases — additive opt-in.
        for alias in self._alias_header_names:
            raw = request.headers.get(alias, "")
            if not raw:
                continue
            if self._alias_prefix_required:
                token = _parse_bearer_header(raw)
            else:
                token = raw.strip() or None
            if token:
                return token

        return None

    def is_discovery_request(self, method: str | None, tool: str | None) -> bool:
        """True when the request should bypass auth.

        Defaults to the spec-mandated discovery set
        (:data:`adcp.server.mcp_tools.DISCOVERY_TOOLS`, currently
        ``{"get_adcp_capabilities"}``). Two ways to widen the gate:

        1. Pass ``discovery_tools=`` at construction (preferred — no
           subclass needed). The override is per-instance and
           composes with the spec-mandated MCP handshake methods
           (``initialize`` / ``tools/list`` / etc., which always
           bypass regardless). When wiring via :class:`BearerTokenAuth`,
           strongly prefer the dataclass field — it runs
           :func:`adcp.server.mcp_tools.validate_discovery_set` for
           you, refusing to silently unauthenticate mutating tools.
        2. Subclass + override this method when the discovery
           predicate needs to inspect the request more deeply than
           a static tool-name set (e.g. allow ``tools/call`` only
           when accompanied by a specific header).

        Operators who want to *tighten* — e.g. require auth on
        ``tools/list`` — still need to subclass; ``discovery_tools``
        only widens.
        """
        if method in DISCOVERY_METHODS:
            return True
        if method != "tools/call":
            return False
        # Explicit ``is not None`` (not ``or``) so an empty frozenset
        # produced by some upstream filter doesn't silently fall back
        # to the spec default. Empty collections are rejected at the
        # dataclass layer; middleware-direct callers who pass an empty
        # set get the literal "nothing bypasses ``tools/call``"
        # behavior they asked for.
        discovery_tools = (
            self._discovery_tools if self._discovery_tools is not None else DISCOVERY_TOOLS
        )
        return tool in discovery_tools

    def _unauthenticated(self) -> JSONResponse:
        # RFC 6750 §3 + RFC 7235 §3.1 require ``WWW-Authenticate: Bearer``
        # on every 401 from a Bearer-protected resource. Always emit;
        # even when the operator overrides ``unauthenticated_response``,
        # the header stays for protocol compliance. Realm matches the
        # A2A leg (RFC 7235 §2.2 — same protection space).
        return JSONResponse(
            self._unauth_body,
            status_code=401,
            headers={"WWW-Authenticate": _WWW_AUTHENTICATE_CHALLENGE},
        )

    @staticmethod
    async def _peek_jsonrpc(request: Request) -> tuple[str | None, str | None]:
        """Inspect the JSON-RPC body without preventing handlers from
        reading it downstream. Returns ``(method, tool_name)``.

        Explicitly caches the body on ``request._body`` so downstream
        handlers receive the same bytes. Starlette's ``Request`` caches
        the first ``.body()`` call via this attribute, but relying on
        that behavior implicitly is fragile — nested ASGI apps that
        read the raw ``receive`` callable (as FastMCP's streamable-HTTP
        transport does) will otherwise observe an empty body. The
        explicit assignment matches the documented Starlette middleware
        body-peek pattern.

        Fails closed on batch arrays — the JSON-RPC 2.0 spec allows
        them, but the handshake methods never come in batches and
        permitting them here would let a client smuggle a mutation past
        the discovery gate inside a batch.
        """
        body = await request.body()
        # Ensure the body is cached for downstream reads. ``request.body()``
        # already sets ``_body``; the explicit re-assignment is a belt-and-
        # suspenders guard against Starlette internals changing and a
        # pinned target for the body-round-trip test.
        request._body = body
        if not body:
            return None, None
        try:
            payload = json.loads(body)
        except ValueError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        method = payload.get("method")
        method = method if isinstance(method, str) else None
        if method != "tools/call":
            return method, None
        params = payload.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        return method, (name if isinstance(name, str) else None)


# ------------------------------------------------------------------
# Matching context_factory — reads what the middleware populated.
# ------------------------------------------------------------------


def auth_context_factory(meta: RequestMetadata) -> ToolContext:
    """Build a :class:`~adcp.server.ToolContext` from auth state the
    :class:`BearerTokenAuthMiddleware` populated for the in-flight
    request.

    Pass this to :func:`~adcp.server.create_mcp_server` (or
    :func:`~adcp.server.serve`) alongside the middleware so handlers
    receive a typed context carrying the authenticated principal.

    Resolution order:

    1. ``meta.request_context.state`` — the standard Starlette
       per-request scratchpad. Survives the stateful streamable-http
       session-task boundary (the dispatch sub-task gets the originating
       Starlette ``Request`` via the upstream MCP ``request_ctx``
       contextvar). Works on both stateless and stateful streamable-http.
    2. Module-level :data:`current_principal` etc. ContextVars — the
       legacy carrier. Works only when the dispatch runs in the same
       async task as the middleware (i.e., stateless streamable-http
       and A2A). In stateful streamable-http, these read ``None``
       because the session task is a separate task.

    Populates ``caller_identity``, ``tenant_id``, and a ``metadata``
    dict containing the transport + tool name plus anything the
    :class:`Principal` provided. SDK-owned keys (``tool_name``,
    ``transport``) take precedence over principal-supplied keys, so a
    validator returning ``Principal(metadata={"tool_name": "x"})``
    cannot shadow audit fields the SDK populates. Returns a bare
    :class:`ToolContext` — agents that want a typed subclass
    (e.g. :class:`~adcp.server.AccountAwareToolContext`) should copy
    the three-line body and return their own subclass instead.

    Also sets ``metadata["adcp.auth_info"]`` to a typed
    :class:`~adcp.decisioning.AuthInfo` when the request is
    authenticated, so :meth:`~adcp.decisioning.PlatformHandler._extract_auth_info`
    surfaces a non-``None`` :attr:`~adcp.decisioning.RequestContext.auth_info`
    for bearer flows — the same typed surface signed-request flows already
    populate.  ``credential`` is ``None`` for bearer flows because inbound
    bearer tokens are not for upstream propagation; adopters who need
    :class:`~adcp.decisioning.BuyerAgentRegistry` dispatch must supply a
    typed credential in a custom ``context_factory`` subclass.

    ``adcp.auth_info`` is server-internal and never wire-echoed by the
    framework. Do not pass ``ctx.metadata`` wholesale to a JSON serializer
    — the ``AuthInfo`` object is not JSON-serializable.
    """
    principal_identity: str | None = None
    tenant_id: str | None = None
    principal_metadata: dict[str, Any] | None = None
    routed_tenant_id: str | None = None
    if meta.request_context is not None:
        triple = _read_request_state_auth(meta.request_context)
        if triple is not None:
            principal_identity, tenant_id, principal_metadata = triple
        state = getattr(meta.request_context, "state", None)
        if state is not None and hasattr(state, REQUEST_STATE_ROUTED_TENANT):
            routed_tenant_id = getattr(state, REQUEST_STATE_ROUTED_TENANT, None)
    if principal_identity is None and tenant_id is None and principal_metadata is None:
        # Either no Request was threaded (stdio MCP, A2A pre-builder
        # path) or the middleware didn't write to state — fall back to
        # the ContextVars. Works on stateless streamable-http and A2A
        # where dispatch shares the middleware's task context.
        principal_identity = current_principal.get()
        tenant_id = current_tenant.get()
        principal_metadata = current_principal_metadata.get()
    if routed_tenant_id is None:
        # A2A dispatch shares the outer ASGI middleware's ContextVar.  MCP
        # stateful dispatch instead uses the request-state value above.
        routed_tenant_id = _current_routed_tenant_id()
    principal_metadata = principal_metadata or {}
    combined_metadata: dict[str, Any] = {
        **principal_metadata,
        "tool_name": meta.tool_name,
        "transport": meta.transport,
    }
    if principal_identity is not None:
        combined_metadata[AUTHENTICATED_TENANT_METADATA_KEY] = tenant_id
        # Lazy import to keep module-load order safe — decisioning.context
        # imports adcp.server.base but not adcp.server.auth, so there is no
        # circular dependency, but hoisting this to module level would create
        # one if the import graph ever changes. Call-time import matches
        # the pattern already used in dispatch._build_request_context.
        from adcp.decisioning.context import AuthInfo  # noqa: PLC0415

        combined_metadata["adcp.auth_info"] = AuthInfo(
            kind="bearer",
            principal=principal_identity,
            credential=None,  # explicit None: no synthesis, no DeprecationWarning
        )
    if routed_tenant_id is not None:
        combined_metadata[ROUTED_TENANT_METADATA_KEY] = routed_tenant_id
    return ToolContext(
        request_id=meta.request_id,
        caller_identity=principal_identity,
        tenant_id=tenant_id,
        metadata=combined_metadata,
    )


async def enforce_authenticated_tenant(
    _skill_name: str,
    _params: dict[str, Any],
    context: ToolContext,
    call_next: Callable[[], Awaitable[Any]],
) -> Any:
    """Reject a bearer credential used on another routed tenant's host.

    Wire this as :data:`~adcp.server.SkillMiddleware` alongside
    :func:`auth_context_factory` and :class:`SubdomainTenantMiddleware`.
    Skill middleware runs inside both transports' structured-error boundary,
    so ``PERMISSION_DENIED`` has the normal MCP/A2A protocol projection.

    Presence, rather than truthiness, of the authenticated-tenant metadata
    key is load-bearing: an authenticated principal with ``tenant_id=None``
    must not be rebound to whichever host the caller selected.
    """
    metadata = context.metadata or {}
    if (
        AUTHENTICATED_TENANT_METADATA_KEY in metadata
        and ROUTED_TENANT_METADATA_KEY in metadata
        and metadata[AUTHENTICATED_TENANT_METADATA_KEY] != metadata[ROUTED_TENANT_METADATA_KEY]
    ):
        from adcp.decisioning import AdcpError  # noqa: PLC0415

        raise AdcpError(
            "PERMISSION_DENIED",
            message="Bearer credential is not valid for this tenant.",
            recovery="correctable",
        )
    return await call_next()


# ------------------------------------------------------------------
# Helpers sellers sometimes need when building their own validator.
# ------------------------------------------------------------------


def constant_time_token_match(token: str, stored_hashes: Mapping[str, _V]) -> _V | None:
    """Look up a token in a dict of SHA-256 hashes using
    :func:`hmac.compare_digest` rather than dict-containment.

    Dict lookup + equality (``candidate_hash in stored_hashes``) leaks
    prefix-match timing because the hash comparison short-circuits on
    first byte mismatch. Iterating every stored hash with
    ``compare_digest`` makes the wall-clock runtime independent of
    how much of the candidate matches any entry.

    Use this when your token store is small enough to iterate linearly
    (hundreds to low-thousands). For larger stores, use a database
    column of hashed tokens with an equality index + one
    ``compare_digest`` check on the single returned row.

    :param token: Raw bearer token supplied by the client.
    :param stored_hashes: ``{sha256_hex: value}`` dictionary. Returns
        ``value`` on the matching entry, ``None`` on no match.
    """
    if not token:
        return None
    candidate = hashlib.sha256(token.encode()).hexdigest()
    for stored_hash, value in stored_hashes.items():
        if hmac.compare_digest(candidate, stored_hash):
            return value
    return None


def validator_from_token_map(
    token_map: Mapping[str, Principal],
) -> SyncTokenValidator:
    """Build a :data:`TokenValidator` from a ``{raw_token: Principal}`` map.

    The shape most demo/test agents actually need — a fixed set of
    tokens mapped to principals — without having to write the
    constant-time plumbing. The returned validator hashes each raw
    token at construction time and does constant-time lookups via
    :func:`hmac.compare_digest` on every call, matching the security
    properties of a hand-rolled validator::

        validate_token = validator_from_token_map({
            "token-acme": Principal(caller_identity="p-acme", tenant_id="acme"),
            "token-globex": Principal(caller_identity="p-globex", tenant_id="globex"),
        })
        app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)

    Production agents looking tokens up in Postgres / Redis / Vault
    should write their own async validator instead — this helper is
    for the small-fixed-set case (demo, test, CI fixtures).

    :param token_map: Mapping of raw bearer tokens to their resolved
        :class:`Principal`. Tokens are hashed at construction; the
        plaintext is not retained.
    :returns: A :data:`SyncTokenValidator` (which satisfies
        :data:`TokenValidator`).
    """
    stored_hashes: dict[str, Principal] = {
        hashlib.sha256(token.encode()).hexdigest(): principal
        for token, principal in token_map.items()
    }

    def _validate(token: str) -> Principal | None:
        return constant_time_token_match(token, stored_hashes)

    return _validate


# ---------------------------------------------------------------------------
# Cross-transport auth config — drives both MCP middleware and A2A builder
# ---------------------------------------------------------------------------


def _merge_alias_sources(
    cross_aliases: Sequence[str] | None,
    leg_aliases: Sequence[str] | None,
    cross_legacy_header: str | None,
    leg_legacy_header: str | None,
) -> list[str]:
    """Compose the effective legacy-alias list from #720's four sources.

    De-duplicates on lowercased name to keep the wire-check loop tight
    when adopters happen to specify the same header twice (e.g.
    legacy ``mcp_header_name="x-adcp-auth"`` plus new-shape
    ``mcp_legacy_header_aliases=("X-ADCP-Auth",)``). First occurrence
    wins; the preserved casing is **display-only** (the middleware
    lowercases every alias before wire matching at
    :meth:`BearerTokenAuthMiddleware.__init__`). The canonical
    ``authorization`` header is filtered out as belt-and-suspenders —
    ``__post_init__`` already rejects listing it as an alias, this
    catches anything that bypasses validation (e.g. the legacy
    ``header_name`` shim folding values into the resolved list).
    """
    raw: list[str] = []
    for src in (cross_aliases, leg_aliases):
        if src:
            raw.extend(src)
    for legacy in (cross_legacy_header, leg_legacy_header):
        if legacy is not None and legacy.lower() != "authorization":
            raw.append(legacy)
    out: list[str] = []
    seen: set[str] = set()
    for name in raw:
        normalized = name.lower()
        if normalized == "authorization":
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(name)
    return out


@dataclass(frozen=True)
class BearerTokenAuth:
    """Cross-transport bearer-token auth config for :func:`adcp.server.serve`.

    Single source of truth that wires the same ``validate_token``
    callback into both the MCP-side :class:`BearerTokenAuthMiddleware`
    and the A2A-side :class:`A2ABearerAuthMiddleware`. Pass via
    ``serve(auth=BearerTokenAuth(...))`` and both legs are
    authenticated against the same token store with no per-leg
    drift::

        from adcp.server import serve
        from adcp.server.auth import BearerTokenAuth, validator_from_token_map

        serve(
            handler,
            transport="both",
            auth=BearerTokenAuth(
                validate_token=validator_from_token_map({
                    "secret-token": Principal(caller_identity="p", tenant_id="acme"),
                }),
            ),
        )

    On MCP, requests without a valid token receive a JSON ``401``
    body. On A2A, requests without a valid token receive an HTTP
    ``401``. Discovery bypasses are transport-specific:

    * **MCP**: ``initialize`` / ``tools/list`` / ``notifications/initialized``
      / ``get_adcp_capabilities`` (JSON-RPC method-level bypass).
    * **A2A**: well-known agent-card paths, plus message skills selected
      with ``a2a_discovery_skills`` when configured.

    **Canonical carrier: ``Authorization: Bearer <token>`` (RFC 6750).**
    Both legs default to this. It is the only header backed by an actual
    RFC, what every off-the-shelf MCP / A2A / HTTP client emits by
    default, and what the AdCP spec is moving toward as canonical for
    both transports. Reach for ``BearerTokenAuth(validate_token=...)``
    with no other knobs and you get the protocol-canonical setup —
    including a ``bearerAuth`` ``HTTPAuthSecurityScheme`` (``scheme="bearer"``)
    auto-published on the agent card so a2a-sdk-based clients attach
    credentials without seller-side intervention.

    **``x-adcp-auth`` is a legacy-compat alias, not a recommended
    default.** Some early MCP adopters baked in a custom ``x-adcp-auth``
    header carrying a raw token (no scheme prefix) before the spec
    settled. Sellers with deployed clients that can't be updated opt
    in additively — ``Authorization: Bearer`` is still accepted, the
    alias is consulted only when the canonical header is absent::

        # Recommended new-shape (#720). Accepts both wire carriers.
        BearerTokenAuth(
            validate_token=...,
            mcp_legacy_header_aliases=["x-adcp-auth"],   # MCP additive
            # A2A keeps the canonical RFC 6750 carrier by default
        )

    Selecting a non-``Authorization`` header on the A2A leg is
    discouraged — buyers using non-a2a-sdk HTTP clients may not parse
    the resulting :class:`APIKeySecurityScheme` shape, and you lose
    interop with off-the-shelf A2A tooling. Use only when you control
    every buyer client.

    **Legacy single-knob compatibility.** ``header_name`` and
    ``bearer_prefix_required`` are still accepted: when set, they
    apply to *both* legs and override the per-leg defaults. Setting
    both ``header_name`` and a per-leg ``*_header_name`` (or both
    ``bearer_prefix_required`` and a per-leg
    ``*_bearer_prefix_required``) raises at construction — the
    framework can't decide which the operator intended.

    **Widening the MCP discovery gate.** Set ``mcp_discovery_tools``
    to allow extra ``tools/call`` names through unauthenticated. The
    spec default is just ``get_adcp_capabilities``; sellers who want
    product discovery (or other read-only surfaces) callable pre-auth
    extend the set::

        from adcp.server import serve
        from adcp.server.auth import BearerTokenAuth
        from adcp.server.mcp_tools import DISCOVERY_TOOLS

        serve(
            handler,
            transport="both",
            auth=BearerTokenAuth(
                validate_token=...,
                mcp_discovery_tools=DISCOVERY_TOOLS | {"get_products"},
            ),
        )

    ``__post_init__`` runs
    :func:`adcp.server.mcp_tools.validate_discovery_set` on the value —
    adding a mutating tool (``create_media_buy``, ``activate_signal``)
    or an unknown tool name fails loudly at boot rather than silently
    unauthenticating writes. Adopters with custom non-ADCP read-only
    tools that don't pass the spec validator should construct
    :class:`BearerTokenAuthMiddleware` directly with ``discovery_tools=``
    instead — the middleware constructor accepts the same kwarg without
    this stricter check, by design.

    **Widening the A2A discovery gate.** ``a2a_discovery_skills`` is the
    A2A counterpart. It is opt-in because evaluating it requires buffering
    and parsing the JSON-RPC message body. The middleware invokes the same
    ``MessageParser`` as dispatch and caches its result so authorization and
    execution cannot disagree. Messages containing multiple recognizable
    invocations fail closed. The same read-only registry validation runs for
    both fields.
    """

    validate_token: TokenValidator
    # Legacy single-knob — applies to BOTH legs when set. Mutually
    # exclusive with the per-leg knobs below. Adopters who want the
    # canonical RFC 6750 setup should leave these unset (defaults
    # resolve to ``Authorization`` + ``Bearer`` prefix).
    header_name: str | None = None
    bearer_prefix_required: bool | None = None
    # Per-leg knobs — opt-in escape hatch for adopters with legacy
    # clients that send a raw token in a custom header (e.g.
    # ``x-adcp-auth``). The protocol-canonical carrier is
    # ``Authorization: Bearer <token>`` on both legs; reach for these
    # only when you can't update the client side.
    mcp_header_name: str | None = None
    mcp_bearer_prefix_required: bool | None = None
    a2a_header_name: str | None = None
    a2a_bearer_prefix_required: bool | None = None
    unauthenticated_response: dict[str, Any] | None = None
    # NEW (#720) — additive legacy aliases. ``Authorization: Bearer``
    # is ALWAYS accepted regardless of these fields; this is purely
    # additive opt-in for adopters mid-migration from custom headers.
    # Resolution order on each request: ``Authorization: Bearer``
    # first; if absent, each alias in order, first non-empty wins.
    #
    # Pick cross-leg ``legacy_header_aliases`` when both MCP and A2A
    # adopters send the same custom header (most common case during
    # migration). Pick per-leg ``mcp_legacy_header_aliases`` /
    # ``a2a_legacy_header_aliases`` when only one transport had
    # legacy clients (e.g. MCP rolled out earlier, A2A always used
    # the spec carrier). Both can coexist; per-leg values are
    # appended to the cross-leg list during resolution.
    #
    # Typed ``Sequence[str]`` so adopters can pass lists or tuples —
    # ``__post_init__`` rejects bare strings (the trailing-comma
    # tuple foot-gun: ``("x-adcp-auth")`` is a string, not a tuple).
    legacy_header_aliases: Sequence[str] | None = None
    mcp_legacy_header_aliases: Sequence[str] | None = None
    a2a_legacy_header_aliases: Sequence[str] | None = None
    legacy_aliases_bearer_prefix_required: bool = False
    # Network-trust mode (both legs). When True, requests carrying NO bearer
    # token are passed through to the app instead of receiving a 401 — identity
    # is resolved downstream by the host (e.g. from trusted X-Identity-* /
    # X-Principal-Id headers, where the agent is reachable only via the host's
    # authenticated proxy). A token that IS present but invalid is still
    # rejected. Default False preserves bearer-required auth on every request.
    allow_unauthenticated: bool = False
    # MCP-only. Set to widen the unauthenticated tool surface beyond the
    # spec-mandated default (:data:`adcp.server.mcp_tools.DISCOVERY_TOOLS`,
    # i.e. just ``get_adcp_capabilities``). The handshake methods
    # (``initialize``, ``tools/list``, ``notifications/initialized``)
    # always bypass regardless — this only widens the ``tools/call``
    # gate.
    #
    # Typed ``Collection[str]`` (not ``Sequence``) so the canonical
    # ``DISCOVERY_TOOLS | {"get_products"}`` example — which produces
    # a ``frozenset`` — type-checks cleanly. Lists/tuples still work
    # for adopters preferring those.
    #
    # ``__post_init__`` runs :func:`adcp.server.mcp_tools.validate_discovery_set`
    # on every non-empty value here, so adding a mutating tool
    # (``create_media_buy``, ``activate_signal``) or a name that
    # doesn't resolve to a known ADCP tool fails loudly at boot.
    # Adopters with custom non-ADCP read-only tools should construct
    # :class:`BearerTokenAuthMiddleware` directly with
    # ``discovery_tools=`` instead — the middleware constructor
    # accepts the same kwarg without the strictness, the dataclass
    # is the "safe-by-default" path.
    mcp_discovery_tools: Collection[str] | None = None
    # A2A counterpart to ``mcp_discovery_tools``. ``None`` preserves the
    # historical path-only bypass. When configured, bearer-less A2A message
    # requests may invoke exactly one skill from this read-only allowlist.
    a2a_discovery_skills: Collection[str] | None = None

    def __post_init__(self) -> None:
        if self.header_name is not None and (
            self.mcp_header_name is not None or self.a2a_header_name is not None
        ):
            raise ValueError(
                "BearerTokenAuth: set either header_name (applies to both legs) "
                "or mcp_header_name / a2a_header_name (per-leg) — not both."
            )
        if self.bearer_prefix_required is not None and (
            self.mcp_bearer_prefix_required is not None
            or self.a2a_bearer_prefix_required is not None
        ):
            raise ValueError(
                "BearerTokenAuth: set either bearer_prefix_required (applies "
                "to both legs) or mcp_bearer_prefix_required / "
                "a2a_bearer_prefix_required (per-leg) — not both."
            )

        # Reject empty-string headers — they would silently 401 every
        # request because no wire header matches an empty name. A typo
        # like ``header_name=""`` should fail loudly at construction.
        for field_name in ("header_name", "mcp_header_name", "a2a_header_name"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"BearerTokenAuth: {field_name} must be a non-empty string.")

        # ``Authorization`` is reserved by RFC 7235 for ``<scheme>
        # <credentials>``. Carrying a raw token in ``Authorization``
        # breaks RFC-compliant intermediaries and a2a-sdk's auth
        # interceptor (which treats the header as bearer-shaped). If an
        # adopter wants a raw token, they need a custom header name.
        for header_field, prefix_field, leg in (
            ("header_name", "bearer_prefix_required", "both"),
            ("mcp_header_name", "mcp_bearer_prefix_required", "MCP"),
            ("a2a_header_name", "a2a_bearer_prefix_required", "A2A"),
        ):
            header = getattr(self, header_field)
            prefix = getattr(self, prefix_field)
            if header is not None and header.lower() == "authorization" and prefix is False:
                raise ValueError(
                    f"BearerTokenAuth: {header_field}='Authorization' with "
                    f"{prefix_field}=False on the {leg} leg violates RFC 7235 "
                    "(Authorization carries '<scheme> <credentials>'). Use a "
                    "custom header name (e.g. 'x-adcp-auth') for raw-token "
                    "schemes."
                )

        # ``mcp_discovery_tools`` validation: same trailing-comma
        # foot-gun as the alias fields, plus reject empty strings and
        # non-string entries. ``"tools/list"`` and similar handshake
        # methods don't belong here — they're matched by
        # ``DISCOVERY_METHODS`` independently of the tool set; listing
        # them as a "discovery tool" is a no-op and almost always a
        # config error. Reject loudly so the misuse doesn't sit silent.
        if self.mcp_discovery_tools is not None:
            if isinstance(self.mcp_discovery_tools, str):
                raise ValueError(
                    "BearerTokenAuth: mcp_discovery_tools must be a "
                    f"list/tuple/set of tool names, got bare str "
                    f"{self.mcp_discovery_tools!r}. Did you forget the "
                    f"trailing comma? Use "
                    f"``mcp_discovery_tools=({self.mcp_discovery_tools!r},)`` "
                    f"for a single-item tuple."
                )
            # Materialize once — both the per-entry validation below and
            # the readOnly check after iterate the collection; if the
            # adopter passes a generator the second pass would see an
            # empty exhausted iterator and silently skip validation.
            tools_list = list(self.mcp_discovery_tools)
            for name in tools_list:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "BearerTokenAuth: mcp_discovery_tools entries "
                        f"must be non-empty strings, got {name!r}."
                    )
            # Reject the empty-collection case explicitly. ``[]`` is a
            # plausible "disable all bypass" attempt, but the
            # middleware's ``None``-sentinel fallback would silently
            # restore the spec default — surprising and undocumented.
            # An operator who genuinely wants every ``tools/call`` to
            # require auth (including ``get_adcp_capabilities``, which
            # the spec mandates is callable pre-auth) should subclass
            # :class:`BearerTokenAuthMiddleware` and override
            # :meth:`is_discovery_request` — a deliberate spec deviation
            # deserves an explicit code change, not an empty list.
            if not tools_list:
                raise ValueError(
                    "BearerTokenAuth: mcp_discovery_tools is empty. "
                    "Either omit the field (``None`` keeps the spec "
                    "default) or subclass BearerTokenAuthMiddleware and "
                    "override is_discovery_request if you want to "
                    "tighten beyond the spec."
                )
            # Fail-closed safety check: reject mutating or unknown ADCP
            # tools. Adding ``create_media_buy`` to the auth-optional
            # set would silently unauthenticate writes — the exact
            # foot-gun :func:`validate_discovery_set` exists to catch.
            # Adopters with custom non-ADCP read-only tools should
            # construct :class:`BearerTokenAuthMiddleware` directly
            # with ``discovery_tools=``; the middleware constructor
            # accepts the same kwarg without this stricter check, by
            # design.
            from adcp.server.mcp_tools import validate_discovery_set

            validate_discovery_set(tools_list)

        if self.a2a_discovery_skills is not None:
            if isinstance(self.a2a_discovery_skills, str):
                raise ValueError(
                    "BearerTokenAuth: a2a_discovery_skills must be a "
                    f"list/tuple/set of skill names, got bare str "
                    f"{self.a2a_discovery_skills!r}. Did you forget the "
                    "trailing comma?"
                )
            skills_list = list(self.a2a_discovery_skills)
            for name in skills_list:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "BearerTokenAuth: a2a_discovery_skills entries "
                        f"must be non-empty strings, got {name!r}."
                    )
            if not skills_list:
                raise ValueError(
                    "BearerTokenAuth: a2a_discovery_skills is empty. "
                    "Either omit the field (None keeps the path-only "
                    "default) or provide at least one read-only skill."
                )
            # A2A dispatch uses the same ADCP handler method names as MCP's
            # tool registry, so the shared readOnlyHint validation is the
            # authoritative mutation/unknown-name guard for both transports.
            from adcp.server.mcp_tools import validate_discovery_set

            validate_discovery_set(skills_list)

        # #720: validate the new ``*_legacy_header_aliases`` fields
        # at construction so silent misconfig fails loudly.
        for alias_field in (
            "legacy_header_aliases",
            "mcp_legacy_header_aliases",
            "a2a_legacy_header_aliases",
        ):
            value = getattr(self, alias_field)
            if value is None:
                continue
            if isinstance(value, str):
                # Bare string: the trailing-comma tuple foot-gun.
                # ``mcp_legacy_header_aliases="x-adcp-auth"`` is a
                # str, which is iterable letter-by-letter — the
                # resolver would happily treat each letter as a
                # separate alias. Fail loudly instead.
                raise ValueError(
                    f"BearerTokenAuth: {alias_field} must be a list/tuple "
                    f"of header names, got bare str {value!r}. Did you "
                    f"forget the trailing comma? Use "
                    f"``{alias_field}=({value!r},)`` for a single-item "
                    f"tuple, or ``{alias_field}=[{value!r}]`` for a list."
                )
            for name in value:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        f"BearerTokenAuth: {alias_field} entries must be "
                        f"non-empty strings, got {name!r}."
                    )
                if name.lower() == "authorization":
                    # ``Authorization`` is always accepted by the
                    # middleware (the spec-canonical carrier per RFC
                    # 6750). Listing it as a legacy alias is a no-op,
                    # so flagging the misconfig loudly here is
                    # friendlier than silently dropping it.
                    raise ValueError(
                        f"BearerTokenAuth: {alias_field} cannot include "
                        "'authorization' — the spec-canonical header is "
                        "always accepted by the middleware unconditionally. "
                        "Listing it as a legacy alias is a no-op and "
                        "almost always a config error."
                    )

        # #720: deprecate the EXCLUSIVE per-leg / cross-leg header_name
        # kwargs in favor of additive ``*_legacy_header_aliases``.
        # Setting any of them maps the value into the resolved alias
        # list AND keeps ``Authorization: Bearer`` accepted — fixes
        # the silent-401 against spec-compliant clients (security
        # baseline ``probe_api_key`` storyboard).
        _legacy_to_new = {
            "header_name": "legacy_header_aliases",
            "mcp_header_name": "mcp_legacy_header_aliases",
            "a2a_header_name": "a2a_legacy_header_aliases",
        }
        for legacy_field, new_field in _legacy_to_new.items():
            value = getattr(self, legacy_field)
            if value is not None and value.lower() != "authorization":
                warnings.warn(
                    f"BearerTokenAuth({legacy_field}={value!r}) is "
                    "deprecated — the EXCLUSIVE single-header model "
                    "silently rejects spec-compliant clients sending "
                    f"``Authorization: Bearer``. Migrate to "
                    f"``{new_field}=({value!r},)``: the new shape ADDS "
                    "your custom header as an alias while keeping the "
                    "RFC 6750 canonical header accepted. See #720.",
                    DeprecationWarning,
                    stacklevel=3,
                )

    def resolved_mcp_header_name(self) -> str:
        """Effective MCP header name after legacy + default fallback.

        Resolution order: legacy ``header_name`` → ``mcp_header_name``
        → ``"authorization"`` (RFC 6750, the protocol-canonical carrier
        on MCP). Adopters with legacy clients sending ``x-adcp-auth``
        opt in via ``mcp_header_name``; the default itself stays on
        ``Authorization`` because that's what the spec is moving
        toward as canonical.
        """
        if self.header_name is not None:
            return self.header_name
        if self.mcp_header_name is not None:
            return self.mcp_header_name
        return "authorization"

    def resolved_mcp_bearer_prefix_required(self) -> bool:
        """Effective MCP bearer-prefix flag after legacy + default fallback.

        Resolution order: legacy ``bearer_prefix_required`` →
        ``mcp_bearer_prefix_required`` → ``True`` (RFC 6750 — the
        canonical setup is ``Authorization: Bearer <token>``).
        """
        if self.bearer_prefix_required is not None:
            return self.bearer_prefix_required
        if self.mcp_bearer_prefix_required is not None:
            return self.mcp_bearer_prefix_required
        return True

    def resolved_a2a_header_name(self) -> str:
        """Effective A2A header name after legacy + default fallback.

        Resolution order: legacy ``header_name`` → ``a2a_header_name``
        → ``"Authorization"`` (RFC 6750 — what a2a-sdk and every
        off-the-shelf HTTP library send by default). Setting
        ``a2a_header_name`` to anything else is discouraged: buyers
        using non-a2a-sdk HTTP clients may not parse the resulting
        :class:`APIKeySecurityScheme` shape on the agent card and
        you lose interop with off-the-shelf A2A tooling.
        """
        if self.header_name is not None:
            return self.header_name
        if self.a2a_header_name is not None:
            return self.a2a_header_name
        return "Authorization"

    def resolved_a2a_bearer_prefix_required(self) -> bool:
        """Effective A2A bearer-prefix flag after legacy + default fallback.

        Resolution order: legacy ``bearer_prefix_required`` →
        ``a2a_bearer_prefix_required`` → ``True`` (RFC 6750 — the
        canonical setup is ``Authorization: Bearer <token>``).
        """
        if self.bearer_prefix_required is not None:
            return self.bearer_prefix_required
        if self.a2a_bearer_prefix_required is not None:
            return self.a2a_bearer_prefix_required
        return True

    def resolved_mcp_legacy_aliases(self) -> list[str]:
        """Effective MCP legacy-alias list after legacy + new-shape merge.

        Per #720, ``Authorization: Bearer`` is ALWAYS accepted; this
        method returns the additional header names accepted alongside.
        Sources, in order:

        1. ``legacy_header_aliases`` (cross-leg, new-shape).
        2. ``mcp_legacy_header_aliases`` (per-leg, new-shape).
        3. Deprecated ``header_name`` when set to a non-canonical
           value (folded in as an alias for back-compat).
        4. Deprecated ``mcp_header_name`` when set to a non-canonical
           value (same).

        Returns an empty list when the adopter wants spec-canonical
        only (no legacy clients to keep working).
        """
        return _merge_alias_sources(
            self.legacy_header_aliases,
            self.mcp_legacy_header_aliases,
            self.header_name,
            self.mcp_header_name,
        )

    def resolved_a2a_legacy_aliases(self) -> list[str]:
        """Effective A2A legacy-alias list — analog of
        :meth:`resolved_mcp_legacy_aliases` for the A2A leg. See #720."""
        return _merge_alias_sources(
            self.legacy_header_aliases,
            self.a2a_legacy_header_aliases,
            self.header_name,
            self.a2a_header_name,
        )

    def resolved_mcp_discovery_tools(self) -> frozenset[str] | None:
        """Effective MCP discovery-tool set, or ``None`` to use the
        spec-mandated default (:data:`adcp.server.mcp_tools.DISCOVERY_TOOLS`).

        Returns a :class:`frozenset` so the middleware's discovery
        check stays an O(1) membership test, and so the value is
        hashable / safe to share across requests without defensive
        copying. ``None`` means the middleware falls back to the
        module-level default rather than constructing a parallel
        empty set (avoids drift if the spec default ever changes).
        """
        if self.mcp_discovery_tools is None:
            return None
        return frozenset(self.mcp_discovery_tools)

    def resolved_a2a_discovery_skills(self) -> frozenset[str] | None:
        """Effective A2A discovery-skill set, or ``None`` for path-only auth."""
        if self.a2a_discovery_skills is None:
            return None
        return frozenset(self.a2a_discovery_skills)


# ---------------------------------------------------------------------------
# A2A: ASGI middleware that gates JSON-RPC requests, exempts agent-card
# ---------------------------------------------------------------------------
#
# Why an ASGI middleware (not a ServerCallContextBuilder)?
# The a2a-sdk v0.3 compat adapter wraps the entire dispatch in
# ``except Exception`` and converts any error — including a builder-
# raised :class:`HTTPException(401)` — into a 200 OK with a JSON-RPC
# error body. That breaks the spec-canonical HTTP 401 contract and
# leaks the auth path as a 200. Authenticating outside the dispatcher,
# at the ASGI layer, returns proper HTTP 401 every time.
#
# A2A discovery (``/.well-known/agent-card.json``) is exempted by URL
# path here because the agent-card route happens to live in the same
# Starlette app — the middleware can't rely on the route topology
# alone. Path-exemption keeps the spec §4.1 public-discovery mandate
# satisfied even if a future a2a-sdk refactor merges the routes.


# Canonical 1.0 path is sourced from a2a-sdk's own constant — if a
# future a2a-sdk release renames the well-known URI, the import-time
# reference here lifts to the new value automatically and
# ``test_discovery_paths_match_a2a_sdk_routes`` verifies that the
# frozenset still covers every route ``create_agent_card_routes``
# actually registers. Hardcoding the string would silently leak auth
# on the renamed route until someone notices.
from a2a.utils.constants import (  # noqa: E402  (intentional placement after BearerTokenAuth definition)
    AGENT_CARD_WELL_KNOWN_PATH as _A2A_AGENT_CARD_PATH,
)

_A2A_DISCOVERY_PATHS: frozenset[str] = frozenset(
    {
        _A2A_AGENT_CARD_PATH,  # 1.0 canonical: ``/.well-known/agent-card.json``.
        # Legacy 0.3 alias — route registered explicitly in create_a2a_server.
        "/.well-known/agent.json",
    }
)


class A2ABearerAuthMiddleware:
    """Pure-ASGI middleware that gates A2A JSON-RPC on a bearer token.

    Wrap the Starlette app produced by
    :func:`adcp.server.a2a_server.create_a2a_server` with this
    middleware to require a valid bearer header on every JSON-RPC
    request, while leaving the spec-mandated public discovery
    surface (``/.well-known/agent-card.json`` and the 0.3 alias
    ``/.well-known/agent.json``) accessible.

    Designed to compose with a2a-sdk's
    :class:`DefaultServerCallContextBuilder`: on auth success the
    middleware writes a duck-typed user object into
    ``scope['user']`` and the principal into ``scope['auth']``,
    matching Starlette's :class:`AuthenticationMiddleware` contract.
    The default builder reads ``scope['user']`` and adapts it via
    :class:`a2a.server.routes.common.StarletteUser`, so downstream
    handlers see ``ServerCallContext.user.user_name`` populated with
    the principal's ``caller_identity`` without a custom builder.

    Also populates :data:`current_principal`, :data:`current_tenant`,
    and :data:`current_principal_metadata` for the duration of the
    downstream call — symmetric with
    :class:`BearerTokenAuthMiddleware`'s contract. Adopters reading
    ``current_principal.get()`` from a platform method see identical
    state on MCP and A2A.

    Composition order matters when ``transport="both"`` is in play:
    wrap the per-leg apps before any outer dispatcher closes over
    them. See ``serve.py:_build_mcp_and_a2a_app`` for the wiring.

    When ``config.a2a_discovery_skills`` is configured, bearer-less message
    bodies are buffered and parsed with ``message_parser`` (or the executor's
    shared default). The parse result is cached in the ASGI scope and replayed
    downstream. Production ``serve()`` installs the request-size limiter
    outside this middleware, bounding the pre-auth buffer.
    """

    def __init__(
        self,
        app: Any,
        config: BearerTokenAuth,
        *,
        message_parser: Callable[[Any], tuple[str | None, dict[str, Any]]] | None = None,
    ) -> None:
        self._app = app
        self._config = config
        self._header_name = config.resolved_a2a_header_name().lower()
        self._bearer_prefix_required = config.resolved_a2a_bearer_prefix_required()
        self._discovery_skills = config.resolved_a2a_discovery_skills()
        self._message_parser = message_parser

    def _has_bearer(self, scope: Any) -> bool:
        """True if the request carries any non-empty auth header.

        Used only to distinguish "no credential" (pass through under
        ``allow_unauthenticated``) from "credential present but invalid"
        (still rejected). Checks the canonical ``authorization`` header and
        the configured A2A header alias.
        """
        wanted = {"authorization", self._header_name}
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.decode("latin-1").lower() in wanted and raw_value.strip():
                return True
        return False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Lifespan + websocket pass through unchanged. Auth applies to
        # HTTP requests only.
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # CORS preflight is part of the public surface — browser-origin
        # clients send ``OPTIONS`` before any auth'd POST. Returning 401
        # here breaks the preflight and the buyer never gets a chance to
        # retry with a token. Pass through; let the inner app's CORS
        # handler (or operator-supplied ``asgi_middleware``) respond.
        if scope.get("method") == "OPTIONS":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _A2A_DISCOVERY_PATHS:
            await self._app(scope, receive, send)
            return

        # Network-trust: a request with NO bearer is passed through — the host
        # resolves identity downstream from trusted headers (the agent is
        # reachable only via the host's authenticated proxy). A token that IS
        # present but invalid still falls through to rejection below.
        if self._config.allow_unauthenticated and not self._has_bearer(scope):
            await self._app(scope, receive, send)
            return

        # Skill-level discovery is opt-in. Preserve the zero-copy historical
        # path when unset, and never parse an authenticated request merely to
        # authorize it. A supplied-but-invalid credential still receives 401,
        # matching the MCP leg's discovery behavior.
        if self._discovery_skills is not None and not self._has_bearer(scope):
            buffered = await self._buffer_and_parse_discovery(receive)
            if buffered is None:
                return
            parsed, replay_receive = buffered
            skill_name, _params = parsed
            if skill_name is not None and skill_name in self._discovery_skills:
                from adcp.server.a2a_server import _A2A_PARSED_REQUEST_SCOPE_KEY

                scope[_A2A_PARSED_REQUEST_SCOPE_KEY] = parsed
                await self._app(scope, replay_receive, send)
                return
            await self._send_unauthenticated(send)
            return

        principal = self._authenticate_scope(scope)
        if principal is None:
            await self._send_unauthenticated(send)
            return

        # Stash both the duck-typed user (for DefaultServerCallContextBuilder)
        # and the raw Principal (for downstream code reading scope['auth']).
        # Mutating the scope dict before delegating propagates state to
        # nested apps without copying.
        principal_metadata = dict(principal.metadata) if principal.metadata else None
        scope["user"] = _A2AAuthenticatedUser(
            display_name=principal.caller_identity,
            tenant_id=principal.tenant_id,
            principal_metadata=principal_metadata,
        )
        scope["auth"] = principal

        # Populate the same ContextVars MCP's ``BearerTokenAuthMiddleware``
        # sets, so adopters reading ``current_principal.get()`` (or the
        # other two) from a platform method see identical state across
        # transports. Without this, A2A handlers fall through to the
        # ``None`` default while MCP handlers see the principal — a silent
        # transport-coupled divergence that breaks tenant policies that
        # require principal-bound calls. See issue #590.
        #
        # ContextVars carry on the A2A leg because the dispatch runs in
        # the same async task as this middleware (no session-task seam
        # like MCP stateful streamable-http). The MCP leg's mirror onto
        # ``request.state`` is what survives the stateful session-task
        # boundary; A2A's dispatcher reads ContextVars directly. If A2A
        # ever grows a long-lived dispatch task that decouples from the
        # request task, we'll need to thread the request through
        # ``RequestMetadata`` on the A2A side too.
        principal_token = current_principal.set(principal.caller_identity)
        tenant_token = current_tenant.set(principal.tenant_id)
        metadata_token = current_principal_metadata.set(principal_metadata)
        try:
            await self._app(scope, receive, send)
        finally:
            current_principal.reset(principal_token)
            current_tenant.reset(tenant_token)
            current_principal_metadata.reset(metadata_token)

    async def _buffer_and_parse_discovery(
        self, receive: Any
    ) -> tuple[tuple[str | None, dict[str, Any]], Any] | None:
        """Buffer an unauthenticated message and return its replay channel.

        ``None`` means the client disconnected before completing the body. All
        parse and configured-parser failures fail closed at the caller.
        """
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return None
            if message_type != "http.request":
                logger.debug(
                    "unexpected ASGI message type %r in A2A auth middleware",
                    message_type,
                )
                continue
            chunks.append(message.get("body", b""))
            more_body = bool(message.get("more_body", False))

        from adcp.server._size_limit import make_replay_receive
        from adcp.server.a2a_server import parse_a2a_jsonrpc_skill

        try:
            payload = json.loads(b"".join(chunks))
            parsed = parse_a2a_jsonrpc_skill(payload, self._message_parser)
        except Exception:
            # Keep this coarse: parser/validation exceptions may embed the
            # request payload, including skill names, in their text.
            logger.info("a2a discovery parse rejected", extra={"reason": "invalid_message"})
            parsed = (None, {})
        return parsed, make_replay_receive(chunks)

    def _authenticate_scope(self, scope: Any) -> Principal | None:
        """Read + validate the bearer header off raw ASGI scope.

        Validator exceptions are projected to :data:`None` (logged for
        operators) so a buggy validator never leaks 500-level stack
        traces or signals path existence to unauthenticated callers.
        Auth-rejection branches log at INFO with a coarse reason code
        so SOC dashboards can detect scanning without bloating logs.
        """
        # ASGI ``headers`` is a list of ``(bytes_lower, bytes)`` tuples.
        target = self._header_name.encode("latin-1")
        raw_value: bytes | None = None
        for name, value in scope.get("headers", ()):
            if name == target:
                raw_value = value
                break

        if raw_value is None:
            logger.info("a2a auth rejected", extra={"reason": "missing_header"})
            return None

        try:
            raw_header = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            logger.info("a2a auth rejected", extra={"reason": "header_decode"})
            return None

        if self._bearer_prefix_required:
            bearer = _parse_bearer_header(raw_header)
        else:
            stripped = raw_header.strip()
            bearer = stripped or None
        if not bearer:
            logger.info("a2a auth rejected", extra={"reason": "wrong_scheme"})
            return None

        try:
            raw = self._config.validate_token(bearer)
        except Exception:
            logger.exception("token validator raised on A2A request")
            return None

        if inspect.isawaitable(raw):
            # Should be unreachable — :func:`_assert_sync_validator` at
            # config time rejects async validators before any traffic
            # lands. This branch is the in-depth catch in case an
            # adopter swaps in an async validator at runtime via a
            # closure that conditionally awaits.
            logger.error(
                "a2a auth rejected: validator returned awaitable at request "
                "time. Async validators are not supported on the A2A leg; "
                "wrap with a sync bridge."
            )
            return None

        if raw is None:
            logger.info("a2a auth rejected", extra={"reason": "invalid_token"})
            return None
        return raw

    async def _send_unauthenticated(self, send: Any) -> None:
        body_obj = self._config.unauthenticated_response or {
            "error": "invalid_token",
            "error_description": "Bearer token missing or invalid",
            "code": "AUTH_REQUIRED",
        }
        body = json.dumps(body_obj).encode("utf-8")
        # RFC 6750 §3 + RFC 7235 §3.1 require ``WWW-Authenticate: Bearer``
        # on every 401. Shared constant with the MCP leg (RFC 7235 §2.2
        # — same protection space).
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"www-authenticate", _WWW_AUTHENTICATE_CHALLENGE.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@dataclass(frozen=True)
class _A2AAuthenticatedUser:
    """Minimal Starlette-BaseUser-shaped object for :class:`StarletteUser`.

    a2a-sdk's :class:`StarletteUser` adapter wants ``is_authenticated``
    (bool) and ``display_name`` (str). It doesn't import Starlette's
    :class:`BaseUser` directly — duck-typing works. We synthesize a
    frozen dataclass so the principal's identity flows through with no
    Starlette dependency on the auth side.
    """

    display_name: str
    tenant_id: str | None = None
    principal_metadata: dict[str, Any] | None = None

    @property
    def is_authenticated(self) -> bool:
        return True
